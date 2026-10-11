"""深度调研的护栏层：引用校验、原文片段核对、覆盖率统计与审计日志。

调查类结论必须能回链到具体邮件。这里全部用确定性方法（不调用模型）：
从结论文本提取 [编号] 引用与引号摘录的原文片段，核对编号是否越界、
片段是否真的出现在某封邮件里，并统计本次调查对邮件库的覆盖情况；
每次调查追加一条 JSONL 审计日志，供合规审计与问题回溯。
"""
import datetime as dt
import json
import os
import re

# 结论中的邮件引用标记：[3]、[12] 等
_CITATION_RE = re.compile(r"\[(\d{1,4})\]")
# 引号摘录的原文片段（中文弯引号/直引号/角引号），6~80 字
_QUOTE_RE = re.compile(r"[“\"「]([^“”\"「」\n]{6,80})[”\"」]")


def extract_citations(text: str) -> list:
    """结论中引用的邮件编号（去重、保持出现顺序）。"""
    seen, out = set(), []
    for m in _CITATION_RE.finditer(text or ""):
        n = int(m.group(1))
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _norm(s: str) -> str:
    """忽略空白与大小写的归一化，用于片段包含匹配。"""
    return re.sub(r"\s+", "", s or "").lower()


def verify_quotes(text: str, emails: list) -> list:
    """提取结论中引号摘录的片段，在全部邮件（主题+正文+信息卡之外的原文）
    中核对出处。返回 [(片段, 命中邮件编号或 None)]，保持出现顺序、按片段去重。"""
    hay = [_norm(f"{e.subject}\n{e.body}") for e in emails]
    seen, out = set(), []
    for m in _QUOTE_RE.finditer(text or ""):
        frag = m.group(1).strip()
        nf = _norm(frag)
        if len(nf) < 6 or nf in seen:
            continue
        seen.add(nf)
        hit = None
        for i, h in enumerate(hay):
            if nf in h:
                hit = i + 1
                break
        out.append((frag, hit))
    return out


def guardrail_sections(text: str, emails: list) -> str:
    """生成护栏附加块（Markdown）：引用校验、原文核对、调查覆盖。

    无引用且无邮件时返回空串。无效引用与找不到出处的摘录显式标记，
    不删改模型原文，让读者（与审计方）能直接看到哪些结论未获证实。
    """
    citations = extract_citations(text)
    if not citations or not emails:
        return ""
    sections = []

    valid = [n for n in citations if 1 <= n <= len(emails)]
    invalid = [n for n in citations if not 1 <= n <= len(emails)]
    lines = ["**引用校验**（每条结论应能回链到具体邮件）"]
    for n in valid:
        e = emails[n - 1]
        lines.append(f"- [{n}] {e.date:%Y-%m-%d %H:%M} | {e.direction} | "
                     f"{e.from_addr} | {e.subject}")
    for n in invalid:
        lines.append(f"- [{n}] （无效引用：邮件编号不存在，共 {len(emails)} 封；"
                     "该处结论未获证实）")
    sections.append("\n".join(lines))

    quotes = verify_quotes(text, emails)
    if quotes:
        lines = ["**原文核对**（结论中摘录的原文片段是否真实存在）"]
        for frag, hit in quotes:
            if hit:
                lines.append(f"- “{frag}” — 已核实，见邮件 [{hit}]")
            else:
                lines.append(f"- “{frag}” — 未在任何邮件原文中找到，"
                             "可能是模型转述或臆造，请以原邮件为准")
        sections.append("\n".join(lines))

    cited = set(valid)
    lines = ["**调查覆盖**",
             f"- 本次结论引用 {len(cited)}/{len(emails)} 封邮件"]
    if cited:
        dates = [emails[n - 1].date for n in cited]
        lines.append(f"- 引用时间范围 {min(dates):%Y-%m-%d} ~ {max(dates):%Y-%m-%d}"
                     f"（邮件库全量 {emails[0].date:%Y-%m-%d} ~ {emails[-1].date:%Y-%m-%d}）")
        uncited_people = []
        for i, e in enumerate(emails):
            if i + 1 not in cited and e.from_addr and e.from_addr not in uncited_people:
                uncited_people.append(e.from_addr)
        if uncited_people:
            shown = "、".join(uncited_people[:5])
            more = f" 等 {len(uncited_people)} 人" if len(uncited_people) > 5 else ""
            lines.append(f"- 未被引用的发件人：{shown}{more}"
                         "（如问题涉及这些联系人，建议补充调查）")
    sections.append("\n".join(lines))

    return "---\n\n" + "\n\n".join(sections)


def audit(log_dir: str, record: dict) -> None:
    """追加一条 JSONL 审计日志（<log_dir>/audit.log）；失败静默，不阻断主流程。"""
    if not log_dir:
        return
    try:
        os.makedirs(log_dir, exist_ok=True)
        entry = {"time": dt.datetime.now().isoformat(timespec="seconds"), **record}
        with open(os.path.join(log_dir, "audit.log"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
