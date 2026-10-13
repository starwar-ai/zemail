"""从邮件客户端导出的 .eml/.zip 文件导入往来邮件到本地缓存。

上传文件先落盘到临时目录（暂存），预览分组与正式导入都从磁盘逐封读取，
内存占用与批次大小无关，因此不限制单次导入的文件数与总大小（仅受服务器
磁盘空间约束）。解析与 IMAP 抓取共用 parse_raw_email 管线（引用链剥离、
正文截断、时区归一化、方向判定保持一致），落库走 cache.merge_emails 去重
合并。附件只记录文件名，不导入内容（与邮件缓存格式一致）。
"""
import datetime as dt
import json
import os
import re
import shutil
import zipfile
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

from mailer.cache import load_emails, merge_emails, save_emails
from mailer.imap_client import parse_raw_email

_DATE_COMMENT_RE = re.compile(r"\([^)]*\)")


def _zip_display_name(info) -> str:
    """zip 成员文件名：无 UTF-8 标记时按 GBK 重解码（中文 Windows 压缩包）。

    不用 ZipFile(metadata_encoding=...)（Python 3.11+ 才有），兼容服务器 3.10。
    """
    name = info.filename
    if not (info.flag_bits & 0x800):
        try:
            name = name.encode("cp437").decode("gbk")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name


class ImportRejected(Exception):
    """导入请求被拒绝（暂存失效等），消息可直接返回前端。"""


class ImportCancelled(Exception):
    """导入任务被用户取消。"""


def unpack_staged(stage_dir: str) -> tuple:
    """把暂存目录里的上传文件展开为 .eml 条目，返回 (items, failures)。

    items: [(显示名, 文件路径)]。.zip 解包取其 .eml 成员（兼容中文 Windows
    压缩包的 GBK 文件名），所有条目平铺到 stage_dir/unpacked/ 下，本地文件
    名用序号防冲突；其余文件类型记入 failures。
    """
    unpacked = os.path.join(stage_dir, "unpacked")
    os.makedirs(unpacked, exist_ok=True)
    items, failures = [], []
    seq = 0

    for name in sorted(os.listdir(stage_dir)):
        path = os.path.join(stage_dir, name)
        if not os.path.isfile(path):
            continue
        lower = name.lower()
        if lower.endswith(".zip"):
            try:
                zf = zipfile.ZipFile(path)
            except zipfile.BadZipFile:
                failures.append({"name": name, "error": "zip 文件损坏或格式不支持"})
                continue
            with zf:
                for info in zf.infolist():
                    if info.is_dir() or not info.filename.lower().endswith(".eml"):
                        continue
                    seq += 1
                    dst = os.path.join(unpacked, f"{seq:05d}.eml")
                    with zf.open(info) as src, open(dst, "wb") as out:
                        shutil.copyfileobj(src, out)
                    display = os.path.basename(_zip_display_name(info)) or info.filename
                    items.append((display, dst))
        elif lower.endswith(".eml"):
            seq += 1
            dst = os.path.join(unpacked, f"{seq:05d}.eml")
            shutil.move(path, dst)
            items.append((name, dst))
        else:
            failures.append({"name": name, "error": "不支持的文件类型（仅支持 .eml / .zip）"})
    return items, failures


def save_meta(stage_dir: str, items: list, failures: list) -> None:
    """把解包结果写入 meta.json，供正式导入时复用（免二次解包）。"""
    meta = {"failures": failures,
            "items": [{"name": n, "file": os.path.relpath(p, stage_dir)}
                      for n, p in items]}
    with open(os.path.join(stage_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)


def load_meta(stage_dir: str) -> tuple:
    """读取 meta.json，返回 (items, failures)；暂存不存在或损坏时抛 ImportRejected。"""
    try:
        with open(os.path.join(stage_dir, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        items = [(str(it["name"]), os.path.join(stage_dir, str(it["file"])))
                 for it in meta["items"]]
        if not all(os.path.isfile(p) for _, p in items):
            raise ValueError("暂存文件缺失")
        failures = list(meta.get("failures") or [])
    except (OSError, ValueError, KeyError, TypeError):
        raise ImportRejected("导入会话已过期，请重新上传文件")
    return items, failures


def _header_date(msg) -> dt.datetime:
    """解析 Date 头为本地 naive 时间（剥掉旧式时区注释）；失败返回 None。"""
    try:
        date = parsedate_to_datetime(
            _DATE_COMMENT_RE.sub(" ", str(msg.get("Date", ""))).strip())
        if date is None:
            return None
        if date.tzinfo is not None:
            date = date.astimezone().replace(tzinfo=None)
        return date
    except Exception:
        return None


def group_items(items: list, own_addr: str) -> tuple:
    """按对方地址分组 .eml 条目，返回 (groups, failures)。

    items: [(显示名, 文件路径)]，逐封从磁盘读取，内存中只保留单封内容。
    方向判定：From 含本人地址 → 发出（对方为 To/Cc），否则 → 收到（对方为 From）。
    一封涉及多个对方的邮件会同时进入每个对方的分组（与 IMAP 按地址检索一致）。
    groups: {地址: {"items": [(显示名, 路径)], "subjects": [样例主题],
                    "first": datetime, "last": datetime}}
    """
    own = (own_addr or "").strip().lower()
    groups, failures = {}, []
    for name, path in items:
        try:
            with open(path, "rb") as f:
                msg = BytesParser(policy=policy.default).parsebytes(f.read())
        except Exception as exc:
            failures.append({"name": name, "error": f"解析失败：{exc}"})
            continue
        from_addrs = [a.lower() for _, a in getaddresses([str(msg.get("From", ""))]) if a]
        to_cc = ", ".join(str(msg.get(h, "")) for h in ("To", "Cc") if msg.get(h))
        to_addrs = [a.lower() for _, a in getaddresses([to_cc]) if a]
        if own and own in from_addrs:
            counterparts = [a for a in to_addrs if a != own]
        else:
            counterparts = [a for a in from_addrs if a != own]
        if not counterparts:
            failures.append({"name": name, "error": "无法识别对方地址（仅涉及本机地址）"})
            continue
        date = _header_date(msg)
        if date is None:
            failures.append({"name": name, "error": "无法解析日期"})
            continue
        subject = str(msg.get("Subject", "(无主题)"))
        for addr in dict.fromkeys(counterparts):
            g = groups.setdefault(addr, {"items": [], "subjects": [],
                                         "first": date, "last": date})
            g["items"].append((name, path))
            if len(g["subjects"]) < 5 and subject not in g["subjects"]:
                g["subjects"].append(subject)
            g["first"] = min(g["first"], date)
            g["last"] = max(g["last"], date)
    return groups, failures


def import_group(cache_dir: str, key: str, addresses: list, items: list,
                 own_addr: str, body_chars: int, progress=None,
                 cancelled=None) -> dict:
    """把一批 .eml 文件去重合并进目标地址的邮件缓存，返回统计与合并后完整列表。

    与目标地址无关、解析失败或日期缺失的邮件记入 failures 并跳过。
    progress(done, total) 每处理若干封回调一次；cancelled() 返回真时抛
    ImportCancelled（已完成的其他分组不受影响，本组不落盘）。
    """
    cached = load_emails(cache_dir, key)
    parsed, failures = [], []
    total = len(items)
    for i, (name, path) in enumerate(items, 1):
        if cancelled and cancelled():
            raise ImportCancelled
        try:
            with open(path, "rb") as f:
                msg = parse_raw_email(f.read(), own_addr, addresses, body_chars)
        except Exception as exc:
            failures.append({"name": name, "error": f"解析失败：{exc}"})
            continue
        if msg is None:
            failures.append({"name": name, "error": "邮件与目标地址无关"})
            continue
        if msg.date == dt.datetime.min:
            failures.append({"name": name, "error": "无法解析日期"})
            continue
        parsed.append(msg)
        if progress and (i % 5 == 0 or i == total):
            progress(i, total)
    merged = merge_emails(cached, parsed)
    if parsed:
        save_emails(cache_dir, key, merged)
    new = len(merged) - len(cached)
    return {"new": new, "duplicates": len(parsed) - new, "total": len(merged),
            "failures": failures, "emails": merged}
