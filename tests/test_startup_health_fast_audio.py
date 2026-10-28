"""Startup diagnostics and prompt first-source response, with offline images."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import cv2
import numpy as np
from fastapi.testclient import TestClient

from locator_demo.stereo_health import check_pair
from locator_demo.web.app import create_app
import test_dominant_sound as core_fixture


class PairHealthTests(unittest.TestCase):
    def setUp(self):
        image=np.random.default_rng(314).integers(30,225,(360,640),dtype=np.uint8)
        self.left=cv2.cvtColor(cv2.GaussianBlur(image,(3,3),.6),cv2.COLOR_GRAY2BGR)

    def test_horizontal_disparity_is_not_treated_as_loss_of_sync(self):
        right=cv2.warpAffine(self.left,np.float32([[1,0,-18],[0,1,0]]),(640,360))
        result=check_pair(self.left,right)
        self.assertEqual(result['status'],'consistent')
        self.assertLess(result['median_vertical_px'],2.)
        self.assertFalse(result['sensor_exposure_sync_verified'])

    def test_a_large_vertical_shift_is_reported_without_mutating_images(self):
        right=cv2.warpAffine(self.left,np.float32([[1,0,-18],[0,1,24]]),(640,360))
        saved=right.copy();result=check_pair(self.left,right)
        self.assertEqual(result['status'],'misaligned')
        np.testing.assert_array_equal(right,saved);self.assertFalse(result['calibration_changed'])

    def test_blank_wall_cannot_be_claimed_as_calibration_pass(self):
        image=np.full_like(self.left,128)
        self.assertEqual(check_pair(image,image)['status'],'unverified')

    def test_black_or_white_eye_is_not_a_healthy_pair(self):
        for value in (0,255):
            self.assertEqual(check_pair(self.left,np.full_like(self.left,value))['status'],'unavailable')

    def test_missing_profile_cannot_be_claimed_as_metric_calibration(self):
        result=check_pair(self.left,self.left,alignment_loaded=False)
        self.assertEqual(result['status'],'unverified');self.assertFalse(result['calibration_changed'])


class StartupApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.app=create_app(simulated=True,settings_path=Path(self.tmp.name)/'settings.json')
        self.r=self.app.state.runtime;self.addCleanup(self.r.shutdown)
        self.r.features=replace(self.r.features,camera_enabled=True,visual_enabled=True)
        self.r.vision.startup_health_check=Mock(return_value={'status':'consistent','message':'fixture'})

    def test_start_checks_the_pair_before_enabling_automatic_control(self):
        with TestClient(self.app) as client:response=client.post('/api/demo/start')
        self.assertEqual(response.status_code,200);self.r.vision.startup_health_check.assert_called_once()
        self.assertEqual(response.json()['stereo_preflight']['status'],'consistent')

    def test_clear_misalignment_blocks_visual_automatic_start(self):
        self.r.vision.startup_health_check.return_value={'status':'misaligned','message':'alignment shifted'}
        with TestClient(self.app) as client:response=client.post('/api/demo/start')
        self.assertEqual(response.status_code,409);self.assertFalse(self.r.running)

    def test_audio_only_still_checks_preview_but_does_not_depend_on_geometry(self):
        self.r.features=replace(self.r.features,visual_enabled=False,audio_enabled=True)
        self.r.vision.startup_health_check.return_value={'status':'misaligned','message':'alignment shifted'}
        with TestClient(self.app) as client:response=client.post('/api/demo/start')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['stereo_preflight']['status'],'misaligned')


class FirstVoiceTests(unittest.TestCase):
    setUp=core_fixture.CoreEvidenceTests.setUp
    feed=core_fixture.CoreEvidenceTests.feed
    acquire=core_fixture.CoreEvidenceTests.acquire

    def test_three_good_new_observations_can_acquire_without_long_competition(self):
        for _ in range(3):state=self.feed()
        self.assertTrue(state['locked']);self.assertLess(state['current_support_s'],.24)
        self.assertEqual(state['reason'],'first_reliable_direction')

    def test_one_loud_packet_still_cannot_trigger_a_turn(self):
        self.assertFalse(self.feed(energy=100.)['locked'])

    def test_a_new_reliable_direction_after_quiet_need_not_defeat_an_absent_voice(self):
        old=self.acquire(35.)
        self.feed(audio=replace(core_fixture.observation(0.,self.clock+1.2,self.sequence+1),
            direction=core_fixture.AudioDirection.UNKNOWN,confidence=0.,doa_confidence=0.,speech_confidence=0.))
        self.clock+=1.2
        for _ in range(4):state=self.feed(-35.)
        self.assertNotEqual(state['core_id'],old['core_id'])
        self.assertLess(state['goal_yaw'],1500)

    def test_weak_third_direction_cannot_use_the_fast_path(self):
        before=self.acquire(35.)
        for _ in range(30):
            state=self.feed(0.,confidence=.2,doa_confidence=.2,speech_confidence=.55,temporal_stability=.5)
            if state['core_id']!=before['core_id']:break
        self.assertNotIn(state['reason'],('first_reliable_direction','new_direction_after_quiet'))


if __name__=='__main__':unittest.main()
