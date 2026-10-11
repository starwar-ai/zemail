"""往来邮件的本地缓存：按目标地址持久化为 JSON，避免重复抓取。"""
import datetime as dt
import hashlib
import json
import os
import re

from .imap_client import EmailMessage


def _cache_path(cache_dir: str, target: str) -> str:
    safe = re.sub(r"[^\w.@-]", "_", target)
    digest = hashlib.md5(target.encode("utf-8")).hexdigest()[:8]
    return os.path.join(cache_dir, f"{safe}-{digest}.json")


def load_emails(cache_dir: str, target: str) -> list:
    path = _cache_path(cache_dir, target)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        emails = [
            EmailMessage(
                date=dt.datetime.fromisoformat(item["date"]),
                subject=item["subject"],
                from_addr=item["from_addr"],
                to_addrs=item["to_addrs"],
                direction=item["direction"],
                body=item["body"],
                html=item.get("html", ""),
                attachments=item.get("attachments") or [],
                message_id=item.get("message_id", ""),
                in_reply_to=item.get("in_reply_to", ""),
                references=item.get("references", ""),
            )
            for item in data
        ]
        emails.sort(key=lambda m: m.date)
        return emails
    except (OSError, ValueError, KeyError, TypeError):
        return []


def save_emails(cache_dir: str, target: str, emails: list) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    path = _cache_path(cache_dir, target)
    data = [
        {
            "date": m.date.isoformat(),
            "subject": m.subject,
            "from_addr": m.from_addr,
            "to_addrs": m.to_addrs,
            "direction": m.direction,
            "body": m.body,
            "html": m.html,
            "attachments": m.attachments,
            "message_id": m.message_id,
            "in_reply_to": m.in_reply_to,
            "references": m.references,
        }
        for m in emails
    ]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def delete_cache(cache_dir: str, target: str) -> bool:
    """删除某目标的本地邮件缓存。返回是否有文件被删。"""
    path = _cache_path(cache_dir, target)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def merge_emails(cached: list, fresh: list) -> list:
    """按 (时间, 主题, 发件人) 去重合并，按时间升序。"""
    seen = {(m.date, m.subject, m.from_addr) for m in cached}
    merged = list(cached)
    for m in fresh:
        key = (m.date, m.subject, m.from_addr)
        if key not in seen:
            seen.add(key)
            merged.append(m)
    merged.sort(key=lambda m: m.date)
    return merged
