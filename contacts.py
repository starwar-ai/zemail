"""通讯簿：企业（公司）→ 联系人 → 一个或多个邮箱地址，按用户存储。

数据文件 <data_dir>/<用户名>.json：
    {"companies": [{"id", "name", "created"}],
     "contacts":  [{"id", "company_id", "name", "title", "note",
                    "addresses": ["主地址", "其他地址"...], "key", "created"}]}

分析键（key）：创建联系人时的主地址，之后不可变。邮件缓存、会话历史、监控
等派生数据都以 key 关联；抓取时按联系人的全部地址检索合并。这样单地址
联系人与历史按地址分析的数据天然兼容，新增地址也不会破坏既有数据。
"""
import datetime as dt
import json
import os
import re
import threading
import uuid

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.@-]", "_", name)


def _norm_addr(addr: str) -> str:
    return str(addr or "").strip().lower()


class ContactError(Exception):
    """通讯簿业务错误，消息可直接返回前端。"""


class ContactStore:
    """一个用户的通讯簿文件（企业 + 联系人），原子写。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _save(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ---------- 企业 ----------

    def list_companies(self) -> list:
        comps = self._load().get("companies") or []
        return sorted(comps, key=lambda c: c.get("name") or "")

    def add_company(self, name: str) -> dict:
        name = str(name or "").strip()[:60]
        if not name:
            raise ContactError("企业名称不能为空")
        with self._lock:
            data = self._load()
            comps = data.setdefault("companies", [])
            if any(c.get("name") == name for c in comps):
                raise ContactError("同名企业已存在")
            company = {"id": "co-" + uuid.uuid4().hex[:8], "name": name,
                       "created": dt.datetime.now().isoformat(timespec="seconds")}
            comps.append(company)
            self._save(data)
            return company

    def rename_company(self, cid: str, name: str) -> None:
        name = str(name or "").strip()[:60]
        if not name:
            raise ContactError("企业名称不能为空")
        with self._lock:
            data = self._load()
            for c in data.get("companies") or []:
                if c.get("id") == cid:
                    c["name"] = name
                    self._save(data)
                    return
            raise ContactError("企业不存在")

    def delete_company(self, cid: str) -> None:
        with self._lock:
            data = self._load()
            if any(c.get("company_id") == cid for c in data.get("contacts") or []):
                raise ContactError("该企业下还有联系人，请先移除或转移")
            comps = data.get("companies") or []
            data["companies"] = [c for c in comps if c.get("id") != cid]
            self._save(data)

    # ---------- 联系人 ----------

    def list_contacts(self) -> list:
        return list(self._load().get("contacts") or [])

    def _validate_addresses(self, addresses) -> list:
        if not isinstance(addresses, list):
            raise ContactError("地址必须是数组")
        cleaned = []
        for a in addresses:
            a = _norm_addr(a)
            if not a:
                continue
            if not EMAIL_RE.match(a):
                raise ContactError(f"邮箱地址格式不正确：{a}")
            if a not in cleaned:
                cleaned.append(a)
        if not cleaned:
            raise ContactError("至少需要一个邮箱地址")
        return cleaned[:10]

    def add_contact(self, name: str, addresses: list, company_id: str = "",
                    title: str = "", note: str = "") -> dict:
        """新建联系人；主地址（第一个有效地址）即分析键，创建后不可变。"""
        addresses = self._validate_addresses(addresses)
        name = str(name or "").strip()[:60]
        title = str(title or "").strip()[:60]
        note = str(note or "").strip()[:200]
        company_id = str(company_id or "").strip()
        with self._lock:
            data = self._load()
            if company_id and not any(c.get("id") == company_id
                                      for c in data.get("companies") or []):
                raise ContactError("企业不存在")
            for c in data.get("contacts") or []:
                if set(c.get("addresses") or []) & set(addresses):
                    raise ContactError("部分地址已属于其他联系人")
            contact = {
                "id": "p-" + uuid.uuid4().hex[:8],
                "company_id": company_id,
                "name": name,
                "title": title,
                "note": note,
                "addresses": addresses,
                "key": addresses[0],
                "created": dt.datetime.now().isoformat(timespec="seconds"),
            }
            data.setdefault("contacts", []).append(contact)
            self._save(data)
            return contact

    def update_contact(self, cid: str, **fields) -> bool:
        """更新联系人（name/title/note/company_id/addresses）；key 不变。"""
        with self._lock:
            data = self._load()
            for c in data.get("contacts") or []:
                if c.get("id") != cid:
                    continue
                if "name" in fields:
                    c["name"] = str(fields["name"] or "").strip()[:60]
                if "title" in fields:
                    c["title"] = str(fields["title"] or "").strip()[:60]
                if "note" in fields:
                    c["note"] = str(fields["note"] or "").strip()[:200]
                if "company_id" in fields:
                    new_co = str(fields["company_id"] or "").strip()
                    if new_co and not any(x.get("id") == new_co
                                          for x in data.get("companies") or []):
                        raise ContactError("企业不存在")
                    c["company_id"] = new_co
                if "addresses" in fields:
                    addrs = self._validate_addresses(fields["addresses"])
                    if c["key"] not in addrs:
                        raise ContactError(f"主地址 {c['key']} 不能移除（分析数据与之关联）")
                    for other in data.get("contacts") or []:
                        if other.get("id") != cid and \
                                set(other.get("addresses") or []) & set(addrs):
                            raise ContactError("部分地址已属于其他联系人")
                    # 主地址始终排在首位
                    c["addresses"] = [c["key"]] + [a for a in addrs if a != c["key"]]
                self._save(data)
                return True
            return False

    def delete_contact(self, cid: str) -> bool:
        """删除联系人建档（不删除其邮件缓存/历史等分析数据）。"""
        with self._lock:
            data = self._load()
            contacts = data.get("contacts") or []
            kept = [c for c in contacts if c.get("id") != cid]
            if len(kept) == len(contacts):
                return False
            data["contacts"] = kept
            self._save(data)
            return True

    # ---------- 解析 ----------

    def resolve(self, target: str) -> tuple:
        """把分析键解析为（抓取地址列表，显示名）；未建档时原样返回。"""
        target = _norm_addr(target)
        for c in self._load().get("contacts") or []:
            if c.get("key") == target:
                return (list(c.get("addresses") or [target]),
                        c.get("name") or "")
        return ([target], "")

    def contact_map(self) -> dict:
        """key → 联系人（含企业名），供列表页合并展示。"""
        data = self._load()
        companies = {c.get("id"): c.get("name") or ""
                     for c in data.get("companies") or []}
        out = {}
        for c in data.get("contacts") or []:
            info = dict(c)
            info["company"] = companies.get(c.get("company_id"), "")
            out[c.get("key")] = info
        return out


class ContactHub:
    """按用户名管理 ContactStore 实例（延迟创建，实例缓存）。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._stores = {}
        self._lock = threading.Lock()

    def store(self, username: str) -> ContactStore:
        with self._lock:
            if username not in self._stores:
                path = os.path.join(self.data_dir, f"{_safe_name(username)}.json")
                self._stores[username] = ContactStore(path)
            return self._stores[username]
