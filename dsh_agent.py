"""DSH（DeepSeek Harness）深度调研。

把会话邮件物化为一个本地工作区（mails/ 每封邮件一个 Markdown + INDEX.md 目录
+ THREADS.md 主题线程 + PROFILE.md 往来画像），然后驱动 DSH agent 在工作区内
用自带的 grep / read / shell 工具反复调查取证，产出标注邮件编号的结论。

依赖 deepseek-harness-sdk（pip 安装，自带 dsh 运行时）；SDK 缺失、启动失败或
运行报错都会以异常抛出，由调用方（Analyzer.investigate_stream）回退到内置的
agent.py 工具循环。
"""
import json
import os
import queue
import re
import shutil
import tempfile
import threading
import uuid

from preprocess import todo_text
from retrieval import catalog_line
from threads import normalize_subject

_AVAILABLE = None


def available() -> bool:
    """DSH Python SDK 是否可导入（结果缓存；运行时故障由调用方捕获后回退）。"""
    global _AVAILABLE
    if _AVAILABLE is None:
        try:
            import deepseek_harness  # noqa: F401
            _AVAILABLE = True
        except Exception:
            _AVAILABLE = False
    return _AVAILABLE


def _safe_dirname(target: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", (target or "").strip()).strip("._")
    return name or "target"


def materialize_workspace(root: str, target: str, emails: list, cards: list = None,
                          threads: list = None, profile: str = "") -> str:
    """把会话邮件物化为 DSH 工作区目录，返回目录路径。重复调用覆盖重写。

    布局：mails/0001.md… 每封邮件全文（含信息卡），INDEX.md 全量目录，
    THREADS.md 主题线程（有线程数据时），PROFILE.md 往来画像（有画像时）。
    文件内容与编号和 agent.py 内置工具、检索补充所使用的邮件编号保持一致。
    """
    workdir = os.path.join(root, _safe_dirname(target))
    shutil.rmtree(workdir, ignore_errors=True)
    mails_dir = os.path.join(workdir, "mails")
    os.makedirs(mails_dir, exist_ok=True)

    index_lines = []
    for i, e in enumerate(emails):
        n = i + 1
        card = cards[i] if cards and i < len(cards) else None
        index_lines.append(catalog_line(e, n, card=card))
        text = e.digest(n)
        if card:
            extra = []
            if card.get("summary"):
                extra.append("摘要: " + str(card["summary"]))
            if card.get("entities"):
                extra.append("实体: " + "、".join(str(x) for x in card["entities"]))
            if card.get("todos"):
                extra.append("待办: " + "；".join(todo_text(x) for x in card["todos"]))
            if extra:
                text += "\n\n【预处理信息卡】\n" + "\n".join(extra)
        with open(os.path.join(mails_dir, f"{n:04d}.md"), "w",
                  encoding="utf-8") as f:
            f.write(text + "\n")

    with open(os.path.join(workdir, "INDEX.md"), "w", encoding="utf-8") as f:
        f.write(f"# 与 {target} 的往来邮件目录（共 {len(emails)} 封，按时间升序）\n\n")
        f.write("\n".join(index_lines) + "\n")

    if threads:
        by_thread = {}
        for i, tid in enumerate(threads):
            by_thread.setdefault(tid, []).append(i)
        lines = []
        for tid, idx in sorted(by_thread.items(),
                               key=lambda kv: emails[kv[1][-1]].date, reverse=True):
            first, last = emails[idx[0]], emails[idx[-1]]
            nums = "、".join(f"[{i + 1}]" for i in idx)
            lines.append(f"- 线程{tid}「{normalize_subject(first.subject)}」"
                         f" {len(idx)} 封，{first.date:%Y-%m-%d} ~ {last.date:%Y-%m-%d}：{nums}")
        with open(os.path.join(workdir, "THREADS.md"), "w", encoding="utf-8") as f:
            f.write("# 主题线程（同一话题的往来邮件分组，编号对应 INDEX.md 与 mails/ 文件名）\n\n")
            f.write("\n".join(lines) + "\n")

    if profile:
        with open(os.path.join(workdir, "PROFILE.md"), "w", encoding="utf-8") as f:
            f.write(f"# 与 {target} 的往来画像\n\n{profile}\n")
    return workdir


def build_task(target: str, question: str) -> str:
    """发给 DSH agent 的调查任务指令。"""
    return f"""你的任务：调查当前工作目录中的邮件档案，回答用户问题。

工作目录内容：
- INDEX.md —— 全部往来邮件目录（编号、日期、方向、主题、信息卡摘要）
- THREADS.md —— 主题线程列表（同一话题的邮件分组，可能不存在）
- PROFILE.md —— 往来画像（可能不存在）
- mails/NNNN.md —— 每封邮件的完整内容（编号与 INDEX.md 一致，四位数字文件名）

调查方法：
1. 先读 INDEX.md 了解全貌，用 grep 在 mails/ 目录按关键词（人名、金额、单号、日期、产品名等）定位相关邮件，再用 read 细读原文；涉及某个话题的完整经过时参考 THREADS.md 把该线程的邮件都读一遍；
2. 每条事实或结论后用 [编号] 标注来源邮件；关键内容（金额、日期、承诺、单号等）尽量用引号摘录原文片段佐证（每段不超过 80 字），例如：[3] …对方确认“一次性验证码为 058259”。重要结论尽量有至少两封邮件或原文细节支撑；
3. 查不到就换关键词、换角度再查，不要臆测；确实查不到就明确说明；
4. 通常需要多轮搜索与阅读，逐步深入，不要急于下结论；
5. 最终答案用中文，条理清晰、分点陈述。

用户问题：{question}"""


def _brief_tool(name: str, arguments) -> str:
    """把一次工具调用渲染成一行进度文本。"""
    try:
        args = json.loads(arguments or "{}")
    except (TypeError, ValueError):
        args = {}
    if not isinstance(args, dict):
        args = {}
    parts = []
    for k, v in args.items():
        s = str(v).replace("\n", " ")
        parts.append(f"{k}={s[:40]}")
    return f"· {name}({', '.join(parts)})"[:120]


def stream_investigation(api_base: str, api_key: str, model: str, workdir: str,
                         dsh_home: str, target: str, question: str):
    """驱动 DSH agent 调查工作区中的邮件。

    生成器协议与 agent.run_agent 一致：产出 ("progress", 文本) 事件，
    最后产出 ("final", 结论文本)；失败抛异常。DSH 的 run() 是阻塞调用，
    放在工作线程中执行，工具调用等进度经队列实时回传给调用方流式输出。
    """
    events = queue.Queue()

    def work():
        try:
            from deepseek_harness import DeepSeekHarness

            def on_notification(n):
                if n.method != "session.event":
                    return
                ev = n.payload.get("event") or {}
                data = ev.get("data") or {}
                if ev.get("type") == "tool/call":
                    events.put(("progress", _brief_tool(data.get("name") or "",
                                                        data.get("arguments"))))
                elif ev.get("type") == "turn/start" and (data.get("turn") or 1) > 1:
                    events.put(("progress", f"· 第 {data['turn']} 轮调查"))

            with DeepSeekHarness(
                dsh_home=dsh_home,
                cwd=workdir,
                provider="deepseek-official",
                model=model,
                api_key=api_key,
                base_url=api_base,
                max_tokens=8192,
            ) as harness:
                result = harness.run(
                    build_task(target, question),
                    session_id="investigate-" + uuid.uuid4().hex[:8],
                    on_notification=on_notification,
                )
            final = (result.final_response or "").strip()
            if not final:
                raise RuntimeError(f"DSH 未产出结论（finish_reason={result.finish_reason}）")
            events.put(("final", final))
        except BaseException as exc:
            events.put(("error", exc))

    threading.Thread(target=work, daemon=True).start()
    while True:
        kind, payload = events.get()
        if kind == "error":
            raise payload
        yield kind, payload
        if kind == "final":
            return


def workspace_root(derived_dir: str) -> str:
    """DSH 相关文件的根目录：有派生数据目录时挂在其下，否则用临时目录。"""
    if derived_dir:
        return os.path.join(derived_dir, "dsh")
    return tempfile.mkdtemp(prefix="zemail-dsh-")
