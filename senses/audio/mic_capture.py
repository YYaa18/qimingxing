from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from senses.audio.base import AudioCapturer

logger = logging.getLogger("qimingxing.senses.mic")


class SoundDeviceMicCapturer(AudioCapturer):
    """Low-latency 16kHz mono signed 16-bit PCM capturer powered by sounddevice."""

    def __init__(self, *, sample_rate: int = 16000, block_ms: int = 100) -> None:
        self.sample_rate = max(8000, int(sample_rate))
        self.blocksize = max(320, int(self.sample_rate * max(20, block_ms) / 1000))
        self._stream: Any = None
        self._lock = threading.Lock()
        self._active = False

    def start(self, on_pcm_chunk: Callable[[bytes], None]) -> None:
        with self._lock:
            if self._active:
                return
            try:
                import sounddevice as sd
            except Exception as exc:
                logger.warning("sounddevice 未安装或不可用，无法打开麦克风: %s", exc)
                return

            def _audio_cb(indata, _frames, _time_info, status) -> None:
                if status:
                    logger.debug("Mic input status: %s", status)
                if not self._active:
                    return
                try:
                    raw = bytes(indata)
                    if raw:
                        on_pcm_chunk(raw)
                except Exception as exc:
                    logger.debug("Ignoring mic chunk error: %s", exc)

            try:
                stream = sd.RawInputStream(
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="int16",
                    blocksize=self.blocksize,
                    callback=_audio_cb,
                )
                stream.start()
                self._stream = stream
                self._active = True
            except Exception as exc:
                logger.warning("打开麦克风流失败（请检查麦克风权限或输入设备）: %s", exc)
                self._stream = None
                self._active = False

    def stop(self) -> None:
        with self._lock:
            self._active = False
            stream = self._stream
            self._stream = None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
