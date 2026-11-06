"""Edge TTS audio -> incremental FFmpeg PCM -> upstream Opus playback queue."""
import asyncio
import json
import os

import edge_tts
from core.providers.tts.edge import TTSProvider as EdgeProvider
from core.providers.tts.dto.dto import SentenceType
from core.utils.tts import MarkdownCleaner


class TTSProvider(EdgeProvider):
    def to_tts_stream(self, text, opus_handler=None):
        sentence_id = self.current_sentence_id
        original = text
        text = MarkdownCleaner.clean_markdown(text)
        if self._correct_words_pattern:
            text = self._correct_words_pattern.sub(lambda m: self.correct_words[m.group(0)], text)
        try:
            asyncio.run(self._stream_audio(text, original, sentence_id))
        except Exception:
            if not self.conn.stop_event.is_set():
                asyncio.run_coroutine_threadsafe(self.conn.websocket.send(json.dumps({
                    "type": "dudu.error", "message": "Edge TTS 合成失败，请检查网络或代理后重拨。",
                }, ensure_ascii=False)), self.conn.loop)
            raise

    async def _stream_audio(self, text, original, sentence_id):
        def aborted():
            return (self.conn.client_abort or self.conn.stop_event.is_set()
                    or sentence_id != self.conn.sentence_id)

        if aborted():
            return
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-probesize", "32",
            "-analyzeduration", "0", "-f", "mp3", "-i", "pipe:0",
            "-f", "s16le", "-ac", "1", "-ar", str(self.conn.sample_rate), "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.opus_encoder.reset_state()

        def packet(data):
            if not aborted():
                self.tts_audio_queue.put((SentenceType.MIDDLE, data, None, sentence_id))

        async def write_mp3():
            try:
                communicate = edge_tts.Communicate(
                    text, voice=self.voice, rate=self.edge_rate, volume=self.edge_volume,
                    pitch=self.edge_pitch, proxy=os.getenv("EDGE_TTS_PROXY") or None,
                )
                async for chunk in communicate.stream():
                    if aborted():
                        return
                    if chunk["type"] == "audio":
                        process.stdin.write(chunk["data"])
                        await process.stdin.drain()
            finally:
                process.stdin.close()

        async def read_pcm():
            first = True
            tail = b""
            while data := await process.stdout.read(2880):
                if aborted():
                    return
                if first:
                    self.tts_audio_queue.put((SentenceType.FIRST, None, original, sentence_id))
                    first = False
                data = tail + data
                size = len(data) - len(data) % 2
                tail = data[size:]
                self.opus_encoder.encode_pcm_to_opus_stream(data[:size], False, packet)
            if not aborted():
                self.opus_encoder.encode_pcm_to_opus_stream(b"", True, packet)
                if first:
                    raise RuntimeError("Edge TTS returned no decoded audio")

        async def watch_abort():
            while not aborted():
                await asyncio.sleep(0.05)

        workers = [asyncio.create_task(write_mp3()), asyncio.create_task(read_pcm())]
        work = asyncio.gather(*workers)
        watcher = asyncio.create_task(watch_abort())
        try:
            async with asyncio.timeout(self.tts_timeout):
                done, _ = await asyncio.wait([work, watcher], return_when=asyncio.FIRST_COMPLETED)
                if work in done:
                    await work
                    code = await process.wait()
                    if code and not aborted():
                        raise RuntimeError("FFmpeg failed to decode Edge TTS audio")
        finally:
            watcher.cancel()
            work.cancel()
            for worker in workers:
                worker.cancel()
            if process.returncode is None:
                process.kill()
            await asyncio.gather(watcher, work, *workers, return_exceptions=True)
            await process.wait()
