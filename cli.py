"""本地命令行模式：无需企业微信，直接在终端里测试邮件抓取与 AI 分析。

用法：python cli.py
"""
from bot import HELP_TEXT
from config import load_config
from server import build_bot


def main():
    cfg = load_config()
    bot = build_bot(cfg)
    print("邮件分析助手（本地模式）")
    print(HELP_TEXT)
    print("输入「退出」结束。\n")
    while True:
        try:
            text = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text.lower() in ("退出", "exit", "quit"):
            break
        for reply in bot.handle("cli-user", text):
            print(f"\n助手: {reply}\n")


if __name__ == "__main__":
    main()
