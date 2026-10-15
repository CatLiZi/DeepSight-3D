import unittest

import numpy as np

from locator_demo.camera import FfmpegCameraSnapshot
from locator_demo.vision import FaceTarget


class CameraPipelineTests(unittest.TestCase):
    def test_filter_limits_standard_camera_output_to_five_fps(self):
        camera = FfmpegCameraSnapshot(device_name="Integrated Camera", video_size="1280x720")

        vf = camera._video_filter("1280x720", crop_left_half=False)

        self.assertIn("fps=15", vf)
        self.assertIn("scale=640:-1", vf)

    def test_filter_crops_stereo_camera_before_scaling(self):
        camera = FfmpegCameraSnapshot(device_name="USB Camera", video_size="2560x800", crop_left_half=True)

        vf = camera._video_filter("2560x800", crop_left_half=True)

        self.assertIn("crop=1280:800:0:0", vf)
        self.assertIn("fps=5", vf)

    def test_filter_uses_configured_output_fps(self):
        camera = FfmpegCameraSnapshot(device_name="USB Camera", video_size="1280x800", crop_left_half=False, fps=12)

        fps = camera._resolved_output_fps("1280x800", crop_left_half=False)
        vf = camera._video_filter("1280x800", crop_left_half=False, fps=fps)

        self.assertEqual(fps, 12)
        self.assertIn("fps=12", vf)

    def test_filter_uses_configured_output_width(self):
        camera = FfmpegCameraSnapshot(
            device_name="USB Camera",
            video_size="1280x800",
            crop_left_half=False,
            output_width=1280,
        )

        width, height = camera._scaled_dimensions("1280x800", crop_left_half=False, output_width=camera.output_width)
        vf = camera._video_filter("1280x800", crop_left_half=False, fps=10, output_width=camera.output_width)

        self.assertEqual((width, height), (1280, 800))
        self.assertIn("scale=1280:-1", vf)

    def test_scrfd_options_are_sanitized(self):
        camera = FfmpegCameraSnapshot(
            face_detector_backend="invalid",
            scrfd_threshold=2.0,
            scrfd_input_size=9999,
        )

        self.assertEqual(camera.face_detector_backend, "mediapipe")
        self.assertEqual(camera.scrfd_threshold, 1.0)
        self.assertEqual(camera.scrfd_input_size, 1280)

    def test_scrfd_detection_converts_bbox_and_keypoints_to_face_target(self):
        camera = FfmpegCameraSnapshot(face_detector_backend="scrfd")
        gray = np.zeros((400, 640), dtype=np.uint8)

        target = camera._face_target_from_scrfd_detection(
            0,
            [100, 80, 180, 180, 0.91],
            [
                [118, 112],
                [162, 112],
                [140, 135],
                [125, 158],
                [155, 158],
            ],
            gray,
        )

        self.assertIsNotNone(target)
        self.assertEqual(target.label, "face")
        self.assertEqual(target.backend, "scrfd")
        self.assertEqual(target.tracking_source, "scrfd_kps")
        self.assertAlmostEqual(target.score, 0.91)
        self.assertGreater(target.tracking_x, 100)
        self.assertLess(target.tracking_x, 180)
        self.assertGreater(target.tracking_y, 80)
        self.assertLess(target.tracking_y, 180)

    def test_stop_closes_mediapipe_detectors(self):
        class DummyDetector:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        camera = FfmpegCameraSnapshot(device_name="USB Camera", video_size="1280x800", crop_left_half=False)
        face = DummyDetector()
        pose = DummyDetector()
        camera._face_landmarker = face
        camera._pose_landmarker = pose

        camera.stop()

        self.assertTrue(face.closed)
        self.assertTrue(pose.closed)
        self.assertIsNone(camera._face_landmarker)
        self.assertIsNone(camera._pose_landmarker)

    def test_target_stabilizer_holds_brief_detection_dropouts(self):
        camera = FfmpegCameraSnapshot(device_name="USB Camera", video_size="1280x800", crop_left_half=False)
        camera._frame_width = 640
        camera._frame_height = 400
        target = FaceTarget(
            100,
            100,
            180,
            180,
            label="face",
            score=0.9,
            face_id=3,
            mouth_motion_score=0.4,
            active_speaker_score=0.6,
            tracking_x=140,
            tracking_y=140,
            tracking_source="test",
        )

        first = camera._stabilize_targets([target], now_s=10.0)
        missed = camera._stabilize_targets([], now_s=10.2)
        expired = camera._stabilize_targets([], now_s=10.6)

        self.assertEqual(first, [target])
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0].face_id, 3)
        self.assertEqual(missed[0].tracking_source, "test_held")
        self.assertEqual(missed[0].mouth_motion_score, 0.0)
        self.assertEqual(missed[0].active_speaker_score, 0.0)
        self.assertEqual(expired, [])

    def test_target_stabilizer_smooths_matched_faces(self):
        camera = FfmpegCameraSnapshot(device_name="USB Camera", video_size="1280x800", crop_left_half=False)
        camera._frame_width = 640
        camera._frame_height = 400
        previous = FaceTarget(
            100,
            100,
            180,
            180,
            label="face",
            score=0.8,
            face_id=5,
            tracking_x=140,
            tracking_y=140,
            tracking_source="previous",
        )
        current = FaceTarget(
            120,
            100,
            200,
            180,
            label="face",
            score=0.9,
            face_id=1,
            tracking_x=160,
            tracking_y=140,
            tracking_source="current",
        )

        camera._stabilize_targets([previous], now_s=20.0)
        smoothed = camera._stabilize_targets([current], now_s=20.1)[0]

        self.assertEqual(smoothed.face_id, 5)
        self.assertGreater(smoothed.x1, previous.x1)
        self.assertLess(smoothed.x1, current.x1)
        self.assertGreater(smoothed.tracking_x, previous.tracking_x)
        self.assertLess(smoothed.tracking_x, current.tracking_x)

    def test_target_stabilizer_assigns_unique_ids_for_colliding_new_faces(self):
        camera = FfmpegCameraSnapshot(device_name="USB Camera", video_size="1280x800", crop_left_half=False)
        camera._frame_width = 640
        camera._frame_height = 400
        first = FaceTarget(
            100,
            100,
            180,
            180,
            label="first",
            score=0.9,
            face_id=0,
            tracking_x=140,
            tracking_y=140,
            tracking_source="detector",
        )
        same_first = FaceTarget(
            104,
            102,
            184,
            182,
            label="first",
            score=0.9,
            face_id=0,
            tracking_x=144,
            tracking_y=142,
            tracking_source="detector",
        )
        second_with_colliding_raw_id = FaceTarget(
            420,
            100,
            500,
            180,
            label="second",
            score=0.9,
            face_id=0,
            tracking_x=460,
            tracking_y=140,
            tracking_source="detector",
        )

        locked_id = camera._stabilize_targets([first], now_s=30.0)[0].face_id
        stabilized = camera._stabilize_targets([same_first, second_with_colliding_raw_id], now_s=30.1)
        ids = [target.face_id for target in stabilized]

        self.assertIn(locked_id, ids)
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2)


if __name__ == "__main__":
    unittest.main()
