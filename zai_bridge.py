"""将有原文依据的候选事项交给 ZAI；真实身份和业务授权由 ZAI 裁决。"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from urllib.parse import urlsplit

import requests


KINDS = {"询价": "INQUIRY", "报价": "INQUIRY", "订单": "ORDER", "合同": "ORDER", "交付": "DELIVERY", "出运": "SHIPMENT", "付款": "PAYMENT", "收款": "PAYMENT", "投诉": "COMPLAINT", "变更": "CHANGE"}


class BridgeError(Exception):
    """不包含请求正文、服务密钥或远端任意错误内容的业务提示。"""


def source_id(mail):
    key = mail.message_id.strip() or json.dumps([mail.date.isoformat(), mail.subject, mail.from_addr, mail.to_addrs, mail.body], ensure_ascii=False)
    return "mail:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def _evidence(body, quote, maximum=2000):
    if not isinstance(quote, str):
        return None
    quote = quote.strip()
    normalize = lambda text: re.sub(r"\s+", " ", text).strip()
    return quote if 4 <= len(quote) <= maximum and normalize(quote) in normalize(body) else None


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return value if dt.date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def _number_in(quote, number):
    if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
        return False
    return any(float(value.replace(",", "")) == number for value in re.findall(r"(?<![\w.+-])[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?(?![\w.])", quote))


def _quoted(body, value):
    if not isinstance(value, dict):
        return None
    text, quote = value.get("value"), _evidence(body, value.get("evidence"), 300)
    return {"value": text, "evidence": quote} if isinstance(text, str) and 1 <= len(text) <= 40 and quote and text in quote else None


def quotation_candidate(body, raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("lines"), list) or not 1 <= len(raw["lines"]) <= 10:
        return None
    result = {"lines": []}
    for key in ("currency", "tradeTerm"):
        if raw.get(key) is not None:
            value = _quoted(body, raw[key])
            if not value:
                return None
            result[key] = value
    for row in raw["lines"]:
        if not isinstance(row, dict):
            return None
        quote, product, quantity, unit = _evidence(body, row.get("evidence"), 300), row.get("product"), row.get("quantity"), row.get("unit", "")
        if not quote or not isinstance(product, str) or not 1 <= len(product) <= 120 or product not in quote or not isinstance(unit, str) or len(unit) > 40 or unit and unit not in quote or not _number_in(quote, quantity) or not 0 < quantity <= 1e9:
            return None
        result["lines"].append({"product": product, "quantity": quantity, "unit": unit, "evidence": quote})
    return result if len(json.dumps(result, ensure_ascii=False)) <= 4500 else None


def procurement_candidate(body, raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("lines"), list) or not 1 <= len(raw["lines"]) <= 10:
        return None
    contract = _quoted(body, raw.get("contractCode"))
    if not contract:
        return None
    result = {"contractCode": contract, "lines": []}
    for row in raw["lines"]:
        if not isinstance(row, dict):
            return None
        quote, product = _evidence(body, row.get("evidence"), 500), row.get("product")
        if not quote or not isinstance(product, str) or not 1 <= len(product) <= 120 or product not in quote:
            return None
        line = {"product": product, "evidence": quote}
        if row.get("progressPercent") is not None:
            number = row["progressPercent"]
            if not _number_in(quote, number) or not 0 <= number <= 100:
                return None
            line["progressPercent"] = number
        if row.get("expectedFinishDate") is not None:
            date = _date(row["expectedFinishDate"])
            if not date or date not in quote:
                return None
            line["expectedFinishDate"] = date
        if len(line) == 2:
            return None
        result["lines"].append(line)
    return result if len(json.dumps(result, ensure_ascii=False)) <= 4500 else None


def build_payload(source_user, counterparty, mail, card):
    body = mail.body[:5000]
    if not body.strip() or mail.date.year < 1970:
        return None
    direction = {"收到": "INBOUND", "发出": "OUTBOUND"}.get(mail.direction, "UNKNOWN")
    kind = KINDS.get(card.get("intent"), "FOLLOW_UP") if isinstance(card, dict) else "FOLLOW_UP"
    items, keys = [], set()
    for todo in (card.get("todos") or []) if isinstance(card, dict) else []:
        if not isinstance(todo, dict):
            continue
        evidence = _evidence(body, todo.get("evidence"))
        title = todo.get("text")
        if not evidence or not isinstance(title, str) or not title.strip():
            continue
        item_id = hashlib.sha256((title.strip() + "\n" + evidence).encode()).hexdigest()[:32]
        if item_id in keys:
            continue
        keys.add(item_id)
        items.append({"itemId": item_id, "title": title.strip()[:200], "kind": kind, "owner": todo.get("owner") if todo.get("owner") in ("me", "them") else "unknown", "dueDate": _date(todo.get("due")), "evidence": evidence})
        if len(items) == 10:
            break
    in_reply_to = mail.in_reply_to.strip()
    references = re.findall(r"<[^<>\r\n]{1,498}>", mail.references)[-20:]
    if not items and direction == "INBOUND" and (in_reply_to or references) and len(body.strip()) >= 4:
        items.append({"itemId": "reply", "title": "核对后续邮件", "kind": "FOLLOW_UP", "owner": "me", "dueDate": None, "evidence": body.strip()[:600]})
    if not items:
        return None
    if direction == "INBOUND":
        candidate = quotation_candidate(body, card.get("quotation")) if isinstance(card, dict) else None
        target = next((item for item in items if item["kind"] == "INQUIRY" and item["owner"] == "me"), None)
        if candidate and target:
            target["quotation"] = candidate
        candidate = procurement_candidate(body, card.get("procurement")) if isinstance(card, dict) else None
        target = next((item for item in items if item["kind"] in ("DELIVERY", "FOLLOW_UP", "CHANGE") and "quotation" not in item), None)
        if candidate and target:
            target["procurement"] = candidate
    sent_at = mail.date.astimezone(dt.timezone.utc).isoformat()
    payload = {"sourceUser": source_user, "sourceId": source_id(mail), "messageId": mail.message_id[:500], "subject": mail.subject[:500], "sender": mail.from_addr[:500], "recipients": mail.to_addrs[:5000], "sentAt": sent_at, "counterpartyEmail": counterparty.lower(), "body": body,
               "bodyTruncated": len(mail.body) > 5000 or "正文过长已截断" in mail.body or getattr(mail, "body_truncated", False), "direction": direction, "attachmentNames": [name[:250] for name in mail.attachments[:20] if isinstance(name, str) and name], "items": items}
    if in_reply_to and len(in_reply_to) <= 500 and not re.search(r"[\r\n]", in_reply_to):
        payload["inReplyTo"] = in_reply_to
    if references:
        payload["references"] = references
    return payload


def send_payload(cfg, payload):
    base = cfg.zai_api_url.rstrip("/")
    try:
        url = urlsplit(base)
        if not cfg.zai_api_key or url.username or url.password or url.query or url.fragment or not (url.scheme == "https" or url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1")):
            raise BridgeError("ZAI 接入地址或应用密钥尚未安全配置")
        with requests.post(base + "/api/external/email-operations/intake", json=payload, headers={"x-api-key": cfg.zai_api_key}, timeout=(3, 10), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise BridgeError(f"ZAI 邮件接入未确认（HTTP {response.status_code}），保留原请求待处理")
            chunks, size = [], 0
            for chunk in response.iter_content(4096):
                size += len(chunk)
                if size > 32_000:
                    raise BridgeError("ZAI 接入回执过大，保留原请求待查证")
                chunks.append(chunk)
            result = json.loads(b"".join(chunks))
        items = result.get("data", {}).get("items") if isinstance(result, dict) and isinstance(result.get("data"), dict) else None
        expected = {item["itemId"] for item in payload["items"]}
        if not isinstance(items, list) or len(items) != len(expected) or {item.get("itemId") for item in items if isinstance(item, dict)} != expected or any(not isinstance(item, dict) or not isinstance(item.get("documentId"), str) or not item["documentId"] or not isinstance(item.get("code"), str) or not isinstance(item.get("duplicate"), bool) for item in items):
            raise BridgeError("ZAI 接入回执不完整，保留原请求待查证")
        return [{key: item[key] for key in ("itemId", "documentId", "code", "duplicate")} for item in items]
    except (requests.RequestException, ValueError, TypeError, KeyError):
        raise BridgeError("ZAI 接入结果不明，保留原请求待查证") from None


def sync_intake(cfg, users, username, target, emails, cards, store, send=send_payload):
    allowed = {name.strip() for name in cfg.zai_intake_users.split(",") if name.strip()}
    if cfg.zai_intake_enabled != 1 or username not in allowed or not users.get_role(username):
        return
    staged = []
    from email.utils import getaddresses
    for index, mail in enumerate(emails):
        if mail.direction != "收到":
            continue
        addresses = [address for _, address in getaddresses([mail.from_addr]) if address]
        if len(addresses) != 1:
            continue
        card = cards[index] if index < len(cards) else None
        staged.append((source_id(mail), build_payload(username, addresses[0], mail, card)))
    # 首次为该监控目标启用接入只建基线，避免将历史邮件批量变成新业务。
    store.stage_zai_intake(target, staged)
    for key, payload in store.pending_zai_intake()[:20]:
        try:
            receipt = send(cfg, payload)
        except BridgeError as error:
            if store.fail_zai_intake(key, str(error)):
                store.add_event("zai_intake", target, "邮件接入待处理", str(error))
        else:
            store.ack_zai_intake(key, receipt)
