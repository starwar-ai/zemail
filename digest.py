"""周期性情报摘要：把一段时间窗内各监控客户的往来动态压缩成一份中文简报。

按客户聚合：每个在时间窗内有新邮件的客户单独一节，由 LLM 基于信息卡与
正文摘录生成中文要点（外文邮件一并翻译为中文，覆盖外贸多语言场景）；
末尾附规则式的逾期承诺与未回复清单。LLM 不可用时由调用方回退到规则式
汇总。全部输入取自本地缓存与派生数据，不重新抓取。
"""
import datetime as dt
import re

from preprocess import todo_text

_SECTION_INSTRUCTION = """以下是我与 {target} 最近{days}天的往来邮件（按时间升序，含信息卡摘要）。
请用中文输出该客户的「期间动态摘要」（无论邮件原文是中文、英文还是其他语言，一律用中文），3-5 条要点：
- 关键进展与结论（保留金额、日期等关键数字）
- 新增的承诺/待办（标注责任方与期限）
- 语气或关系的变化（如有）
直接输出要点列表，每行以「·」开头，不要标题与解释，总共不超过 200 字。"""

_SECTION_INPUT_CHARS = 4000   # 单个客户的输入预算（信息卡行超限时掐头留尾）
MAX_SECTIONS = 8              # 一份简报最多聚合的客户数（超出按新邮件数取前 N 个）


def _card_line(email, card) -> str:
    line = f"{email.date:%Y-%m-%d %H:%M} | {email.direction} | {email.subject}"
    if card:
        if card.get("summary"):
            line += f" | {card['summary']}"
        todos = [t for t in (todo_text(x) for x in card.get("todos") or []) if t]
        if todos:
            line += " | 待办: " + "；".join(todos)
        if card.get("sentiment") and card["sentiment"] != "中性":
            line += f" | 语气: {card['sentiment']}"
    else:
        line += " | " + re.sub(r"\s+", " ", email.body.strip())[:80]
    return line


def _fit(lines: list, budget: int) -> list:
    """行总长度超预算时掐头留尾（优先保留近期动态）。"""
    if sum(len(l) for l in lines) <= budget:
        return lines
    head = lines[:2]
    used = sum(len(l) for l in head)
    tail = []
    for line in reversed(lines[2:]):
        if used + len(line) > budget:
            break
        tail.append(line)
        used += len(line)
    tail.reverse()
    omitted = len(lines) - len(head) - len(tail)
    return head + ([f"……（中间省略 {omitted} 封）……"] if omitted else []) + tail


def customer_section(llm, target: str, emails: list, cards: list,
                     since: dt.datetime, name: str = "") -> dict:
    """生成单个客户的期间摘要小节；时间窗内无往来或生成失败返回 None。"""
    recent = [(i, e) for i, e in enumerate(emails) if e.date >= since]
    if not recent:
        return None
    lines = _fit([_card_line(e, cards[i] if cards and i < len(cards) else None)
                  for i, e in recent], _SECTION_INPUT_CHARS)
    days = max(1, (dt.datetime.now() - since).days + 1)
    prompt = _SECTION_INSTRUCTION.format(target=target, days=days) \
        + "\n\n" + "\n".join(lines)
    text = llm.chat([{"role": "user", "content": prompt}]).strip()
    if not text:
        return None
    return {
        "target": target,
        "name": name,
        "new_count": len(recent),
        "summary": text[:800],
    }


def build_report(sections: list, overdue: list, unreplied: list,
                 title: str) -> str:
    """拼装完整简报：客户小节 + 逾期承诺 + 未回复清单。无内容返回空串。"""
    if not sections and not overdue and not unreplied:
        return ""
    today = dt.datetime.now().date().isoformat()
    lines = [title]
    for s in sections:
        head = f"【{s['name']} <{s['target']}>】" if s.get("name") else f"【{s['target']}】"
        lines.append(f"\n{head}（新邮件 {s['new_count']} 封）")
        lines.append(s["summary"])
    if overdue:
        lines.append("\n———")
        lines.append(f"待闭环承诺（{len(overdue)} 条已到期/逾期）：")
        for c in overdue[:8]:
            owner = {"me": "我", "them": "对方"}.get(c.get("owner"), "")
            tag = f"已逾期 {_overdue_days(c['due'], today)}" \
                if c["due"] < today else "今日到期"
            lines.append(f"▢ {c['target']}｜{owner}：{c['text']}｜{tag}")
    if unreplied:
        lines.append(f"\n等待回复（{len(unreplied)} 位客户）：")
        for u in unreplied[:8]:
            lines.append(f"· {u['target']}（对方来信已 {u['days']} 天）")
    return "\n".join(lines)


def _overdue_days(due: str, today: str) -> str:
    try:
        n = (dt.date.fromisoformat(today) - dt.date.fromisoformat(due)).days
        return f"{n} 天"
    except ValueError:
        return "数天"


def generate_report(llm, customers: list, since: dt.datetime, title: str,
                    overdue: list = None, unreplied: list = None) -> str:
    """生成整份简报；customers：[{target, name, emails, cards}]。无内容返回空串。"""
    sections = []
    for c in customers:
        try:
            section = customer_section(llm, c["target"], c["emails"],
                                       c.get("cards"), since,
                                       name=c.get("name") or "")
        except Exception:
            section = None
        if section:
            sections.append(section)
    sections.sort(key=lambda s: -s["new_count"])
    return build_report(sections[:MAX_SECTIONS], overdue or [], unreplied or [],
                        title)
