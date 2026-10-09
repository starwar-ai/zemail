"""基于 OpenAI 兼容接口的邮件往来会话式分析。"""
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


class _Session:
    def __init__(self, target: str, messages: list):
        self.target = target
        self.messages = messages
        self.updated = time.time()


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

    def chat(self, user: str, text: str) -> str:
        sess = self._get(user)
        if sess is None:
            raise RuntimeError("没有进行中的分析会话，请先发送邮箱地址开始分析")
        with self._lock:
            sess.messages.append({"role": "user", "content": text})
            sess.updated = time.time()
            # 保留 system 与最初的邮件摘要，仅裁剪过老的中间对话轮次
            while len(sess.messages) > 6 and sum(len(m["content"]) for m in sess.messages) > self.context_chars + 8000:
                sess.messages.pop(2)
            messages = list(sess.messages)
        reply = self.llm.chat(messages)
        with self._lock:
            sess.messages.append({"role": "assistant", "content": reply})
        self._record(history.append_exchange, sess.target, text, reply)
        return reply

    def _record(self, fn, target: str, *args) -> None:
        if not self.history_dir:
            return
        fn(self.history_dir, target, *args)
