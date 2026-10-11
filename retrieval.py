"""邮件检索：纯 Python 的轻量相关度排序（中文 unigram+bigram、英文单词，类 BM25 打分）。

邮件量大时完整正文无法全部注入模型上下文，这里负责两件事：
1. 生成全部邮件的紧凑目录（一行一封，优先采用预处理信息卡的摘要）；
2. 追问时按问题对全部邮件打分，挑出最相关的若干封全文补充进当轮上下文。

提供预处理信息卡（摘要/实体/待办）时，这些字段以更高权重参与打分；
提供向量嵌入时，BM25 得分与余弦相似度混合排序，兼顾关键词与语义命中；
提供线程编号时，命中的邮件会自动扩展到同一线程的其余邮件（预算内），
让模型看到一个话题的完整来龙去脉。
"""
import math
import re
from collections import Counter

from preprocess import todo_text

_SEG_RE = re.compile(r"[一-鿿]+|[a-zA-Z0-9]+")


def tokenize(text: str) -> list:
    """中文切 unigram+bigram，英文/数字按单词，全部小写。"""
    tokens = []
    for seg in _SEG_RE.findall(text.lower()):
        if seg[0].isascii():
            tokens.append(seg)
        else:
            tokens.extend(seg)
            tokens.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return tokens


def _date_tokens(d) -> list:
    """邮件日期的常见写法也作为检索词，让「3月5日」「2024-03-05」这类问题能直接命中。"""
    if d.year <= 1900:
        return []
    return [f"{d:%Y-%m-%d}", f"{d.year}年{d.month}月{d.day}日", f"{d.month}月{d.day}日"]


def _normalize_vec(vec) -> list:
    norm = math.sqrt(sum(x * x for x in vec))
    if not norm:
        return None
    return [x / norm for x in vec]


def _dot(a: list, b: list) -> float:
    return sum(x * y for x, y in zip(a, b))


def catalog_line(email, index: int, card=None, snippet_chars: int = 40) -> str:
    """目录行：序号、日期、方向、主题、摘要片段（无信息卡时退化为正文开头）。"""
    if card and card.get("summary"):
        snippet = re.sub(r"\s+", " ", card["summary"])[:60]
    else:
        snippet = re.sub(r"\s+", " ", email.body.strip())[:snippet_chars]
    line = (f"[{index}] {email.date:%Y-%m-%d %H:%M} | {email.direction} | "
            f"{email.subject} | {snippet}")
    if card:
        tags = []
        if card.get("intent"):
            tags.append(card["intent"])
        if card.get("todos"):
            tags.append("含待办")
        if tags:
            line += f"（{'，'.join(tags)}）"
    return line


class EmailIndex:
    """一份邮件列表的检索索引。打分 = Σ idf(t) * min(tf(t), TF_CAP)，主题词权重加倍。

    cards（信息卡，与 emails 对齐）的摘要/待办词权重 ×2、实体词与主题同权；
    vectors（嵌入向量，与 emails 对齐）存在时，search 可传 query_vector 做混合排序；
    threads（线程编号，与 emails 对齐）存在时，命中邮件自动扩展同线程邮件。
    """

    SUBJECT_WEIGHT = 3
    CARD_WEIGHT = 2
    TF_CAP = 3
    BODY_CHARS = 4000  # 建索引只看正文前 4000 字，覆盖要点且控制耗时

    def __init__(self, emails: list, cards: list = None, vectors: list = None,
                 threads: list = None):
        self.emails = list(emails)
        docs, df = [], Counter()
        for i, e in enumerate(self.emails):
            counts = Counter(tokenize(e.body[:self.BODY_CHARS]) + tokenize(e.from_addr))
            for t, c in Counter(tokenize(e.subject)).items():
                counts[t] += c * self.SUBJECT_WEIGHT
            card = cards[i] if cards and i < len(cards) else None
            if card:
                for t, c in Counter(tokenize(str(card.get("summary") or ""))).items():
                    counts[t] += c * self.CARD_WEIGHT
                for t, c in Counter(tokenize(" ".join(
                        str(x) for x in card.get("entities") or []))).items():
                    counts[t] += c * self.SUBJECT_WEIGHT
                for t, c in Counter(tokenize(" ".join(
                        todo_text(x) for x in card.get("todos") or []))).items():
                    counts[t] += c * self.CARD_WEIGHT
            for t in _date_tokens(e.date):
                counts[t] += 1
            docs.append(counts)
            df.update(counts.keys())
        n = max(len(docs), 1)
        self._docs = docs
        self._idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self._vectors = None
        if vectors:
            vs = [_normalize_vec(v) if v else None for v in vectors]
            if any(vs):
                self._vectors = vs
        self._threads = threads
        self._by_thread = {}
        if threads:
            for i, tid in enumerate(threads):
                self._by_thread.setdefault(tid, []).append(i)

    def search(self, query: str, max_chars: int, exclude: frozenset = frozenset(),
               max_results: int = 20, query_vector: list = None) -> list:
        """按相关度选中邮件下标（按时间升序返回）。零命中返回空表。

        只保留得分不低于最高分 25% 的结果，过滤泛词带来的噪声命中；
        选中的邮件 digest 总长度控制在 max_chars 内。query_vector 存在时，
        BM25 得分（按最高分归一）与余弦相似度相加做混合排序，纯语义命中
        （无关键词重叠）的邮件也能入选。命中邮件所在线程的其余邮件在预算内
        一并带出（线程扩展），保证话题上下文完整。
        """
        scored = []
        q_tokens = set(tokenize(query))
        if q_tokens:
            for i, counts in enumerate(self._docs):
                if i in exclude:
                    continue
                s = sum(self._idf.get(t, 0.0) * min(counts.get(t, 0), self.TF_CAP)
                        for t in q_tokens)
                if s > 0:
                    scored.append((s, i))
        if query_vector is not None and self._vectors:
            qv = _normalize_vec(query_vector)
            if qv:
                bm = {i: s for s, i in scored}
                bm_best = max(bm.values()) if bm else 0.0
                combined = []
                for i in range(len(self._docs)):
                    if i in exclude:
                        continue
                    s = bm.get(i, 0.0) / bm_best if bm_best else 0.0
                    v = self._vectors[i] if i < len(self._vectors) else None
                    if v is not None:
                        s += max(0.0, _dot(qv, v))
                    if s > 0:
                        combined.append((s, i))
                scored = combined
        if not scored:
            return []
        scored.sort(reverse=True)
        best = scored[0][0]
        picked, used = [], 0
        for s, i in scored[:max_results]:
            if s < best * 0.25:
                break
            size = len(self.emails[i].digest(i + 1))
            if picked and used + size > max_chars:
                continue
            picked.append(i)
            used += size
        # 线程扩展：命中邮件的同线程邮件在预算内一并带出，保证话题来龙去脉完整
        if self._by_thread:
            picked_set = set(picked)
            for i in list(picked):
                for j in self._by_thread.get(self._threads[i], []):
                    if j in picked_set or j in exclude:
                        continue
                    size = len(self.emails[j].digest(j + 1))
                    if used + size > max_chars:
                        continue
                    picked_set.add(j)
                    picked.append(j)
                    used += size
        return sorted(picked)
