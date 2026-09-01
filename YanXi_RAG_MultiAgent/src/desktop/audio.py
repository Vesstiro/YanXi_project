"""Lazy audio adapters. No model download or microphone access at GUI startup."""
import asyncio
import os
import shutil
import time
import uuid
import wave
from pathlib import Path

from src.desktop.settings import ROOT

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".wma", ".mp4", ".webm", ".aiff"}


def collect_audio(paths):
    """Expand folders (not recursively), filter supported types, preserve order, deduplicate."""
    found, seen = [], set()
    for value in paths:
        path = Path(value)
        candidates = sorted(path.iterdir()) if path.is_dir() else [path]
        for candidate in candidates:
            if candidate.is_file() and candidate.suffix.lower() in AUDIO_EXTENSIONS:
                full = str(candidate.resolve())
                identity = full.casefold()
                if identity not in seen:
                    found.append(full)
                    seen.add(identity)
    return found


class LocalTranscriber:
    def __init__(self, model="turbo", engine="whisper"):
        self.size = model
        self.engine = engine
        self.model = None
        self.cache = ROOT / "models" / "whisper"

    def original_cache_path(self):
        filename = "large-v3-turbo" if self.size == "turbo" else self.size
        return Path.home() / ".cache" / "whisper" / f"{filename}.pt"

    def is_cached(self):
        if self.model is not None:
            return True
        if self.engine == "whisper":
            return self.original_cache_path().is_file()
        aliases = (self.size, "large-v3-turbo") if self.size == "turbo" else (self.size,)
        return any(next(self.cache.glob(f"models--*--faster-whisper-{alias}/snapshots/*/model.bin"), None)
                   for alias in aliases)

    def transcribe(self, path, progress, allow_download=False):
        if self.engine == "whisper":
            return self._original_whisper(path, progress, allow_download)
        if self.model is None:
            progress("加载识别模型" if self.is_cached() else "下载并加载识别模型")
            from faster_whisper import WhisperModel
            self.model = WhisperModel(self.size, device="cpu", compute_type="int8",
                download_root=str(self.cache), local_files_only=not allow_download, cpu_threads=4)
        progress("正在转写音频")
        segments, info = self.model.transcribe(str(path), language="zh", beam_size=5, vad_filter=True)
        parts = []
        for segment in segments:
            parts.append(segment.text.strip())
            progress(f"转写中 {min(segment.end, info.duration):.0f} / {info.duration:.0f} 秒")
        text = "".join(parts).strip()
        if not text:
            raise ValueError("未识别到有效语音。请检查录音音量、语言或文件内容。")
        return text

    def _original_whisper(self, path, progress, allow_download):
        # Same ASR parameters as PythonProject2/call_agent.py; no cross-project imports.
        if not shutil.which("ffmpeg"):
            bundled = Path.home() / "AppData/Local/ffmpegio/ffmpeg-downloader/ffmpeg/bin"
            if (bundled / "ffmpeg.exe").is_file():
                os.environ["PATH"] = str(bundled) + os.pathsep + os.environ.get("PATH", "")
            else:
                raise RuntimeError("Whisper 需要 FFmpeg。请安装并加入 PATH，或在设置中切换 Faster-Whisper。")
        if self.model is None:
            if not self.is_cached() and not allow_download:
                raise RuntimeError("识别模型未缓存，请允许首次下载后重试。")
            progress("加载 Whisper 本地模型（首次加载较慢）")
            import whisper
            # Loading an explicit cached path cannot silently trigger a re-download.
            cached = self.original_cache_path()
            self.model = whisper.load_model(str(cached) if cached.is_file() else self.size)
        progress("Whisper 正在识别中文（长录音需要更多时间）")
        result = self.model.transcribe(str(path), language="zh", fp16=False, temperature=0.0,
            initial_prompt="这是一通电话的通话录音，内容是外卖、快递、诈骗推销相关的中文句子。")
        text = result["text"].strip()
        if not text:
            raise ValueError("未识别到有效语音，请检查录音内容和音量。")
        return text


class MicrophoneRecorder:
    @staticmethod
    def devices():
        import sounddevice as sd
        return [(i, f"[{i}] {info['name']} · {sd.query_hostapis(info['hostapi'])['name']}")
                for i, info in enumerate(sd.query_devices()) if info["max_input_channels"] > 0]

    def record(self, stop, progress, device=None, max_seconds=600):
        import numpy as np
        import sounddevice as sd
        directory = ROOT / "data" / "desktop" / "recordings"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"mic_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.wav"
        count = 0
        try:
            info = sd.query_devices(device, "input")
            rate = 16000
            try:
                sd.check_input_settings(device=device, samplerate=rate, channels=1, dtype="int16")
            except sd.PortAudioError:
                rate = int(info["default_samplerate"])
            with sd.RawInputStream(device=device, samplerate=rate, channels=1, dtype="int16", blocksize=1024) as stream, wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                started, last_update = time.monotonic(), 0.0
                while not stop.is_set() and time.monotonic() - started < max_seconds:
                    data, overflowed = stream.read(1024)
                    if overflowed:
                        raise RuntimeError("麦克风采集溢出，请关闭高负载程序后重试。")
                    chunk = bytes(data)
                    wav.writeframesraw(chunk)
                    count += len(chunk) // 2
                    elapsed = time.monotonic() - started
                    if elapsed - last_update >= 0.10:
                        samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
                        level = min(100, float(np.sqrt(np.mean(samples ** 2))) / 327.68)
                        progress(elapsed, level)
                        last_update = elapsed
            if count / rate < 0.25:
                raise ValueError("录音过短，请至少录制一秒后再停止。")
            return str(path)
        except Exception:
            path.unlink(missing_ok=True)
            raise


class AudioSpeaker:
    """All mixer calls are made on the playback worker; stop is a thread Event."""
    def speak(self, text, voice, stop):
        if not text.strip():
            raise ValueError("当前结果没有可朗读的文字。")
        asyncio.run(self._speak(text, voice, stop))

    async def _speak(self, text, voice, stop):
        import tempfile
        import edge_tts
        import pygame
        with tempfile.TemporaryDirectory(prefix="yanxi_tts_") as directory:
            path = Path(directory) / "reply.mp3"
            synthesis = asyncio.create_task(edge_tts.Communicate(text, voice).save(str(path)))
            try:
                started = time.monotonic()
                while not synthesis.done():
                    if stop.is_set():
                        synthesis.cancel()
                        return
                    if time.monotonic() - started > 60:
                        raise TimeoutError("语音合成超时，请检查 Edge TTS 网络连接。")
                    await asyncio.sleep(0.1)
                await synthesis
                if stop.is_set():
                    return
                pygame.mixer.init()
                pygame.mixer.music.load(str(path))
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy() and not stop.is_set():
                    await asyncio.sleep(0.1)
            finally:
                if not synthesis.done():
                    synthesis.cancel()
                try:
                    await synthesis
                except asyncio.CancelledError:
                    pass
                if pygame.mixer.get_init():
                    pygame.mixer.music.stop()
                    pygame.mixer.music.unload()
                    pygame.mixer.quit()
