import math
import struct
import tempfile
import unittest
from pathlib import Path

from locator_demo.audio import AudioDirection, classify_audio_direction
from locator_demo.audio_device import FfmpegAudioLocalizer


class BlockingStdout:
    def read(self, _size):
        raise AssertionError("read_estimate must not block on ffmpeg stdout")


class FakeProcess:
    stdout = BlockingStdout()
    stderr = None

    def poll(self):
        return None


class AudioDeviceTests(unittest.TestCase):
    def test_read_estimate_uses_cached_background_result(self):
        localizer = FfmpegAudioLocalizer()
        localizer.start = lambda: None
        localizer._process = FakeProcess()

        estimate = localizer.read_estimate()

        self.assertEqual(estimate.direction, AudioDirection.UNKNOWN)

    def test_runtime_audio_is_buffered_and_saved_on_stop_without_denoise(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            class FakeStdout:
                def __init__(self):
                    self._yielded = False

                def read(self, _size):
                    if self._yielded:
                        return b""
                    self._yielded = True
                    return b"\x00\x01\x00\x01" * 40

            class FakeProcessWithStdout:
                def __init__(self, stdout):
                    self.stdout = stdout
                    self.stderr = None
                    self._poll_count = 0

                def poll(self):
                    self._poll_count += 1
                    return 0 if self._poll_count > 1 else None

            localizer = FfmpegAudioLocalizer(denoise=False, denoise_output_dir=tmpdir, recording_enabled=True)
            localizer._process = FakeProcessWithStdout(FakeStdout())

            localizer._read_loop()
            localizer.stop()

            files = sorted(Path(tmpdir).glob("*.wav"))
            self.assertEqual(1, len(files))
            self.assertGreater(files[0].stat().st_size, 0)

    def test_channel_level_bias_detects_close_side_speech(self):
        localizer = FfmpegAudioLocalizer()
        samples = []
        for i in range(160):
            wave = math.sin(i / 160.0 * math.tau * 4)
            samples.extend([int(wave * 26000), int(wave * 3000)])
        raw = struct.pack("<" + "h" * len(samples), *samples)

        estimate = localizer._estimate_from_raw(raw)

        self.assertEqual(estimate.direction, AudioDirection.LEFT)

    def test_low_voiced_speech_keeps_vad_confidence(self):
        localizer = FfmpegAudioLocalizer()
        sample_rate = localizer.sample_rate
        left = []
        right = []
        for i in range(int(sample_rate * 0.04)):
            value = 0.8 * math.sin(2 * math.pi * 160 * i / sample_rate)
            value += 0.35 * math.sin(2 * math.pi * 600 * i / sample_rate)
            value += 0.20 * math.sin(2 * math.pi * 1800 * i / sample_rate)
            left.append(value * 0.2)
            right.append(value * 0.2)

        confidence, noise_state = localizer._speech_confidence(left, right, energy=0.8)

        self.assertGreater(confidence, 0.35)
        self.assertEqual(noise_state, "voiced_speech")

    def test_srp_path_reports_front_azimuth(self):
        localizer = FfmpegAudioLocalizer()
        pulse = [0] * 64 + [24000] + [0] * 64
        left = [0] * 4 + pulse
        right = pulse + [0] * 4
        samples = []
        for l_value, r_value in zip(left, right):
            samples.extend([l_value, r_value])
        raw = struct.pack("<" + "h" * len(samples), *samples)

        estimate = localizer._estimate_from_raw(raw)

        self.assertEqual(estimate.direction, AudioDirection.LEFT)
        self.assertGreater(estimate.azimuth_deg, 0.0)
        self.assertGreater(estimate.doa_confidence, 0.12)

    def test_raw_direction_is_preferred_when_denoised_confirms_human_voice(self):
        localizer = FfmpegAudioLocalizer(denoise=True, use_denoised_for_localization=True)

        raw = classify_audio_direction(
            -0.00035,
            0.8,
            min_energy=0.08,
            speech_confidence=0.8,
            doa_confidence=0.9,
            noise_state="voiced_speech",
        )
        denoised = classify_audio_direction(
            0.00035,
            0.8,
            min_energy=0.08,
            speech_confidence=0.8,
            doa_confidence=0.9,
            noise_state="voiced_speech",
        )

        result = localizer._human_voice_double_check(raw, denoised)

        self.assertEqual(result.direction, AudioDirection.LEFT)
        self.assertEqual(result.noise_state, "voiced_speech")

    def test_single_wav_is_written_when_speech_is_located(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            localizer = FfmpegAudioLocalizer(denoise_output_dir=tmpdir, recording_enabled=True)
            estimate = classify_audio_direction(
                0.00035,
                0.8,
                min_energy=0.08,
                speech_confidence=0.8,
                doa_confidence=0.9,
                noise_state="voiced_speech",
            )

            localizer._append_recording_chunk(b"\x01\x02" * 160, estimate)

            files = sorted(Path(tmpdir).glob("*.wav"))
            self.assertEqual(1, len(files))
            self.assertGreater(files[0].stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
