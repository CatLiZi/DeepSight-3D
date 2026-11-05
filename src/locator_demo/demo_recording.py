"""Local demonstration recording using existing PCM; no device or motor control.

Browser-selected tab video is uploaded separately. Audio is drained from the
capture history on a worker, retaining four channels and its ADC time reference.
Only a small, reviewed telemetry schema is accepted; all files stay local.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import threading
import time
import uuid
import wave
import zipfile
from pathlib import Path

MAX_SECONDS = 60
MAX_VIDEO_BYTES = 96 * 1024 * 1024
MAX_LOG_BYTES = 4 * 1024 * 1024
DOWNLOADS = {
    'video': ('demo.mp4', 'video/mp4'),
    'screen': ('screen.webm', 'video/webm'),
    'audio': ('audio-4ch.wav', 'audio/wav'),
    'timeline': ('timeline.jsonl', 'application/x-ndjson'),
    'manifest': ('session.json', 'application/json'),
    'bundle': ('bundle.zip', 'application/zip'),
}


class RecordingError(ValueError):
    pass


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def write_json(path, value):
    temporary = path.with_suffix('.writing')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def validate_timeline(payload):
    """The browser records display values, not images or arbitrary runtime logs."""
    if not isinstance(payload, dict) or set(payload) != {'video', 'events'}:
        raise RecordingError('录制时间线格式无效')
    video = payload['video']
    if not isinstance(video, dict):
        raise RecordingError('缺少视频时钟')
    start, duration = video.get('started_unix_ms'), video.get('elapsed_ms')
    if not finite(start) or not finite(duration) or not 100 <= duration <= 65000:
        raise RecordingError('视频时钟或长度无效')
    events = payload['events']
    if not isinstance(events, list) or len(events) > 4000:
        raise RecordingError('时间线条目过多')
    rows = []
    previous_ms = -1
    allowed = {'t_ms', 'kind', 'speaker', 'cue', 'revision', 'running', 'mode',
               'target_id', 'locked_speaker_id', 'audio', 'pose', 'feedback', 'candidates', 'motion', 'selection'}
    for event in events:
        if not isinstance(event, dict) or set(event) - allowed:
            raise RecordingError('时间线包含未支持的字段')
        if not finite(event.get('t_ms')) or not 0 <= event['t_ms'] <= 65000:
            raise RecordingError('时间线时间戳无效')
        if event['t_ms'] < previous_ms or event['t_ms'] > duration + 1000:
            raise RecordingError('时间线时间顺序或录制范围无效')
        previous_ms = event['t_ms']
        # Bound every nested container/string, and refuse non-finite numbers.
        def check(value, depth=0):
            if depth > 4:
                raise RecordingError('时间线层级无效')
            if value is None or isinstance(value, bool):
                return
            if isinstance(value, str):
                if len(value) > 120:
                    raise RecordingError('时间线文本过长')
            elif isinstance(value, (int, float)):
                if not finite(value) or abs(value) > 1e12:
                    raise RecordingError('时间线数值无效')
            elif isinstance(value, list):
                if len(value) > 20:
                    raise RecordingError('时间线候选过多')
                for v in value:
                    check(v, depth + 1)
            elif isinstance(value, dict):
                if len(value) > 16 or any(not isinstance(k, str) or len(k) > 30 for k in value):
                    raise RecordingError('时间线对象无效')
                for v in value.values():
                    check(v, depth + 1)
            else:
                raise RecordingError('时间线类型无效')
        check(event)
        rows.append(event)
    return {'started_unix_ms': float(start), 'elapsed_ms': float(duration),
            'surface': str(video.get('surface', 'unknown'))[:20]}, rows


class RecordingSession:
    def __init__(self, directory, capture, capture_getter, *, left_channel=2, right_channel=3, context=None):
        self.directory, self.capture, self.capture_getter = Path(directory), capture, capture_getter
        self.id = self.directory.name
        self.started_mono, self.started_wall = time.perf_counter(), time.time()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._cutoff = None
        self._last_sequence = None
        self._sample_end = None
        self._last_received = None
        self._last_data_mono = self.started_mono
        self.directory.mkdir(parents=True, exist_ok=False)
        self.meta = {
            'id': self.id, 'feature_version': 'D1', 'status': 'recording',
            'started_unix_ms': self.started_wall * 1000, 'max_seconds': MAX_SECONDS,
            'audio_channels': 4, 'audio_sample_rate_hz': 48000, 'audio_bits': 16,
            'playback_channels': [left_channel, right_channel], 'audio_first_unix_ms': None,
            'audio_frames': 0, 'missing_frames_padded': 0, 'overflow_blocks': 0,
            'stop_reason': '', 'error': '', 'context': context or {},
            'sync_scope': 'software clocks; screen/ADC physical latency not calibrated',
        }
        self._wav = wave.open(str(self.directory / 'audio-4ch.wav'), 'wb')
        self._wav.setnchannels(4)
        self._wav.setsampwidth(2)
        self._wav.setframerate(48000)
        write_json(self.directory / 'session.json', self.meta)
        self.worker = threading.Thread(target=self._run, daemon=True, name='demo-audio-recorder')
        self.worker.start()

    def _consume(self, samples, cutoff):
        for sample in samples:
            if self._last_received is not None and sample.received_monotonic <= self._last_received:
                continue
            if sample.channels != 4 or sample.sample_rate != 48000 or len(sample.pcm) != sample.frame_count * 8:
                raise RecordingError('录制期间音频格式改变')
            if self._last_sequence is not None and (sample.sequence <= self._last_sequence or sample.sample_start < self._sample_end):
                raise RecordingError('音频流已重连或采样时钟回退')
            gap = max(0, sample.sample_start - self._sample_end) if self._sample_end is not None else 0
            self._last_sequence, self._sample_end = sample.sequence, sample.sample_start + sample.frame_count
            self._last_received = sample.received_monotonic
            start = max(0, math.ceil((self.started_mono - sample.captured_monotonic) * 48000))
            end = min(sample.frame_count, math.floor((cutoff - sample.captured_monotonic) * 48000))
            if end <= start:
                continue
            if not self.meta['audio_frames']:
                self.meta['audio_first_unix_ms'] = (sample.captured_unix + start / 48000) * 1000
            elif gap:
                if gap > MAX_SECONDS * 48000:
                    raise RecordingError('录制期间采样时钟跳变')
                padding = min(gap, max(0, int(MAX_SECONDS * 48000) - self.meta['audio_frames']))
                self._wav.writeframesraw(bytes(padding * 8))
                self.meta['missing_frames_padded'] += padding
                self.meta['audio_frames'] += padding
            end = min(end, start + max(0, int(MAX_SECONDS * 48000) - self.meta['audio_frames']))
            self._wav.writeframesraw(sample.pcm[start * 8:end * 8])
            self.meta['audio_frames'] += end - start
            self.meta['overflow_blocks'] += int(sample.discontinuity)
            self._last_data_mono = time.perf_counter()

    def _run(self):
        try:
            while True:
                now = time.perf_counter()
                cutoff = min(self._cutoff or now, self.started_mono + MAX_SECONDS)
                if self.capture_getter() is not self.capture:
                    raise RecordingError('音频设备配置已改变，录制已结束')
                self._consume(self.capture.recent(3.5), cutoff)
                if self._stop.is_set() or now >= self.started_mono + MAX_SECONDS:
                    if not self.meta['stop_reason']:
                        self.meta['stop_reason'] = 'duration_limit'
                    break
                if now - self._last_data_mono > 2.0:
                    raise RecordingError('没有收到连续音频，录制已结束')
                self._stop.wait(.02)
        except Exception as exc:
            self.meta['error'] = str(exc)[:300]
            self.meta['stop_reason'] = 'audio_error'
        finally:
            self._wav.close()
            with self._lock:
                self.meta['status'] = 'awaiting_video'
                self.meta['stopped_unix_ms'] = time.time() * 1000
                write_json(self.directory / 'session.json', self.meta)

    def stop(self, reason='user_stop'):
        if self.worker.is_alive() and not self._stop.is_set():
            self._cutoff = min(time.perf_counter(), self.started_mono + MAX_SECONDS)
            self.meta['stop_reason'] = reason
            self._stop.set()
        self.worker.join(timeout=3)
        if self.worker.is_alive():
            raise RecordingError('音频写入尚未结束，请稍后重试')
        return self.snapshot()

    def snapshot(self):
        with self._lock:
            return dict(self.meta)


class DemoRecorder:
    def __init__(self, root, capture_getter, *, runner=subprocess.run):
        self.root, self.capture_getter, self.runner = Path(root), capture_getter, runner
        self._lock = threading.RLock()
        self._sessions = {}
        self._uploading = set()
        self._processing = set()

    def _path(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{32}', identity):
            raise RecordingError('录制编号无效')
        path = self.root / identity
        if path.is_symlink() or path.resolve().parent != self.root.resolve():
            raise RecordingError('录制目录无效')
        return path

    def start(self, *, left_channel=2, right_channel=3, context=None):
        if left_channel not in range(1, 5) or right_channel not in range(1, 5) or left_channel == right_channel:
            raise RecordingError('两耳播放通道配置无效')
        with self._lock:
            if any(s.worker.is_alive() for s in self._sessions.values()) or self._processing:
                raise RecordingError('已有录制或视频生成任务，请先结束')
            capture = self.capture_getter()
            if capture is None or not callable(getattr(capture, 'recent', None)):
                raise RecordingError('需要当前四通道 WASAPI RAW 采集；请先开启摄像头和麦克风采集')
            metrics = capture.metrics()
            if not metrics.get('ok') or (metrics.get('channels'), metrics.get('sample_rate'), metrics.get('sample_bits')) != (4, 48000, 16) or metrics.get('raw_stream_configured') is not True:
                raise RecordingError('四通道 RAW 音频尚未就绪；录制不会另开麦克风')
            identity = uuid.uuid4().hex
            session = RecordingSession(self._path(identity), capture, self.capture_getter,
                                       left_channel=left_channel, right_channel=right_channel, context=context)
            self._sessions[identity] = session
            return self.describe(identity)

    def stop(self, identity, reason='user_stop'):
        path = self._path(identity)
        with self._lock:
            session = self._sessions.get(identity)
        if session:
            session.stop(reason)
        elif not (path / 'session.json').is_file():
            raise RecordingError('找不到本次录制')
        return self.describe(identity)

    def describe(self, identity):
        path = self._path(identity)
        with self._lock:
            session = self._sessions.get(identity)
            if session:
                meta = session.snapshot()
            elif (path / 'session.json').is_file():
                meta = json.loads((path / 'session.json').read_text(encoding='utf-8'))
                if meta.get('status') in ('recording', 'processing') and identity not in self._processing:
                    meta = {**meta, 'status': 'interrupted', 'error': '程序上次退出时录制未完成'}
            else:
                raise RecordingError('找不到本次录制')
        available = {kind: f'/api/demo-recordings/{identity}/download/{kind}'
                     for kind, (name, _) in DOWNLOADS.items() if (path / name).is_file()
                     and (kind not in ('video', 'bundle') or meta['status'] == 'ready')
                     and (kind != 'audio' or meta['status'] != 'recording')}
        return {**meta, 'files': available}

    def recent(self):
        if not self.root.is_dir():
            return []
        paths = sorted((p for p in self.root.iterdir() if re.fullmatch(r'[0-9a-f]{32}', p.name)),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        rows = []
        for path in paths[:10]:
            try:
                rows.append(self.describe(path.name))
            except (ValueError, OSError):
                continue
        return rows

    def save_timeline(self, identity, payload):
        path = self._path(identity)
        video, events = validate_timeline(payload)
        self.stop(identity)
        with self._lock:
            meta = self.describe(identity)
            if identity in self._processing or meta['status'] == 'ready':
                raise RecordingError('视频已生成或正在生成，不能改写时间线')
            if not meta['started_unix_ms'] - 2000 <= video['started_unix_ms'] <= meta['started_unix_ms'] + 5000:
                raise RecordingError('浏览器与服务时间不一致，请重新录制')
            temporary = path / 'timeline.writing'
            with temporary.open('w', encoding='utf-8') as stream:
                for event in events:
                    stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n')
            temporary.replace(path / 'timeline.jsonl')
            meta.pop('files', None)
            meta['video_clock'] = video
            meta['telemetry_rows'] = len(events)
            self._save_meta(identity, meta)
        return self.describe(identity)

    def _save_meta(self, identity, meta):
        # Windows can reject an open racing with atomic file replacement.
        # describe() uses this same lock; export completion must do so too.
        with self._lock:
            write_json(self._path(identity) / 'session.json', meta)
            session = self._sessions.get(identity)
            if session:
                with session._lock:
                    session.meta = dict(meta)

    def begin_upload(self, identity):
        self.stop(identity)
        with self._lock:
            meta = self.describe(identity)
            if identity in self._uploading or identity in self._processing or meta['status'] == 'ready':
                raise RecordingError('本次视频已上传或正在处理')
            if 'video_clock' not in meta:
                raise RecordingError('请先保存录制时间线')
            self._uploading.add(identity)
        return self._path(identity) / 'screen.upload'

    def finish_upload(self, identity, success):
        with self._lock:
            self._uploading.discard(identity)
            path = self._path(identity)
            temporary = path / 'screen.upload'
            if not success:
                temporary.unlink(missing_ok=True)
                return
            if not temporary.is_file() or not 100 <= temporary.stat().st_size <= MAX_VIDEO_BYTES:
                temporary.unlink(missing_ok=True)
                raise RecordingError('视频文件为空或超过96 MiB')
            temporary.replace(path / 'screen.webm')
            meta = self.describe(identity)
            meta.pop('files', None)
            meta['status'] = 'processing'
            meta.pop('export_error', None)
            self._save_meta(identity, meta)
            self._processing.add(identity)
        threading.Thread(target=self._mux, args=(identity,), daemon=True, name='demo-video-export').start()

    def _mux(self, identity):
        path = self._path(identity)
        try:
            meta = self.describe(identity)
            ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
            if not ffmpeg or not ffprobe:
                raise RecordingError('未找到FFmpeg/FFprobe；原始画面、四路声音和时间线已保留')
            if meta['audio_frames'] <= 0 or meta['audio_first_unix_ms'] is None:
                raise RecordingError('没有收到有效原始音频；原画面已保存')
            probe = self.runner([ffprobe, '-v', 'error', '-protocol_whitelist', 'file,pipe',
                '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'json', str(path / 'screen.webm')],
                capture_output=True, timeout=15, check=True)
            streams = json.loads(probe.stdout)['streams']
            if not streams or not 1 <= streams[0]['width'] <= 4096 or not 1 <= streams[0]['height'] <= 2160:
                raise RecordingError('录制画面格式不支持')
            left, right = (n - 1 for n in meta['playback_channels'])
            delta = (meta['audio_first_unix_ms'] - meta['video_clock']['started_unix_ms']) / 1000
            duration = min(MAX_SECONDS, meta['video_clock']['elapsed_ms'] / 1000)
            filters = [f'pan=stereo|c0=c{left}|c1=c{right}']
            if delta < 0:
                filters += [f'atrim=start={-delta:.6f}', 'asetpts=PTS-STARTPTS']
            else:
                filters += [f'adelay={round(delta * 1000)}:all=1']
            filters += ['apad', f'atrim=duration={duration:.6f}']
            command = [ffmpeg, '-hide_banner', '-nostdin', '-v', 'error', '-y',
                '-protocol_whitelist', 'file,pipe', '-i', str(path / 'screen.webm'),
                '-i', str(path / 'audio-4ch.wav'), '-map', '0:v:0', '-map', '1:a:0',
                '-vf', "setpts=PTS-STARTPTS,scale='min(1920,iw)':-2,fps=20", '-af', ','.join(filters),
                '-t', f'{duration:.6f}', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
                '-threads', '2', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k',
                '-map_metadata', '-1', '-movflags', '+faststart', str(path / 'demo.mp4')]
            self.runner(command, capture_output=True, timeout=120, check=True)
            meta.pop('files', None)
            meta.update(status='ready', audio_video_offset_ms=round(delta * 1000, 3), output_duration_limit_s=duration)
            with zipfile.ZipFile(path / 'bundle.writing', 'w', zipfile.ZIP_DEFLATED) as archive:
                for name in ('demo.mp4', 'screen.webm', 'audio-4ch.wav', 'timeline.jsonl'):
                    archive.write(path / name, name)
                archive.writestr('session.json', json.dumps(meta, ensure_ascii=False, indent=2))
            (path / 'bundle.writing').replace(path / 'bundle.zip')
            self._save_meta(identity, meta)
        except Exception as exc:
            with self._lock:
                meta = self.describe(identity)
                meta.pop('files', None)
                meta.update(status='export_error', export_error=str(exc)[:400])
                self._save_meta(identity, meta)
        finally:
            with self._lock:
                self._processing.discard(identity)

    def download(self, identity, kind):
        if kind not in DOWNLOADS:
            raise RecordingError('下载类型无效')
        if kind not in self.describe(identity)['files']:
            raise RecordingError('文件尚未完成')
        path = self._path(identity) / DOWNLOADS[kind][0]
        if not path.is_file() or path.is_symlink():
            raise RecordingError('文件尚未生成')
        return path, DOWNLOADS[kind][1]

    def shutdown(self):
        for session in list(self._sessions.values()):
            try:
                session.stop('server_shutdown')
            except RecordingError:
                pass
