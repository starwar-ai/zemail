"""Web 界面入口：独立前端页面，无需企业微信。

分析耗时较长（IMAP 抓取 + LLM 生成），采用「后台任务 + 前端轮询」模式；
追问走同步接口。分析会话按「登录用户 + 目标邮箱」隔离，历史记录按用户分目录存储。

用户与权限：所有 /api/* 接口需登录；admin 可管理用户，普通用户只能访问自己的数据。

用法：python web.py，然后浏览器打开 http://localhost:8000
生产部署：waitress-serve --listen=0.0.0.0:8000 --call web:create_app
"""
import imaplib
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from urllib.parse import quote
from email.utils import getaddresses

from flask import Flask, Response, jsonify, request, session

import history
from analyzer import Analyzer, LLMClient, REPORT_INSTRUCTION
from auth import UserError, UserStore, admin_required, current_user, login_required
from bot import EMAIL_RE
from config import (RUNTIME_SETTINGS, load_config, load_runtime_settings,
                    save_runtime_settings)
from exporter import build_report_docx
from mailer import IMAPFetcher
from mailer.cache import delete_cache, load_emails
from summaries import delete_summaries

JOB_TTL_SECONDS = 3600

NO_MAIL_HINT = (
    "可能原因：超过配置的查找时间范围、目标地址拼写有误，"
    "或「已发送」文件夹未被纳入（可在 MAIL_FOLDERS 中配置）。"
)


def _session_user(username: str, target: str) -> str:
    return f"web:{username}:{target}"


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.@-]", "_", name)


def _display_name(emails: list, target: str) -> str:
    """从往来邮件中提取目标地址对应的显示名（「张三 <zhang@x.com>」→「张三」）。

    从最新的邮件往前找，拿到第一个非空显示名；没有则返回空串。
    """
    for m in reversed(emails):
        for header in (m.from_addr, m.to_addrs):
            for name, addr in getaddresses([header]):
                if addr.lower() == target and name:
                    return name
    return ""


def _load_or_create_secret_key(cfg) -> str:
    if cfg.secret_key:
        return cfg.secret_key
    path = os.path.join(os.path.dirname(cfg.users_file) or "data", ".secret_key")
    try:
        with open(path, encoding="utf-8") as f:
            key = f.read().strip()
            if key:
                return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(key)
    return key


def _migrate_legacy_history(history_dir: str, admin_username: str) -> None:
    """把用户体系上线前的全局历史文件（<地址>-<8位哈希>.json）迁移到首个管理员名下。"""
    if not os.path.isdir(history_dir):
        return
    legacy = [n for n in os.listdir(history_dir)
              if re.match(r"^.+-[0-9a-f]{8}\.json$", n)
              and os.path.isfile(os.path.join(history_dir, n))]
    if not legacy:
        return
    dest = os.path.join(history_dir, _safe_name(admin_username))
    os.makedirs(dest, exist_ok=True)
    for name in legacy:
        os.replace(os.path.join(history_dir, name), os.path.join(dest, name))


def _migrate_legacy_email_cache(cache_dir: str, usernames: list) -> None:
    """把按用户隔离前的全局邮件缓存（缓存根目录下的散文件）复制到每个现有用户名下。

    旧方案缓存是全局共享的，所有用户都能看到；改为复制（而非移动）可让
    每个用户在新的按用户隔离方案下继续看到原有缓存。复制完成后删除散文件。
    """
    if not os.path.isdir(cache_dir) or not usernames:
        return
    legacy = [n for n in os.listdir(cache_dir)
              if re.match(r"^.+-[0-9a-f]{8}\.json$", n)
              and os.path.isfile(os.path.join(cache_dir, n))]
    if not legacy:
        return
    for username in usernames:
        dest = os.path.join(cache_dir, _safe_name(username))
        os.makedirs(dest, exist_ok=True)
        for name in legacy:
            shutil.copy2(os.path.join(cache_dir, name), os.path.join(dest, name))
    for name in legacy:
        os.remove(os.path.join(cache_dir, name))


def create_app(cfg=None):
    cfg = cfg or load_config()
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.secret_key = _load_or_create_secret_key(cfg)
    settings_path = os.path.join(os.path.dirname(cfg.users_file) or "data", "settings.json")
    load_runtime_settings(cfg, settings_path)  # 后台保存的设置优先于 .env
    missing = [name for name, ok in [
        ("IMAP_HOST", cfg.imap_host),
        ("EMAIL_ADDRESS", cfg.email_address),
        ("EMAIL_PASSWORD", cfg.email_password),
        ("LLM_API_KEY", cfg.llm_api_key),
    ] if not ok]
    if missing:
        app.logger.warning("配置缺失：%s（请检查 .env）", ", ".join(missing))
        print(f"⚠️  配置缺失：{', '.join(missing)}，请检查 .env 后重启", flush=True)
    llm = LLMClient(cfg.llm_api_base, cfg.llm_api_key, cfg.llm_model)

    users = UserStore(cfg.users_file)
    if users.is_empty():
        users.create(cfg.admin_username, cfg.admin_password, "admin")
        print(f"已创建初始管理员 {cfg.admin_username}（密码来自 ADMIN_PASSWORD，"
              f"默认为 admin123，请登录后尽快修改）", flush=True)
    _migrate_legacy_history(cfg.session_history_dir, cfg.admin_username)
    _migrate_legacy_email_cache(cfg.email_cache_dir,
                                [u["username"] for u in users.list_users()])

    def user_cache_dir(username: str) -> str:
        """邮件缓存按用户隔离：不同用户可配置不同邮箱，缓存不能混用。"""
        return os.path.join(cfg.email_cache_dir, _safe_name(username))

    def get_fetcher(username: str) -> IMAPFetcher:
        """按当前生效的邮箱设置构建抓取器：个人设置优先，未设置的项用系统默认。

        每次使用现建（连接在抓取时才建立），管理员运行时设置与个人设置
        修改后下一次抓取自然生效。
        """
        ms = users.get_mail_settings(username)
        return IMAPFetcher(
            host=ms.get("imap_host") or cfg.imap_host,
            port=int(ms.get("imap_port") or cfg.imap_port),
            user=ms.get("email_address") or cfg.email_address,
            password=ms.get("email_password") or cfg.email_password,
            folders=cfg.mail_folders, lookback_days=cfg.mail_lookback_days,
            max_results=cfg.mail_max_results, body_chars=cfg.email_body_chars,
            cache_dir=user_cache_dir(username),
        )

    # 每个登录用户一个 Analyzer：历史记录落在 data/sessions/<用户名>/ 下，互不可见
    analyzers = {}
    analyzers_lock = threading.Lock()

    def get_analyzer(username: str) -> Analyzer:
        with analyzers_lock:
            if username not in analyzers:
                analyzers[username] = Analyzer(
                    llm, context_chars=cfg.llm_context_chars,
                    ttl_seconds=cfg.session_ttl_minutes * 60,
                    history_dir=os.path.join(cfg.session_history_dir, _safe_name(username)),
                    summary_dir=user_cache_dir(username),
                )
            return analyzers[username]

    def user_history_dir() -> str:
        return os.path.join(cfg.session_history_dir, _safe_name(current_user()["username"]))

    def effective_report_prompt(username: str) -> str:
        """当前用户生效的报告默认提示词：个人设置优先，未设置用系统默认。"""
        return users.get_report_prompt(username) or REPORT_INSTRUCTION

    jobs = {}
    jobs_lock = threading.Lock()

    def set_job(job_id: str, **fields) -> None:
        with jobs_lock:
            jobs.setdefault(job_id, {}).update(fields)

    def get_job(job_id: str) -> dict:
        with jobs_lock:
            # 顺手清理过期任务
            expired = [k for k, v in jobs.items()
                       if time.time() - v.get("created", 0) > JOB_TTL_SECONDS]
            for k in expired:
                del jobs[k]
            return dict(jobs.get(job_id) or {})

    MAIL_CONFIG_HINT = "请在「个人中心」填写邮箱地址、密码与 IMAP 服务器，或联系管理员配置系统默认。"

    def run_analysis(job_id: str, username: str, target: str) -> None:
        set_job(job_id, stage="fetch")
        fetcher = get_fetcher(username)
        if not (fetcher.host and fetcher.user and fetcher.password):
            set_job(job_id, status="error", error=f"邮箱连接未配置。{MAIL_CONFIG_HINT}")
            return
        try:
            emails = fetcher.fetch_correspondence(target)
        except Exception as exc:
            set_job(job_id, status="error",
                    error=f"抓取邮件失败（IMAP {fetcher.host}:{fetcher.port}）：{exc}")
            return
        if not emails:
            set_job(job_id, status="error",
                    error=f"未在邮箱中找到与 {target} 的往来邮件。{NO_MAIL_HINT}")
            return
        # 只建立会话（注入邮件摘要），不生成报告；报告由用户在对话后手动生成
        if get_job(job_id).get("cancel"):
            set_job(job_id, status="cancelled")
            return
        set_job(job_id, stage="session")
        hdir = os.path.join(cfg.session_history_dir, _safe_name(username))
        history.touch_session(hdir, target)
        # 新抓取后活跃会话指向最新会话块，内存会话与其绑定（问答落库到该块）
        sessions = history.get_sessions(hdir, target)
        idx = len(sessions) - 1
        history.set_active_index(hdir, target, idx)
        get_analyzer(username).prepare_session(_session_user(username, target),
                                               target, emails, history_index=idx)
        set_job(job_id, status="done", target=target, count=len(emails),
                date_range=f"{emails[0].date:%Y-%m-%d} ~ {emails[-1].date:%Y-%m-%d}")

    def _match_email(raw: str):
        return EMAIL_RE.search(str(raw or ""))

    def ensure_session(username: str, target: str) -> bool:
        """确保有可用会话：无内存会话时按活跃会话块（含其邮件子集与历史问答）自动重建。"""
        analyzer = get_analyzer(username)
        user = _session_user(username, target)
        if analyzer.has_session(user):
            return True
        all_emails = load_emails(user_cache_dir(username), target)
        if not all_emails:
            return False
        hdir = user_history_dir()
        idx = history.get_active_index(hdir, target)
        sess_rec = history.get_session(hdir, target, idx) if idx >= 0 else None
        # 会话若基于选中的邮件子集建立，重建时用同一子集
        emails = all_emails
        if sess_rec and sess_rec.get("email_indices") is not None:
            selected = [all_emails[i] for i in sess_rec["email_indices"]
                        if isinstance(i, int) and 0 <= i < len(all_emails)]
            if selected:
                emails = selected
        analyzer.prepare_session(user, target, emails,
                                 history_index=idx if idx >= 0 else None)
        if sess_rec:
            analyzer.replay(user, sess_rec.get("messages") or [])
        return True

    @app.get("/")
    def index():
        resp = app.send_static_file("index.html")
        # 单页应用无构建版本号，禁缓存保证前端更新立即生效
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    # ---------- 认证与用户管理 ----------

    @app.post("/api/login")
    def login():
        data = request.get_json(silent=True) or {}
        user = users.verify(str(data.get("username", "")).strip(),
                            str(data.get("password", "")))
        if not user:
            return jsonify({"error": "用户名或密码错误"}), 401
        session["user"] = user["username"]
        session["role"] = user["role"]
        return jsonify(user)

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify({"ok": True})

    @app.get("/api/me")
    def me():
        user = current_user()
        if not user:
            return jsonify({"error": "未登录"}), 401
        return jsonify(user)

    @app.post("/api/me/password")
    @login_required
    def me_change_password():
        """修改自己的登录密码（登录态即视为本人，不再校验原密码）。"""
        data = request.get_json(silent=True) or {}
        try:
            users.set_password(current_user()["username"], str(data.get("new_password", "")))
        except UserError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.get("/api/me/mail-settings")
    @login_required
    def me_mail_settings_get():
        """自己的邮箱（IMAP）设置覆盖项与系统默认（密码均不回显）。"""
        ms = users.get_mail_settings(current_user()["username"])
        return jsonify({
            "settings": {
                "email_address": ms.get("email_address", ""),
                "imap_host": ms.get("imap_host", ""),
                "imap_port": ms.get("imap_port", ""),
                "has_password": bool(ms.get("email_password")),
            },
            "global": {
                "email_address": cfg.email_address,
                "imap_host": cfg.imap_host,
                "imap_port": cfg.imap_port,
                "has_password": bool(cfg.email_password),
            },
        })

    @app.post("/api/me/mail-settings")
    @login_required
    def me_mail_settings_save():
        """保存自己的邮箱设置覆盖项；空值表示清除该项（回退系统默认），密码留空则不修改。"""
        data = request.get_json(silent=True) or {}
        updates = {
            "email_address": str(data.get("email_address", "")).strip() or None,
            "imap_host": str(data.get("imap_host", "")).strip() or None,
            "imap_port": str(data.get("imap_port", "")).strip() or None,
        }
        password = str(data.get("email_password", "")).strip()
        if password:
            updates["email_password"] = password
        try:
            users.set_mail_settings(current_user()["username"], updates)
        except UserError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.post("/api/me/mail-settings/test")
    @login_required
    def me_mail_settings_test():
        """尝试 IMAP 登录。可用请求体带上表单中未保存的值，空项回退已保存/系统默认。"""
        username = current_user()["username"]
        data = request.get_json(silent=True) or {}
        ms = users.get_mail_settings(username)

        def pick(key: str):
            return str(data.get(key, "")).strip() or ms.get(key) or getattr(cfg, key)

        host, user, password = pick("imap_host"), pick("email_address"), pick("email_password")
        try:
            port = int(pick("imap_port"))
        except (TypeError, ValueError):
            return jsonify({"error": "IMAP 端口必须是整数"}), 400
        if not (host and user and password):
            return jsonify({"error": f"邮箱连接未配置。{MAIL_CONFIG_HINT}"}), 400
        try:
            conn = imaplib.IMAP4_SSL(host, port, timeout=15)
            try:
                conn.login(user, password)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as exc:
            return jsonify({"error": f"连接失败（IMAP {host}:{port}）：{exc}"}), 400
        return jsonify({"ok": True})

    @app.get("/api/users")
    @admin_required
    def user_list():
        return jsonify({"users": users.list_users()})

    @app.post("/api/users")
    @admin_required
    def user_create():
        data = request.get_json(silent=True) or {}
        try:
            users.create(str(data.get("username", "")).strip(),
                         str(data.get("password", "")),
                         str(data.get("role", "user")))
        except UserError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.delete("/api/users/<username>")
    @admin_required
    def user_delete(username: str):
        if username == current_user()["username"]:
            return jsonify({"error": "不能删除当前登录的账号"}), 400
        try:
            users.delete(username)
        except UserError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.post("/api/users/<username>/password")
    @admin_required
    def user_set_password(username: str):
        data = request.get_json(silent=True) or {}
        try:
            users.set_password(username, str(data.get("password", "")))
        except UserError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.post("/api/users/<username>/role")
    @admin_required
    def user_set_role(username: str):
        data = request.get_json(silent=True) or {}
        try:
            users.set_role(username, str(data.get("role", "")))
        except UserError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True})

    # ---------- 运行时设置（管理员） ----------

    @app.get("/api/settings")
    @admin_required
    def settings_get():
        return jsonify({"settings": [
            {"key": key, "label": label, "minimum": minimum,
             "value": int(getattr(cfg, key))}
            for key, (label, minimum) in RUNTIME_SETTINGS.items()
        ]})

    @app.post("/api/settings")
    @admin_required
    def settings_save():
        data = request.get_json(silent=True) or {}
        try:
            save_runtime_settings(settings_path, data)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        load_runtime_settings(cfg, settings_path)
        # 热生效：抓取器按请求现建（自然读到新值），已存在的分析会话立即使用新值
        for a in analyzers.values():
            a.context_chars = cfg.llm_context_chars
            a.ttl = cfg.session_ttl_minutes * 60
        return jsonify({"ok": True})

    # ---------- 业务接口（均需登录，数据按用户隔离） ----------

    @app.get("/api/targets")
    @login_required
    def targets():
        """侧边栏目标列表：当前用户的历史记录 + 会话状态。"""
        username = current_user()["username"]
        analyzer = get_analyzer(username)
        result = []
        for item in history.list_targets(user_history_dir()):
            target = item["target"]
            emails = load_emails(user_cache_dir(username), target)
            result.append({
                "target": target,
                "name": _display_name(emails, target),
                "sessions": item["sessions"],
                "updated": item["updated"],
                "email_count": len(emails),
                "has_session": analyzer.has_session(_session_user(username, target)),
            })
        return jsonify({"targets": result})

    @app.get("/api/status/<path:email>")
    @login_required
    def status(email: str):
        match = _match_email(email)
        if not match:
            return jsonify({"error": "请提供有效的邮箱地址"}), 400
        username = current_user()["username"]
        target = match.group(0)
        ready = get_analyzer(username).has_session(_session_user(username, target)) \
            or bool(load_emails(user_cache_dir(username), target))
        return jsonify({"has_session": ready})

    @app.post("/api/analyze")
    @login_required
    def analyze():
        data = request.get_json(silent=True) or {}
        match = EMAIL_RE.search(str(data.get("email", "")))
        if not match:
            return jsonify({"error": "请提供有效的邮箱地址"}), 400
        job_id = uuid.uuid4().hex
        username = current_user()["username"]
        set_job(job_id, status="running", stage="fetch", created=time.time(),
                owner=username)
        threading.Thread(target=run_analysis, args=(job_id, username, match.group(0)),
                         daemon=True).start()
        return jsonify({"job_id": job_id})

    @app.get("/api/jobs/<job_id>")
    @login_required
    def job_status(job_id: str):
        job = get_job(job_id)
        if not job or job.get("owner") != current_user()["username"]:
            return jsonify({"error": "任务不存在或已过期"}), 404
        job.pop("created", None)
        job.pop("owner", None)
        return jsonify(job)

    @app.post("/api/jobs/<job_id>/cancel")
    @login_required
    def job_cancel(job_id: str):
        """标记任务取消：工作线程在抓取结束后检查该标志并丢弃结果。"""
        job = get_job(job_id)
        if not job or job.get("owner") != current_user()["username"]:
            return jsonify({"error": "任务不存在或已过期"}), 404
        set_job(job_id, cancel=True)
        return jsonify({"ok": True})

    @app.post("/api/fetch")
    @login_required
    def fetch_new():
        """增量抓取新邮件（只下载缓存之后的邮件），不动现有会话。"""
        data = request.get_json(silent=True) or {}
        match = _match_email(data.get("target", ""))
        if not match:
            return jsonify({"error": "请提供有效的目标邮箱地址"}), 400
        target = match.group(0)
        job_id = uuid.uuid4().hex
        username = current_user()["username"]
        set_job(job_id, status="running", stage="fetch", created=time.time(),
                owner=username)

        def run() -> None:
            fetcher = get_fetcher(username)
            if not (fetcher.host and fetcher.user and fetcher.password):
                set_job(job_id, status="error",
                        error=f"邮箱连接未配置。{MAIL_CONFIG_HINT}")
                return
            try:
                before = len(load_emails(user_cache_dir(username), target))
                emails = fetcher.fetch_correspondence(target)
            except Exception as exc:
                set_job(job_id, status="error",
                        error=f"抓取邮件失败（IMAP {fetcher.host}:{fetcher.port}）：{exc}")
                return
            new_count = max(0, len(emails) - before)
            if not emails:
                set_job(job_id, status="error",
                        error=f"未在邮箱中找到与 {target} 的往来邮件。{NO_MAIL_HINT}")
                return
            if get_job(job_id).get("cancel"):
                set_job(job_id, status="cancelled")
                return
            set_job(job_id, status="done", target=target, count=len(emails),
                    new=new_count,
                    date_range=f"{emails[0].date:%Y-%m-%d} ~ {emails[-1].date:%Y-%m-%d}")

        threading.Thread(target=run, daemon=True).start()
        return jsonify({"job_id": job_id})

    @app.post("/api/session")
    @login_required
    def session_create():
        """新建对话会话：默认使用全部缓存邮件，也可传 indices 用选中子集。

        旧会话保留在历史记录中，可通过会话列表随时切换回来。
        """
        data = request.get_json(silent=True) or {}
        match = _match_email(data.get("target", ""))
        if not match:
            return jsonify({"error": "请提供有效的目标邮箱地址"}), 400
        target = match.group(0)
        emails = load_emails(user_cache_dir(current_user()["username"]), target)
        if not emails:
            return jsonify({"error": "没有该邮箱的缓存邮件，请先发起分析"}), 409
        indices = data.get("indices")
        valid_indices = None
        if indices is not None:
            if not isinstance(indices, list):
                return jsonify({"error": "indices 必须是序号数组"}), 400
            valid_indices = [i for i in indices
                             if isinstance(i, int) and 0 <= i < len(emails)]
            if not valid_indices:
                return jsonify({"error": "请至少选择一封邮件"}), 400
            emails = [emails[i] for i in valid_indices]
        username = current_user()["username"]
        user = _session_user(username, target)
        date_range = f"{emails[0].date:%Y-%m-%d %H:%M} ~ {emails[-1].date:%Y-%m-%d %H:%M}"
        idx = history.new_session(user_history_dir(), target, indices=valid_indices,
                                  email_count=len(emails), date_range=date_range)
        get_analyzer(username).prepare_session(user, target, emails, history_index=idx)
        return jsonify({"target": target, "session_index": idx,
                        "email_count": len(emails), "date_range": date_range})

    @app.get("/api/sessions/<path:email>")
    @login_required
    def session_list(email: str):
        """会话列表（含活跃标记），供前端会话切换器展示。"""
        match = _match_email(email)
        if not match:
            return jsonify({"error": "请提供有效的邮箱地址"}), 400
        target = match.group(0)
        hdir = user_history_dir()
        sessions = history.get_sessions(hdir, target)
        return jsonify({
            "target": target,
            "active": history.get_active_index(hdir, target),
            "sessions": [{
                "index": i,
                "started_at": s.get("started_at", ""),
                "title": s.get("title") or "",
                "turns": sum(1 for m in s.get("messages") or [] if m.get("role") == "user"),
                "email_count": s.get("email_count"),
                "date_range": s.get("date_range", ""),
            } for i, s in enumerate(sessions)],
        })

    @app.post("/api/session/active")
    @login_required
    def session_switch():
        """切换活跃会话：以该会话块的邮件子集与历史问答重建内存会话。"""
        data = request.get_json(silent=True) or {}
        match = _match_email(data.get("target", ""))
        if not match:
            return jsonify({"error": "请提供有效的目标邮箱地址"}), 400
        target = match.group(0)
        index = data.get("index")
        if not history.set_active_index(user_history_dir(), target, index):
            return jsonify({"error": "会话不存在"}), 404
        username = current_user()["username"]
        user = _session_user(username, target)
        analyzer = get_analyzer(username)
        analyzer.reset(user)
        ensure_session(username, target)
        return jsonify({"target": target, "session_index": index,
                        "messages": analyzer.get_conversation(user)})

    @app.post("/api/chat")
    @login_required
    def chat():
        data = request.get_json(silent=True) or {}
        message = str(data.get("message", "")).strip()
        match = _match_email(data.get("target", ""))
        if not match:
            return jsonify({"error": "请提供有效的目标邮箱地址"}), 400
        if not message:
            return jsonify({"error": "消息不能为空"}), 400
        username = current_user()["username"]
        user = _session_user(username, match.group(0))
        analyzer = get_analyzer(username)
        if not ensure_session(username, match.group(0)):
            return jsonify({"error": "没有该邮箱的缓存邮件，请先发起分析"}), 409
        try:
            reply = analyzer.chat(user, message)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify({"reply": reply})

    @app.post("/api/chat/stream")
    @login_required
    def chat_stream():
        """流式追问：text/plain 分块返回增量文本。参数校验失败时仍返回 JSON 错误。"""
        data = request.get_json(silent=True) or {}
        message = str(data.get("message", "")).strip()
        match = _match_email(data.get("target", ""))
        if not match:
            return jsonify({"error": "请提供有效的目标邮箱地址"}), 400
        if not message:
            return jsonify({"error": "消息不能为空"}), 400
        username = current_user()["username"]
        target = match.group(0)
        user = _session_user(username, target)
        analyzer = get_analyzer(username)
        if not ensure_session(username, target):
            return jsonify({"error": "没有该邮箱的缓存邮件，请先发起分析"}), 409

        def gen():
            try:
                yield from analyzer.chat_stream(user, message)
            except Exception as exc:
                yield f"\n\n[出错：{exc}]"

        return Response(gen(), mimetype="text/plain; charset=utf-8")

    @app.get("/api/conversation/<path:email>")
    @login_required
    def conversation_detail(email: str):
        """当前进行中的会话问答记录，供前端切换目标后回填聊天窗。"""
        match = _match_email(email)
        if not match:
            return jsonify({"error": "请提供有效的邮箱地址"}), 400
        target = match.group(0)
        username = current_user()["username"]
        analyzer = get_analyzer(username)
        user = _session_user(username, target)
        messages = analyzer.get_conversation(user) if analyzer.has_session(user) else []
        return jsonify({"target": target, "messages": messages,
                        "session_index": history.get_active_index(user_history_dir(), target)})

    @app.get("/api/emails/<path:email>")
    @login_required
    def email_list(email: str):
        """某目标的往来邮件元数据列表（含正文预览）。

        带 q 参数时按关键词做 grep 式全文检索（忽略大小写子串匹配，
        覆盖主题、收发件人、正文全文、附件名），额外返回命中的 idx 列表。
        """
        match = _match_email(email)
        if not match:
            return jsonify({"error": "请提供有效的邮箱地址"}), 400
        target = match.group(0)
        emails = load_emails(user_cache_dir(current_user()["username"]), target)
        payload = {
            "target": target,
            "count": len(emails),
            "emails": [{
                "idx": i,
                "date": m.date.isoformat(timespec="seconds"),
                "subject": m.subject,
                "from_addr": m.from_addr,
                "to_addrs": m.to_addrs,
                "direction": m.direction,
                "preview": m.body.strip()[:200],
                "attachments": m.attachments,
            } for i, m in enumerate(emails)],
        }
        q = request.args.get("q", "").strip().lower()
        if q:
            payload["hits"] = [
                i for i, m in enumerate(emails)
                if q in m.subject.lower()
                or q in m.from_addr.lower()
                or q in m.to_addrs.lower()
                or q in m.body.lower()
                or any(q in a.lower() for a in m.attachments)
            ]
        return jsonify(payload)

    @app.get("/api/emails/<path:email>/<int:idx>")
    @login_required
    def email_detail(email: str, idx: int):
        """单封邮件完整缓存内容（正文长度受 EMAIL_BODY_CHARS 限制）。"""
        match = _match_email(email)
        if not match:
            return jsonify({"error": "请提供有效的邮箱地址"}), 400
        emails = load_emails(user_cache_dir(current_user()["username"]), match.group(0))
        if idx < 0 or idx >= len(emails):
            return jsonify({"error": "邮件不存在"}), 404
        m = emails[idx]
        return jsonify({
            "date": m.date.isoformat(timespec="seconds"),
            "subject": m.subject,
            "from_addr": m.from_addr,
            "to_addrs": m.to_addrs,
            "direction": m.direction,
            "body": m.body,
            "html": m.html,
            "attachments": m.attachments,
        })

    @app.get("/api/history")
    @login_required
    def history_list():
        return jsonify({"targets": history.list_targets(user_history_dir())})

    @app.get("/api/history/<path:email>")
    @login_required
    def history_detail(email: str):
        match = _match_email(email)
        if not match:
            return jsonify({"error": "请提供有效的邮箱地址"}), 400
        sessions = history.get_sessions(user_history_dir(), match.group(0))
        return jsonify({"target": match.group(0), "sessions": sessions})

    @app.delete("/api/history/<path:email>")
    @login_required
    def history_delete(email: str):
        """删除目标：清内存会话、历史记录与本地邮件缓存。"""
        match = _match_email(email)
        if not match:
            return jsonify({"error": "请提供有效的邮箱地址"}), 400
        target = match.group(0)
        username = current_user()["username"]
        get_analyzer(username).reset(_session_user(username, target))
        removed = history.delete_target(user_history_dir(), target)
        delete_cache(user_cache_dir(username), target)
        delete_summaries(user_cache_dir(username), target)
        if not removed:
            return jsonify({"error": "该目标暂无记录"}), 404
        return jsonify({"ok": True})

    @app.get("/api/reports/<path:email>")
    @login_required
    def report_versions(email: str):
        """某目标的报告版本列表（含内容，按版本号升序）。"""
        match = _match_email(email)
        if not match:
            return jsonify({"error": "请提供有效的邮箱地址"}), 400
        return jsonify({"target": match.group(0),
                        "reports": history.get_reports(user_history_dir(), match.group(0))})

    @app.get("/api/report-prompt")
    @login_required
    def report_prompt_default():
        """当前用户生效的报告生成指令（个人设置优先），供前端展示与编辑。"""
        username = current_user()["username"]
        return jsonify({
            "prompt": effective_report_prompt(username),
            "default": REPORT_INSTRUCTION,
            "customized": bool(users.get_report_prompt(username)),
        })

    @app.post("/api/report-prompt")
    @login_required
    def report_prompt_save():
        """保存当前用户的报告默认提示词；空串表示恢复系统默认。"""
        data = request.get_json(silent=True) or {}
        prompt = str(data.get("prompt", "")).strip()
        try:
            users.set_report_prompt(current_user()["username"], prompt)
        except UserError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.post("/api/report")
    @login_required
    def report_generate():
        """基于当前会话（邮件 + 对话）生成新版报告。"""
        data = request.get_json(silent=True) or {}
        match = _match_email(data.get("target", ""))
        if not match:
            return jsonify({"error": "请提供有效的目标邮箱地址"}), 400
        target = match.group(0)
        username = current_user()["username"]
        user = _session_user(username, target)
        analyzer = get_analyzer(username)
        if not ensure_session(username, target):
            return jsonify({"error": "没有该邮箱的缓存邮件，请先发起分析"}), 409
        # 报告生成指令：前端可查看并修改，留空则用个人默认（未设置时为系统默认）
        prompt = str(data.get("prompt", "")).strip() or effective_report_prompt(username)
        # 报告溯源：素材范围以会话实际注入的邮件为准（可能是用户选中的子集）
        info = analyzer.session_info(user)
        emails = load_emails(user_cache_dir(username), target)
        conversation = analyzer.get_conversation(user)
        meta = {
            "email_count": info.get("email_count", len(emails)),
            "date_range": info.get("date_range") or (
                f"{emails[0].date:%Y-%m-%d %H:%M} ~ {emails[-1].date:%Y-%m-%d %H:%M}"
                if emails else ""),
            "chat_turns": sum(1 for m in conversation if m.get("role") == "user"),
            "prompt": prompt,
            "conversation": [
                {"role": m["role"],
                 "content": m["content"][:2000] + " …" if len(m["content"]) > 2000 else m["content"]}
                for m in conversation
            ],
        }
        try:
            report = analyzer.chat(user, prompt)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        version = history.add_report(user_history_dir(), target, report, meta=meta)
        entry = history.get_reports(user_history_dir(), target)[-1]
        return jsonify({"version": version, "time": entry["time"], "report": report})

    @app.post("/api/report/stream")
    @login_required
    def report_generate_stream():
        """流式生成报告：分块返回正文；全部完成后才落库为一个新版本。"""
        data = request.get_json(silent=True) or {}
        match = _match_email(data.get("target", ""))
        if not match:
            return jsonify({"error": "请提供有效的目标邮箱地址"}), 400
        target = match.group(0)
        username = current_user()["username"]
        user = _session_user(username, target)
        analyzer = get_analyzer(username)
        if not ensure_session(username, target):
            return jsonify({"error": "没有该邮箱的缓存邮件，请先发起分析"}), 409
        prompt = str(data.get("prompt", "")).strip() or effective_report_prompt(username)
        # 溯源信息与同步版一致：以生成前的会话为准
        info = analyzer.session_info(user)
        emails = load_emails(user_cache_dir(username), target)
        conversation = analyzer.get_conversation(user)
        meta = {
            "email_count": info.get("email_count", len(emails)),
            "date_range": info.get("date_range") or (
                f"{emails[0].date:%Y-%m-%d %H:%M} ~ {emails[-1].date:%Y-%m-%d %H:%M}"
                if emails else ""),
            "chat_turns": sum(1 for m in conversation if m.get("role") == "user"),
            "prompt": prompt,
            "conversation": [
                {"role": m["role"],
                 "content": m["content"][:2000] + " …" if len(m["content"]) > 2000 else m["content"]}
                for m in conversation
            ],
        }

        def gen():
            full = []
            try:
                for delta in analyzer.chat_stream(user, prompt):
                    full.append(delta)
                    yield delta
            except Exception as exc:
                yield f"\n\n[出错：{exc}]"
                return
            history.add_report(user_history_dir(), target, "".join(full).strip(), meta=meta)

        return Response(gen(), mimetype="text/plain; charset=utf-8")

    @app.post("/api/report/save")
    @login_required
    def report_save():
        """把对话流中的一条助手回复保存为新报告版本（前端「存为报告」按钮）。"""
        data = request.get_json(silent=True) or {}
        match = _match_email(data.get("target", ""))
        if not match:
            return jsonify({"error": "请提供有效的目标邮箱地址"}), 400
        target = match.group(0)
        content = str(data.get("content", "")).strip()
        if not content:
            return jsonify({"error": "保存内容为空"}), 400
        username = current_user()["username"]
        user = _session_user(username, target)
        analyzer = get_analyzer(username)
        # 溯源信息与生成版一致：以当前会话实际注入的素材为准
        info = analyzer.session_info(user)
        emails = load_emails(user_cache_dir(username), target)
        conversation = analyzer.get_conversation(user)
        meta = {
            "email_count": info.get("email_count", len(emails)),
            "date_range": info.get("date_range") or (
                f"{emails[0].date:%Y-%m-%d %H:%M} ~ {emails[-1].date:%Y-%m-%d %H:%M}"
                if emails else ""),
            "chat_turns": sum(1 for m in conversation if m.get("role") == "user"),
            "conversation": [
                {"role": m["role"],
                 "content": m["content"][:2000] + " …" if len(m["content"]) > 2000 else m["content"]}
                for m in conversation
            ],
        }
        version = history.add_report(user_history_dir(), target, content, meta=meta)
        return jsonify({"version": version})

    @app.get("/api/export/<path:email>")
    @login_required
    def export_docx(email: str):
        """导出该目标的报告（默认最新版，可用 ?version=N 指定）与历史会话为 Word 文档。"""
        match = _match_email(email)
        if not match:
            return jsonify({"error": "请提供有效的邮箱地址"}), 400
        target = match.group(0)
        sessions = history.get_sessions(user_history_dir(), target)
        if not sessions:
            return jsonify({"error": "该邮箱暂无分析记录，无法导出"}), 404
        reports = history.get_reports(user_history_dir(), target)
        version_no = request.args.get("version", type=int)
        picked = None
        if version_no is not None:
            picked = next((r for r in reports if r["version"] == version_no), None)
        if picked is None and reports:
            picked = reports[-1]
        content = build_report_docx(target, picked["content"] if picked else "", sessions)
        filename = f"邮件分析-{target}-{time.strftime('%Y-%m-%d')}.docx"
        return Response(
            content,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
            },
        )

    @app.delete("/api/reports/<path:email>/<int:version>")
    @login_required
    def report_delete(email: str, version: int):
        """删除某目标的指定版本报告，其他版本不受影响。"""
        match = _match_email(email)
        if not match:
            return jsonify({"error": "请提供有效的邮箱地址"}), 400
        if not history.delete_report(user_history_dir(), match.group(0), version):
            return jsonify({"error": "该版本不存在"}), 404
        return jsonify({"ok": True})

    @app.post("/api/reset")
    @login_required
    def reset():
        data = request.get_json(silent=True) or {}
        match = _match_email(data.get("target", ""))
        if not match:
            return jsonify({"error": "请提供有效的目标邮箱地址"}), 400
        username = current_user()["username"]
        get_analyzer(username).reset(_session_user(username, match.group(0)))
        return jsonify({"ok": True})

    return app


if __name__ == "__main__":
    application = create_app()
    port = int(os.environ.get("PORT", 8000))
    try:
        from waitress import serve
        print(f"服务已启动：http://localhost:{port}")
        serve(application, host="0.0.0.0", port=port, threads=8)
    except ImportError:
        application.run(host="0.0.0.0", port=port, threaded=True)
