import os
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from locator_demo.asd import AsdBackendStatus
from locator_demo.audio import AudioDirection, AudioEstimate
from locator_demo.audio_device import AudioDeviceStatus
from locator_demo.camera import VisionFrameResult, VisionStatus
from locator_demo.head import HeadPose
from locator_demo.vision import BodyTarget, FaceTarget
from locator_demo.web.app import create_app


TEST_TMP = Path(__file__).resolve().parents[1] / ".tmp"


def temp_settings_path():
    TEST_TMP.mkdir(exist_ok=True)
    case_dir = TEST_TMP / f"settings-{uuid.uuid4().hex}"
    case_dir.mkdir()
    return case_dir / "settings.json"


class FixedAudio:
    def __init__(self, direction=AudioDirection.LEFT, *, tdoa_s=0.00025, azimuth_deg=0.0):
        self.direction = direction
        self.tdoa_s = tdoa_s
        self.azimuth_deg = azimuth_deg

    def read_estimate(self):
        return AudioEstimate(
            self.direction,
            0.9,
            self.tdoa_s,
            0.8,
            azimuth_deg=self.azimuth_deg,
            speech_confidence=0.9,
            doa_confidence=0.9,
        )

    def status(self):
        return AudioDeviceStatus(True, "fake", "running")


class LowSpeechAudio:
    def read_estimate(self):
        return AudioEstimate(
            AudioDirection.UNKNOWN,
            0.22,
            0.00025,
            0.10,
            speech_confidence=0.05,
            doa_confidence=0.30,
        )

    def status(self):
        return AudioDeviceStatus(True, "fake", "running")


class FixedVision:
    def __init__(self, target, *, body_target=None, targets=None):
        self.target = target
        self.body_target = body_target
        self.targets = targets
        self.reads = 0
        self.stopped = False

    def read(self):
        self.reads += 1
        targets = self.targets if self.targets is not None else ([] if self.target is None else [self.target])
        return VisionFrameResult(
            target=self.target,
            frame_width=640,
            frame_height=400,
            jpeg_base64="",
            status=VisionStatus(True, "fake", "running", fps=5.0),
            targets=list(targets),
            body_target=self.body_target,
        )

    def stop(self):
        self.stopped = True


class PendingTalkNet:
    @property
    def status(self):
        return AsdBackendStatus(
            requested="talknet",
            actual="talknet",
            available=True,
            fallback=False,
            message="test pending",
            device="cuda",
        )

    def score(self, *args, **kwargs):
        raise RuntimeError("TalkNet needs recent face frames and audio samples")


class BrokenTalkNet(PendingTalkNet):
    def score(self, *args, **kwargs):
        raise RuntimeError("test hard talknet failure")


class WebAppTests(unittest.TestCase):
    def test_session_and_state_are_available(self):
        client = TestClient(create_app(simulated=True))

        session = client.get("/api/session").json()
        state = client.get("/api/state").json()

        self.assertEqual(session["servo"]["pitch_id"], 2)
        self.assertIn("mode", state)
        self.assertIn("audio", state)

    def test_scan_ids_returns_detected_ids(self):
        client = TestClient(create_app(simulated=True))

        response = client.post("/api/servo/scan", json={"ids": [1, 2, 3]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["found_ids"], [2])

    def test_direct_servo_diagnostic_move_targets_one_id(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.bus.moves.clear()
        client = TestClient(app)

        response = client.post("/api/servo/direct-move", json={"servo_id": 2, "position": 1560, "time_ms": 900})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["servo_id"], 2)
        self.assertEqual(runtime.bus.moves, [(2, 1560, 900)])

    def test_servo_read_diagnostic_reports_position_span(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.bus.positions[2] = 1560
        client = TestClient(app)

        response = client.post("/api/servo/read", json={"servo_id": 2, "samples": 3, "interval_ms": 0})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["positions"], [1560, 1560, 1560])
        self.assertEqual(response.json()["span"], 0)

    def test_servo_read_raw_diagnostic_reports_responses(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.bus.positions[2] = 1560
        client = TestClient(app)

        response = client.post("/api/servo/read-raw", json={"servo_id": 2, "samples": 2, "interval_ms": 0})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["records"],
            [{"raw": "#002P1560!", "position": 1560}, {"raw": "#002P1560!", "position": 1560}],
        )
        self.assertEqual(response.json()["span"], 0)

    def test_manual_move_accepts_yaw_and_pitch(self):
        client = TestClient(create_app(simulated=True))

        response = client.post("/api/head/move", json={"yaw": 1600, "pitch": 1400, "time_ms": 500})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["target"], {"yaw": 1600, "pitch": 1400})

    def test_visual_calibration_sample_reports_manual_alignment_error(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.vision = FixedVision(
            FaceTarget(
                100,
                100,
                200,
                220,
                label="face",
                score=0.9,
                tracking_x=160,
                tracking_y=180,
                tracking_source="test",
            )
        )
        client = TestClient(app)
        client.post(
            "/api/config/vision-processing",
            json={"target_offset_x_norm": 0.0, "target_offset_y_norm": 0.0, "visual_yaw_mode": "small", "visual_yaw_max_delta": 25},
        )
        client.post("/api/demo/tick")

        response = client.post("/api/debug/visual-calibration", json={"yaw": 1550, "pitch": 1450})
        payload = response.json()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["aligned_pose"], {"yaw": 1550, "pitch": 1450})
        self.assertEqual(payload["tracking_center"]["source"], "test")
        self.assertAlmostEqual(payload["pixel_error"]["x"], -160.0)
        self.assertAlmostEqual(payload["pixel_error"]["y"], -20.0)
        self.assertAlmostEqual(payload["normalized_error"]["x"], -0.5)
        self.assertAlmostEqual(payload["normalized_error"]["y"], -0.1)
        self.assertEqual(payload["model_delta_from_aligned"], {"yaw": -25, "pitch": 0})
        self.assertEqual(len(runtime.visual_calibration_samples), 1)

    def test_visual_calibration_sample_reports_no_target(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.vision = FixedVision(None)
        client = TestClient(app)
        client.post("/api/demo/tick")

        payload = client.post("/api/debug/visual-calibration", json={}).json()

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "no visual target")

    def test_manual_pitch_move_sends_only_changed_pitch_axis(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.update_servo_config(yaw_id=3, pitch_id=2)
        runtime.pose = HeadPose(1500, 1500)
        runtime.bus.moves.clear()
        client = TestClient(app)

        response = client.post("/api/head/move", json={"yaw": 1500, "pitch": 1580, "time_ms": 500})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(runtime.bus.moves, [(2, 1580, 500)])
        self.assertEqual(runtime.events[-1]["detail"]["changed_axes"], ["pitch"])

    def test_axis_limits_config_clamps_manual_pitch(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.update_servo_config(yaw_id=3, pitch_id=2)
        client = TestClient(app)

        response = client.post(
            "/api/config/axis-limits",
            json={
                "yaw_min": 1200,
                "yaw_center": 1500,
                "yaw_max": 1800,
                "pitch_min": 1600,
                "pitch_center": 1640,
                "pitch_max": 1680,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["axis_limits"]["pitch_center"], 1640)

        move = client.post("/api/head/move", json={"yaw": 1500, "pitch": 1500, "time_ms": 500})

        self.assertEqual(move.json()["target"], {"yaw": 1500, "pitch": 1600})

    def test_axis_limits_config_persists_to_disk(self):
        settings_path = temp_settings_path()
        client = TestClient(create_app(simulated=True, settings_path=settings_path))

        response = client.post(
            "/api/config/axis-limits",
            json={
                "yaw_min": 1200,
                "yaw_center": 1500,
                "yaw_max": 1800,
                "pitch_min": 1600,
                "pitch_center": 1640,
                "pitch_max": 1680,
            },
        )
        self.assertEqual(response.status_code, 200)

        reloaded = TestClient(create_app(simulated=True, settings_path=settings_path))
        session = reloaded.get("/api/session").json()

        self.assertEqual(session["axis_limits"]["pitch_min"], 1600)
        self.assertEqual(session["axis_limits"]["pitch_center"], 1640)
        self.assertEqual(session["axis_limits"]["pitch_max"], 1680)

    def test_vision_processing_offset_persists_to_disk(self):
        settings_path = temp_settings_path()
        client = TestClient(create_app(simulated=True, settings_path=settings_path))

        response = client.post(
            "/api/config/vision-processing",
            json={
                "active_speaker_enabled": True,
                "target_offset_x_norm": 0.12,
                "target_offset_y_norm": -0.05,
                "visual_mirror_x": True,
                "visual_speaker_threshold": 0.28,
                "mouth_evidence_threshold": 0.07,
                "visual_yaw_mode": "off",
                "visual_pitch_enabled": False,
                "visual_yaw_deadband": 0.11,
                "visual_pitch_deadband": 0.09,
                "visual_yaw_min_delta": 12,
                "visual_yaw_max_delta": 44,
                "visual_pitch_min_delta": 8,
                "visual_pitch_max_delta": 33,
            },
        )
        self.assertEqual(response.status_code, 200)

        reloaded = TestClient(create_app(simulated=True, settings_path=settings_path))
        session = reloaded.get("/api/session").json()

        self.assertAlmostEqual(session["vision_processing"]["target_offset_x_norm"], 0.12)
        self.assertAlmostEqual(session["vision_processing"]["target_offset_y_norm"], -0.05)
        self.assertTrue(session["vision_processing"]["visual_mirror_x"])
        self.assertAlmostEqual(session["vision_processing"]["visual_speaker_threshold"], 0.28)
        self.assertAlmostEqual(session["vision_processing"]["mouth_evidence_threshold"], 0.07)
        self.assertEqual(session["vision_processing"]["visual_yaw_mode"], "off")
        self.assertFalse(session["vision_processing"]["visual_pitch_enabled"])
        self.assertAlmostEqual(session["vision_processing"]["visual_yaw_deadband"], 0.11)
        self.assertAlmostEqual(session["vision_processing"]["visual_pitch_deadband"], 0.09)
        self.assertEqual(session["vision_processing"]["visual_yaw_min_delta"], 12)
        self.assertEqual(session["vision_processing"]["visual_yaw_max_delta"], 44)
        self.assertEqual(session["vision_processing"]["visual_pitch_min_delta"], 8)
        self.assertEqual(session["vision_processing"]["visual_pitch_max_delta"], 33)

    def test_crowded_strategy_config_persists_to_disk(self):
        settings_path = temp_settings_path()
        client = TestClient(create_app(simulated=True, settings_path=settings_path))

        response = client.post(
            "/api/config/vision-processing",
            json={
                "active_speaker_enabled": True,
                "tracking_strategy": "crowded_visual_first",
                "asd_backend": "talknet",
                "speaker_lock_policy": "until_lost",
                "target_offset_x_norm": 0.02,
                "target_offset_y_norm": 0.01,
                "visual_mirror_x": True,
                "visual_speaker_threshold": 0.34,
                "mouth_evidence_threshold": 0.08,
                "visual_yaw_mode": "small",
                "visual_pitch_enabled": True,
                "min_face_height_ratio": 0.18,
                "keep_face_height_ratio": 0.11,
                "talknet_threshold": 0.62,
                "speaker_lock_hold_s": 1.6,
                "speaker_lost_timeout_s": 0.9,
                "audio_interrupt_enabled": True,
                "audio_search_after_silent_visual": True,
                "silent_visual_hold_s": 1.4,
            },
        )
        self.assertEqual(response.status_code, 200)

        reloaded = TestClient(create_app(simulated=True, settings_path=settings_path))
        session = reloaded.get("/api/session").json()

        self.assertEqual(session["vision_processing"]["tracking_strategy"], "crowded_visual_first")
        self.assertEqual(session["vision_processing"]["asd_backend"], "talknet")
        self.assertEqual(session["vision_processing"]["speaker_lock_policy"], "until_lost")
        self.assertTrue(session["vision_processing"]["visual_mirror_x"])
        self.assertAlmostEqual(session["vision_processing"]["min_face_height_ratio"], 0.18)
        self.assertAlmostEqual(session["vision_processing"]["keep_face_height_ratio"], 0.11)
        self.assertAlmostEqual(session["vision_processing"]["talknet_threshold"], 0.62)
        self.assertAlmostEqual(session["vision_processing"]["speaker_lock_hold_s"], 1.6)
        self.assertAlmostEqual(session["vision_processing"]["speaker_lost_timeout_s"], 0.9)
        self.assertTrue(session["vision_processing"]["audio_interrupt_enabled"])
        self.assertTrue(session["vision_processing"]["audio_search_after_silent_visual"])
        self.assertAlmostEqual(session["vision_processing"]["silent_visual_hold_s"], 1.4)

    def test_audio_processing_thresholds_persist_to_disk(self):
        settings_path = temp_settings_path()
        client = TestClient(create_app(simulated=True, settings_path=settings_path))

        response = client.post(
            "/api/config/audio-processing",
            json={
                "vad_enabled": True,
                "audio_confidence_threshold": 0.22,
                "speech_confidence_threshold": 0.16,
                "doa_confidence_threshold": 0.06,
                "required_audio_hits": 2,
            },
        )
        self.assertEqual(response.status_code, 200)

        reloaded = TestClient(create_app(simulated=True, settings_path=settings_path))
        session = reloaded.get("/api/session").json()

        self.assertAlmostEqual(session["audio_processing"]["audio_confidence_threshold"], 0.22)
        self.assertAlmostEqual(session["audio_processing"]["speech_confidence_threshold"], 0.16)
        self.assertAlmostEqual(session["audio_processing"]["doa_confidence_threshold"], 0.06)
        self.assertEqual(session["audio_processing"]["required_audio_hits"], 2)

    def test_demo_can_start_and_stop(self):
        client = TestClient(create_app(simulated=True))

        self.assertTrue(client.post("/api/demo/start").json()["running"])
        self.assertFalse(client.post("/api/demo/stop").json()["running"])

    def test_can_update_axis_servo_ids(self):
        client = TestClient(create_app(simulated=True))

        response = client.post("/api/config/servos", json={"yaw_id": 3, "pitch_id": 2})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["servo"]["yaw_id"], 3)

    def test_snapshot_includes_device_status(self):
        client = TestClient(create_app(simulated=True))

        state = client.post("/api/demo/tick").json()

        self.assertIn("status", state["audio"])
        self.assertIn("status", state["visual"])

    def test_runtime_can_omit_frame_for_lightweight_updates(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.last_frame_b64 = "encoded-frame"

        state = runtime.snapshot(include_frame=False)

        self.assertEqual(state["visual"]["frame"], "")
        self.assertIn("audio", state)
        self.assertIn("pose", state)

    def test_can_jog_head_with_direction_config(self):
        client = TestClient(create_app(simulated=True))
        client.post(
            "/api/config/direction",
            json={"yaw_left_sign": 1, "pitch_up_sign": 1, "manual_step": 80},
        )

        response = client.post("/api/head/jog", json={"direction": "left", "time_ms": 500})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["target"], {"yaw": 1580, "pitch": 1500})

    def test_direction_config_persists_to_disk(self):
        settings_path = temp_settings_path()
        client = TestClient(create_app(simulated=True, settings_path=settings_path))

        response = client.post(
            "/api/config/direction",
            json={"yaw_left_sign": 1, "pitch_up_sign": -1, "manual_step": 90},
        )
        self.assertEqual(response.status_code, 200)

        reloaded = TestClient(create_app(simulated=True, settings_path=settings_path))
        session = reloaded.get("/api/session").json()

        self.assertEqual(session["direction"]["yaw_left_sign"], 1)
        self.assertEqual(session["direction"]["manual_step"], 90)

    def test_tracking_feature_config_persists_to_disk(self):
        settings_path = temp_settings_path()
        client = TestClient(create_app(simulated=True, settings_path=settings_path))

        response = client.post(
            "/api/config/features",
            json={"audio_enabled": False, "visual_enabled": True, "camera_enabled": False},
        )
        self.assertEqual(response.status_code, 200)

        reloaded = TestClient(create_app(simulated=True, settings_path=settings_path))
        session = reloaded.get("/api/session").json()

        self.assertFalse(session["features"]["audio_enabled"])
        self.assertTrue(session["features"]["visual_enabled"])
        self.assertFalse(session["features"]["camera_enabled"])

    def test_camera_can_be_disabled_and_releases_stream(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.vision = FixedVision(FaceTarget(40, 100, 160, 260, label="person", score=0.9))
        client = TestClient(app)

        client.post(
            "/api/config/features",
            json={"audio_enabled": False, "visual_enabled": True, "camera_enabled": False},
        )
        state = client.post("/api/demo/tick").json()

        self.assertTrue(runtime.vision.stopped)
        self.assertEqual(runtime.vision.reads, 0)
        self.assertFalse(state["visual"]["locked"])
        self.assertEqual(state["visual"]["status"]["message"], "camera off")

    def test_camera_can_be_re_enabled_without_stale_status(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.vision = FixedVision(FaceTarget(40, 100, 160, 260, label="person", score=0.9))
        client = TestClient(app)

        off = {"audio_enabled": False, "visual_enabled": True, "camera_enabled": False}
        on = {"audio_enabled": False, "visual_enabled": True, "camera_enabled": True}

        client.post("/api/config/features", json=off)
        client.post("/api/demo/tick")
        self.assertEqual(runtime.last_vision_status.message, "camera off")

        # Turning the camera back on must clear the "camera off" placeholder.
        # The dashboard reads this status directly, and ticks may not resume
        # immediately, so a stale value would leave it reporting a dead camera.
        session = client.post("/api/config/features", json=on).json()
        self.assertTrue(session["features"]["camera_enabled"])
        self.assertNotEqual(runtime.last_vision_status.message, "camera off")

        # The capture itself restarts on the next tick, via read().
        state = client.post("/api/demo/tick").json()
        self.assertEqual(state["visual"]["status"]["message"], "running")
        self.assertEqual(runtime.vision.reads, 1)

    def test_feature_update_keeps_camera_status_when_unchanged(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.vision = FixedVision(FaceTarget(40, 100, 160, 260, label="person", score=0.9))
        client = TestClient(app)

        on = {"audio_enabled": True, "visual_enabled": True, "camera_enabled": True}
        client.post("/api/config/features", json=on)
        client.post("/api/demo/tick")
        self.assertEqual(runtime.last_vision_status.message, "running")

        # Re-posting the same features (the dashboard does this on load) must
        # not tear down a working camera's status.
        client.post("/api/config/features", json=on)
        self.assertEqual(runtime.last_vision_status.message, "running")

    def test_yaw_servo_id_zero_is_not_treated_as_unset(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        client = TestClient(app)

        client.post("/api/config/servos", json={"port": None, "yaw_id": 0, "pitch_id": 2})

        # Servo id 0 is valid and in use on the demo bus. A previous `yaw_id or 3`
        # fell back to 3 for it, so the logic axis pointed at the wrong servo.
        self.assertIsNotNone(runtime.head.yaw)
        self.assertEqual(runtime.head.yaw.servo_id, 0)
        self.assertEqual(runtime.logic.yaw.servo_id, 0)

    def test_yaw_axis_is_absent_when_servo_id_unset(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        client = TestClient(app)

        client.post("/api/config/servos", json={"port": None, "yaw_id": None, "pitch_id": 2})

        # With no yaw id there is no yaw axis at all, which is what makes the
        # head refuse to turn left/right until the bus is scanned.
        self.assertIsNone(runtime.head.yaw)
        self.assertIsNotNone(runtime.head.pitch)

    def test_camera_config_persists_to_disk(self):
        settings_path = temp_settings_path()
        client = TestClient(create_app(simulated=True, settings_path=settings_path))

        response = client.post(
            "/api/config/camera",
            json={
                "device_name": "USB Camera",
                "video_size": "1280x720",
                "crop_left_half": False,
                "fps": 12,
                "output_width": 1280,
                "face_detector_backend": "scrfd",
                "scrfd_model_path": "models/scrfd_10g_bnkps.onnx",
                "scrfd_threshold": 0.42,
                "scrfd_input_size": 960,
            },
        )
        self.assertEqual(response.status_code, 200)

        reloaded = TestClient(create_app(simulated=True, settings_path=settings_path))
        session = reloaded.get("/api/session").json()

        self.assertEqual(session["camera"]["device_name"], "USB Camera")
        self.assertEqual(session["camera"]["video_size"], "1280x720")
        self.assertFalse(session["camera"]["crop_left_half"])
        self.assertEqual(session["camera"]["fps"], 12)
        self.assertEqual(session["camera"]["output_width"], 1280)
        self.assertEqual(session["camera"]["face_detector_backend"], "scrfd")
        self.assertEqual(session["camera"]["scrfd_model_path"], "models/scrfd_10g_bnkps.onnx")
        self.assertAlmostEqual(session["camera"]["scrfd_threshold"], 0.42)
        self.assertEqual(session["camera"]["scrfd_input_size"], 960)

    def test_audio_channel_swap_persists_to_disk(self):
        settings_path = temp_settings_path()
        client = TestClient(create_app(simulated=True, settings_path=settings_path))

        response = client.post(
            "/api/config/audio",
            json={"swap_channels": True},
        )
        self.assertEqual(response.status_code, 200)

        reloaded = TestClient(create_app(simulated=True, settings_path=settings_path))
        session = reloaded.get("/api/session").json()

        self.assertTrue(session["audio_config"]["swap_channels"])

    def test_audio_channel_swap_mirrors_runtime_direction(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.LEFT)
        runtime.vision = FixedVision(None)
        client = TestClient(app)

        client.post("/api/config/audio", json={"swap_channels": True})
        state = client.post("/api/demo/tick").json()

        self.assertEqual(state["audio"]["direction"], "right")

    def test_visual_tracking_can_be_disabled_while_preview_still_updates(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.vision = FixedVision(FaceTarget(40, 100, 160, 260, label="person", score=0.9))
        client = TestClient(app)

        client.post("/api/config/features", json={"audio_enabled": False, "visual_enabled": False})
        client.post("/api/demo/start")
        state = client.post("/api/demo/tick").json()

        self.assertEqual(state["pose"], {"yaw": 1500, "pitch": 1500})
        self.assertIsNotNone(state["visual"]["target"])

    def test_visible_silent_face_does_not_lock_or_move(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.UNKNOWN)
        runtime.vision = FixedVision(
            FaceTarget(40, 100, 160, 260, label="person", score=0.9, mouth_motion_score=0.9, active_speaker_score=0.9)
        )
        runtime.motion_interval_s = 0.0
        client = TestClient(app)

        client.post("/api/demo/start")
        state = client.post("/api/demo/tick").json()

        self.assertTrue(state["visual"]["visible"])
        self.assertFalse(state["visual"]["locked"])
        self.assertEqual(state["pose"], {"yaw": 1500, "pitch": 1500})

    def test_visual_status_exposes_detector_name(self):
        client = TestClient(create_app(simulated=True))

        state = client.post("/api/demo/tick").json()

        self.assertEqual(state["visual"]["status"]["detector"], "simulated")

    def test_audio_tracking_can_be_disabled(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.LEFT)
        runtime.vision = FixedVision(None)
        runtime.motion_interval_s = 0.0
        runtime.motor_guard_until_s = 0.0
        client = TestClient(app)

        client.post("/api/config/features", json={"audio_enabled": False, "visual_enabled": False})
        client.post("/api/demo/start")
        client.post("/api/demo/tick")
        state = client.post("/api/demo/tick").json()

        self.assertEqual(state["pose"], {"yaw": 1500, "pitch": 1500})
        self.assertEqual(state["audio"]["direction"], "left")

    def test_audio_only_tracking_preserves_current_pitch(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.LEFT)
        runtime.vision = FixedVision(None)
        runtime.motion_interval_s = 0.0
        runtime.motor_guard_until_s = 0.0
        client = TestClient(app)

        client.post("/api/head/move", json={"yaw": 1500, "pitch": 1600, "time_ms": 100})
        runtime.last_motion_s = 0.0
        runtime.motor_guard_until_s = 0.0
        client.post("/api/config/features", json={"audio_enabled": True, "visual_enabled": False})
        client.post("/api/demo/start")
        client.post("/api/demo/tick")
        state = client.post("/api/demo/tick").json()

        self.assertEqual(state["pose"]["pitch"], 1600)
        self.assertNotEqual(state["pose"]["yaw"], 1500)

    def test_audio_only_mode_preserves_pitch_without_sound(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.UNKNOWN)
        runtime.vision = FixedVision(None)
        client = TestClient(app)

        client.post("/api/head/move", json={"yaw": 1500, "pitch": 1650, "time_ms": 100})
        runtime.last_motion_s = 0.0
        runtime.motor_guard_until_s = 0.0
        client.post("/api/config/features", json={"audio_enabled": True, "visual_enabled": False})
        client.post("/api/demo/start")
        state = client.post("/api/demo/tick").json()

        self.assertEqual(state["pose"], {"yaw": 1500, "pitch": 1650})

    def test_control_profile_persists_to_disk(self):
        settings_path = temp_settings_path()
        client = TestClient(create_app(simulated=True, settings_path=settings_path))

        response = client.post(
            "/api/config/control",
            json={"control_profile": "fast", "motor_guard_ms": 400},
        )
        self.assertEqual(response.status_code, 200)

        reloaded = TestClient(create_app(simulated=True, settings_path=settings_path))
        session = reloaded.get("/api/session").json()

        self.assertEqual(session["control"]["control_profile"], "fast")
        self.assertEqual(session["control"]["motor_guard_ms"], 400)

    def test_motor_guard_suppresses_audio_turn(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.LEFT)
        runtime.vision = FixedVision(None)
        runtime.motion_interval_s = 0.0
        runtime.motor_guard_until_s = 9999999999.0
        client = TestClient(app)

        client.post("/api/config/features", json={"audio_enabled": True, "visual_enabled": False})
        client.post("/api/demo/start")
        state = client.post("/api/demo/tick").json()

        self.assertEqual(state["pose"], {"yaw": 1500, "pitch": 1500})
        self.assertTrue(state["audio"]["motor_suppressed"])

    def test_no_visible_person_uses_audio_for_coarse_seek(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.LEFT)
        runtime.vision = FixedVision(None)
        runtime.motion_interval_s = 0.0
        client = TestClient(app)

        client.post("/api/demo/start")
        state = client.post("/api/demo/tick").json()

        self.assertNotEqual(state["pose"]["yaw"], 1500)
        self.assertEqual(state["pose"]["pitch"], 1500)

    def test_no_face_reframe_uses_pitch_after_first_audio_yaw(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.LEFT)
        runtime.vision = FixedVision(None)
        runtime.motion_interval_s = 0.0
        client = TestClient(app)

        client.post("/api/demo/start")
        first = client.post("/api/demo/tick").json()
        runtime.motor_guard_until_s = 0.0
        runtime.last_motion_s = 0.0
        second = client.post("/api/demo/tick").json()
        runtime.motor_guard_until_s = 0.0
        runtime.last_motion_s = 0.0
        runtime._last_reframe_step_s = 0.0
        third = client.post("/api/demo/tick").json()

        self.assertNotEqual(first["pose"]["yaw"], 1500)
        self.assertEqual(second["mode"], "reframe_visual")
        self.assertEqual(third["mode"], "reframe_visual")
        self.assertEqual(third["pose"]["yaw"], first["pose"]["yaw"])
        self.assertNotEqual(third["pose"]["pitch"], 1500)

    def test_reframe_pitch_sequence_respects_axis_limits(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.LEFT)
        runtime.vision = FixedVision(None)
        runtime.motion_interval_s = 0.0
        client = TestClient(app)

        client.post(
            "/api/config/axis-limits",
            json={
                "yaw_min": 1200,
                "yaw_center": 1500,
                "yaw_max": 1800,
                "pitch_min": 1600,
                "pitch_center": 1640,
                "pitch_max": 1680,
            },
        )
        runtime.pose = HeadPose(1500, 1640)
        client.post("/api/demo/start")
        client.post("/api/demo/tick")
        runtime.motor_guard_until_s = 0.0
        runtime.last_motion_s = 0.0
        client.post("/api/demo/tick")
        runtime.motor_guard_until_s = 0.0
        runtime.last_motion_s = 0.0
        runtime._last_reframe_step_s = 0.0
        state = client.post("/api/demo/tick").json()

        self.assertGreaterEqual(state["pose"]["pitch"], 1600)
        self.assertLessEqual(state["pose"]["pitch"], 1680)

    def test_body_target_reframe_uses_pose_before_blind_pitch_sequence(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.LEFT)
        body_target = BodyTarget(520, 40, 600, 120, score=0.8, visibility=0.8)
        runtime.vision = FixedVision(None, body_target=body_target)
        runtime.motion_interval_s = 0.0
        client = TestClient(app)

        client.post("/api/demo/start")
        first = client.post("/api/demo/tick").json()
        runtime.motor_guard_until_s = 0.0
        runtime.last_motion_s = 0.0
        second = client.post("/api/demo/tick").json()

        self.assertEqual(second["mode"], "reframe_visual")
        self.assertIsNotNone(second["visual"]["body_target"])
        self.assertLessEqual(abs(second["pose"]["yaw"] - first["pose"]["yaw"]), 30)
        self.assertNotEqual(second["pose"]["pitch"], 1500)

    def test_visible_unconfirmed_face_reframes_pitch_without_extra_yaw(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.LEFT)
        runtime.vision = FixedVision(
            FaceTarget(40, 20, 160, 100, label="face", score=0.9, mouth_motion_score=0.07, active_speaker_score=0.34)
        )
        runtime.motion_interval_s = 0.0
        client = TestClient(app)

        # Exercise an explicitly unconfirmed mouth signal; the current
        # low-frame-rate default is 0.06, below this fixture's 0.07.
        client.post('/api/config/vision-processing', json={'mouth_evidence_threshold': 0.12})
        client.post("/api/demo/start")
        first = client.post("/api/demo/tick").json()
        runtime.motor_guard_until_s = 0.0
        runtime.last_motion_s = 0.0
        second = client.post("/api/demo/tick").json()
        runtime.motor_guard_until_s = 0.0
        runtime.last_motion_s = 0.0
        third = client.post("/api/demo/tick").json()

        self.assertFalse(first["visual"]["locked"])
        self.assertEqual(first["mode"], "reframe_visual")
        self.assertEqual(first["pose"]["yaw"], 1500)
        self.assertNotEqual(first["pose"]["pitch"], 1500)
        self.assertIn(second["mode"], ("reframe_visual", "track_speaker"))
        self.assertIn(third["mode"], ("reframe_visual", "track_speaker"))

    def test_single_visible_face_can_reframe_pitch_even_if_audio_region_mismatches(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.RIGHT)
        runtime.vision = FixedVision(
            FaceTarget(40, 20, 160, 100, label="face", score=0.9, mouth_motion_score=0.07, active_speaker_score=0.34)
        )
        runtime.motion_interval_s = 0.0
        client = TestClient(app)

        # Exercise an explicitly unconfirmed mouth signal; the current
        # low-frame-rate default is 0.06, below this fixture's 0.07.
        client.post('/api/config/vision-processing', json={'mouth_evidence_threshold': 0.12})
        client.post("/api/demo/start")
        client.post("/api/demo/tick")
        runtime.motor_guard_until_s = 0.0
        runtime.last_motion_s = 0.0
        client.post("/api/demo/tick")
        runtime.motor_guard_until_s = 0.0
        runtime.last_motion_s = 0.0
        state = client.post("/api/demo/tick").json()

        self.assertEqual(state["mode"], "reframe_visual")
        self.assertNotEqual(state["pose"]["pitch"], 1500)

    def test_center_speech_leaves_listening_and_allows_visual_acquire(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.CENTER, tdoa_s=0.0, azimuth_deg=0.0)
        runtime.vision = FixedVision(
            FaceTarget(
                180,
                40,
                300,
                165,
                label="face",
                score=0.85,
                frontal_score=0.98,
                mouth_motion_score=0.06,
                active_speaker_score=0.33,
            )
        )
        runtime.motion_interval_s = 0.0
        client = TestClient(app)

        client.post("/api/demo/start")
        client.post("/api/demo/tick")
        state = client.post("/api/demo/tick").json()

        self.assertEqual(state["mode"], "track_speaker")
        self.assertTrue(state["state"]["target_confirmed"])
        self.assertTrue(state["visual"]["locked"])

    def test_visual_yaw_off_preserves_audio_yaw_while_pitch_tracks(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.CENTER, tdoa_s=0.0, azimuth_deg=0.0)
        runtime.vision = FixedVision(
            FaceTarget(
                40,
                20,
                160,
                100,
                label="face",
                score=0.9,
                mouth_motion_score=0.2,
                active_speaker_score=0.8,
            )
        )
        runtime.motion_interval_s = 0.0
        client = TestClient(app)

        client.post(
            "/api/config/vision-processing",
            json={
                "active_speaker_enabled": True,
                "visual_yaw_mode": "off",
                "visual_pitch_enabled": True,
                "target_offset_x_norm": 0.0,
                "target_offset_y_norm": 0.0,
                "visual_speaker_threshold": 0.30,
                "mouth_evidence_threshold": 0.06,
            },
        )
        client.post("/api/demo/start")
        for _ in range(4):
            state = client.post("/api/demo/tick").json()
            runtime.motor_guard_until_s = 0.0
            runtime.last_motion_s = 0.0

        self.assertEqual(state["pose"]["yaw"], 1500)
        self.assertNotEqual(state["pose"]["pitch"], 1500)

    def test_visual_yaw_small_limits_tracking_correction(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.CENTER, tdoa_s=0.0, azimuth_deg=0.0)
        runtime.vision = FixedVision(
            FaceTarget(
                20,
                150,
                120,
                250,
                label="face",
                score=0.9,
                mouth_motion_score=0.2,
                active_speaker_score=0.8,
            )
        )
        runtime.motion_interval_s = 0.0
        client = TestClient(app)

        client.post(
            "/api/config/vision-processing",
            json={
                "active_speaker_enabled": True,
                "visual_yaw_mode": "small",
                "visual_yaw_max_delta": 25,
                "visual_pitch_enabled": False,
                "target_offset_x_norm": 0.0,
                "target_offset_y_norm": 0.0,
                "visual_speaker_threshold": 0.30,
                "mouth_evidence_threshold": 0.06,
            },
        )
        client.post("/api/demo/start")
        client.post("/api/demo/tick")
        state = client.post("/api/demo/tick").json()

        self.assertEqual(state["mode"], "track_speaker")
        self.assertLessEqual(abs(state["pose"]["yaw"] - 1500), 25)
        self.assertNotEqual(state["pose"]["yaw"], 1500)
        self.assertEqual(state["pose"]["pitch"], 1500)

    def test_visual_mirror_x_flips_visual_yaw_correction_only(self):
        target = FaceTarget(
            520,
            150,
            620,
            250,
            label="face",
            score=0.9,
            mouth_motion_score=0.2,
            active_speaker_score=0.8,
        )

        def run_case(mirror_x: bool) -> int:
            app = create_app(simulated=True)
            runtime = app.state.runtime
            runtime.audio = FixedAudio(AudioDirection.CENTER, tdoa_s=0.0, azimuth_deg=0.0)
            runtime.vision = FixedVision(target)
            runtime.motion_interval_s = 0.0
            client = TestClient(app)
            client.post(
                "/api/config/vision-processing",
                json={
                    "active_speaker_enabled": True,
                    "visual_yaw_mode": "small",
                    "visual_pitch_enabled": False,
                    "visual_mirror_x": mirror_x,
                    "target_offset_x_norm": 0.0,
                    "target_offset_y_norm": 0.0,
                    "visual_speaker_threshold": 0.30,
                    "mouth_evidence_threshold": 0.06,
                },
            )
            client.post("/api/demo/start")
            client.post("/api/demo/tick")
            return client.post("/api/demo/tick").json()["pose"]["yaw"]

        self.assertGreater(run_case(False), 1500)
        self.assertLess(run_case(True), 1500)

    def test_visual_control_step_range_overrides_profile(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.CENTER, tdoa_s=0.0, azimuth_deg=0.0)
        runtime.vision = FixedVision(
            FaceTarget(
                325,
                150,
                365,
                250,
                label="face",
                score=0.9,
                mouth_motion_score=0.2,
                active_speaker_score=0.8,
            )
        )
        runtime.motion_interval_s = 0.0
        client = TestClient(app)
        client.post(
            "/api/config/vision-processing",
            json={
                "active_speaker_enabled": True,
                "visual_yaw_mode": "small",
                "visual_pitch_enabled": False,
                "target_offset_x_norm": 0.0,
                "target_offset_y_norm": 0.0,
                "visual_speaker_threshold": 0.30,
                "mouth_evidence_threshold": 0.06,
                "visual_yaw_deadband": 0.02,
                "visual_yaw_min_delta": 30,
                "visual_yaw_max_delta": 35,
            },
        )

        client.post("/api/demo/start")
        client.post("/api/demo/tick")
        state = client.post("/api/demo/tick").json()

        self.assertEqual(state["pose"]["yaw"], 1530)

    def test_visual_tracking_waits_for_motor_guard_before_next_step(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.CENTER, tdoa_s=0.0, azimuth_deg=0.0)
        runtime.vision = FixedVision(
            FaceTarget(
                520,
                150,
                620,
                250,
                label="face",
                score=0.9,
                mouth_motion_score=0.2,
                active_speaker_score=0.8,
            )
        )
        runtime.motion_interval_s = 0.0
        client = TestClient(app)
        client.post(
            "/api/config/vision-processing",
            json={
                "active_speaker_enabled": True,
                "visual_yaw_mode": "small",
                "visual_pitch_enabled": False,
                "target_offset_x_norm": 0.0,
                "target_offset_y_norm": 0.0,
                "visual_speaker_threshold": 0.30,
                "mouth_evidence_threshold": 0.06,
                "visual_yaw_deadband": 0.02,
                "visual_yaw_min_delta": 20,
                "visual_yaw_max_delta": 35,
            },
        )

        client.post("/api/demo/start")
        client.post("/api/demo/tick")
        moved = client.post("/api/demo/tick").json()["pose"]
        guarded = client.post("/api/demo/tick").json()["pose"]

        self.assertNotEqual(moved["yaw"], 1500)
        self.assertEqual(guarded, moved)
        self.assertEqual(runtime.events[-1]["event"], "auto_move")

    def test_body_target_cannot_directly_lock_tracking(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.UNKNOWN)
        runtime.vision = FixedVision(None, body_target=BodyTarget(300, 80, 380, 180, score=0.9, visibility=0.9))
        client = TestClient(app)

        client.post("/api/demo/start")
        state = client.post("/api/demo/tick").json()

        self.assertFalse(state["visual"]["locked"])
        self.assertIsNone(state["visual"]["target"])
        self.assertIsNotNone(state["visual"]["body_target"])

    def test_silent_visible_face_does_not_block_offscreen_audio_yaw(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.LEFT)
        runtime.vision = FixedVision(FaceTarget(260, 100, 380, 260, label="silent", score=0.9))
        runtime.motion_interval_s = 0.0
        client = TestClient(app)

        client.post("/api/demo/start")
        client.post("/api/demo/tick")
        state = client.post("/api/demo/tick").json()

        self.assertFalse(state["visual"]["locked"])
        self.assertNotEqual(state["pose"]["yaw"], 1500)

    def test_audio_seek_preserves_pitch_when_visual_tracking_is_enabled(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.LEFT)
        runtime.vision = FixedVision(None)
        runtime.motion_interval_s = 0.0
        runtime.motor_guard_until_s = 0.0
        runtime.pose = HeadPose(1500, 1600)
        client = TestClient(app)

        client.post("/api/demo/start")
        state = client.post("/api/demo/tick").json()

        self.assertNotEqual(state["pose"]["yaw"], 1500)
        self.assertEqual(state["pose"]["pitch"], 1600)

    def test_audio_seek_continues_from_current_yaw_with_front_window(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.LEFT)
        runtime.vision = FixedVision(None)
        runtime.motion_interval_s = 0.0
        runtime.pose = HeadPose(1300, 1500)
        client = TestClient(app)

        client.post("/api/demo/start")
        state = client.post("/api/demo/tick").json()

        self.assertLess(state["pose"]["yaw"], 1300)
        self.assertGreaterEqual(state["pose"]["yaw"], max(runtime.axis_limits.yaw_min,
            1300-runtime._profile_params()["audio_seek_window"]))
        self.assertEqual(state["pose"]["pitch"], 1500)

    def test_vad_disabled_reclassifies_low_speech_audio(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = LowSpeechAudio()
        runtime.vision = FixedVision(None)
        runtime.motion_interval_s = 0.0
        client = TestClient(app)

        client.post("/api/config/audio-processing", json={"vad_enabled": False})
        client.post("/api/demo/start")
        state = client.post("/api/demo/tick").json()

        self.assertEqual(state["audio"]["direction"], "left")
        self.assertNotEqual(state["pose"]["yaw"], 1500)

    def test_crowded_mode_locks_near_active_speaker(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.CENTER, tdoa_s=0.0, azimuth_deg=0.0)
        runtime.vision = FixedVision(
            FaceTarget(
                250,
                70,
                390,
                250,
                label="face",
                score=0.9,
                mouth_motion_score=0.7,
                active_speaker_score=0.8,
            )
        )
        runtime.motion_interval_s = 0.0
        client = TestClient(app)

        client.post(
            "/api/config/vision-processing",
            json={
                "active_speaker_enabled": True,
                "tracking_strategy": "crowded_visual_first",
                "asd_backend": "rules",
                "visual_speaker_threshold": 0.30,
            },
        )
        client.post("/api/demo/start")
        state = client.post("/api/demo/tick").json()

        self.assertEqual(state["strategy"], "crowded_visual_first")
        self.assertEqual(state["mode"], "speaker_locked")
        self.assertTrue(state["specific_speaker_detected"])
        self.assertTrue(state["visual"]["targets"][0]["locked"])
        self.assertTrue(state["visual"]["targets"][0]["specific_speaker"])
        self.assertTrue(state["visual"]["targets"][0]["active_candidate"])
        self.assertFalse(state["audio_search_allowed"])

    def test_crowded_rules_do_not_lock_without_audio_gate(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.UNKNOWN)
        runtime.vision = FixedVision(
            FaceTarget(
                250,
                70,
                390,
                250,
                label="face",
                score=0.9,
                mouth_motion_score=0.8,
                active_speaker_score=0.9,
            )
        )
        client = TestClient(app)

        client.post(
            "/api/config/vision-processing",
            json={
                "active_speaker_enabled": True,
                "tracking_strategy": "crowded_visual_first",
                "asd_backend": "rules",
                "visual_speaker_threshold": 0.30,
            },
        )
        client.post("/api/demo/start")
        state = client.post("/api/demo/tick").json()

        self.assertFalse(state["specific_speaker_detected"])
        self.assertFalse(state["visual"]["targets"][0]["active_candidate"])
        self.assertEqual(state["mode"], "visual_candidate")

    def test_crowded_mode_rejects_too_far_face_for_control(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.UNKNOWN)
        runtime.vision = FixedVision(
            FaceTarget(
                280,
                120,
                340,
                150,
                label="far-face",
                score=0.9,
                mouth_motion_score=0.9,
                active_speaker_score=0.9,
            )
        )
        client = TestClient(app)

        client.post(
            "/api/config/vision-processing",
            json={
                "active_speaker_enabled": True,
                "tracking_strategy": "crowded_visual_first",
                "asd_backend": "rules",
                "min_face_height_ratio": 0.12,
            },
        )
        client.post("/api/demo/start")
        state = client.post("/api/demo/tick").json()

        self.assertFalse(state["specific_speaker_detected"])
        self.assertFalse(state["visual"]["targets"][0]["near_candidate"])
        self.assertTrue(state["visual"]["targets"][0]["too_far"])
        self.assertFalse(state["visual"]["targets"][0]["active_candidate"])
        self.assertEqual(state["pose"], {"yaw": 1500, "pitch": 1500})

    def test_crowded_mode_holds_when_near_face_is_silent(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.LEFT)
        runtime.vision = FixedVision(FaceTarget(260, 90, 380, 260, label="silent", score=0.9))
        runtime.motion_interval_s = 0.0
        client = TestClient(app)

        client.post(
            "/api/config/vision-processing",
            json={
                "active_speaker_enabled": True,
                "tracking_strategy": "crowded_visual_first",
                "asd_backend": "rules",
                "visual_speaker_threshold": 0.30,
            },
        )
        client.post("/api/demo/start")
        state = client.post("/api/demo/tick").json()

        self.assertFalse(state["specific_speaker_detected"])
        self.assertFalse(state["audio_search_allowed"])
        self.assertEqual(state["mode"], "visual_candidate")
        self.assertEqual(state["pose"], {"yaw": 1500, "pitch": 1500})

    def test_crowded_mode_can_search_audio_after_silent_visual_hold(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.LEFT)
        runtime.vision = FixedVision(FaceTarget(260, 90, 380, 260, label="silent", score=0.9))
        runtime.motion_interval_s = 0.0
        client = TestClient(app)

        client.post(
            "/api/config/vision-processing",
            json={
                "active_speaker_enabled": True,
                "tracking_strategy": "crowded_visual_first",
                "asd_backend": "rules",
                "visual_speaker_threshold": 0.30,
                "audio_search_after_silent_visual": True,
                "silent_visual_hold_s": 0.1,
            },
        )
        client.post("/api/demo/start")
        first = client.post("/api/demo/tick").json()
        self.assertEqual(first["mode"], "visual_candidate")
        self.assertFalse(first["audio_search_allowed"])

        runtime.motor_guard_until_s = 0.0
        runtime._silent_visual_candidate_since_s = time.time() - 1.0
        state = client.post("/api/demo/tick").json()

        self.assertEqual(state["mode"], "audio_search")
        self.assertTrue(state["audio_search_allowed"])
        self.assertEqual(state["state"]["last_event"], "silent_visual_audio_search")
        self.assertNotEqual(state["pose"]["yaw"], 1500)

    def test_audio_search_continues_yaw_before_visual_reframe(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.LEFT)
        silent = FaceTarget(260, 90, 380, 260, label="silent", score=0.9)
        runtime.vision = FixedVision(silent)
        runtime.motion_interval_s = 0.0
        client = TestClient(app)

        client.post(
            "/api/config/vision-processing",
            json={
                "active_speaker_enabled": True,
                "tracking_strategy": "crowded_visual_first",
                "asd_backend": "rules",
                "visual_speaker_threshold": 0.30,
                "audio_search_after_silent_visual": True,
                "silent_visual_hold_s": 0.1,
            },
        )
        client.post("/api/demo/start")
        client.post("/api/demo/tick")

        runtime.motor_guard_until_s = 0.0
        runtime._silent_visual_candidate_since_s = time.time() - 1.0
        first_search = client.post("/api/demo/tick").json()
        first_yaw = first_search["pose"]["yaw"]
        first_pitch = first_search["pose"]["pitch"]

        runtime.motor_guard_until_s = 0.0
        # A real search needs a new, converging bearing. Replaying an
        # unchanged acoustic direction must not drive an endless sweep.
        runtime.audio.azimuth_deg = 25.0
        runtime.vision.target = None
        runtime.vision.targets = []
        second_search = client.post("/api/demo/tick").json()

        self.assertEqual(second_search["mode"], "audio_search")
        self.assertTrue(second_search["audio_search_allowed"])
        self.assertGreater(abs(second_search["pose"]["yaw"] - 1500), abs(first_yaw - 1500))
        self.assertEqual(second_search["pose"]["pitch"], first_pitch)

    def test_until_lost_policy_keeps_first_speaker_while_visible(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.CENTER, tdoa_s=0.0, azimuth_deg=0.0)
        first_active = FaceTarget(
            120,
            80,
            260,
            260,
            label="first",
            score=0.9,
            face_id=1,
            mouth_motion_score=0.8,
            active_speaker_score=0.9,
        )
        first_silent = FaceTarget(120, 80, 260, 260, label="first", score=0.9, face_id=1)
        second_active = FaceTarget(
            380,
            80,
            520,
            260,
            label="second",
            score=0.9,
            face_id=2,
            mouth_motion_score=0.9,
            active_speaker_score=0.95,
        )
        runtime.vision = FixedVision(first_active, targets=[first_active])
        client = TestClient(app)

        client.post(
            "/api/config/vision-processing",
            json={
                "active_speaker_enabled": True,
                "tracking_strategy": "crowded_visual_first",
                "asd_backend": "rules",
                "speaker_lock_policy": "until_lost",
                "visual_speaker_threshold": 0.30,
            },
        )
        client.post("/api/demo/start")
        locked = client.post("/api/demo/tick").json()
        self.assertEqual(locked["locked_speaker_id"], 1)

        runtime.motor_guard_until_s = 0.0
        runtime.vision.target = first_silent
        runtime.vision.targets = [first_silent, second_active]
        runtime._locked_speaker_spoke_s = time.time() - 10.0
        state = client.post("/api/demo/tick").json()

        self.assertEqual(state["mode"], "speaker_locked")
        self.assertEqual(state["locked_speaker_id"], 1)
        self.assertTrue(state["visual"]["targets"][0]["locked"])
        self.assertFalse(state["visual"]["targets"][1]["locked"])
        self.assertTrue(state["visual"]["targets"][1]["active_candidate"])

    def test_turn_hold_policy_switches_after_first_speaker_goes_silent(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.CENTER, tdoa_s=0.0, azimuth_deg=0.0)
        first_active = FaceTarget(
            120,
            80,
            260,
            260,
            label="first",
            score=0.9,
            face_id=1,
            mouth_motion_score=0.8,
            active_speaker_score=0.9,
        )
        first_silent = FaceTarget(120, 80, 260, 260, label="first", score=0.9, face_id=1)
        second_active = FaceTarget(
            380,
            80,
            520,
            260,
            label="second",
            score=0.9,
            face_id=2,
            mouth_motion_score=0.9,
            active_speaker_score=0.95,
        )
        runtime.vision = FixedVision(first_active, targets=[first_active])
        client = TestClient(app)

        client.post(
            "/api/config/vision-processing",
            json={
                "active_speaker_enabled": True,
                "tracking_strategy": "crowded_visual_first",
                "asd_backend": "rules",
                "speaker_lock_policy": "turn_hold",
                "visual_speaker_threshold": 0.30,
            },
        )
        client.post("/api/demo/start")
        locked = client.post("/api/demo/tick").json()
        self.assertEqual(locked["locked_speaker_id"], 1)

        runtime.motor_guard_until_s = 0.0
        runtime.vision.target = first_silent
        runtime.vision.targets = [first_silent, second_active]
        runtime._locked_speaker_spoke_s = time.time() - 10.0
        state = client.post("/api/demo/tick").json()

        self.assertEqual(state["mode"], "speaker_locked")
        self.assertEqual(state["locked_speaker_id"], 2)
        self.assertFalse(state["visual"]["targets"][0]["locked"])
        self.assertTrue(state["visual"]["targets"][1]["locked"])

    def test_until_lost_policy_releases_after_locked_speaker_disappears(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.CENTER, tdoa_s=0.0, azimuth_deg=0.0)
        first_active = FaceTarget(
            120,
            80,
            260,
            260,
            label="first",
            score=0.9,
            face_id=1,
            mouth_motion_score=0.8,
            active_speaker_score=0.9,
        )
        second_active = FaceTarget(
            380,
            80,
            520,
            260,
            label="second",
            score=0.9,
            face_id=2,
            mouth_motion_score=0.9,
            active_speaker_score=0.95,
        )
        runtime.vision = FixedVision(first_active, targets=[first_active])
        client = TestClient(app)

        client.post(
            "/api/config/vision-processing",
            json={
                "active_speaker_enabled": True,
                "tracking_strategy": "crowded_visual_first",
                "asd_backend": "rules",
                "speaker_lock_policy": "until_lost",
                "visual_speaker_threshold": 0.30,
            },
        )
        client.post("/api/demo/start")
        locked = client.post("/api/demo/tick").json()
        self.assertEqual(locked["locked_speaker_id"], 1)

        runtime.motor_guard_until_s = 0.0
        runtime.vision.target = second_active
        runtime.vision.targets = [second_active]
        runtime._locked_speaker_seen_s = time.time() - 10.0
        state = client.post("/api/demo/tick").json()

        self.assertEqual(state["mode"], "speaker_locked")
        self.assertEqual(state["locked_speaker_id"], 2)
        self.assertTrue(state["visual"]["targets"][0]["locked"])

    def test_interruptible_policy_switches_to_much_stronger_speaker(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.CENTER, tdoa_s=0.0, azimuth_deg=0.0)
        first_active = FaceTarget(
            120,
            80,
            260,
            260,
            label="first",
            score=0.9,
            face_id=1,
            mouth_motion_score=0.25,
            active_speaker_score=0.42,
        )
        second_active = FaceTarget(
            380,
            80,
            520,
            260,
            label="second",
            score=0.9,
            face_id=2,
            mouth_motion_score=0.9,
            active_speaker_score=0.95,
        )
        runtime.vision = FixedVision(first_active, targets=[first_active])
        client = TestClient(app)

        client.post(
            "/api/config/vision-processing",
            json={
                "active_speaker_enabled": True,
                "tracking_strategy": "crowded_visual_first",
                "asd_backend": "rules",
                "speaker_lock_policy": "interruptible",
                "visual_speaker_threshold": 0.30,
            },
        )
        client.post("/api/demo/start")
        locked = client.post("/api/demo/tick").json()
        self.assertEqual(locked["locked_speaker_id"], 1)

        runtime.motor_guard_until_s = 0.0
        runtime.vision.target = first_active
        runtime.vision.targets = [first_active, second_active]
        state = client.post("/api/demo/tick").json()

        self.assertEqual(state["mode"], "speaker_locked")
        self.assertEqual(state["locked_speaker_id"], 2)
        self.assertFalse(state["visual"]["targets"][0]["locked"])
        self.assertTrue(state["visual"]["targets"][1]["locked"])

    def test_crowded_mode_marks_one_locked_target_when_detector_ids_collide(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.CENTER, tdoa_s=0.0, azimuth_deg=0.0)
        first_active = FaceTarget(
            120,
            80,
            260,
            260,
            label="first",
            score=0.9,
            face_id=7,
            mouth_motion_score=0.35,
            active_speaker_score=0.5,
        )
        second_active = FaceTarget(
            380,
            80,
            520,
            260,
            label="second",
            score=0.9,
            face_id=7,
            mouth_motion_score=0.9,
            active_speaker_score=0.95,
        )
        runtime.vision = FixedVision(first_active, targets=[first_active, second_active])
        client = TestClient(app)

        client.post(
            "/api/config/vision-processing",
            json={
                "active_speaker_enabled": True,
                "tracking_strategy": "crowded_visual_first",
                "asd_backend": "rules",
                "visual_speaker_threshold": 0.30,
            },
        )
        client.post("/api/demo/start")
        state = client.post("/api/demo/tick").json()
        locked_targets = [target for target in state["visual"]["targets"] if target["locked"]]

        self.assertEqual(state["mode"], "speaker_locked")
        self.assertEqual(len(locked_targets), 1)
        self.assertEqual(locked_targets[0]["label"], "second")
        self.assertFalse(state["visual"]["targets"][0]["locked"])
        self.assertTrue(state["visual"]["targets"][1]["locked"])

    def test_crowded_mode_audio_searches_when_no_face_is_visible(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.LEFT)
        runtime.vision = FixedVision(None)
        runtime.motion_interval_s = 0.0
        client = TestClient(app)

        client.post(
            "/api/config/vision-processing",
            json={
                "active_speaker_enabled": True,
                "tracking_strategy": "crowded_visual_first",
                "asd_backend": "rules",
                "visual_speaker_threshold": 0.30,
            },
        )
        client.post("/api/demo/start")
        state = client.post("/api/demo/tick").json()

        self.assertTrue(state["audio_search_allowed"])
        self.assertEqual(state["mode"], "audio_search")
        self.assertNotEqual(state["pose"]["yaw"], 1500)

    def test_crowded_mode_talknet_unavailable_falls_back_to_rules(self):
        os.environ.pop("LOCATOR_TALKNET_MODULE", None)
        old_repo = os.environ.get("LOCATOR_TALKNET_REPO")
        try:
            os.environ["LOCATOR_TALKNET_REPO"] = str(TEST_TMP / "missing-talknet-repo")
            app = create_app(simulated=True)
            runtime = app.state.runtime
            runtime.audio = FixedAudio(AudioDirection.CENTER, tdoa_s=0.0, azimuth_deg=0.0)
            runtime.vision = FixedVision(
                FaceTarget(
                    250,
                    70,
                    390,
                    250,
                    label="face",
                    score=0.9,
                    mouth_motion_score=0.7,
                    active_speaker_score=0.8,
                )
            )
            client = TestClient(app)

            client.post(
                "/api/config/vision-processing",
                json={
                    "active_speaker_enabled": True,
                    "tracking_strategy": "crowded_visual_first",
                    "asd_backend": "talknet",
                    "visual_speaker_threshold": 0.30,
                },
            )
            client.post("/api/demo/start")
            state = client.post("/api/demo/tick").json()

            self.assertEqual(state["asd_backend"], "talknet")
            self.assertEqual(state["asd_backend_status"]["actual"], "rules")
            self.assertTrue(state["asd_backend_status"]["fallback"])
            self.assertIn("TalkNet repo missing", state["asd_backend_status"]["message"])
            self.assertEqual(state["visual"]["targets"][0]["backend"], "rules")
        finally:
            if old_repo is None:
                os.environ.pop("LOCATOR_TALKNET_REPO", None)
            else:
                os.environ["LOCATOR_TALKNET_REPO"] = old_repo

    def test_crowded_mode_talknet_pending_does_not_fallback_to_rules(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.CENTER, tdoa_s=0.0, azimuth_deg=0.0)
        runtime.vision = FixedVision(
            FaceTarget(
                250,
                70,
                390,
                250,
                label="face",
                score=0.9,
                mouth_motion_score=0.7,
                active_speaker_score=0.8,
            )
        )
        client = TestClient(app)

        client.post(
            "/api/config/vision-processing",
            json={
                "active_speaker_enabled": True,
                "tracking_strategy": "crowded_visual_first",
                "asd_backend": "rules",
                "visual_speaker_threshold": 0.30,
            },
        )
        runtime.vision_processing = replace(runtime.vision_processing, asd_backend="talknet")
        runtime.asd_backend = PendingTalkNet()
        client.post("/api/demo/start")
        state = client.post("/api/demo/tick").json()

        self.assertEqual(state["asd_backend_status"]["actual"], "talknet")
        self.assertFalse(state["asd_backend_status"]["fallback"])
        self.assertFalse(state["visual"]["targets"][0]["active_candidate"])

    def test_crowded_mode_talknet_error_preserves_requested_backend(self):
        app = create_app(simulated=True)
        runtime = app.state.runtime
        runtime.audio = FixedAudio(AudioDirection.CENTER, tdoa_s=0.0, azimuth_deg=0.0)
        runtime.vision = FixedVision(
            FaceTarget(
                250,
                70,
                390,
                250,
                label="face",
                score=0.9,
                mouth_motion_score=0.7,
                active_speaker_score=0.8,
            )
        )
        client = TestClient(app)

        client.post(
            "/api/config/vision-processing",
            json={
                "active_speaker_enabled": True,
                "tracking_strategy": "crowded_visual_first",
                "asd_backend": "rules",
                "visual_speaker_threshold": 0.30,
            },
        )
        runtime.vision_processing = replace(runtime.vision_processing, asd_backend="talknet")
        runtime.asd_backend = BrokenTalkNet()
        client.post("/api/demo/start")
        state = client.post("/api/demo/tick").json()

        self.assertEqual(state["asd_backend_status"]["requested"], "talknet")
        self.assertEqual(state["asd_backend_status"]["actual"], "rules")
        self.assertTrue(state["asd_backend_status"]["fallback"])
        self.assertIn("test hard talknet failure", state["asd_backend_status"]["message"])


if __name__ == "__main__":
    unittest.main()
