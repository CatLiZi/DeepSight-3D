"""Visible association scores and diagnostic-only raw audio; no live hardware."""
from dataclasses import replace
from pathlib import Path
import json,math,shutil,subprocess,tempfile,time,unittest
from unittest.mock import Mock

from locator_demo.audio import AudioDirection
from locator_demo.array_audio import WasapiArrayAudio
from locator_demo.visual_priority import VisualPrioritySelector
from locator_demo.web.app import DemoRuntime,RuntimeConfig,AudioProcessingConfigRequest
from test_av_guidance import voice
from test_spectral_delay import CONFIG
from test_visual_priority import face


class AudioVisualScoreTests(unittest.TestCase):
    def run_voice(self,people,angle,count=15):
        s=VisualPrioritySelector()
        for n in range(count):
            now=10.+n*.1;s.update(people,now=now,size=(640,360),audio=voice(angle,now,n+1))
        return s

    def test_closer_direction_can_outweigh_a_moderately_larger_face(self):
        people={1:[face(1,-10,width=110)],2:[face(2,-35,width=90)]}
        s=self.run_voice(people,-35)
        self.assertEqual(s.person,2)
        self.assertEqual(s.metrics['candidates'][0]['person_id'],2)

    def test_same_direction_uses_relative_face_size(self):
        s=self.run_voice({1:[face(1,-30,width=130)],2:[face(2,-30,width=50)]},-30)
        self.assertEqual(s.person,1)
        self.assertGreater(s.metrics['candidates'][0]['relative_size_score'],s.metrics['candidates'][1]['relative_size_score'])

    def test_every_visible_face_has_a_bounded_rank_and_components(self):
        s=self.run_voice({1:[face(1,-30,width=100)],2:[face(2,-40,width=80)],3:[face(3,40,width=140)]},-35)
        rows=s.metrics['candidates'];self.assertEqual(len(rows),3)
        self.assertEqual([r['rank'] for r in rows],[1,2,3])
        self.assertEqual(rows[-1]['person_id'],3);self.assertFalse(rows[-1]['in_sector']);self.assertEqual(rows[-1]['match_score'],0.)
        for row in rows:
            for key in ('match_score','angular_score','relative_size_score','visual_quality_score','continuity_score'):
                self.assertTrue(math.isfinite(row[key]));self.assertGreaterEqual(row[key],0);self.assertLessEqual(row[key],1)

    def test_silence_has_visual_scores_and_no_acoustic_range(self):
        s=VisualPrioritySelector();winner=s.update({1:[face(1,20,width=120)],2:[face(2,-20,width=60)]},now=10.,size=(640,360))
        self.assertEqual(winner,1);self.assertEqual(s.metrics['candidates'][0]['person_id'],1)
        self.assertTrue(all(r['score_mode']=='visual_only' for r in s.metrics['candidates']))
        self.assertFalse(s.metrics['acoustic_distance_available']);self.assertFalse(s.metrics['metric_distance_enabled'])
        self.assertEqual(s.metrics['range_basis'],'relative_face_size_only_no_acoustic_range')

    def test_visual_conflict_returns_visual_scores_without_forced_match(self):
        s=self.run_voice({1:[face(1,30,width=120)],2:[face(2,15,width=60)]},-60)
        self.assertEqual(s.person,1);self.assertEqual(s.metrics['audio_reason'],'visual_audio_conflict')
        self.assertTrue(all(r['score_mode']=='visual_only' for r in s.metrics['candidates']))

    def test_invalid_bearing_cannot_become_an_acoustic_candidate(self):
        target=face(1,-30,width=100);target=replace(target,bearing={**target.bearing,'azimuth_left_deg':float('nan')})
        s=self.run_voice({1:[target],2:[face(2,30,width=60)]},-30)
        self.assertIsNone(s.metrics['candidate_person_id']);json.dumps(s.metrics,allow_nan=False)

    def test_duplicate_eye_views_do_not_double_the_size_score(self):
        a=face(1,-30,width=80);b=face(2,-30,width=100)
        s=self.run_voice({1:[a,replace(a,source_eye='right')],2:[b]},-30)
        self.assertEqual(s.person,2)
        smaller=next(r for r in s.metrics['candidates'] if r['person_id']==1)
        self.assertAlmostEqual(smaller['relative_size_score'],math.sqrt(.8))

    def test_first_audio_observation_does_not_jump_to_ranked_challenger(self):
        people={1:[face(1,20,width=120)],2:[face(2,-35,width=80)]};s=VisualPrioritySelector()
        self.assertEqual(s.update(people,now=9.,size=(640,360)),1)
        self.assertEqual(s.update(people,now=10.,size=(640,360),audio=voice(-35,10.,1)),1)
        self.assertEqual(s.metrics['candidates'][0]['person_id'],2)
        self.assertEqual(s.audio_hits,1)


class AudioDiagnosticContractTests(unittest.TestCase):
    def runtime(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        r=DemoRuntime(RuntimeConfig(simulated=True,settings_path=Path(temp.name)/'settings.json'))
        self.addCleanup(r.shutdown);return r

    def test_motor_context_preserves_reliable_direction_and_raw_observation(self):
        r=self.runtime();r.features=replace(r.features,camera_enabled=False)
        r.motor_guard_until_s=time.time()+60
        r.audio.read_estimate=Mock(return_value=voice(25,time.perf_counter(),12))
        result=r.tick(include_frame=False)
        self.assertFalse(result['audio']['motor_suppressed']);self.assertEqual(result['audio']['direction'],'left')
        self.assertTrue(result['audio']['motion_context']['active']);self.assertTrue(result['audio']['motion_context']['admitted'])
        self.assertEqual(result['audio']['raw_observation']['direction'],'left')
        self.assertEqual(result['audio']['raw_observation']['sample_sequence'],12)
        self.assertFalse(result['running'])

    def test_unreliable_delay_in_motion_keeps_raw_diagnostics_but_cannot_steer(self):
        r=self.runtime();r.features=replace(r.features,camera_enabled=False)
        r.motor_guard_until_s=time.time()+60
        r.audio.read_estimate=Mock(return_value=replace(voice(25,time.perf_counter(),12),doa_confidence=.05))
        result=r.tick(include_frame=False)
        self.assertTrue(result['audio']['motor_suppressed']);self.assertEqual(result['audio']['direction'],'unknown')
        self.assertEqual(result['audio']['raw_observation']['direction'],'left')
        self.assertEqual(result['audio']['motion_context']['reason'],'motion_acoustic_uncertain')

    def test_recording_false_is_honored_without_enabling_denoising(self):
        r=self.runtime();r.audio=WasapiArrayAudio(ear_config=CONFIG)
        r.audio._recording_enabled=True;r.audio.capture.stop_recording=Mock()
        r.update_audio_processing_config(AudioProcessingConfigRequest(recording_enabled=False,denoise_enabled=False,vad_enabled=False))
        self.assertFalse(r.audio_processing.recording_enabled);self.assertFalse(r.audio._recording_enabled)
        self.assertFalse(r.audio_processing.denoise_enabled);r.audio.capture.stop_recording.assert_called_once()

    def test_static_projection_and_display_memory_contract(self):
        node=shutil.which('node');self.assertIsNotNone(node)
        project=Path(__file__).resolve().parents[1]
        result=subprocess.run([node,str(project/'tools/test_audio_view.js'),str(project/'src/locator_demo/web/static/audio_view.js')],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertTrue(json.loads(result.stdout)['successful'])


if __name__=='__main__':unittest.main()
