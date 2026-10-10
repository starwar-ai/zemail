"""早期邮件的分层摘要：按月分块，由 LLM 压缩为要点并持久化缓存。

邮件量超过上下文预算、目录也放不下全部邮件时，早期邮件不再只折叠成一行
说明，而是按月生成摘要注入上下文。摘要按 (目标, 月份, 内容哈希) 缓存在
`<邮件缓存目录>/<地址>-<哈希>.summaries.json`，邮件内容不变就不重复调用模型。
"""
import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

_INSTRUCTION = (
    "以下是 {month} 我与 {target} 的往来邮件（共 {count} 封，按时间升序）。"
    "请压缩为中文要点摘要，不超过 {cap} 字：按时间梳理关键事件、承诺、"
    "金额/日期等关键数字与待办，丢弃寒暄与重复内容。直接输出摘要正文，不要标题或解释。"
)


def _safe_path(dir_path: str, target: str) -> str:
    safe = re.sub(r"[^\w.@-]", "_", target)
    digest = hashlib.md5(target.encode("utf-8")).hexdigest()[:8]
    return os.path.join(dir_path, f"{safe}-{digest}.summaries.json")


def _content_hash(emails: list) -> str:
    h = hashlib.md5()
    for e in emails:
        h.update(f"{e.date.isoformat()}|{e.subject}|{e.from_addr}|{len(e.body)}\n"
                 .encode("utf-8"))
    return h.hexdigest()


def group_by_month(emails: list) -> list:
    """按月份分组，返回 [(month, [emails])]，保持邮件原有顺序。无有效日期的归入「未知日期」。"""
    groups = []
    for e in emails:
        key = f"{e.date:%Y-%m}" if e.date.year > 1900 else "未知日期"
        if groups and groups[-1][0] == key:
            groups[-1][1].append(e)
        else:
            groups.append((key, [e]))
    return groups


class SummaryStore:
    """按目标地址持久化月份摘要：{month: {hash, count, summary}}。"""

    def __init__(self, dir_path: str):
        self.dir = dir_path

    def _load(self, target: str) -> dict:
        try:
            with open(_safe_path(self.dir, target), encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def get(self, target: str, month: str, content_hash: str):
        """摘要存在且内容哈希未变时返回摘要文本，否则返回 None。"""
        entry = self._load(target).get(month)
        if entry and entry.get("hash") == content_hash:
            return entry.get("summary") or None
        return None

    def set(self, target: str, month: str, content_hash: str,
            summary: str, count: int) -> None:
        os.makedirs(self.dir, exist_ok=True)
        path = _safe_path(self.dir, target)
        data = self._load(target)
        data[month] = {"hash": content_hash, "count": count, "summary": summary}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)


def delete_summaries(dir_path: str, target: str) -> bool:
    """删除某目标的摘要缓存。返回是否有文件被删。"""
    path = _safe_path(dir_path, target)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def ensure_month_summaries(llm, store, target: str, groups: list,
                           per_cap: int, workers: int = 4) -> dict:
    """返回 {month: summary}。命中缓存的直接复用，缺失的并发生成；
    生成失败的月份不在结果中（调用方自行折叠处理）。"""
    result, todo = {}, []
    for month, mails in groups:
        ch = _content_hash(mails)
        cached = store.get(target, month, ch) if store else None
        if cached:
            result[month] = cached
        else:
            todo.append((month, mails, ch))

    def gen(item):
        month, mails, ch = item
        digest = "\n\n".join(e.digest(i) for i, e in enumerate(mails, 1))
        prompt = _INSTRUCTION.format(month=month, target=target,
                                     count=len(mails), cap=per_cap)
        try:
            return month, ch, len(mails), llm.chat(
                [{"role": "user", "content": prompt + "\n\n" + digest}]).strip()
        except Exception:
            return month, ch, len(mails), ""

    if todo:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for month, ch, count, summary in pool.map(gen, todo):
                if summary:
                    result[month] = summary
                    if store:
                        store.set(target, month, ch, summary, count)
    return result


def build_summary_block(llm, store, target: str, emails: list, budget: int) -> str:
    """把 emails（早期隐藏段，按时间升序）按月压缩为一个摘要注入块。

    每月篇幅按预算均分（120~400 字）；总预算放不下时优先保留最早的月份
    （关系起点对总体分析最重要），其余注明可从检索调阅。全部失败返回空串。
    """
    groups = group_by_month(emails)
    if not groups or budget < 300:
        return ""
    per_cap = max(120, min(400, budget // len(groups) - 40))
    summaries = ensure_month_summaries(llm, store, target, groups, per_cap)
    lines, used, covered = [], 0, 0
    for month, mails in groups:
        s = summaries.get(month)
        if not s:
            continue
        line = f"▪ {month}（{len(mails)} 封）：{s}"
        if lines and used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)
        covered += len(mails)
    if not lines:
        return ""
    rest = len(emails) - covered
    text = f"【早期邮件摘要】（共 {len(emails)} 封，按月压缩）\n" + "\n".join(lines)
    if rest > 0:
        text += f"\n（另有 {rest} 封未纳入摘要，可经检索调阅）"
    return text
