"""用户与权限管理：本地 JSON 用户库 + Flask 会话登录。

用户库格式（data/users.json）：
    {"<用户名>": {"password_hash": "...", "role": "admin"|"user", "created_at": "..."}}
"""
import datetime as dt
import functools
import json
import os
import re
import threading

from flask import jsonify, session
from werkzeug.security import check_password_hash, generate_password_hash

ROLES = ("admin", "user")
USERNAME_RE = re.compile(r"^[\w.@-]{2,32}$")
EMAIL_ADDR_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# 用户可在个人中心覆盖的邮箱（IMAP）设置项，未设置的项回退系统默认
MAIL_SETTING_FIELDS = ("email_address", "email_password", "imap_host", "imap_port")


class UserError(Exception):
    """用户操作的业务错误，消息可直接返回给前端。"""


class UserStore:
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
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def is_empty(self) -> bool:
        return not self._load()

    def verify(self, username: str, password: str):
        """校验成功返回用户记录，失败返回 None。"""
        user = self._load().get(username)
        if user and check_password_hash(user["password_hash"], password):
            return {"username": username, "role": user["role"]}
        return None

    def get_role(self, username: str) -> str:
        user = self._load().get(username)
        return user["role"] if user else ""

    def list_users(self) -> list:
        data = self._load()
        return [
            {"username": name, "role": u["role"], "created_at": u.get("created_at", "")}
            for name, u in sorted(data.items())
        ]

    def create(self, username: str, password: str, role: str = "user") -> None:
        if not USERNAME_RE.match(username):
            raise UserError("用户名需为 2-32 位字母、数字或 . _ @ -")
        if len(password) < 6:
            raise UserError("密码至少 6 位")
        if role not in ROLES:
            raise UserError("角色必须是 admin 或 user")
        with self._lock:
            data = self._load()
            if username in data:
                raise UserError("用户名已存在")
            data[username] = {
                "password_hash": generate_password_hash(password),
                "role": role,
                "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
            self._save(data)

    def delete(self, username: str) -> None:
        with self._lock:
            data = self._load()
            if username not in data:
                raise UserError("用户不存在")
            if data[username]["role"] == "admin" and self._count_admins(data) <= 1:
                raise UserError("不能删除最后一名管理员")
            del data[username]
            self._save(data)

    def set_password(self, username: str, password: str) -> None:
        if len(password) < 6:
            raise UserError("密码至少 6 位")
        with self._lock:
            data = self._load()
            if username not in data:
                raise UserError("用户不存在")
            data[username]["password_hash"] = generate_password_hash(password)
            self._save(data)

    def set_role(self, username: str, role: str) -> None:
        if role not in ROLES:
            raise UserError("角色必须是 admin 或 user")
        with self._lock:
            data = self._load()
            if username not in data:
                raise UserError("用户不存在")
            if data[username]["role"] == "admin" and role != "admin" \
                    and self._count_admins(data) <= 1:
                raise UserError("不能移除最后一名管理员")
            data[username]["role"] = role
            self._save(data)

    def get_report_prompt(self, username: str) -> str:
        """用户自定义的报告默认提示词，未设置返回空串。"""
        user = self._load().get(username)
        return (user or {}).get("report_prompt") or ""

    def set_report_prompt(self, username: str, prompt: str) -> None:
        """保存用户的报告默认提示词；空串表示恢复系统默认。"""
        if len(prompt) > 4000:
            raise UserError("提示词不能超过 4000 字")
        with self._lock:
            data = self._load()
            if username not in data:
                raise UserError("用户不存在")
            if prompt:
                data[username]["report_prompt"] = prompt
            else:
                data[username].pop("report_prompt", None)
            self._save(data)

    def get_mail_settings(self, username: str) -> dict:
        """用户的邮箱（IMAP）设置覆盖项，未设置的字段不出现。"""
        user = self._load().get(username) or {}
        return dict(user.get("mail_settings") or {})

    def set_mail_settings(self, username: str, updates: dict) -> None:
        """保存用户的邮箱设置覆盖项；值为 None 表示清除该项（回退系统默认）。"""
        cleaned = {}
        for key, value in updates.items():
            if key not in MAIL_SETTING_FIELDS:
                raise UserError(f"不支持的设置项：{key}")
            if value is None or value == "":
                cleaned[key] = None
                continue
            value = str(value).strip()
            if key == "email_address" and not EMAIL_ADDR_RE.match(value):
                raise UserError("邮箱地址格式不正确")
            if key == "imap_host" and not value:
                raise UserError("IMAP 服务器不能为空")
            if key == "imap_port":
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise UserError("IMAP 端口必须是整数")
                if not 1 <= value <= 65535:
                    raise UserError("IMAP 端口需在 1-65535 之间")
            cleaned[key] = value
        with self._lock:
            data = self._load()
            if username not in data:
                raise UserError("用户不存在")
            ms = dict(data[username].get("mail_settings") or {})
            for key, value in cleaned.items():
                if value is None:
                    ms.pop(key, None)
                else:
                    ms[key] = value
            if ms:
                data[username]["mail_settings"] = ms
            else:
                data[username].pop("mail_settings", None)
            self._save(data)

    @staticmethod
    def _count_admins(data: dict) -> int:
        return sum(1 for u in data.values() if u["role"] == "admin")


def current_user() -> dict:
    """从会话中取当前登录用户，未登录返回 None。"""
    username = session.get("user")
    if not username:
        return None
    return {"username": username, "role": session.get("role", "user")}


def login_required(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user():
            return jsonify({"error": "未登录或登录已过期"}), 401
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"error": "未登录或登录已过期"}), 401
        if user["role"] != "admin":
            return jsonify({"error": "需要管理员权限"}), 403
        return view(*args, **kwargs)
    return wrapper
