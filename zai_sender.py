"""ZAI 已确认邮件的发送与查证；发送前由 ZAI 原子领取一次性凭据。"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import imaplib
import json
import re
import smtplib
import ssl
import time
import uuid
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, getaddresses
from urllib.parse import urlsplit

import requests
from flask import Blueprint, jsonify, request


MAX_PAYLOAD = 7_000_000
MAX_MESSAGE = 8_000_000
ADDRESS = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}\Z")
MESSAGE_ID = re.compile(r"<[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+>\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")


class SenderError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Account:
    address: str
    password: str
    imap_host: str
    imap_port: int


def _text(value, maximum, label, header=False):
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise SenderError(f"{label}无效")
    if header and any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise SenderError(f"{label}无效")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise SenderError(f"{label}编码无效") from None
    return value


def _object(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise SenderError("请求字段无效")
    return value


def _json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise SenderError("请求包含重复字段")
            value[key] = item
        return value
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (ValueError, TypeError, RecursionError):
        raise SenderError("JSON 格式无效") from None


def parse_delivery(data, sending):
    _object(data, ("deliveryId", "messageId", "payload", "token") if sending else ("deliveryId", "messageId", "payload"))
    delivery_id = _text(data["deliveryId"], 36, "发送标识", True)
    try:
        if str(uuid.UUID(delivery_id)) != delivery_id:
            raise ValueError()
    except ValueError:
        raise SenderError("发送标识无效") from None
    message_id = _text(data["messageId"], 254, "邮件标识", True)
    if not MESSAGE_ID.fullmatch(message_id):
        raise SenderError("邮件标识无效")
    raw = _text(data["payload"], MAX_PAYLOAD, "发送内容")
    if len(raw.encode("utf-8")) > MAX_PAYLOAD:
        raise SenderError("发送内容超过体积上限", 413)
    payload = _object(_json(raw), ("sourceUser", "sender", "recipient", "subject", "body", "attachments"))
    _text(payload["sourceUser"], 64, "来源用户", True)
    for key in ("sender", "recipient"):
        address = _text(payload[key], 254, "邮箱", True)
        if not ADDRESS.fullmatch(address):
            raise SenderError("邮箱无效")
    _text(payload["subject"], 500, "主题", True)
    _text(payload["body"], 20_000, "正文")
    attachments = payload["attachments"]
    if not isinstance(attachments, list) or len(attachments) > 5:
        raise SenderError("附件数量无效")
    total = 0
    decoded = []
    for attachment in attachments:
        _object(attachment, ("name", "mimeType", "sha256", "content"))
        name = _text(attachment["name"], 255, "附件名", True)
        if "/" in name or "\\" in name or name in (".", ".."):
            raise SenderError("附件名无效")
        mime = _text(attachment["mimeType"], 127, "附件类型", True)
        if not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", mime):
            raise SenderError("附件类型无效")
        digest = _text(attachment["sha256"], 64, "附件指纹", True)
        if not SHA256.fullmatch(digest) or not isinstance(attachment["content"], str):
            raise SenderError("附件指纹无效")
        try:
            content = base64.b64decode(attachment["content"], validate=True)
        except (ValueError, binascii.Error):
            raise SenderError("附件编码无效") from None
        total += len(content)
        if total > 4_000_000 or hashlib.sha256(content).hexdigest() != digest:
            raise SenderError("附件体积或内容指纹不一致")
        decoded.append(content)
    if sending and not SHA256.fullmatch(_text(data["token"], 64, "领取凭据", True)):
        raise SenderError("领取凭据无效")
    return payload, hashlib.sha256(raw.encode("utf-8")).hexdigest(), decoded


def build_message(data, payload, digest, decoded):
    message = EmailMessage(policy=policy.SMTP)
    message["Message-ID"] = data["messageId"]
    message["From"] = payload["sender"]
    message["To"] = payload["recipient"]
    message["Subject"] = payload["subject"]
    message["Date"] = formatdate(localtime=False, usegmt=True)
    message["X-ZAI-Delivery-ID"] = data["deliveryId"]
    message["X-ZAI-Content-SHA256"] = digest
    message.set_content(payload["body"], cte="quoted-printable")
    for attachment, content in zip(payload["attachments"], decoded):
        main, sub = attachment["mimeType"].split("/")
        message.add_attachment(content, maintype=main, subtype=sub, filename=attachment["name"])
    return message


def matches_message(raw, data, payload, digest):
    """不只信任自定义头，同时核对封存收发件人、正文与逐个附件。"""
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        for key, expected in (("Message-ID", data["messageId"]), ("X-ZAI-Delivery-ID", data["deliveryId"]), ("X-ZAI-Content-SHA256", digest), ("Subject", payload["subject"])):
            actual = str(message[key]) if key == "Subject" else str(message[key]).strip()
            if len(message.get_all(key, [])) != 1 or actual != expected:
                return False
        for key, address in (("From", payload["sender"]), ("To", payload["recipient"])):
            if len(message.get_all(key, [])) != 1 or getaddresses(message.get_all(key, [])) != [("", address)]:
                return False
        if message.get_all("Cc") or message.get_all("Bcc"):
            return False
        parts = list(message.iter_parts()) if message.is_multipart() else [message]
        if len(parts) != len(payload["attachments"]) + 1 or parts[0].get_content_type() != "text/plain":
            return False
        body = payload["body"].replace("\r\n", "\n").replace("\r", "\n")
        if not body.endswith("\n"):
            body += "\n"
        if parts[0].get_content().replace("\r\n", "\n").replace("\r", "\n") != body:
            return False
        for part, attachment in zip(parts[1:], payload["attachments"]):
            if part.is_multipart() or part.get_filename() != attachment["name"] or part.get_content_type() != attachment["mimeType"].lower():
                return False
            if hashlib.sha256(part.get_payload(decode=True)).hexdigest() != attachment["sha256"]:
                return False
        return not message.defects
    except (ValueError, TypeError, KeyError, LookupError, UnicodeError):
        return False


class ZaiSender:
    def __init__(self, cfg, users, smtp_factory=smtplib.SMTP_SSL, imap_factory=imaplib.IMAP4_SSL, http_post=requests.post):
        self.cfg, self.users = cfg, users
        self.smtp_factory, self.imap_factory, self.http_post = smtp_factory, imap_factory, http_post

    def account(self, source_user, sending=True):
        _text(source_user, 64, "来源用户", True)
        allowed = {name.strip() for name in self.cfg.zai_send_users.split(",") if name.strip()}
        if not self.users.get_role(source_user) or (sending and source_user not in allowed):
            raise SenderError("来源用户未获得发送授权", 403)
        settings = self.users.get_mail_settings(source_user)
        account = Account(settings.get("email_address") or self.cfg.email_address,
                          settings.get("email_password") or self.cfg.email_password,
                          settings.get("imap_host") or self.cfg.imap_host,
                          int(settings.get("imap_port") or self.cfg.imap_port))
        if not ADDRESS.fullmatch(account.address) or not account.password or not account.imap_host:
            raise SenderError("来源邮箱配置不完整", 503)
        if sending and not self.cfg.smtp_host:
            raise SenderError("SMTP 尚未配置", 503)
        return account

    def claim(self, data, digest):
        base = self.cfg.zai_api_url.rstrip("/")
        url = urlsplit(base)
        if not base or not self.cfg.zai_api_key or url.username or url.password or url.query or url.fragment or not (url.scheme == "https" or url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1")):
            raise SenderError("ZAI 领取回调尚未安全配置", 503)
        # requests 默认不重试；回调超时/重定向/不明确结果均不发送。
        try:
            with self.http_post(base + "/api/external/email-operations/delivery-claim", json={"deliveryId": data["deliveryId"], "token": data["token"], "contentHash": digest}, headers={"x-api-key": self.cfg.zai_api_key}, timeout=(3, 8), allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    return None
                chunks, size = [], 0
                for chunk in response.iter_content(4096):
                    size += len(chunk)
                    if size > 16_000:
                        return None
                    chunks.append(chunk)
                result = _json(b"".join(chunks))
                claimed = result.get("data", {}).get("claimed") if isinstance(result, dict) and isinstance(result.get("data"), dict) else None
                return claimed if isinstance(claimed, bool) else None
        except (requests.RequestException, SenderError, ValueError):
            return None

    def _imap(self, account):
        connection = self.imap_factory(account.imap_host, account.imap_port, ssl_context=ssl.create_default_context(), timeout=8)
        try:
            connection.login(account.address, account.password)
            return connection
        except Exception:
            self._close(connection, "logout")
            raise

    @staticmethod
    def _close(connection, method):
        try:
            getattr(connection, method)()
        except Exception:
            pass

    def _folder(self):
        folder = _text(self.cfg.mail_sent_folder, 200, "已发送文件夹", True)
        return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def verify(self, data, payload, digest, account):
        connection = None
        try:
            connection = self._imap(account)
            if connection.select(self._folder(), readonly=True)[0] != "OK":
                return {"status": "UNKNOWN", "receipt": ""}
            status, rows = connection.uid("search", None, "HEADER", "Message-ID", '"' + data["messageId"] + '"')
            ids = rows[0].split() if status == "OK" and rows and isinstance(rows[0], bytes) else []
            for mail_id in ids[-20:]:
                status, info = connection.uid("fetch", mail_id, "(RFC822.SIZE)")
                if status != "OK":
                    continue
                size = re.search(rb"RFC822.SIZE (\d+)", b" ".join(row for row in info if isinstance(row, bytes)))
                if not size or int(size.group(1)) > MAX_MESSAGE:
                    continue
                status, records = connection.uid("fetch", mail_id, "(BODY.PEEK[])")
                if status != "OK":
                    continue
                for record in records:
                    if isinstance(record, tuple) and isinstance(record[1], bytes) and len(record[1]) <= MAX_MESSAGE and matches_message(record[1], data, payload, digest):
                        return {"status": "ACCEPTED", "receipt": "SENT_ARCHIVE:" + data["messageId"]}
        except (OSError, imaplib.IMAP4.error, ValueError):
            pass
        finally:
            if connection is not None:
                self._close(connection, "logout")
        return {"status": "UNKNOWN", "receipt": ""}

    def deliver(self, data, sending):
        payload, digest, decoded = parse_delivery(data, sending)
        account = self.account(payload["sourceUser"], sending)
        if payload["sender"].lower() != account.address.lower():
            raise SenderError("发件人与当前来源邮箱不一致", 409)
        if not sending:
            return self.verify(data, payload, digest, account)
        # 在领取前构造并验证完整 MIME，避免领取后才发现无效头或附件。
        message = build_message(data, payload, digest, decoded)
        raw = message.as_bytes()
        claimed = self.claim(data, digest)
        if claimed is not True:
            return self.verify(data, payload, digest, account) if claimed is False else {"status": "UNKNOWN", "receipt": ""}
        smtp = None
        try:
            smtp = self.smtp_factory(self.cfg.smtp_host, self.cfg.smtp_port, timeout=8, context=ssl.create_default_context())
            smtp.login(account.address, account.password)
            refused = smtp.send_message(message, from_addr=account.address, to_addrs=[payload["recipient"]])
            if refused:
                return {"status": "REJECTED", "receipt": "SMTP_REFUSED"}
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError, smtplib.SMTPAuthenticationError):
            return {"status": "REJECTED", "receipt": "SMTP_REFUSED"}
        except (OSError, smtplib.SMTPException):
            return {"status": "UNKNOWN", "receipt": ""}
        finally:
            if smtp is not None:
                self._close(smtp, "close")
        connection = None
        try:
            connection = self._imap(account)
            connection.append(self._folder(), "\\Seen", imaplib.Time2Internaldate(time.time()), raw)
        except (OSError, imaplib.IMAP4.error, ValueError):
            pass  # SMTP 已接受的事实不因归档失败而被改写；未知时仅查证。
        finally:
            if connection is not None:
                self._close(connection, "logout")
        return {"status": "ACCEPTED", "receipt": "SMTP_ACCEPTED:" + data["messageId"]}


def register_zai_sender(app, cfg, users, sender=None):
    adapter = sender or ZaiSender(cfg, users)
    blueprint = Blueprint("zai_sender", __name__, url_prefix="/api/zai/quotation")

    @blueprint.before_request
    def authorize():
        if not cfg.zai_send_token:
            raise SenderError("ZAI 邮件发送尚未启用", 503)
        supplied = request.headers.get("Authorization", "")
        expected = "Bearer " + cfg.zai_send_token
        if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            raise SenderError("服务凭据无效", 401)
        if not request.is_json:
            raise SenderError("须使用 JSON 请求")
        if request.content_length is not None and request.content_length > 2 * MAX_PAYLOAD + 4096:
            raise SenderError("请求超过体积上限", 413)
        # 包括未知长度的分块请求，均限制读取量。
        raw = request.stream.read(2 * MAX_PAYLOAD + 4097)
        if len(raw) > 2 * MAX_PAYLOAD + 4096:
            raise SenderError("请求超过体积上限", 413)
        from flask import g
        g.zai_sender_data = _json(raw)

    @blueprint.errorhandler(SenderError)
    def business_error(error):
        return jsonify({"error": str(error)}), error.status

    @blueprint.post("/account")
    def account():
        from flask import g
        data = _object(g.zai_sender_data, ("sourceUser",))
        return jsonify({"sender": adapter.account(data["sourceUser"]).address})

    def execute(sending):
        from flask import g
        return jsonify(adapter.deliver(g.zai_sender_data, sending))

    blueprint.add_url_rule("/send", "send", lambda: execute(True), methods=["POST"])
    blueprint.add_url_rule("/verify", "verify", lambda: execute(False), methods=["POST"])
    app.register_blueprint(blueprint)
    return adapter
