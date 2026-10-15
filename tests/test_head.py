import unittest

from locator_demo.head import AxisConfig, DirectionConfig, HeadControllerLogic, HeadPose


class HeadLogicTests(unittest.TestCase):
    def test_clamps_two_axis_targets_to_safe_range(self):
        logic = HeadControllerLogic(
            yaw=AxisConfig(servo_id=3, center=1500, minimum=1200, maximum=1800),
            pitch=AxisConfig(servo_id=2, center=1500, minimum=1200, maximum=1800),
        )

        pose = logic.clamp_pose(HeadPose(yaw=2000, pitch=900))

        self.assertEqual(pose, HeadPose(yaw=1800, pitch=1200))

    def test_maps_direction_to_safe_yaw_targets(self):
        logic = HeadControllerLogic()

        self.assertEqual(logic.pose_for_audio_direction("left").yaw, 1300)
        self.assertEqual(logic.pose_for_audio_direction("right").yaw, 1700)
        self.assertEqual(logic.pose_for_audio_direction("left_front").yaw, 1350)
        self.assertEqual(logic.pose_for_audio_direction("right_back").yaw, 1800)

    def test_applies_visual_error_with_deadband(self):
        logic = HeadControllerLogic()

        self.assertEqual(logic.apply_visual_error(HeadPose(1500, 1500), 0.02, 0.0), HeadPose(1500, 1500))
        self.assertEqual(logic.apply_visual_error(HeadPose(1500, 1500), 0.5, -0.5), HeadPose(1600, 1600))

    def test_visual_error_respects_min_and_max_step_ranges(self):
        logic = HeadControllerLogic()

        pose = logic.apply_visual_error(
            HeadPose(1500, 1500),
            0.08,
            -0.08,
            yaw_deadband=0.02,
            pitch_deadband=0.02,
            min_yaw_delta=30,
            max_yaw_delta=40,
            min_pitch_delta=25,
            max_pitch_delta=35,
        )

        self.assertEqual(pose, HeadPose(1530, 1475))

    def test_visual_error_corrects_left_right_sign(self):
        logic = HeadControllerLogic(direction=DirectionConfig(yaw_left_sign=-1, pitch_up_sign=-1))

        pose = logic.apply_visual_error(HeadPose(1500, 1500), -0.5, 0.0, yaw_deadband=0.0, pitch_deadband=0.0)

        self.assertGreater(pose.yaw, 1500)

    def test_jogs_with_configured_axis_directions(self):
        logic = HeadControllerLogic(direction=DirectionConfig(yaw_left_sign=1, pitch_up_sign=1, manual_step=80))

        self.assertEqual(logic.jog(HeadPose(1500, 1500), "left"), HeadPose(1580, 1500))
        self.assertEqual(logic.jog(HeadPose(1500, 1500), "right"), HeadPose(1420, 1500))
        self.assertEqual(logic.jog(HeadPose(1500, 1500), "up"), HeadPose(1500, 1580))
        self.assertEqual(logic.jog(HeadPose(1500, 1500), "down"), HeadPose(1500, 1420))

    def test_audio_azimuth_moves_relative_to_current_pose(self):
        logic = HeadControllerLogic(direction=DirectionConfig(yaw_left_sign=-1))

        pose = logic.apply_audio_azimuth(
            HeadPose(1300, 1570),
            35.0,
            max_yaw_delta=150,
            min_yaw_delta=60,
            full_scale_deg=45.0,
            seek_min_yaw=1200,
            seek_max_yaw=1720,
        )

        self.assertEqual(pose.yaw, 1200)
        self.assertEqual(pose.pitch, 1570)

    def test_audio_and_visual_follow_configured_yaw_direction(self):
        logic = HeadControllerLogic(direction=DirectionConfig(yaw_left_sign=1, pitch_up_sign=-1))

        self.assertEqual(logic.pose_for_audio_direction("left").yaw, 1700)
        self.assertEqual(logic.pose_for_audio_direction("right").yaw, 1300)
        self.assertEqual(logic.apply_visual_error(HeadPose(1500, 1500), 0.5, 0.5), HeadPose(1600, 1600))


if __name__ == "__main__":
    unittest.main()
