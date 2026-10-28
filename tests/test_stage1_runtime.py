import tempfile
from dataclasses import replace
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from locator_demo.array_audio import WasapiArrayAudio
from locator_demo.audio import AudioDirection, AudioEstimate
from locator_demo.audio_device import AudioDeviceStatus
from locator_demo.camera import VisionFrameResult, VisionStatus
from locator_demo.head import HeadPose
from locator_demo.settings import AudioCaptureConfig, CameraConfig, ControlConfig, SavedSettings, save_settings, load_settings
from locator_demo.web.app import create_app, DemoRuntime, RuntimeConfig
from locator_demo.vision import FaceTarget
from types import SimpleNamespace


class FakeAudio:
    def __init__(self, **kwargs):
        self.calls = 0
        self.stopped = False
    def read_estimate(self):
        self.calls += 1
        return AudioEstimate(AudioDirection.UNKNOWN, 0, 0, .1)
    def status(self):
        return AudioDeviceStatus(True, 'fake 4ch', 'capturing')
    def capture_metrics(self):
        return {'channels': 4, 'sample_rate': 48000, 'sample_bits': 16}
    def stop(self):
        self.stopped = True


class FakeVision:
    def __init__(self, **kwargs):
        self.calls = 0
        self.stopped = False
    def read(self):
        self.calls += 1
        return VisionFrameResult(None, 1920, 1080, 'left', VisionStatus(True, 'fake', 'capturing', fps=33), stereo_jpeg_base64='both', capture_sequence=self.calls)
    def capture_metrics(self):
        return {'width': 3840, 'height': 1080, 'requested_fps': 60, 'measured_fps': 33}
    def stop(self):
        self.stopped = True


class Stage1RuntimeTests(unittest.TestCase):
    def test_real_runtime_captures_without_websocket_and_shutdown_closes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / 'settings.json'
            save_settings(settings, SavedSettings())
            with patch('locator_demo.web.app.WasapiArrayAudio', FakeAudio), patch('locator_demo.web.app.StereoVisionTracker', FakeVision), patch('locator_demo.web.app.choose_servo_port', return_value=None):
                app = create_app(simulated=False, settings_path=settings)
                runtime = app.state.runtime
                with TestClient(app) as client:
                    deadline = time.monotonic() + 2
                    while runtime.vision.calls < 3 and time.monotonic() < deadline:
                        time.sleep(.01)
                    payload = client.get('/api/state').json()
                    self.assertGreaterEqual(runtime.vision.calls, 3)
                    self.assertEqual(payload['visual']['capture']['width'], 3840)
                    self.assertEqual(payload['visual']['frame_width'], 1920)
                    self.assertEqual(payload['audio']['capture']['channels'], 4)
                    self.assertEqual(payload['visual']['stereo_frame'], 'both')
                    self.assertTrue(payload['control_loop']['background'])
                    self.assertIsNone(runtime.bus._serial)
                    self.assertIsNone(runtime.config.port)
                    runtime.head = SimpleNamespace(estimated_pose=HeadPose(1530, 1490), stop=lambda: None)
                    runtime.update_hardware_config(port=None, yaw_id=0, pitch_id=2)
                    self.assertEqual(runtime.head.estimated_pose, HeadPose(1530, 1490))
                self.assertFalse(runtime.control_loop_active)
                self.assertTrue(runtime.audio.stopped)
                self.assertTrue(runtime.vision.stopped)

    def test_new_camera_settings_round_trip_preserves_60_fps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            camera = CameraConfig(video_size='3840x1080', input_fps=60, fps=60, output_width=1920)
            save_settings(path, SavedSettings(camera=camera))
            read = load_settings(path)
            self.assertEqual((read.camera.input_fps, read.camera.fps, read.camera.output_width), (60, 60, 1920))
            self.assertEqual((read.audio_capture.channels, read.audio_capture.sample_rate), (4, 48000))

    def test_new_audio_capture_profile_does_not_accept_stereo_downgrade(self):
        with self.assertRaises(ValueError):
            AudioCaptureConfig(channels=2)

    def test_audio_adapter_does_not_fabricate_uncalibrated_direction(self):
        audio = WasapiArrayAudio()
        audio.capture.start = lambda: None
        estimate = audio.read_estimate()
        self.assertEqual(estimate.direction, AudioDirection.UNKNOWN)
        self.assertEqual(estimate.doa_confidence, 0)
        self.assertFalse(audio.capture_metrics()['localization_ready'])

    def test_visual_updates_continue_during_audio_motor_guard(self):
        with patch('locator_demo.web.app.WasapiArrayAudio', FakeAudio), patch('locator_demo.web.app.StereoVisionTracker', FakeVision), patch('locator_demo.web.app.choose_servo_port', return_value=None):
            runtime = DemoRuntime(RuntimeConfig())
            # This is the visual-only actuator contract, not speaker detection.
            runtime.vision_processing = replace(runtime.vision_processing, active_speaker_enabled=False)
            runtime.state_machine = runtime._make_state_machine()
            commands = []
            runtime.head = SimpleNamespace(move_to=lambda *args, **kwargs: commands.append(args), estimated_pose=HeadPose(1500, 1500))
            target = FaceTarget(1400, 300, 1600, 600, score=1, mouth_motion_score=1)
            runtime.vision.read = lambda: VisionFrameResult(target, 1920, 1080, '', VisionStatus(True, 'fake', 'ok'), targets=[target], detection_sequence=1)
            runtime.running = True
            runtime._motion_pose_synced = True  # Fake actuator starts at the supplied pose.
            runtime.motor_guard_until_s = time.time() + 60
            runtime.tick(include_frame=False)
            self.assertIsNotNone(runtime._smoothed_error)
            self.assertEqual(len(commands), 1)
            runtime.shutdown()

    def test_disabling_vad_does_not_create_direction_for_uncalibrated_array(self):
        with patch('locator_demo.web.app.WasapiArrayAudio', FakeAudio), patch('locator_demo.web.app.StereoVisionTracker', FakeVision), patch('locator_demo.web.app.choose_servo_port', return_value=None):
            runtime = DemoRuntime(RuntimeConfig())
            runtime.audio.localization_ready = False
            runtime.audio.read_estimate = lambda: AudioEstimate(AudioDirection.UNKNOWN, 0, 0, 1)
            runtime.audio_processing = replace(runtime.audio_processing, vad_enabled=False)
            runtime.tick(include_frame=False)
            self.assertEqual(runtime.last_audio.direction, AudioDirection.UNKNOWN)
            self.assertFalse(runtime.last_state.audio_ready)
            runtime.shutdown()

    def test_same_detected_frame_does_not_issue_repeated_visual_goals(self):
        with patch('locator_demo.web.app.WasapiArrayAudio', FakeAudio), patch('locator_demo.web.app.StereoVisionTracker', FakeVision), patch('locator_demo.web.app.choose_servo_port', return_value=None):
            runtime = DemoRuntime(RuntimeConfig())
            # This is the visual-only actuator contract, not speaker detection.
            runtime.vision_processing = replace(runtime.vision_processing, active_speaker_enabled=False)
            runtime.state_machine = runtime._make_state_machine()
            commands = []
            runtime.head = SimpleNamespace(move_to=lambda *args, **kwargs: commands.append(args), estimated_pose=HeadPose(1500, 1500))
            target = FaceTarget(1400, 300, 1600, 600, score=1, mouth_motion_score=1)
            runtime.vision.read = lambda: VisionFrameResult(target, 1920, 1080, '', VisionStatus(True, 'fake', 'ok'), targets=[target], detection_sequence=1)
            runtime.running = True
            runtime._motion_pose_synced = True  # Fake actuator starts at the supplied pose.
            runtime.tick(include_frame=False)
            time.sleep(.06)
            runtime.tick(include_frame=False)
            self.assertEqual(len(commands), 1)
            runtime.shutdown()


if __name__ == '__main__':
    unittest.main()
