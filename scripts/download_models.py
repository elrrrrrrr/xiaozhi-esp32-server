"""Download the two ONNX ASR files; interrupted downloads can be resumed."""
import argparse
import os
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "main/xiaozhi-server/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
SOURCES = {
    "modelscope": "https://modelscope.cn/models/pengzhendong/sherpa-onnx-sense-voice-zh-en-ja-ko-yue/resolve/master/",
    "huggingface": "https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/",
}


def download(name, base):
    target = TARGET / name
    if target.exists() and target.stat().st_size:
        print(f"Already downloaded: {name}", flush=True)
        return
    partial = target.with_suffix(target.suffix + ".part")
    offset = partial.stat().st_size if partial.exists() else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    request = urllib.request.Request(base + name, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        resume = response.status == 206
        mode = "ab" if resume else "wb"
        done = offset if resume else 0
        expected = done + int(response.headers.get("Content-Length", "0"))
        last_report = done
        with partial.open(mode) as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                done += len(chunk)
                if done - last_report >= 10 * 1024 * 1024:
                    print(f"{name}: {done / 1024**2:.0f} MiB", flush=True)
                    last_report = done
        if done == 0:
            raise RuntimeError(f"Empty download: {name}")
        if expected and done != expected:
            raise RuntimeError(f"Incomplete download: {name}; rerun to resume")
    os.replace(partial, target)
    print(f"Downloaded {name}: {done / 1024**2:.1f} MiB", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=SOURCES, default="modelscope")
    args = parser.parse_args()
    TARGET.mkdir(parents=True, exist_ok=True)
    for filename in ("tokens.txt", "model.int8.onnx"):
        download(filename, SOURCES[args.source])
