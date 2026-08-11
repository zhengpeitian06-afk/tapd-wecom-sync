#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TAPD 需求拉取脚本（服务器版，不依赖 WorkBuddy / MCP）。

直接调用 TAPD OpenAPI（https://api.tapd.cn），使用 Basic Auth 或 Bearer Token。
拉取所有配置中的 TAPD 项目的 stories，并额外补全两个开发期遗漏的关键字段：

  1) 需求申请人员（实际提出需求的业务人员）
     - TAPD 中它是一个「自定义字段」，且不同项目用的 custom_field_* 不同（与 OA 需求单号同理）。
     - 本脚本对每个项目调用 get_fields_lable 自动定位该字段的 key，再把值挂到每条需求的
       `_applicant` 上；也支持在 project_oa_config.json 里用 project_overrides[pid].applicant_field 写死。

  2) 状态变更时间（最近一次「状态」字段变更的时间，≠ 最后修改）
     - `modified` 是需求任意字段最后一次编辑时间，并不等于状态变更时间。
     - 本脚本对每条需求调用 story_changes（change_field=status，取最新一条）拿到真正的状态变更时间，
       挂到 `_status_change_time`；用 status_change_cache.json 缓存，仅状态变化的那部分会重新请求。

输出 JSON 供 incremental_sync.py 消费。

鉴权（二选一，通过环境变量）：
  - 基础账号（推荐）：TAPD_API_USER / TAPD_API_PASSWORD  （TAPD 后台「API 账号」）
  - 开放平台 Token：  TAPD_TOKEN                        （Bearer）

用法：
  python3 tapd_fetcher.py [--config project_oa_config.json] [--output /tmp/tapd_current_oa.json]
                          [--no-discover] [--no-status-time]
"""
import json
import os
import sys
import time
import base64
import argparse
import ssl
import urllib.request
import urllib.error
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))

# 拉取过程中的失败计数：任何一次请求失败都会 +1。
# incremental_sync.py 会读取输出 JSON 里的 fetch_errors，若 >0 则拒绝执行删除，
# 避免「拉取失败 → 数据变少 → 误删企微记录」这类灾难性后果。
FETCH_ERRORS = []

# 标准字段（同步表需要的）+ 全量自定义字段（确保「需求申请人员」所在的 custom_field_* 一定被拉到）
STANDARD_FIELDS = ["id", "name", "status", "priority", "priority_label",
                   "owner", "creator", "created", "modified"]
# 注意：TAPD OpenAPI 的自定义字段 key 命名不一致——OA 需求单号用单词形式(custom_field_six)，
# 而需求申请人员用数字编号(custom_field_9/13/14)。为稳妥，两种形式都请求。
CUSTOM_WORDS = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
                "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
                "seventeen", "eighteen", "nineteen", "twenty"]
CUSTOM_FIELDS = [f"custom_field_{i}" for i in range(1, 61)] + [f"custom_field_{w}" for w in CUSTOM_WORDS]

# 数字编号 <-> 英文单词 互转（TAPD 字段键两种形式混用，读取时双向兜底）
NUM_WORD = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven",
            8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen",
            14: "fourteen", 15: "fifteen", 16: "sixteen", 17: "seventeen", 18: "eighteen",
            19: "nineteen", 20: "twenty"}
WORD_NUM = {v: k for k, v in NUM_WORD.items()}


def _field_val(story, key):
    """读取自定义字段值，数字/单词两种键形式都尝试。"""
    v = (story.get(key) or "").strip()
    if v:
        return v
    if isinstance(key, str) and key.startswith("custom_field_"):
        rest = key[len("custom_field_"):]
        alt = None
        if rest.isdigit():
            w = NUM_WORD.get(int(rest))
            if w:
                alt = "custom_field_" + w
        else:
            n = WORD_NUM.get(rest)
            if n:
                alt = "custom_field_" + str(n)
        if alt and alt != key:
            return (story.get(alt) or "").strip()
    return ""
FIELDS = ",".join(STANDARD_FIELDS + CUSTOM_FIELDS)

PER_PAGE = 200
API_BASE = os.environ.get("TAPD_API_ENDPOINT", "https://api.tapd.cn").rstrip("/")

# 需求申请人员 字段标签可能包含的关键词（越长越优先，避免误匹配「申请部门」之类）
APPLICANT_KEYWORDS = [
    "需求申请人员", "需求申请人", "申请人员", "需求提出人", "提出人",
    "申请人", "需求人", "业务人员", "业务方",
]
FIELD_DISCOVERY_FILE = os.path.join(HERE, "field_discovery.json")
STATUS_TIME_CACHE_FILE = os.path.join(HERE, "status_change_cache.json")


def log(msg):
    print(f"[tapd] {msg}", flush=True)


def build_auth_header():
    token = os.environ.get("TAPD_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}
    user = os.environ.get("TAPD_API_USER")
    pwd = os.environ.get("TAPD_API_PASSWORD")
    if not user or not pwd:
        raise RuntimeError(
            "缺少 TAPD 凭据：请设置 TAPD_TOKEN，或同时设置 TAPD_API_USER / TAPD_API_PASSWORD"
        )
    raw = f"{user}:{pwd}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


# 全局限速：两次请求之间至少间隔 RATE_INTERVAL 秒，避免触发 TAPD 429 限流
RATE_INTERVAL = 1.2
_last_req = [0.0]


def _throttle():
    now = time.time()
    delta = RATE_INTERVAL - (now - _last_req[0])
    if delta > 0:
        time.sleep(delta)
    _last_req[0] = time.time()


def _build_ssl_context():
    """构建可用的 SSL 上下文。

    macOS 上用 python.org 安装的 Python.framework 默认不带根证书，会报
    CERTIFICATE_VERIFY_FAILED（未运行 Install Certificates.command）。
    依次尝试：SSL_CERT_FILE 环境变量 → certifi → 系统 /etc/ssl/cert.pem。
    """
    ctx = ssl.create_default_context()
    if os.environ.get("SSL_CERT_FILE"):
        return ctx
    try:
        ctx.load_verify_locations(cafile=__import__("certifi").where())
        return ctx
    except Exception:
        pass
    for ca in ("/etc/ssl/cert.pem", "/usr/local/etc/openssl@3/cert.pem"):
        if os.path.exists(ca):
            try:
                ctx.load_verify_locations(cafile=ca)
                return ctx
            except Exception:
                continue
    return ctx


SSL_CTX = _build_ssl_context()


def http_get(url, headers, retries=6):
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(retries):
        _throttle()
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                ra = e.headers.get("Retry-After") if hasattr(e, "headers") else None
                try:
                    wait = float(ra) if ra else 2 ** (attempt + 2)
                except (TypeError, ValueError):
                    wait = 2 ** (attempt + 2)
                wait = min(wait, 30)
                log(f"HTTP {e.code}，第 {attempt+1} 次重试，等待 {wait}s")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("TAPD 请求多次失败")


# ---------- 自定义字段发现 ----------
def parse_field_labels(data):
    """把 get_fields_lable 的返回解析成 {字段key: 中文标签}。结构不确定时尽量兼容。"""
    labels = {}
    d = data.get("data") if isinstance(data, dict) else None
    if not d:
        return labels
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, dict):
                labels[k] = v.get("name") or v.get("label") or v.get("text") or ""
            elif isinstance(v, str):
                labels[k] = v
    elif isinstance(d, list):
        for item in d:
            if isinstance(item, dict):
                k = item.get("field_key") or item.get("key") or item.get("field_name") or ""
                if k:
                    labels[k] = item.get("name") or item.get("label") or item.get("text") or ""
    return labels


def find_applicant_field(label_map):
    """在自定义字段里找标题最匹配『需求申请人员』的 key。"""
    best, best_score = None, 0
    for key, label in label_map.items():
        if not str(key).startswith("custom_field"):
            continue
        label = label or ""
        for kw in APPLICANT_KEYWORDS:
            if kw in label:
                score = len(kw)
                if score > best_score:
                    best_score, best = score, key
                break
    return best


def load_json_file(path, default):
    if os.path.exists(path):
        try:
            return json.load(open(path, "r", encoding="utf-8"))
        except Exception:
            return default
    return default


def discover_applicant_field(workspace_id, auth_header, config, discovery_cache):
    """返回该项目 需求申请人员 对应的 custom_field key（优先配置写死 > 已缓存 > 自动发现）。"""
    override = config.get("project_overrides", {}).get(str(workspace_id), {}).get("applicant_field")
    if override:
        return override
    if workspace_id in discovery_cache and discovery_cache[workspace_id].get("applicant_field"):
        return discovery_cache[workspace_id]["applicant_field"]
    try:
        url = f"{API_BASE}/stories/get_fields_lable?workspace_id={workspace_id}"
        data = http_get(url, auth_header)
        labels = parse_field_labels(data)
        key = find_applicant_field(labels)
        log(f"项目 {workspace_id} 字段标签映射（部分）：" +
            ", ".join(f"{k}={v}" for k, v in labels.items() if str(k).startswith("custom_field"))[:500])
        discovery_cache[workspace_id] = {"applicant_field": key,
                                         "labels": labels if len(json.dumps(labels)) < 20000 else {}}
        return key
    except Exception as e:
        log(f"项目 {workspace_id} 字段发现失败：{e}")
        discovery_cache[workspace_id] = {"applicant_field": None}
        return None


# ---------- 状态变更时间 ----------
def normalize_change(item):
    if isinstance(item, dict) and "StoryChange" in item:
        return item["StoryChange"]
    if isinstance(item, dict) and "story_change" in item:
        return item["story_change"]
    return item


def fetch_status_change_time(workspace_id, story_id, auth_header):
    """取该需求最近一次『状态』字段变更的时间。无状态变更记录时返回 None。"""
    params = (f"workspace_id={workspace_id}&story_id={story_id}"
              f"&change_field=status&order={urllib.parse.quote('created desc')}&limit=1")
    url = f"{API_BASE}/story_changes?{params}"
    try:
        data = http_get(url, auth_header)
    except Exception as e:
        log(f"状态变更时间获取失败 {story_id}: {e}")
        return None
    if not data or data.get("status") != 1:
        return None
    items = data.get("data") or []
    if items:
        ch = normalize_change(items[0])
        return ch.get("created") or ch.get("created_time") or None
    return None


def _change_touches_status(ch):
    """判断该变更事件是否真的包含『状态』字段的改动。"""
    fc = ch.get("field_changes")
    if isinstance(fc, list):
        if any((f.get("field") == "status") for f in fc if isinstance(f, dict)):
            return True
    raw = ch.get("changes") or ""
    return "\"field\":\"status\"" in raw or '"field": "status"' in raw


def fetch_status_times_bulk(workspace_id, story_ids, auth_header, batch=40):
    """批量获取一批需求最近一次『状态』变更时间。

    通过 story_changes 的 story_id 多值（逗号分隔）一次性查询，
    仅返回触及 status 字段的变更事件，按 created desc 排序，
    取每个需求最新的那条作为其状态变更时间。把 361 次调用降为 ~8 次/项目。
    返回 {story_id: 状态变更时间字符串}。
    """
    result = {}
    uniq = list(dict.fromkeys([s for s in story_ids if s]))
    if not uniq:
        return result
    for i in range(0, len(uniq), batch):
        chunk = uniq[i:i + batch]
        params = (
            f"workspace_id={workspace_id}"
            f"&story_id={urllib.parse.quote(','.join(chunk))}"
            f"&change_field=status&order={urllib.parse.quote('created desc')}&limit={batch}"
        )
        url = f"{API_BASE}/story_changes?{params}"
        try:
            data = http_get(url, auth_header)
        except Exception as e:
            log(f"批量状态变更时间获取失败(项目 {workspace_id} 第 {i // batch + 1} 批)：{e}")
            continue
        if not data or data.get("status") != 1:
            continue
        for it in (data.get("data") or []):
            ch = it.get("StoryChange") or it.get("WorkitemChange") or it
            sid = str(ch.get("story_id") or "")
            created = ch.get("created") or ""
            if sid and _change_touches_status(ch):
                if sid not in result or (created and created > result[sid]):
                    result[sid] = created
    return result


# ---------- 拉取 stories ----------
def normalize_story(item):
    if isinstance(item, dict) and "Story" in item:
        return item["Story"]
    if isinstance(item, dict) and "story" in item:
        return item["story"]
    return item


def fetch_project_stories(workspace_id, auth_header):
    stories = []
    page = 1
    while True:
        params = (
            f"workspace_id={workspace_id}&limit={PER_PAGE}&page={page}"
            f"&fields={urllib.parse.quote(FIELDS)}"
        )
        url = f"{API_BASE}/stories?{params}"
        try:
            data = http_get(url, auth_header)
        except Exception as e:
            log(f"项目 {workspace_id} 第 {page} 页请求失败：{e}")
            FETCH_ERRORS.append(f"{workspace_id}#p{page}: {e}")
            break
        if not data or data.get("status") != 1:
            info = data.get("info") if data else "无响应"
            log(f"项目 {workspace_id} 返回异常（status={data.get('status') if data else 'NA'}）：{info}")
            FETCH_ERRORS.append(f"{workspace_id}#p{page}: status异常 {info}")
            break
        items = data.get("data") or []
        if not items:
            break
        for it in items:
            s = normalize_story(it)
            s["_project_id"] = str(workspace_id)
            stories.append(s)
        if len(items) < PER_PAGE:
            break
        page += 1
        time.sleep(0.2)
    return stories


def _has_oa(story, workspace_id, config):
    """该需求是否含 OA 需求单号（即我们同步范围内）。"""
    override = config.get("project_overrides", {}).get(str(workspace_id), {})
    field = override.get("oa_field", "custom_field_six")
    return bool(_field_val(story, field))


def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--config", default=os.path.join(here, "project_oa_config.json"))
    ap.add_argument("--output", default="/tmp/tapd_current_oa.json")
    ap.add_argument("--no-discover", action="store_true", help="跳过需求申请人员字段自动发现")
    ap.add_argument("--no-status-time", action="store_true", help="不拉取状态变更时间")
    ap.add_argument("--project-ids", default="", help="仅处理指定项目ID（逗号分隔），用于限定范围")
    args = ap.parse_args()

    config = json.load(open(args.config, "r", encoding="utf-8"))
    projects = config.get("projects", [])
    if args.project_ids:
        ids = [x.strip() for x in args.project_ids.split(",") if x.strip()]
        projects = [p for p in projects if str(p["id"]) in ids]
    if not projects:
        log("配置中无项目列表")
        sys.exit(1)

    auth_header = build_auth_header()
    discovery_cache = load_json_file(FIELD_DISCOVERY_FILE, {})
    status_cache = load_json_file(STATUS_TIME_CACHE_FILE, {})

    all_stories = []
    for p in projects:
        pid = p["id"]
        pname = p.get("name", pid)

        applicant_field = None
        if not args.no_discover:
            applicant_field = discover_applicant_field(pid, auth_header, config, discovery_cache)
            log(f"项目 {pname}({pid}) 需求申请人员字段 = {applicant_field or '（未找到，留空）'}")
            time.sleep(0.1)

        try:
            got = fetch_project_stories(pid, auth_header)
        except Exception as e:
            log(f"项目 {pname}({pid}) 拉取异常：{e}")
            got = []

        for s in got:
            # 1) 需求申请人员（循环内直接赋值）
            s["_applicant"] = _field_val(s, applicant_field) if applicant_field else ""

        # 2) 状态变更时间：批量拉取本项目含 OA 单号的需求（大幅降低调用次数，规避 429）
        if args.no_status_time:
            for s in got:
                s["_status_change_time"] = ""
        else:
            oa_stories = [s for s in got if _has_oa(s, pid, config)]
            bulk = fetch_status_times_bulk(pid, [s.get("id", "") for s in oa_stories], auth_header)
            for s in got:
                sid = s.get("id", "")
                if not _has_oa(s, pid, config):
                    s["_status_change_time"] = ""
                    continue
                cached = status_cache.get(sid)
                if cached and cached.get("status") == s.get("status") and cached.get("time"):
                    s["_status_change_time"] = cached["time"]
                else:
                    t = bulk.get(sid) or (s.get("created") or "")[:19]  # 无状态变更记录 → 退回创建时间
                    s["_status_change_time"] = t
                    status_cache[sid] = {"status": s.get("status"), "time": t}
            # 增量持久化，避免中途被杀丢失进度
            json.dump(status_cache, open(STATUS_TIME_CACHE_FILE, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)

        log(f"项目 {pname}({pid})：拉取 {len(got)} 条（含OA {len(oa_stories) if not args.no_status_time else 0} 条已取状态变更时间）")
        all_stories.extend(got)

    # 持久化发现结果与状态变更缓存
    json.dump(discovery_cache, open(FIELD_DISCOVERY_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    json.dump(status_cache, open(STATUS_TIME_CACHE_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    out = {
        "stories": all_stories,
        "fetch_errors": len(FETCH_ERRORS),
        "fetch_error_detail": FETCH_ERRORS[:20],
        "projects_queried": len(projects),
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    log(f"共拉取 {len(all_stories)} 条需求，已写入 {args.output}")
    if FETCH_ERRORS:
        log(f"⚠️ 本次有 {len(FETCH_ERRORS)} 次请求失败，数据不完整——下游同步将拒绝执行删除操作。")
        sys.exit(2)


if __name__ == "__main__":
    main()
