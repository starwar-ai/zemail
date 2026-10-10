"""基于 OpenAI 兼容接口的邮件往来会话式分析。"""
import json
import threading
import time

import requests

import history

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


def build_digest(emails: list, max_chars: int) -> tuple:
    """把邮件列表整理成文本摘要，超长时丢弃最早期的邮件。返回 (摘要, 实际纳入封数)。"""
    blocks = [e.digest(i) for i, e in enumerate(emails, 1)]
    while blocks and sum(len(b) for b in blocks) > max_chars:
        blocks.pop(0)
    dropped = len(emails) - len(blocks)
    digest = "\n\n".join(blocks)
    if dropped:
        digest = f"（因长度限制，最早的 {dropped} 封邮件未纳入分析）\n\n" + digest
    return digest, len(blocks)


# Web 模式「开始分析」只建会话不出报告，注入的指令不要求模型主动输出报告
_PREPARE_INSTRUCTION = "以上是全部往来邮件，请熟悉这些内容。之后我会围绕这些邮件提问，需要报告时我会明确要求；现在无需输出任何分析。"

# 上下文超预算时，把最老的对话轮次压缩为摘要而非直接丢弃
_COMPRESS_INSTRUCTION = (
    "请把以下对话记录压缩为一份精炼的中文要点摘要（不超过 600 字），"
    "保留关键事实、结论、数字、承诺与待办，丢弃寒暄与重复内容。"
    "直接输出摘要正文，不要加任何解释或标题。"
)
_SUMMARY_PREFIX = "【前情摘要】"


class _Session:
    def __init__(self, target: str, messages: list, history_index: int = None):
        self.target = target
        self.messages = messages
        self.updated = time.time()
        self.email_count = 0    # 纳入会话的邮件数（可能是用户选中的子集）
        self.date_range = ""    # 纳入会话邮件的时间范围
        self.history_index = history_index  # 对应持久化历史中的会话块下标


class Analyzer:
    """维护每个用户的分析会话：首次注入邮件摘要，之后多轮追问。"""

    def __init__(self, llm: LLMClient, context_chars: int = 24000, ttl_seconds: int = 7200,
                 history_dir: str = ""):
        self.llm = llm
        self.context_chars = context_chars
        self.ttl = ttl_seconds
        self.history_dir = history_dir
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

    def prepare_session(self, user: str, target: str, emails: list,
                        history_index: int = None) -> None:
        """只注入邮件摘要建立会话，不调用模型、不生成报告。"""
        digest, count = build_digest(emails, self.context_chars)
        prompt = (
            f"以下是我的邮箱与 {target} 的往来邮件（共 {len(emails)} 封，纳入分析 {count} 封，"
            f"按时间升序排列）：\n\n{digest}\n\n{_PREPARE_INSTRUCTION}"
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        sess = _Session(target, messages, history_index)
        sess.email_count = len(emails)
        if emails:
            sess.date_range = f"{emails[0].date:%Y-%m-%d %H:%M} ~ {emails[-1].date:%Y-%m-%d %H:%M}"
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
        digest, count = build_digest(emails, self.context_chars)
        prompt = (
            f"以下是我的邮箱与 {target} 的往来邮件（共 {len(emails)} 封，纳入分析 {count} 封，"
            f"按时间升序排列）：\n\n{digest}\n\n{_INITIAL_INSTRUCTION}"
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        reply = self.llm.chat(messages)
        messages.append({"role": "assistant", "content": reply})
        with self._lock:
            self._sessions[user] = _Session(target, messages)
        self._record(history.start_session, target, _INITIAL_INSTRUCTION, reply)
        return reply

    def get_conversation(self, user: str) -> list:
        """当前会话中的问答记录（不含 system 与邮件摘要注入），用于报告溯源。"""
        sess = self._get(user)
        if sess is None:
            return []
        with self._lock:
            return [dict(m) for m in sess.messages[2:]]

    def chat(self, user: str, text: str) -> str:
        sess = self._get(user)
        if sess is None:
            raise RuntimeError("没有进行中的分析会话，请先发送邮箱地址开始分析")
        with self._lock:
            sess.messages.append({"role": "user", "content": text})
            sess.updated = time.time()
        self._trim(sess)  # 超预算时把最老轮次压缩为摘要，而非直接遗忘
        with self._lock:
            messages = list(sess.messages)
        reply = self.llm.chat(messages)
        with self._lock:
            sess.messages.append({"role": "assistant", "content": reply})
        self._record(history.append_exchange, sess.target, text, reply, sess.history_index)
        return reply

    def chat_stream(self, user: str, text: str):
        """流式追问：先写入用户消息，逐段产出回复增量，完成后落库。

        客户端中断（GeneratorExit）或流式出错时，移除刚写入的用户消息，
        避免会话里留下没有回答的悬空上下文。
        """
        sess = self._get(user)
        if sess is None:
            raise RuntimeError("没有进行中的分析会话，请先发送邮箱地址开始分析")
        with self._lock:
            sess.messages.append({"role": "user", "content": text})
            sess.updated = time.time()
        self._trim(sess)  # 超预算时把最老轮次压缩为摘要，而非直接遗忘
        with self._lock:
            messages = list(sess.messages)
        full, completed = [], False
        try:
            for delta in self.llm.chat_stream(messages):
                full.append(delta)
                yield delta
            completed = True
        finally:
            if completed:
                reply = "".join(full).strip()
                with self._lock:
                    sess.messages.append({"role": "assistant", "content": reply})
                self._record(history.append_exchange, sess.target, text, reply,
                             sess.history_index)
            else:
                with self._lock:
                    if (sess.messages and sess.messages[-1].get("role") == "user"
                            and sess.messages[-1].get("content") == text):
                        sess.messages.pop()

    def _record(self, fn, target: str, *args) -> None:
        if not self.history_dir:
            return
        fn(self.history_dir, target, *args)
