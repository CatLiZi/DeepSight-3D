"""Release artifacts are usable without private per-machine settings."""
import json
import hashlib
from pathlib import Path
import unittest

from locator_demo.settings import load_settings
from locator_demo.stereo_calibration import alignment_profile_path

ROOT=Path(__file__).resolve().parents[1]


class PortableReleaseTests(unittest.TestCase):
    def test_assembled_profile_keeps_the_confirmed_four_microphone_mapping(self):
        config=load_settings(ROOT/'configs/panda-assembled-r51.example.json')
        audio=config.audio_capture
        self.assertEqual((audio.ear_left_channel,audio.ear_right_channel),(2,3))
        self.assertEqual((audio.body_left_channel,audio.body_right_channel),(1,4))
        self.assertEqual(audio.localization_mode,'four_mic')
        self.assertAlmostEqual(audio.body_yaw_units_per_degree,2000/270)
        self.assertEqual(audio.body_yaw_zero_position,1952)

    def test_example_does_not_start_with_feature_switches_enabled(self):
        config=load_settings(ROOT/'configs/panda-assembled-r51.example.json')
        self.assertFalse(config.features.audio_enabled)
        self.assertFalse(config.features.visual_enabled)
        self.assertFalse(config.features.camera_enabled)

    def test_known_rig_profile_axis_limits_are_explicit(self):
        config=load_settings(ROOT/'configs/panda-assembled-r51.example.json')
        limits=config.axis_limits
        self.assertEqual((limits.yaw_min,limits.yaw_center,limits.yaw_max),(1500,2000,2500))
        self.assertEqual((limits.pitch_min,limits.pitch_center,limits.pitch_max),(1255,1550,1855))

    def test_historical_failure_fixture_is_sanitized_and_bundled(self):
        rows=json.loads((ROOT/'tests/fixtures/original-regression-26.json').read_text(encoding='utf-8'))
        self.assertEqual(len(rows),26)
        self.assertTrue(all(set(row)=={'id'} for row in rows))
        self.assertEqual(len({row['id'] for row in rows}),26)

    def test_board_pdf_is_a_real_packaged_pdf(self):
        data=(ROOT/'output/calibration/stereo-charuco-A4-20mm.pdf').read_bytes()
        self.assertTrue(data.startswith(b'%PDF'))

    def test_rig_profile_falls_back_to_packaged_alignment_preview(self):
        config=load_settings(ROOT/'configs/panda-assembled-r51.example.json')
        metric=ROOT/config.camera.stereo_calibration_path
        preview=alignment_profile_path(metric)
        self.assertTrue(preview.is_file())
        profile=json.loads(preview.read_text(encoding='utf-8'))
        self.assertFalse(profile['metric_geometry_enabled'])

    def test_generic_example_does_not_claim_mic_mapping_is_confirmed(self):
        config=load_settings(ROOT/'locator_demo_settings.example.json')
        self.assertEqual((config.audio_capture.ear_left_channel,config.audio_capture.ear_right_channel),(2,3))
        self.assertFalse(config.audio_capture.ear_mapping_confirmed)

    def test_archived_ui_backup_has_portable_restore_and_matching_files(self):
        backup=ROOT/'ui_backups/r30-before-redesign-20261027'
        manifest=json.loads((backup/'manifest.json').read_text(encoding='utf-8'))
        for name,expected in manifest['files'].items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((backup/'static'/name).read_bytes()).hexdigest(),expected)
        script=(backup/'Restore-UI.ps1').read_text(encoding='utf-8')
        self.assertNotIn('C:\\Users\\',script)
        self.assertIn('$VerifyOnly',script)


if __name__=='__main__':unittest.main()
