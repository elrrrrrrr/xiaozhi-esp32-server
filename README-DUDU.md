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
- 家长设置 → 更多通话设置可查看本机 RSS、CPU、可用内存和事件循环延迟；体验记录包含本次通话的资源统计，也能单独导出资源采样。
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

## 本机资源与 NAS 评估

`GET /api/resources` 返回当前占用、最近 60 秒统计、空闲样本及最多一小时的服务采样统计。
`?session_id=<握手返回的 resource_session_id>` 可读取本次通话平均值、P95 和峰值；加 `&samples=1` 导出原始采样。
主进程及 FFmpeg 子进程每 0.5 秒采样一次，最多保留 7,200 条；最多保留 20 次已结束通话的汇总。
原生 ESP32 连接也按通话记录；音色试听单独标记为任务，不计入空闲基线。

CPU 的 **100% 表示一个逻辑核心**，允许超过 100%；macOS/Linux 也计入已结束并回收的 FFmpeg 子进程 CPU。
RSS 为进程树各进程驻留内存之和，共享页可能重复计算；它不是 macOS 内存占用量或 Docker 的 cgroup 内存用量。
采样峰值是采样间隔内的观测，不能代表更短时间内的瞬时峰值。

运行可复现的资源测试：

```bash
.venv/bin/python scripts/benchmark_resources.py
# 双客户端测试（会并行调用在线 Edge TTS）：
.venv/bin/python scripts/benchmark_resources.py --clients 2 --rounds 3
```

脚本启动独立服务进程，在临时端口测试启动、10 秒空闲、三轮实时语音、10 秒通话后空闲。
麦克风持续每 20ms 发送一帧，AI 播放期间继续发送静音；测试真实 ASR、真实在线 TTS 和最后一轮打断。
文本模型使用本地固定响应，不产生付费模型调用；排除测试客户端和模拟 API 自身的 RSS/CPU。
结果写入 `.runtime/resource-benchmark.json`，日志写入同名 `.log`；都不提交到仓库。

判断 NAS 时主要看加载后 RSS、通话峰值及 ASR 耗时，并为 NAS 系统和其他容器预留内存。
Apple Silicon 的推理速度不能直接换算成 NAS x86/ARM CPU 的速度；应在目标 NAS 用相同脚本复测。
当前安装脚本针对 macOS，NAS 的 Linux 依赖和容器部署需要另外适配。

2026-11-06 本机参考结果：Apple M3、8 核、24 GiB，单客户端三轮语音（最后一轮打断），每 0.5 秒采样。

| 阶段 | RSS 平均 / 峰值 | CPU 平均 / 采样峰值（100%=1 核） |
|---|---|---|
| 加载模型后空闲 10 秒 | 750.6 / 756.3 MiB | 8.38% / 44.73% |
| 三轮实时通话，约 33 秒 | 777.9 / 799.0 MiB | 17.49% / 88.35% |
| 通话后空闲 10 秒 | 776.8 / 784.3 MiB | 5.84% / 7.57% |

三轮 ASR 耗时 113–121ms；检测到说完后首段音频返回 419–685ms，不含 VAD 等待约 1 秒和真实 Qwen 延迟。
进程树 CPU/RSS 包含采样器自身开销；这次短测不能用于证明长时间内存稳定性或目标 NAS 的推理速度。

## 后续 ESP32

上游原生设备入口保留在 `:8000/xiaozhi/v1/`，设备使用 Opus。当前浏览器入口通过桥接发送 PCM。
接入局域网 ESP32 时，将本机 `.config.yaml` 的 `server.ip` 改为 Mac 的局域网地址，设备连接该地址的 8000 端口。
这版默认只监听回环地址，网页也只支持同源连接；如需公网访问，再配置 HTTPS、设备鉴权和入口代理。
当前启动入口不启用 OTA HTTP 服务，设备需要手动配置 WebSocket 地址；后续可接回上游 OTA。

原前端服务和部署均未修改。上游完整功能和部署方式见 [README.md](README.md)。
