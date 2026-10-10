"""邮件检索：纯 Python 的轻量相关度排序（中文 unigram+bigram、英文单词，类 BM25 打分）。

邮件量大时完整正文无法全部注入模型上下文，这里负责两件事：
1. 生成全部邮件的紧凑目录（一行一封），让模型始终了解全貌；
2. 追问时按问题对全部邮件打分，挑出最相关的若干封全文补充进当轮上下文。
"""
import math
import re
from collections import Counter

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


def catalog_line(email, index: int, snippet_chars: int = 40) -> str:
    """目录行：序号、日期、方向、主题、正文开头片段。"""
    snippet = re.sub(r"\s+", " ", email.body.strip())[:snippet_chars]
    return (f"[{index}] {email.date:%Y-%m-%d %H:%M} | {email.direction} | "
            f"{email.subject} | {snippet}")


class EmailIndex:
    """一份邮件列表的检索索引。打分 = Σ idf(t) * min(tf(t), TF_CAP)，主题词权重加倍。"""

    SUBJECT_WEIGHT = 3
    TF_CAP = 3
    BODY_CHARS = 4000  # 建索引只看正文前 4000 字，覆盖要点且控制耗时

    def __init__(self, emails: list):
        self.emails = list(emails)
        docs, df = [], Counter()
        for e in self.emails:
            counts = Counter(tokenize(e.body[:self.BODY_CHARS]) + tokenize(e.from_addr))
            for t, c in Counter(tokenize(e.subject)).items():
                counts[t] += c * self.SUBJECT_WEIGHT
            for t in _date_tokens(e.date):
                counts[t] += 1
            docs.append(counts)
            df.update(counts.keys())
        n = max(len(docs), 1)
        self._docs = docs
        self._idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def search(self, query: str, max_chars: int, exclude: frozenset = frozenset(),
               max_results: int = 20) -> list:
        """按相关度选中邮件下标（按时间升序返回）。零命中返回空表。

        只保留得分不低于最高分 25% 的结果，过滤泛词带来的噪声命中；
        选中的邮件 digest 总长度控制在 max_chars 内。
        """
        q_tokens = set(tokenize(query))
        if not q_tokens:
            return []
        scored = []
        for i, counts in enumerate(self._docs):
            if i in exclude:
                continue
            s = sum(self._idf.get(t, 0.0) * min(counts.get(t, 0), self.TF_CAP)
                    for t in q_tokens)
            if s > 0:
                scored.append((s, i))
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
        return sorted(picked)
