"""Start the upstream voice server and the reused Dudu browser UI together."""
import asyncio
import os
import secrets
import shutil
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "main/xiaozhi-server"
sys.path.insert(0, str(SERVER))
os.chdir(SERVER)


def prepare():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
    target = SERVER / "data/.config.yaml"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "config/dudu.local.yaml", target)
    from config.settings import check_config_file
    check_config_file()
    from config.config_loader import load_config
    config = asyncio.run(load_config())
    key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if key.startswith("sk-ws-"):
        raise SystemExit("DASHSCOPE_API_KEY 需要 HTTP API Key；不能直接使用原 Realtime sk-ws Key。")
    llm = config["LLM"][config["selected_module"]["LLM"]]
    llm.update(api_key=key or "not-configured",
               base_url=os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
               model_name=os.getenv("LLM_MODEL", "qwen-flash"))
    config["server"]["port"] = int(os.getenv("DUDU_DEVICE_PORT", "8000"))
    config["server"]["auth_key"] = secrets.token_hex(32)
    config["TTS"][config["selected_module"]["TTS"]]["voice"] = os.getenv("EDGE_TTS_VOICE", "zh-CN-XiaoxiaoNeural")
    if not shutil.which("ffmpeg"):
        raise SystemExit("缺少 ffmpeg，请先运行 brew install ffmpeg opus")
    model_dir = SERVER / config["ASR"][config["selected_module"]["ASR"]]["model_dir"]
    if not all((model_dir / name).is_file() for name in ("model.int8.onnx", "tokens.txt")):
        raise SystemExit("缺少识别模型，请先运行 .venv/bin/python scripts/download_models.py")
    return config, bool(key)


async def run(config, configured, resources):
    from aiohttp import web
    from core.websocket_server import WebSocketServer
    from dudu_web import create_web_app
    server = WebSocketServer(config)
    app = create_web_app(server, configured, config["server"]["port"], resources)
    runner = web.AppRunner(app)
    await runner.setup()
    host = os.getenv("DUDU_HOST", "127.0.0.1")
    port = int(os.getenv("DUDU_PORT", "8080"))
    ws_task = asyncio.create_task(server.start())
    # Surface bind errors before announcing the UI is ready.
    await asyncio.sleep(0.1)
    if ws_task.done():
        await ws_task
    await web.TCPSite(runner, host, port).start()
    print(f"嘟嘟的小电话：http://{host}:{port}", flush=True)
    if not configured:
        print("未配置文本模型 Key：可试听 Edge TTS；配置 .env 后重启即可通话。", flush=True)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    waiter = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait([ws_task, waiter], return_when=asyncio.FIRST_COMPLETED)
        if ws_task in done:
            await ws_task
    finally:
        waiter.cancel()
        ws_task.cancel()
        await asyncio.gather(waiter, ws_task, return_exceptions=True)
        await runner.cleanup()


if __name__ == "__main__":
    from dudu_resources import ResourceMonitor
    resources = ResourceMonitor().start()
    try:
        config, configured = prepare()
        asyncio.run(run(config, configured, resources))
    finally:
        resources.stop()
