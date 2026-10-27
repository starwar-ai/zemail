"""使用真实应用装配和原监控入口，所有文件位于临时目录且不启动后台线程。"""
import datetime as dt
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from config import Config
from mailer.imap_client import EmailMessage
from monitor import Monitor, MonitorHub, MonitorStore
from test_zai_sender import FakeUsers
from web import create_app
from zai_bridge import sync_intake


class AppWiringTests(unittest.TestCase):
    def test_real_web_app_registers_service_routes_with_default_off(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = Config(users_file=root + "/users.json", secret_key="synthetic-test-only",
                         session_history_dir=root + "/sessions", email_cache_dir=root + "/emails",
                         monitor_data_dir=root + "/monitor", contacts_data_dir=root + "/contacts",
                         email_address="seller@example.com", email_password="synthetic-only",
                         imap_host="synthetic.invalid", llm_api_key="synthetic-only", smtp_host="synthetic.invalid")
            with open(cfg.users_file, "w") as stream:
                json.dump({"seller": {"role": "user", "password_hash": "not-used"}}, stream)
            with patch("web.Monitor.start") as start:
                app = create_app(cfg)
                start.assert_called_once()
            client = app.test_client()
            path = "/api/zai/quotation/account"
            self.assertEqual(client.post(path, json={"sourceUser": "seller"}).status_code, 503)
            cfg.zai_send_token, cfg.zai_send_users = "synthetic-test-only", "seller"
            self.assertEqual(client.post(path, json={"sourceUser": "seller"}).status_code, 401)
            self.assertEqual(client.post(path, json={"sourceUser": "seller"}, headers={"Authorization": "Bearer synthetic-test-only"}).json, {"sender": "seller@example.com"})

    def test_original_monitor_entry_stages_new_mail_and_preserves_original_events(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = Config(zai_intake_enabled=1, zai_intake_users="seller")
            users, store = FakeUsers(), MonitorStore(root + "/monitor.json")
            mail = EmailMessage(date=dt.datetime.now(), subject="合成询价", from_addr="buyer@example.com", to_addrs="seller@example.com", direction="收到", body="Please confirm delivery.", message_id="<baseline@example.com>")
            fetcher = SimpleNamespace(host="synthetic.invalid", user="seller@example.com", password="synthetic-only", cache_dir=root, fetch_correspondence=lambda addresses: [mail])
            card = {"intent": "交付", "todos": [{"text": "核对交期", "owner": "me", "evidence": mail.body}]}
            delivered = []
            def send(config, payload):
                delivered.append(payload)
                return [{"itemId": payload["items"][0]["itemId"], "documentId": "synthetic-id", "code": "SYNTHETIC", "duplicate": False}]
            def intake(config, people, username, target, emails, cards, storage):
                return sync_intake(config, people, username, target, emails, cards, storage, send)
            monitor = Monitor(cfg, users, MonitorHub(root), lambda username: fetcher, None)
            store.add_watch("buyer@example.com")
            with patch("monitor.ensure_cards", return_value=[card]), patch("monitor.sync_intake", side_effect=intake) as sync:
                monitor._check_target("seller", "buyer@example.com", store.get_watch()["buyer@example.com"], store)
                self.assertFalse(delivered)
                mail.message_id, mail.body = "<new@example.com>", "Please confirm updated delivery."
                card["todos"][0]["evidence"] = mail.body
                monitor._check_target("seller", "buyer@example.com", store.get_watch()["buyer@example.com"], store)
                self.assertEqual(sync.call_count, 2)
            self.assertEqual(len(delivered), 1)
            self.assertTrue(any(event["kind"] == "new_mail" for event in store.list_events()))


if __name__ == "__main__":
    unittest.main()
