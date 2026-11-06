# 嘟嘟的小电话：本地低成本测试版

基于 [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) 的 Fork。
保留上游 ESP32 服务端，在旁边增加本地网页适配入口，复用已有“嘟嘟的小电话”的原生 HTML/CSS/ES Modules 前端。

## 启动

```bash
./scripts/bootstrap.sh
# 编辑 .env，填写普通 HTTP DASHSCOPE_API_KEY
./scripts/start.sh
```

打开 <http://127.0.0.1:8080>。使用当前 Mac 的浏览器允许麦克风，即可通话。
未填写文本模型 Key 时，页面会提示等待配置，仍可在家长设置中免费试听 Edge TTS。
修改 `.env` 或配置后需要重启服务。

原页面 URL 里的 `sk-ws-...` 是实时语音专用 Key，本项目不把它当作普通 HTTP Key 使用，也没有保存该凭据。
如使用百炼业务空间的专属文本 API，请同时修改 `LLM_BASE_URL`，使其和 Key 的业务空间、地域匹配。

## 当前链路

```text
网页 AudioWorklet (16 kHz PCM)
  → 本地网页适配器 :8080/ws
  → 上游小智服务端 :8000/xiaozhi/v1/
  → Silero VAD → Sherpa-ONNX / SenseVoice Small → Qwen-Flash → Edge TTS
  → FFmpeg 流式解码 → Opus → 网页适配器转 24 kHz PCM → Web Audio 播放
```

- VAD、ASR 在 Mac 本地 CPU 上运行，不依赖 PyTorch、CUDA 或 Docker。
- Qwen-Flash 是云端文本 API，按 tokens 付费；默认关闭思考，保留五轮历史，最多输出 180 tokens。
- Edge TTS 是在线服务，当前使用无需付费 Key 的社区库；它不是本地离线合成。
- Edge 音频一边接收、一边转码播放；文本模型输出按短句送入合成。
- 持续开麦，VAD 检测到开口即打断播放；旧句子的合成、转码和发送队列会停止。
- 儿童停顿阈值初始为 1 秒，需用真实童声调整；默认依赖浏览器开启回声消除。
- Edge 的接口可用性和音色需要实际测试。`EDGE_TTS_PROXY` 可配置 Edge 专用代理。
- 首音等待、模型返回的 Token 用量和打断次数可在家长设置中查看，体验记录可导出。
- 本地临时 ASR 文件识别完成后删除；不启用持久化聊天记忆或管理后台上报。

## 目录

| 路径 | 内容 |
|---|---|
| `main/dudu-web/` | 从现有部署页面复用的前端及角色图片 |
| `main/xiaozhi-server/dudu_web.py` | 网页协议适配、试听和健康检查 |
| `main/xiaozhi-server/core/providers/tts/edge_stream.py` | 增量 Edge TTS 音频转码、取消 |
| `config/dudu.local.yaml` | 可提交的本地配置模板，不含密钥 |
| `main/xiaozhi-server/data/.config.yaml` | 首次启动生成的本机配置，不提交 |
| `.env` | 本机文本模型凭据，不提交 |
| `scripts/` | 初始化、模型下载、启动 |

模板仅在本机配置不存在时复制；后续请编辑 `main/xiaozhi-server/data/.config.yaml`。
模型文件约 228 MiB，下载后不提交；下载中断后可以重新运行下载脚本续传。

## 验证

```bash
DYLD_LIBRARY_PATH=/opt/homebrew/lib .venv/bin/python -m unittest discover -s tests -v
node --check main/dudu-web/app.js
curl http://127.0.0.1:8080/api/health
# 真实本地 ASR + 真实 Edge TTS + 本地模拟文本 API（不会调用付费模型）：
DYLD_LIBRARY_PATH=/opt/homebrew/lib .venv/bin/python scripts/smoke_audio.py
```

自动测试使用受控的模型/网络替身验证协议、取消和音频转码，不代表真实童声识别率或云端延迟。
真实通话还需要有效的文本 API Key、网络和麦克风。

## 后续 ESP32

上游原生设备入口保留在 `:8000/xiaozhi/v1/`，设备使用 Opus。当前浏览器入口通过桥接发送 PCM。
接入局域网 ESP32 时，将本机 `.config.yaml` 的 `server.ip` 改为 Mac 的局域网地址，设备连接该地址的 8000 端口。
这版默认只监听回环地址，网页也只支持同源连接；如需公网访问，再配置 HTTPS、设备鉴权和入口代理。
当前启动入口不启用 OTA HTTP 服务，设备需要手动配置 WebSocket 地址；后续可接回上游 OTA。

原前端服务和部署均未修改。上游完整功能和部署方式见 [README.md](README.md)。
