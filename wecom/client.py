"""企业微信 API 客户端：access_token 缓存 + 应用消息推送。"""
import threading
import time

import requests


def split_bytes(text: str, limit: int = 2000) -> list:
    """按 UTF-8 字节数切分长文本（企微单条消息上限 2048 字节），优先在换行处断开。"""
    chunks, current, current_bytes = [], [], 0
    for line in text.splitlines(keepends=True):
        line_bytes = len(line.encode("utf-8"))
        if line_bytes > limit:
            if current:
                chunks.append("".join(current))
                current, current_bytes = [], 0
            for i in range(0, len(line), limit // 2):
                piece = line[i:i + limit // 2]
                while len(piece.encode("utf-8")) > limit:
                    piece = piece[:-1]
                chunks.append(piece)
            continue
        if current_bytes + line_bytes > limit and current:
            chunks.append("".join(current))
            current, current_bytes = [], 0
        current.append(line)
        current_bytes += line_bytes
    if current:
        chunks.append("".join(current))
    return chunks or [""]


class WeComClient:
    def __init__(self, corpid: str, secret: str, agentid: int,
                 api_base: str = "https://qyapi.weixin.qq.com"):
        self.corpid = corpid
        self.secret = secret
        self.agentid = agentid
        self.api_base = api_base
        self._token = None
        self._token_expire = 0.0
        self._lock = threading.Lock()

    def get_access_token(self) -> str:
        with self._lock:
            now = time.time()
            if self._token and now < self._token_expire:
                return self._token
            resp = requests.get(
                f"{self.api_base}/cgi-bin/gettoken",
                params={"corpid": self.corpid, "corpsecret": self.secret},
                timeout=10,
            )
            data = resp.json()
            if data.get("errcode"):
                raise RuntimeError(f"获取 access_token 失败: {data}")
            self._token = data["access_token"]
            self._token_expire = now + int(data.get("expires_in", 7200)) - 300
            return self._token

    def _send(self, touser: str, payload: dict) -> dict:
        payload.update({"touser": touser, "agentid": self.agentid})
        resp = requests.post(
            f"{self.api_base}/cgi-bin/message/send",
            params={"access_token": self.get_access_token()},
            json=payload,
            timeout=10,
        )
        data = resp.json()
        if data.get("errcode"):
            raise RuntimeError(f"发送消息失败: {data}")
        return data

    def send_text(self, touser: str, content: str) -> None:
        for chunk in split_bytes(content):
            self._send(touser, {"msgtype": "text", "text": {"content": chunk}})

    def send_markdown(self, touser: str, content: str) -> None:
        for chunk in split_bytes(content):
            self._send(touser, {"msgtype": "markdown", "markdown": {"content": chunk}})
