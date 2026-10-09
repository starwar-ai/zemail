"""机器人核心：解析用户指令，串联邮件抓取与 AI 分析。企微服务与本地 CLI 共用。"""
import re

HELP_TEXT = """**邮件分析助手使用说明**
- 发送 `分析 someone@example.com`（或直接发一个邮箱地址）：抓取本邮箱与该地址的往来邮件并生成 AI 分析报告
- 报告生成后，直接发送文字即可围绕这些邮件继续提问
- 发送 `重置`：结束当前会话
- 发送 `帮助`：显示本说明"""

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


class BotCore:
    def __init__(self, fetcher, analyzer):
        self.fetcher = fetcher
        self.analyzer = analyzer

    def handle(self, user: str, text: str) -> list:
        """返回待回复给用户的消息列表。"""
        text = text.strip()
        low = text.lower()
        try:
            if low in ("帮助", "help", "?", "？", "菜单"):
                return [HELP_TEXT]

            if low in ("重置", "reset", "清空"):
                self.analyzer.reset(user)
                return ["已重置会话。发送邮箱地址可开始新的分析。"]

            match = EMAIL_RE.search(text)
            is_analyze_cmd = low.startswith(("分析", "analyze", "整理"))
            if is_analyze_cmd or (match and not self.analyzer.has_session(user)):
                if not match:
                    return ["请提供要分析的邮箱地址，例如：`分析 someone@example.com`"]
                return self._analyze(user, match.group(0))

            if self.analyzer.has_session(user):
                return [self.analyzer.chat(user, text)]

            return ["没有进行中的分析会话。发送一个邮箱地址开始分析，或发送 `帮助` 查看说明。"]
        except Exception as exc:
            return [f"处理失败：{exc}"]

    def _analyze(self, user: str, target: str) -> list:
        emails = self.fetcher.fetch_correspondence(target)
        if not emails:
            return [
                f"未在邮箱中找到与 {target} 的往来邮件。\n"
                "可能原因：超过配置的查找时间范围、目标地址拼写有误，或「已发送」文件夹未被纳入（可在 MAIL_FOLDERS 中配置）。"
            ]
        reply = self.analyzer.start_analysis(user, target, emails)
        header = (
            f"已抓取与 **{target}** 的往来邮件 **{len(emails)}** 封"
            f"（{emails[0].date:%Y-%m-%d} ~ {emails[-1].date:%Y-%m-%d}），AI 分析报告如下："
        )
        return [header, reply]
