"""
audio_capture.py — Dual-stream audio capture for Windows.

Captures two streams simultaneously and mixes them:
  1. Microphone input  — your voice
  2. WASAPI loopback   — system audio (Teams, Zoom, browser calls, etc.)

The mixed mono signal is what gets saved to WAV and fed to Whisper + pyannote.

Why loopback matters for Teams/Zoom:
  - Teams/Zoom play remote participants through your speakers/headphones
  - A normal mic only captures YOUR voice
  - Loopback captures the full mix: remote voices + notification sounds
  - Combined: both sides of every call in one recording

Windows WASAPI loopback is the standard, low-latency way to capture system audio.
sounddevice supports it via the `wasapi_exclusive` flag or by selecting a
"Stereo Mix" / "What U Hear" device — we detect this automatically.

Usage:
    from audio_capture import AudioMixer
    mixer = AudioMixer(sample_rate=16000)
    mixer.start()
    ...
    audio_np = mixer.get_chunk(seconds=300)
    mixer.stop()
"""

import queue
import logging
import threading
import numpy as np
import sounddevice as sd

log = logging.getLogger("voice-logger.capture")


def list_audio_devices() -> list[dict]:
    """Return all audio devices with their index, name and channel counts."""
    devices = []
    for i, d in enumerate(sd.query_devices()):
        devices.append({
            "index":      i,
            "name":       d["name"],
            "max_input":  d["max_input_channels"],
            "max_output": d["max_output_channels"],
            "host_api":   sd.query_hostapis(d["hostapi"])["name"],
            "default_sr": int(d["default_samplerate"]),
        })
    return devices


def find_loopback_device() -> int | None:
    """
    Auto-detect a Windows loopback (system audio) device.
    Priority:
      1. Any device named "Stereo Mix", "What U Hear", "Loopback"
      2. Any WASAPI input device whose name contains "output" or "speakers"
         (Windows 10/11 exposes output devices as inputs via WASAPI)
      3. None — loopback not available, mic-only mode
    """
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()

    # Names that typically indicate loopback
    loopback_keywords = ["stereo mix", "what u hear", "loopback",
                         "wave out mix", "mixer", "sum"]

    wasapi_idx = None
    for i, ha in enumerate(hostapis):
        if "wasapi" in ha["name"].lower():
            wasapi_idx = i
            break

    # Pass 1: exact keyword match
    for i, d in enumerate(devices):
        name_lower = d["name"].lower()
        if d["max_input_channels"] > 0:
            for kw in loopback_keywords:
                if kw in name_lower:
                    log.info(f"Loopback device found: [{i}] {d['name']}")
                    return i

    # Pass 2: WASAPI output-as-input (Windows 10/11 exposes these)
    if wasapi_idx is not None:
        output_kw = ["speakers", "headphones", "output", "realtek", "audio out"]
        for i, d in enumerate(devices):
            if d["hostapi"] == wasapi_idx and d["max_input_channels"] > 0:
                name_lower = d["name"].lower()
                for kw in output_kw:
                    if kw in name_lower:
                        log.info(f"WASAPI loopback candidate: [{i}] {d['name']}")
                        return i

    log.warning("No loopback device found. Recording microphone only.")
    return None


def _resample(data: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Linear-interpolation resample — good enough for speech at moderate ratios."""
    if src_sr == dst_sr:
        return data
    n_dst = int(len(data) * dst_sr / src_sr)
    return np.interp(
        np.linspace(0, len(data) - 1, n_dst),
        np.arange(len(data)),
        data,
    ).astype(np.float32)


def find_mic_device() -> int | None:
    """Return default microphone device index, or None to use system default."""
    try:
        default = sd.query_devices(kind="input")
        log.info(f"Default mic: {default['name']}")
        return None  # None = use system default
    except Exception:
        return None


class AudioMixer:
    """
    Records mic + loopback simultaneously, resamples both to `sample_rate`,
    and mixes them into a single mono stream.

    Thread-safe: call get_chunk() from any thread.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        mic_device: int | None = None,
        loopback_device: int | None = None,
        mic_gain: float = 1.0,
        loopback_gain: float = 1.0,
    ):
        self.sr             = sample_rate
        self.mic_device     = mic_device
        self.loopback_device = loopback_device if loopback_device is not None else find_loopback_device()
        self.mic_gain       = mic_gain
        self.loopback_gain  = loopback_gain

        self._mic_q:      queue.Queue = queue.Queue()
        self._loopback_q: queue.Queue = queue.Queue()
        self._mixed_q:    queue.Queue = queue.Queue()

        self._stop      = threading.Event()
        self._mic_stream      = None
        self._loopback_stream = None
        self._mixer_thread    = None
        self._loopback_native_sr: int = sample_rate  # updated in start() if device needs native rate

        if self.loopback_device is not None:
            log.info(f"AudioMixer: mic + loopback [{self.loopback_device}] → mix")
        else:
            log.info("AudioMixer: mic only (no loopback device)")

    # ── Stream callbacks ────────────────────────────────────────────────────
    def _mic_callback(self, indata, frames, time_info, status):
        if status:
            log.debug(f"Mic status: {status}")
        self._mic_q.put(indata[:, 0].copy())  # take ch0 → mono

    def _loopback_callback(self, indata, frames, time_info, status):
        if status:
            log.debug(f"Loopback status: {status}")
        mono = indata.mean(axis=1) if indata.ndim > 1 else indata[:, 0]
        if self._loopback_native_sr != self.sr:
            mono = _resample(mono, self._loopback_native_sr, self.sr)
        self._loopback_q.put(mono.copy())

    # ── Mixer thread ────────────────────────────────────────────────────────
    def _mix_loop(self):
        """
        Continuously drain both queues, align by block size, mix, and
        push to _mixed_q.
        """
        block_size = int(self.sr * 0.5)  # 500 ms blocks

        mic_buf      = np.zeros(0, dtype=np.float32)
        loopback_buf = np.zeros(0, dtype=np.float32)

        while not self._stop.is_set():
            # Drain mic
            while not self._mic_q.empty():
                mic_buf = np.concatenate([mic_buf, self._mic_q.get()])

            # Drain loopback
            if self.loopback_device is not None:
                while not self._loopback_q.empty():
                    loopback_buf = np.concatenate([loopback_buf, self._loopback_q.get()])

            # Emit mixed blocks when we have enough data
            while len(mic_buf) >= block_size:
                mic_block = mic_buf[:block_size] * self.mic_gain
                mic_buf   = mic_buf[block_size:]

                if self.loopback_device is not None and len(loopback_buf) >= block_size:
                    lb_block      = loopback_buf[:block_size] * self.loopback_gain
                    loopback_buf  = loopback_buf[block_size:]
                    mixed = np.clip(mic_block + lb_block, -1.0, 1.0)
                else:
                    mixed = mic_block

                self._mixed_q.put(mixed)

            self._stop.wait(timeout=0.05)

    # ── Public API ──────────────────────────────────────────────────────────
    def start(self):
        blocksize = int(self.sr * 0.5)

        # Mic stream (always)
        self._mic_stream = sd.InputStream(
            device=self.mic_device,
            samplerate=self.sr,
            channels=1,
            dtype="float32",
            callback=self._mic_callback,
            blocksize=blocksize,
        )
        self._mic_stream.start()
        log.info("Mic stream started.")

        # Loopback stream (optional)
        if self.loopback_device is not None:
            try:
                lb_info = sd.query_devices(self.loopback_device)
                lb_ch   = min(2, lb_info["max_input_channels"])
                opened  = False
                for try_sr in [self.sr, int(lb_info["default_samplerate"])]:
                    try:
                        self._loopback_stream = sd.InputStream(
                            device=self.loopback_device,
                            samplerate=try_sr,
                            channels=lb_ch,
                            dtype="float32",
                            callback=self._loopback_callback,
                            blocksize=int(try_sr * 0.5),
                        )
                        self._loopback_stream.start()
                        self._loopback_native_sr = try_sr
                        opened = True
                        if try_sr != self.sr:
                            log.info(
                                f"Loopback stream started at native {try_sr} Hz "
                                f"(resampling to {self.sr} Hz): [{self.loopback_device}] {lb_info['name']}"
                            )
                        else:
                            log.info(f"Loopback stream started: [{self.loopback_device}] {lb_info['name']}")
                        break
                    except Exception as e_sr:
                        log.debug(f"Loopback at {try_sr} Hz failed: {e_sr}")
                if not opened:
                    raise RuntimeError("all sample rates failed")
            except Exception as e:
                log.warning(f"Loopback stream failed ({e}) — mic only mode.")
                self._loopback_stream = None
                self.loopback_device  = None

        self._mixer_thread = threading.Thread(target=self._mix_loop, daemon=True)
        self._mixer_thread.start()

    def stop(self):
        self._stop.set()
        if self._mic_stream:
            self._mic_stream.stop()
            self._mic_stream.close()
        if self._loopback_stream:
            self._loopback_stream.stop()
            self._loopback_stream.close()
        if self._mixer_thread:
            self._mixer_thread.join(timeout=3)
        log.info("AudioMixer stopped.")

    def get_audio(self, timeout: float = 1.0) -> np.ndarray | None:
        """
        Pull one 500 ms block from the mixed queue.
        Returns None on timeout (use to check _stop_event in the caller).
        """
        try:
            return self._mixed_q.get(timeout=timeout)
        except queue.Empty:
            return None

    def has_speech(self, audio: np.ndarray, threshold: float = 0.01) -> bool:
        return float(np.sqrt(np.mean(audio ** 2))) >= threshold

    @property
    def using_loopback(self) -> bool:
        return self._loopback_stream is not None
