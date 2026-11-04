import unittest,time,tempfile,json,subprocess,sys
from types import SimpleNamespace
from pathlib import Path
from locator_demo.array_pose import FeedbackAlignedYaw,fit_yaw_calibration
from locator_demo.settings import AudioCaptureConfig,SavedSettings,save_settings,load_settings
from test_four_mic import pcm,EARS,BODY
import test_four_mic as fixtures
from locator_demo.ear_localization import EarPairLocalizer
from locator_demo.pair_fusion import FourMicLocalizer


class DynamicYawTests(unittest.TestCase):
    def args(self,t=11.,moving=False):
        position=1958+(t-10)*100
        return dict(now=t,wall_now=100+t,observed=t-.03,window_s=.192,
            yaw=dict(valid=True,position=position-20,ts=100+t-.2),
            pitch=dict(valid=True,position=1555,ts=100+t-.2),estimated_yaw=position,
            moving=moving,scale=10.,zero=1958.,sign=1,yaw_limits=(1404,2500),pitch_center=1555,session=1)

    def warm(self,**overrides):
        h=FeedbackAlignedYaw();last=None
        for i in range(16):
            a=self.args(10+i*.1,True);a.update(overrides);last=h.resolve(**a)
        return h,last

    def test_stationary_uses_measured_position_not_command(self):
        h=FeedbackAlignedYaw();a=self.args();a['estimated_yaw']=2300
        result=h.resolve(**a);self.assertAlmostEqual(result['head_yaw_deg'],8.)

    def test_actual_feedback_zero_is_separate_from_saved_command_center(self):
        h=FeedbackAlignedYaw();a=self.args();a['yaw']['position']=1958
        self.assertEqual(h.resolve(**a)['head_yaw_deg'],0.)

    def test_motion_uses_audio_midpoint_not_current_trajectory(self):
        h,r=self.warm();self.assertIsNotNone(r)
        self.assertAlmostEqual(r['head_yaw_deg'],13.74,places=5)
        self.assertGreater(r['window_rotation_deg'],0)
        self.assertTrue(r['time_aligned'])

    def test_negative_motor_direction_maps_to_positive_physical_left(self):
        h=FeedbackAlignedYaw();a=self.args();a['sign']=-1
        self.assertAlmostEqual(h.resolve(**a)['head_yaw_deg'],-8.)

    def test_motion_without_feedback_history_waits(self):
        h=FeedbackAlignedYaw();self.assertIsNone(h.resolve(**self.args(moving=True)))

    def test_future_audio_and_nonfinite_clocks_rejected(self):
        for value in (float('nan'),'wrong'):
            h=FeedbackAlignedYaw();a=self.args();a['now']=value
            self.assertIsNone(h.resolve(**a))
        h=FeedbackAlignedYaw();a=self.args();a['observed']=12.
        self.assertIsNone(h.resolve(**a))

    def test_old_audio_and_stale_feedback_rejected(self):
        for key in ('observed','feedback'):
            h=FeedbackAlignedYaw();a=self.args()
            if key=='observed':a['observed']=10.
            else:a['yaw']['ts']=100.
            self.assertIsNone(h.resolve(**a))

    def test_failed_feedback_cannot_be_replaced_by_model(self):
        h=FeedbackAlignedYaw();a=self.args();a['yaw']['valid']=False
        self.assertIsNone(h.resolve(**a))

    def test_uncalibrated_scale_does_not_use_control_gain(self):
        h=FeedbackAlignedYaw();a=self.args();a['scale']=None
        self.assertIsNone(h.resolve(**a))

    def test_unverified_pitch_geometry_preserves_only_acoustic_support(self):
        h=FeedbackAlignedYaw();a=self.args();a['pitch']['position']=1655
        self.assertIsNone(h.resolve(**a))

    def test_new_serial_session_cannot_reuse_trajectory_history(self):
        h,_=self.warm();a=self.args(11.6,True);a['session']=2
        self.assertIsNone(h.resolve(**a))

    def test_observation_gap_cannot_be_interpolated(self):
        h,_=self.warm();a=self.args(12.5,True)
        self.assertIsNone(h.resolve(**a))

    def test_large_measured_model_residual_is_rejected(self):
        h,_=self.warm();a=self.args(11.6,True);a['yaw']['position']+=100
        self.assertIsNone(h.resolve(**a))

    def test_motion_blur_beyond_window_budget_is_rejected(self):
        h,r=self.warm(scale=1.);self.assertIsNone(r)

    def test_stopping_does_not_immediately_call_old_moving_audio_stationary(self):
        h,_=self.warm();self.assertIsNone(h.resolve(**self.args(11.6,False)))
        self.assertIsNotNone(h.resolve(**self.args(11.9,False)))

    def test_angular_uncertainty_includes_window_rotation(self):
        h,r=self.warm();self.assertGreater(r['pose_uncertainty_deg'],12.)


class CalibrationTests(unittest.TestCase):
    def test_three_measured_points_find_scale_and_feedback_neutral(self):
        r=fit_yaw_calibration([[1658,-30],[1958,0],[2258,30]])
        self.assertAlmostEqual(r['body_yaw_units_per_degree'],10.)
        self.assertAlmostEqual(r['body_yaw_zero_position'],1958.)

    def test_reverse_direction_is_reported_not_silently_applied(self):
        r=fit_yaw_calibration([[1658,30],[1958,0],[2258,-30]])
        self.assertEqual(r['measured_left_sign'],-1)

    def test_invalid_or_one_sided_measurements_rejected(self):
        for points in ([[1958,0],[2258,30]],[[1958,0],[2058,10],[2258,30]],
                [[1658,-30],[1958,10],[2258,0]],[[1658,-30],[1958,0],[1958,30]]):
            with self.assertRaises(ValueError):fit_yaw_calibration(points)

    def test_bad_fit_does_not_invent_angle_scale(self):
        with self.assertRaises(ValueError):fit_yaw_calibration([[1658,-10],[1958,0],[2258,60]])

    def test_calibration_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'settings.json';s=SavedSettings(audio_capture=AudioCaptureConfig(body_yaw_units_per_degree=10.,body_yaw_zero_position=1958.))
            save_settings(p,s);self.assertEqual(load_settings(p).audio_capture.body_yaw_zero_position,1958.)

    def test_invalid_zero_position_rejected(self):
        for x in (float('nan'),True,3000):
            with self.assertRaises(ValueError):AudioCaptureConfig(body_yaw_zero_position=x)

    def test_cli_applies_only_geometry_fields_and_keeps_original_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=root/'settings.json';save_settings(config,SavedSettings())
            original=config.read_bytes();data=json.loads(original);sign=data['direction']['yaw_left_sign']
            measurements=root/'points.json';measurements.write_text(json.dumps([[1700,-20*sign],[1900,0],[2100,20*sign]]))
            tool=Path(__file__).resolve().parents[1]/'tools/calibrate_array_yaw.py'
            result=subprocess.run([sys.executable,str(tool),str(measurements),'--output',str(root/'fit.json'),'--apply-config',str(config)],capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            new=json.loads(config.read_text(encoding='utf-8'))
            self.assertEqual(new['axis_limits'],data['axis_limits']);self.assertEqual(new['control'],data['control'])
            self.assertEqual(new['direction'],data['direction'])
            self.assertEqual(config.with_suffix('.json.before-array-yaw').read_bytes(),original)
            self.assertEqual(new['audio_capture']['body_yaw_zero_position'],1900)

    def test_cli_does_not_overwrite_config_when_direction_conflicts(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=root/'settings.json';save_settings(config,SavedSettings())
            original=config.read_bytes();sign=json.loads(original)['direction']['yaw_left_sign']
            measurements=root/'points.json';measurements.write_text(json.dumps([[1700,20*sign],[1900,0],[2100,-20*sign]]))
            tool=Path(__file__).resolve().parents[1]/'tools/calibrate_array_yaw.py'
            result=subprocess.run([sys.executable,str(tool),str(measurements),'--output',str(root/'fit.json'),'--apply-config',str(config)],capture_output=True)
            self.assertNotEqual(result.returncode,0);self.assertEqual(config.read_bytes(),original)


class RuntimeFeedbackTests(unittest.TestCase):
    def test_periodic_feedback_supersedes_stale_bus_cache(self):
        r=fixtures.CachedPoseTests().runtime()
        for row in r.bus._position_reads.values():row['ts']=time.time()-10
        r._feedback={0:dict(position=1958,ts=time.time(),valid=True),2:dict(position=1555,ts=time.time(),valid=True)}
        self.assertEqual(r._audio_pose_context()['head_yaw_deg'],0.)

    def test_newer_failed_periodic_feedback_is_not_hidden(self):
        r=fixtures.CachedPoseTests().runtime();r._feedback={0:dict(position=None,ts=time.time(),valid=False)}
        self.assertIsNone(r._audio_pose_context())

    def test_stationary_calibrated_feedback_works_away_from_center(self):
        r=fixtures.CachedPoseTests().runtime();r.audio_capture.body_yaw_units_per_degree=10.
        r.audio_capture.body_yaw_zero_position=1958.;r.bus._position_reads[0]['position']=2258
        self.assertAlmostEqual(r._audio_pose_context()['head_yaw_deg'],30.)

    def test_pair_transform_after_left_and_right_head_turns(self):
        for yaw in (-40.,-20.,20.,40.):
            local=FourMicLocalizer(EarPairLocalizer(EARS),BODY)
            values=pcm(ear_angle=15-yaw,body_angle=15,seed=71)
            for i in range(8):
                t=10+i*.02
                result=local.estimate(values,speech_probability=.95,body_speech_probability=.95,
                    sample_end=len(values)+960*i,observed_monotonic=t,
                    pose=dict(valid=True,head_yaw_deg=yaw,pose_uncertainty_deg=13.,observed_monotonic=t,time_aligned=True,basis='synthetic_known_rotation'))
            self.assertTrue(local.fusion['body_direction_used'])
            self.assertAlmostEqual(result.azimuth_deg,15-yaw,delta=4.)


if __name__=='__main__':unittest.main()
