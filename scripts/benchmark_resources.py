"""Profile a fresh service process with realtime audio and a local fake LLM API."""
import argparse
import asyncio
import base64
import json
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import socket
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main/xiaozhi-server"))
from dudu_resources import summarize
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def mock_completion(request):
    await request.json()
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await response.prepare(request)
    for text in ("嘟嘟，我听到啦，", "我们一起去看小兔子吧！"):
        chunk = {"id": "benchmark", "object": "chat.completion.chunk", "created": 0,
                 "model": "qwen-flash", "choices": [{"index": 0,
                 "delta": {"content": text}, "finish_reason": None}]}
        await response.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())
    usage = {"id": "benchmark", "object": "chat.completion.chunk", "created": 0,
             "model": "qwen-flash", "choices": [], "usage": {
             "prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}
    await response.write(("data: " + json.dumps(usage) + "\n\ndata: [DONE]\n\n").encode())
    await response.write_eof()
    return response


async def exercise(client, base, pcm, rounds):
    ws = await client.ws_connect(base + "/ws")
    await ws.send_json({"type": "session.update", "session": {
        "voice": "zh-CN-XiaoxiaoNeural", "instructions": "你是儿童AI伙伴，请用普通话简短回答。"}})
    hello = await ws.receive_json(timeout=20)
    if hello.get("type") != "session.updated":
        raise RuntimeError("Call handshake failed")
    pending, events = deque(), asyncio.Queue()

    async def microphone():
        deadline = asyncio.get_running_loop().time()
        while True:
            frame = pending.popleft() if pending else bytes(640)
            await ws.send_json({"type": "input_audio_buffer.append",
                                "audio": base64.b64encode(frame).decode()})
            deadline += .02
            await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))

    async def receive():
        while True:
            event = await ws.receive_json()
            await events.put((time.perf_counter(), event))

    tasks = [asyncio.create_task(microphone()), asyncio.create_task(receive())]
    measurements = []
    try:
        for index in range(rounds):
            frames = [pcm[i:i+640].ljust(640, b"\0") for i in range(0, len(pcm), 640)]
            pending.extend(frames)
            stopped = None
            audio_packets = 0
            result = {"round": index + 1}
            async with asyncio.timeout(60):
                while True:
                    if any(task.done() for task in tasks):
                        for task in tasks:
                            if task.done():
                                task.result()
                    received_at, event = await asyncio.wait_for(events.get(), timeout=30)
                    kind = event.get("type")
                    if kind == "input_audio_buffer.speech_stopped":
                        stopped = received_at
                    elif kind == "conversation.item.input_audio_transcription.completed":
                        if stopped is not None:
                            result["asr_after_stop_ms"] = round((received_at - stopped) * 1000, 2)
                    elif kind == "response.audio.delta":
                        audio_packets += 1
                        if audio_packets == 1 and stopped is not None:
                            result["first_audio_after_stop_ms"] = round((received_at - stopped) * 1000, 2)
                        # Last turn exercises cancellation while audio is still being sent.
                        if index == rounds - 1 and audio_packets == 8:
                            await ws.send_json({"type": "response.cancel"})
                    elif kind == "response.done":
                        result["status"] = event["response"]["status"]
                        result["audio_packets"] = audio_packets
                        if not audio_packets or "asr_after_stop_ms" not in result:
                            raise RuntimeError(f"Incomplete audio round: {result}")
                        measurements.append(result)
                        break
                    elif kind in ("app.error", "error"):
                        raise RuntimeError("Audio service failed: " + str(event.get("message", event.get("error"))))
            await asyncio.sleep(.5)
        return {"session_id": hello["resource_session_id"], "rounds": measurements}
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await ws.close()


async def benchmark(args):
    output = (ROOT / args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    log_path = output.with_suffix(".log")
    source = ROOT / "main/xiaozhi-server/models/SenseVoiceSmall/example/zh.mp3"
    decoder = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-i", str(source), "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    pcm, errors = await decoder.communicate()
    if decoder.returncode:
        raise RuntimeError(errors.decode())
    fake = web.Application()
    fake.router.add_post("/v1/chat/completions", mock_completion)
    phases = {}
    report = {"created_at": datetime.now(timezone.utc).isoformat(),
        "method": {"clients": args.clients, "rounds_per_client": args.rounds,
                   "input_clip_seconds": len(pcm) / 32000, "microphone": "16 kHz PCM, one frame every 20 ms; silence sent while AI speaks",
                   "asr": "real local SenseVoice", "tts": "real online Edge TTS",
                   "llm": "local fixture; no paid model; real Qwen latency is excluded",
                   "sampling_seconds": .5, "idle_seconds_each_phase": args.idle_seconds,
                   "cpu": "100% = one logical core, includes descendant CPU and reaped children on macOS/Linux",
                   "rss": "sum of parent and live children RSS; excludes browser, client driver, mock API, and OS page cache; shared pages can be counted twice"}}
    async with TestServer(fake) as mock, ClientSession() as client:
        web_port, device_port = free_port(), free_port()
        while device_port == web_port:
            device_port = free_port()
        env = dict(os.environ)
        env.update(DUDU_HOST="127.0.0.1", DUDU_PORT=str(web_port), DUDU_DEVICE_PORT=str(device_port),
                   DASHSCOPE_API_KEY="benchmark-only", LLM_MODEL="qwen-flash",
                   LLM_BASE_URL=str(mock.make_url("/v1")),
                   DYLD_LIBRARY_PATH="/opt/homebrew/lib:/usr/local/lib:" + env.get("DYLD_LIBRARY_PATH", ""))
        started = time.time()
        with log_path.open("w") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "scripts/start.py", cwd=ROOT, env=env, stdout=log, stderr=log)
            base = f"http://127.0.0.1:{web_port}"
            try:
                print("Measuring fresh-process startup...", flush=True)
                async with asyncio.timeout(60):
                    while True:
                        if process.returncode is not None:
                            raise RuntimeError(f"Service exited; see {log_path}")
                        try:
                            async with client.get(base + "/api/health") as response:
                                if response.status == 200:
                                    break
                        except OSError:
                            pass
                        await asyncio.sleep(.2)
                phases["startup"] = (started, time.time())
                print("Measuring loaded-model idle baseline...", flush=True)
                before = time.time()
                await asyncio.sleep(args.idle_seconds)
                phases["idle_before"] = (before, time.time())
                print(f"Measuring {args.clients} call(s), {args.rounds} realtime audio rounds each...", flush=True)
                before = time.time()
                report["calls"] = await asyncio.gather(*[
                    exercise(client, base, pcm, args.rounds) for _ in range(args.clients)])
                phases["calls"] = (before, time.time())
                print("Measuring post-call idle baseline...", flush=True)
                before = time.time()
                await asyncio.sleep(args.idle_seconds)
                phases["idle_after"] = (before, time.time())
                async with client.get(base + "/api/resources?samples=1") as response:
                    report["service_resources"] = await response.json()
                report["host"] = report["service_resources"]["host"]
                rows = report["service_resources"]["samples"]
                for call in report["calls"]:
                    async with client.get(base + "/api/resources", params={"session_id": call["session_id"]}) as response:
                        call["resources"] = (await response.json())["session"]
                    if not call["resources"] or not call["resources"]["ended_at"]:
                        raise RuntimeError("Missing completed call resource summary")
                report["phases"] = {}
                for name, (begin, end) in phases.items():
                    report["phases"][name] = {"wall_seconds": round(end - begin, 3),
                        **summarize(s for s in rows if begin <= s["timestamp"] <= end)}
                report["samples"] = rows
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
                print(json.dumps({"host": report["host"], "phases": report["phases"],
                                  "calls": report["calls"], "report": str(output)}, ensure_ascii=False, indent=2), flush=True)
            finally:
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 8)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--clients", type=int, default=1)
    parser.add_argument("--idle-seconds", type=float, default=10)
    parser.add_argument("--output", default=".runtime/resource-benchmark.json")
    args = parser.parse_args()
    if args.rounds < 1 or args.clients < 1 or args.idle_seconds < 2:
        parser.error("rounds/clients must be positive; idle-seconds must be >= 2")
    asyncio.run(benchmark(args))
