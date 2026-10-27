"""配置加载：环境变量优先，自动读取 .env。"""
import json
import os
from dataclasses import dataclass, fields


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Config:
    # 企业微信
    wecom_corpid: str = ""
    wecom_agentid: int = 0
    wecom_secret: str = ""
    wecom_token: str = ""
    wecom_encoding_aes_key: str = ""
    wecom_api_base: str = "https://qyapi.weixin.qq.com"

    # 邮箱 IMAP
    imap_host: str = ""
    imap_port: int = 993
    email_address: str = ""
    email_password: str = ""
    mail_folders: str = ""
    mail_lookback_days: int = 0  # 0 = 不限时间
    mail_max_results: int = 0    # 0 = 不限数量
    email_body_chars: int = 2000
    email_cache_dir: str = "data/emails"

    # ZAI 已确认邮件发送；服务凭据和明确用户白名单缺省为空。
    zai_send_token: str = ""
    zai_send_users: str = ""
    zai_api_url: str = ""
    zai_api_key: str = ""
    zai_intake_enabled: int = 0
    zai_intake_users: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    mail_sent_folder: str = "Sent"

    # 大模型
    llm_api_base: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_card_model: str = ""       # 预处理（信息卡/画像/向量）模型；留空复用 LLM_MODEL
    llm_embedding_model: str = ""  # 向量检索的嵌入模型；留空不启用向量召回
    llm_context_chars: int = 24000

    session_ttl_minutes: int = 120
    session_history_dir: str = "data/sessions"

    # 后台监控（新邮件推送 / 承诺催办 / 未回复提醒）
    monitor_interval_minutes: int = 30  # 轮询间隔
    monitor_data_dir: str = "data/monitor"
    monitor_quiet_start: int = 23  # 静默时段起点（小时，0-23）
    monitor_quiet_end: int = 7     # 静默时段终点（小时，0-23）
    digest_hour: int = 8           # 每日简报推送时间（小时，-1 关闭）
    digest_weekly_day: int = -1    # 每周简报推送日（0=周一…6=周日，-1 关闭）

    # 通讯簿（企业/联系人/多地址）
    contacts_data_dir: str = "data/contacts"

    # Web 用户与权限
    secret_key: str = ""
    users_file: str = "data/users.json"
    admin_username: str = "admin"
    admin_password: str = "admin123"


def load_config() -> Config:
    _load_dotenv()
    cfg = Config()
    for f in fields(cfg):
        raw = os.environ.get(f.name.upper())
        if raw is None or raw == "":
            continue
        if isinstance(f.default, int):
            setattr(cfg, f.name, int(raw))
        else:
            setattr(cfg, f.name, raw)
    return cfg


# 管理员可在后台修改的运行时设置（存 data/settings.json，优先级高于 .env）
RUNTIME_SETTINGS = {
    "mail_lookback_days": ("邮件查找时间范围（天）", 0),
    "mail_max_results": ("单次抓取邮件数上限", 0),
    "email_body_chars": ("单封正文截断长度（字符）", 100),
    "llm_context_chars": ("注入模型的邮件上下文总量（字符）", 1000),
    "session_ttl_minutes": ("分析会话有效期（分钟）", 1),
    "monitor_interval_minutes": ("监控轮询间隔（分钟）", 5),
    "digest_hour": ("每日简报推送时间（小时，-1 关闭）", -1),
    "digest_weekly_day": ("每周简报推送日（0=周一…6=周日，-1 关闭）", -1),
}


def load_runtime_settings(cfg: Config, path: str) -> None:
    """把 settings.json 中保存的值覆盖到 cfg 上。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    for key in RUNTIME_SETTINGS:
        if key in data:
            try:
                setattr(cfg, key, int(data[key]))
            except (TypeError, ValueError):
                continue


def save_runtime_settings(path: str, values: dict) -> None:
    """校验并保存管理员修改的设置。值须为非负整数，且不低于各项最小值。"""
    cleaned = {}
    for key, value in values.items():
        if key not in RUNTIME_SETTINGS:
            raise ValueError(f"不支持的设置项：{key}")
        _, minimum = RUNTIME_SETTINGS[key]
        try:
            ivalue = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{RUNTIME_SETTINGS[key][0]} 必须是整数")
        if ivalue < minimum:
            raise ValueError(f"{RUNTIME_SETTINGS[key][0]} 不能小于 {minimum}")
        cleaned[key] = ivalue
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.update(cleaned)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
