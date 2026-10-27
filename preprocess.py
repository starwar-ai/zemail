"""邮件派生数据的离线预计算：单封信息卡、联系人往来画像、可选向量嵌入。

抓取/同步邮件后由分析入口调用一次，结果按内容哈希缓存在邮件缓存目录下：
- <地址>-<哈希>.derived.json：信息卡（每封邮件一张）与往来画像
- <地址>-<哈希>.vectors.json：向量嵌入（仅配置嵌入模型时生成）

邮件内容不变则对应派生数据直接复用，不重复调用模型；分析时只读加载，
注入上下文与检索都以派生数据替代原文，原文仅作按需调阅的兜底。
"""
import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

DERIVED_SUFFIX = ".derived.json"
VECTORS_SUFFIX = ".vectors.json"

# 信息卡结构版本：todos 从字符串列表升级为 {text, owner, due} 对象列表。
# 缓存中旧版本卡片视为失效，下次预处理时重新抽取（不重算哈希，只重跑解析）。
CARD_VERSION = 3


def _safe_path(dir_path: str, target: str, suffix: str) -> str:
    safe = re.sub(r"[^\w.@-]", "_", target)
    digest = hashlib.md5(target.encode("utf-8")).hexdigest()[:8]
    return os.path.join(dir_path, f"{safe}-{digest}{suffix}")


def email_hash(email) -> str:
    """单封邮件的内容哈希：信息卡与向量的缓存键，内容变化即重新生成。"""
    h = hashlib.md5()
    h.update(f"{email.date.isoformat()}|{email.subject}|{email.from_addr}\n{email.body}"
             .encode("utf-8"))
    return h.hexdigest()[:16]


def _set_hash(emails: list) -> str:
    """整份邮件列表的内容哈希：画像的新鲜度判据（任一邮件变化即失效）。"""
    h = hashlib.md5(b"profile-v1")
    for e in emails:
        h.update(email_hash(e).encode("ascii"))
    return h.hexdigest()


class DerivedStore:
    """一个目标地址的派生数据文件（信息卡 + 往来画像），原子写。"""

    def __init__(self, dir_path: str, target: str):
        self.path = _safe_path(dir_path, target, DERIVED_SUFFIX)
        self.data = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, self.path)


# ---------- 单封信息卡 ----------

_CARD_INSTRUCTION = """从下面这封邮件中提取关键信息，输出严格的 JSON 对象，字段如下：
- "summary"：一句话概括邮件核心内容（不超过 60 字）
- "intent"：邮件意图，从 询价/报价/订单/合同/交付/催办/会议/通知/咨询/投诉/致谢/闲聊/其他 中选一个
- "entities"：关键实体列表（金额、日期、数量、合同号、产品名、人名等，最多 8 个）
- "todos"：承诺、待办或行动项列表（没有则为空数组，最多 5 条）。每条为对象：
  - "text"：事项内容（不超过 60 字）
  - "owner"：责任方，"me" 表示应由我方（邮箱主人）完成，"them" 表示应由对方完成
  - "due"：截止或承诺日期，格式 YYYY-MM-DD；邮件中未明确日期则为 null
  - "evidence"：支持该事项的邮件正文原句（4-500 字），不得编造或改写
- "quotation"：仅客户询价且我方待处理时提取；否则 null。结构为 {"currency":{"value":"原文币种","evidence":"正文原句"},"tradeTerm":{"value":"原文贸易条款","evidence":"正文原句"},"lines":[{"product":"原文商品名称或编号","quantity":数字,"unit":"原文单位，没有则空字符串","evidence":"同时包含商品及数量的正文原句"}]}。不猜币种、价格、单位或数量；可省略缺失的 currency、tradeTerm，没有明确数量行则 null。
- "procurement"：仅供应商交期/进度陈述，结构为 {"contractCode":{"value":"正文采购合同编号","evidence":"包含编号的正文原句"},"lines":[{"product":"原文商品","progressPercent":数字,"expectedFinishDate":"YYYY-MM-DD","evidence":"包含商品及明确百分比/预计完工日期的正文原句"}]}。只保留原文明示的进度及日期，缺少合同编号则 null；这只是供应商陈述，不能据此认定已完工、已验货。
- "sentiment"：语气，从 积极/中性/消极/强硬 中选一个
只输出 JSON 本身，不要任何解释或 markdown 代码围栏。"""

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.S)
_DUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_todo(item):
    """归一化单条待办：兼容字符串（旧格式）与对象（v2），非法返回 None。"""
    if isinstance(item, str):
        text = item.strip()
        return {"text": text[:60], "owner": "", "due": None} if text else None
    if not isinstance(item, dict):
        return None
    text = str(item.get("text") or "").strip()
    if not text:
        return None
    owner = str(item.get("owner") or "").strip()
    if owner not in ("me", "them"):
        owner = ""
    due = str(item.get("due") or "").strip() or None
    if due and not _DUE_RE.match(due):
        due = None
    return {"text": text[:60], "owner": owner, "due": due,
            "evidence": str(item.get("evidence") or "").strip()[:2000]}


def todo_text(todo) -> str:
    """待办的纯文本形式（画像/向量/展示用），兼容旧字符串格式。"""
    if isinstance(todo, str):
        return todo
    if isinstance(todo, dict):
        return str(todo.get("text") or "")
    return ""


def parse_card(text: str):
    """从模型回复解析信息卡；失败返回 None。"""
    m = _JSON_BLOCK_RE.search(text or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    def str_list(key, cap):
        items = data.get(key)
        if not isinstance(items, list):
            return []
        return [str(x).strip()[:60] for x in items if str(x).strip()][:cap]

    card = {
        "v": CARD_VERSION,
        "summary": str(data.get("summary") or "").strip()[:120],
        "intent": str(data.get("intent") or "").strip()[:20],
        "entities": str_list("entities", 8),
        "todos": [t for t in (_parse_todo(x) for x in
                              (data.get("todos") if isinstance(data.get("todos"), list) else []))
                  if t][:5],
        "sentiment": str(data.get("sentiment") or "").strip()[:10],
    }
    # parse_card 不持有原文，完整候选暂存，build_payload 对实际正文逐项核对。
    for key in ("quotation", "procurement"):
        if isinstance(data.get(key), dict):
            card[key] = data[key]
    return card if card["summary"] else None


def ensure_cards(llm, store: DerivedStore, target: str, emails: list,
                 workers: int = 4, progress=None) -> list:
    """返回与 emails 对齐的信息卡列表（抽取失败的位置为 None）。

    命中缓存的直接复用，缺失的并发抽取；progress(done, total) 回调抛异常
    会中断抽取（用于任务取消），已完成的卡片仍会落盘，下次调用续跑。
    """
    hashes = [email_hash(e) for e in emails]
    table = store.data.setdefault("cards", {})
    cards = [c if isinstance(c, dict) and c.get("v") == CARD_VERSION else None
             for c in (table.get(h) for h in hashes)]
    todo = [(i, emails[i]) for i, c in enumerate(cards) if not c]

    def gen(item):
        i, e = item
        prompt = _CARD_INSTRUCTION + "\n\n" + e.digest(i + 1)
        try:
            return i, parse_card(llm.chat([{"role": "user", "content": prompt}]))
        except Exception:
            return i, None

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [pool.submit(gen, item) for item in todo]
        done = 0
        for fut in as_completed(futures):
            i, card = fut.result()
            done += 1
            if card:
                cards[i] = card
                table[hashes[i]] = card
            if progress:
                progress(done, len(todo))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        keep = set(hashes)
        store.data["cards"] = {h: c for h, c in table.items() if h in keep}
        store.save()
    return cards


# ---------- 联系人往来画像 ----------

_PROFILE_INSTRUCTION = (
    "以下是我的邮箱与 {target} 的往来邮件信息卡（共 {count} 封，按时间升序，每行一封）。"
    "请输出一份不超过 {cap} 字的中文「往来画像」，供后续分析直接引用，包含：\n"
    "1. 关系背景与合作主线；2. 关键承诺与待办台账（保留日期、金额等关键数字）；\n"
    "3. 未决问题与潜在风险；4. 沟通语气与双方关系的变化趋势。\n"
    "分点陈述，直接输出正文，不要标题或解释。"
)

_PROFILE_INPUT_CHARS = 16000  # 画像生成的输入预算：信息卡行超限时掐头留尾


def _profile_line(index: int, email, card) -> str:
    line = f"[{index}] {email.date:%Y-%m-%d} {email.direction} | {email.subject}"
    if card:
        if card.get("summary"):
            line += f" | {card['summary']}"
        if card.get("todos"):
            line += " | 待办: " + "；".join(t for t in (todo_text(x) for x in card["todos"]) if t)
    else:
        line += " | " + re.sub(r"\s+", " ", email.body.strip())[:60]
    return line


def _fit_lines(lines: list, budget: int) -> list:
    """信息卡行总长度超预算时：保留最早 1/4（关系起点）与尽量多的近期行。"""
    if sum(len(l) for l in lines) <= budget:
        return lines
    head_n = max(1, len(lines) // 4)
    head = lines[:head_n]
    used = sum(len(l) for l in head)
    tail = []
    for line in reversed(lines[head_n:]):
        if used + len(line) > budget:
            break
        tail.append(line)
        used += len(line)
    tail.reverse()
    omitted = len(lines) - head_n - len(tail)
    return head + [f"……（中间省略 {omitted} 封）……"] + tail


def ensure_profile(llm, store: DerivedStore, target: str, emails: list,
                   cards: list, cap: int = 800) -> str:
    """往来画像：基于全部信息卡生成，按邮件列表内容哈希缓存；未变化时直接复用。"""
    key = _set_hash(emails)
    cached = store.data.get("profile") or {}
    if cached.get("hash") == key and cached.get("text"):
        return cached["text"]
    lines = _fit_lines([_profile_line(i, e, cards[i - 1] if i - 1 < len(cards) else None)
                        for i, e in enumerate(emails, 1)], _PROFILE_INPUT_CHARS)
    prompt = (_PROFILE_INSTRUCTION.format(target=target, count=len(emails), cap=cap)
              + "\n\n" + "\n".join(lines))
    try:
        text = llm.chat([{"role": "user", "content": prompt}]).strip()
    except Exception:
        return ""
    if text:
        store.data["profile"] = {"hash": key, "text": text}
        store.save()
    return text


# ---------- 向量嵌入（可选，配置嵌入模型时启用） ----------

def _vector_path(dir_path: str, target: str) -> str:
    return _safe_path(dir_path, target, VECTORS_SUFFIX)


def _vector_text(email, card) -> str:
    text = email.subject or ""
    if card and card.get("summary"):
        text += "\n" + card["summary"]
        if card.get("todos"):
            text += "\n" + "；".join(t for t in (todo_text(x) for x in card["todos"]) if t)
    else:
        text += "\n" + email.body[:300]
    return text


def ensure_vectors(llm, model: str, dir_path: str, target: str, emails: list,
                   cards: list, batch_size: int = 32, workers: int = 4,
                   progress=None) -> list:
    """按邮件生成嵌入向量并持久化（缓存键为邮件内容哈希），返回与 emails 对齐的向量列表。

    嵌入文本取「主题 + 信息卡摘要」（无信息卡时为正文开头）。分批并发调用；
    progress 回调抛异常会中断（用于任务取消），已完成部分落盘、下次续跑。
    """
    hashes = [email_hash(e) for e in emails]
    try:
        with open(_vector_path(dir_path, target), encoding="utf-8") as f:
            table = json.load(f)
        if not isinstance(table, dict):
            table = {}
    except (OSError, ValueError):
        table = {}
    vectors = [table.get(h) for h in hashes]
    todo = [i for i, v in enumerate(vectors) if not v]
    chunks = [todo[i:i + batch_size] for i in range(0, len(todo), batch_size)]

    def gen(chunk):
        texts = [_vector_text(emails[i], cards[i] if i < len(cards) else None)
                 for i in chunk]
        try:
            return chunk, llm.embed(texts, model=model)
        except Exception:
            return chunk, None

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [pool.submit(gen, c) for c in chunks]
        done = 0
        for fut in as_completed(futures):
            chunk, vecs = fut.result()
            done += len(chunk)
            if vecs:
                for i, v in zip(chunk, vecs):
                    if v:
                        vectors[i] = v
                        table[hashes[i]] = v
            if progress:
                progress(done, len(todo))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        keep = set(hashes)
        pruned = {h: v for h, v in table.items() if h in keep}
        os.makedirs(dir_path, exist_ok=True)
        path = _vector_path(dir_path, target)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(pruned, f)
        os.replace(tmp, path)
    return vectors


# ---------- 只读加载与清理（分析路径用，不调用模型） ----------

def load_derived(dir_path: str, target: str, emails: list):
    """只读加载派生数据，返回 (cards, profile)，cards 与 emails 对齐；无缓存时为空。"""
    if not dir_path:
        return [], ""
    store = DerivedStore(dir_path, target)
    table = store.data.get("cards") or {}
    cards = [table.get(email_hash(e)) for e in emails]
    cached = store.data.get("profile") or {}
    profile = cached.get("text", "") if cached.get("hash") == _set_hash(emails) else ""
    return cards, profile


def load_vectors(dir_path: str, target: str, emails: list):
    """只读加载向量，与 emails 对齐；无向量文件返回 None。"""
    if not dir_path:
        return None
    try:
        with open(_vector_path(dir_path, target), encoding="utf-8") as f:
            table = json.load(f)
        if not isinstance(table, dict):
            return None
    except (OSError, ValueError):
        return None
    return [table.get(email_hash(e)) for e in emails]


def delete_derived(dir_path: str, target: str) -> None:
    """删除某目标的派生数据文件（信息卡/画像/向量）。"""
    for suffix in (DERIVED_SUFFIX, VECTORS_SUFFIX):
        path = _safe_path(dir_path, target, suffix)
        if os.path.isfile(path):
            os.remove(path)
