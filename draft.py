"""AI 回复起草：基于与目标联系人的往来上下文，生成多个语气版本的回复草稿。

知情起草是与其他通用邮箱助手的差异点：草稿自动挂接该联系人的往来画像、
最近线程上下文与未闭环承诺。语气模仿：从我方历史发出邮件提炼写作风格
画像（按内容哈希缓存），并取近期发出邮件作示例，产出「我的语气」版本。
只做起草，不接管发信。
"""
import hashlib
import json
import re

from threads import group_threads, normalize_subject

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.S)

_DRAFT_INSTRUCTION = """你是邮件起草助手。请基于以下材料，替我方起草一封给对方最新来信的回复邮件。

要求：
- 回复必须"知情"：回应对方最新来信中的问题与诉求，必要时引用之前的承诺、报价、约定（以材料中的事实为准，不得编造数字与日期）
- 输出严格的 JSON：{"drafts": [{"tone": "简洁", "body": "..."}, {"tone": "正式", "body": "..."}, {"tone": "商务推进", "body": "..."}, {"tone": "我的语气", "body": "..."}]}
- 前三个版本语气区分明显：简洁版 100 字内直给；正式版规范商务书信；商务推进版在回复之余主动推进下一步（约时间、催决策、提方案）
- 第四个版本「我的语气」：严格模仿【我的写作风格】与【我的邮件示例】——沿用我惯用的称呼、开头结尾、句长、正式度与签名（材料中没有签名则用"我"落款，不得编造姓名与联系方式）；若材料中无风格信息，该版本与简洁版相同即可
- 只输出 JSON 本身，不要任何解释或 markdown 代码围栏

{extra}"""

_STYLE_INSTRUCTION = """以下是我最近发给 {target} 的 {count} 封邮件。请总结我在与该对象通信时的写作风格，供 AI 模仿，输出不超过 300 字的中文要点，覆盖：
1. 称呼与开头习惯（如"X总好"/"Hi X"/直呼其名）；
2. 结尾与签名习惯（如"祝好"+名字/公司落款，原文照录签名）；
3. 语言与正式度（中文/英文/混用，商务正式还是随和）；
4. 句式与篇幅（长句短句、是否爱用列表/编号、典型篇幅）；
5. 值得模仿的惯用表达（摘录 2-3 句原文短语）。
分点陈述，直接输出正文，不要标题或解释。"""

_STYLE_INPUT_CHARS = 8000  # 风格提炼的输入预算：近期发出邮件截断注入
_STYLE_MIN_MAILS = 3       # 发出邮件少于此数不提炼风格（样本不足）
_STYLE_EXAMPLES = 3        # 起草提示词中附带的我方邮件示例数


def _sent_indices(emails: list) -> list:
    return [i for i, e in enumerate(emails) if e.direction == "发出"]


def _sent_hash(emails: list) -> str:
    """发出邮件集合的内容哈希：风格画像的缓存键，有新发出邮件即失效。"""
    from preprocess import email_hash
    h = hashlib.md5(b"style-v1")
    for i in _sent_indices(emails):
        h.update(email_hash(emails[i]).encode("ascii"))
    return h.hexdigest()


def ensure_style(llm, store, target: str, emails: list) -> str:
    """写作风格画像：基于我发给 target 的邮件提炼，按内容哈希缓存。

    发出邮件少于 _STYLE_MIN_MAILS 封时不调用模型，返回空串。
    """
    sent = _sent_indices(emails)
    if len(sent) < _STYLE_MIN_MAILS:
        return ""
    key = _sent_hash(emails)
    cached = store.data.get("style") or {}
    if cached.get("hash") == key and cached.get("text"):
        return cached["text"]
    lines = []
    used = 0
    for i in reversed(sent):
        e = emails[i]
        piece = (f"{e.date:%Y-%m-%d} | {e.subject}\n"
                 + re.sub(r"\s+", " ", e.body.strip())[:800])
        if used + len(piece) > _STYLE_INPUT_CHARS:
            break
        lines.append(piece)
        used += len(piece)
    lines.reverse()
    prompt = _STYLE_INSTRUCTION.format(target=target, count=len(lines)) \
        + "\n\n" + "\n\n".join(lines)
    try:
        text = llm.chat([{"role": "user", "content": prompt}]).strip()
    except Exception:
        return ""
    if text:
        store.data["style"] = {"hash": key, "text": text}
        store.save()
    return text


def style_examples(emails: list, cap: int = _STYLE_EXAMPLES) -> str:
    """近期发出邮件的截断摘录，作为起草时的语气示例（few-shot）。"""
    sent = _sent_indices(emails)
    pieces = []
    for i in sent[-cap:]:
        e = emails[i]
        pieces.append(f"{e.date:%Y-%m-%d} | {e.subject}\n"
                      + re.sub(r"\s+", " ", e.body.strip())[:500])
    return "\n\n".join(pieces)


def _thread_context(emails: list, cap: int = 6) -> list:
    """对方最新来信所在线程的邮件（按时间升序，最多 cap 封）；无线程信息取最近几封。"""
    in_indices = [i for i, e in enumerate(emails) if e.direction == "收到"]
    if not in_indices:
        return []
    last_in = in_indices[-1]
    try:
        threads = group_threads(emails)
        for indices in threads:
            if last_in in indices:
                return list(indices)[-cap:]
    except Exception:
        pass
    return list(range(max(0, len(emails) - cap), len(emails)))


def _material_lines(emails: list, indices: list, cards: list) -> list:
    lines = []
    for i in indices:
        e = emails[i]
        lines.append(f"[{i + 1}] {e.date:%Y-%m-%d %H:%M} | {e.direction} | 主题: {e.subject}")
        body = re.sub(r"\s+", " ", e.body.strip())[:600]
        card = cards[i] if cards and i < len(cards) else None
        if card and card.get("summary"):
            lines.append(f"摘要: {card['summary']}")
        lines.append(f"正文: {body}\n")
    return lines


def parse_drafts(text: str) -> list:
    """从模型回复解析草稿列表；失败返回 None。"""
    m = _JSON_BLOCK_RE.search(text or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    drafts = data.get("drafts") if isinstance(data, dict) else None
    if not isinstance(drafts, list):
        return None
    out = []
    for d in drafts:
        if not isinstance(d, dict):
            continue
        body = str(d.get("body") or "").strip()
        tone = str(d.get("tone") or "").strip()[:20] or "草稿"
        if body:
            out.append({"tone": tone, "body": body[:4000]})
    return out or None


def generate_drafts(llm, target: str, emails: list, cards: list = None,
                    profile: str = "", commitments: list = None,
                    style: str = "", style_note: str = "",
                    instruction: str = "") -> dict:
    """生成四个语气版本（含「我的语气」）的回复草稿；无对方来信时返回 None。"""
    indices = _thread_context(emails)
    if not indices:
        return None
    last_in = emails[indices[-1]] if emails[indices[-1]].direction == "收到" else None
    if last_in is None:
        return None

    parts = []
    if style:
        parts.append(f"【我的写作风格】\n{style}")
    examples = style_examples(emails) if style else ""
    if examples:
        parts.append(f"【我的邮件示例】\n{examples}")
    if style_note:
        parts.append(f"【风格补充说明（用户手动指定，「我的语气」版本须优先遵循）】\n"
                     f"{style_note}")
    if profile:
        parts.append(f"【往来画像】\n{profile}")
    open_items = [c for c in (commitments or []) if c.get("status") == "open"]
    if open_items:
        lines = []
        for c in open_items[:10]:
            owner = {"me": "我方", "them": "对方"}.get(c.get("owner"), "双方")
            due = f"（截止 {c['due']}）" if c.get("due") else ""
            lines.append(f"- {owner}：{c['text']}{due}")
        parts.append("【未闭环承诺/待办】\n" + "\n".join(lines))
    parts.append("【最近往来邮件】\n" + "\n".join(
        _material_lines(emails, indices, cards)))
    material = "\n\n".join(parts)

    extra = f"用户的补充要求：{instruction}" if instruction else ""
    prompt = _DRAFT_INSTRUCTION.replace("{extra}", extra) + "\n\n" + material
    drafts = parse_drafts(llm.chat([{"role": "user", "content": prompt}]))
    if not drafts:
        return None
    subject = normalize_subject(last_in.subject)
    return {
        "subject": f"Re: {subject}" if subject else "Re: （无主题）",
        "in_reply_to": {
            "date": last_in.date.isoformat(timespec="seconds"),
            "subject": last_in.subject,
        },
        "drafts": drafts,
    }
