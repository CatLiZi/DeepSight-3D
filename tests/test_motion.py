from types import SimpleNamespace
import unittest

import numpy as np

from locator_demo.head import AxisConfig, HeadPose
from locator_demo.motion import SmoothMotionPlanner, SmoothHeadHardware
from locator_demo.settings import ControlConfig


class MotionTests(unittest.TestCase):
    def test_bounds_velocity_acceleration_jerk_and_reaches_target(self):
        planner = SmoothMotionPlanner(HeadPose(1500, 1500))
        self.assertTrue(planner.retarget(HeadPose(1780, 1230), 0))
        times = np.linspace(0, planner.duration, 2001)
        states = np.array([planner.sample(t) for t in times])
        self.assertTrue(np.all(states[:, :, 0] >= 1200 - 1e-6))
        self.assertTrue(np.all(states[:, :, 0] <= 1800 + 1e-6))
        self.assertLessEqual(np.abs(states[:, :, 1]).max(), 180.0001)
        self.assertLessEqual(np.abs(states[:, :, 2]).max(), 600.0001)
        jerk = np.diff(states[:, :, 2], axis=0) / (times[1] - times[0])
        self.assertLessEqual(np.abs(jerk).max(), 3000.1)
        np.testing.assert_allclose(states[-1, :, 0], [1780, 1230], atol=1e-7)
        np.testing.assert_allclose(states[[0, -1], :, 1:], 0, atol=1e-7)

    def test_retarget_preserves_position_velocity_and_acceleration(self):
        planner = SmoothMotionPlanner(HeadPose(1500, 1500))
        planner.retarget(HeadPose(1750, 1450), 0)
        before = planner.sample(.6)
        self.assertTrue(planner.retarget(HeadPose(1320, 1620), .6))
        np.testing.assert_allclose(planner.sample(.6), before, atol=1e-7)
        end = np.array(planner.sample(.6 + planner.duration))
        np.testing.assert_allclose(end[:, 0], [1320, 1620], atol=1e-6)
        np.testing.assert_allclose(end[:, 1:], 0, atol=1e-6)

    def test_reversal_near_limits_never_exceeds_physical_bounds(self):
        planner = SmoothMotionPlanner(HeadPose(1790, 1500))
        planner.retarget(HeadPose(1800, 1500), 0)
        planner.retarget(HeadPose(1200, 1500), .1)
        samples = np.array([planner.sample(t) for t in np.linspace(.1, planner.started + planner.duration, 1000)])
        self.assertGreaterEqual(samples[:, :, 0].min(), 1200 - 1e-6)
        self.assertLessEqual(samples[:, :, 0].max(), 1800 + 1e-6)

    def test_target_updates_produce_short_continuous_commands_and_cancel_holds(self):
        commands = []
        head = SimpleNamespace(yaw=AxisConfig(servo_id=0), pitch=AxisConfig(servo_id=2),
                               move_to=lambda pose, **kwargs: commands.append((pose, kwargs)))
        now = [0.]
        hardware = SmoothHeadHardware(head, initial=HeadPose(1500, 1500), config=ControlConfig(),
                                      clock=lambda: now[0], autostart=False)
        hardware.move_to(HeadPose(1700, 1400), time_ms=100)
        for i in range(1, 151):
            now[0] = i * .02
            hardware.step()
        self.assertGreater(len(commands), 50)
        self.assertTrue(all(kwargs['time_ms'] == 40 for _pose, kwargs in commands))
        deltas = [abs(b[0].yaw - a[0].yaw) for a, b in zip(commands, commands[1:])]
        self.assertLessEqual(max(deltas), 5)
        self.assertEqual(commands[-1][0], HeadPose(1700, 1400))
        hardware.move_to(HeadPose(1300, 1700), time_ms=100)
        now[0] += .3
        hardware.step()
        hardware.cancel()
        count = len(commands)
        now[0] += 3
        hardware.step()
        self.assertEqual(len(commands), count)


if __name__ == '__main__':
    unittest.main()
