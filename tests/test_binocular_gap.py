"""Fresh confirmation after a visual interruption; no hardware is opened."""
import unittest

from locator_demo.binocular import BinocularFusion
from test_av_guidance import voice
from test_binocular import fixture
from test_visual_priority import face


class BinocularGapTests(unittest.TestCase):
    policies = ({}, {'guided': True}, {'guided': True, 'visual_priority': True})

    def warm(self, *, options=None, geometry=None, single=False):
        images, health, left, right = fixture()
        fusion = BinocularFusion(**(options or {}))
        targets = {'left': [left], 'right': [] if single else [right]}
        for now in (1., 1.1, 1.2, 1.3):
            result = fusion.update(images, targets, health, now=now, geometric_matches=geometry)
        self.assertTrue(result['control_ready'])
        return fusion, images, health, targets, result

    def test_long_gap_needs_three_fresh_observations_before_control_recovers(self):
        for options in self.policies:
            with self.subTest(options=options):
                fusion, images, health, targets, _ = self.warm(options=options)
                for i, now in enumerate((2.15, 2.25, 2.35)):
                    result = fusion.update(images, targets, health, now=now)
                    self.assertEqual(result['control_ready'], i == 2)
                    self.assertIsNotNone(result['target'])  # New coordinates remain inspectable.

    def test_old_pair_confirmation_does_not_merge_the_first_recovered_pair(self):
        fusion, images, health, targets, _ = self.warm()
        result = fusion.update(images, targets, health, now=2.15)
        self.assertEqual(result['summary']['associations'], [])
        self.assertEqual(len(result['summary']['people']), 2)

    def test_geometric_pairs_also_require_new_confirmation_after_a_gap(self):
        geometry = [{'left_track': 1, 'right_track': 1,
                     'kind': 'aligned_feature_correspondence', 'confidence': .95}]
        fusion, images, health, targets, _ = self.warm(geometry=geometry)
        result = fusion.update(images, targets, health, now=2.15, geometric_matches=geometry)
        self.assertEqual(result['summary']['associations'], [])
        self.assertFalse(result['control_ready'])
        for now in (2.25, 2.35):
            result = fusion.update(images, targets, health, now=now, geometric_matches=geometry)
        self.assertEqual(len(result['summary']['associations']), 1)
        self.assertTrue(result['control_ready'])

    def test_single_eye_control_also_reconfirms_after_a_gap(self):
        fusion, images, health, targets, _ = self.warm(single=True)
        result = fusion.update(images, targets, health, now=2.15)
        self.assertIsNotNone(result['target'])
        self.assertFalse(result['control_ready'])
        self.assertEqual(result['target'].source_eye, 'left')

    def test_partial_pair_evidence_before_a_gap_cannot_complete_afterwards(self):
        images, health, left, right = fixture()
        fusion = BinocularFusion()
        targets = {'left': [left], 'right': [right]}
        for now in (1., 1.1):
            fusion.update(images, targets, health, now=now)
        result = fusion.update(images, targets, health, now=1.95)
        self.assertEqual(result['summary']['associations'], [])
        self.assertFalse(result['control_ready'])

    def test_normal_skipped_frames_do_not_destroy_a_confirmed_pair(self):
        for gap in (.2, .4, .7):
            with self.subTest(gap=gap):
                fusion, images, health, targets, prior = self.warm()
                result = fusion.update(images, targets, health, now=1.3 + gap)
                self.assertTrue(result['control_ready'])
                self.assertEqual(len(result['summary']['associations']), 1)
                self.assertEqual(result['target'].face_id, prior['target'].face_id)

    def test_brief_single_eye_occlusion_still_uses_the_other_fresh_eye(self):
        fusion, images, health, targets, prior = self.warm(options={'guided': True, 'visual_priority': True})
        selected = prior['eye']
        health[selected]['valid'] = False
        result = fusion.update(images, targets, health, now=1.4)
        self.assertNotEqual(result['eye'], selected)
        self.assertEqual(result['target'].face_id, prior['target'].face_id)
        self.assertFalse(result['control_ready'])

    def test_audio_confirmation_cannot_bridge_a_long_visual_gap(self):
        for options in ({'guided': True}, {'guided': True, 'visual_priority': True}):
            with self.subTest(options=options):
                images, health, _, _ = fixture()
                fusion = BinocularFusion(**options)
                targets = {'left': [face(1, 25, width=140), face(2, -40, width=70)], 'right': []}
                for n, now in enumerate((1., 1.1)):
                    fusion.update(images, targets, health, now=now, audio=voice(-40, now, n))
                result = fusion.update(images, targets, health, now=1.95, audio=voice(-40, 1.95, 2))
                selection = result['summary']['target_selection']
                self.assertEqual(selection['audio_confirmation_hits'], 1)
                self.assertEqual(result['target'].center, targets['left'][0].center)
                self.assertFalse(result['control_ready'])

    def test_larger_face_challenge_needs_sustained_new_observations_after_a_gap(self):
        images, health, _, _ = fixture()
        fusion = BinocularFusion(guided=True, visual_priority=True)
        small = face(1, 25, width=60)
        big = face(2, -40, width=140)
        first = fusion.update(images, {'left': [small], 'right': []}, health, now=1.)
        target_id = first['target'].face_id
        targets = {'left': [small, big], 'right': []}
        fusion.update(images, targets, health, now=1.1)
        result = fusion.update(images, targets, health, now=1.95)
        self.assertEqual(result['target'].face_id, target_id)
        for now in (2.05, 2.15, 2.35):
            result = fusion.update(images, targets, health, now=now)
        self.assertEqual(result['target'].center, big.center)

    def test_no_faces_after_a_gap_never_reuses_an_old_coordinate(self):
        for options in self.policies:
            with self.subTest(options=options):
                fusion, images, health, _, _ = self.warm(options=options)
                result = fusion.update(images, {'left': [], 'right': []}, health, now=2.15)
                self.assertIsNone(result['target'])
                self.assertFalse(result['control_ready'])
                self.assertEqual(result['summary']['people'], [])


if __name__ == '__main__':
    unittest.main()
