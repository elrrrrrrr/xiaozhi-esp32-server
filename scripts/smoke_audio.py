"""Real ASR/Edge TTS + a local fake text API. Does not call a paid model."""
import asyncio
import base64
import copy
import json
import socket
import subprocess
import time
from pathlib import Path

from start import prepare, ROOT


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def smoke(config):
    from aiohttp import web, ClientSession
    from aiohttp.test_utils import TestServer
    from core.websocket_server import WebSocketServer
    from dudu_web import create_web_app

    received = []

    async def completion(request):
        body = await request.json()
        received.append(body)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for text in ("嘟嘟，我听到啦，", "我们一起去看小兔子吧！"):
            chunk = {"id": "smoke", "object": "chat.completion.chunk", "created": 0,
                     "model": "qwen-flash", "choices": [{"index": 0,
                     "delta": {"content": text}, "finish_reason": None}]}
            await response.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())
        chunk = {"id": "smoke", "object": "chat.completion.chunk", "created": 0,
                 "model": "qwen-flash", "choices": [], "usage": {
                 "prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}
        await response.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
        await response.write_eof()
        return response

    fake = web.Application()
    fake.router.add_post("/v1/chat/completions", completion)
    async with TestServer(fake) as mock:
        config = copy.deepcopy(config)
        config["server"]["port"] = free_port()
        llm = config["LLM"][config["selected_module"]["LLM"]]
        llm.update(api_key="smoke-test-only", base_url=str(mock.make_url("/v1")))
        server = WebSocketServer(config)
        app = create_web_app(server, True, config["server"]["port"])
        native = asyncio.create_task(server.start())
        try:
            async with TestServer(app) as site, ClientSession() as client:
                await asyncio.sleep(.1)
                ws = await client.ws_connect(site.make_url("/ws"))
                await ws.send_json({"type": "session.update", "session": {
                    "voice": "zh-CN-XiaoxiaoNeural", "instructions": "你是儿童AI伙伴，请用普通话简短回答。"}})
                hello = await ws.receive_json(timeout=20)
                assert hello["type"] == "session.updated", hello
                incoming = asyncio.Queue()

                async def pump_events():
                    while not ws.closed:
                        event = await ws.receive_json()
                        await incoming.put((time.perf_counter(), event))

                pump = asyncio.create_task(pump_events())
                source = ROOT / "main/xiaozhi-server/models/SenseVoiceSmall/example/zh.mp3"
                pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(source),
                    "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
                    check=True, capture_output=True).stdout
                for i in range(0, len(pcm), 640):
                    await ws.send_json({"type": "input_audio_buffer.append",
                                       "audio": base64.b64encode(pcm[i:i+640]).decode()})
                    await asyncio.sleep(.001)
                for _ in range(80):
                    await ws.send_json({"type": "input_audio_buffer.append",
                                       "audio": base64.b64encode(bytes(640)).decode()})
                    await asyncio.sleep(.02)
                stopped = None
                packets = 0
                metrics = {}
                async with asyncio.timeout(40):
                    while True:
                        received_at, event = await incoming.get()
                        kind = event.get("type")
                        if kind == "input_audio_buffer.speech_stopped":
                            stopped = received_at
                        elif kind == "conversation.item.input_audio_transcription.completed":
                            metrics["recognized"] = event["transcript"]
                        elif kind == "app.usage":
                            assert event["usage"]["total_tokens"] == 30
                            metrics["usage_received"] = True
                        elif kind == "response.audio.delta":
                            assert len(base64.b64decode(event["delta"])) == 2880
                            packets += 1
                            if packets == 1:
                                metrics["first_audio_after_stop_seconds"] = round(received_at - stopped, 3)
                            if packets == 8:
                                await ws.send_json({"type": "response.cancel"})
                        elif kind == "response.done" and event["response"]["status"] == "cancelled":
                            metrics["cancelled"] = True
                            break
                        elif kind == "app.error":
                            raise RuntimeError(event["message"])
                # Once cancellation is acknowledged, stale audio must stay suppressed.
                try:
                    _, event = await asyncio.wait_for(incoming.get(), timeout=.2)
                    assert event.get("type") != "response.audio.delta", event
                except asyncio.TimeoutError:
                    pass
                await ws.close()
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
                assert received and metrics.get("recognized") and metrics.get("usage_received"), metrics
                assert received[0]["messages"][0]["content"].strip() == "你是儿童AI伙伴，请用普通话简短回答。"
                print(json.dumps(metrics, ensure_ascii=False, indent=2), flush=True)
        finally:
            native.cancel()
            await asyncio.gather(native, return_exceptions=True)


if __name__ == "__main__":
    config, _ = prepare()
    # Import upstream providers only after synchronous config preparation.
    from core.websocket_server import WebSocketServer
    asyncio.run(smoke(config))
