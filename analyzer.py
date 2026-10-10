"""基于 OpenAI 兼容接口的邮件往来会话式分析。"""
import json
import re
import threading
import time

import requests

import history
from retrieval import EmailIndex, catalog_line
from summaries import SummaryStore, build_summary_block

SYSTEM_PROMPT = """你是一名专业的邮件往来分析助手。用户会提供其邮箱与某个指定联系人之间的历史往来邮件。你的任务：
1. 梳理双方的关系背景、沟通主线与关键事件时间线；
2. 提炼重要信息：承诺与待办事项、价格/合同/日期等关键数字、尚未解决的问题；
3. 分析沟通语气与双方关系的变化趋势；
4. 回答用户围绕这些邮件提出的任何问题。

要求：使用中文回答，条理清晰、分点陈述；严格依据邮件内容，不要编造不存在的信息；信息不足时明确说明。"""

_INITIAL_INSTRUCTION = "请先输出一份总体分析报告，包含：关系概述、主要话题与时间线、关键承诺与待办、值得注意的风险或语气变化。之后我会继续追问细节。"

REPORT_INSTRUCTION = (
    "请基于以上邮件内容和我们到目前为止的对话，重新生成一份最新的总体分析报告"
    "（关系概述、主要话题与时间线、关键承诺与待办、值得注意的风险或语气变化），"
    "并融入对话中已经明确的结论与新发现。"
)


class LLMClient:
    """OpenAI 兼容的 chat/completions 客户端。"""

    def __init__(self, api_base: str, api_key: str, model: str, timeout: int = 90):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        # 不读取系统代理环境变量，避免本机代理软件未运行时请求被错误代理
        self._session = requests.Session()
        self._session.trust_env = False

    def chat(self, messages: list) -> str:
        resp = self._session.post(
            f"{self.api_base}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": messages, "temperature": 0.3},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"LLM 接口返回 {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()

    def chat_stream(self, messages: list):
        """流式调用（OpenAI SSE）：逐段产出增量文本。非 200 时抛异常。"""
        resp = self._session.post(
            f"{self.api_base}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": messages,
                  "temperature": 0.3, "stream": True},
            timeout=self.timeout, stream=True,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"LLM 接口返回 {resp.status_code}: {resp.text[:200]}")
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                delta = json.loads(payload)["choices"][0]["delta"].get("content")
            except (ValueError, KeyError, IndexError):
                continue
            if delta:
                yield delta


def build_context(emails: list, max_chars: int, hidden_note: str = None) -> tuple:
    """整理注入模型的邮件材料，返回 (文本, 附全文的邮件下标集合, 目录未覆盖的早期邮件数)。

    全部放得下时保持全文升序注入；放不下时改为「全量目录 + 最近若干封全文」：
    目录占约 40% 预算，剩余预算放最近邮件全文。目录排不下的早期邮件默认折叠为
    一行说明，提供 hidden_note（如按月摘要块）时替换该说明。未附全文的邮件不丢失
    ——追问时会按问题检索补充（见 Analyzer._augment），模型也可按 REQUEST_EMAIL
    协议主动调阅。
    """
    blocks = [e.digest(i) for i, e in enumerate(emails, 1)]
    if sum(len(b) for b in blocks) <= max_chars:
        return "\n\n".join(blocks), frozenset(range(len(emails))), 0
    catalog_budget = int(max_chars * 0.4)
    lines, used, hidden = [], 0, 0
    for i in range(len(emails) - 1, -1, -1):
        line = catalog_line(emails[i], i + 1)
        if used + len(line) > catalog_budget:
            hidden = i + 1
            break
        lines.append(line)
        used += len(line)
    lines.reverse()
    catalog = "【全部邮件目录（一行一封）】\n"
    if hidden and hidden_note:
        catalog += hidden_note + "\n"
    elif hidden:
        catalog += (f"（另有 {hidden} 封更早期邮件未列入目录："
                    f"{emails[0].date:%Y-%m-%d} ~ {emails[hidden - 1].date:%Y-%m-%d}，"
                    "可经检索调阅）\n")
    catalog += "\n".join(lines)
    body_budget = max_chars - len(catalog)
    full_blocks, used, start = [], 0, len(emails)
    for i in range(len(emails) - 1, -1, -1):
        if full_blocks and used + len(blocks[i]) > body_budget:
            break
        full_blocks.append(blocks[i])
        used += len(blocks[i])
        start = i
    full_blocks.reverse()
    text = (catalog + "\n\n【最近邮件全文】\n" + "\n\n".join(full_blocks)
            + "\n\n（更早的邮件未附全文；围绕任何邮件提问时，系统会自动检索相关邮件全文补充进来）")
    return text, frozenset(range(start, len(emails))), hidden


# Web 模式「开始分析」只建会话不出报告，注入的指令不要求模型主动输出报告
_PREPARE_INSTRUCTION = "以上是全部往来邮件，请熟悉这些内容。之后我会围绕这些邮件提问，需要报告时我会明确要求；现在无需输出任何分析。"

# 上下文超预算时，把最老的对话轮次压缩为摘要而非直接丢弃
_COMPRESS_INSTRUCTION = (
    "请把以下对话记录压缩为一份精炼的中文要点摘要（不超过 600 字），"
    "保留关键事实、结论、数字、承诺与待办，丢弃寒暄与重复内容。"
    "直接输出摘要正文，不要加任何解释或标题。"
)
_SUMMARY_PREFIX = "【前情摘要】"

# 会话块首轮问答落库后，用 AI 总结语义化标题
_TITLE_INSTRUCTION = (
    "请为以下对话起一个简短的中文标题（不超过 15 个字），概括对话的核心主题。"
    "只输出标题本身，不要引号、书名号、结尾标点或任何解释。"
)


# 追问时按问题检索相关邮件全文，拼进当轮消息的标记（get_conversation 据此剥离展示）
_RETRIEVAL_HEAD = "【检索补充】"
_QUESTION_MARK = "【我的问题】"
_STRIP_RETRIEVAL_RE = re.compile(
    rf"^{re.escape(_RETRIEVAL_HEAD)}.*?{re.escape(_QUESTION_MARK)}", re.S)


def _strip_retrieval(content: str) -> str:
    """去掉消息里的检索补充块，只留用户原始问题（用于对话展示与报告溯源）。"""
    return _STRIP_RETRIEVAL_RE.sub("", content, count=1)


# 模型主动调阅协议：目录中只有片段的邮件，模型可请求全文（见 _materials 注入的说明）
_REQUEST_PROTOCOL = (
    "如果某封邮件只有目录片段而你需要完整正文来作答，可在回复中单独一行写：\n"
    "REQUEST_EMAIL: 3, 7\n"
    "系统会把对应编号邮件的全文发给你，然后你继续作答。一次最多 8 封；已附全文的邮件不要请求。"
)
_REQUEST_LINE_RE = re.compile(r"^[^\n]*REQUEST_EMAIL\s*[:：][^\n]*$", re.I | re.M)
_REQUEST_NUMS_RE = re.compile(r"REQUEST_EMAIL\s*[:：]\s*([0-9\s,，、;；/-]+)", re.I)
_FETCH_HEAD = "【调阅结果】"
_REQUEST_LIMIT_REPLY = "（邮件调阅次数已达上限，如需更多邮件内容请换个问法继续提问）"


def _parse_request(reply: str) -> list:
    """从模型回复中解析 REQUEST_EMAIL 编号（1 起始），最多 8 个。"""
    m = _REQUEST_NUMS_RE.search(reply or "")
    if not m:
        return []
    nums = [int(tok) for tok in re.split(r"[^0-9]+", m.group(1)) if tok]
    return nums[:8]


def _clean_request(reply: str) -> str:
    """去掉回复中的 REQUEST_EMAIL 行（用于落库与前端展示）。"""
    return _REQUEST_LINE_RE.sub("", reply or "").strip()


class _Session:
    def __init__(self, target: str, messages: list, history_index: int = None):
        self.target = target
        self.messages = messages
        self.updated = time.time()
        self.email_count = 0    # 纳入会话的邮件数（可能是用户选中的子集）
        self.date_range = ""    # 纳入会话邮件的时间范围
        self.history_index = history_index  # 对应持久化历史中的会话块下标
        self.emails = []        # 会话邮件（供追问时检索）
        self.index = None       # EmailIndex；全部邮件已附全文时为 None（无需检索）
        self.full_indices = frozenset()  # 已随初始注入附全文的邮件下标


class Analyzer:
    """维护每个用户的分析会话：首次注入邮件摘要，之后多轮追问。"""

    def __init__(self, llm: LLMClient, context_chars: int = 24000, ttl_seconds: int = 7200,
                 history_dir: str = "", summary_dir: str = ""):
        self.llm = llm
        self.context_chars = context_chars
        self.ttl = ttl_seconds
        self.history_dir = history_dir
        self.summary_dir = summary_dir  # 早期邮件按月摘要的缓存目录；留空禁用分层摘要
        self._sessions = {}
        self._lock = threading.Lock()

    def _get(self, user: str):
        with self._lock:
            sess = self._sessions.get(user)
            if sess and time.time() - sess.updated > self.ttl:
                del self._sessions[user]
                return None
            return sess

    def has_session(self, user: str) -> bool:
        return self._get(user) is not None

    def reset(self, user: str) -> None:
        with self._lock:
            self._sessions.pop(user, None)

    @staticmethod
    def _coverage(emails: list, full_indices: frozenset) -> str:
        """注入材料中全文覆盖情况的说明文字。"""
        if len(full_indices) == len(emails):
            return f"（共 {len(emails)} 封，按时间升序排列，均附全文）"
        return (f"（共 {len(emails)} 封，全部列于目录，其中 {len(full_indices)} 封附全文，"
                "其余可在追问时按需检索调阅）")

    def _bind_emails(self, sess, emails: list, full_indices: frozenset) -> None:
        """把会话邮件挂上会话，并在未全部附全文时建检索索引（供追问按需调阅）。"""
        sess.emails = list(emails)
        sess.full_indices = full_indices
        if len(full_indices) < len(emails):
            sess.index = EmailIndex(emails)

    def _materials(self, target: str, emails: list) -> tuple:
        """生成注入材料。目录也放不下的早期邮件达到 10 封且有摘要存储时，
        改用按月摘要替代一行折叠说明；摘要块比一行说明长，会再多挤出几封
        隐藏邮件，迭代重排到收敛（最多 3 轮，摘要按月缓存，迭代几乎零成本）。"""
        digest, full, hidden = build_context(emails, self.context_chars)
        if not self.summary_dir or hidden < 10:
            return digest, full
        store = SummaryStore(self.summary_dir)
        budget = int(self.context_chars * 0.25)
        for _ in range(3):
            note = build_summary_block(self.llm, store, target, emails[:hidden], budget)
            if not note:
                break
            digest, full, new_hidden = build_context(emails, self.context_chars,
                                                     hidden_note=note)
            if new_hidden <= hidden:
                break
            hidden = new_hidden
        return digest, full

    def _inject_prompt(self, target: str, emails: list, digest: str,
                       full_indices: frozenset, instruction: str) -> str:
        """拼装首次注入的用户消息；未全部附全文时附 REQUEST_EMAIL 调阅协议说明。"""
        prompt = (f"以下是我的邮箱与 {target} 的往来邮件材料"
                  f"{self._coverage(emails, full_indices)}：\n\n{digest}")
        if len(full_indices) < len(emails):
            prompt += f"\n\n{_REQUEST_PROTOCOL}"
        return prompt + f"\n\n{instruction}"

    def prepare_session(self, user: str, target: str, emails: list,
                        history_index: int = None) -> None:
        """只注入邮件材料建立会话，不调用模型、不生成报告。"""
        digest, full_indices = self._materials(target, emails)
        prompt = self._inject_prompt(target, emails, digest, full_indices,
                                     _PREPARE_INSTRUCTION)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        sess = _Session(target, messages, history_index)
        sess.email_count = len(emails)
        if emails:
            sess.date_range = f"{emails[0].date:%Y-%m-%d %H:%M} ~ {emails[-1].date:%Y-%m-%d %H:%M}"
        self._bind_emails(sess, emails, full_indices)
        with self._lock:
            self._sessions[user] = sess

    def replay(self, user: str, messages: list) -> None:
        """把持久化的历史问答回放进当前内存会话（切换会话/重启恢复用），随后按需压缩。"""
        sess = self._get(user)
        if sess is None:
            return
        with self._lock:
            for m in messages:
                if m.get("role") in ("user", "assistant") and m.get("content"):
                    sess.messages.append({"role": m["role"], "content": m["content"]})
            sess.updated = time.time()
        self._trim(sess)

    def _trim(self, sess) -> None:
        """上下文超预算时压缩最老的中间轮次为摘要；保留 system、邮件注入与最近 4 条原文。

        压缩调用模型失败时回退为直接丢弃，保证主流程不中断。
        """
        budget = self.context_chars + 8000
        with self._lock:
            over = len(sess.messages) > 6 and \
                sum(len(m["content"]) for m in sess.messages) > budget
            old = list(sess.messages[2:-4]) if over else []
        if not old:
            return
        text = "\n\n".join(
            ("用户：" if m["role"] == "user" else "助手：") + m["content"] for m in old)
        try:
            summary = self.llm.chat([
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _COMPRESS_INSTRUCTION + "\n\n" + text},
            ])
        except Exception:
            summary = ""
        with self._lock:
            if summary:
                sess.messages[2:-4] = [
                    {"role": "user", "content": f"{_SUMMARY_PREFIX}此前对话要点：\n{summary}"},
                    {"role": "assistant", "content": "好的，我已了解此前对话要点，请继续。"},
                ]
            else:
                del sess.messages[2:-4]
            # 极端情况下（最近几轮单条超长）仍超预算，退化为丢弃最老原文轮次（保住摘要与最近 4 条）
            start = 4 if summary else 2
            while len(sess.messages) > start + 4 and \
                    sum(len(m["content"]) for m in sess.messages) > budget:
                sess.messages.pop(start)

    def session_info(self, user: str) -> dict:
        """会话素材信息（邮件数与时间范围），供报告溯源。"""
        sess = self._get(user)
        if sess is None:
            return {}
        return {"email_count": sess.email_count, "date_range": sess.date_range}

    def start_analysis(self, user: str, target: str, emails: list) -> str:
        digest, full_indices = self._materials(target, emails)
        prompt = self._inject_prompt(target, emails, digest, full_indices,
                                     _INITIAL_INSTRUCTION)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        reply = self.llm.chat(messages)
        messages.append({"role": "assistant", "content": reply})
        sess = _Session(target, messages)
        sess.email_count = len(emails)
        if emails:
            sess.date_range = f"{emails[0].date:%Y-%m-%d %H:%M} ~ {emails[-1].date:%Y-%m-%d %H:%M}"
        self._bind_emails(sess, emails, full_indices)
        with self._lock:
            self._sessions[user] = sess
        self._record(history.start_session, target, _INITIAL_INSTRUCTION, reply)
        return reply

    def get_conversation(self, user: str) -> list:
        """当前会话中的问答记录（不含 system 与邮件材料注入），检索补充块、
        调阅结果与 REQUEST_EMAIL 请求行已剥离，纯调阅的中间轮次不展示。"""
        sess = self._get(user)
        if sess is None:
            return []
        with self._lock:
            out = []
            for m in sess.messages[2:]:
                content = m["content"]
                if m["role"] == "user":
                    if content.startswith(_FETCH_HEAD):
                        continue
                    content = _strip_retrieval(content)
                else:
                    content = _clean_request(content)
                if content:
                    out.append({"role": m["role"], "content": content})
            return out

    def _augment(self, sess, text: str) -> str:
        """追问时按问题检索尚未附全文的邮件，把最相关的全文拼进当轮问题。

        全部邮件已附全文（无检索索引）或零命中时原样返回。检索预算取上下文
        总量的一半，限制在 2000~12000 字符之间。
        """
        if sess.index is None:
            return text
        budget = min(12000, max(2000, self.context_chars // 2))
        hits = sess.index.search(text, max_chars=budget, exclude=sess.full_indices)
        if not hits:
            return text
        blocks = "\n\n".join(sess.emails[i].digest(i + 1) for i in hits)
        return (f"{_RETRIEVAL_HEAD}系统按问题检索到 {len(hits)} 封此前未附全文的相关邮件，"
                f"供回答参考：\n\n{blocks}\n\n{_QUESTION_MARK}{text}")

    def _fetch_block(self, sess, nums: list) -> str:
        """把模型请求调阅的邮件全文拼成一条用户消息；无有效请求时返回空串。

        仅在会话未全部附全文（有检索索引）时可用；单次调阅总量与检索预算一致。
        """
        if not nums or sess.index is None:
            return ""
        budget = min(12000, max(2000, self.context_chars // 2))
        blocks, notes, used = [], [], 0
        for n in dict.fromkeys(nums):
            i = n - 1
            if not 0 <= i < len(sess.emails):
                notes.append(f"[{n}] 编号无效")
            elif i in sess.full_indices:
                notes.append(f"[{n}] 全文已在上文，无需调阅")
            else:
                b = sess.emails[i].digest(n)
                if blocks and used + len(b) > budget:
                    notes.append(f"[{n}] 超出本次调阅预算，未附上")
                    continue
                blocks.append(b)
                used += len(b)
        if not blocks and not notes:
            return ""
        parts = []
        if blocks:
            parts.append("你请求的邮件全文如下：\n\n" + "\n\n".join(blocks))
        if notes:
            parts.append("说明：" + "；".join(notes))
        return _FETCH_HEAD + "\n\n".join(parts) + "\n\n请基于以上材料继续作答。"

    def chat(self, user: str, text: str) -> str:
        sess = self._get(user)
        if sess is None:
            raise RuntimeError("没有进行中的分析会话，请先发送邮箱地址开始分析")
        content = self._augment(sess, text)
        with self._lock:
            sess.messages.append({"role": "user", "content": content})
            sess.updated = time.time()
        self._trim(sess)  # 超预算时把最老轮次压缩为摘要，而非直接遗忘
        with self._lock:
            messages = list(sess.messages)
        reply = self.llm.chat(messages)
        for _ in range(2):  # 模型按 REQUEST_EMAIL 协议调阅未附全文的邮件后继续作答
            block = self._fetch_block(sess, _parse_request(reply))
            if not block:
                break
            with self._lock:
                sess.messages.append({"role": "assistant", "content": reply})
                sess.messages.append({"role": "user", "content": block})
                messages = list(sess.messages)
            reply = self.llm.chat(messages)
        reply = _clean_request(reply) or _REQUEST_LIMIT_REPLY
        with self._lock:
            sess.messages.append({"role": "assistant", "content": reply})
        self._record(history.append_exchange, sess.target, text, reply, sess.history_index)
        self._record_title(sess, text, reply)
        return reply

    def chat_stream(self, user: str, text: str):
        """流式追问：先写入用户消息，逐段产出回复增量，完成后落库。

        模型回复中含 REQUEST_EMAIL 调阅请求时，补入全文后续流式作答（最多 2 轮）。
        客户端中断（GeneratorExit）或流式出错时，会话回滚到本轮提问之前，
        避免留下没有回答的悬空上下文。
        """
        sess = self._get(user)
        if sess is None:
            raise RuntimeError("没有进行中的分析会话，请先发送邮箱地址开始分析")
        content = self._augment(sess, text)
        with self._lock:
            sess.messages.append({"role": "user", "content": content})
            sess.updated = time.time()
        self._trim(sess)  # 超预算时把最老轮次压缩为摘要，而非直接遗忘
        with self._lock:
            mark = len(sess.messages) - 1  # 本轮用户消息的位置；中断时回滚到此
            messages = list(sess.messages)
        segments, completed = [], False
        try:
            for _ in range(3):  # 1 次作答 + 最多 2 轮调阅续答
                buf = []
                for delta in self.llm.chat_stream(messages):
                    buf.append(delta)
                    yield delta
                reply = "".join(buf).strip()
                segments.append(reply)
                block = self._fetch_block(sess, _parse_request(reply))
                if not block:
                    break
                yield "\n\n（调阅邮件全文后继续作答）\n\n"
                with self._lock:
                    sess.messages.append({"role": "assistant", "content": reply})
                    sess.messages.append({"role": "user", "content": block})
                    messages = list(sess.messages)
            completed = True
        finally:
            if completed:
                final = _clean_request(segments[-1]) or _REQUEST_LIMIT_REPLY
                with self._lock:
                    sess.messages.append({"role": "assistant", "content": final})
                self._record(history.append_exchange, sess.target, text, final,
                             sess.history_index)
                self._record_title(sess, text, final)
            else:
                with self._lock:
                    del sess.messages[mark:]

    def _record(self, fn, target: str, *args) -> None:
        if not self.history_dir:
            return
        fn(self.history_dir, target, *args)

    def _record_title(self, sess, question: str, reply: str) -> None:
        """会话块的首轮问答落库后，用 AI 总结语义化标题；失败时回退为问题前 20 字。"""
        if not self.history_dir or sess.history_index is None:
            return
        rec = history.get_session(self.history_dir, sess.target, sess.history_index)
        if not rec or rec.get("title") or len(rec.get("messages") or []) != 2:
            return
        title = ""
        try:
            title = self.llm.chat([
                {"role": "user", "content":
                    f"{_TITLE_INSTRUCTION}\n\n用户：{question[:300]}\n\n助手：{reply[:300]}"},
            ]).strip().strip("\"'“”‘’《》。. \n")[:20]
        except Exception:
            pass
        history.set_title(self.history_dir, sess.target, sess.history_index,
                          title or question.strip()[:20] or "新会话")
