import datetime as dt
import json
import tempfile
import unittest
from unittest.mock import patch

from config import Config
from mailer.imap_client import EmailMessage, parse_raw_email
from monitor import MonitorStore
from preprocess import CARD_VERSION, parse_card
from test_zai_sender import ClaimResponse, FakeUsers
from zai_bridge import BridgeError, build_payload, send_payload, source_id, sync_intake


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.mail = EmailMessage(date=dt.datetime(2026, 10, 27, 9, tzinfo=dt.timezone.utc), subject="询价", from_addr="Buyer <buyer@example.com>", to_addrs="seller@example.com", direction="收到", body="Please quote Widget-1, 5 PCS in USD.", message_id="<inquiry@example.com>")
        self.card = {"summary": "询价", "intent": "询价", "todos": [{"text": "准备报价", "owner": "me", "due": "2026-10-28", "evidence": self.mail.body}], "quotation": {"currency": {"value": "USD", "evidence": self.mail.body}, "lines": [{"product": "Widget-1", "quantity": 5, "unit": "PCS", "evidence": self.mail.body}]}}
        self.cfg = Config(zai_api_url="http://127.0.0.1:1", zai_api_key="synthetic-only", zai_intake_enabled=1, zai_intake_users="seller")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MonitorStore(self.temp.name + "/monitor.json")
        self.sent = []

    def payload(self):
        return build_payload("seller", "buyer@example.com", self.mail, self.card)

    def send(self, cfg, payload):
        self.sent.append(payload)
        return [{"itemId": item["itemId"], "documentId": "synthetic-id", "code": "SYNTHETIC", "duplicate": False} for item in payload["items"]]

    def sync(self, emails=None, cards=None):
        sync_intake(self.cfg, FakeUsers(), "seller", "buyer@example.com", emails or [self.mail], cards or [self.card], self.store, self.send)

    def test_payload_has_evidence_but_no_identity_or_permissions(self):
        payload = self.payload()
        self.assertEqual(payload["direction"], "INBOUND")
        self.assertEqual(payload["items"][0]["quotation"]["lines"][0]["quantity"], 5)
        self.assertEqual(payload["items"][0]["evidence"], self.mail.body)
        self.assertNotIn("userId", payload)
        self.assertNotIn("permissions", payload)

    def test_source_id_survives_card_or_body_change_with_message_id(self):
        original = source_id(self.mail)
        self.mail.body = "Changed body"
        self.assertEqual(source_id(self.mail), original)

    def test_unsupported_or_fabricated_evidence_not_submitted(self):
        self.card["todos"][0]["evidence"] = "Missing from actual original mail"
        self.assertIsNone(self.payload())

    def test_legacy_string_todos_never_gain_fabricated_evidence(self):
        self.card["todos"] = ["准备报价"]
        self.assertIsNone(self.payload())

    def test_missing_quantity_keeps_ordinary_review_item(self):
        self.card["quotation"]["lines"][0]["quantity"] = 50
        self.assertNotIn("quotation", self.payload()["items"][0])

    def test_invalid_calendar_date_is_left_missing(self):
        self.card["todos"][0]["due"] = "2026-02-30"
        self.assertIsNone(self.payload()["items"][0]["dueDate"])

    def test_reply_with_no_model_todo_still_requires_human_review(self):
        self.card["todos"] = []
        self.mail.in_reply_to = "<previous@example.com>"
        self.mail.references = "<root@example.com> <previous@example.com>"
        payload = self.payload()
        self.assertEqual(payload["inReplyTo"], self.mail.in_reply_to)
        self.assertEqual(payload["references"], ["<root@example.com>", "<previous@example.com>"])
        self.assertEqual(payload["items"][0]["kind"], "FOLLOW_UP")

    def test_body_truncation_is_never_hidden(self):
        self.mail.body = self.mail.body + "x" * 6000
        self.assertTrue(self.payload()["bodyTruncated"])
        self.assertEqual(len(self.payload()["body"]), 5000)
        self.mail.body = "source …(正文过长已截断)"
        self.card["todos"][0]["evidence"] = "source"
        self.assertTrue(self.payload()["bodyTruncated"])

    def test_procurement_is_only_statement_with_explicit_original_evidence(self):
        self.mail.body = "PC-261027-0001 Widget-1 progress 50%, expected finish 2026-10-30."
        self.card = {"intent": "交付", "todos": [{"text": "核对进度", "owner": "me", "evidence": self.mail.body}], "procurement": {"contractCode": {"value": "PC-261027-0001", "evidence": self.mail.body}, "lines": [{"product": "Widget-1", "progressPercent": 50, "expectedFinishDate": "2026-10-30", "evidence": self.mail.body}]}}
        candidate = self.payload()["items"][0]["procurement"]
        self.assertEqual(candidate["lines"][0]["progressPercent"], 50)
        self.card["procurement"]["lines"][0]["progressPercent"] = 100
        self.assertNotIn("procurement", self.payload()["items"][0])

    def test_first_enable_establishes_baseline_then_only_new_mail(self):
        self.sync()
        self.assertFalse(self.sent)
        self.mail.message_id = "<new@example.com>"
        self.sync()
        self.sync()
        self.assertEqual(len(self.sent), 1)

    def test_disabled_or_unlisted_users_do_not_start_or_retry(self):
        self.cfg.zai_intake_enabled = 0
        self.sync()
        self.assertFalse(self.store._load())
        self.cfg.zai_intake_enabled = 1
        self.cfg.zai_intake_users = "other"
        self.sync()
        self.assertFalse(self.store._load())

    def test_frozen_pending_payload_survives_failure_and_restart(self):
        self.sync()
        self.mail.message_id = "<new@example.com>"
        payload = self.payload()
        def fail(cfg, value):
            self.assertEqual(value, payload)
            raise BridgeError("合成网络中断")
        sync_intake(self.cfg, FakeUsers(), "seller", "buyer@example.com", [self.mail], [self.card], self.store, fail)
        self.store = MonitorStore(self.store.path)
        self.mail.body = "Entirely changed body"
        with patch("monitor._now", return_value=dt.datetime.now() + dt.timedelta(minutes=2)):
            self.sync()
        self.assertEqual(self.sent, [payload])
        self.assertEqual(self.store.pending_zai_intake(), [])
        self.assertEqual(len(self.store.list_events()), 1)

    def test_repeated_failure_deduplicates_exception_event(self):
        self.sync()
        self.mail.message_id = "<new@example.com>"
        def fail(*args):
            raise BridgeError("合成失败")
        for minutes in (0, 2):
            with patch("monitor._now", return_value=dt.datetime.now() + dt.timedelta(minutes=minutes)):
                sync_intake(self.cfg, FakeUsers(), "seller", "buyer@example.com", [self.mail], [self.card], self.store, fail)
        self.assertEqual(len(self.store.list_events()), 1)

    def test_failed_old_request_does_not_starve_later_mail(self):
        self.store.stage_zai_intake("buyer@example.com", [])
        rows = [(str(i), {"index": i}) for i in range(25)]
        self.store.stage_zai_intake("buyer@example.com", rows)
        for key, _ in self.store.pending_zai_intake()[:20]:
            self.store.fail_zai_intake(key, "合成失败")
        with patch("monitor._now", return_value=dt.datetime.now() + dt.timedelta(minutes=2)):
            self.assertEqual(len([value for _, value in self.store.pending_zai_intake()[:5] if value["index"] >= 5]), 5)

    def test_outbound_mail_never_enters_automatic_intake(self):
        self.sync()
        self.mail.message_id, self.mail.direction = "<outbound@example.com>", "发出"
        self.sync()
        self.assertFalse(self.sent)

    def test_card_v3_preserves_original_evidence_and_candidates(self):
        self.assertEqual(CARD_VERSION, 3)
        parsed = parse_card(json.dumps(self.card))
        self.assertEqual(parsed["todos"][0]["evidence"], self.mail.body)
        self.assertEqual(parsed["quotation"], self.card["quotation"])

    def test_send_uses_application_key_without_identity_headers_or_redirects(self):
        payload = self.payload()
        response = ClaimResponse()
        receipt = self.send(self.cfg, payload)
        response.body = json.dumps({"data": {"items": receipt}}).encode()
        with patch("zai_bridge.requests.post", return_value=response) as post:
            self.assertEqual(send_payload(self.cfg, payload), receipt)
            options = post.call_args.kwargs
            self.assertEqual(options["headers"], {"x-api-key": "synthetic-only"})
            self.assertFalse(options["allow_redirects"])

    def test_incomplete_receipt_does_not_acknowledge_delivery(self):
        response = ClaimResponse()
        response.body = b'{"data":{"items":[]}}'
        with patch("zai_bridge.requests.post", return_value=response):
            with self.assertRaises(BridgeError):
                send_payload(self.cfg, self.payload())

    def test_remote_error_body_never_leaks_into_local_exception(self):
        response = ClaimResponse(status=403)
        response.body = b"remote-private-body"
        with patch("zai_bridge.requests.post", return_value=response):
            with self.assertRaises(BridgeError) as error:
                send_payload(self.cfg, self.payload())
        self.assertNotIn("remote-private-body", str(error.exception))

    def test_raw_email_references_are_retained(self):
        raw = b"From: buyer@example.com\r\nTo: seller@example.com\r\nDate: Tue, 27 Oct 2026 09:00:00 +0800\r\nMessage-ID: <new@example.com>\r\nIn-Reply-To: <old@example.com>\r\nReferences: <root@example.com> <old@example.com>\r\n\r\nPlease confirm delivery."
        mail = parse_raw_email(raw, "seller@example.com", "buyer@example.com")
        payload = build_payload("seller", "buyer@example.com", mail, {})
        self.assertEqual(payload["inReplyTo"], "<old@example.com>")
        self.assertEqual(payload["references"], ["<root@example.com>", "<old@example.com>"])


if __name__ == "__main__":
    unittest.main()
