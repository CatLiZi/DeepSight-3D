"""Capture callbacks cannot split PCM, sequence and timestamp observations."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from locator_demo.array_audio import WasapiArrayAudio
from locator_demo.audio import AudioDirection
from test_spectral_delay import CONFIG, signal


class AudioSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.audio = WasapiArrayAudio(ear_config=CONFIG)
        self.audio.capture.start = lambda: None
        self.audio.capture.metrics = lambda: dict(ok=True, sample_clock_valid=True)
        self.voice = SimpleNamespace(update=Mock(return_value=.9), reset=Mock())
        self.audio.voice = self.voice
        self.estimate = Mock(wraps=self.audio.localizer.estimate)
        self.audio.localizer.estimate = self.estimate
        self.pcm = signal(n=960)

    def feed(self, received, *, overflow=False, pcm=None):
        data = self.pcm if pcm is None else pcm
        with patch('locator_demo.capture.time.perf_counter', return_value=received):
            self.audio.capture._callback(data.tobytes(), len(data),
                SimpleNamespace(currentTime=1., inputBufferAdcTime=1.),
                SimpleNamespace(input_overflow=overflow))

    def read(self, now):
        with patch('locator_demo.array_audio.time.perf_counter', return_value=now):
            return self.audio.read_estimate()

    def advance_before_snapshot(self, action):
        recent = self.audio.capture.recent
        def read_window(seconds):
            action()
            return recent(seconds)
        self.audio.capture.recent = read_window

    def assert_matching_snapshot(self, result):
        latest = self.audio.capture.latest()
        end = latest.sample_start + latest.frame_count
        call = self.estimate.call_args
        self.assertEqual(call.kwargs['sample_end'], end)
        self.assertEqual(result.sample_sequence, latest.sequence)
        self.assertEqual(result.observed_monotonic, latest.received_monotonic)
        self.assertEqual(self.voice.update.call_args.kwargs['sample_start'], end-len(call.args[0]))

    def test_callback_advance_keeps_pcm_clock_and_metadata_together(self):
        for n in range(20): self.feed(10.+n*.02)
        self.advance_before_snapshot(lambda: self.feed(10.40))
        result = self.read(10.41)
        self.assert_matching_snapshot(result)
        np.testing.assert_array_equal(self.estimate.call_args.args[0][-960:], self.pcm)

    def test_first_window_cannot_produce_a_negative_vad_clock(self):
        self.feed(10.)
        self.advance_before_snapshot(lambda: self.feed(10.02))
        result = self.read(10.03)
        self.assert_matching_snapshot(result)
        self.assertEqual(self.voice.update.call_args.kwargs['sample_start'], 0)
        self.assertNotEqual(result.noise_state, 'invalid_sample_clock')

    def test_reopened_capture_snapshot_resets_old_vad_context(self):
        for n in range(12): self.feed(10.+n*.02)
        self.read(10.23)
        def reopen():
            self.audio.capture._history.clear()
            self.audio.capture._frames = self.audio.capture._sequence = 0
            self.feed(10.24)
        self.advance_before_snapshot(reopen)
        result = self.read(10.25)
        self.assert_matching_snapshot(result)
        self.assertEqual(result.sample_sequence, 1)
        self.voice.reset.assert_called_once()
        self.assertEqual(self.voice.update.call_args.kwargs['sample_start'], 0)

    def test_empty_restarted_buffer_does_not_reuse_old_sample(self):
        self.feed(10.)
        self.advance_before_snapshot(self.audio.capture._history.clear)
        result = self.read(10.03)
        self.assertEqual(result.direction, AudioDirection.UNKNOWN)
        self.assertEqual(result.noise_state, 'stale_audio')
        self.estimate.assert_not_called()
        self.voice.update.assert_not_called()

    def test_fresh_snapshot_replaces_a_previously_stale_tail(self):
        self.feed(9.)
        self.advance_before_snapshot(lambda: self.feed(10.))
        result = self.read(10.01)
        self.assert_matching_snapshot(result)
        self.assertNotEqual(result.noise_state, 'stale_audio')

    def test_overflow_in_new_snapshot_is_not_direction_evidence(self):
        self.feed(10.)
        self.advance_before_snapshot(lambda: self.feed(10.02, overflow=True))
        result = self.read(10.03)
        self.assertEqual(result.noise_state, 'audio_gap')
        self.assertEqual(result.sample_sequence, 2)
        self.assertEqual(result.observed_monotonic, 10.02)
        self.estimate.assert_not_called()
        self.voice.update.assert_not_called()

    def test_repeated_snapshot_does_not_duplicate_vad_or_doa_evidence(self):
        self.feed(10.)
        first = self.read(10.01)
        for _ in range(4): self.assertEqual(self.read(10.02), first)
        self.voice.update.assert_called_once()
        self.estimate.assert_called_once()

    def test_selected_ears_exclude_body_channels_during_callback_advance(self):
        data = self.pcm.copy();data[:, 0] = 32767;data[:, 3] = -32768
        self.feed(10., pcm=data)
        self.advance_before_snapshot(lambda: self.feed(10.02, pcm=data))
        result = self.read(10.03)
        self.assert_matching_snapshot(result)
        selected = self.voice.update.call_args.args[0]
        expected = np.concatenate((data, data))[:, [1, 2]]
        np.testing.assert_array_equal(selected, expected)


if __name__ == '__main__': unittest.main()
