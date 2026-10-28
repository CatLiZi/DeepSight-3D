"""Hardware-upgrade contracts: real four-channel DSP and consistent actuation."""
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import numpy as np
from fastapi.testclient import TestClient

from locator_demo.array_audio import WasapiArrayAudio
from locator_demo.audio import AudioDirection, AudioEstimate
from locator_demo.audio_device import FfmpegAudioLocalizer
from locator_demo.capture import AudioSample
from locator_demo.ear_localization import EarPairConfig
from locator_demo.head import DirectionConfig, HeadControllerLogic, HeadPose
from locator_demo.settings import AudioCaptureConfig
from locator_demo.visual_priority import progressive_visual_step
from locator_demo.web.app import create_app, DemoRuntime, RuntimeConfig


EAR = EarPairConfig('ear_pair', 3, 2, True, .13176241517)
LIMITS = dict(yaw_deadband=.02, pitch_deadband=.02, min_yaw_delta=0,
              min_pitch_delta=0, max_yaw_delta=30, max_pitch_delta=30)


def four_channel_signal(n=4608):
    rng = np.random.default_rng(203)
    x = rng.normal(size=n+20)
    freq = np.fft.rfftfreq(len(x), 1/48000)
    x = np.fft.irfft(np.fft.rfft(x)*((freq > 250) & (freq < 5000)), n=len(x))
    x *= 4000 / np.std(x)
    data = np.zeros((n, 4), '<i2')
    data[:, 2] = x[10:n+10]
    data[:, 1] = x[1:n+1]
    data[:, 0] = 32767
    data[:, 3] = -32768
    return data


def feed(audio, data, overflow=False):
    audio.capture._callback(data.tobytes(), len(data),
        SimpleNamespace(currentTime=1., inputBufferAdcTime=1.),
        SimpleNamespace(input_overflow=overflow))


class FourChannelRepairTests(unittest.TestCase):
    def test_new_array_profile_refuses_legacy_downmix(self):
        with self.assertRaisesRegex(ValueError, 'WASAPI RAW'):
            AudioCaptureConfig(backend='legacy')

    def test_new_array_profile_refuses_processed_shared_mode(self):
        with self.assertRaisesRegex(ValueError, 'WASAPI RAW'):
            AudioCaptureConfig(wasapi_raw=False)

    def test_array_adapter_cannot_reshape_stereo_as_four_channels(self):
        for kwargs in (dict(channels=2), dict(sample_rate=16000), dict(wasapi_raw=False)):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                WasapiArrayAudio(**kwargs)

    def test_legacy_route_keeps_explicit_device_and_recording_preference(self):
        runtime = DemoRuntime(RuntimeConfig(simulated=True))
        runtime.config = replace(runtime.config, simulated=False)
        runtime.audio_capture = AudioCaptureConfig(backend='legacy', device_name='YDM2MIC', usb_vid='', usb_pid='')
        runtime.audio_processing = replace(runtime.audio_processing, recording_enabled=False)
        with patch('locator_demo.web.app.FfmpegAudioLocalizer') as factory:
            runtime._make_audio()
        self.assertEqual(factory.call_args.kwargs['device_name'], 'YDM2MIC')
        self.assertFalse(factory.call_args.kwargs['recording_enabled'])

    def test_legacy_device_capture_refuses_four_mic_board(self):
        capture = FfmpegAudioLocalizer(device_name='Microphone (YDPI4MIC)')
        with patch('locator_demo.audio_device.subprocess.Popen') as process:
            capture.start()
        process.assert_not_called()
        self.assertIn('WASAPI RAW', capture.status().message)

    def test_four_channel_adapter_sends_only_ch3_ch2_to_vad(self):
        audio = WasapiArrayAudio(ear_config=EAR)
        audio.capture.start = lambda: None
        calls = []
        audio.voice = SimpleNamespace(update=lambda data, **kw: calls.append(data.copy()) or .9)
        data = four_channel_signal()
        feed(audio, data)
        estimate = audio.read_estimate()
        self.assertEqual(estimate.direction, AudioDirection.LEFT)
        self.assertEqual(estimate.speech_backend, 'silero_onnx_cpu')
        np.testing.assert_array_equal(calls[0], data[:, [2, 1]])
        audio.read_estimate()
        self.assertEqual(len(calls), 1, 'cached audio is not new speech evidence')

    def test_body_noise_cannot_create_voice_activity(self):
        audio = WasapiArrayAudio(ear_config=EAR)
        audio.capture.start = lambda: None
        data = four_channel_signal()
        data[:, [1, 2]] = 0
        feed(audio, data)
        result = audio.read_estimate()
        self.assertEqual(result.direction, AudioDirection.UNKNOWN)
        self.assertEqual(result.noise_state, 'quiet')
        self.assertFalse(audio.localization_ready)

    def test_vad_silence_clears_the_previous_delay_history(self):
        audio = WasapiArrayAudio(ear_config=EAR)
        audio.capture.start = lambda: None
        data = four_channel_signal()
        feed(audio, data)
        self.assertEqual(audio.read_estimate().direction, AudioDirection.LEFT)
        self.assertTrue(audio.localizer._delays)
        audio.voice = SimpleNamespace(update=lambda *a, **k: .01)
        feed(audio, data)
        result = audio.read_estimate()
        self.assertEqual(result.noise_state, 'no_voice_activity')
        self.assertFalse(audio.localizer._delays)
        self.assertFalse(audio.localization_ready)

    def test_overflow_audio_is_retained_raw_but_excluded_from_localization(self):
        audio = WasapiArrayAudio(ear_config=EAR)
        audio.capture.start = lambda: None
        data = four_channel_signal()
        feed(audio, data, overflow=True)
        self.assertEqual(audio.capture.latest().pcm, data.tobytes())
        self.assertEqual(audio.read_estimate().noise_state, 'audio_gap')
        self.assertEqual(audio.recent_mono_audio()[1], [])

    def test_reopened_audio_stream_resets_vad_and_reprocesses_its_pcm(self):
        audio = WasapiArrayAudio(ear_config=EAR)
        audio.capture.start = lambda: None
        resets, calls = [], []
        audio.voice = SimpleNamespace(update=lambda data, **kw: calls.append(kw['sample_start']) or .9,
                                      reset=lambda: resets.append(True))
        data = four_channel_signal()
        feed(audio, data)
        feed(audio, data)
        self.assertEqual(audio.read_estimate().direction, AudioDirection.LEFT)
        audio.capture._history.clear()
        audio.capture._sequence = audio.capture._frames = 0
        feed(audio, data)
        result = audio.read_estimate()
        self.assertEqual(result.direction, AudioDirection.LEFT)
        self.assertEqual(resets, [True])
        # The 192 ms analysis context includes both initial blocks. A reopened
        # stream starts again at zero and cannot reuse the old VAD PCM clock.
        self.assertEqual(calls, [0, 0])

    def test_reopened_stream_with_reused_sequence_is_not_a_cached_old_frame(self):
        audio = WasapiArrayAudio(ear_config=EAR)
        audio.capture.start = lambda: None
        data = four_channel_signal()
        feed(audio, data)
        audio.read_estimate()
        audio.capture._history.clear()
        audio.capture._sequence = audio.capture._frames = 0
        # Same sequence number as the old capture, different PCM and timestamp.
        data[:, [1, 2]] = 0
        feed(audio, data)
        self.assertEqual(audio.read_estimate().noise_state, 'quiet')

    def test_audio_profile_api_rejects_unsafe_fallback(self):
        with TestClient(create_app(simulated=True)) as client:
            for kwargs in (dict(backend='legacy'), dict(wasapi_raw=False), dict(channels=2)):
                with self.subTest(kwargs=kwargs):
                    response = client.post('/api/config/audio-capture', json=kwargs)
                    self.assertEqual(response.status_code, 422)

    def test_sample_clock_guard_checks_received_pcm_not_just_configured_rate(self):
        for rate in (48000, 32000):
            with self.subTest(received_rate=rate):
                audio = WasapiArrayAudio(ear_config=EAR)
                audio.capture._stream = SimpleNamespace(active=True)
                now = time.perf_counter()
                for second in range(3):
                    audio.capture._history.append(AudioSample(second+1, b'', second*rate,
                        960, 4, 48000, now-2+second, 0., now-2+second))
                metrics = audio.capture.metrics()
                self.assertAlmostEqual(metrics['observed_sample_rate_hz'], rate, delta=2)
                self.assertEqual(metrics['sample_clock_valid'], rate == 48000)
                self.assertEqual(metrics['ok'], rate == 48000)

    def test_bad_pcm_clock_cannot_reach_the_direction_or_vad_algorithms(self):
        audio = WasapiArrayAudio(ear_config=EAR)
        audio.capture.start = lambda: None
        audio.capture.metrics = lambda: dict(ok=False, sample_clock_valid=False)
        events = []
        audio.diagnostic_sink = lambda event, detail: events.append((event, detail))
        with patch.object(audio.localizer, 'estimate') as estimate:
            result = audio.read_estimate()
            audio.read_estimate()
        self.assertEqual(result.direction, AudioDirection.UNKNOWN)
        self.assertEqual(result.noise_state, 'sample_clock_mismatch')
        estimate.assert_not_called()
        self.assertEqual([e[0] for e in events], ['audio_capture_fault'])
        audio.capture.metrics = lambda: dict(ok=False, sample_clock_valid=True)
        audio.read_estimate()
        self.assertEqual([e[0] for e in events], ['audio_capture_fault', 'audio_capture_recovered'])


class ControlConventionRepairTests(unittest.TestCase):
    def test_all_visual_and_progressive_directions_match_manual_axes(self):
        for yaw_sign in (-1, 1):
            for pitch_sign in (-1, 1):
                logic = HeadControllerLogic(direction=DirectionConfig(yaw_sign, pitch_sign))
                for x, y, h, v in ((-.5, -.5, 'left', 'up'), (.5, .5, 'right', 'down')):
                    with self.subTest(yaw=yaw_sign, pitch=pitch_sign, x=x, y=y):
                        start = HeadPose(1500, 1500)
                        manual = HeadPose(logic.jog(start, h, 30).yaw, logic.jog(start, v, 30).pitch)
                        general = logic.apply_visual_error(start, x, y, max_yaw_delta=30, max_pitch_delta=30)
                        progressive, _ = progressive_visual_step(logic, start, x, y, LIMITS, measured=(x, y))
                        self.assertEqual(general, manual)
                        self.assertEqual(progressive, manual)

    def test_new_path_keeps_pre_repair_live_direction_for_both_mirror_settings(self):
        logic = HeadControllerLogic(direction=DirectionConfig(-1, 1))
        for mirror in (False, True):
            error = -.5 if mirror else .5
            result, _ = progressive_visual_step(logic, HeadPose(1500, 1553), error, 0., LIMITS, measured=(error, 0.))
            self.assertEqual(result.yaw, 1470 if mirror else 1530)


class LegacyPipeRepairTests(unittest.TestCase):
    def test_short_pipe_reads_and_eof_tail_are_bit_exact_in_wav(self):
        pcm = b'\x00\x01\x00\x01' * 680
        class FragmentedPipe(io.BytesIO):
            def read(self, size):
                return super().read(min(size, 73))
        with tempfile.TemporaryDirectory() as directory:
            localizer = FfmpegAudioLocalizer(denoise_output_dir=directory, recording_enabled=True)
            localizer._process = SimpleNamespace(stdout=FragmentedPipe(pcm), stderr=None, returncode=0, poll=lambda: 0)
            try:
                localizer._read_loop()
            finally:
                localizer.stop()
            files = list(Path(directory).glob('*.wav'))
            self.assertEqual(len(files), 1, 'disabled denoise must not create a duplicate recording')
            with wave.open(str(files[0]), 'rb') as recording:
                self.assertEqual(recording.getnframes(), 680)
                self.assertEqual(recording.readframes(680), pcm)

    def test_recording_toggle_flushes_and_does_not_reuse_the_same_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            localizer = FfmpegAudioLocalizer(denoise_output_dir=directory)
            estimate = AudioEstimate(AudioDirection.UNKNOWN, 0, 0, 0)
            with patch('locator_demo.audio_device.time.time_ns', return_value=42):
                for _ in range(2):
                    localizer.set_recording_enabled(True)
                    localizer._append_recording_chunk(b'\0' * 160, estimate)
                    localizer.set_recording_enabled(False)
            localizer.stop()
            self.assertEqual(len(list(Path(directory).glob('*.wav'))), 2)
            self.assertTrue(all(item['frames_written'] == 40 and not item['error']
                                for item in localizer._last_recordings))


if __name__ == '__main__':
    unittest.main()
