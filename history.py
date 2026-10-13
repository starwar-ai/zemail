"""分析会话历史的持久化：按目标地址把问答记录保存为 JSON。"""
import datetime as dt
import hashlib
import json
import os
import re


def _history_path(history_dir: str, target: str) -> str:
    safe = re.sub(r"[^\w.@-]", "_", target)
    digest = hashlib.md5(target.encode("utf-8")).hexdigest()[:8]
    return os.path.join(history_dir, f"{safe}-{digest}.json")


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _load(history_dir: str, target: str) -> dict:
    path = _history_path(history_dir, target)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("sessions"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"target": target, "sessions": []}


def _save(history_dir: str, target: str, data: dict) -> None:
    os.makedirs(history_dir, exist_ok=True)
    path = _history_path(history_dir, target)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def touch_session(history_dir: str, target: str) -> None:
    """登记一次分析（仅建空会话记录，报告尚未生成），让目标立即出现在列表中。

    最后一条会话还没有任何问答时复用该块（仅刷新时间），避免反复抓取堆出大量空块。
    """
    data = _load(history_dir, target)
    if data["sessions"] and not data["sessions"][-1].get("messages"):
        data["sessions"][-1]["started_at"] = _now()
    else:
        data["sessions"].append({"started_at": _now(), "messages": []})
    _save(history_dir, target, data)


def start_session(history_dir: str, target: str, first_question: str, first_reply: str) -> int:
    """一次新的分析开始时，记录首个问题（总体分析指令）与报告。返回报告版本号。"""
    data = _load(history_dir, target)
    data["sessions"].append({
        "started_at": _now(),
        "messages": [
            {"role": "user", "content": first_question, "time": _now()},
            {"role": "assistant", "content": first_reply, "time": _now()},
        ],
    })
    reports = data.setdefault("reports", [])
    version = len(reports) + 1
    reports.append({"version": version, "time": _now(), "content": first_reply})
    _save(history_dir, target, data)
    return version


def add_report(history_dir: str, target: str, content: str, meta: dict = None) -> int:
    """基于当前对话生成的新报告，追加为一个版本，返回版本号。

    meta 记录该版报告的素材来源：email_count、date_range、chat_turns、conversation。
    """
    data = _load(history_dir, target)
    reports = data.setdefault("reports", [])
    version = len(reports) + 1
    entry = {"version": version, "time": _now(), "content": content}
    if meta:
        entry["meta"] = meta
    reports.append(entry)
    _save(history_dir, target, data)
    return version


def get_reports(history_dir: str, target: str) -> list:
    """报告版本列表（按版本号升序）。旧格式文件无 reports 字段时，用首个会话的报告兜底为 v1。"""
    data = _load(history_dir, target)
    reports = data.get("reports") or []
    if not reports:
        for sess in data["sessions"]:
            for m in sess.get("messages") or []:
                if m.get("role") == "assistant":
                    return [{"version": 1,
                             "time": m.get("time") or sess.get("started_at", ""),
                             "content": m.get("content", "")}]
        return []
    return reports


def append_exchange(history_dir: str, target: str, question: str, reply: str,
                    session_index: int = None) -> None:
    """追问时向指定会话块追加一轮问答；缺省写入最近一块。标题由调用方另行设置。"""
    data = _load(history_dir, target)
    if not data["sessions"]:
        data["sessions"].append({"started_at": _now(), "messages": []})
    idx = session_index if isinstance(session_index, int) and \
        0 <= session_index < len(data["sessions"]) else len(data["sessions"]) - 1
    sess = data["sessions"][idx]
    sess.setdefault("messages", []).extend([
        {"role": "user", "content": question, "time": _now()},
        {"role": "assistant", "content": reply, "time": _now()},
    ])
    _save(history_dir, target, data)


def set_title(history_dir: str, target: str, session_index: int, title: str) -> None:
    """设置指定会话块的标题（首轮问答后由 AI 总结生成）。"""
    data = _load(history_dir, target)
    if isinstance(session_index, int) and 0 <= session_index < len(data["sessions"]):
        data["sessions"][session_index]["title"] = title
        _save(history_dir, target, data)


def set_report_title(history_dir: str, target: str, version: int, title: str) -> bool:
    """设置某版报告的名称（AI 总结或用户编辑）。旧格式文件先物化报告列表。"""
    data = _load(history_dir, target)
    reports = data.get("reports")
    if not isinstance(reports, list):
        reports = data["reports"] = get_reports(history_dir, target)
    for r in reports:
        if r.get("version") == version:
            r["title"] = title
            _save(history_dir, target, data)
            return True
    return False


def delete_target(history_dir: str, target: str) -> bool:
    """删除某目标的全部历史与报告。返回是否有文件被删。"""
    path = _history_path(history_dir, target)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def delete_report(history_dir: str, target: str, version: int) -> bool:
    """删除指定版本号的报告，版本号不回填。返回是否删除成功。"""
    data = _load(history_dir, target)
    reports = data.get("reports") or []
    remaining = [r for r in reports if r.get("version") != version]
    if len(remaining) == len(reports):
        return False
    data["reports"] = remaining
    _save(history_dir, target, data)
    return True


def list_targets(history_dir: str) -> list:
    """列出所有有历史记录的目标地址，按最近活跃时间倒序。"""
    result = []
    if not os.path.isdir(history_dir):
        return result
    for name in os.listdir(history_dir):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(history_dir, name), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        sessions = data.get("sessions") or []
        if not sessions:
            continue
        last = sessions[-1]
        messages = last.get("messages") or []
        result.append({
            "target": data.get("target", ""),
            "sessions": len(sessions),
            "updated": messages[-1]["time"] if messages else last.get("started_at", ""),
        })
    result.sort(key=lambda x: x["updated"], reverse=True)
    return result


def get_sessions(history_dir: str, target: str) -> list:
    return _load(history_dir, target)["sessions"]


def new_session(history_dir: str, target: str, indices: list = None,
                email_count: int = 0, date_range: str = "") -> int:
    """强制开启一个新会话块并设为活跃（不复用末尾空块），返回其下标。"""
    data = _load(history_dir, target)
    sess = {"started_at": _now(), "messages": []}
    if indices is not None:
        sess["email_indices"] = indices
    if email_count:
        sess["email_count"] = email_count
    if date_range:
        sess["date_range"] = date_range
    data["sessions"].append(sess)
    idx = len(data["sessions"]) - 1
    data["active_session"] = idx
    _save(history_dir, target, data)
    return idx


def get_active_index(history_dir: str, target: str) -> int:
    """活跃会话块下标；缺省为最后一块。"""
    data = _load(history_dir, target)
    sessions = data["sessions"]
    idx = data.get("active_session")
    if isinstance(idx, int) and 0 <= idx < len(sessions):
        return idx
    return len(sessions) - 1


def set_active_index(history_dir: str, target: str, index: int) -> bool:
    data = _load(history_dir, target)
    if not isinstance(index, int) or not 0 <= index < len(data["sessions"]):
        return False
    data["active_session"] = index
    _save(history_dir, target, data)
    return True


def get_session(history_dir: str, target: str, index: int):
    sessions = _load(history_dir, target)["sessions"]
    if isinstance(index, int) and 0 <= index < len(sessions):
        return sessions[index]
    return None
