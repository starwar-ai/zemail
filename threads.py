"""主题线程归并：把往来邮件串成话题线程，并支持按线程生成「来龙去脉」报告。

归并依据两个信号（并查集）：
1. Message-ID 引用链（References / In-Reply-To 头，入库时捕获；旧缓存邮件没有
   这些头，自动退化为仅靠主题归并）；
2. 规范化主题（剥掉 Re:/回复:/转发: 前缀与 [列表标签] 后的主题）。

同一联系人的往来中主题相同的邮件默认视为同一话题，便于追踪某主题的来龙去脉。
"""
import hashlib
import re

from preprocess import email_hash

_REPLY_PREFIX_RE = re.compile(r"^\s*(re|fw|fwd|回复|答复|转发)\s*[:：]\s*", re.I)
_LIST_TAG_RE = re.compile(r"^\[[^\]\n]{0,40}\]\s*")
_MSG_ID_RE = re.compile(r"<([^<>\s]+)>")


def normalize_subject(subject: str) -> str:
    """剥掉 Re:/回复:/转发: 前缀与 [列表标签]，得到线程归并用的规范主题。"""
    s = (subject or "").strip()
    prev = None
    while prev != s:
        prev = s
        s = _REPLY_PREFIX_RE.sub("", s)
        s = _LIST_TAG_RE.sub("", s).strip()
    return s or "(无主题)"


def group_threads(emails: list) -> list:
    """把邮件归并为线程，返回 [邮件下标列表（升序）]，按线程首封日期升序。"""
    n = len(emails)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    # 第一遍：Message-ID 引用链
    id_map = {}
    for i, e in enumerate(emails):
        mid = (getattr(e, "message_id", "") or "").strip().strip("<>")
        if mid and mid not in id_map:
            id_map[mid] = i
    for i, e in enumerate(emails):
        refs = _MSG_ID_RE.findall(getattr(e, "references", "") or "")
        irr = (getattr(e, "in_reply_to", "") or "").strip().strip("<>")
        if irr:
            refs.append(irr)
        for r in refs:
            j = id_map.get(r)
            if j is not None:
                union(i, j)
    # 第二遍：规范化主题（同主题并入同一线程；无主题不参与归并）
    by_subject = {}
    for i, e in enumerate(emails):
        key = normalize_subject(e.subject).lower()
        if key == "(无主题)":
            continue
        if key in by_subject:
            union(i, by_subject[key])
        else:
            by_subject[key] = i
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=lambda g: emails[g[0]].date)


def thread_ids(emails: list) -> list:
    """与 emails 对齐的线程编号列表（编号即 group_threads 返回的顺序）。"""
    ids = [0] * len(emails)
    for tid, group in enumerate(group_threads(emails)):
        for i in group:
            ids[i] = tid
    return ids


# ---------- 线程「来龙去脉」报告 ----------

_THREAD_REPORT_INSTRUCTION = (
    "以下是我的邮箱与 {target} 在「{subject}」这一主题下的往来邮件"
    "（共 {count} 封，按时间升序）。请梳理这个主题的来龙去脉，"
    "用 Markdown 按下面模板输出中文报告（模板外不要任何解释）：\n"
    "## 一句话结论\n"
    "（用一句话概括当前状态与走向，不超过 40 字）\n"
    "## 起因\n"
    "- （主题因何而起、由谁提出；1~3 条，每条一行）\n"
    "## 关键进展\n"
    "- **M月D日** 要点（谁做了什么、结果如何；按时间升序）\n"
    "## 结论与现状\n"
    "- ✅ 已达成：……（没有就写“暂无”）\n"
    "- ⏳ 待跟进：……（没有就写“暂无”）\n"
    "## 关键数字\n"
    "- 指标：数值（涉及的价格、数量、日期、版本号等及其变化；无则整节省略）\n"
    "要求：严格依据邮件内容；每条要点不超过 30 字，日期写成 M月D日；"
    "关键数字加粗；不要编造。"
)

# 报告模板版本：升级提示词模板时递增，旧缓存（无前缀键）自动失效
_REPORT_TPL_VERSION = 2

_MATERIAL_CHARS = 16000  # 线程报告的输入预算：超长线程掐头留尾


def _thread_materials(emails: list, indices: list, budget: int = _MATERIAL_CHARS) -> str:
    blocks = [emails[i].digest(n) for n, i in enumerate(indices, 1)]
    if sum(len(b) for b in blocks) <= budget:
        return "\n\n".join(blocks)
    head_n = max(1, len(blocks) // 4)
    head = blocks[:head_n]
    used = sum(len(b) for b in head)
    tail = []
    for b in reversed(blocks[head_n:]):
        if used + len(b) > budget:
            break
        tail.append(b)
        used += len(b)
    tail.reverse()
    omitted = len(blocks) - head_n - len(tail)
    return "\n\n".join(head + [f"……（中间省略 {omitted} 封）……"] + tail)


def thread_report(llm, store, target: str, emails: list, indices: list) -> str:
    """生成某主题线程的「来龙去脉」报告；按线程内容哈希缓存在派生数据文件中。

    线程内任一邮件变化（或有新邮件并入线程）则缓存失效、重新生成。
    """
    key = f"v{_REPORT_TPL_VERSION}:" + hashlib.md5(
        "|".join(email_hash(emails[i]) for i in indices).encode("ascii")
    ).hexdigest()
    cached = (store.data.get("threads") or {}).get(key)
    if cached:
        return cached
    subject = normalize_subject(emails[indices[0]].subject)
    prompt = (_THREAD_REPORT_INSTRUCTION.format(
        target=target, subject=subject, count=len(indices))
        + "\n\n" + _thread_materials(emails, indices))
    text = llm.chat([{"role": "user", "content": prompt}]).strip()
    if text:
        threads = store.data.setdefault("threads", {})
        # 顺手清掉旧模板的缓存条目，避免派生文件无限累积
        stale = [k for k in threads
                 if not k.startswith(f"v{_REPORT_TPL_VERSION}:")]
        for k in stale:
            del threads[k]
        threads[key] = text
        store.save()
    return text
