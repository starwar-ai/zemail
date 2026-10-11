"""深度调研 Agent：模型在 harness 循环中调用 grep/检索/读信/线程工具，
对邮件做多轮调查后输出有证据支撑的分析结论。

工具调用走 OpenAI 兼容的 function calling（DeepSeek 等支持）；模型接口不支持
tools 时首轮调用即抛错，由调用方（Analyzer.investigate_stream）回退到普通追问。
"""
import json
import re

from retrieval import EmailIndex, catalog_line
from threads import normalize_subject

INVESTIGATE_SYSTEM = """你是一名邮件调查分析员。你可以调用工具在用户的往来邮件中反复查找证据，再给出结论。

工作要求：
1. 先概览再深入：用 list_threads / search_emails / grep_emails 定位相关邮件与话题主线，再用 read_emails / read_thread 细读原文；
2. 多方印证：重要结论尽量有至少两封邮件或原文细节支撑；每条事实或结论后用 [编号] 标注证据来源邮件，关键内容（金额、日期、承诺、单号等）尽量用引号摘录原文片段佐证（每段不超过 80 字）；
3. 工具结果不足时换关键词、换角度再查，不要臆测；确实查不到就明确说明；
4. 工具调用应围绕问题逐步深入，通常 2~6 轮即可，不要无目的重复调用；
5. 最终答案用中文，条理清晰、分点陈述。"""

_WRAP_UP = "调查轮次已用完。请基于已收集的材料直接给出最终结论，不要再请求工具。"

_MAX_TOOL_CALLS_PER_ROUND = 4  # 单轮最多执行的工具调用数，防止失控

TOOLS = [
    {"type": "function", "function": {
        "name": "grep_emails",
        "description": "在全部往来邮件的主题、正文、信息卡（摘要/实体/待办）中做正则搜索（大小写不敏感），"
                       "返回命中邮件的编号、目录行与命中片段。适合按关键词、人名、金额、单号、日期等精确查找。",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "正则表达式，如「报价|单价|¥[0-9]+」"}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "search_emails",
        "description": "按语义相关度检索邮件，返回最相关邮件的编号与目录行。适合没有明确关键词的话题查找。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "检索问题或描述"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "read_emails",
        "description": "读取指定编号邮件的完整正文，一次最多 8 封。编号来自目录、grep_emails 或 search_emails 的结果。",
        "parameters": {"type": "object", "properties": {
            "indices": {"type": "array", "items": {"type": "integer"},
                        "description": "邮件编号（目录中方括号里的数字，1 起始）"}},
            "required": ["indices"]}}},
    {"type": "function", "function": {
        "name": "list_threads",
        "description": "列出全部主题线程：编号、主题、邮件数、起止日期。用于了解往来中有哪些话题主线。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "read_thread",
        "description": "读取某个主题线程的全部邮件全文（按时间升序），用于梳理一个话题的来龙去脉。线程编号来自 list_threads。",
        "parameters": {"type": "object", "properties": {
            "tid": {"type": "integer", "description": "线程编号"}},
            "required": ["tid"]}}},
]


def _brief_args(args: dict) -> str:
    parts = []
    for k, v in args.items():
        s = json.dumps(v, ensure_ascii=False)
        parts.append(f"{k}={s[:40]}")
    return ", ".join(parts)[:120]


class EmailToolbox:
    """绑定一份会话邮件的工具执行器；所有结果都做了长度预算控制。"""

    GREP_MAX_HITS = 12
    GREP_BUDGET = 6000
    READ_BUDGET = 12000
    LIST_MAX = 60
    SEARCH_MAX = 10

    def __init__(self, emails: list, cards: list = None, threads: list = None,
                 index: EmailIndex = None):
        self.emails = list(emails)
        self.cards = cards or []
        self.threads = threads or []
        self.index = index or EmailIndex(self.emails, cards=self.cards,
                                         threads=self.threads)
        self._by_thread = {}
        for i, tid in enumerate(self.threads):
            self._by_thread.setdefault(tid, []).append(i)

    def _card(self, i: int):
        return self.cards[i] if i < len(self.cards) else None

    def run(self, name: str, args: dict) -> str:
        fn = getattr(self, "tool_" + name, None)
        if fn is None:
            return f"未知工具：{name}"
        try:
            return fn(**args)
        except TypeError:
            return f"工具参数有误：{name}({json.dumps(args, ensure_ascii=False)[:100]})"
        except Exception as exc:
            return f"工具执行失败：{exc}"

    def tool_grep_emails(self, pattern: str = "") -> str:
        try:
            rx = re.compile(pattern or "", re.I)
        except re.error as exc:
            return f"正则表达式无效：{exc}"
        hits = []
        capped = False
        for i, e in enumerate(self.emails):
            haystacks = [("主题", e.subject or ""), ("正文", e.body or "")]
            card = self._card(i)
            if card:
                haystacks.append(("信息卡", " ".join([
                    str(card.get("summary") or ""),
                    " ".join(str(x) for x in card.get("entities") or []),
                    "；".join(str(x) for x in card.get("todos") or []),
                ])))
            matched = []
            for label, text in haystacks:
                m = rx.search(text)
                if m:
                    start = max(0, m.start() - 40)
                    end = min(len(text), m.end() + 40)
                    snippet = re.sub(r"\s+", " ", text[start:end]).strip()
                    matched.append(f"{label}: …{snippet}…")
            if matched:
                hits.append((i, matched[:2]))
                if len(hits) >= self.GREP_MAX_HITS:
                    capped = True
                    break
        if not hits:
            return "没有命中的邮件。"
        lines = [f"命中 {len(hits)}{'+' if capped else ''} 封邮件"
                 "（编号可用于 read_emails 调阅全文）："]
        for i, matched in hits:
            lines.append(catalog_line(self.emails[i], i + 1, card=self._card(i)))
            lines.extend("    " + m for m in matched)
        return "\n".join(lines)[:self.GREP_BUDGET]

    def tool_search_emails(self, query: str = "") -> str:
        if not query.strip():
            return "请提供检索问题（query）。"
        hits = self.index.search(query, max_chars=100000, max_results=self.SEARCH_MAX)
        if not hits:
            return "没有相关邮件命中。"
        lines = ["按相关度排序的命中邮件（编号可用于 read_emails 调阅全文）："]
        for i in hits:
            lines.append(catalog_line(self.emails[i], i + 1, card=self._card(i)))
        return "\n".join(lines)

    def tool_read_emails(self, indices=None) -> str:
        nums = [int(n) for n in (indices or []) if isinstance(n, (int, float))][:8]
        if not nums:
            return "请提供要读取的邮件编号（indices 数组，1 起始）。"
        blocks, notes, used = [], [], 0
        for n in dict.fromkeys(nums):
            i = n - 1
            if not 0 <= i < len(self.emails):
                notes.append(f"[{n}] 编号无效")
                continue
            b = self.emails[i].digest(n)
            if blocks and used + len(b) > self.READ_BUDGET:
                notes.append(f"[{n}] 超出本次预算，未附上")
                continue
            blocks.append(b)
            used += len(b)
        out = []
        if blocks:
            out.append("\n\n".join(blocks))
        if notes:
            out.append("说明：" + "；".join(notes))
        return "\n\n".join(out) or "没有可读取的邮件。"

    def tool_list_threads(self) -> str:
        if not self._by_thread:
            return "无线程信息。"
        ordered = sorted(self._by_thread.items(),
                         key=lambda kv: self.emails[kv[1][-1]].date, reverse=True)
        lines = []
        for tid, indices in ordered[:self.LIST_MAX]:
            first, last = self.emails[indices[0]], self.emails[indices[-1]]
            lines.append(f"[{tid}] {normalize_subject(first.subject)} | "
                         f"{len(indices)} 封 | {first.date:%Y-%m-%d} ~ {last.date:%Y-%m-%d}")
        return "\n".join(lines)

    def tool_read_thread(self, tid: int = 0) -> str:
        indices = self._by_thread.get(tid)
        if not indices:
            return f"线程 {tid} 不存在，可先用 list_threads 查看线程列表。"
        subject = normalize_subject(self.emails[indices[0]].subject)
        blocks, used = [], 0
        for i in indices:
            b = self.emails[i].digest(i + 1)  # 编号用全局目录编号，供 read_emails 引用
            if blocks and used + len(b) > self.READ_BUDGET:
                blocks.append(f"……（线程其余 {len(indices) - len(blocks)} 封超出预算，"
                              "可用 read_emails 单独调阅）")
                break
            blocks.append(b)
            used += len(b)
        return (f"线程 [{tid}]「{subject}」共 {len(indices)} 封：\n\n"
                + "\n\n".join(blocks))


def run_agent(llm, system: str, materials: str, question: str,
              toolbox: EmailToolbox, max_rounds: int = 8):
    """harness 循环：产出 ("progress", 文本) 事件，最后产出 ("final", 结论文本)。

    每轮调用模型：模型返回 tool_calls 则执行工具并把结果回填，直到模型直接
    给出文本结论；轮次用尽时去掉工具让模型基于已收集材料强制总结。
    """
    messages = [{"role": "system", "content": system}]
    if materials:
        messages.append({"role": "user", "content": materials})
    messages.append({"role": "user", "content": question})
    for _ in range(max_rounds):
        msg = llm.chat_tools(messages, TOOLS)
        calls = msg.get("tool_calls") or []
        if not calls:
            yield ("final", (msg.get("content") or "").strip())
            return
        messages.append(msg)
        for call in calls[:_MAX_TOOL_CALLS_PER_ROUND]:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {}
            except ValueError:
                args = {}
            yield ("progress", f"· {name}({_brief_args(args)})")
            result = toolbox.run(name, args)
            messages.append({"role": "tool",
                             "tool_call_id": call.get("id", ""),
                             "content": result})
    yield ("progress", "· 调查轮次已用完，正在基于已收集的材料总结")
    messages.append({"role": "user", "content": _WRAP_UP})
    yield ("final", llm.chat(messages).strip())
