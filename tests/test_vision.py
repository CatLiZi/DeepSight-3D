import unittest

from locator_demo.vision import BodyTarget, FaceTarget, choose_active_speaker_target, choose_primary_target, normalized_center_error


class VisionLogicTests(unittest.TestCase):
    def test_chooses_largest_face_target(self):
        small = FaceTarget(10, 10, 50, 50, label="small", score=0.8)
        large = FaceTarget(100, 20, 260, 220, label="large", score=0.7)

        self.assertEqual(choose_primary_target([small, large]), large)

    def test_chooses_active_speaker_over_largest_face(self):
        large_silent = FaceTarget(100, 20, 260, 220, label="silent", score=0.8, active_speaker_score=0.2)
        small_speaker = FaceTarget(10, 10, 80, 100, label="speaker", score=0.7, active_speaker_score=0.8)

        self.assertEqual(choose_active_speaker_target([large_silent, small_speaker]), small_speaker)

    def test_normalizes_center_error(self):
        target = FaceTarget(480, 100, 640, 260, label="person", score=0.9)
        error_x, error_y = normalized_center_error(target, frame_width=640, frame_height=480)

        self.assertAlmostEqual(error_x, 0.75)
        self.assertAlmostEqual(error_y, -0.25)

    def test_target_offset_changes_center_error_direction(self):
        target = FaceTarget(280, 200, 360, 280, label="person", score=0.9)
        error_x, error_y = normalized_center_error(
            target,
            frame_width=640,
            frame_height=480,
            target_offset_x_norm=0.10,
            target_offset_y_norm=-0.05,
        )

        self.assertAlmostEqual(error_x, -0.20)
        self.assertAlmostEqual(error_y, 0.10)

    def test_face_tracking_center_drives_control_error(self):
        target = FaceTarget(
            480,
            100,
            640,
            260,
            label="person",
            score=0.9,
            tracking_x=320,
            tracking_y=240,
            tracking_source="test",
        )
        error_x, error_y = normalized_center_error(target, frame_width=640, frame_height=480)

        self.assertAlmostEqual(error_x, 0.0)
        self.assertAlmostEqual(error_y, 0.0)

    def test_body_target_uses_same_normalized_error(self):
        target = BodyTarget(280, 120, 360, 200, score=0.8, visibility=0.8)
        error_x, error_y = normalized_center_error(target, frame_width=640, frame_height=400)

        self.assertAlmostEqual(error_x, 0.0)
        self.assertAlmostEqual(error_y, -0.20)


if __name__ == "__main__":
    unittest.main()
