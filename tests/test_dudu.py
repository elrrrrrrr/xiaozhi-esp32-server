"""Regression tests for the new browser adapter; no paid API calls."""
import asyncio
import base64
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main/xiaozhi-server"))
from dudu_web import BrowserBridge, create_web_app
import opuslib_next
from aiohttp.test_utils import TestClient, TestServer


class Browser:
    closed = False

    def __init__(self):
        self.events = asyncio.Queue()

    async def send_json(self, event):
        await self.events.put(event)

    async def close(self):
        self.closed = True


class Native:
    def __init__(self):
        self.sent = []
        self.incoming = asyncio.Queue()

    async def send(self, event):
        self.sent.append(event)

    async def recv(self):
        return json.dumps({"type": "hello", "session_id": "test-session"})

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.incoming.get()

    async def close(self):
        pass


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.browser = Browser()
        self.native = Native()
        self.profiles, self.sessions = {}, {}
        self.bridge = BrowserBridge(self.browser, "ws://localhost:8000/", self.profiles, self.sessions)
        with patch("dudu_web.websockets.connect", new=AsyncMock(return_value=self.native)):
            await self.bridge.configure({"voice": "zh-CN-XiaoxiaoNeural", "instructions": "儿童角色"})
        self.assertEqual((await self.browser.events.get())["type"], "session.updated")

    async def asyncTearDown(self):
        await self.bridge.close()

    async def event(self, payload):
        await self.native.incoming.put(json.dumps(payload))
        return await asyncio.wait_for(self.browser.events.get(), 1)

    async def test_handshake_and_microphone_frames(self):
        hello = json.loads(self.native.sent[0])
        self.assertEqual(hello["audio_params"]["format"], "pcm")
        self.assertEqual(self.profiles[self.bridge.device_id]["instructions"], "儿童角色")
        pcm = b"\x00\x00" * 320
        await self.bridge.client_event({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()})
        self.assertEqual(self.native.sent[-1], pcm)

    async def test_audio_transcoding_and_cancel_discards_late_packets(self):
        started = await self.event({"type": "tts", "state": "start"})
        self.assertEqual(started["type"], "response.created")
        encoder = opuslib_next.Encoder(24000, 1, "audio")
        packet = encoder.encode(b"\x00\x00" * 1440, 1440)
        await self.native.incoming.put(packet)
        audio = await asyncio.wait_for(self.browser.events.get(), 1)
        self.assertEqual(len(base64.b64decode(audio["delta"])), 2880)
        await self.bridge.cancel()
        cancelled = await self.browser.events.get()
        self.assertEqual(cancelled["response"]["status"], "cancelled")
        self.assertEqual(json.loads(self.native.sent[-1])["type"], "abort")
        await self.native.incoming.put(packet)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.browser.events.get(), .05)

    async def test_typed_text_is_not_echoed_twice(self):
        await self.bridge.client_event({"type": "conversation.item.create", "item": {
            "content": [{"type": "input_text", "text": "你好"}]}})
        await self.bridge.client_event({"type": "response.create"})
        self.assertEqual(json.loads(self.native.sent[-1])["text"], "你好")
        await self.native.incoming.put(json.dumps({"type": "stt", "text": "你好"}))
        await self.native.incoming.put(json.dumps({"type": "dudu.speech_started"}))
        self.assertEqual((await asyncio.wait_for(self.browser.events.get(), 1))["type"], "input_audio_buffer.speech_started")

    async def test_invalid_audio_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.bridge.client_event({"type": "input_audio_buffer.append", "audio": "AA=="})

    async def test_connection_cleanup(self):
        await self.bridge.close()
        self.assertFalse(self.profiles)
        self.assertFalse(self.sessions)


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_preview_page_and_origin(self):
        server = SimpleNamespace(config={}, _llm=SimpleNamespace(model_name="qwen-flash"),
                                 get_connection_config=lambda headers: {})
        app = create_web_app(server, False, 8000)
        async with TestClient(TestServer(app)) as client:
            health = await client.get("/api/health")
            self.assertFalse((await health.json())["configured"])
            page = await client.get("/")
            self.assertIn("嘟嘟的小电话", await page.text())
            self.assertEqual((await client.get("/.env")).status, 404)
            self.assertEqual((await client.get("/ws", headers={"Origin": "https://example.com"})).status, 403)
            self.assertEqual((await client.get("/ws")).status, 503)


if __name__ == "__main__":
    unittest.main()
