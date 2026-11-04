"""Four-mic acoustic fusion and frame safety; no physical hardware is opened."""
from dataclasses import replace
import json
import math
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from locator_demo.array_audio import WasapiArrayAudio
from locator_demo.audio import AudioDirection, AudioEstimate
from locator_demo.ear_localization import EarPairConfig,EarPairLocalizer
from locator_demo.head import HeadPose
from locator_demo.pair_fusion import FourMicLocalizer,common_spectral_support,combine_pair_directions
from locator_demo.settings import AudioCaptureConfig,SavedSettings,save_settings,load_settings
from locator_demo.web.app import AudioCaptureConfigRequest,DemoRuntime

EARS=EarPairConfig('ear_pair',2,3,True,.1355)
BODY=EarPairConfig('ear_pair',1,4,True,.0755)


def pcm(ear_angle=25.,body_angle=25.,*,n=9216,seed=91,body_source='same',body_gain=1.):
    rng=np.random.default_rng(seed)
    def source():
        raw=rng.normal(size=n+512)
        f=np.fft.rfftfreq(len(raw),1/48000)
        raw=np.fft.irfft(np.fft.rfft(raw)*((f>250)&(f<5000)),n=len(raw))
        return raw*2300/np.std(raw)
    a=source();b=a if body_source=='same' else source()
    t=np.arange(n)+240.
    ear_delay=math.sin(math.radians(ear_angle))*.1355/343*48000
    body_delay=math.sin(math.radians(body_angle))*.0755/343*48000
    values=np.stack([np.interp(t-40,np.arange(len(b)),b)*body_gain,
                     np.interp(t,np.arange(len(a)),a),
                     np.interp(t-ear_delay,np.arange(len(a)),a),
                     np.interp(t-40-body_delay,np.arange(len(b)),b)*body_gain],axis=1)
    values+=rng.normal(0,10,values.shape)
    return np.clip(values,-32768,32767).astype('<i2')


def estimate(angle,quality=.85):
    return AudioEstimate(AudioDirection.CENTER if abs(angle)<8 else
        AudioDirection.LEFT if angle>0 else AudioDirection.RIGHT,quality,0,.4,angle,
        .95,quality,3.,'speech_like',azimuth_std_deg=8.,temporal_stability=1.)


def warmed(local,values,*,pose=None,ear_p=.9,body_p=.9):
    for i in range(5):
        result=local.estimate(values,sample_end=len(values)+960*i,speech_probability=ear_p,
            body_speech_probability=body_p,pose=pose,observed_monotonic=10.+i*.02)
    return result


class FourMicDSPTests(unittest.TestCase):
    def make(self):return FourMicLocalizer(EarPairLocalizer(EARS),BODY)

    def test_body_changes_reliable_frequency_weights_without_pose_assumption(self):
        weights,detail=common_spectral_support(pcm(-25,40),EARS,BODY)
        self.assertIsNotNone(weights);self.assertGreater(detail['shared_source_coherence'],.55)
        self.assertGreaterEqual(float(weights.min()),.75);self.assertLessEqual(float(weights.max()),1.25)
        self.assertGreater(float(np.std(weights)),.01)

    def test_one_fft_cannot_claim_coherence(self):
        for n in (1024,1536,2048):
            weights,detail=common_spectral_support(pcm(n=n,body_source='other'),EARS,BODY)
            self.assertIsNone(weights);self.assertFalse(detail['support_available'])

    def test_unrelated_body_source_cannot_supply_ear_speech_or_direction_weight(self):
        values=pcm(body_source='other');local=self.make()
        result=warmed(local,values,ear_p=.01)
        self.assertEqual(result.direction,AudioDirection.UNKNOWN)
        self.assertFalse(local.fusion['body_voice_used']);self.assertFalse(local.fusion['body_spectral_used'])

    def test_body_can_assist_weak_ear_vad_only_for_the_same_coherent_voice(self):
        local=self.make();result=warmed(local,pcm(),ear_p=.1)
        self.assertTrue(local.fusion['body_voice_used']);self.assertGreaterEqual(result.speech_confidence,.9)
        self.assertAlmostEqual(result.azimuth_deg,25,delta=3)

    def test_turned_head_does_not_average_body_and_camera_angles(self):
        local=self.make();result=warmed(local,pcm(-25,35))
        self.assertAlmostEqual(result.azimuth_deg,-25,delta=3)
        self.assertFalse(local.fusion['body_direction_used']);self.assertTrue(local.fusion['body_spectral_used'])
        self.assertAlmostEqual(local.last_body.azimuth_deg,35,delta=4)

    def test_fresh_measured_pose_allows_two_pair_consensus(self):
        local=self.make();pose=dict(valid=True,head_yaw_deg=60.,observed_monotonic=10.,basis='test_measured')
        result=warmed(local,pcm(-25,35),pose=pose)
        self.assertEqual(local.fusion['status'],'pair_consensus')
        self.assertTrue(local.fusion['body_direction_used']);self.assertLessEqual(local.fusion['body_weight'],.35)
        self.assertAlmostEqual(result.azimuth_deg,-25,delta=3)

    def test_missing_stale_future_or_nan_pose_keeps_head_frame(self):
        for pose in (None,dict(valid=True,head_yaw_deg=60,observed_monotonic=8.),
                     dict(valid=True,head_yaw_deg=60,observed_monotonic=12.),
                     dict(valid=True,head_yaw_deg=float('nan'),observed_monotonic=10.)):
            local=self.make();result=warmed(local,pcm(-25,35),pose=pose)
            self.assertAlmostEqual(result.azimuth_deg,-25,delta=3);self.assertFalse(local.fusion['body_direction_used'])

    def test_bad_pose_cannot_average_opposing_peaks_into_a_fictional_middle(self):
        result,info=combine_pair_directions(estimate(-35),estimate(40),head_yaw_deg=0)
        self.assertEqual(result.azimuth_deg,-35);self.assertEqual(info['status'],'pair_conflict')

    def test_body_only_requires_valid_head_coordinate_transform(self):
        values=pcm();values[:,[1,2]]=0
        local=self.make();result=warmed(local,values)
        self.assertEqual(result.direction,AudioDirection.UNKNOWN)
        pose=dict(valid=True,head_yaw_deg=0.,observed_monotonic=10.,basis='confirmed_neutral')
        result=warmed(self.make(),values,pose=pose)
        self.assertEqual(result.noise_state,'body_pair_fallback')
        self.assertAlmostEqual(result.azimuth_deg,25,delta=4)
        self.assertGreaterEqual(result.azimuth_std_deg,18)

    def test_body_behind_head_cannot_be_folded_into_visible_front(self):
        result,info=combine_pair_directions(AudioEstimate(AudioDirection.UNKNOWN,0,0,0),
                                            estimate(60),head_yaw_deg=-50)
        self.assertEqual(result.direction,AudioDirection.UNKNOWN)
        self.assertEqual(info['reason'],'body_outside_head_front')

    def test_spectral_body_noise_cannot_bypass_missing_neural_voice_confirmation(self):
        values=pcm();values[:,[1,2]]=0
        pose=dict(valid=True,head_yaw_deg=0.,observed_monotonic=10.,basis='confirmed_neutral')
        result=warmed(self.make(),values,pose=pose,body_p=None)
        self.assertEqual(result.direction,AudioDirection.UNKNOWN)

    def test_body_clipping_or_missing_signal_does_not_regress_ears(self):
        for body_level in (0,32767):
            values=pcm();values[:,[0,3]]=body_level
            local=self.make();result=warmed(local,values)
            baseline=EarPairLocalizer(EARS)
            for i in range(5):old=baseline.estimate(values,speech_probability=.9,sample_end=len(values)+960*i)
            self.assertEqual(result,old);self.assertFalse(local.fusion['body_direction_used'])

    def test_body_temporal_reconfirmation_cannot_erase_a_trusted_ear_result(self):
        local=self.make();valid=estimate(25)
        local.ears.estimate=Mock(return_value=valid)
        local.assisted_ears.estimate=Mock(return_value=AudioEstimate(AudioDirection.UNKNOWN,0,0,.3,
                                                                     noise_state='direction_transition'))
        result=warmed(local,pcm())
        self.assertEqual(result,valid);self.assertTrue(local.fusion['baseline_ear_preserved'])

    def test_body_direction_uses_existing_motion_and_core_quality_checks(self):
        from locator_demo.audio_motion import qualify_audio_during_motion
        from locator_demo.dominant_sound import DominantSoundLock
        values=pcm(25,25);values[:,[1,2]]=0
        pose=dict(valid=True,head_yaw_deg=0.,observed_monotonic=10.,basis='test_measured')
        value=warmed(self.make(),values,pose=pose)
        admitted,context=qualify_audio_during_motion(value,active=True)
        self.assertTrue(context['admitted']);self.assertFalse(admitted.motor_suppressed)
        lock=DominantSoundLock()
        for i in range(16):
            now=10.1+i*.05
            audio=replace(admitted,observed_monotonic=now,sample_sequence=i+1)
            state=lock.update(audio,now=now,yaw=1952,units_per_degree=5.,left_sign=1,
                              yaw_min=1404,yaw_max=2500,command_window=340)
        self.assertTrue(state['locked'])

    def test_silence_noise_tone_and_body_rubbing_do_not_create_a_direction(self):
        n=9216;rng=np.random.default_rng(39)
        tone=(np.sin(np.arange(n)*2*np.pi*1000/48000)*3000).astype('<i2')
        cases=[np.zeros((n,4),'<i2'),rng.integers(-2000,2000,(n,4),dtype=np.int16),
               np.stack([tone,np.roll(tone,3),np.roll(tone,8),np.roll(tone,6)],axis=1)]
        rub=np.zeros((n,4),'<i2');rub[:,0]=rng.integers(-15000,15000,n,dtype=np.int16);cases.append(rub)
        for values in cases:
            with self.subTest(kind=len(cases)):
                result=warmed(self.make(),values)
                self.assertEqual(result.direction,AudioDirection.UNKNOWN)

    def test_reset_clears_both_histories_and_presentation(self):
        local=self.make();warmed(local,pcm());local._unknown('audio_gap')
        self.assertFalse(local.diagnostics['signal_valid'])
        self.assertEqual(local.last_body.direction,AudioDirection.UNKNOWN)
        self.assertIsNone(local.last_speech_probability)
        self.assertFalse(local.fusion['body_spectral_used'])

    def test_gain_mismatch_preserves_arrival_sign_in_both_pairs(self):
        for gain in (.3,2.5):
            local=self.make();result=warmed(local,pcm(-30,-30,body_gain=gain))
            self.assertAlmostEqual(result.azimuth_deg,-30,delta=3)
            self.assertAlmostEqual(local.last_body.azimuth_deg,-30,delta=5)


class FourMicSettingsTests(unittest.TestCase):
    def config(self,**kwargs):
        return AudioCaptureConfig(localization_mode='four_mic',ear_left_channel=2,ear_right_channel=3,
            ear_mapping_confirmed=True,ear_spacing_m=.1355,body_mapping_confirmed=True,**kwargs)

    def test_round_trip_and_api_keep_body_mapping(self):
        config=self.config()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'settings.json';save_settings(path,SavedSettings(audio_capture=config))
            self.assertEqual(load_settings(path).audio_capture,config)
        self.assertEqual(AudioCaptureConfigRequest(**config.__dict__).body_right_channel,4)

    def test_overlapping_unconfirmed_or_invalid_geometry_is_rejected(self):
        for changes in [dict(body_left_channel=2),dict(body_right_channel=1),
                        dict(body_mapping_confirmed=False),dict(body_spacing_m=float('nan')),
                        dict(body_yaw_units_per_degree=0),dict(body_yaw_units_per_degree=float('inf'))]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                replace(self.config(),**changes)

    def test_legacy_pair_keeps_body_excluded(self):
        config=AudioCaptureConfig(localization_mode='ear_pair',ear_left_channel=2,ear_right_channel=3,
                                  ear_mapping_confirmed=True,ear_spacing_m=.1355)
        self.assertEqual(config.ear_pair_config(),EARS)
        audio=WasapiArrayAudio(ear_config=EARS)
        self.assertEqual(audio.capture_metrics()['active_channels'],[2,3])


class FourMicAdapterTests(unittest.TestCase):
    def setUp(self):
        self.audio=WasapiArrayAudio(ear_config=EARS,body_config=BODY)
        self.audio.capture.start=lambda:None
        self.audio.capture.metrics=lambda:dict(ok=True,sample_clock_valid=True)
        self.audio.voice=SimpleNamespace(update=Mock(return_value=.9),reset=Mock(),metrics=lambda:{})
        self.audio.body_voice=SimpleNamespace(update=Mock(return_value=.85),reset=Mock(),metrics=lambda:{})

    def feed(self,t,values=None,overflow=False):
        values=pcm(n=960) if values is None else values
        with patch('locator_demo.capture.time.perf_counter',return_value=t):
            self.audio.capture._callback(values.tobytes(),len(values),
                SimpleNamespace(currentTime=1.,inputBufferAdcTime=1.),SimpleNamespace(input_overflow=overflow))

    def read(self,t):
        with patch('locator_demo.array_audio.time.perf_counter',return_value=t):return self.audio.read_estimate()

    def test_independent_vads_receive_correct_pairs_and_identical_pcm_clock(self):
        values=pcm(n=960);self.feed(10.,values);self.read(10.01)
        np.testing.assert_array_equal(self.audio.voice.update.call_args.args[0],values[:,[1,2]])
        np.testing.assert_array_equal(self.audio.body_voice.update.call_args.args[0],values[:,[0,3]])
        self.assertEqual(self.audio.voice.update.call_args.kwargs['sample_start'],
                         self.audio.body_voice.update.call_args.kwargs['sample_start'])

    def test_duplicate_pcm_never_gives_extra_body_votes(self):
        self.feed(10.);first=self.read(10.01)
        for _ in range(4):self.assertEqual(self.read(10.02),first)
        self.audio.body_voice.update.assert_called_once()

    def test_gap_and_stale_reset_both_vad_streams(self):
        self.feed(10.);self.read(10.01);self.feed(10.02,overflow=True)
        self.assertEqual(self.read(10.03).noise_state,'audio_gap')
        self.audio.voice.reset.assert_called_once();self.audio.body_voice.reset.assert_called_once()
        self.assertEqual(self.read(11.).noise_state,'stale_audio')
        self.assertEqual(self.audio.body_voice.reset.call_count,2)

    def test_four_channels_and_native_body_coordinate_frame_are_reported(self):
        m=self.audio.capture_metrics()
        self.assertEqual(m['active_channels'],[1,2,3,4]);self.assertEqual(m['ignored_channels'],[])
        self.assertEqual(m['channel_roles']['1'],'body_left')
        self.assertEqual(m['pairs']['body']['frame'],'body')
        self.assertFalse(m['geometry_calibrated']);self.assertFalse(m['front_back_resolved'])
        json.dumps(m,allow_nan=False)

    def test_pose_provider_failure_does_not_interrupt_audio_capture(self):
        self.audio.pose_provider=Mock(side_effect=RuntimeError('missing feedback'))
        self.feed(10.);result=self.read(10.01)
        self.assertEqual(result.sample_sequence,1)

    def test_mouth_sync_audio_does_not_phase_cancel_the_four_mics(self):
        values=np.tile(np.array([30000,2000,4000,-30000],'<i2'),(960,1))
        self.feed(10.,values)
        rate,mono=self.audio.recent_mono_audio(seconds=.02)
        self.assertEqual(rate,48000);np.testing.assert_allclose(mono,3000/32768)


class CachedPoseTests(unittest.TestCase):
    def runtime(self):
        # Exercise the actual conversion without constructing/opening hardware.
        r=DemoRuntime.__new__(DemoRuntime)
        r._motion_pose_synced=True;r.config=SimpleNamespace(yaw_id=0,pitch_id=2)
        r.axis_limits=SimpleNamespace(yaw_min=1404,yaw_center=1952,yaw_max=2500,
                                      pitch_min=1255,pitch_center=1555,pitch_max=1855)
        r.direction=SimpleNamespace(yaw_left_sign=1)
        r.pose=HeadPose(1952,1555);r.head=SimpleNamespace(is_moving=False,estimated_pose=r.pose)
        r.audio_capture=SimpleNamespace(body_yaw_units_per_degree=None)
        r.bus=SimpleNamespace(connected=True,_position_reads={
            0:dict(position=1952,ts=time.time()-.1,outcome='valid'),
            2:dict(position=1555,ts=time.time()-.1,outcome='valid')})
        return r

    def test_only_confirmed_neutral_pose_has_implicit_zero_body_offset(self):
        r=self.runtime();c=r._audio_pose_context();self.assertEqual(c['head_yaw_deg'],0)
        r.bus._position_reads[0]['position']=2100
        self.assertIsNone(r._audio_pose_context())

    def test_neutral_pose_cannot_be_reused_while_moving_or_after_feedback_loss(self):
        r=self.runtime();r.head.is_moving=True;self.assertIsNone(r._audio_pose_context())
        r.head.is_moving=False;r.bus._position_reads[0]['ts']=time.time()-5
        self.assertIsNone(r._audio_pose_context())
        r.bus.connected=False;self.assertIsNone(r._audio_pose_context())

    def test_normal_six_tick_settling_error_is_neutral_but_larger_offsets_are_not(self):
        r=self.runtime();r.bus._position_reads[0]['position']=1958
        self.assertEqual(r._audio_pose_context()['head_yaw_deg'],0.)
        r.bus._position_reads[0]['position']=1961
        self.assertIsNone(r._audio_pose_context())

    def test_operator_measured_scale_is_separate_from_steering_gain(self):
        r=self.runtime();r.audio_capture.body_yaw_units_per_degree=10.
        r.head.estimated_pose=HeadPose(2252,1555)
        r.bus._position_reads[0]['position']=2252
        self.assertAlmostEqual(r._audio_pose_context()['head_yaw_deg'],30)
        r.direction.yaw_left_sign=-1
        self.assertAlmostEqual(r._audio_pose_context()['head_yaw_deg'],-30)


if __name__=='__main__':unittest.main()
