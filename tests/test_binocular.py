"""Two-view behaviour, ambiguity rejection and motion gating without hardware."""
from dataclasses import replace
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

import cv2
import numpy as np

from locator_demo.binocular import BinocularFusion
from locator_demo.camera import FfmpegCameraSnapshot, VisionFrameResult, VisionStatus
from locator_demo.stereo_vision import StereoVisionTracker, prepare_stereo_eyes
from locator_demo.vision import FaceTarget
from locator_demo.web.app import DemoRuntime, RuntimeConfig


def face(x=100, y=80, track=1, score=.9):
    return FaceTarget(x, y, x+80, y+100, face_id=track, score=score, tracking_source="scrfd_kps")


def fixture():
    rng = np.random.default_rng(83)
    patch = rng.integers(40, 220, (100, 80, 3), dtype=np.uint8)
    images = {eye: rng.integers(30, 230, (360, 640, 3), dtype=np.uint8) for eye in ("left", "right")}
    a, b = face(), face(120)
    images["left"][80:180, 100:180] = patch
    images["right"][80:180, 120:200] = patch
    health = {eye: {"valid": True, "quality_score": .8} for eye in images}
    return images, health, a, b


class BinocularArbitrationTests(unittest.TestCase):
    def test_right_face_is_used_even_when_left_image_is_valid(self):
        images, health, _, b = fixture()
        fusion = BinocularFusion()
        result = fusion.update(images, {"left": [], "right": [b]}, health, now=1)
        self.assertEqual(result["eye"], "right")
        self.assertEqual(result["target"].source_eye, "right")
        self.assertFalse(result["control_ready"])
        for now in (1.1, 1.2):
            result = fusion.update(images, {"left": [], "right": [b]}, health, now=now)
        self.assertTrue(result["control_ready"])

    def test_same_appearance_needs_temporal_confirmation_then_one_person(self):
        images, health, a, b = fixture()
        fusion = BinocularFusion()
        for now in (1, 1.1):
            result = fusion.update(images, {"left": [a], "right": [b]}, health, now=now)
            self.assertEqual(len(result["summary"]["people"]), 2)
        result = fusion.update(images, {"left": [a], "right": [b]}, health, now=1.2)
        self.assertEqual(len(result["summary"]["people"]), 1)
        self.assertEqual(result["targets"]["left"][0].face_id, result["targets"]["right"][0].face_id)
        self.assertEqual(result["summary"]["associations"][0]["status"], "tentative_appearance")
        self.assertFalse(result["summary"]["calibrated"])
        self.assertFalse(result["summary"]["depth_enabled"])

    def test_occluding_selected_eye_keeps_other_observation_and_person_id(self):
        images, health, a, b = fixture()
        fusion = BinocularFusion()
        for now in (1, 1.1, 1.2, 1.3):
            result = fusion.update(images, {"left": [a], "right": [b]}, health, now=now)
        gid = result["target"].face_id
        health["left"]["valid"] = False
        result = fusion.update(images, {"left": [a], "right": [b]}, health, now=1.4)
        self.assertEqual(result["eye"], "right")
        self.assertEqual(result["target"].face_id, gid)
        self.assertFalse(result["control_ready"])
        self.assertEqual(result["targets"]["left"], [])

    def test_held_left_face_cannot_override_fresh_right_face(self):
        images, health, a, b = fixture()
        result = BinocularFusion().update(images, {"left": [replace(a, tracking_source="scrfd_held")], "right": [b]}, health, now=1)
        self.assertEqual(result["eye"], "right")
        self.assertEqual(len(result["summary"]["people"]), 1)

    def test_ambiguous_similar_faces_remain_separate(self):
        images, health, a, b = fixture()
        c = face(210, track=2)
        images["left"][80:180, 210:290] = images["left"][80:180, 100:180]
        fusion = BinocularFusion()
        for now in (1, 1.1, 1.2, 1.3):
            result = fusion.update(images, {"left": [a, c], "right": [b]}, health, now=now)
        self.assertEqual(len(result["summary"]["people"]), 3)
        self.assertEqual(result["summary"]["associations"], [])

    def test_different_appearance_cannot_merge_from_box_position_alone(self):
        images, health, a, b = fixture()
        images["right"] = 255-images["right"]
        fusion = BinocularFusion()
        for now in (1, 1.1, 1.2):
            result = fusion.update(images, {"left": [a], "right": [b]}, health, now=now)
        self.assertEqual(len(result["summary"]["people"]), 2)

    def test_flat_face_crop_is_insufficient_for_association(self):
        images, health, a, b = fixture()
        images["left"][:] = images["right"][:] = 120
        fusion = BinocularFusion()
        for now in (1, 1.1, 1.2):
            result = fusion.update(images, {"left": [a], "right": [b]}, health, now=now)
        self.assertEqual(result["summary"]["associations"], [])

    def test_no_cross_eye_pixel_averaging(self):
        images, health, a, b = fixture()
        fusion = BinocularFusion(tracking_eye="right")
        for now in (1, 1.1, 1.2):
            result = fusion.update(images, {"left": [a], "right": [b]}, health, now=now)
        self.assertEqual(result["target"].center, b.center)
        self.assertEqual(result["summary"]["coordinate_frame"], "selected_eye_pixels")

    def test_manual_control_eye_does_not_disable_other_eye_detection(self):
        images, health, a, b = fixture()
        result = BinocularFusion("left").update(images, {"left": [a], "right": [b]}, health, now=1)
        self.assertEqual(result["eye"], "left")
        self.assertEqual(len(result["targets"]["right"]), 1)

    def test_both_bad_drop_target_and_ready(self):
        images, health, a, b = fixture()
        fusion = BinocularFusion()
        fusion.update(images, {"left": [a], "right": [b]}, health, now=1)
        for q in health.values():
            q["valid"] = False
        result = fusion.update(images, {"left": [a], "right": [b]}, health, now=1.1)
        self.assertIsNone(result["target"])
        self.assertFalse(result["control_ready"])
        self.assertEqual(result["summary"]["people"], [])

    def test_expired_view_track_gets_new_person_id(self):
        images, health, a, _ = fixture()
        fusion = BinocularFusion()
        first = fusion.update(images, {"left": [a]}, health, now=1)["target"].face_id
        second = fusion.update(images, {"left": [a]}, health, now=3)["target"].face_id
        self.assertNotEqual(first, second)

    def test_small_score_changes_do_not_flip_control_view(self):
        images, health, a, b = fixture()
        fusion = BinocularFusion()
        for i in range(15):
            result = fusion.update(images, {"left": [replace(a, score=.9+(i%2)*.02)],
                                             "right": [replace(b, score=.9+((i+1)%2)*.02)]}, health, now=1+i*.1)
            if i == 2:
                selected = result["eye"]
            if i > 2:
                self.assertEqual(result["eye"], selected)


class BinocularPipelineTests(unittest.TestCase):
    def tracker(self):
        tracker = StereoVisionTracker(video_size="640x180", output_width=320, focus_assist=False, multi_scale_search=False)
        tracker.capture.start = lambda: None
        original_metrics = tracker.capture.metrics
        tracker.capture.metrics = lambda: {**original_metrics(), "ok": True}
        self.addCleanup(tracker.stop)
        return tracker

    def feed(self, tracker, image):
        tracker.capture._accept(cv2.imencode('.jpg', image)[1].tobytes())
        seq = tracker.capture.latest().sequence
        tracker._start()
        deadline = time.perf_counter()+3
        while tracker.capture_metrics()["detection_sequence"] != seq and time.perf_counter()<deadline:
            time.sleep(.005)
        self.assertEqual(tracker.capture_metrics()["detection_sequence"], seq, tracker._latest_error)
        return tracker.read()

    def test_temporal_gray_mouth_and_local_tracks_are_isolated(self):
        tracker = self.tracker()
        pixels = np.random.default_rng(43).integers(30, 220, (180, 320, 3), dtype=np.uint8)
        calls = []
        def detect(image):
            calls.append(None if tracker._previous_gray is None else tracker._previous_gray[0,0])
            tracker._previous_gray = np.full((180, 320), len(calls), np.uint8)
            tracker._previous_mouth_open[0] = len(calls)
            return [face(10, 10, track=0)]
        tracker._detect_image_targets = detect
        tracker._process_eye(pixels, "left", now=1)
        tracker._process_eye(pixels, "right", now=1)
        tracker._process_eye(pixels, "left", now=1.1)
        tracker._process_eye(pixels, "right", now=1.1)
        self.assertEqual(calls, [None, None, 1, 2])
        self.assertEqual(tracker._eye_states["left"].values["_previous_mouth_open"][0], 3)
        self.assertEqual(tracker._eye_states["right"].values["_previous_mouth_open"][0], 4)

    def test_both_views_are_detected_on_the_same_native_sample(self):
        tracker = self.tracker()
        calls = []
        tracker._detect_image_targets = lambda image: calls.append(image.shape) or [face(20, 20)]
        image = np.random.default_rng(19).integers(30, 220, (180, 640, 3), dtype=np.uint8)
        frame = self.feed(tracker, image)
        self.assertEqual(calls, [(180,320,3), (180,320,3)])
        self.assertEqual(set(frame.eye_targets), {"left", "right"})
        self.assertTrue(all(frame.eye_frames.values()))
        metrics = tracker.capture_metrics()
        self.assertEqual(metrics["per_eye"]["left"]["detection_sequence"], frame.detection_sequence)
        self.assertEqual(metrics["per_eye"]["right"]["detection_sequence"], frame.detection_sequence)
        time.sleep(.05)
        self.assertEqual(tracker.capture_metrics()["processed_frames"], 1)

    def test_one_detector_failure_does_not_block_other_view(self):
        tracker = self.tracker()
        count = 0
        def detect(image):
            nonlocal count
            count += 1
            if count == 1:
                raise RuntimeError("left failure")
            return [face(20, 20)]
        tracker._detect_image_targets = detect
        image = np.random.default_rng(11).integers(30, 220, (180, 640, 3), dtype=np.uint8)
        frame = self.feed(tracker, image)
        self.assertEqual(frame.control_eye, "right")
        self.assertTrue(frame.status.ok)
        self.assertIn("left failure", tracker.capture_metrics()["per_eye"]["left"]["error"])

    def test_stale_results_clear_both_views_and_control(self):
        tracker = self.tracker()
        tracker._detect_image_targets = lambda _: [face(20, 20)]
        image = np.random.default_rng(11).integers(30, 220, (180, 640, 3), dtype=np.uint8)
        self.feed(tracker, image)
        tracker._detection_captured_monotonic = time.perf_counter()-1
        frame = tracker.read()
        self.assertFalse(frame.control_ready)
        self.assertIsNone(frame.target)
        self.assertTrue(all(not targets for targets in frame.eye_targets.values()))

    def test_forced_bad_control_eye_still_exposes_healthy_other_eye_detections(self):
        tracker = self.tracker()
        tracker.tracking_eye = tracker.fusion.tracking_eye = "left"
        tracker._detect_image_targets = lambda _: [face(20,20)]
        image = np.random.default_rng(63).integers(30,220,(180,640,3),dtype=np.uint8)
        image[:,:320] = 255
        frame = self.feed(tracker,image)
        self.assertIsNone(frame.target)
        self.assertFalse(frame.control_ready)
        self.assertEqual(len(frame.eye_targets["right"]),1)
        self.assertEqual(frame.eye_targets["left"],[])

    def test_flat_occluded_image_is_not_usable_even_when_neither_black_nor_white(self):
        image = np.random.default_rng(71).integers(30,220,(180,640,3),dtype=np.uint8)
        image[:,:320] = 146
        _, quality = prepare_stereo_eyes(image)
        self.assertFalse(quality["left"]["valid"])
        self.assertEqual(quality["left"]["reason"], "low_texture")
        self.assertTrue(quality["right"]["valid"])

    def test_body_fallback_is_rate_limited_without_reusing_old_body_box(self):
        tracker = self.tracker()
        tracker._processing_eye_state = tracker._eye_states["left"]
        body = object()
        with patch.object(FfmpegCameraSnapshot, "_mediapipe_body_target", return_value=body) as detect, \
             patch("locator_demo.stereo_vision.time.perf_counter", side_effect=[1,1.1,1.4]):
            self.assertIs(tracker._mediapipe_body_target(None,None), body)
            self.assertIsNone(tracker._mediapipe_body_target(None,None))
            self.assertIs(tracker._mediapipe_body_target(None,None), body)
        self.assertEqual(detect.call_count, 2)


class BinocularRuntimeTests(unittest.TestCase):
    def test_handoff_cancels_future_smooth_trajectory_commands(self):
        runtime = DemoRuntime(RuntimeConfig(simulated=True))
        self.addCleanup(runtime.shutdown)
        runtime.head = SimpleNamespace(is_moving=True,cancel=Mock(),estimated_pose=runtime.pose)
        runtime.features = replace(runtime.features,camera_enabled=True,visual_enabled=True)
        runtime.vision.read = lambda: VisionFrameResult(None,640,360,"",VisionStatus(False,"fake","bad"),
            control_ready=False,binocular={"selection_reason":"no_usable_eye"})
        runtime.running=True
        runtime.move=Mock()
        runtime.tick(include_frame=False)
        runtime.head.cancel.assert_called_once()
        runtime.move.assert_not_called()

    def test_handoff_and_sensor_loss_never_issue_auto_motion(self):
        for reason in ("confirming_view", "no_usable_eye", "stale_frame"):
            with self.subTest(reason=reason):
                runtime = DemoRuntime(RuntimeConfig(simulated=True))
                self.addCleanup(runtime.shutdown)
                target = face(20, 20)
                frame = VisionFrameResult(target,640,360,"",VisionStatus(True,"fake","ok"),targets=[target],
                    binocular={"selection_reason":reason,"control_ready":False},control_eye="right",control_ready=False,
                    eye_targets={"left":[],"right":[target]})
                runtime.vision.read = lambda: frame
                runtime.features = replace(runtime.features, camera_enabled=True, visual_enabled=True, audio_enabled=False)
                runtime.running = True
                runtime.move = Mock()
                runtime._smoothed_error = (.9,.9)
                payload = runtime.tick(include_frame=False)
                runtime.move.assert_not_called()
                self.assertIsNone(runtime._smoothed_error)
                self.assertEqual(payload["visual"]["eye_targets"]["right"][0]["face_id"], 1)
                self.assertEqual(payload["visual"]["eye_frames"], {})

    def test_camera_disable_clears_all_binocular_ui_and_target_state(self):
        from locator_demo.web.app import FeatureConfigRequest
        runtime = DemoRuntime(RuntimeConfig(simulated=True))
        self.addCleanup(runtime.shutdown)
        runtime.last_eye_frames = {"right":"old"}
        runtime.last_eye_targets = {"right":[face()]}
        runtime.last_binocular = {"selected_eye":"right"}
        runtime.update_feature_config(FeatureConfigRequest(camera_enabled=False,visual_enabled=False,audio_enabled=False))
        visual = runtime.snapshot()["visual"]
        self.assertEqual(visual["eye_frames"], {})
        self.assertEqual(visual["eye_targets"], {})
        self.assertEqual(visual["binocular"], {})


if __name__ == "__main__":
    unittest.main()
