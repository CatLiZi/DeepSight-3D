"""Native acquisition with bounded buffers and measured clocks, independent of DSP/inference."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import subprocess
import threading
import time
import wave
import queue
from pathlib import Path

import numpy as np

from .devices import choose_dshow_device, choose_wasapi_input


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Read JPEG SOF dimensions without decoding its pixels."""
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("Not a JPEG")
    index = 2
    while index + 4 < len(data):
        if data[index] != 255:
            index += 1
            continue
        marker = data[index + 1]
        index += 2
        if marker == 255:
            index -= 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            continue
        if marker in (0xD9, 0xDA):
            break
        length = int.from_bytes(data[index:index + 2], "big")
        if length < 2 or index + length > len(data):
            break
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            return int.from_bytes(data[index + 5:index + 7], "big"), int.from_bytes(data[index + 3:index + 5], "big")
        index += length
    raise ValueError("JPEG dimensions missing")


class JpegParser:
    def __init__(self, max_bytes: int = 16 * 1024 * 1024):
        self.buffer = bytearray()
        self.max_bytes = max_bytes

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buffer.extend(chunk)
        frames = []
        while True:
            start = self.buffer.find(b"\xff\xd8")
            if start < 0:
                self.buffer[:] = self.buffer[-1:]
                break
            if start:
                del self.buffer[:start]
            end = self.buffer.find(b"\xff\xd9", 2)
            nested = self.buffer.find(b"\xff\xd8", 2)
            if nested >= 0 and (end < 0 or nested < end):
                del self.buffer[:nested]
                continue
            if end < 0:
                if len(self.buffer) > self.max_bytes:
                    self.buffer.clear()
                break
            frames.append(bytes(self.buffer[:end + 2]))
            del self.buffer[:end + 2]
        return frames


@dataclass(frozen=True)
class VideoSample:
    sequence: int
    jpeg: bytes
    width: int
    height: int
    captured_monotonic: float
    captured_unix: float


class StereoCameraCapture:
    def __init__(self, *, device_name="USB Video Device", usb_vid="1BCF", usb_pid="0B15",
                 width=3840, height=1080, fps=60, ffmpeg="ffmpeg"):
        self.device_name, self.usb_vid, self.usb_pid = device_name, usb_vid, usb_pid
        self.width, self.height, self.requested_fps = int(width), int(height), float(fps)
        self.ffmpeg = ffmpeg
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._process = None
        self._reader = self._stderr_reader = None
        self._latest = None
        self._times = deque(maxlen=600)
        self._history = deque(maxlen=12)
        self._sequence = self._bytes = self._invalid = 0
        self._error = "stopped"
        self._stderr = deque(maxlen=30)
        self._last_start = -math.inf
        self.actual_device = None
        self.diagnostic_sink = None
        self._lifecycle_lock = threading.RLock()
        self._enabled = False
        self._watchdog_stop = threading.Event()
        self._watchdog = None
        self._recoveries = 0
        self._retry_after = 0.
        self._recovery_reason = ''

    def command(self, device) -> list[str]:
        selector = device.alternative_name or device.name
        return [self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin",
                "-f", "dshow", "-rtbufsize", "256M", "-video_size", f"{self.width}x{self.height}",
                "-framerate", str(self.requested_fps), "-vcodec", "mjpeg", "-i", f"video={selector}",
                "-map", "0:v:0", "-an", "-c:v", "copy", "-f", "image2pipe", "pipe:1"]

    def start(self):
        self._enabled = True
        if not self._lifecycle_lock.acquire(blocking=False):
            return
        try:
            if self._watchdog is None or not self._watchdog.is_alive():
                self._watchdog_stop.clear()
                self._watchdog = threading.Thread(target=self._watch_stream, daemon=True)
                self._watchdog.start()
            self._start_process()
        finally:
            self._lifecycle_lock.release()

    def _start_process(self):
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return
            if not self._enabled or time.perf_counter() < max(self._last_start + 2, self._retry_after):
                return
            self._last_start = time.perf_counter()
            try:
                device = choose_dshow_device("video", [self.device_name], ffmpeg=self.ffmpeg,
                                             usb_vid=self.usb_vid, usb_pid=self.usb_pid)
                if device is None:
                    raise RuntimeError(f"Camera {self.usb_vid}:{self.usb_pid} not found")
                self.actual_device = device
                self._stop.clear()
                self._error = "starting"
                self._times.clear()
                self._history.clear()
                self._latest = None
                # Keep sequence monotonic across restarts so downstream users
                # never mistake a new session's frame for an old cached frame.
                self._stderr.clear()
                process = subprocess.Popen(self.command(device), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           stdin=subprocess.DEVNULL, bufsize=0,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self._process = process
                self._stderr_reader = threading.Thread(target=self._drain_stderr, args=(process,), daemon=True)
                self._reader = threading.Thread(target=self._read_loop, args=(process,), daemon=True)
                self._stderr_reader.start()
                self._reader.start()
            except Exception as exc:
                self._error = str(exc)

    def _watch_stream(self):
        while not self._watchdog_stop.wait(.5):
            with self._lock:
                if not self._enabled:
                    return
                process = self._process
                age = time.perf_counter() - self._latest.captured_monotonic if self._latest else None
                started = time.perf_counter() - self._last_start
                stalled = process is not None and (process.poll() is not None or (age is not None and age > 3) or (age is None and started > 8))
            if stalled:
                with self._lifecycle_lock:
                    if not self._enabled:
                        return
                    self._recoveries += 1
                    self._recovery_reason = 'camera stopped delivering fresh frames'
                    if self.diagnostic_sink:
                        try:
                            self.diagnostic_sink('camera_stall_error', dict(age_s=age, recoveries=self._recoveries))
                        except Exception:
                            pass
                    self._close_process()
                    self._retry_after = time.perf_counter() + min(30., 2 ** min(self._recoveries, 5))
            if self._enabled:
                self.start()

    def _drain_stderr(self, process):
        for line in iter(process.stderr.readline, b""):
            self._stderr.append(line.decode("utf-8", errors="replace").strip())

    def _accept(self, jpeg, now=None, *, process=None):
        width, height = jpeg_dimensions(jpeg)
        if (width, height) != (self.width, self.height):
            self._invalid += 1
            self._error = f"Unexpected JPEG size {width}x{height}; expected {self.width}x{self.height}"
            return
        now = time.perf_counter() if now is None else now
        with self._lock:
            if process is not None and process is not self._process:
                return
            self._sequence += 1
            self._bytes += len(jpeg)
            sample = VideoSample(self._sequence, jpeg, width, height, now, time.time())
            self._latest = sample
            self._times.append(now)
            self._history.append(sample)
            self._error = "streaming"

    def _read_loop(self, process):
        parser = JpegParser()
        try:
            while not self._stop.is_set() and process is self._process:
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                for jpeg in parser.feed(chunk):
                    try:
                        self._accept(jpeg, process=process)
                    except ValueError:
                        self._invalid += 1
        except (OSError, ValueError) as exc:
            if not self._stop.is_set() and process is self._process:
                self._error = str(exc)
        finally:
            if not self._stop.is_set() and process is self._process:
                self._error = "camera stream ended; " + " | ".join(list(self._stderr)[-2:])

    def latest(self) -> VideoSample | None:
        with self._lock:
            return self._latest

    def metrics(self) -> dict:
        with self._lock:
            stamps = list(self._times)
            latest = self._latest
            age = time.perf_counter() - latest.captured_monotonic if latest else None
            intervals = np.diff(stamps)
            fps = (len(stamps) - 1) / (stamps[-1] - stamps[0]) if len(stamps) > 1 and stamps[-1] > stamps[0] else 0
            return {"backend": "ffmpeg_dshow_mjpeg_copy", "device_name": self.actual_device.name if self.actual_device else self.device_name,
                    "usb_vid": self.usb_vid, "usb_pid": self.usb_pid, "width": self.width, "height": self.height,
                    "layout": "side_by_side", "per_eye_width": self.width // 2, "format": "MJPEG",
                    "requested_fps": self.requested_fps, "measured_fps": round(fps, 3) if age is not None and age < 2 else 0,
                    "frames": self._sequence, "jpeg_bytes": self._bytes, "invalid_frames": self._invalid,
                    "frame_age_s": round(age, 4) if age is not None else None,
                    "interval_p95_ms": round(float(np.percentile(intervals, 95)) * 1000, 3) if len(intervals) else None,
                    "timestamp_clock": "perf_counter", "timestamp_kind": "host JPEG receipt; exposure clock not calibrated",
                    "ok": age is not None and age < 2 and self._reader is not None and self._reader.is_alive(),
                    "message": 'camera stalled; recovering' if age is not None and age >= 2 else self._error,
                    "recovery_count": self._recoveries, "recovery_reason": self._recovery_reason,
                    "stderr_tail": list(self._stderr)[-4:]}

    def stop(self):
        self._enabled = False
        self._watchdog_stop.set()
        with self._lifecycle_lock:
            self._close_process()
        if self._watchdog and self._watchdog is not threading.current_thread():
            self._watchdog.join(timeout=1)

    def _close_process(self):
        self._stop.set()
        process = self._process
        with self._lock:
            self._process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for worker in (self._reader, self._stderr_reader):
            if worker and worker is not threading.current_thread():
                worker.join(timeout=3)
        for handle in (getattr(process, "stdout", None), getattr(process, "stderr", None)):
            if handle:
                handle.close()
        with self._lock:
            self._error = "stopped"
            self._latest = None


@dataclass(frozen=True)
class AudioSample:
    sequence: int
    pcm: bytes
    sample_start: int
    frame_count: int
    channels: int
    sample_rate: int
    captured_monotonic: float
    captured_unix: float
    received_monotonic: float
    discontinuity: bool = False


class PcmRecorder:
    def __init__(self, path, *, channels, sample_rate):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("xb")
        self._wav = wave.open(self._file, "wb")
        self._wav.setnchannels(channels)
        self._wav.setsampwidth(2)
        self._wav.setframerate(sample_rate)
        self.channels = channels
        self._queue = queue.Queue(maxsize=500)
        self._done = threading.Event()
        self.frames = self.dropped_blocks = 0
        self.error = ""
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def add(self, raw):
        if self._done.is_set():
            return
        try:
            self._queue.put_nowait(raw)
        except queue.Full:
            self.dropped_blocks += 1

    def _run(self):
        try:
            while not self._done.is_set() or not self._queue.empty():
                try:
                    raw = self._queue.get(timeout=.05)
                except queue.Empty:
                    continue
                self._wav.writeframesraw(raw)
                self.frames += len(raw) // (self.channels * 2)
        except Exception as exc:
            self.error = str(exc)
        finally:
            self._wav.close()
            self._file.close()

    def close(self):
        self._done.set()
        self._worker.join(timeout=5)
        if self._worker.is_alive():
            self.error = "recording writer did not finish within 5 seconds"

    def metrics(self):
        return {"path": str(self.path), "frames_written": self.frames,
                "dropped_blocks": self.dropped_blocks, "error": self.error}


class WasapiAudioCapture:
    def __init__(self, *, device_name="YDPI4MIC", channels=4, sample_rate=48000, block_ms=20,
                 usb_vid="EF09", usb_pid="459D", audio_module=None, wasapi_raw=True):
        self.device_name, self.channels, self.sample_rate = device_name, int(channels), int(sample_rate)
        self.usb_vid, self.usb_pid = usb_vid, usb_pid
        self.block_frames = round(self.sample_rate * block_ms / 1000)
        self._audio_module = audio_module
        self.wasapi_raw = bool(wasapi_raw)
        self._raw_stream_option = None
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._recorder = None
        self._last_recording = None
        self._stream = None
        self._history = deque(maxlen=max(1, math.ceil(3.5 * self.sample_rate / self.block_frames)))
        self._frames = self._sequence = self._overflows = 0
        self._levels = [0.0] * self.channels
        self._error = "stopped"
        self._last_start = -math.inf
        self.actual_device = None

    def start(self):
        with self._lifecycle_lock:
            try:active=self._stream is not None and self._stream.active
            except Exception:active=False
            if active:
                return
            if time.perf_counter() - self._last_start < 2:
                return
            self._last_start = time.perf_counter()
            try:
                if self._stream is not None:
                    old,self._stream=self._stream,None
                    try:old.close()
                    except Exception:pass  # A removed USB endpoint may no longer close cleanly.
                module = self._audio_module
                if module is None:
                    import sounddevice as module
                self.actual_device = choose_wasapi_input(self.device_name, channels=self.channels,
                                                        usb_vid=self.usb_vid, usb_pid=self.usb_pid, audio_module=module)
                with self._lock:
                    self._history.clear()
                    self._frames = self._sequence = self._overflows = 0
                self._raw_stream_option = None
                extra = module.WasapiSettings(exclusive=False)
                if self.wasapi_raw:
                    # PortAudio eStreamOptionRaw=1 bypasses endpoint DSP without
                    # changing global Windows settings. sounddevice 0.5.x only
                    # exposes this native option through its CFFI stream info.
                    info = getattr(extra, "_streaminfo", None)
                    if info is None or not hasattr(info, "streamOption"):
                        raise RuntimeError("WASAPI RAW unavailable; install supported sounddevice/PortAudio")
                    info.streamOption = 1
                    self._raw_stream_option = int(info.streamOption)
                stream = module.RawInputStream(device=self.actual_device["index"], channels=self.channels,
                                               samplerate=self.sample_rate, dtype="int16", blocksize=self.block_frames,
                                               extra_settings=extra,
                                               callback=self._callback)
                self._stream = stream
                stream.start()
                self._error = "capturing"
            except Exception as exc:
                if self._stream is not None:
                    failed,self._stream=self._stream,None
                    try:failed.close()
                    except Exception:pass
                self._error = str(exc)

    def _callback(self, data, frames, clock, status):
        received = time.perf_counter()
        raw = bytes(data)
        if len(raw) != frames * self.channels * 2:
            self._error = "Audio callback size mismatch"
            return
        # PortAudio's ADC clock is mapped to the shared host monotonic clock.
        delay = max(0.0, float(clock.currentTime - clock.inputBufferAdcTime))
        captured = received - delay
        values = np.frombuffer(raw, dtype="<i2").reshape(frames, self.channels).astype(np.float64)
        levels = (np.sqrt(np.mean(values * values, axis=0)) / 32768).tolist()
        with self._lock:
            self._sequence += 1
            sample = AudioSample(self._sequence, raw, self._frames, frames, self.channels, self.sample_rate,
                                 captured, time.time() - delay, received,
                                 discontinuity=bool(getattr(status, "input_overflow", False)))
            self._history.append(sample)
            self._frames += frames
            self._overflows += int(bool(getattr(status, "input_overflow", False)))
            self._levels = levels
            recorder = self._recorder
        if recorder:
            recorder.add(raw)

    def start_recording(self, path):
        with self._lock:
            if self._recorder is None:
                self._recorder = PcmRecorder(path, channels=self.channels, sample_rate=self.sample_rate)

    def stop_recording(self):
        with self._lock:
            recorder, self._recorder = self._recorder, None
        if recorder:
            recorder.close()
            self._last_recording = recorder.metrics()

    def latest(self):
        with self._lock:
            return self._history[-1] if self._history else None

    def recent(self, seconds=2.0):
        with self._lock:
            if not self._history:
                return []
            cutoff = self._history[-1].sample_start + self._history[-1].frame_count - int(seconds * self.sample_rate)
            return [sample for sample in self._history if sample.sample_start + sample.frame_count > cutoff]

    def save_recent(self, path: Path, seconds=3.0):
        samples = self.recent(seconds)
        if not samples:
            raise RuntimeError("No audio captured")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as output:
            output.setnchannels(self.channels)
            output.setsampwidth(2)
            output.setframerate(self.sample_rate)
            raw = b"".join(sample.pcm for sample in samples)
            output.writeframes(raw[-round(seconds * self.sample_rate) * self.channels * 2:])

    def metrics(self):
        with self._lock:
            latest = self._history[-1] if self._history else None
            age = time.perf_counter() - latest.received_monotonic if latest else None
            stream = self._stream
            try:
                active = stream is not None and stream.active
            except Exception:
                active = False
            rate = None
            clock_valid = None
            if len(self._history) > 1:
                first = self._history[0]
                span = latest.received_monotonic - first.received_monotonic
                if span >= 2.0:
                    rate = (latest.sample_start - first.sample_start) / span
                    clock_valid = abs(rate / self.sample_rate - 1.) <= .10
            ok = active and age is not None and age < 1 and clock_valid is not False
            message = ('sample_clock_mismatch: received %.0f frames/s; requested %d' % (rate, self.sample_rate)
                       if clock_valid is False else 'capturing' if ok else self._error)
            return {"backend": "WASAPI shared", "wasapi_raw": self.wasapi_raw,
                    "wasapi_raw_requested": self.wasapi_raw, "raw_stream_option": self._raw_stream_option,
                    "raw_stream_configured": bool(active and self._raw_stream_option == 1), "device_name": self.actual_device["name"] if self.actual_device else self.device_name,
                    "usb_vid": self.usb_vid, "usb_pid": self.usb_pid, "channels": self.channels,
                    "sample_rate": self.sample_rate, "sample_bits": 16, "format": "PCM s16le",
                    "block_frames": self.block_frames, "sample_frames": self._frames, "blocks": self._sequence,
                    "overflow_count": self._overflows, "channel_rms": list(self._levels),
                    "observed_sample_rate_hz": round(rate, 2) if rate is not None else None,
                    "sample_clock_valid": clock_valid, "stream_active": bool(active),
                    "block_age_s": round(age, 4) if age is not None else None,
                    "timestamp_clock": "perf_counter", "timestamp_kind": "PortAudio ADC mapped to host clock",
                    "recording": self._recorder.metrics() if self._recorder else self._last_recording,
                    "ok": bool(ok), "message": message}

    def stop(self):
        # Do not hold the callback lock while waiting for PortAudio to stop.
        with self._lifecycle_lock:
            stream, self._stream = self._stream, None
            if stream is not None:
                stream.stop()
                stream.close()
            self.stop_recording()
        self._error = "stopped"
