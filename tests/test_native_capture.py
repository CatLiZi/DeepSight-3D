import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

import cv2
import numpy as np

from locator_demo.capture import JpegParser, StereoCameraCapture, WasapiAudioCapture, jpeg_dimensions
from locator_demo.devices import DshowDevice, choose_dshow_device, choose_wasapi_input, usb_identity
from locator_demo.servo_bus import SerialPortCandidate, ZPServoBus, choose_servo_port
from locator_demo.stereo_vision import StereoVisionTracker


def jpeg(width=128, height=36):
    ok, data = cv2.imencode('.jpg', np.full((height, width, 3), 87, np.uint8))
    assert ok
    return data.tobytes()


class NativeCaptureTests(unittest.TestCase):
    def test_parser_handles_split_markers_and_multiple_frames(self):
        data = jpeg()
        parser = JpegParser()
        found = []
        stream = b'noise' + data + data
        for i in range(0, len(stream), 7):
            found += parser.feed(stream[i:i + 7])
        self.assertEqual(found, [data, data])
        self.assertEqual(jpeg_dimensions(found[0]), (128, 36))

    def test_camera_rejects_wrong_resolution_and_reports_receipt_rate(self):
        capture = StereoCameraCapture(width=128, height=36)
        now = time.perf_counter()
        capture._accept(jpeg(), now - .06)
        capture._accept(jpeg(), now - .03)
        capture._accept(jpeg(), now)
        capture._accept(jpeg(64, 36), now)
        metrics = capture.metrics()
        self.assertEqual(metrics['frames'], 3)
        self.assertEqual(metrics['invalid_frames'], 1)
        self.assertAlmostEqual(metrics['measured_fps'], 33.333, places=2)
        self.assertEqual(capture.latest().width, 128)

    def test_camera_command_copies_native_mjpeg_without_fps_filter(self):
        capture = StereoCameraCapture()
        device = DshowDevice('camera', 'video', 'stable-usb-path')
        command = capture.command(device)
        self.assertEqual(command[command.index('-c:v') + 1], 'copy')
        self.assertEqual(command[command.index('-video_size') + 1], '3840x1080')
        self.assertEqual(command[command.index('-framerate') + 1], '60.0')
        self.assertIn('video=stable-usb-path', command)
        self.assertNotIn('-vf', command)
        self.assertNotIn('-r', command)

    def test_usb_selection_never_falls_back_to_unrelated_camera(self):
        devices = [DshowDevice('other', 'video', 'usb#vid_9999&pid_0001'),
                   DshowDevice('renamed camera', 'video', 'usb#vid_1bcf&pid_0b15')]
        with patch('locator_demo.devices.list_dshow_devices', return_value=devices):
            selected = choose_dshow_device('video', ['USB Video Device'], usb_vid='1BCF', usb_pid='0B15')
            self.assertEqual(selected.name, 'renamed camera')
            self.assertIsNone(choose_dshow_device('video', [], usb_vid='1234', usb_pid='5678'))
        self.assertEqual(usb_identity('USB VID:PID=EF09:459D'), ('EF09', '459D'))

    def test_microphone_com_is_blocked_even_if_explicitly_selected(self):
        mic = SerialPortCandidate('COM4', 'USB serial', 'USB VID:PID=EF09:459D')
        with patch('locator_demo.servo_bus.list_serial_ports', return_value=[mic]):
            self.assertIsNone(choose_servo_port('COM4'))
            with patch('locator_demo.servo_bus._default_serial_factory') as factory:
                with self.assertRaisesRegex(RuntimeError, 'microphone'):
                    ZPServoBus('COM4').connect()
                factory.assert_not_called()

    def test_unknown_serial_device_is_not_automatically_selected(self):
        with patch('locator_demo.servo_bus.list_serial_ports', return_value=[SerialPortCandidate('COM7', 'Bluetooth', '')]):
            self.assertIsNone(choose_servo_port())
            self.assertEqual(choose_servo_port('COM7'), 'COM7')

    def test_wasapi_selects_four_channels_and_rejects_ambiguity(self):
        module = SimpleNamespace(query_hostapis=lambda: [{'name': 'MME'}, {'name': 'Windows WASAPI'}],
                                 query_devices=lambda: [dict(name='YDPI4MIC', hostapi=0, max_input_channels=2, default_samplerate=44100),
                                                        dict(name='Microphone YDPI4MIC', hostapi=1, max_input_channels=4, default_samplerate=48000)])
        device = choose_wasapi_input('YDPI4MIC', audio_module=module)
        self.assertEqual(device['index'], 1)
        with self.assertRaisesRegex(RuntimeError, 'not present'):
            choose_wasapi_input('YDPI4MIC', usb_vid='EF09', usb_pid='459D', usb_interfaces=[], audio_module=module)
        module.query_devices = lambda: [dict(name='YDPI4MIC', hostapi=1, max_input_channels=4, default_samplerate=48000)] * 2
        with self.assertRaisesRegex(RuntimeError, 'found 2'):
            choose_wasapi_input('YDPI4MIC', audio_module=module)

    def test_four_channel_pcm_and_adc_timestamp_are_preserved(self):
        capture = WasapiAudioCapture()
        values = np.tile(np.array([1000, 2000, 3000, 4000], dtype='<i2'), (960, 1))
        clock = SimpleNamespace(currentTime=5., inputBufferAdcTime=4.98)
        capture._callback(values.tobytes(), 960, clock, SimpleNamespace(input_overflow=True))
        sample = capture.latest()
        self.assertEqual(sample.pcm, values.tobytes())
        self.assertEqual((sample.channels, sample.sample_rate, sample.frame_count, sample.sample_start), (4, 48000, 960, 0))
        self.assertAlmostEqual(sample.received_monotonic - sample.captured_monotonic, .02, places=5)
        self.assertEqual(capture.metrics()['overflow_count'], 1)
        np.testing.assert_allclose(capture.metrics()['channel_rms'], np.array([1000, 2000, 3000, 4000]) / 32768, rtol=1e-6)

    def test_four_channel_wav_streaming_and_recent_export(self):
        capture = WasapiAudioCapture()
        raw = np.tile(np.array([100, -200, 300, -400], dtype='<i2'), (960, 1)).tobytes()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'full.wav'
            capture.start_recording(path)
            for i in range(10):
                capture._callback(raw, 960, SimpleNamespace(currentTime=i * .02, inputBufferAdcTime=i * .02), SimpleNamespace(input_overflow=False))
            capture.stop_recording()
            with wave.open(str(path), 'rb') as recording:
                self.assertEqual((recording.getnchannels(), recording.getframerate(), recording.getsampwidth(), recording.getnframes()), (4, 48000, 2, 9600))
                self.assertEqual(recording.readframes(9600), raw * 10)
            recent = Path(directory) / 'recent.wav'
            capture.save_recent(recent, seconds=.05)
            with wave.open(str(recent), 'rb') as recording:
                self.assertEqual(recording.getnframes(), 2400)
                self.assertEqual(recording.readframes(2400), (raw * 10)[-2400 * 8:])

    def test_slow_inference_does_not_block_native_stereo_acceptance(self):
        tracker = StereoVisionTracker(video_size='128x36', output_width=320)
        tracker.capture.start = lambda: None
        entered, release = threading.Event(), threading.Event()
        def detect(_jpeg):
            entered.set()
            release.wait(timeout=3)
            return []
        tracker._detect_image_targets = detect
        # A flat image is now rejected before inference. Use a textured scene
        # so this still exercises a genuinely blocked detector.
        data = cv2.imencode('.jpg', np.random.default_rng(9).integers(30, 220, (36, 128, 3), dtype=np.uint8))[1].tobytes()
        tracker.capture._accept(data)
        tracker._start()
        try:
            self.assertTrue(entered.wait(timeout=3))
            for _ in range(20):
                tracker.capture._accept(data)
            frame = tracker.read()
            self.assertEqual(frame.capture_sequence, 21)
            self.assertEqual(frame.detection_sequence, 0)
            self.assertTrue(frame.stereo_jpeg_base64)
        finally:
            release.set()
            tracker.stop()


if __name__ == '__main__':
    unittest.main()
