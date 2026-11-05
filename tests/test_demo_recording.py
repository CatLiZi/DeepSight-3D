"""Local recording checks with synthetic PCM/video; no device or servo access."""
import io
import json
import math
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
import wave
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from locator_demo.capture import AudioSample
from locator_demo.demo_recording import DemoRecorder, RecordingError, RecordingSession, validate_timeline
from locator_demo.web.app import create_app


class FakeCapture:
    def __init__(self):
        self.zero = time.perf_counter()
        self.wall = time.time()
        self.reads = 0

    def metrics(self):
        return {'ok': True, 'channels': 4, 'sample_rate': 48000, 'sample_bits': 16, 'wasapi_raw': True, 'raw_stream_configured': True}

    def recent(self, _):
        self.reads += 1
        total = max(0, int((time.perf_counter() - self.zero) / .02))
        return [AudioSample(i + 1, bytes(960 * 8), i * 960, 960, 4, 48000,
                            self.zero + i * .02, self.wall + i * .02, self.zero + (i + 1) * .02)
                for i in range(max(0, total - 175), total)]


def wait_until(fn, timeout=3):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if fn():
            return
        time.sleep(.01)
    raise AssertionError('timed out')


class DemoAudioTests(unittest.TestCase):
    def test_recording_reuses_capture_and_finalizes_all_four_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = FakeCapture()
            recorder = DemoRecorder(Path(tmp), lambda: capture)
            result = recorder.start()
            session = recorder._sessions[result['id']]
            wait_until(lambda: session.meta['audio_frames'] >= 960)
            finished = recorder.stop(result['id'])
            self.assertEqual(finished['status'], 'awaiting_video')
            self.assertGreater(finished['audio_frames'], 0)
            with wave.open(str(session.directory / 'audio-4ch.wav')) as w:
                self.assertEqual((w.getnchannels(), w.getframerate(), w.getsampwidth()), (4, 48000, 2))
                self.assertEqual(w.getnframes(), finished['audio_frames'])
            self.assertGreater(capture.reads, 0)

    def test_source_change_ends_recording_without_opening_another_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = [FakeCapture()]
            recorder = DemoRecorder(Path(tmp), lambda: capture[0])
            identity = recorder.start()['id']
            capture[0] = FakeCapture()
            wait_until(lambda: not recorder._sessions[identity].worker.is_alive())
            result = recorder.describe(identity)
            self.assertEqual(result['stop_reason'], 'audio_error')
            self.assertIn('设备配置', result['error'])

    def test_no_ready_stream_does_not_create_a_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = DemoRecorder(Path(tmp) / 'recordings', lambda: None)
            with self.assertRaises(RecordingError):
                recorder.start()
            self.assertFalse(recorder.root.exists())

    def test_two_active_recorders_are_not_allowed_and_stop_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = FakeCapture()
            recorder = DemoRecorder(Path(tmp), lambda: capture)
            identity = recorder.start()['id']
            with self.assertRaises(RecordingError):
                recorder.start()
            first = recorder.stop(identity)
            second = recorder.stop(identity)
            self.assertEqual(first['stop_reason'], second['stop_reason'])

    def test_missing_blocks_are_padded_and_duplicate_blocks_are_not_repeated(self):
        session = RecordingSession.__new__(RecordingSession)
        session.started_mono = 10.
        session._last_received = session._last_sequence = session._sample_end = None
        session.meta = {'audio_frames': 0, 'missing_frames_padded': 0, 'overflow_blocks': 0}
        session._wav = wave.open(io.BytesIO(), 'wb')
        session._wav.setparams((4, 2, 48000, 0, 'NONE', 'not compressed'))
        first = AudioSample(1, bytes(80), 0, 10, 4, 48000, 10., 100., 10.01)
        gap = AudioSample(3, bytes(80), 20, 10, 4, 48000, 10.02, 100.02, 10.03, True)
        session._consume([first, first, gap], 11.)
        self.assertEqual(session.meta['audio_frames'], 30)
        self.assertEqual(session.meta['missing_frames_padded'], 10)
        self.assertEqual(session.meta['overflow_blocks'], 1)
        reset = AudioSample(1, bytes(80), 0, 10, 4, 48000, 10.04, 100.04, 10.05)
        with self.assertRaisesRegex(RecordingError, '时钟回退'):
            session._consume([reset], 11.)
        session._wav.close()

    def test_watchdog_closes_audio_without_a_browser_stop(self):
        with tempfile.TemporaryDirectory() as tmp, patch('locator_demo.demo_recording.MAX_SECONDS', .08):
            capture = FakeCapture()
            recorder = DemoRecorder(Path(tmp), lambda: capture)
            identity = recorder.start()['id']
            wait_until(lambda: not recorder._sessions[identity].worker.is_alive())
            self.assertEqual(recorder.describe(identity)['stop_reason'], 'duration_limit')


class DemoTimelineTests(unittest.TestCase):
    def test_export_status_read_waits_for_atomic_metadata_replacement(self):
        import locator_demo.demo_recording as module
        with tempfile.TemporaryDirectory() as tmp:
            identity = uuid.uuid4().hex
            directory = Path(tmp) / identity
            directory.mkdir()
            module.write_json(directory/'session.json', {'id':identity,'status':'processing'})
            recorder = DemoRecorder(Path(tmp), lambda: None)
            entered, release, reading, finished = (threading.Event() for _ in range(4))
            result = []
            real_write = module.write_json
            def delayed_write(path, value):
                entered.set()
                if not release.wait(2): raise AssertionError('writer not released')
                real_write(path, value)
            def read():
                reading.set()
                result.append(recorder.describe(identity))
                finished.set()
            with patch.object(module, 'write_json', side_effect=delayed_write):
                writer=threading.Thread(target=recorder._save_meta,args=(identity,{'id':identity,'status':'ready'}))
                writer.start()
                self.assertTrue(entered.wait(1))
                reader=threading.Thread(target=read)
                reader.start()
                self.assertTrue(reading.wait(1))
                try:
                    self.assertFalse(finished.wait(.05))
                finally:
                    release.set()
                    writer.join(2)
                    reader.join(2)
            self.assertTrue(finished.is_set())
            self.assertEqual(result[0]['status'],'ready')

    def payload(self):
        return {'video': {'started_unix_ms': time.time() * 1000, 'elapsed_ms': 1500},
                'events': [{'t_ms': 0, 'kind': 'state', 'target_id': 2}]}

    def test_no_images_or_arbitrary_top_level_payload(self):
        payload = self.payload()
        payload['events'][0]['frame'] = 'private-image'
        with self.assertRaises(RecordingError):
            validate_timeline(payload)

    def test_invalid_times_and_nonfinite_fields_rejected(self):
        for field in ('started_unix_ms', 'elapsed_ms'):
            payload = self.payload()
            payload['video'][field] = float('nan')
            with self.assertRaises(RecordingError):
                validate_timeline(payload)
        payload = self.payload()
        payload['events'][0]['t_ms'] = -1
        with self.assertRaises(RecordingError):
            validate_timeline(payload)

    def test_out_of_order_or_outside_recording_markers_are_rejected(self):
        payload = self.payload()
        payload['events'] = [{'t_ms': 500, 'kind': 'state'}, {'t_ms': 200, 'kind': 'state'}]
        with self.assertRaises(RecordingError):
            validate_timeline(payload)
        payload['events'] = [{'t_ms': 10000, 'kind': 'observer_label', 'speaker': 'A'}]
        with self.assertRaises(RecordingError):
            validate_timeline(payload)

    def test_in_progress_video_is_not_offered_as_a_finished_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            identity = uuid.uuid4().hex
            directory = Path(tmp) / identity
            directory.mkdir()
            (directory / 'session.json').write_text(json.dumps({'id': identity, 'status': 'processing'}))
            (directory / 'demo.mp4').write_bytes(b'incomplete')
            recorder = DemoRecorder(Path(tmp), lambda: None)
            recorder._processing.add(identity)
            self.assertEqual(recorder.describe(identity)['status'], 'processing')
            self.assertNotIn('video', recorder.describe(identity)['files'])
            with self.assertRaises(RecordingError):
                recorder.download(identity, 'video')

    def test_download_path_cannot_escape_the_session_directory(self):
        recorder = DemoRecorder(Path('not-created'), lambda: None)
        for identity in ('../other', 'A' * 32, '0' * 31, '0' * 32 + '/..'):
            with self.assertRaises(RecordingError):
                recorder.describe(identity)


class DemoRecordingApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app(simulated=True, settings_path=Path(self.tmp.name) / 'settings.json')
        self.runtime = self.app.state.runtime
        self.runtime.audio_capture = replace(self.runtime.audio_capture, ear_left_channel=2, ear_right_channel=3)
        self.addCleanup(self.runtime.shutdown)
        self.capture = FakeCapture()
        self.runtime.demo_recorder.capture_getter = lambda: self.capture

    def test_start_stop_are_independent_of_robot_running_state(self):
        self.runtime.running = False
        with TestClient(self.app) as client:
            identity = client.post('/api/demo-recordings/start').json()['id']
            self.assertFalse(self.runtime.running)
            response = client.post(f'/api/demo-recordings/{identity}/stop')
            self.assertEqual(response.status_code, 200)
            self.assertFalse(self.runtime.running)
            self.assertEqual(self.runtime.bus.moves, [])

    def test_cross_origin_cannot_start_recording(self):
        with TestClient(self.app) as client:
            response = client.post('/api/demo-recordings/start', headers={'Origin': 'https://outside.example'})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.runtime.demo_recorder._sessions, {})

    def test_failed_oversized_upload_keeps_wav_and_removes_partial_video(self):
        with TestClient(self.app) as client:
            session = client.post('/api/demo-recordings/start').json()
            identity = session['id']
            client.post(f'/api/demo-recordings/{identity}/stop')
            payload = {'video': {'started_unix_ms': session['started_unix_ms'], 'elapsed_ms': 1200}, 'events': []}
            self.assertEqual(client.post(f'/api/demo-recordings/{identity}/timeline', json=payload).status_code, 200)
            with patch('locator_demo.web.demo_recording.MAX_VIDEO_BYTES', 100):
                response = client.put(f'/api/demo-recordings/{identity}/video', content=bytes(101), headers={'Content-Type': 'video/webm'})
            self.assertEqual(response.status_code, 413)
            directory = self.runtime.demo_recorder._path(identity)
            self.assertFalse((directory / 'screen.upload').exists())
            self.assertTrue((directory / 'audio-4ch.wav').exists())


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg tools required')
class DemoMuxIntegrationTests(unittest.TestCase):
    def test_actual_mp4_has_synced_stereo_from_the_two_ear_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = uuid.uuid4().hex
            directory = root / identity
            directory.mkdir()
            source = root / 'synthetic.webm'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=teal:s=320x180:r=10:d=1.2',
                            '-an', '-c:v', 'libvpx', '-y', str(source)], check=True, capture_output=True)
            t = np.arange(48000) / 48000
            data = np.stack([np.zeros_like(t), .15*np.sin(2*np.pi*300*t), .15*np.sin(2*np.pi*700*t),
                             .15*np.sin(2*np.pi*1100*t)], axis=1)
            with wave.open(str(directory / 'audio-4ch.wav'), 'wb') as wav:
                wav.setparams((4, 2, 48000, 0, 'NONE', 'not compressed'))
                wav.writeframes((data*32767).astype('<i2').tobytes())
            start = time.time() * 1000
            meta = {'id': identity, 'status': 'awaiting_video', 'started_unix_ms': start,
                    'stopped_unix_ms': start+1200, 'audio_first_unix_ms': start+200,
                    'audio_frames': 48000, 'playback_channels': [2, 3], 'error': ''}
            (directory / 'session.json').write_text(json.dumps(meta), encoding='utf-8')
            recorder = DemoRecorder(root, lambda: None)
            recorder.save_timeline(identity, {'video': {'started_unix_ms': start, 'elapsed_ms': 1200}, 'events': []})
            target = recorder.begin_upload(identity)
            shutil.copyfile(source, target)
            recorder.finish_upload(identity, True)
            wait_until(lambda: recorder.describe(identity)['status'] in ('ready', 'export_error'), timeout=15)
            result = recorder.describe(identity)
            self.assertEqual(result['status'], 'ready', result)
            info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(directory/'demo.mp4')]))
            self.assertEqual({s['codec_type'] for s in info['streams']}, {'video', 'audio'})
            audio = next(s for s in info['streams'] if s['codec_type'] == 'audio')
            self.assertEqual(audio['channels'], 2)
            raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(directory/'demo.mp4'), '-f', 's16le', '-ac', '2', '-ar', '48000', '-'])
            values = np.frombuffer(raw, dtype='<i2').reshape(-1, 2).astype(float)
            self.assertLess(np.max(np.abs(values[:6000])), 100)
            signal = values[15000:45000]
            peaks = np.argmax(abs(np.fft.rfft(signal, axis=0)), axis=0) * 48000 / len(signal)
            np.testing.assert_allclose(peaks, [300, 700], atol=3)
            self.assertTrue((directory/'bundle.zip').is_file())


if __name__ == '__main__':
    unittest.main()
