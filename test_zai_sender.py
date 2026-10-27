"""隔离发送协议测试；serve_fixture 供 ZAI 的真实 HTTP 跨进程验收使用。"""
import base64
import copy
import hashlib
import json
import os
import smtplib
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, jsonify, request
from werkzeug.serving import make_server

from config import Config
from zai_sender import ZaiSender, build_message, parse_delivery, register_zai_sender


class FakeUsers:
    exists = True
    settings = {}

    def get_role(self, name):
        return "user" if self.exists and name == "seller" else ""

    def get_mail_settings(self, name):
        return self.settings


class MailTransport:
    def __init__(self):
        self.mode = "ACCEPT"
        self.calls = 0
        self.messages = []
        self.archives = []
        self.login_names = []
        self.append_failure = False
        self.close_failure = False
        self.lock = threading.Lock()

    def smtp(self, host, port, **kwargs):
        owner = self

        class SMTP:
            def login(self, user, password):
                owner.login_names.append(user)

            def send_message(self, message, from_addr, to_addrs):
                with owner.lock:
                    owner.calls += 1
                    if owner.mode == "REFUSE":
                        raise smtplib.SMTPRecipientsRefused({to_addrs[0]: (550, b"refused")})
                    if owner.mode == "UNKNOWN":
                        raise smtplib.SMTPServerDisconnected("合成断连")
                    owner.messages.append(message.as_bytes().decode("utf-8"))
                return {}

            def close(self):
                if owner.close_failure:
                    raise OSError("合成关闭失败")
        return SMTP()

    def imap(self, host, port, **kwargs):
        owner = self

        class IMAP:
            def login(self, user, password):
                pass

            def select(self, folder, readonly=False):
                assert readonly
                return "OK", [b"1"]

            def uid(self, operation, *args):
                if operation == "search":
                    return "OK", [b" ".join(str(i + 1).encode() for i in range(len(owner.archives)))]
                raw = owner.archives[int(args[0]) - 1]
                if args[1] == "(RFC822.SIZE)":
                    return "OK", [f"1 (RFC822.SIZE {len(raw)})".encode()]
                assert args[1] == "(BODY.PEEK[])"
                return "OK", [(b"1 (BODY[])", raw)]

            def append(self, folder, flags, date, raw):
                if owner.append_failure:
                    raise OSError("合成归档失败")
                owner.archives.append(raw)
                return "OK", [b"APPENDUID 1 1"]

            def logout(self):
                pass
        return IMAP()


def synthetic_config():
    return Config(zai_send_token="synthetic-send-token", zai_send_users="seller", smtp_host="synthetic.invalid",
                  email_address="seller@example.com", email_password="synthetic-only", imap_host="synthetic.invalid",
                  zai_api_url="http://127.0.0.1:1", zai_api_key="synthetic-only")


class ClaimResponse:
    def __init__(self, claimed=True, status=200):
        self.status_code = status
        self.body = json.dumps({"data": {"claimed": claimed}}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, size):
        yield self.body


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.users, self.mail = synthetic_config(), FakeUsers(), MailTransport()
        self.claim_calls = []
        self.response = ClaimResponse()
        self.adapter = ZaiSender(self.cfg, self.users, self.mail.smtp, self.mail.imap, self.post_claim)
        self.app = Flask(__name__)
        register_zai_sender(self.app, self.cfg, self.users, self.adapter)
        self.client = self.app.test_client()
        self.payload = {"sourceUser": "seller", "sender": "seller@example.com", "recipient": "buyer@example.com",
                        "subject": "合成报价", "body": "已核对的正文\n第二行", "attachments": [{"name": "报价.pdf", "mimeType": "application/pdf", "sha256": hashlib.sha256(b"synthetic").hexdigest(), "content": base64.b64encode(b"synthetic").decode()}]}
        self.data = {"deliveryId": str(uuid.uuid4()), "messageId": f"<zai-{uuid.uuid4()}@example.com>",
                     "payload": json.dumps(self.payload, ensure_ascii=False), "token": "a" * 64}

    def post_claim(self, url, **kwargs):
        self.claim_calls.append((url, kwargs))
        return self.response

    def post(self, path="send", data=None, token="synthetic-send-token"):
        return self.client.post("/api/zai/quotation/" + path, json=data or self.data, headers={"Authorization": "Bearer " + token})

    def verify_data(self):
        return {k: v for k, v in self.data.items() if k != "token"}

    def changed_payload(self, **patch):
        payload = {**self.payload, **patch}
        return {**self.data, "payload": json.dumps(payload, ensure_ascii=False)}

    def test_disabled_by_default(self):
        self.cfg.zai_send_token = ""
        self.assertEqual(self.post().status_code, 503)
        self.assertEqual(self.mail.calls, 0)

    def test_requires_service_token_not_browser_session(self):
        self.assertEqual(self.post(token="wrong").status_code, 401)
        self.assertEqual(self.mail.calls, 0)

    def test_account_uses_existing_user_and_explicit_allowlist(self):
        self.assertEqual(self.post("account", {"sourceUser": "seller"}).json, {"sender": "seller@example.com"})
        self.cfg.zai_send_users = ""
        self.assertEqual(self.post("account", {"sourceUser": "seller"}).status_code, 403)

    def test_deleted_user_cannot_fall_back_to_system_account(self):
        self.users.exists = False
        self.assertEqual(self.post().status_code, 403)
        self.assertFalse(self.claim_calls)

    def test_sender_must_match_current_personal_mailbox(self):
        self.users.settings = {"email_address": "new@example.com"}
        self.assertEqual(self.post().status_code, 409)
        self.assertFalse(self.claim_calls)

    def test_no_extra_envelope_fields(self):
        self.assertEqual(self.post(data={**self.data, "smtpHost": "evil"}).status_code, 400)

    def test_no_header_injection_or_address_lists(self):
        for patch in ({"subject": "x\r\nBcc: hidden@example.com"}, {"sender": "other@example.com"}, {"recipient": "a@example.com,b@example.com"}):
            with self.subTest(patch=patch):
                self.assertIn(self.post(data=self.changed_payload(**patch)).status_code, (400, 409))
        self.assertFalse(self.claim_calls)

    def test_attachment_content_verified_before_claim(self):
        attachments = copy.deepcopy(self.payload["attachments"])
        attachments[0]["content"] = base64.b64encode(b"changed").decode()
        self.assertEqual(self.post(data=self.changed_payload(attachments=attachments)).status_code, 400)
        self.assertFalse(self.claim_calls)

    def test_unknown_fields_duplicate_keys_and_invalid_json_rejected(self):
        for raw in ("null", "{", '{"sourceUser":"seller","sourceUser":"other"}'):
            response = self.client.post("/api/zai/quotation/account", data=raw, content_type="application/json", headers={"Authorization": "Bearer synthetic-send-token"})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.post(data=self.changed_payload(bcc="other@example.com")).status_code, 400)

    def test_malformed_message_id_and_token_rejected_before_claim(self):
        for patch in ({"messageId": '<bad"@example.com>'}, {"deliveryId": "bad"}, {"token": "not-a-token"}):
            self.assertEqual(self.post(data={**self.data, **patch}).status_code, 400)
        self.assertFalse(self.claim_calls)

    def test_claim_uses_exact_payload_hash_and_authenticated_fixed_url(self):
        self.assertEqual(self.post().json["status"], "ACCEPTED")
        url, options = self.claim_calls[0]
        self.assertEqual(url, "http://127.0.0.1:1/api/external/email-operations/delivery-claim")
        self.assertEqual(options["json"], {"deliveryId": self.data["deliveryId"], "token": "a" * 64, "contentHash": hashlib.sha256(self.data["payload"].encode()).hexdigest()})
        self.assertFalse(options["allow_redirects"])
        self.assertEqual(self.mail.calls, 1)

    def test_unknown_claim_response_never_calls_smtp(self):
        self.response = ClaimResponse(None)
        self.assertEqual(self.post().json, {"status": "UNKNOWN", "receipt": ""})
        self.assertEqual(self.mail.calls, 0)

    def test_callback_timeout_never_calls_smtp(self):
        def timeout(*args, **kwargs):
            raise requests.Timeout()
        self.adapter.http_post = timeout
        self.assertEqual(self.post().json["status"], "UNKNOWN")
        self.assertEqual(self.mail.calls, 0)

    def test_callback_rejection_never_calls_smtp(self):
        self.response = ClaimResponse(status=403)
        self.assertEqual(self.post().json["status"], "UNKNOWN")
        self.assertEqual(self.mail.calls, 0)

    def test_insecure_callback_configuration_rejected(self):
        for base in ("http://remote.invalid", "https://user:password@zai.invalid", "https://zai.invalid/?token=x"):
            self.cfg.zai_api_url = base
            self.assertEqual(self.post().status_code, 503)
        self.assertFalse(self.claim_calls)

    def test_duplicate_claim_only_queries_sent_archive(self):
        self.post()
        self.response = ClaimResponse(False)
        self.assertEqual(self.post().json["status"], "ACCEPTED")
        self.assertEqual(self.mail.calls, 1)

    def test_claimed_but_no_archive_remains_unknown(self):
        self.response = ClaimResponse(False)
        self.assertEqual(self.post().json["status"], "UNKNOWN")
        self.assertEqual(self.mail.calls, 0)

    def test_concurrent_claims_send_at_most_once(self):
        lock, seen = threading.Lock(), set()
        def claim(data, digest):
            with lock:
                if data["deliveryId"] in seen:
                    return False
                seen.add(data["deliveryId"])
                return True
        self.adapter.claim = claim
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda _: self.adapter.deliver(self.data, True), range(5)))
        self.assertEqual(self.mail.calls, 1)

    def test_definite_smtp_rejection_is_distinct(self):
        self.mail.mode = "REFUSE"
        self.assertEqual(self.post().json["status"], "REJECTED")
        self.assertEqual(self.mail.calls, 1)
        self.assertFalse(self.mail.archives)

    def test_lost_smtp_result_never_infers_rejection(self):
        self.mail.mode = "UNKNOWN"
        self.assertEqual(self.post().json["status"], "UNKNOWN")
        self.assertEqual(self.mail.calls, 1)

    def test_archive_failure_preserves_received_smtp_acceptance(self):
        self.mail.append_failure = True
        self.assertEqual(self.post().json["status"], "ACCEPTED")
        self.assertEqual(self.post("verify", self.verify_data()).json["status"], "UNKNOWN")
        self.assertEqual(self.mail.calls, 1)

    def test_socket_close_failure_preserves_smtp_acceptance(self):
        self.mail.close_failure = True
        self.assertEqual(self.post().json["status"], "ACCEPTED")

    def test_verify_full_mime_and_never_sends(self):
        self.post()
        self.assertEqual(self.post("verify", self.verify_data()).json["status"], "ACCEPTED")
        self.assertEqual(self.mail.calls, 1)
        self.assertEqual(len(self.claim_calls), 1)

    def test_tampered_archive_cannot_match_only_custom_headers(self):
        self.post()
        payload, digest, content = parse_delivery(self.data, True)
        for change in ({"body": "替换正文"}, {"recipient": "other@example.com"}, {"attachments": []}):
            tampered = {**payload, **change}
            self.mail.archives = [build_message(self.data, tampered, digest, content).as_bytes()]
            self.assertEqual(self.post("verify", self.verify_data()).json["status"], "UNKNOWN")

    def test_send_allowlist_revocation_keeps_verification_available(self):
        self.post()
        self.cfg.zai_send_users = ""
        self.assertEqual(self.post().status_code, 403)
        self.assertEqual(self.post("verify", self.verify_data()).json["status"], "ACCEPTED")
        self.assertEqual(self.mail.calls, 1)


def serve_fixture():
    """真实 HTTP 入口、真实 ZAI 领取回调，仅 SMTP/IMAP 使用内存合成传输。"""
    cfg, users, mail = synthetic_config(), FakeUsers(), MailTransport()
    cfg.zai_api_url, cfg.zai_api_key = os.environ["ZAI_FIXTURE_URL"], os.environ["ZAI_FIXTURE_KEY"]
    app = Flask(__name__)
    adapter = ZaiSender(cfg, users, mail.smtp, mail.imap)
    register_zai_sender(app, cfg, users, adapter)

    @app.route("/fixture", methods=["GET", "POST"])
    def fixture():
        if request.method == "POST":
            mail.mode = request.get_json()["mode"]
        return jsonify({"calls": mail.calls, "messages": mail.messages})

    @app.after_request
    def lost_receipt(response):
        if mail.mode == "LOST" and request.path.endswith("/send") and response.status_code == 200:
            return app.make_response((jsonify({"error": "合成回执丢失"}), 503))
        return response

    server = make_server("127.0.0.1", 0, app, threaded=True)
    print(server.server_port, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    unittest.main()
