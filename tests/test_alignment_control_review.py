"""Regression cases from the R40 control/alignment review; no physical IO."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np
from fastapi.testclient import TestClient

from locator_demo.servo_bus import ZPServoBus
from locator_demo.stereo_health import check_pair
from locator_demo.stereo_calibration import StereoRectifier
from locator_demo.web.app import create_app, MotionRequestCancelled
from test_servo_bus import FakeSerial
from test_stereo_calibration import profile


class AlignmentReviewTests(unittest.TestCase):
    def setUp(self):
        gray=np.random.default_rng(714).integers(20,235,(360,640),dtype=np.uint8)
        self.image=cv2.cvtColor(cv2.GaussianBlur(gray,(3,3),.7),cv2.COLOR_GRAY2BGR)

    def shifted(self,dy=0,dx=-22):
        return cv2.warpAffine(self.image,np.float32([[1,0,dx],[0,1,dy]]),(640,360))

    def test_mild_vertical_error_is_a_warning_not_a_false_pass(self):
        result=check_pair(self.image,self.shifted(4))
        self.assertEqual(result['status'],'degraded')
        self.assertFalse(result['mild_misalignment_blocks_tracking'])

    def test_horizontal_parallax_is_preserved_and_reported(self):
        result=check_pair(self.image,self.shifted(dx=-45))
        self.assertEqual(result['status'],'consistent')
        self.assertAlmostEqual(result['horizontal_disparity_median_px'],45,delta=.7)
        self.assertLess(result['p95_vertical_px'],1.)

    def test_negative_and_positive_vertical_errors_are_symmetric(self):
        for dy in (-18,18):
            self.assertEqual(check_pair(self.image,self.shifted(dy))['status'],'misaligned')

    def test_one_textured_patch_cannot_certify_whole_image(self):
        image=np.full_like(self.image,128);image[145:200,270:360]=self.image[145:200,270:360]
        result=check_pair(image,cv2.warpAffine(image,np.float32([[1,0,-10],[0,1,0]]),(640,360)))
        self.assertEqual(result['status'],'unverified')

    def test_reflection_is_not_a_valid_camera_rotation(self):
        p=profile();p['R']=np.diag([-1.,1.,1.]).tolist()
        with self.assertRaisesRegex(ValueError,'rotation'):
            StereoRectifier(p,allow_synthetic=True)

    def test_singular_or_noncanonical_intrinsics_cannot_load(self):
        for row in ([0.,0.,0.],[1.,0.,1.],[0.,0.,2.]):
            p=profile();p['K_right'][2]=row
            with self.assertRaises(ValueError):StereoRectifier(p,allow_synthetic=True)


class DirectCommandReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.app=create_app(simulated=True,settings_path=Path(self.tmp.name)/'settings.json')
        self.r=self.app.state.runtime;self.addCleanup(self.r.shutdown)
        self.r.config.simulated=False
        serial=FakeSerial();self.serial=serial
        self.bus=ZPServoBus(serial_factory=lambda *a,**k:serial)
        self.bus._serial=serial;self.bus._session_generation=1
        self.bus.read_position=Mock(return_value=1500)
        self.bus.read_mode=Mock(return_value=1)
        self.r.bus=self.bus;self.r._attach_bus_diagnostics()
        self.r.head=SimpleNamespace(stop=Mock(),cancel=Mock(),metrics=lambda:{'worker_active':False})

    def move(self,arm=False):
        return self.r.direct_servo_move(servo_id=2,position=1560,time_ms=500,arm=arm)

    def test_arm_false_does_not_bypass_missing_feedback(self):
        self.bus.read_position.return_value=None
        with self.assertRaisesRegex(RuntimeError,'feedback'):self.move()
        self.assertEqual(self.serial.writes,[])

    def test_arm_false_does_not_bypass_out_of_range_feedback(self):
        self.bus.read_position.return_value=878
        with self.assertRaisesRegex(RuntimeError,'limits'):self.move()
        self.assertEqual(self.serial.writes,[])

    def test_arm_false_still_checks_mode_without_enabling_torque(self):
        self.bus.read_mode.return_value=5
        with self.assertRaisesRegex(RuntimeError,'mode'):self.move()
        self.assertEqual(self.serial.writes,[])

    def test_valid_raw_command_never_enables_torque_or_rewrites_mode(self):
        self.move()
        self.assertEqual(self.serial.writes,['#002P1560T0500!'])
        self.assertFalse(self.r._motion_pose_synced)

    def test_arm_true_enables_only_once_and_checks_feedback_after_enable(self):
        with patch('locator_demo.servo_bus.time.sleep'):self.move(arm=True)
        self.assertEqual(self.serial.writes,['#002PULR!','#002P1560T0500!'])
        self.assertEqual(self.bus.read_position.call_count,3)

    def test_unexpected_enable_jump_cannot_send_direct_goal(self):
        self.bus.read_position.side_effect=[1500,1500,878]
        with patch('locator_demo.servo_bus.time.sleep'),self.assertRaisesRegex(RuntimeError,'unexpectedly'):
            self.move(arm=True)
        self.assertEqual(self.serial.writes,['#002PULR!'])

    def test_stop_during_feedback_prevents_late_position_write(self):
        def read(_):self.r.halt_motion();return 1500
        self.bus.read_position.side_effect=read
        with self.assertRaises(MotionRequestCancelled):self.move()
        self.assertEqual(self.serial.writes,[])

    def test_raw_endpoint_cannot_replace_a_running_automatic_goal(self):
        self.r.running=True
        with self.assertRaisesRegex(RuntimeError,'Stop automatic'):self.move()
        self.assertEqual(self.serial.writes,[])

    def test_still_running_worker_blocks_raw_command(self):
        self.r.head.metrics=lambda:{'worker_active':True}
        with self.assertRaisesRegex(RuntimeError,'worker'):self.move()
        self.assertEqual(self.serial.writes,[])


class CameraReplacementReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.app=create_app(simulated=True,settings_path=Path(self.tmp.name)/'settings.json')
        self.r=self.app.state.runtime;self.addCleanup(self.r.shutdown)

    def test_reloading_camera_cancels_old_target_motion_and_preflight_result(self):
        self.r.running=True;self.r._stereo_preflight={'status':'consistent'}
        old_generation=self.r.motion_generation();self.r.head.cancel=Mock()
        with TestClient(self.app) as client:
            response=client.post('/api/config/camera',json={})
        self.assertEqual(response.status_code,200);self.assertFalse(self.r.running)
        self.assertGreater(self.r.motion_generation(),old_generation)
        self.r.head.cancel.assert_called()
        self.assertEqual(self.r._stereo_preflight['status'],'not_checked')

    def test_mild_alignment_warning_does_not_block_two_dimensional_following(self):
        self.r.features=replace(self.r.features,camera_enabled=True,visual_enabled=True)
        self.r.vision.startup_health_check=Mock(return_value={'status':'degraded','message':'fixture: 4px'})
        with TestClient(self.app) as client:response=client.post('/api/demo/start')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['stereo_preflight']['status'],'degraded')


if __name__=='__main__':unittest.main()
