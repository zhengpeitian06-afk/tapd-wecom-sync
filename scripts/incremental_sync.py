#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TAPD -> 企微智能表格 增量同步核心脚本（服务器版）

工作流：
1. 读取 TAPD 原始需求数据（由 tapd_fetcher.py 拉取后写入 input JSON）
2. 按各项目配置过滤含「OA需求单号」的需求，并用 per-project status_mappings 翻译状态
3. 与本地缓存 wecom_sync_cache.json（含 record_id）做 diff
4. 仅对【新增 / 变更】记录写企微（add / update），对【已从 TAPD 移除】的记录做 delete
5. 更新本地缓存

完全不依赖企微读权限（851008）——record_id 持久化在本地缓存中。

依赖：仅标准库 + 同目录 wecom_smartsheet.py（同样仅标准库）。
用法：
  python3 incremental_sync.py <current_input_json> [--config ...] [--cache ...] [--dry-run]
"""
import json
import os
import sys
import re
import argparse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from wecom_smartsheet import (WeComSmartsheetClient, ensure_table, ensure_columns,
                              to_ms_timestamp, cell_text, WeComError)  # noqa: E402

# 注意：顺序需与 wecom_smartsheet.COLUMNS 保持一致（仅影响日志/可读性与初始建表列序）
FIELDS = ["OA需求单号", "需求标题", "状态", "优先级", "处理人", "创建人", "需求申请人员",
          "所属项目", "创建时间", "最后修改", "状态变更时间", "TAPD需求ID"]
TS_RE = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}")

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


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_oa_number(story, project_id, config):
    """按项目配置提取 OA 需求单号，过滤掉时间戳等无效值。"""
    overrides = config.get("project_overrides", {}).get(project_id, {})
    oa_field = overrides.get("oa_field", config.get("default_oa_field", "custom_field_six"))
    oa_field_alt = overrides.get("oa_field_alt")
    val = _field_val(story, oa_field)
    if TS_RE.match(val):
        val = ""
    if not val and oa_field_alt:
        alt_val = _field_val(story, oa_field_alt)
        if alt_val and not TS_RE.match(alt_val):
            val = alt_val
    return val


def map_status(status_code, project_id, config):
    mapping = config.get("status_mappings", {}).get(project_id, {})
    return mapping.get(status_code, status_code)


def build_values(story, project_id, config, project_name):
    oa = extract_oa_number(story, project_id, config)
    status_label = map_status(story.get("status", ""), project_id, config)
    priority = story.get("priority_label") or story.get("priority", "") or ""
    owner = (story.get("owner", "") or "").rstrip(";").strip()
    creator = story.get("creator", "") or ""
    applicant = (story.get("_applicant") or story.get("applicant") or "").strip()
    created = (story.get("created", "") or "")[:10]
    modified = (story.get("modified", "") or "")[:10]
    status_change = (story.get("_status_change_time") or story.get("status_change_time") or "")
    return {
        "OA需求单号": [{"type": "text", "text": oa}],
        "需求标题": [{"type": "text", "text": story.get("name", "")}],
        "状态": [{"type": "text", "text": status_label}],
        "优先级": [{"type": "text", "text": priority}],
        "处理人": [{"type": "text", "text": owner}],
        "创建人": [{"type": "text", "text": creator}],
        "需求申请人员": [{"type": "text", "text": applicant}],
        "所属项目": [{"type": "text", "text": project_name}],
        "创建时间": to_ms_timestamp(created),
        "最后修改": to_ms_timestamp(modified),
        "状态变更时间": to_ms_timestamp(status_change),
        "TAPD需求ID": [{"type": "text", "text": story.get("id", "")}],
        "最后同步时间": [{"type": "text", "text": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}],
    }


def build_wecom_client(config, log=print):
    """根据环境变量构建企微客户端；WECOM_MOCK=1 时返回内存 Mock（仅供测试）。"""
    if os.environ.get("WECOM_MOCK") == "1":
        return MockWeComClient(log=log)
    corpid = os.environ.get("WECOM_CORPID") or config.get("wecom_corpid")
    corpsecret = os.environ.get("WECOM_CORPSECRET") or config.get("wecom_corpsecret")
    if corpid and corpsecret:
        return WeComSmartsheetClient(corpid, corpsecret, log=log)
    # 无自建应用凭据时，回退到本机 WorkBuddy 企微连接器的 wecom-cli（机器人身份）
    try:
        from wecomcli_client import WeComCliClient, cli_available
        if cli_available():
            log("[client] 未配置 WECOM_CORPID/CORPSECRET，改用本机 wecom-cli（企微连接器机器人身份）")
            return WeComCliClient(log=log)
    except ImportError:
        pass
    raise RuntimeError(
        "缺少企微凭据：请设置 WECOM_CORPID / WECOM_CORPSECRET（自建应用），"
        "或确保本机可用 wecom-cli（WorkBuddy 企微连接器）"
    )


class MockWeComClient:
    """用于本地 pipeline 验证，不发起真实网络请求。"""

    def __init__(self, log=print):
        self._seq = 1000
        self.created = None
        self.log = log

    def create_doc(self, doc_name):
        self._seq += 1
        return f"MOCKDOC{self._seq}", "https://mock/doc", "MOCKSCODE"

    def get_sheet(self, docid):
        return [{"sheet_id": "MOCKSHEET1", "title": "sheet1"}]

    def get_fields(self, docid, sheet_id):
        return [{"field_id": "f_default", "field_title": "文本", "field_type": "FIELD_TYPE_TEXT"}]

    def rename_field(self, docid, sheet_id, field_id, title):
        pass

    def add_fields(self, docid, sheet_id, fields):
        pass

    def add_records(self, docid, sheet_id, values_list):
        return [f"rid_{self._seq + i}" for i in range(len(values_list))]

    def update_records(self, docid, sheet_id, records):
        pass

    def delete_records(self, docid, sheet_id, record_ids):
        pass


def reconcile_cache(client, docid, sheet_id, cache, log=print):
    """读取智能表格现有记录，按 TAPD需求ID 把 record_id 及关键字段回填到缓存。

    用于「复用已有表（如 augJOD）」场景：开发期数据由 WorkBuddy 机器人写入，
    服务器身份（自建应用）首次运行缓存为空/不全，直接跑会把已有行当「新增」重复写。
    先读回全表、按 TAPD需求ID 对齐 record_id，即可 0 重复并支持后续增量更新。
    要求自建应用对目标表拥有【读】权限；若无读权限会明确报错而非静默重复写入。
    """
    try:
        records = client.get_records(docid, sheet_id)
    except WeComError as e:
        raise WeComError(
            f"读取智能表格失败（请确认自建应用已加入该表「可调用应用」且拥有读权限）：{e}"
        )
    before = len(cache)
    for r in records:
        vals = r.get("values") or {}
        tapd_id = cell_text(vals.get("TAPD需求ID")).strip()
        if not tapd_id:
            continue
        cache[tapd_id] = {
            "record_id": r.get("record_id"),
            "oa_number": cell_text(vals.get("OA需求单号")),
            "status_label": cell_text(vals.get("状态")),
            "modified": cell_text(vals.get("最后修改")),
            "applicant": cell_text(vals.get("需求申请人员")),
            "status_change": cell_text(vals.get("状态变更时间")),
            "name": cell_text(vals.get("需求标题")),
        }
    log(f"[reconcile] 从智能表格读回 {len(records)} 条记录，缓存由 {before} → {len(cache)} 条")
    return cache


def stamp_sync_time(client, docid, sheet_id, cache, log=print):
    """运行心跳：把全部记录的『最后同步时间』列刷成当前时间（文本格式）。

    用途：Rainbond 日志 Tab 抓不到纯 cron 容器的 stdout、Web 终端又连不上时，
    用户无法看到任何运行证据。此函数在每次同步（无论是否有数据变更）后刷新整表
    该列为当前时间文本，用户打开智能表格即可肉眼确认定时任务在跑；配合日志中的
    [diff] 新增/更新/移除计数即可区分「在跑但无变更」与「真有数据变更」。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    recs = []
    for tid, c in cache.items():
        rid = c.get("record_id")
        if rid:
            recs.append({"record_id": rid, "values": {"最后同步时间": now}})
    if not recs:
        return
    for i in range(0, len(recs), 100):
        batch = recs[i:i + 100]
        try:
            client.update_records(docid, sheet_id, batch)
        except Exception as e:
            log(f"[stamp] 心跳标记失败（批次 {i}）：{str(e)[:160]}")
            return
    log(f"[stamp] 已刷新 {len(recs)} 行『最后同步时间』= {now}")


def guard_deletions(removed_ids, cache_ids, current_ids, fetch_errors, force, log=print):
    """删除护栏：判断本次是否允许执行删除。

    真实事故场景：TAPD 拉取因 SSL/网络/限流失败 → 输入只有 0 条 → diff 认为
    全部记录都被移除 → 把企微表 231 行全删光。因此删除必须满足全部条件：
      1) 本次拉取无任何失败（fetch_errors == 0）
      2) 当前数据非空
      3) 删除量不超过缓存的 20%
    不满足则本轮跳过删除（新增/更新照常），下轮数据正常时自然收敛。--force 可强制。
    """
    if not removed_ids:
        return True, ""
    if force:
        return True, "--force 强制执行删除"
    if fetch_errors:
        return False, f"本次 TAPD 拉取有 {fetch_errors} 次失败，数据不完整，拒绝删除"
    if not current_ids:
        return False, "本次拉取结果为空，拒绝删除（疑似拉取失败）"
    if cache_ids and len(removed_ids) > max(5, 0.2 * len(cache_ids)):
        return False, (f"待删除 {len(removed_ids)} 条，超过缓存总量 {len(cache_ids)} 的 20% 阈值，"
                       f"拒绝删除（如确为正常下线，请加 --force）")
    return True, ""


def run_sync(current_raw, config, cache_doc, client, dry_run=False, log=print, force=False):
    sheet_id = cache_doc.get("sheet_id")
    docid = cache_doc.get("docid")
    cache = cache_doc.get("records", {})
    created_new = False

    # 首次运行：自动建表
    if not docid or not sheet_id:
        if dry_run:
            log("[init] dry-run：跳过自动建表")
        else:
            viewer = os.environ.get("WECOM_VIEWER_USERID")
            log("[init] 未配置 docid/sheet_id，自动创建智能表格并建列…")
            docid, sheet_id = ensure_table(client, admin_users=[viewer] if viewer else None)
            cache_doc["docid"] = docid
            cache_doc["sheet_id"] = sheet_id
            created_new = True
            log(f"[init] 已创建智能表格 docid={docid} sheet_id={sheet_id}")
    # 无论新建还是复用，每次同步都确保列齐全（补建缺失列，幂等；
    # 修复"新建表批量建列偶发丢尾列、后续不再补"的 bug）
    if not dry_run:
        added = ensure_columns(client, docid, sheet_id, log=log)
        if added:
            log(f"[columns] 自动补齐新增列：{', '.join(added)}")
    if not created_new:
        # 复用已有表（如 augJOD）：先读回现有记录、按 TAPD需求ID 对齐 record_id，
        # 避免把已有行当「新增」重复写入。需要自建应用对目标表拥有读权限。
        # 机器人身份通常无读权限(851008)：读不到就退回本地 record_id 缓存，不阻断同步
        if not dry_run and not created_new:
            try:
                reconcile_cache(client, docid, sheet_id, cache, log=log)
            except Exception as e:
                log(f"[reconcile] 跳过读回对账（{str(e)[:160]}），改用本地 record_id 缓存 {len(cache)} 条")

    pid_to_name = {p["id"]: p["name"] for p in config.get("projects", [])}

    raw_stories = current_raw.get("stories", current_raw if isinstance(current_raw, list) else [])
    current = {}
    skipped_no_oa = 0
    for s in raw_stories:
        pid = str(s.get("_project_id", ""))
        oa = extract_oa_number(s, pid, config)
        if not oa:
            skipped_no_oa += 1
            continue
        pname = pid_to_name.get(pid, s.get("_project_name", pid))
        values = build_values(s, pid, config, pname)
        status_label = values["状态"][0]["text"]
        modified = values["最后修改"]
        applicant = values["需求申请人员"][0]["text"]
        status_change = values["状态变更时间"]
        current[s.get("id", "")] = {
            "values": values,
            "oa_number": oa,
            "status_label": status_label,
            "modified": modified,
            "applicant": applicant,
            "status_change": status_change,
            "name": s.get("name", ""),
        }

    cache_ids = set(cache.keys())
    current_ids = set(current.keys())
    new_ids = current_ids - cache_ids
    removed_ids = cache_ids - current_ids
    changed_ids = []
    for tid in current_ids & cache_ids:
        c = cache[tid]
        cur = current[tid]
        if (cur["status_label"] != c.get("status_label", "")) or \
           (cur["modified"] != c.get("modified", "")) or \
           (cur["oa_number"] != c.get("oa_number", "")) or \
           (cur["name"] != c.get("name", "")) or \
           (cur["applicant"] != c.get("applicant", "")) or \
           (cur["status_change"] != c.get("status_change", "")):
            changed_ids.append(tid)

    fetch_errors = int(current_raw.get("fetch_errors", 0)) if isinstance(current_raw, dict) else 0
    log(f"[diff] 当前TAPD含OA需求: {len(current_ids)} | 缓存: {len(cache_ids)}")
    log(f"[diff] 新增: {len(new_ids)} | 变更: {len(changed_ids)} | 移除: {len(removed_ids)} | 跳过(无OA单号): {skipped_no_oa}")

    allow_delete, deny_reason = guard_deletions(
        removed_ids, cache_ids, current_ids, fetch_errors, force, log=log)
    if removed_ids and not allow_delete:
        log(f"[guard] ⛔ 跳过删除：{deny_reason}")
        removed_ids = set()

    if dry_run:
        return {"new": len(new_ids), "changed": len(changed_ids), "removed": len(removed_ids),
                "blocked": deny_reason if not allow_delete else ""}

    added = updated = deleted = 0
    if new_ids:
        values_list = [current[tid]["values"] for tid in new_ids]
        rids = client.add_records(docid, sheet_id, values_list)
        for tid, rid in zip(new_ids, rids):
            cur = current[tid]
            cache[tid] = {
                "record_id": rid, "oa_number": cur["oa_number"],
                "status_label": cur["status_label"], "modified": cur["modified"],
                "applicant": cur["applicant"], "status_change": cur["status_change"],
                "name": cur["name"],
            }
            added += 1
        log(f"[add] 写入 {added} 条新记录")

    if changed_ids:
        recs = [{"record_id": cache[tid]["record_id"], "values": current[tid]["values"]} for tid in changed_ids]
        client.update_records(docid, sheet_id, recs)
        for tid in changed_ids:
            cur = current[tid]
            cache[tid].update({
                "oa_number": cur["oa_number"], "status_label": cur["status_label"],
                "modified": cur["modified"], "applicant": cur["applicant"],
                "status_change": cur["status_change"], "name": cur["name"],
            })
            updated += 1
        log(f"[update] 更新 {updated} 条变更记录")

    if removed_ids:
        rids = [cache[tid]["record_id"] for tid in removed_ids]
        client.delete_records(docid, sheet_id, rids)
        for tid in removed_ids:
            del cache[tid]
            deleted += 1
        log(f"[delete] 移除 {deleted} 条失效记录")

    cache_doc["records"] = cache
    cache_doc["last_sync_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 运行心跳：刷新整表『最后同步时间』列，用户可在智能表格直接看到同步在跑
    try:
        stamp_cache = cache
        # 若本地缓存无 record_id（reconcile 未成功读回，例如自建应用仅有写权限无读权限），
        # 尝试直接读表一次性拿全部 record_id 作为兜底，确保心跳列能覆盖全表。
        if not any(c.get("record_id") for c in cache.values()):
            try:
                recs = client.get_records(docid, sheet_id)
                stamp_cache = {
                    cell_text(r.get("values", {}).get("TAPD需求ID")): {"record_id": r.get("record_id")}
                    for r in recs
                    if cell_text(r.get("values", {}).get("TAPD需求ID"))
                }
                log(f"[stamp] 本地缓存无 record_id，已通过 get_records 兜底获取 {len(stamp_cache)} 行")
            except Exception as e:
                log(f"[stamp] 兜底读取失败（可能无读权限）：{str(e)[:160]}")
        stamp_sync_time(client, docid, sheet_id, stamp_cache, log=log)
    except Exception as e:
        log(f"[stamp] 心跳标记异常（不影响主同步）：{str(e)[:160]}")
    return {"new": added, "changed": updated, "removed": deleted, "total": len(cache)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--config", default=os.path.join(HERE, "project_oa_config.json"))
    ap.add_argument("--cache", default=os.path.join(HERE, "wecom_sync_cache.json"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="跳过删除安全护栏，强制执行删除（确认 TAPD 侧确实已下线时使用）")
    args = ap.parse_args()

    config = load_json(args.config)
    cache_doc = load_json(args.cache) if os.path.exists(args.cache) else {"docid": "", "sheet_id": "", "records": {}}
    current_raw = load_json(args.input)

    # 复用已有表：通过环境变量指定目标 docid/sheet_id（留空则首次运行自动新建）。
    # 关键：环境变量非空时【始终覆盖】缓存——支持从一张表切换到另一张表
    # （例如从自建应用新建的表切回 augJOD），reconcile_cache 会自动读回目标表的 record_id。
    env_doc = (os.environ.get("WECOM_DOCID") or "").strip()
    env_sheet = (os.environ.get("WECOM_SHEET_ID") or "").strip()
    if env_doc:
        if cache_doc.get("docid") and cache_doc["docid"] != env_doc:
            print(f"[init] 环境变量 WECOM_DOCID 与缓存不一致，以环境变量为准（{cache_doc['docid'][:20]}… → {env_doc[:20]}…）")
        cache_doc["docid"] = env_doc
        cache_doc["sheet_id"] = env_sheet or cache_doc.get("sheet_id", "")

    client = build_wecom_client(config)
    result = run_sync(current_raw, config, cache_doc, client, dry_run=args.dry_run, force=args.force)

    if not args.dry_run:
        with open(args.cache, "w", encoding="utf-8") as f:
            json.dump(cache_doc, f, ensure_ascii=False, indent=2)

    if args.dry_run:
        print(f"\n[dry-run] 新增 {result['new']} / 变更 {result['changed']} / 移除 {result['removed']}（未执行写操作）")
    else:
        print(f"\n[done] 新增 {result['new']} / 更新 {result['changed']} / 移除 {result['removed']} / 总计在表 {result.get('total')} 条")
        print(f"[cache] 已更新 {args.cache}")


if __name__ == "__main__":
    main()
