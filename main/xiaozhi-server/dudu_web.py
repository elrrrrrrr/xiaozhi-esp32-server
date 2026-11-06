"""Local web adapter around the upstream ESP32 server (not a replacement backend)."""

import asyncio
import base64
import copy
import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlencode

import edge_tts
import opuslib_next
from aiohttp import web, WSMsgType
import websockets

WEB_ROOT = Path(__file__).resolve().parents[1] / "dudu-web"
VOICES = {
    "zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural", "zh-CN-YunxiNeural",
    "zh-CN-YunjianNeural", "zh-CN-YunxiaNeural", "zh-CN-YunyangNeural",
}


class BrowserBridge:
    def __init__(self, ws, backend_url, profiles, sessions, resources=None):
        self.ws = ws
        self.backend_url = backend_url
        self.profiles = profiles
        self.sessions = sessions
        self.resources = resources
        self.device_id = "browser-" + uuid.uuid4().hex
        self.session_id = None
        self.backend = None
        self.receiver = None
        self.response_id = None
        self.pending_text = ""
        self.typed_echo = None
        self.decoder = opuslib_next.Decoder(24000, 1)

    async def emit(self, event):
        if not self.ws.closed:
            await self.ws.send_json(event)

    async def configure(self, session):
        if not isinstance(session, dict):
            raise ValueError("无效会话配置。")
        if self.backend is not None:
            raise ValueError("通话中不能重新配置，请先挂断。")
        voice = session.get("voice", "zh-CN-XiaoxiaoNeural")
        if voice not in VOICES:
            raise ValueError("请选择列表中的 Edge TTS 音色。")
        instructions = str(session.get("instructions", ""))[:8000]
        self.profiles[self.device_id] = {"voice": voice, "instructions": instructions}
        self.backend = await websockets.connect(
            self.backend_url + "?" + urlencode({"device-id": self.device_id}),
            open_timeout=15, max_size=2**20,
        )
        await self.native({
            "type": "hello", "version": 1, "transport": "websocket",
            "features": {"emoji": False},
            "audio_params": {"format": "pcm", "sample_rate": 24000,
                             "channels": 1, "frame_duration": 60},
        })
        hello = json.loads(await asyncio.wait_for(self.backend.recv(), 20))
        if hello.get("type") != "hello":
            raise ValueError("本地语音服务握手失败。")
        self.session_id = hello["session_id"]
        self.sessions[self.session_id] = self
        await self.native({"type": "listen", "state": "start", "mode": "realtime"})
        self.receiver = asyncio.create_task(self.receive())
        await self.emit({"type": "session.updated", "resource_session_id": self.session_id})

    async def native(self, event):
        if self.backend:
            await self.backend.send(json.dumps(event, ensure_ascii=False))

    async def cancel(self):
        old_id, self.response_id = self.response_id, None
        if self.backend:
            await self.native({"type": "abort", "reason": "user_interruption"})
        if old_id:
            await self.emit({"type": "response.done", "response": {
                "id": old_id, "status": "cancelled",
            }})

    async def client_event(self, event):
        if not isinstance(event, dict):
            raise ValueError("无效通话消息。")
        kind = event.get("type")
        if kind == "session.update":
            await self.configure(event.get("session", {}))
        elif kind == "input_audio_buffer.append" and self.backend:
            audio = base64.b64decode(event.get("audio", ""), validate=True)
            if len(audio) > 32000 or len(audio) % 2:
                raise ValueError("无效的麦克风音频帧。")
            await self.backend.send(audio)
        elif kind == "response.cancel":
            await self.cancel()
        elif kind == "conversation.item.create":
            parts = event.get("item", {}).get("content", [])
            self.pending_text = "".join(p.get("text", "") for p in parts
                                        if p.get("type") == "input_text")[:1200]
        elif kind == "response.create" and self.pending_text:
            text, self.pending_text = self.pending_text, ""
            self.typed_echo = text
            await self.native({"type": "listen", "state": "detect", "text": text})

    async def receive(self):
        try:
            async for raw in self.backend:
                if isinstance(raw, bytes):
                    if self.response_id:
                        pcm = self.decoder.decode(raw, 1440)
                        await self.emit({"type": "response.audio.delta",
                            "response_id": self.response_id,
                            "delta": base64.b64encode(pcm).decode()})
                    continue
                event = json.loads(raw)
                kind = event.get("type")
                if kind == "tts":
                    state = event.get("state")
                    if state == "start":
                        self.response_id = uuid.uuid4().hex
                        await self.emit({"type": "response.created",
                                         "response": {"id": self.response_id}})
                    elif state == "sentence_start" and self.response_id and event.get("text"):
                        await self.emit({"type": "response.audio_transcript.delta",
                                         "delta": event["text"]})
                    elif state == "stop" and self.response_id:
                        response_id, self.response_id = self.response_id, None
                        await self.emit({"type": "response.done", "response": {
                            "id": response_id, "status": "completed",
                        }})
                elif kind == "stt" and event.get("text"):
                    if event["text"] != self.typed_echo:
                        await self.emit({"type": "conversation.item.input_audio_transcription.completed",
                            "item_id": uuid.uuid4().hex, "transcript": event["text"]})
                    self.typed_echo = None
                elif kind == "dudu.speech_started":
                    await self.emit({"type": "input_audio_buffer.speech_started"})
                elif kind == "dudu.speech_stopped":
                    await self.emit({"type": "input_audio_buffer.speech_stopped"})
                elif kind == "dudu.error":
                    await self.emit({"type": "app.error", "message": event["message"]})
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.emit({"type": "app.error", "message": "本地语音服务连接中断，请重拨。"})
        finally:
            if not self.ws.closed:
                await self.ws.close()

    async def close(self):
        self.profiles.pop(self.device_id, None)
        if self.session_id:
            self.sessions.pop(self.session_id, None)
        if self.receiver:
            self.receiver.cancel()
            await asyncio.gather(self.receiver, return_exceptions=True)
        if self.backend:
            await self.backend.close()


def create_web_app(server, configured, port, resources=None):
    profiles, sessions = {}, {}
    server.resource_monitor = resources
    original_config = server.get_connection_config

    def connection_config(headers):
        profile = profiles.get(headers.get("device-id"))
        if not profile:
            return original_config(headers)
        config = copy.deepcopy(server.config)
        if profile["instructions"]:
            config["prompt"] = profile["instructions"]
        config["TTS"][config["selected_module"]["TTS"]]["voice"] = profile["voice"]
        return config

    server.get_connection_config = connection_config

    def usage_callback(session_id, usage):
        bridge = sessions.get(session_id)
        if bridge:
            asyncio.run_coroutine_threadsafe(
                bridge.emit({"type": "app.usage", "usage": usage}), loop)

    loop = asyncio.get_running_loop()
    server._llm.usage_callback = usage_callback
    app = web.Application(client_max_size=128 * 1024)

    async def resource_snapshot(request):
        if resources is None:
            raise web.HTTPServiceUnavailable(text="Resource sampling is not enabled")
        return web.json_response(resources.snapshot(
            request.query.get("session_id"), request.query.get("samples") == "1"))

    async def resource_lifecycle(app):
        async def lag_sampler():
            while True:
                target = loop.time() + .5
                await asyncio.sleep(.5)
                resources.event_loop_lag_ms = round(max(0, loop.time() - target) * 1000, 2)

        task = asyncio.create_task(lag_sampler())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    if resources:
        app.cleanup_ctx.append(resource_lifecycle)

    async def health(request):
        return web.json_response({"ready": True, "configured": configured,
            "model": server._llm.model_name, "asr": "SenseVoice Small (local)",
            "tts": "Edge TTS (online)", "device_port": port})

    async def websocket(request):
        # Browsers may only use this loopback adapter from its own origin.
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden(text="Origin mismatch")
        if not configured:
            raise web.HTTPServiceUnavailable(text="请先配置 .env 中的 DASHSCOPE_API_KEY")
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=128 * 1024)
        await ws.prepare(request)
        bridge = BrowserBridge(ws, f"ws://127.0.0.1:{port}/xiaozhi/v1/", profiles, sessions, resources)
        try:
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    try:
                        await bridge.client_event(json.loads(message.data))
                    except (ValueError, KeyError, TypeError, asyncio.TimeoutError):
                        await bridge.emit({"type": "app.error", "message": "通话初始化失败，请检查本地服务配置。"})
                        break
                elif message.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            await bridge.close()
            await ws.close()
        return ws

    async def preview(request):
        voice = request.query.get("voice", "zh-CN-XiaoxiaoNeural")
        if voice not in VOICES:
            raise web.HTTPBadRequest(text="无效音色")
        text = "嘟嘟，你好呀！我在这里听你说，一起出发吧！"
        communicate = edge_tts.Communicate(text, voice=voice, rate="-10%",
                                           proxy=os.getenv("EDGE_TTS_PROXY") or None)
        audio = bytearray()
        preview_id = "preview-" + uuid.uuid4().hex
        if resources:
            resources.begin(preview_id, kind="preview")
        try:
            async with asyncio.timeout(30):
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        audio.extend(chunk["data"])
        except Exception:
            raise web.HTTPBadGateway(text="Edge TTS 暂时不可用，请检查网络或 EDGE_TTS_PROXY。")
        finally:
            if resources:
                resources.end(preview_id)
        return web.Response(body=audio, content_type="audio/mpeg", headers={"Cache-Control": "no-store"})

    async def static(request):
        relative = request.match_info.get("path", "") or "index.html"
        target = (WEB_ROOT / relative).resolve()
        if not target.is_relative_to(WEB_ROOT) or not target.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(target, headers={"Cache-Control": "no-cache"})

    app.router.add_get("/api/health", health)
    app.router.add_get("/api/resources", resource_snapshot)
    app.router.add_get("/api/preview", preview)
    app.router.add_get("/ws", websocket)
    app.router.add_get("/{path:.*}", static)
    return app
