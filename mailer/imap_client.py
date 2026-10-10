"""通过 IMAP 抓取并整理与指定邮箱地址的往来邮件。"""
import base64
import datetime as dt
import imaplib
import re
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

_FOLDER_KEYWORDS = ("inbox", "sent", "已发送", "已發送", "发件", "寄件", "收件")

# IMAP SINCE 日期格式（服务器端过滤），手动拼英文月份避免本地化输出
_IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _imap_date(d: dt.datetime) -> str:
    return f"{d.day:02d}-{_IMAP_MONTHS[d.month - 1]}-{d.year}"

# LIST 响应格式：(\HasNoChildren) "/" "Sent Messages"
_LIST_RE = re.compile(r'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.+)$')


def parse_list_line(line: str) -> tuple:
    """解析 IMAP LIST 响应行，返回 (flags, 原始文件夹名)。"""
    m = _LIST_RE.match(line)
    if not m:
        return "", ""
    name = m.group("name").strip()
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    return m.group("flags"), name


# ---------- IMAP modified UTF-7（文件夹名编码，如「已发送」） ----------

def decode_modified_utf7(s: str) -> str:
    def repl(m):
        token = m.group(1)
        if token == "":
            return "&"
        b64 = token.replace(",", "/") + "=" * (-len(token) % 4)
        return base64.b64decode(b64).decode("utf-16-be")
    return re.sub(r"&([^-]*)-", repl, s)


def encode_modified_utf7(s: str) -> str:
    out, buf = [], []

    def flush():
        if buf:
            b64 = base64.b64encode("".join(buf).encode("utf-16-be")).decode()
            out.append("&" + b64.rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in s:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


# ---------- 邮件解析 ----------

class _HTMLStripper(HTMLParser):
    _SKIP_TAGS = {"script", "style", "head"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def get_text(self) -> str:
        return " ".join("".join(self.parts).split())


def strip_html(html: str) -> str:
    stripper = _HTMLStripper()
    try:
        stripper.feed(html)
        return stripper.get_text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)


def _normalize(text: str) -> str:
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def extract_text(msg) -> str:
    """优先取 text/plain，没有则退化为剥离标签的 text/html。"""
    plains, htmls = [], []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        ctype = part.get_content_type()
        disp = str(part.get("Content-Disposition") or "").lower()
        if "attachment" in disp:
            continue
        try:
            content = part.get_content()
            if not isinstance(content, str):
                content = str(content)
        except Exception:
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            content = payload.decode(charset, errors="replace")
        if ctype == "text/plain":
            plains.append(content)
        elif ctype == "text/html":
            htmls.append(content)
    if plains:
        return _normalize("\n".join(plains))
    if htmls:
        return _normalize(strip_html("\n".join(htmls)))
    return ""


def extract_html(msg, html_chars: int = 200_000) -> str:
    """提取邮件的 text/html 原文用于页面渲染；纯文本邮件返回空串。"""
    htmls = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_type() != "text/html":
            continue
        disp = str(part.get("Content-Disposition") or "").lower()
        if "attachment" in disp:
            continue
        try:
            content = part.get_content()
            if not isinstance(content, str):
                content = str(content)
        except Exception:
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            content = payload.decode(charset, errors="replace")
        htmls.append(content)
    html = "\n".join(htmls).strip()
    if len(html) > html_chars:
        html = html[:html_chars]
    return html


def extract_attachment_names(msg) -> list:
    """收集附件文件名（只记录名字，不下载内容）。"""
    names = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disp = str(part.get("Content-Disposition") or "").lower()
        if "attachment" not in disp:
            continue
        name = part.get_filename()
        if name:
            names.append(str(name))
    return names


@dataclass
class EmailMessage:
    date: dt.datetime
    subject: str
    from_addr: str
    to_addrs: str
    direction: str  # "发出" / "收到"
    body: str
    html: str = ""  # text/html 原文，供前端渲染；纯文本邮件为空
    attachments: list = field(default_factory=list)

    def digest(self, index: int) -> str:
        text = (
            f"[{index}] {self.date:%Y-%m-%d %H:%M} | {self.direction} | 主题: {self.subject}\n"
            f"发件人: {self.from_addr}\n"
            f"收件人: {self.to_addrs}\n"
            f"正文:\n{self.body}"
        )
        if self.attachments:
            text += f"\n附件: {', '.join(self.attachments)}（未读取内容）"
        return text


def parse_raw_email(raw: bytes, own_addr: str, target: str, body_chars: int = 2000):
    """解析原始邮件字节；与 target 无关时返回 None。"""
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    from_addr = str(msg.get("From", ""))
    to_addrs = ", ".join(str(msg.get(h, "")) for h in ("To", "Cc") if msg.get(h))
    addr_pool = {a.lower() for _, a in getaddresses([from_addr, to_addrs])}
    if target.lower() not in addr_pool:
        return None
    try:
        date = parsedate_to_datetime(str(msg.get("Date", "")))
        if date is None:
            raise ValueError
        if date.tzinfo is not None:
            date = date.astimezone().replace(tzinfo=None)
    except Exception:
        date = dt.datetime.min
    direction = "发出" if own_addr and own_addr.lower() in from_addr.lower() else "收到"
    body = extract_text(msg)
    if len(body) > body_chars:
        body = body[:body_chars] + " …(正文过长已截断)"
    return EmailMessage(
        date=date,
        subject=str(msg.get("Subject", "(无主题)")),
        from_addr=from_addr,
        to_addrs=to_addrs,
        direction=direction,
        body=body,
        html=extract_html(msg),
        attachments=extract_attachment_names(msg),
    )


# ---------- IMAP 抓取 ----------

class IMAPFetcher:
    def __init__(self, host: str, port: int, user: str, password: str,
                 folders: str = "", lookback_days: int = 365,
                 max_results: int = 100, body_chars: int = 2000,
                 cache_dir: str = ""):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.folders = [f.strip() for f in folders.split(",") if f.strip()]
        self.lookback_days = lookback_days
        self.max_results = max_results
        self.body_chars = body_chars
        self.cache_dir = cache_dir

    def _resolve_folders(self, conn) -> list:
        if self.folders:
            return self.folders
        found = []
        try:
            typ, data = conn.list()
            if typ == "OK":
                for item in data:
                    if not item:
                        continue
                    line = item.decode("utf-8", errors="replace") if isinstance(item, bytes) else item
                    flags, raw_name = parse_list_line(line)
                    if not raw_name or "\\noselect" in flags.lower():
                        continue
                    name = decode_modified_utf7(raw_name)
                    if any(kw.lower() in name.lower() for kw in _FOLDER_KEYWORDS):
                        found.append(name)
        except Exception:
            pass
        seen, result = set(), []
        for name in found or ["INBOX"]:
            if name not in seen:
                seen.add(name)
                result.append(name)
        return result

    @staticmethod
    def _select(conn, folder: str) -> bool:
        name = encode_modified_utf7(folder) if any(ord(c) > 127 for c in folder) else folder
        try:
            typ, _ = conn.select(f'"{name}"', readonly=True)
            return typ == "OK"
        except Exception:
            return False

    @staticmethod
    def _search(conn, addr: str, since: dt.datetime = None) -> list:
        date_crit = f'SINCE "{_imap_date(since)}"' if since else "ALL"
        try:
            typ, data = conn.search(None, "OR", f'(FROM "{addr}")', f'(TO "{addr}")', date_crit)
            if typ == "OK":
                return data[0].split()
        except Exception:
            pass
        ids = set()
        for crit in ("FROM", "TO"):
            try:
                typ, data = conn.search(None, crit, f'"{addr}"', date_crit)
                if typ == "OK":
                    ids.update(data[0].split())
            except Exception:
                continue
        return sorted(ids, key=lambda x: int(x))

    def fetch_correspondence(self, target: str) -> list:
        """返回与 target 的往来邮件列表，按时间升序。

        配置了 cache_dir 时，结果缓存在本地：再次分析同一地址时，IMAP 搜索
        会带 SINCE 日期条件（最近一封缓存邮件往前回 1 天），只有新邮件才
        下载正文，重叠部分靠去重合并。
        """
        target = target.strip().lower()
        if not self.cache_dir:
            since = self._full_since()
            return self._fetch(target, since)
        from .cache import load_emails, merge_emails, save_emails
        cached = load_emails(self.cache_dir, target)
        if cached and cached[-1].date > dt.datetime.min:
            # 增量抓取：从最近一封缓存邮件往前回看 1 天，重叠部分靠去重合并
            since = cached[-1].date - dt.timedelta(days=1)
        else:
            since = self._full_since()
        fresh = self._fetch(target, since)
        merged = merge_emails(cached, fresh)
        save_emails(self.cache_dir, target, merged)
        return merged

    def _full_since(self):
        """无缓存时的起始时间；lookback_days <= 0 表示不限时间。"""
        if self.lookback_days and self.lookback_days > 0:
            return dt.datetime.now() - dt.timedelta(days=self.lookback_days)
        return None

    def _fetch(self, target: str, since) -> list:
        """从 IMAP 抓取 since 之后（None 表示不限）与 target 的往来邮件，按时间升序。"""
        messages, seen = [], set()
        conn = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            conn.login(self.user, self.password)
            for folder in self._resolve_folders(conn):
                if not self._select(conn, folder):
                    continue
                ids = self._search(conn, target, since)
                # max_results <= 0 表示不限数量（ids[-0:] 即全部）
                selected = ids[-self.max_results:] if self.max_results > 0 else ids
                for num in selected:
                    try:
                        typ, data = conn.fetch(num, "(BODY.PEEK[])")
                        if typ != "OK" or not data or not isinstance(data[0], tuple):
                            continue
                        parsed = parse_raw_email(data[0][1], self.user, target, self.body_chars)
                    except Exception:
                        continue
                    if parsed is None or (since and parsed.date < since):
                        continue
                    key = (parsed.date, parsed.subject, parsed.from_addr)
                    if key in seen:
                        continue
                    seen.add(key)
                    messages.append(parsed)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        messages.sort(key=lambda m: m.date)
        return messages
