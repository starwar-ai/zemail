"""配置加载：环境变量优先，自动读取 .env。"""
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
    mail_lookback_days: int = 365
    mail_max_results: int = 100
    email_body_chars: int = 2000
    email_cache_dir: str = "data/emails"

    # 大模型
    llm_api_base: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_context_chars: int = 24000

    session_ttl_minutes: int = 120
    session_history_dir: str = "data/sessions"

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
