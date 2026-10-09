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


def start_session(history_dir: str, target: str, first_question: str, first_reply: str) -> None:
    """一次新的分析开始时，记录首个问题（总体分析指令）与报告。"""
    data = _load(history_dir, target)
    data["sessions"].append({
        "started_at": _now(),
        "messages": [
            {"role": "user", "content": first_question, "time": _now()},
            {"role": "assistant", "content": first_reply, "time": _now()},
        ],
    })
    data.setdefault("reports", []).append({
        "version": len(data.get("reports") or []) + 1,
        "time": _now(),
        "content": first_reply,
    })
    _save(history_dir, target, data)


def add_report(history_dir: str, target: str, content: str) -> int:
    """基于当前对话生成的新报告，追加为一个版本，返回版本号。"""
    data = _load(history_dir, target)
    reports = data.setdefault("reports", [])
    version = len(reports) + 1
    reports.append({"version": version, "time": _now(), "content": content})
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


def append_exchange(history_dir: str, target: str, question: str, reply: str) -> None:
    """追问时向该目标最近的一次会话追加一轮问答。"""
    data = _load(history_dir, target)
    if not data["sessions"]:
        data["sessions"].append({"started_at": _now(), "messages": []})
    data["sessions"][-1]["messages"].extend([
        {"role": "user", "content": question, "time": _now()},
        {"role": "assistant", "content": reply, "time": _now()},
    ])
    _save(history_dir, target, data)


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
