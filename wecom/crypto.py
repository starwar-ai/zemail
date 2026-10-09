"""企业微信回调消息的签名验证与 AES 加解密（WXBizMsgCrypt）。

加密格式（官方文档）：
    明文 = random(16) + msg_len(4字节网络序) + msg + corpid
    AES-256-CBC，key = Base64Decode(EncodingAESKey + "=")，iv = key[:16]
    PKCS7 填充，块大小 32
"""
import base64
import hashlib
import random
import struct
import xml.etree.ElementTree as ET

from Crypto.Cipher import AES


class CryptoError(Exception):
    pass


def sha1_sign(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    items = sorted([token, timestamp, nonce, encrypt])
    return hashlib.sha1("".join(items).encode("utf-8")).hexdigest()


def parse_xml(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)
    return {child.tag: (child.text or "") for child in root}


class WXBizMsgCrypt:
    def __init__(self, token: str, encoding_aes_key: str, corpid: str):
        self.token = token
        self.key = base64.b64decode(encoding_aes_key + "=")
        if len(self.key) != 32:
            raise CryptoError("EncodingAESKey 解码后不是 32 字节，请检查配置")
        self.iv = self.key[:16]
        self.corpid = corpid

    def _encrypt(self, plain: bytes) -> str:
        pad = 32 - len(plain) % 32
        plain += bytes([pad]) * pad
        cipher = AES.new(self.key, AES.MODE_CBC, self.iv)
        return base64.b64encode(cipher.encrypt(plain)).decode("utf-8")

    def _decrypt(self, encrypted: str) -> bytes:
        cipher = AES.new(self.key, AES.MODE_CBC, self.iv)
        plain = cipher.decrypt(base64.b64decode(encrypted))
        pad = plain[-1]
        if pad < 1 or pad > 32:
            raise CryptoError("解密后填充字节非法")
        return plain[:-pad]

    def encrypt_message(self, reply: str, timestamp: str, nonce: str) -> str:
        """加密被动回复消息，返回完整 XML。"""
        msg = reply.encode("utf-8")
        plain = (
            random.randbytes(16)
            + struct.pack(">I", len(msg))
            + msg
            + self.corpid.encode("utf-8")
        )
        encrypt = self._encrypt(plain)
        signature = sha1_sign(self.token, timestamp, nonce, encrypt)
        return (
            "<xml>"
            f"<Encrypt><![CDATA[{encrypt}]]></Encrypt>"
            f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
            f"<TimeStamp>{timestamp}</TimeStamp>"
            f"<Nonce><![CDATA[{nonce}]]></Nonce>"
            "</xml>"
        )

    def _decrypt_to_message(self, signature: str, timestamp: str, nonce: str, encrypt: str) -> str:
        if sha1_sign(self.token, timestamp, nonce, encrypt) != signature:
            raise CryptoError("签名校验失败")
        content = self._decrypt(encrypt)
        if len(content) < 20:
            raise CryptoError("解密内容过短")
        msg_len = struct.unpack(">I", content[16:20])[0]
        msg = content[20:20 + msg_len]
        corpid = content[20 + msg_len:].decode("utf-8")
        if corpid != self.corpid:
            raise CryptoError(f"CorpID 不匹配: {corpid}")
        return msg.decode("utf-8")

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        """GET 验证回调 URL：解密 echostr 并返回明文。"""
        return self._decrypt_to_message(msg_signature, timestamp, nonce, echostr)

    def decrypt_message(self, msg_signature: str, timestamp: str, nonce: str, post_data: str) -> dict:
        """POST 接收消息：验签、解密并解析为 dict。"""
        encrypt = parse_xml(post_data).get("Encrypt", "")
        if not encrypt:
            raise CryptoError("POST 数据中没有 Encrypt 节点")
        plain_xml = self._decrypt_to_message(msg_signature, timestamp, nonce, encrypt)
        return parse_xml(plain_xml)
