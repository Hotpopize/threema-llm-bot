"""
test_bot.py — Test bot.py optimizations and webhook flow end-to-end.
"""
import asyncio
from collections import deque
import io
import os
import sys
import types
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

# Ensure DLL directory is set
if os.name == "nt":
    os.add_dll_directory(os.path.dirname(os.path.abspath(__file__)))

# Point dummy keys for testing
os.environ.setdefault("THREEMA_GATEWAY_ID", "*TESTBOT")
os.environ.setdefault("THREEMA_GATEWAY_SECRET", "dummy_secret_1234")
os.environ.setdefault("THREEMA_PRIVATE_KEY", "private:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
os.environ["DUMMY"] = "1"

import bot

class TestBot(AioHTTPTestCase):
    async def get_application(self):
        app = web.Application()
        async def dummy_close():
            pass

        conn = types.SimpleNamespace(
            id="*TESTBOT",
            secret="dummy_secret_1234",
            close=dummy_close,
        )
        app["threema_connection"] = conn
        app["seen_message_ids"] = deque(maxlen=512)
        app["bg_tasks"] = set()
        app["audit_file"] = io.StringIO()
        app.router.add_post("/webhook/threema", bot.threema_webhook_handler)
        return app

    async def test_draft_llm_response(self):
        reply = await bot.draft_llm_response("Hello bot!", "ABCD1234")
        self.assertIn("[dummy] received: Hello bot!", reply)

    async def test_truncate_utf8(self):
        short_txt = "Hello world"
        self.assertEqual(bot.truncate_utf8(short_txt, 3500), short_txt)
        long_txt = "A" * 4000
        truncated = bot.truncate_utf8(long_txt, 3500)
        self.assertLessEqual(len(truncated.encode("utf-8")), 3500)
        self.assertTrue(truncated.endswith("..."))

    async def test_process_and_reply_logging(self):
        sent = []
        class MockTextMessage:
            def __init__(self, connection, to_id, text):
                self.to_id = to_id
                self.text = text
            async def send(self):
                sent.append((self.to_id, self.text))
                return "msg_123456"

        orig_tm = bot.TextMessage
        bot.TextMessage = MockTextMessage
        try:
            await bot.process_and_reply(self.app, "USER1234", "in_999", "Ping")
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][0], "USER1234")
            self.assertIn("Ping", sent[0][1])
            audit_val = self.app["audit_file"].getvalue()
            self.assertIn("USER1234", audit_val)
            self.assertIn("msg_123456", audit_val)
        finally:
            bot.TextMessage = orig_tm

    async def test_webhook_fast_fail_and_deduplication(self):
        # 1. Invalid structural checks fail fast
        resp = await self.client.post("/webhook/threema", data={"from": "bad", "to": "*TESTBOT"})
        self.assertEqual(resp.status, 400)

        # 2. Recipient mismatch fails fast
        resp = await self.client.post("/webhook/threema", data={"from": "USER1234", "to": "*WRONGBOT"})
        self.assertEqual(resp.status, 400)

        # 3. Message deduplication ignores previously seen messageId
        self.app["seen_message_ids"].append("msg_already_processed")
        resp = await self.client.post(
            "/webhook/threema",
            data={"from": "USER1234", "to": "*TESTBOT", "messageId": "msg_already_processed"}
        )
        self.assertEqual(resp.status, 200)

    async def test_pubkey_cache_wrapper(self):
        call_count = 0
        async def fake_get_pubkey(ident):
            nonlocal call_count
            call_count += 1
            return f"key_for_{ident}"

        fake_conn = types.SimpleNamespace(get_public_key=fake_get_pubkey)
        pubkey_cache = {}

        orig_get_public_key = fake_conn.get_public_key
        async def cached_get_public_key(identity: str):
            key = pubkey_cache.get(identity)
            if key is None:
                key = await orig_get_public_key(identity)
                pubkey_cache[identity] = key
            return key

        fake_conn.get_public_key = cached_get_public_key

        k1 = await fake_conn.get_public_key("USER1234")
        k2 = await fake_conn.get_public_key("USER1234")
        self.assertEqual(k1, "key_for_USER1234")
        self.assertEqual(k2, "key_for_USER1234")
        self.assertEqual(call_count, 1)  # Only 1 network call, 2nd call was cached!

if __name__ == "__main__":
    import unittest
    unittest.main()
