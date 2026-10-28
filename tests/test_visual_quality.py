"""Native detail, adaptive search and capability limits, without hardware I/O."""
import base64
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from locator_demo.camera import FfmpegCameraSnapshot
from locator_demo.multiscale_faces import AdaptiveFaceSearch, merge_faces
from locator_demo.settings import CameraConfig, SavedSettings, load_settings, save_settings
from locator_demo.vision import FaceTarget
from locator_demo.visual_quality import CameraCapabilityProbe, FocusAssist, focus_sample


class VisualQualityTests(unittest.TestCase):
    def test_defocus_reduces_score_for_same_textured_target(self):
        rows, cols = np.indices((360, 640))
        gray = (((rows // 12 + cols // 12) % 2) * 180 + 40).astype('uint8')
        sharp = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        blurred = cv2.GaussianBlur(sharp, (17, 17), 3.5)
        _, a = focus_sample(sharp)
        _, b = focus_sample(blurred)
        self.assertGreater(a['score'], b['score'] * 3)
        self.assertEqual(a['roi'], b['roi'])
        self.assertEqual(a['reason'], 'relative_measurement')

    def test_focus_roi_is_clamped_and_preserves_native_pixels(self):
        image = np.random.default_rng(5).integers(0, 255, (1080, 1920, 3), dtype=np.uint8)
        roi, metrics = focus_sample(image, (1920, 1080))
        self.assertEqual(metrics['roi'], [1280, 720, 640, 360])
        np.testing.assert_array_equal(roi, image[720:, 1280:])
        self.assertTrue(metrics['native_pixels'])

    def test_blank_scene_is_not_labelled_sharp_or_autofocused(self):
        image = np.full((100, 200, 3), 120, np.uint8)
        _, metrics = focus_sample(image)
        self.assertEqual(metrics['reason'], 'low_texture')
        self.assertEqual(metrics['score'], 0)

    def test_focus_assist_rate_limit_and_roi_scene_reset(self):
        rng = np.random.default_rng(8)
        image = rng.integers(50, 200, (540, 960, 3), dtype=np.uint8)
        assist = FocusAssist()
        assist.update({'left': image}, now=0)
        first = assist.previews['left']
        assist.update({'left': np.zeros_like(image)}, now=.1)
        self.assertEqual(assist.previews['left'], first)
        assist.update({'left': np.zeros_like(image)}, now=1)
        self.assertEqual(assist.metrics['left']['peak_same_roi'], 0)
        decoded = cv2.imdecode(np.frombuffer(base64.b64decode(first), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(decoded.shape, (360, 640, 3))

    def test_camera_settings_round_trip_search_and_preview_quality(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'settings.json'
            camera = CameraConfig(face_detector_backend='scrfd', multi_scale_search=True,
                                  focus_assist=False, preview_jpeg_quality=97)
            save_settings(path, SavedSettings(camera=camera))
            self.assertEqual(load_settings(path).camera, camera)

    def test_native_detector_does_not_encode_or_decode_a_preview(self):
        camera = FfmpegCameraSnapshot()
        image = np.zeros((1080, 1920, 3), np.uint8)
        with patch.object(camera, '_mediapipe_body_target', return_value=None), \
             patch.object(camera, '_mediapipe_targets', return_value=[]) as detect, \
             patch('cv2.imencode', side_effect=AssertionError('Preview encoding in detector')), \
             patch('cv2.imdecode', side_effect=AssertionError('Preview decoding in detector')):
            self.assertEqual(camera._detect_image_targets(image), [])
            self.assertIs(detect.call_args.args[0], image)

    def test_unknown_capability_is_not_reported_as_supported(self):
        probe = CameraCapabilityProbe()
        self.assertEqual(probe.snapshot()['status'], 'not_checked')
        self.assertNotIn('optical_autofocus_available', probe.snapshot())


class AdaptiveSearchTests(unittest.TestCase):
    def test_local_detection_maps_coordinates_and_keypoint_target(self):
        image = np.zeros((1080, 1920, 3), np.uint8)
        search = AdaptiveFaceSearch()
        found = FaceTarget(20, 30, 100, 140, score=.9, tracking_x=51, tracking_y=67, tracking_source='scrfd_kps')
        for i in range(2):
            search.search(image, [], lambda _: [], now=i)
        result = search.search(image, [], lambda _: [found], now=2)
        x, y, _, _ = search.last_region['xywh']
        self.assertEqual(result[0].x1, found.x1+x)
        self.assertEqual(result[0].tracking_x, found.tracking_x+x)
        self.assertEqual(result[0].tracking_y, found.tracking_y+y)
        self.assertTrue(result[0].tracking_source.endswith('_native_roi'))

    def test_dedup_retains_one_face_and_separate_faces(self):
        face = FaceTarget(100, 100, 200, 240, score=.9)
        duplicate = replace(face, x1=105, score=.8)
        other = replace(face, x1=400, x2=500, score=.75)
        self.assertEqual(merge_faces([duplicate, other, face]), [face, other])

    def test_search_budget_is_bounded_and_does_not_invent_cached_faces(self):
        image = np.zeros((540, 960, 3), np.uint8)
        search, calls = AdaptiveFaceSearch(), []
        callback = lambda crop: calls.append(crop.shape) or []
        search.search(image, [], callback, now=0)
        for i in range(1, 15):
            self.assertEqual(search.search(image, [], callback, now=i*.01), [])
        self.assertEqual(len(calls), 1)
        search.search(image, [], callback, now=.2)
        self.assertEqual(len(calls), 2)

    def test_six_tiles_cover_full_view_without_changing_the_input(self):
        image = np.random.default_rng(2).integers(0, 255, (540, 960, 3), dtype=np.uint8)
        original = image.copy()
        search, covered = AdaptiveFaceSearch(), np.zeros(image.shape[:2], bool)
        for i in range(6):
            search.search(image, [], lambda _: [], now=i)
            x,y,w,h = search.last_region['xywh']
            covered[y:y+h, x:x+w] = True
        self.assertTrue(covered.all())
        np.testing.assert_array_equal(image, original)


if __name__ == '__main__':
    unittest.main()
