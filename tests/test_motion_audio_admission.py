"""Reliable speech survives movement; only actuator pacing waits. No hardware."""
from dataclasses import replace
import math
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch

from locator_demo.audio import AudioDirection,AudioEstimate
from locator_demo.audio_motion import qualify_audio_during_motion
from locator_demo.camera import VisionFrameResult,VisionStatus
from locator_demo.head import HeadPose
from locator_demo.visual_priority import VisualPrioritySelector
import test_av_motion_coordination as coordination_fixture
from test_visual_priority import face


def speech(**changes):
    result=AudioEstimate(AudioDirection.LEFT,.8,.0002,.4,azimuth_deg=30.,
        speech_confidence=.9,doa_confidence=.8,noise_state='speech_like',
        temporal_stability=.9,azimuth_std_deg=12.,observed_monotonic=10.,sample_sequence=1)
    return replace(result,**changes)


class MotionQualityTests(unittest.TestCase):
    def test_movement_alone_does_not_change_reliable_direction_or_metadata(self):
        raw=speech();estimate,context=qualify_audio_during_motion(raw,active=True)
        self.assertIs(estimate,raw)
        self.assertTrue(context['admitted'])
        self.assertFalse(estimate.motor_suppressed)
        self.assertFalse(context['motion_alone_disables_audio'])

    def test_stationary_input_keeps_existing_localizer_contract(self):
        raw=speech(speech_confidence=.4)
        self.assertIs(qualify_audio_during_motion(raw,active=False)[0],raw)

    def test_unknown_noise_and_ambiguous_input_keep_their_actual_explanation(self):
        for reason in ('no_voice_activity','ambiguous_delay','low_coherence','quiet','channels_nearly_identical'):
            raw=speech(direction=AudioDirection.UNKNOWN,noise_state=reason)
            estimate,context=qualify_audio_during_motion(raw,active=True)
            self.assertIs(estimate,raw);self.assertEqual(estimate.noise_state,reason)
            self.assertFalse(context['admitted'])

    def test_speech_without_reliable_delay_is_not_a_direction(self):
        for changes in ({'doa_confidence':.05},{'confidence':.05},{'temporal_stability':.1},
                        {'azimuth_std_deg':40.},{'speech_confidence':.05}):
            estimate,context=qualify_audio_during_motion(speech(**changes),active=True)
            self.assertEqual(estimate.direction,AudioDirection.UNKNOWN)
            self.assertFalse(context['admitted'])

    def test_lower_volume_but_qualified_speech_is_not_rejected_due_to_motion(self):
        estimate,context=qualify_audio_during_motion(speech(speech_confidence=.55,doa_confidence=.4,confidence=.4,temporal_stability=.6),active=True)
        self.assertEqual(estimate.direction,AudioDirection.LEFT);self.assertTrue(context['admitted'])

    def test_exact_existing_quality_boundaries_are_accepted(self):
        estimate,context=qualify_audio_during_motion(speech(speech_confidence=.5,doa_confidence=.2,confidence=.2,
            temporal_stability=.5,azimuth_std_deg=25.),active=True)
        self.assertFalse(estimate.motor_suppressed);self.assertTrue(context['admitted'])

    def test_flagged_motor_or_tone_noise_cannot_override_acoustic_rejection(self):
        for reason in ('fan_or_motor','noise','low_band_noise','denoise_guard'):
            estimate,_=qualify_audio_during_motion(speech(noise_state=reason),active=True)
            self.assertEqual(estimate.direction,AudioDirection.UNKNOWN)

    def test_nonfinite_and_out_of_range_probabilities_cannot_pass(self):
        for key in ('speech_confidence','confidence','doa_confidence','temporal_stability','azimuth_deg','azimuth_std_deg'):
            for value in (math.nan,math.inf,-math.inf):
                estimate,context=qualify_audio_during_motion(speech(**{key:value}),active=True)
                self.assertFalse(context['admitted']);self.assertEqual(estimate.direction,AudioDirection.UNKNOWN)
        for key in ('speech_confidence','confidence','doa_confidence','temporal_stability'):
            self.assertFalse(qualify_audio_during_motion(speech(**{key:1.1}),active=True)[1]['admitted'])

    def test_motion_policy_does_not_remove_freshness_protection(self):
        selector=VisualPrioritySelector()
        estimate,_=qualify_audio_during_motion(speech(observed_monotonic=8.),active=True)
        self.assertEqual(selector._audio_validity(estimate,10.,0.)[:2],(False,'stale_audio'))


class MovingRuntimeTests(unittest.TestCase):
    # Reuse setup without re-running the six original tests through inheritance.
    setUp=coordination_fixture.CoordinationRuntimeTests.setUp
    tearDown=coordination_fixture.CoordinationRuntimeTests.tearDown

    def test_continuous_movement_and_small_retargets_do_not_disable_strong_voice(self):
        self.head.is_moving=True
        for n in range(80):
            self.voice=replace(speech(observed_monotonic=time.perf_counter()),sample_sequence=n+1)
            self.frame=replace(self.frame,detection_sequence=n+1)
            with patch('locator_demo.web.app.time.time',return_value=100.+n*.1):
                state=self.runtime.tick(include_frame=False)
            self.assertEqual(state['audio']['direction'],'left')
            self.assertFalse(state['audio']['motor_suppressed'])
            self.assertTrue(state['audio']['motion_context']['active'])

    def test_audio_only_keeps_measurement_but_waits_for_actuator(self):
        self.runtime.features=replace(self.runtime.features,visual_enabled=False)
        self.head.is_moving=True;self.voice=speech(observed_monotonic=time.perf_counter())
        self.runtime.tick(include_frame=False)
        self.assertEqual(self.runtime.last_audio.direction,AudioDirection.LEFT)
        self.assertTrue(self.runtime.last_state.audio_ready)
        self.runtime.move.assert_not_called()

    def _audio_seek_sequence(self,*,fresh=True,spacing=.3):
        self.runtime.features=replace(self.runtime.features,visual_enabled=False)
        self.runtime.pose=HeadPose(1500,1500);self.head.estimated_pose=self.runtime.pose
        commands=[]
        def move(pose,**_kwargs):
            commands.append(pose);self.runtime.pose=pose;self.head.estimated_pose=pose
        self.runtime.move=Mock(side_effect=move)
        base=time.perf_counter()
        for n in range(12):
            self.voice=speech(azimuth_deg=20.,observed_monotonic=base+n*.02 if fresh else base,
                sample_sequence=n+1 if fresh else 1)
            self.runtime.motor_guard_until_s=0
            with patch('locator_demo.web.app.time.time',return_value=100.+n*spacing):
                self.runtime.tick(include_frame=False)
        return commands

    def test_new_pcm_allows_bounded_steps_without_a_six_degree_jump(self):
        commands=self._audio_seek_sequence()
        self.assertEqual(len(commands),self.runtime._profile_params()['audio_seek_max_steps'])
        self.assertGreater(len(commands),1)
        self.assertTrue(all(abs(p.yaw-1500)<=self.runtime._profile_params()['audio_seek_window'] for p in commands))
        self.assertEqual(self.runtime._audio_seek_stop_reason,'step_budget_reached')

    def test_cached_direction_does_not_repeat_audio_seek(self):
        self.assertEqual(len(self._audio_seek_sequence(fresh=False)),1)
        self.assertEqual(self.runtime._audio_seek_stop_reason,'awaiting_new_acoustic_observation')

    def test_elapsed_time_cannot_renew_an_exhausted_search_budget(self):
        self.assertEqual(len(self._audio_seek_sequence(spacing=3.)),self.runtime._profile_params()['audio_seek_max_steps'])

    def test_reliable_voice_selects_matching_face_while_head_keeps_moving(self):
        selector=VisualPrioritySelector();base=time.perf_counter();tick=[0];guidance=[None]
        people={1:[face(1,0,width=130)],2:[face(2,35,width=100)]}
        def read():
            now=base+tick[0]*.1+.02
            chosen=selector.update(people,now=now,size=(640,360),audio=guidance[0],frame_time=now)
            target=people[chosen][0] if chosen is not None else None
            return VisionFrameResult(target,640,360,'',VisionStatus(True,'fixture','ok'),
                targets=[x[0] for x in people.values()],detection_sequence=tick[0]+1,control_eye='left',
                binocular={'people':[{'person_id':x} for x in people], 'target_selection':dict(selector.metrics)})
        self.runtime.vision=SimpleNamespace(read=read,stop=lambda:None,capture_metrics=lambda:{},
            set_audio_guidance=lambda audio:guidance.__setitem__(0,audio))
        self.head.is_moving=True
        for n in range(18):
            tick[0]=n;self.voice=speech(azimuth_deg=35.,sample_sequence=n+1,observed_monotonic=base+n*.1)
            with patch('locator_demo.web.app.time.time',return_value=100.+n*.1):
                state=self.runtime.tick(include_frame=False)
        self.assertEqual(selector.person,2)
        self.assertEqual(state['visual']['binocular']['target_selection']['mode'],'audio_sector_visual')
        self.assertEqual(self.runtime.last_target.face_id,2)
        self.assertFalse(state['audio']['motor_suppressed'])
        self.assertTrue(state['audio']['motion_context']['active'])

    def test_numeric_journal_explains_motion_and_face_association(self):
        self.head.is_moving=True;self.voice=speech(observed_monotonic=time.perf_counter())
        self.runtime.tick(include_frame=False)
        event=next(r for r in self.runtime.events if r['event']=='voice_direction_diagnostic')
        detail=event['detail']
        self.assertTrue(detail['features']['visual_enabled'])
        self.assertTrue(detail['motion_context']['admitted'])
        self.assertTrue(detail['angles_are_estimates_not_calibration_truth'])
        self.assertNotIn('pcm',detail);self.assertNotIn('image',detail)


if __name__=='__main__':unittest.main()
