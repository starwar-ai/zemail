"""Web 界面入口：独立前端页面，无需企业微信。

分析耗时较长（IMAP 抓取 + LLM 生成），采用「后台任务 + 前端轮询」模式；
追问走同步接口。分析会话按「登录用户 + 目标邮箱」隔离，历史记录按用户分目录存储。

用户与权限：所有 /api/* 接口需登录；admin 可管理用户，普通用户只能访问自己的数据。

用法：python web.py，然后浏览器打开 http://localhost:8000
生产部署：waitress-serve --listen=0.0.0.0:8000 --call web:create_app
"""
import os
import re
import secrets
import threading
import time
import uuid
from urllib.parse import quote

from flask import Flask, Response, jsonify, request, session

import history
from analyzer import Analyzer, LLMClient, REPORT_INSTRUCTION
from auth import UserError, UserStore, admin_required, current_user, login_required
from bot import EMAIL_RE
from config import load_config
from exporter import build_report_docx
from mailer import IMAPFetcher
from mailer.cache import load_emails

JOB_TTL_SECONDS = 3600

NO_MAIL_HINT = (
    "可能原因：超过配置的查找时间范围、目标地址拼写有误，"
    "或「已发送」文件夹未被纳入（可在 MAIL_FOLDERS 中配置）。"
)


def _session_user(username: str, target: str) -> str:
    return f"web:{username}:{target}"


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.@-]", "_", name)


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


def create_app(cfg=None):
    cfg = cfg or load_config()
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.secret_key = _load_or_create_secret_key(cfg)
    missing = [name for name, ok in [
        ("IMAP_HOST", cfg.imap_host),
        ("EMAIL_ADDRESS", cfg.email_address),
        ("EMAIL_PASSWORD", cfg.email_password),
        ("LLM_API_KEY", cfg.llm_api_key),
    ] if not ok]
    if missing:
        app.logger.warning("配置缺失：%s（请检查 .env）", ", ".join(missing))
        print(f"⚠️  配置缺失：{', '.join(missing)}，请检查 .env 后重启", flush=True)
    fetcher = IMAPFetcher(
        host=cfg.imap_host, port=cfg.imap_port,
        user=cfg.email_address, password=cfg.email_password,
        folders=cfg.mail_folders, lookback_days=cfg.mail_lookback_days,
        max_results=cfg.mail_max_results, body_chars=cfg.email_body_chars,
        cache_dir=cfg.email_cache_dir,
    )
    llm = LLMClient(cfg.llm_api_base, cfg.llm_api_key, cfg.llm_model)

    users = UserStore(cfg.users_file)
    if users.is_empty():
        users.create(cfg.admin_username, cfg.admin_password, "admin")
        print(f"已创建初始管理员 {cfg.admin_username}（密码来自 ADMIN_PASSWORD，"
              f"默认为 admin123，请登录后尽快修改）", flush=True)
    _migrate_legacy_history(cfg.session_history_dir, cfg.admin_username)

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
                )
            return analyzers[username]

    def user_history_dir() -> str:
        return os.path.join(cfg.session_history_dir, _safe_name(current_user()["username"]))

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

    def run_analysis(job_id: str, username: str, target: str) -> None:
        set_job(job_id, stage="fetch")
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
        set_job(job_id, stage="llm", count=len(emails),
                date_range=f"{emails[0].date:%Y-%m-%d} ~ {emails[-1].date:%Y-%m-%d}")
        try:
            report = get_analyzer(username).start_analysis(
                _session_user(username, target), target, emails)
        except Exception as exc:
            set_job(job_id, status="error",
                    error=f"AI 分析失败（{llm.api_base}）：{exc}")
            return
        set_job(job_id, status="done", target=target, report=report)

    def _match_email(raw: str):
        return EMAIL_RE.search(str(raw or ""))

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

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
            emails = load_emails(cfg.email_cache_dir, target)
            result.append({
                "target": target,
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
        return jsonify({"has_session": get_analyzer(username).has_session(
            _session_user(username, match.group(0)))})

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
        if not analyzer.has_session(user):
            return jsonify({"error": "该邮箱没有进行中的分析会话，请先发起分析"}), 409
        try:
            reply = analyzer.chat(user, message)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify({"reply": reply})

    @app.get("/api/emails/<path:email>")
    @login_required
    def email_list(email: str):
        """某目标的往来邮件元数据列表（含正文预览）。"""
        match = _match_email(email)
        if not match:
            return jsonify({"error": "请提供有效的邮箱地址"}), 400
        target = match.group(0)
        emails = load_emails(cfg.email_cache_dir, target)
        return jsonify({
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
            } for i, m in enumerate(emails)],
        })

    @app.get("/api/emails/<path:email>/<int:idx>")
    @login_required
    def email_detail(email: str, idx: int):
        """单封邮件完整缓存内容（正文长度受 EMAIL_BODY_CHARS 限制）。"""
        match = _match_email(email)
        if not match:
            return jsonify({"error": "请提供有效的邮箱地址"}), 400
        emails = load_emails(cfg.email_cache_dir, match.group(0))
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

    @app.get("/api/reports/<path:email>")
    @login_required
    def report_versions(email: str):
        """某目标的报告版本列表（含内容，按版本号升序）。"""
        match = _match_email(email)
        if not match:
            return jsonify({"error": "请提供有效的邮箱地址"}), 400
        return jsonify({"target": match.group(0),
                        "reports": history.get_reports(user_history_dir(), match.group(0))})

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
        if not analyzer.has_session(user):
            return jsonify({"error": "该邮箱没有进行中的分析会话，请先发起分析"}), 409
        try:
            report = analyzer.chat(user, REPORT_INSTRUCTION)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        version = history.add_report(user_history_dir(), target, report)
        entry = history.get_reports(user_history_dir(), target)[-1]
        return jsonify({"version": version, "time": entry["time"], "report": report})

    @app.get("/api/export/<path:email>")
    @login_required
    def export_docx(email: str):
        """导出该目标的最新报告与历史会话为 Word 文档。"""
        match = _match_email(email)
        if not match:
            return jsonify({"error": "请提供有效的邮箱地址"}), 400
        target = match.group(0)
        sessions = history.get_sessions(user_history_dir(), target)
        if not sessions:
            return jsonify({"error": "该邮箱暂无分析记录，无法导出"}), 404
        reports = history.get_reports(user_history_dir(), target)
        latest = reports[-1]["content"] if reports else ""
        content = build_report_docx(target, latest, sessions)
        filename = f"邮件分析-{target}-{time.strftime('%Y-%m-%d')}.docx"
        return Response(
            content,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
            },
        )

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
