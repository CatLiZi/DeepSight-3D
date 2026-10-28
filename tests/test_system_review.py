"""Configuration transitions and four-channel evidence integrity; no hardware I/O."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from locator_demo.array_audio import WasapiArrayAudio
from locator_demo.audio import AudioDirection, AudioEstimate
from locator_demo.head import HeadPose
from locator_demo.settings import SavedSettings, AxisLimitsConfig, load_settings, save_settings
from locator_demo.web.app import (DemoRuntime, RuntimeConfig, create_app, AxisLimitsConfigRequest,
    DirectionConfigRequest, ControlConfigRequest, AudioCaptureConfigRequest, AudioMappingConfigRequest,
    FeatureConfigRequest, AudioProcessingConfigRequest)
import test_av_motion_coordination as coordination_fixture

FINAL = dict(yaw_min=1404, yaw_center=1952, yaw_max=2500,
             pitch_min=1255, pitch_center=1555, pitch_max=1855)


class ConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'settings.json'
        save_settings(self.path, SavedSettings(yaw_id=0, axis_limits=AxisLimitsConfig(**FINAL)))
        self.app = create_app(simulated=True, settings_path=self.path)
        self.r = self.app.state.runtime
        self.addCleanup(self.r.shutdown)
        self.client = TestClient(self.app)

    def rejected(self, url, payload):
        before = self.path.read_bytes()
        response = self.client.post(url, json=payload)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.r.bus.moves, [])

    def test_out_of_protocol_range_is_not_silently_clipped(self):
        self.rejected('/api/config/axis-limits', {**FINAL, 'yaw_max': 2652})

    def test_reversed_endpoints_are_not_silently_exchanged(self):
        self.rejected('/api/config/axis-limits', {**FINAL, 'yaw_min': 2400, 'yaw_max': 1404})

    def test_center_outside_range_is_not_silently_replaced(self):
        self.rejected('/api/config/axis-limits', {**FINAL, 'pitch_center': 1900})

    def test_zero_travel_range_is_rejected(self):
        self.rejected('/api/config/axis-limits', {**FINAL, 'yaw_min': 1952, 'yaw_max': 1952})

    def test_same_servo_cannot_be_assigned_to_both_axes(self):
        self.rejected('/api/config/servos', {'yaw_id': 2, 'pitch_id': 2, 'port': 'COM3'})

    def test_current_ranges_roundtrip_exactly(self):
        result = self.client.post('/api/config/axis-limits', json=FINAL)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['axis_limits'], FINAL)
        self.assertEqual(asdict(load_settings(self.path).axis_limits), FINAL)
        self.assertEqual(result.json()['head_presets']['center'], {'yaw': 1952, 'pitch': 1555})


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'settings.json'
        self.settings = SavedSettings(yaw_id=0, axis_limits=AxisLimitsConfig(**FINAL))
        save_settings(self.path, self.settings)

    def test_truncated_settings_do_not_silently_restore_old_center(self):
        self.path.write_text('{"axis_limits":', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'settings.json'):
            load_settings(self.path)

    def test_wrong_top_level_format_has_actionable_error(self):
        self.path.write_text('[]', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'settings.json'):
            load_settings(self.path)

    def test_invalid_saved_range_does_not_autocorrect_on_start(self):
        data = asdict(self.settings); data['axis_limits']['yaw_max'] = 2652
        self.path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(ValueError): load_settings(self.path)

    def test_duplicate_saved_servo_ids_prevent_startup(self):
        data = asdict(self.settings); data['yaw_id'] = data['pitch_id']
        self.path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(ValueError): load_settings(self.path)

    def test_interrupted_flush_keeps_previous_complete_settings(self):
        before = self.path.read_bytes()
        with patch('os.fsync', side_effect=OSError('simulated disk flush failure')):
            with self.assertRaises(OSError):
                save_settings(self.path, replace(self.settings, port='COM9'))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_failed_replace_keeps_previous_complete_settings(self):
        before = self.path.read_bytes()
        with patch('os.replace', side_effect=PermissionError('simulated locked file')):
            with self.assertRaises(OSError):
                save_settings(self.path, replace(self.settings, port='COM9'))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_first_run_without_a_file_still_uses_defaults(self):
        self.assertEqual(load_settings(self.path.parent / 'new.json'), SavedSettings())


class TransitionTests(unittest.TestCase):
    def runtime(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        r = DemoRuntime(RuntimeConfig(simulated=True, settings_path=Path(tmp.name)/'settings.json'))
        self.addCleanup(r.shutdown)
        r.running = True; r._motion_pose_synced = True
        r.head = SimpleNamespace(cancel=Mock(), stop=Mock(), estimated_pose=HeadPose(1510, 1520), is_moving=True)
        r._make_head = Mock(return_value=r.head)
        return r

    def assert_paused(self, r, old_generation):
        self.assertFalse(r.running)
        self.assertGreater(r.motion_generation(), old_generation)
        self.assertFalse(r._motion_pose_synced)
        self.assertTrue(r.head.cancel.called)
        self.assertEqual(r.bus.moves, [])

    def test_direction_change_cancels_motion_in_old_coordinates(self):
        r = self.runtime(); generation = r.motion_generation()
        r.update_direction_config(DirectionConfigRequest(yaw_left_sign=1))
        self.assert_paused(r, generation)

    def test_control_rebuild_needs_fresh_feedback_not_planned_pose(self):
        r = self.runtime(); generation = r.motion_generation()
        r.update_control_config(ControlConfigRequest(control_profile='fast'))
        self.assert_paused(r, generation)

    def test_array_replacement_cancels_before_closing_old_stream(self):
        r = self.runtime(); generation = r.motion_generation()
        def close(): self.assertFalse(r.running)
        r.audio.stop = Mock(side_effect=close)
        replacement = r._make_audio()
        r._make_audio = Mock(return_value=replacement)
        r.update_audio_capture_config(AudioCaptureConfigRequest(ear_left_channel=2, ear_right_channel=3))
        self.assert_paused(r, generation)

    def test_mapping_change_revokes_old_direction(self):
        r = self.runtime(); generation = r.motion_generation()
        r.update_audio_config(AudioMappingConfigRequest(swap_channels=True))
        self.assert_paused(r, generation)

    def test_direct_servo_config_entry_cannot_keep_old_running_state(self):
        r = self.runtime(); generation = r.motion_generation()
        r.update_servo_config(yaw_id=0, pitch_id=2)
        self.assert_paused(r, generation)

    def test_disabling_all_features_cancels_existing_visual_trajectory_immediately(self):
        r = self.runtime()
        r.update_feature_config(FeatureConfigRequest(audio_enabled=False, visual_enabled=False, camera_enabled=False))
        self.assertTrue(r.head.cancel.called)
        self.assertFalse(r.running)

    def test_switching_modes_cancels_old_goal_without_disabling_new_mode(self):
        r = self.runtime()
        r.update_feature_config(FeatureConfigRequest(audio_enabled=True, visual_enabled=False, camera_enabled=True))
        self.assertTrue(r.head.cancel.called)
        self.assertTrue(r.running)
        self.assertEqual(r.pose, r.head.estimated_pose)

    def test_unchanged_feature_save_does_not_interrupt_smooth_motion(self):
        r = self.runtime()
        r.update_feature_config(FeatureConfigRequest(**asdict(r.features)))
        r.head.cancel.assert_not_called()
        self.assertTrue(r.running)

    def test_save_failure_pauses_runtime_and_does_not_claim_success(self):
        r = self.runtime()
        with patch('locator_demo.web.app.save_settings', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(RuntimeError, '保存失败'):
                r._persist_settings()
        self.assertFalse(r.running)
        self.assertTrue(r.head.cancel.called)

    def test_recording_toggle_does_not_reset_continuous_vad_evidence(self):
        r = self.runtime()
        adapter = WasapiArrayAudio.__new__(WasapiArrayAudio)
        adapter.status = r.audio.status; adapter.stop = lambda: None
        adapter.set_recording_enabled = Mock(); adapter.set_vad_enabled = Mock()
        r.audio = adapter
        r.update_audio_processing_config(AudioProcessingConfigRequest(
            **{**asdict(r.audio_processing), 'recording_enabled': not r.audio_processing.recording_enabled}))
        adapter.set_recording_enabled.assert_called_once()
        adapter.set_vad_enabled.assert_not_called()
        self.assertTrue(r.running)

    def test_real_vad_change_cancels_old_goal_and_reconfigures_once(self):
        r = self.runtime()
        adapter = WasapiArrayAudio.__new__(WasapiArrayAudio)
        adapter.status = r.audio.status; adapter.stop = lambda: None
        adapter.set_recording_enabled = Mock(); adapter.set_vad_enabled = Mock()
        r.audio = adapter
        r.update_audio_processing_config(AudioProcessingConfigRequest(vad_enabled=False))
        self.assertFalse(r.running)
        adapter.set_vad_enabled.assert_called_once_with(False)


class FourChannelEvidenceTests(unittest.TestCase):
    setUp = coordination_fixture.CoordinationRuntimeTests.setUp
    tearDown = coordination_fixture.CoordinationRuntimeTests.tearDown

    def test_four_channel_vad_toggle_preserves_original_evidence_metadata(self):
        r = self.runtime
        r.features = replace(r.features, visual_enabled=False)
        r.audio_processing = replace(r.audio_processing, vad_enabled=False)
        r.audio_capture = replace(r.audio_capture, localization_mode='ear_pair', ear_mapping_confirmed=True)
        original = replace(self.voice, temporal_stability=.9, azimuth_std_deg=4., sample_sequence=23)
        adapter = WasapiArrayAudio.__new__(WasapiArrayAudio)
        adapter.read_estimate = lambda: original
        adapter.status = r.audio.status; adapter.stop = lambda: None
        adapter.capture_metrics = lambda: {}
        adapter.ear_config = SimpleNamespace(ready=True)
        adapter.localizer = SimpleNamespace(diagnostics={'signal_valid': True})
        r.audio = adapter
        r.tick(include_frame=False)
        self.assertEqual(r.last_audio, original)

    def test_four_channel_unknown_does_not_get_invented_confidence_when_vad_off(self):
        r = self.runtime
        r.features = replace(r.features, visual_enabled=False)
        r.audio_processing = replace(r.audio_processing, vad_enabled=False)
        original = replace(self.voice, direction=AudioDirection.UNKNOWN, confidence=0.,
                           doa_confidence=0., speech_confidence=0., noise_state='ambiguous_delay')
        adapter = WasapiArrayAudio.__new__(WasapiArrayAudio)
        adapter.read_estimate = lambda: original
        adapter.status = r.audio.status; adapter.stop = lambda: None
        adapter.capture_metrics = lambda: {}
        adapter.ear_config = SimpleNamespace(ready=True)
        adapter.localizer = SimpleNamespace(diagnostics={'signal_valid': True})
        r.audio = adapter
        r.tick(include_frame=False)
        self.assertEqual(r.last_audio.direction, AudioDirection.UNKNOWN)
        self.assertEqual(r.last_audio.noise_state, 'ambiguous_delay')
        self.assertEqual(r.last_audio.confidence, 0.)


if __name__ == '__main__': unittest.main()
