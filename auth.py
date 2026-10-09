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
