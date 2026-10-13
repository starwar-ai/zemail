"""客户全景：跨联系人的关系健康度汇总（纯本地计算，不调用模型）。

数据来源：本地邮件缓存 + 预处理信息卡 + 监控台账。对每个联系人输出：
往来规模与最近活跃、未回复状态、未闭环/逾期承诺数、语气趋势、平均回复
速度、往来频率趋势，并合成 0-100 的健康度评分与风险等级。

健康度评分（确定性启发式，每项扣分理由随结果返回，便于前端解释）：
- 对方最后来信超过 unreplied_days 天未回复：-20
- 逾期承诺：每条 -15（合计最多 -30）
- 最近两封对方来信语气均为消极/强硬：-25；仅最近一封：-10
- 平均回复速度较近早期放慢 1.5 倍以上：-10
- 近 30 天往来频率较前 30 天下降过半（且历史邮件 ≥10 封）：-10
等级：<50 高风险，<75 需关注，否则健康。
信息卡覆盖率 <50% 时语气/承诺信号缺失，等级强制为「数据不足」（评分不可信，
不呈现为健康），待分析/监控补齐信号后恢复正常评级。
"""
import datetime as dt

from mailer.cache import load_emails
from preprocess import load_derived

BAD_SENTIMENTS = ("消极", "强硬")

# 低回复义务的意图：致谢/客套、闲聊类来信不视为"等待我方回复"
LOW_OBLIGATION_INTENTS = ("致谢", "闲聊")


def _reply_deltas(emails: list) -> list:
    """我方回复耗时（小时）列表，按时间升序。

    优先按主题线程配对：线程内对方发出一组来信后，我方下一封发出邮件即视为
    回复，耗时从该组第一封来信起算（对方连发多封时等待从首封计）；主动发信
    不在同一线程内，不会污染样本。线程归并失败时退化为相邻「收到→发出」配对。
    """
    try:
        from threads import group_threads
        threads = group_threads(emails)
    except Exception:
        threads = None
    if threads:
        deltas = []
        for indices in threads:
            pending = None  # 线程内等待回复的首封来信下标
            for i in indices:
                e = emails[i]
                if e.direction == "收到":
                    if pending is None:
                        pending = i
                elif pending is not None:
                    hours = (e.date - emails[pending].date).total_seconds() / 3600
                    if 0 <= hours <= 24 * 30:  # 超过 30 天视为无关回复，不计入
                        deltas.append((e.date, hours))
                    pending = None
        return [h for _, h in sorted(deltas)]
    deltas = []
    for a, b in zip(emails, emails[1:]):
        if a.direction == "收到" and b.direction == "发出":
            hours = (b.date - a.date).total_seconds() / 3600
            if 0 <= hours <= 24 * 30:  # 超过 30 天视为无关回复，不计入
                deltas.append(hours)
    return deltas


def _sentiment_flags(cards: list, emails: list) -> tuple:
    """最近对方来信的语气命中：返回 (最近一封差, 最近两封均差, 最近一封语气)。"""
    received = [i for i, e in enumerate(emails) if e.direction == "收到"]
    recent = []
    for i in reversed(received):
        card = cards[i] if i < len(cards) else None
        if card and card.get("sentiment"):
            recent.append(card["sentiment"])
        if len(recent) >= 2:
            break
    if not recent:
        return False, False, ""
    last_bad = recent[0] in BAD_SENTIMENTS
    both_bad = len(recent) >= 2 and all(s in BAD_SENTIMENTS for s in recent[:2])
    return last_bad, both_bad, recent[0]


def contact_overview(target: str, cache_dir: str, commitments: list,
                     unreplied_days: int = 2, name: str = "") -> dict:
    """单个联系人的全景指标；无缓存邮件时返回 None。"""
    emails = load_emails(cache_dir, target)
    if not emails:
        return None
    cards, _ = load_derived(cache_dir, target, emails)
    now = dt.datetime.now()
    last = emails[-1]
    days_since = max(0, (now - last.date).days)

    open_items = [c for c in commitments if c.get("status") == "open"]
    today = now.date().isoformat()
    overdue = [c for c in open_items if c.get("due") and c["due"] < today]

    # 未回复判定：最后一封为对方来信且超过阈值；但信息卡显示该信为致谢/闲聊等
    # 低回复义务意图时不计入（对方客套收尾不需要回复）
    unreplied = False
    if last.direction == "收到" \
            and (now - last.date).total_seconds() >= unreplied_days * 86400:
        last_card = cards[len(emails) - 1] if len(cards) >= len(emails) else None
        if not (last_card and last_card.get("intent") in LOW_OBLIGATION_INTENTS):
            unreplied = True

    # 信息卡覆盖率：对方来信中已生成信息卡的比例。过低时语气/承诺信号缺失，
    # 分数只剩未回复/频率等表层信号，不能呈现为「健康」——风险等级标记为
    # 「数据不足」，待对该联系人运行一次「分析」并加入监控后再纳入正常评分
    received = [i for i, e in enumerate(emails) if e.direction == "收到"]
    covered = sum(1 for i in received if i < len(cards) and cards[i])
    card_coverage = round(covered / len(received), 2) if received else 0.0
    data_limited = card_coverage < 0.5

    last_bad, both_bad, last_sentiment = _sentiment_flags(cards, emails)

    deltas = _reply_deltas(emails)
    reply_avg = round(sum(deltas[-10:]) / len(deltas[-10:]), 1) if deltas else None
    reply_slowed = False
    if len(deltas) >= 6:
        early = sum(deltas[:-5]) / len(deltas[:-5])
        recent = sum(deltas[-5:]) / 5
        reply_slowed = early > 0 and recent > early * 1.5

    half = now - dt.timedelta(days=30)
    half_prev = now - dt.timedelta(days=60)
    freq_recent = sum(1 for e in emails if e.date >= half)
    freq_prev = sum(1 for e in emails if half_prev <= e.date < half)
    freq_dropped = len(emails) >= 10 and freq_prev >= 2 and freq_recent < freq_prev * 0.5

    score, reasons = 100, []
    if unreplied:
        score -= 20
        reasons.append(f"来信已 {days_since} 天未回复")
    if overdue:
        penalty = min(30, 15 * len(overdue))
        score -= penalty
        reasons.append(f"{len(overdue)} 条承诺已逾期")
    if both_bad:
        score -= 25
        reasons.append("对方最近两封来信语气消极/强硬")
    elif last_bad:
        score -= 10
        reasons.append(f"对方最近来信语气「{last_sentiment}」")
    if reply_slowed:
        score -= 10
        reasons.append("平均回复速度明显放慢")
    if freq_dropped:
        score -= 10
        reasons.append("近 30 天往来频率下降过半")
    if data_limited:
        reasons.append(
            f"评分不可信：{len(received)} 封来信中仅 {covered} 封经过 AI 分析，"
            "语气、承诺等信号缺失；对该联系人运行一次「分析」并加入监控后补齐")
    score = max(0, score)
    risk = "high" if score < 50 else ("mid" if score < 75 else "ok")
    if data_limited:
        risk = "insufficient"

    return {
        "target": target,
        "name": name,
        "email_count": len(emails),
        "first_date": emails[0].date.isoformat(timespec="seconds"),
        "last_date": last.date.isoformat(timespec="seconds"),
        "days_since_last": days_since,
        "unreplied": unreplied,
        "open_commitments": len(open_items),
        "overdue_commitments": len(overdue),
        "last_sentiment": last_sentiment,
        "sentiment_worsening": both_bad,
        "reply_hours_avg": reply_avg,
        "freq_last_30d": freq_recent,
        "health": score,
        "risk": risk,
        "risk_reasons": reasons,
        "card_coverage": card_coverage,
        "data_limited": data_limited,
    }


def build_overview(targets: list, cache_dir: str, monitor_store,
                   watch: dict = None, name_of=None) -> list:
    """汇总全部联系人的全景指标，按健康度升序（最需关注的排前）。

    targets：[{target, name?}]；watch：监控清单（提供各自的未回复天数阈值）；
    monitor_store：取承诺台账。无缓存邮件的联系人跳过。
    """
    watch = watch or {}
    commitments = monitor_store.list_commitments() if monitor_store else []
    out = []
    for item in targets:
        target = item["target"]
        entry = watch.get(target) or {}
        days = entry.get("unreplied_days") or 2
        per_target = [c for c in commitments if c.get("target") == target]
        row = contact_overview(target, cache_dir, per_target,
                               unreplied_days=days,
                               name=item.get("name") or "")
        if row:
            row["watched"] = target in watch
            out.append(row)
    # 风险等级优先（数据不足的排在健康之前：评分不可信，不代表没问题）
    order = {"high": 0, "mid": 1, "insufficient": 2, "ok": 3}
    out.sort(key=lambda r: (order.get(r["risk"], 4), r["health"], -r["email_count"]))
    return out
