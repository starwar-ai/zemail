"""企业微信回调服务入口。

企业微信要求回调在 5 秒内响应，而抓邮件 + AI 分析耗时较长，
因此收到消息后立即返回 success，后台线程处理完再通过应用消息接口推送结果。
"""
import os
import threading

from flask import Flask, request

from analyzer import Analyzer, LLMClient
from bot import BotCore
from config import load_config
from mailer import IMAPFetcher
from wecom import CryptoError, WeComClient, WXBizMsgCrypt


def build_bot(cfg):
    fetcher = IMAPFetcher(
        host=cfg.imap_host, port=cfg.imap_port,
        user=cfg.email_address, password=cfg.email_password,
        folders=cfg.mail_folders, lookback_days=cfg.mail_lookback_days,
        max_results=cfg.mail_max_results, body_chars=cfg.email_body_chars,
        cache_dir=cfg.email_cache_dir,
    )
    llm = LLMClient(cfg.llm_api_base, cfg.llm_api_key, cfg.llm_model)
    card_llm = LLMClient(cfg.llm_api_base, cfg.llm_api_key,
                         cfg.llm_card_model or cfg.llm_model)
    analyzer = Analyzer(llm, context_chars=cfg.llm_context_chars,
                        ttl_seconds=cfg.session_ttl_minutes * 60,
                        history_dir=cfg.session_history_dir,
                        derived_dir=cfg.email_cache_dir,
                        card_llm=card_llm,
                        embedding_model=cfg.llm_embedding_model)
    return BotCore(fetcher, analyzer)


def create_app(cfg=None):
    cfg = cfg or load_config()
    app = Flask(__name__)
    crypt = WXBizMsgCrypt(cfg.wecom_token, cfg.wecom_encoding_aes_key, cfg.wecom_corpid)
    client = WeComClient(cfg.wecom_corpid, cfg.wecom_secret, cfg.wecom_agentid, cfg.wecom_api_base)
    bot = build_bot(cfg)

    def respond(user: str, content: str) -> None:
        try:
            for reply in bot.handle(user, content):
                try:
                    client.send_markdown(user, reply)
                except Exception:
                    client.send_text(user, reply)
        except Exception as exc:
            try:
                client.send_text(user, f"处理出错：{exc}")
            except Exception:
                app.logger.exception("推送失败提示也发送失败")

    @app.get("/callback")
    def verify():
        try:
            return crypt.verify_url(
                request.args["msg_signature"], request.args["timestamp"],
                request.args["nonce"], request.args["echostr"],
            )
        except (CryptoError, KeyError) as exc:
            return f"验证失败: {exc}", 403

    @app.post("/callback")
    def receive():
        try:
            msg = crypt.decrypt_message(
                request.args["msg_signature"], request.args["timestamp"],
                request.args["nonce"], request.data.decode("utf-8"),
            )
        except (CryptoError, KeyError) as exc:
            app.logger.warning("回调解密失败: %s", exc)
            return "success"  # 解密失败也回 success，避免企微反复重试
        if msg.get("MsgType") == "text":
            threading.Thread(
                target=respond,
                args=(msg.get("FromUserName", ""), msg.get("Content", "")),
                daemon=True,
            ).start()
        return "success"

    @app.get("/health")
    def health():
        return "ok"

    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), threaded=True)
