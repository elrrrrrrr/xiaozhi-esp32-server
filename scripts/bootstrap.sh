#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$TASK_ROOT"
if ! command -v brew >/dev/null; then
  echo '请先安装 Homebrew：https://brew.sh'
  exit 1
fi
for dependency in uv opus ffmpeg; do
  if ! brew list --versions "$dependency" >/dev/null 2>&1; then
    HOMEBREW_NO_AUTO_UPDATE=1 brew install "$dependency"
  fi
done
if [ ! -x .venv/bin/python ]; then uv venv --python 3.13 .venv; fi
if [ -f requirements-dudu.lock ]; then
  uv pip sync --python .venv/bin/python requirements-dudu.lock
else
  uv pip install --python .venv/bin/python -r requirements-dudu.txt
fi
if [ ! -f .env ]; then cp .env.example .env; chmod 600 .env; fi
.venv/bin/python scripts/download_models.py
echo '环境已准备好。填写 .env 后运行 ./scripts/start.sh；未填 Key 也可以先试听音色。'
