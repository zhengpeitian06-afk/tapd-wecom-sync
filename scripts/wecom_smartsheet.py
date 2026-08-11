#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
企微智能表格 HTTP 客户端（服务器版，不依赖 wecom-cli / WorkBuddy）。

使用企业微信「自建应用」的 corpid + corpsecret 获取 access_token，再调用
/cgi-bin/wedoc/smartsheet/* 系列接口完成：建表、字段管理、记录增删改查。

前置（企业微信管理后台一次性配置）：
  1. 创建自建应用，拿到 corpid 与 corpsecret；
  2. 给该应用授予「文档」相关接口权限；
  3. 在目标智能表格的「可调用应用」列表里加入该自建应用（否则调用会被拒）。

依赖：仅标准库（urllib），无需 pip 安装任何包。

字段值约定：
  - 文本：{"type":"text","text":"内容"}  或 直接传字符串
  - 日期时间（FIELD_TYPE_DATE_TIME）：毫秒级 unix 时间戳字符串
"""
import json
import time
import urllib.request
import urllib.error
from datetime import datetime

WECOM_API = "https://qyapi.weixin.qq.com/cgi-bin"


class WeComError(RuntimeError):
    pass


def to_ms_timestamp(date_str):
    """将 'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM:SS' 转为毫秒时间戳字符串。"""
    if not date_str:
        return ""
    s = str(date_str).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return str(int(dt.timestamp() * 1000))
        except ValueError:
            continue
    return s  # 无法解析则原样返回，由接口决定是否报错


class WeComSmartsheetClient:
    def __init__(self, corpid, corpsecret, log=print):
        self.corpid = corpid
        self.corpsecret = corpsecret
        self._log = log
        self._token = None
        self._token_expire = 0

    # ---------- 基础请求 ----------
    def _get_token(self):
        now = time.time()
        if self._token and now < self._token_expire - 60:
            return self._token
        url = f"{WECOM_API}/gettoken?corpid={self.corpid}&corpsecret={self.corpsecret}"
        data = self._http(url, None, method="GET")
        if data.get("errcode") != 0:
            raise WeComError(f"获取 access_token 失败：{data}")
        self._token = data["access_token"]
        self._token_expire = now + int(data.get("expires_in", 7200))
        return self._token

    def _http(self, url, body, method="POST"):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503):
                    wait = 2 ** attempt
                    self._log(f"[wecom] HTTP {e.code}，第 {attempt+1} 次重试，等待 {wait}s")
                    time.sleep(wait)
                    continue
                raise
        raise WeComError("企微请求多次失败")

    def _api(self, path, body):
        token = self._get_token()
        url = f"{WECOM_API}{path}?access_token={token}"
        resp = self._http(url, body)
        if resp.get("errcode") not in (0, None):
            raise WeComError(f"企微接口 {path} 失败：{resp}")
        return resp

    # ---------- 文档 / 子表 / 字段 ----------
    def create_doc(self, doc_name, admin_users=None):
        """新建智能表格（doc_type=10），返回 (docid, url, scode)。

        admin_users: 可选，传入企微账号(userid)列表，创建后立即把这些成员加为文档管理员，
                      方便人类在「文档 → 由应用创建的文档」中打开查看。
        """
        body = {"doc_type": 10, "doc_name": doc_name}
        if admin_users:
            aus = admin_users if isinstance(admin_users, list) else [admin_users]
            aus = [u for u in aus if u]
            if aus:
                body["admin_users"] = aus
        resp = self._api("/wedoc/create_doc", body)
        return resp.get("docid"), resp.get("url"), resp.get("scode")

    def get_sheet(self, docid):
        """返回子表列表 [{'sheet_id','title',...}]。"""
        resp = self._api("/wedoc/smartsheet/get_sheet", {"docid": docid})
        return resp.get("sheets") or resp.get("data") or []

    def get_fields(self, docid, sheet_id):
        resp = self._api("/wedoc/smartsheet/get_fields", {"docid": docid, "sheet_id": sheet_id})
        return resp.get("fields") or resp.get("data") or []

    def rename_field(self, docid, sheet_id, field_id, title):
        self._api("/wedoc/smartsheet/update_fields", {
            "docid": docid, "sheet_id": sheet_id,
            "fields": [{"field_id": field_id, "field_title": title}],
        })

    def add_fields(self, docid, sheet_id, fields):
        """fields: [{'field_title','field_type', ...}]"""
        self._api("/wedoc/smartsheet/add_fields", {
            "docid": docid, "sheet_id": sheet_id, "fields": fields,
        })

    # ---------- 记录 ----------
    def add_records(self, docid, sheet_id, values_list):
        """values_list: [ {col_title: value}, ... ]，返回 [record_id,...]。"""
        resp = self._api("/wedoc/smartsheet/add_records", {
            "docid": docid, "sheet_id": sheet_id,
            "key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE",
            "records": [{"values": v} for v in values_list],
        })
        recs = resp.get("records") or []
        return [r.get("record_id") for r in recs]

    def update_records(self, docid, sheet_id, records):
        """records: [ {'record_id':..., 'values':{col_title:value}}, ... ]"""
        self._api("/wedoc/smartsheet/update_records", {
            "docid": docid, "sheet_id": sheet_id,
            "key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE",
            "records": records,
        })

    def delete_records(self, docid, sheet_id, record_ids):
        self._api("/wedoc/smartsheet/delete_records", {
            "docid": docid, "sheet_id": sheet_id, "record_ids": record_ids,
        })

    # ---------- 读取（复用已有表时对账用） ----------
    def get_records(self, docid, sheet_id, limit=200):
        """分页读取子表全部记录。返回 [{'record_id':.., 'values':{field_title:[...]}}]。

        用于「复用已有智能表格」场景：首次运行前先读回现有记录，按 TAPD需求ID
        把 record_id 补进本地缓存，避免重复写入。
        要求自建应用对目标表拥有【读】权限（与写权限一并授予）。
        """
        out = []
        cursor = None
        while True:
            body = {"docid": docid, "sheet_id": sheet_id}
            if cursor:
                body["cursor"] = cursor
            if limit:
                body["limit"] = limit
            resp = self._api("/wedoc/smartsheet/get_records", body)
            recs = resp.get("records") or []
            out.extend(recs)
            if not resp.get("has_more") or not recs:
                break
            cursor = resp.get("next_cursor")
            if not cursor:
                break
        return out


def cell_text(cell):
    """从单元格值提取文本：兼容 [{type,text}] / {text} / 纯字符串。"""
    if cell is None:
        return ""
    if isinstance(cell, str):
        return cell
    if isinstance(cell, list):
        if not cell:
            return ""
        item = cell[0]
        if isinstance(item, dict):
            return item.get("text") or item.get("name") or ""
        return str(item)
    if isinstance(cell, dict):
        return cell.get("text") or cell.get("name") or ""
    return str(cell)


# ---------- 字段结构定义（与现有同步表一致） ----------
# 顺序即建表列顺序；同步时会自动补齐缺失列（ensure_columns）。
COLUMNS = [
    ("OA需求单号", "FIELD_TYPE_TEXT"),
    ("需求标题", "FIELD_TYPE_TEXT"),
    ("状态", "FIELD_TYPE_TEXT"),
    ("优先级", "FIELD_TYPE_TEXT"),
    ("处理人", "FIELD_TYPE_TEXT"),
    ("创建人", "FIELD_TYPE_TEXT"),
    ("需求申请人员", "FIELD_TYPE_TEXT"),     # 实际提出需求的业务人员（TAPD 自定义字段）
    ("所属项目", "FIELD_TYPE_TEXT"),
    ("创建时间", "FIELD_TYPE_DATE_TIME"),
    ("最后修改", "FIELD_TYPE_DATE_TIME"),
    ("状态变更时间", "FIELD_TYPE_DATE_TIME"),  # 最近一次「状态」字段变更的时间（≠ 最后修改）
    ("TAPD需求ID", "FIELD_TYPE_TEXT"),
    ("最后同步时间", "FIELD_TYPE_TEXT"),  # 运行心跳列（用文本类型避免日期列建列限制），每次同步刷新为当前时间，确认定时任务在跑
]


def ensure_columns(client, docid, sheet_id, columns=COLUMNS, log=print):
    """确保目标表中存在 COLUMNS 定义的所有列；缺失的列逐列补建。

    单列表失败仅记日志、不阻断其余列与主同步（避免某列建列异常拖垮整个流程）。
    返回本次新增的列标题列表（用于日志）。
    """
    try:
        existing = client.get_fields(docid, sheet_id)
    except Exception as e:
        log(f"[columns] 读取现有列失败（{str(e)[:160]}），跳过本轮补列")
        return []
    existing_titles = {f.get("field_title") for f in existing}
    added = []
    for t, ft in columns:
        if t in existing_titles:
            continue
        try:
            client.add_fields(docid, sheet_id, [{"field_title": t, "field_type": ft}])
            added.append(t)
            log(f"[columns] 已补建列：{t}")
        except Exception as e:
            log(f"[columns] 补建列失败（{t}）：{str(e)[:160]}")
    return added


def ensure_table(client, doc_name="TAPD需求进度同步表", admin_users=None):
    """若未传入 docid，则创建智能表格并按 COLUMNS 建好列结构。
    返回 (docid, sheet_id)。
    逐列建立（单失败仅记日志不阻断），确保『最后同步时间』等尾列一定建出。"""
    docid, _, _ = client.create_doc(doc_name, admin_users=admin_users)
    sheets = client.get_sheet(docid)
    if not sheets:
        raise WeComError("新建文档后未获取到子表")
    sheet_id = sheets[0]["sheet_id"]
    fields = client.get_fields(docid, sheet_id)
    # 默认自带一个文本字段，重命名为第一列
    if fields:
        try:
            client.rename_field(docid, sheet_id, fields[0]["field_id"], COLUMNS[0][0])
        except Exception as e:
            client._log(f"[ensure_table] 重命名首列失败（{str(e)[:120]}）")
    # 逐列建立：避免批量 add_fields 偶发丢尾列（如『最后同步时间』）
    for t, ft in COLUMNS[1:]:
        try:
            client.add_fields(docid, sheet_id, [{"field_title": t, "field_type": ft}])
        except Exception as e:
            client._log(f"[ensure_table] 建列失败（{t}）：{str(e)[:120]}")
    return docid, sheet_id
