"""后台监控：定时同步监控联系人的往来邮件，检测事件并推送企业微信。

事件类型：
- new_mail    监控联系人有新往来邮件（附信息卡摘要）
- unreplied   对方来信超过 N 天我方未回复
- commitment  承诺/待办到期未闭环（台账条目的 due 到期）
- sentiment   对方语气连续两封转消极/强硬

数据按用户存 <monitor_data_dir>/<用户名>.json：监控清单（含轮询状态）、
承诺台账、事件记录。全部原子写。推送走企业微信应用消息；用户未绑定
wecom_userid 或未配置企业微信时只落站内事件。静默时段内跳过整轮检测，
事件在静默结束后的下一轮一并检出并推送。
"""
import datetime as dt
import json
import os
import re
import threading
import uuid
from email.utils import getaddresses

import digest as digest_mod
from mailer.cache import load_emails
from overview import LOW_OBLIGATION_INTENTS
from preprocess import DerivedStore, email_hash, ensure_cards, load_derived
from zai_bridge import sync_intake

WATCH_NAME_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EVENTS_CAP = 100          # 每用户保留的事件条数上限
OPEN_COMMITMENTS_CAP = 200  # 每用户未闭环台账条目上限（超出不再自动收割）
DEFAULT_UNREPLIED_DAYS = 2
BAD_SENTIMENTS = ("消极", "强硬")


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.@-]", "_", name)


def _now() -> dt.datetime:
    return dt.datetime.now()


class MonitorStore:
    """一个用户的监控数据文件（监控清单 + 承诺台账 + 事件记录），原子写。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _save(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def stage_zai_intake(self, target, rows):
        with self._lock:
            data = self._load()
            state = data.setdefault("zai_intake", {})
            baselines = state.setdefault("baselines", {})
            if target not in baselines:
                baselines[target] = [key for key, _ in rows]
            else:
                pending, receipts = state.setdefault("pending", {}), state.setdefault("receipts", {})
                for key, payload in rows:
                    if payload and key not in baselines[target] and key not in pending and key not in receipts:
                        pending[key] = {"payload": payload, "error": "", "nextAttemptAt": ""}
            self._save(data)

    def pending_zai_intake(self):
        pending = (self._load().get("zai_intake") or {}).get("pending") or {}
        now = _now().isoformat()
        rows = sorted(pending.items(), key=lambda item: (item[1].get("lastAttemptAt", ""), item[0]))
        return [(key, row["payload"]) for key, row in rows if not row.get("nextAttemptAt") or row["nextAttemptAt"] <= now]

    def fail_zai_intake(self, key, error):
        with self._lock:
            data = self._load()
            row = ((data.get("zai_intake") or {}).get("pending") or {}).get(key)
            if not row:
                return False
            changed = row.get("error") != error
            row["error"] = error
            row["lastAttemptAt"] = _now().isoformat()
            row["nextAttemptAt"] = (_now() + dt.timedelta(minutes=1)).isoformat()
            self._save(data)
            return changed

    def ack_zai_intake(self, key, receipt):
        with self._lock:
            data = self._load()
            state = data.setdefault("zai_intake", {})
            if state.setdefault("pending", {}).pop(key, None) is not None:
                state.setdefault("receipts", {})[key] = receipt
                self._save(data)

    # ---------- 监控清单 ----------

    def get_watch(self) -> dict:
        return dict(self._load().get("watch") or {})

    def add_watch(self, target: str, unreplied_days: int = DEFAULT_UNREPLIED_DAYS) -> None:
        target = target.strip().lower()
        if not WATCH_NAME_RE.match(target):
            raise ValueError("请提供有效的邮箱地址")
        unreplied_days = max(1, int(unreplied_days))
        with self._lock:
            data = self._load()
            watch = data.setdefault("watch", {})
            entry = watch.setdefault(target, {"state": {}})
            entry["unreplied_days"] = unreplied_days
            entry["created"] = entry.get("created") or _now().isoformat(timespec="seconds")
            self._save(data)

    def remove_watch(self, target: str) -> None:
        target = target.strip().lower()
        with self._lock:
            data = self._load()
            if (data.get("watch") or {}).pop(target, None) is not None:
                self._save(data)

    def update_state(self, target: str, **fields) -> None:
        target = target.strip().lower()
        with self._lock:
            data = self._load()
            entry = (data.setdefault("watch", {})).get(target)
            if entry is None:
                return
            entry.setdefault("state", {}).update(fields)
            entry["state"]["last_check"] = _now().isoformat(timespec="seconds")
            self._save(data)

    # ---------- 承诺台账 ----------

    def list_commitments(self, status: str = None, target: str = None) -> list:
        items = self._load().get("commitments") or []
        if status:
            items = [c for c in items if c.get("status") == status]
        if target:
            items = [c for c in items if c.get("target") == target.strip().lower()]
        return sorted(items, key=lambda c: (c.get("due") or "9999", c.get("created") or ""))

    def add_commitments(self, items: list) -> int:
        """收割台账条目；按 (target, source_hash, text) 去重，返回新增条数。"""
        if not items:
            return 0
        with self._lock:
            data = self._load()
            table = data.setdefault("commitments", [])
            existing = {(c.get("target"), c.get("source_hash"), c.get("text"))
                        for c in table}
            if sum(1 for c in table if c.get("status") == "open") >= OPEN_COMMITMENTS_CAP:
                return 0
            added = 0
            for item in items:
                key = (item.get("target"), item.get("source_hash"), item.get("text"))
                if not item.get("text") or key in existing:
                    continue
                existing.add(key)
                table.append({
                    "id": "c-" + uuid.uuid4().hex[:8],
                    "target": item.get("target"),
                    "text": str(item["text"])[:60],
                    "owner": item.get("owner") or "",
                    "due": item.get("due"),
                    "source_hash": item.get("source_hash"),
                    "source_subject": str(item.get("source_subject") or "")[:120],
                    "source_date": item.get("source_date") or "",
                    "status": "open",
                    "created": _now().isoformat(timespec="seconds"),
                    "reminded_on": [],
                })
                added += 1
            if added:
                self._save(data)
            return added

    def update_commitment(self, cid: str, **fields) -> bool:
        """更新台账条目（status/due/text/owner），条目不存在返回 False。"""
        allowed = {"status", "due", "text", "owner"}
        with self._lock:
            data = self._load()
            for c in data.get("commitments") or []:
                if c.get("id") == cid:
                    for key, value in fields.items():
                        if key in allowed:
                            c[key] = value
                    if fields.get("status") == "open":
                        c["reminded_on"] = []
                    self._save(data)
                    return True
            return False

    def mark_reminded(self, cid: str, day: str) -> None:
        with self._lock:
            data = self._load()
            for c in data.get("commitments") or []:
                if c.get("id") == cid:
                    c.setdefault("reminded_on", []).append(day)
            self._save(data)

    # ---------- 事件记录 ----------

    def add_event(self, kind: str, target: str, title: str, content: str) -> dict:
        with self._lock:
            data = self._load()
            events = data.setdefault("events", [])
            event = {
                "id": "e-" + uuid.uuid4().hex[:8],
                "time": _now().isoformat(timespec="seconds"),
                "kind": kind,
                "target": target,
                "title": title[:120],
                "content": content[:2000],
                "read": False,
            }
            events.append(event)
            data["events"] = events[-EVENTS_CAP:]
            self._save(data)
            return event

    def list_events(self, limit: int = 50) -> list:
        events = self._load().get("events") or []
        return list(reversed(events[-limit:]))

    def unread_count(self) -> int:
        return sum(1 for e in self._load().get("events") or [] if not e.get("read"))

    def mark_all_read(self) -> None:
        with self._lock:
            data = self._load()
            changed = False
            for e in data.get("events") or []:
                if not e.get("read"):
                    e["read"] = True
                    changed = True
            if changed:
                self._save(data)

    def remove_target(self, target: str) -> None:
        """删除目标时连带清理其监控项与台账条目（事件保留作审计）。"""
        target = target.strip().lower()
        with self._lock:
            data = self._load()
            (data.get("watch") or {}).pop(target, None)
            table = data.get("commitments") or []
            data["commitments"] = [c for c in table if c.get("target") != target]
            self._save(data)

    # ---------- 每日简报状态 ----------

    def get_digest_date(self) -> str:
        return str(self._load().get("digest_on") or "")

    def set_digest_date(self, day: str) -> None:
        with self._lock:
            data = self._load()
            data["digest_on"] = day
            self._save(data)

    # ---------- 每周简报状态 ----------

    def get_weekly_digest_week(self) -> str:
        return str(self._load().get("weekly_digest_on") or "")

    def set_weekly_digest_week(self, week: str) -> None:
        with self._lock:
            data = self._load()
            data["weekly_digest_on"] = week
            self._save(data)


class MonitorHub:
    """按用户名管理 MonitorStore 实例（延迟创建，实例缓存）。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._stores = {}
        self._lock = threading.Lock()

    def store(self, username: str) -> MonitorStore:
        with self._lock:
            if username not in self._stores:
                path = os.path.join(self.data_dir, f"{_safe_name(username)}.json")
                self._stores[username] = MonitorStore(path)
            return self._stores[username]

    def watched_usernames(self) -> list:
        """有监控数据文件且监控清单非空的用户名列表。"""
        try:
            names = [n[:-5] for n in os.listdir(self.data_dir) if n.endswith(".json")]
        except OSError:
            return []
        result = []
        for name in names:
            path = os.path.join(self.data_dir, name + ".json")
            store = MonitorStore(path)
            if store.get_watch():
                with self._lock:
                    self._stores.setdefault(name, store)
                result.append(name)
        return result


class Monitor(threading.Thread):
    """监控调度线程：每 monitor_interval_minutes 跑一轮，静默时段跳过。

    依赖均以回调注入，避免与 web.py 循环引用：
    - get_fetcher(username)：按用户当前生效的邮箱设置构建 IMAP 抓取器
    - card_llm：信息卡抽取用的 LLMClient
    - wecom：WeComClient 或 None（未配置企业微信时只落站内事件）
    """

    def __init__(self, cfg, users: "object", hub: MonitorHub,
                 get_fetcher, card_llm, wecom=None, logger=None, resolve=None):
        super().__init__(daemon=True, name="mail-monitor")
        self._cfg = cfg
        self._users = users
        self._hub = hub
        self._get_fetcher = get_fetcher
        self._card_llm = card_llm
        self._wecom = wecom
        self._log = logger or (lambda *a: None)
        self._resolve = resolve or (lambda _u, t: ([t], ""))
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        if self._stop_event.wait(60):  # 启动后等一分钟，让服务先就绪
            return
        while not self._stop_event.is_set():
            try:
                self.run_cycle()
            except Exception as exc:
                self._log(f"监控轮询异常：{exc}")
            interval = max(5, int(self._cfg.monitor_interval_minutes or 30)) * 60
            self._stop_event.wait(interval)

    # ---------- 轮询主流程 ----------

    def _in_quiet_hours(self) -> bool:
        start = int(self._cfg.monitor_quiet_start or 0) % 24
        end = int(self._cfg.monitor_quiet_end or 0) % 24
        if start == end:
            return False
        hour = _now().hour
        return start <= hour < end if start < end else (hour >= start or hour < end)

    def run_cycle(self) -> None:
        if self._in_quiet_hours():
            return
        for username in self._hub.watched_usernames():
            store = self._hub.store(username)
            for target, watch in store.get_watch().items():
                if self._stop_event.is_set():
                    return
                try:
                    self._check_target(username, target, watch, store)
                except Exception as exc:
                    self._log(f"监控检查失败（{username}/{target}）：{exc}")
            try:
                self._sweep_commitments(username, store)
            except Exception as exc:
                self._log(f"承诺催办失败（{username}）：{exc}")
            try:
                self._maybe_digest(username, store)
            except Exception as exc:
                self._log(f"每日简报失败（{username}）：{exc}")
            try:
                self._maybe_weekly_digest(username, store)
            except Exception as exc:
                self._log(f"每周简报失败（{username}）：{exc}")

    def _check_target(self, username: str, target: str, watch: dict,
                      store: MonitorStore) -> None:
        fetcher = self._get_fetcher(username)
        if not (fetcher.host and fetcher.user and fetcher.password):
            return
        addresses, _ = self._resolve(username, target)
        emails = fetcher.fetch_correspondence(addresses)
        if not emails:
            return
        state = watch.get("state") or {}
        hashes = [email_hash(e) for e in emails]
        derived = DerivedStore(fetcher.cache_dir, target)

        if self._cfg.zai_intake_enabled == 1:
            cards = ensure_cards(self._card_llm, derived, target, emails)
            sync_intake(self._cfg, self._users, username, target, emails, cards, store)

        # 首次启用：记录基线并收割存量承诺，不对历史邮件推送
        if "seen" not in state:
            cards = ensure_cards(self._card_llm, derived, target, emails)
            self._harvest(store, target, emails, hashes, cards,
                          range(len(emails)))
            store.update_state(target, seen=hashes)
            return

        seen = set(state.get("seen") or [])
        new_indices = [i for i, h in enumerate(hashes) if h not in seen]
        cards = None
        pushes = []

        if new_indices:
            cards = ensure_cards(self._card_llm, derived, target, emails)
            title, content = self._new_mail_event(target, emails, cards, new_indices)
            store.add_event("new_mail", target, title, content)
            pushes.append(content)
            self._harvest(store, target, emails, hashes, cards, new_indices)
            store.update_state(target, seen=hashes)

        unreplied_days = max(1, int(watch.get("unreplied_days")
                                    or DEFAULT_UNREPLIED_DAYS))
        last = emails[-1]
        if last.direction == "收到":
            age = (_now() - last.date).total_seconds()
            # 致谢/闲聊等低回复义务的来信不提醒（需要信息卡判断意图）
            if cards is None:
                cards = ensure_cards(self._card_llm, derived, target, emails)
            last_card = cards[-1] if cards else None
            low_obligation = last_card \
                and last_card.get("intent") in LOW_OBLIGATION_INTENTS
            if not low_obligation and age >= unreplied_days * 86400 \
                    and state.get("unreplied_notified") != hashes[-1]:
                content = (f"【未回复提醒】{target}\n"
                           f"对方最后一封来信已超过 {unreplied_days} 天未回复：\n"
                           f"主题：{last.subject}\n时间：{last.date:%Y-%m-%d %H:%M}")
                store.add_event("unreplied", target, f"未回复提醒 | {target}", content)
                pushes.append(content)
                store.update_state(target, unreplied_notified=hashes[-1])

        # 对方语气连续恶化：最近两封来信的信息卡均为消极/强硬
        in_indices = [i for i, e in enumerate(emails) if e.direction == "收到"]
        if len(in_indices) >= 2:
            last_two = in_indices[-2:]
            if cards is None:
                cards = ensure_cards(self._card_llm, derived, target, emails)
            pair = [cards[i] if i < len(cards) else None for i in last_two]
            if all(c and c.get("sentiment") in BAD_SENTIMENTS for c in pair):
                marker = "|".join(hashes[i] for i in last_two)
                if state.get("sentiment_notified") != marker:
                    content = (f"【语气预警】{target}\n"
                               f"对方最近两封来信语气均为「{pair[-1]['sentiment']}」"
                               f"（{emails[last_two[0]].date:%m-%d}、"
                               f"{emails[last_two[1]].date:%m-%d}），建议尽快关注处理。")
                    store.add_event("sentiment", target, f"语气预警 | {target}", content)
                    pushes.append(content)
                    store.update_state(target, sentiment_notified=marker)

        for text in pushes:
            self._push(username, text)

    def _new_mail_event(self, target: str, emails: list, cards: list,
                        indices: list) -> tuple:
        lines = [f"【新邮件】{target}（{len(indices)} 封）"]
        for i in indices[-5:]:  # 一封推送最多列 5 封，再多的去看站内事件
            e = emails[i]
            card = cards[i] if i < len(cards) else None
            lines.append(f"\n> {e.date:%m-%d %H:%M} {e.direction} | {e.subject}")
            if card:
                meta = " | ".join(x for x in (card.get("intent"), card.get("sentiment")) if x)
                if meta:
                    lines.append(f"> {meta}")
                if card.get("summary"):
                    lines.append(f"> {card['summary']}")
                todos = [t for t in (card.get("todos") or []) if isinstance(t, dict)]
                for t in todos[:3]:
                    owner = {"me": "我", "them": "对方"}.get(t.get("owner"), "")
                    due = f"（截止 {t['due']}）" if t.get("due") else ""
                    lines.append(f"> ▢ {owner}：{t.get('text')}{due}")
        return f"新邮件 | {target}", "\n".join(lines)

    def _harvest(self, store: MonitorStore, target: str, emails: list,
                 hashes: list, cards: list, indices) -> None:
        """从信息卡的 todos 收割承诺台账条目（自动去重）。"""
        items = []
        for i in indices:
            card = cards[i] if cards and i < len(cards) else None
            if not card:
                continue
            for todo in card.get("todos") or []:
                if not isinstance(todo, dict) or not todo.get("text"):
                    continue
                items.append({
                    "target": target,
                    "text": todo["text"],
                    "owner": todo.get("owner") or "",
                    "due": todo.get("due"),
                    "source_hash": hashes[i],
                    "source_subject": emails[i].subject,
                    "source_date": emails[i].date.isoformat(timespec="seconds"),
                })
        store.add_commitments(items)

    def _sweep_commitments(self, username: str, store: MonitorStore) -> None:
        """到期未闭环的台账条目：每天提醒一次，直到标记完成/忽略。"""
        today = _now().date().isoformat()
        due_items = [c for c in store.list_commitments(status="open")
                     if c.get("due") and c["due"] <= today
                     and today not in (c.get("reminded_on") or [])]
        if not due_items:
            return
        lines = [f"【承诺到期提醒】共 {len(due_items)} 条"]
        for c in due_items[:10]:
            owner = {"me": "我", "them": "对方"}.get(c.get("owner"), "")
            overdue = "（已逾期）" if c["due"] < today else "（今日到期）"
            lines.append(f"▢ {c['target']}｜{owner}：{c['text']}｜{c['due']}{overdue}")
        content = "\n".join(lines)
        store.add_event("commitment", "", f"承诺到期提醒（{len(due_items)} 条）", content)
        self._push(username, content)
        for c in due_items:
            store.mark_reminded(c["id"], today)

    # ---------- 周期性情报摘要（每日/每周简报） ----------

    @staticmethod
    def _display_name(emails: list, target: str) -> str:
        """从往来邮件中提取目标地址的显示名（最新的往前找，没有返回空串）。"""
        for m in reversed(emails):
            for header in (m.from_addr, m.to_addrs):
                for name, addr in getaddresses([header]):
                    if addr.lower() == target and name:
                        return name
        return ""

    def _digest_customers(self, username: str, store: MonitorStore,
                          since: dt.datetime) -> list:
        """时间窗内有新邮件的监控客户：加载缓存邮件与信息卡供 LLM 汇总。"""
        fetcher = self._get_fetcher(username)
        customers = []
        for target in store.get_watch():
            emails = load_emails(fetcher.cache_dir, target)
            if not emails or not any(e.date >= since for e in emails):
                continue
            cards, _ = load_derived(fetcher.cache_dir, target, emails)
            _, contact_name = self._resolve(username, target)
            customers.append({"target": target,
                              "name": contact_name or self._display_name(emails, target),
                              "emails": emails, "cards": cards})
        return customers

    def _unreplied_list(self, username: str, store: MonitorStore) -> list:
        """对方最后来信已超过各自阈值天数未回复的监控客户清单。"""
        fetcher = self._get_fetcher(username)
        out = []
        for target, watch in store.get_watch().items():
            emails = load_emails(fetcher.cache_dir, target)
            if not emails or emails[-1].direction != "收到":
                continue
            days = (_now() - emails[-1].date).days
            threshold = max(1, int(watch.get("unreplied_days")
                                   or DEFAULT_UNREPLIED_DAYS))
            if (_now() - emails[-1].date).total_seconds() >= threshold * 86400:
                out.append({"target": target, "days": days})
        return sorted(out, key=lambda u: -u["days"])

    def _overdue_list(self, store: MonitorStore) -> list:
        today = _now().date().isoformat()
        return [c for c in store.list_commitments(status="open")
                if c.get("due") and c["due"] <= today]

    def _send_digest(self, username: str, store: MonitorStore, since: dt.datetime,
                     title: str, mark) -> None:
        """生成并推送一期简报：优先 LLM 按客户聚合的中文摘要，失败回退规则式。

        没有任何内容时不推送、不占用当天/当周额度（下个周期再试）。
        """
        overdue = self._overdue_list(store)
        unreplied = self._unreplied_list(username, store)
        content = ""
        try:
            customers = self._digest_customers(username, store, since)
            if customers or overdue or unreplied:
                content = digest_mod.generate_report(
                    self._card_llm, customers, since, title,
                    overdue=overdue, unreplied=unreplied)
        except Exception as exc:
            self._log(f"LLM 简报生成失败，回退规则式（{username}）：{exc}")
        if not content:
            content = self._rule_digest(store, title, overdue)
        if not content:
            return
        store.add_event("digest", "", title, content)
        self._push(username, content)
        mark()

    def _rule_digest(self, store: MonitorStore, title: str, overdue: list) -> str:
        """规则式简报（不调用模型）：近 24 小时事件计数 + 到期/逾期承诺。"""
        cutoff = (_now() - dt.timedelta(hours=24)).isoformat(timespec="seconds")
        recent = [e for e in store.list_events(50)
                  if e.get("time", "") >= cutoff and e.get("kind") != "digest"]
        if not recent and not overdue:
            return ""
        today = _now().date().isoformat()
        kind_names = {"new_mail": "新邮件", "unreplied": "未回复提醒",
                      "commitment": "承诺到期", "sentiment": "语气预警"}
        counts = {}
        for e in recent:
            counts[e["kind"]] = counts.get(e["kind"], 0) + 1
        lines = [title]
        if counts:
            lines.append("近 24 小时动态：" + "、".join(
                f"{kind_names.get(k, k)} {v} 条" for k, v in counts.items()))
            for e in recent[:5]:
                lines.append(f"· {kind_names.get(e['kind'], e['kind'])}｜{e['title']}")
        if overdue:
            lines.append(f"\n待闭环承诺（{len(overdue)} 条已到期/逾期）：")
            for c in overdue[:8]:
                owner = {"me": "我", "them": "对方"}.get(c.get("owner"), "")
                tag = "已逾期" if c["due"] < today else "今日到期"
                lines.append(f"▢ {c['target']}｜{owner}：{c['text']}｜{tag}")
        return "\n".join(lines)

    def _maybe_digest(self, username: str, store: MonitorStore) -> None:
        """每日简报：到达 digest_hour 后当天第一轮生成并推送。"""
        hour = int(getattr(self._cfg, "digest_hour", 8))
        if hour < 0 or _now().hour < hour:
            return
        today = _now().date().isoformat()
        if store.get_digest_date() == today:
            return
        since = _now() - dt.timedelta(hours=24)
        self._send_digest(username, store, since,
                          f"【每日客户简报】{_now():%Y-%m-%d}",
                          lambda: store.set_digest_date(today))

    def _maybe_weekly_digest(self, username: str, store: MonitorStore) -> None:
        """每周简报：digest_weekly_day（0=周一）且过了 digest_hour 的当周第一期。"""
        weekday = int(getattr(self._cfg, "digest_weekly_day", -1))
        if weekday < 0 or _now().weekday() != weekday:
            return
        hour = int(getattr(self._cfg, "digest_hour", 8))
        if hour >= 0 and _now().hour < hour:
            return
        week = f"{_now().isocalendar().year}-W{_now().isocalendar().week:02d}"
        if store.get_weekly_digest_week() == week:
            return
        since = _now() - dt.timedelta(days=7)
        self._send_digest(username, store, since,
                          f"【每周客户简报】{_now():%Y-%m-%d}",
                          lambda: store.set_weekly_digest_week(week))

    def _push(self, username: str, text: str) -> None:
        """推送企业微信；未配置企微或用户未绑定 userid 时静默跳过（事件已落站内）。"""
        if not self._wecom:
            return
        userid = self._users.get_wecom_userid(username)
        if not userid:
            return
        try:
            self._wecom.send_markdown(userid, text)
        except Exception as exc:
            self._log(f"企业微信推送失败（{username}）：{exc}")
