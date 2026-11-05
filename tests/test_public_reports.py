"""Privacy and consistency checks for the public progress reports."""
import base64
import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / 'docs' / 'report'


class ReportData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.recording = False
        self.data = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script' and dict(attrs).get('id') == 'report-data':
            self.recording = True

    def handle_data(self, data):
        if self.recording:
            self.data.append(data)

    def handle_endtag(self, tag):
        if tag == 'script':
            self.recording = False


class PublicReportTests(unittest.TestCase):
    def test_reports_do_not_embed_audio_or_personal_home_paths(self):
        for name in ('index.html', 'assessment.html'):
            with self.subTest(report=name):
                text = (REPORT_DIR / name).read_text(encoding='utf-8')
                self.assertNotIn('data:audio/', text.lower())
                # Ignore image bytes when checking textual path patterns: base64
                # may coincidentally contain arbitrary short account strings.
                text = re.sub(r'data:[^\s"\']+;base64,[A-Za-z0-9+/=]+', '', text)
                self.assertNotRegex(text, r'(?i)[a-z]:[\\/]+users[\\/]+[^\\/\s"<>]+')
                self.assertNotRegex(text, r'(?i)/(?:users|home)/[^/\s"<>]+')

    def test_public_guide_uses_summary_and_omits_recordings(self):
        parser = ReportData()
        parser.feed((REPORT_DIR / 'index.html').read_text(encoding='utf-8'))
        data = json.loads(''.join(parser.data))
        self.assertEqual(data['assets']['audio'], '')
        self.assertEqual(data['waveforms']['overview'], [[], [], [], []])
        self.assertEqual(data['waveforms']['detail'], [[], [], [], []])
        public_evidence = {
            'docs/publication-validation-r51.json', 'docs/VALIDATION_R51.md',
            'docs/CURRENT_RELEASE_CN.md', 'calibration/microphone_layout.json',
            'hardware/cad/panda_source_manifest.json', 'configs/panda-assembled-r51.example.json',
        }
        self.assertEqual({item['path'] for item in data['evidence_files']}, public_evidence)
        for item in data['evidence_files']:
            text_bytes = (ROOT / item['path']).read_bytes().replace(b'\r\n', b'\n')
            self.assertEqual(hashlib.sha256(text_bytes).hexdigest(), item['sha256'])
        self.assertNotIn('latest', data)  # private runtime snapshots are not embedded
        self.assertIn('R51', data['content']['summary'])
        self.assertIn('CH1', data['content']['algorithms'][0]['steps'][1])

    def test_public_guide_images_are_cad_or_privacy_placeholder(self):
        text = (REPORT_DIR / 'index.html').read_text(encoding='utf-8')
        items = re.findall(r'data:image/(png|svg\+xml);base64,([A-Za-z0-9+/=]+)', text)
        self.assertTrue(items)
        cad_hash = hashlib.sha256((ROOT / 'docs/assets/panda-assembled-cad.png').read_bytes()).hexdigest()
        for kind, encoded in items:
            payload = base64.b64decode(encoded, validate=True)
            if kind == 'png':
                self.assertEqual(hashlib.sha256(payload).hexdigest(), cad_hash)
            else:
                self.assertIn('现场图片与录音未在公开版提供', payload.decode('utf-8'))
        self.assertNotRegex(text, r'data:image/(?:jpeg|webp|gif);base64,')

    def test_report_files_link_to_each_other(self):
        guide = (REPORT_DIR / 'index.html').read_text(encoding='utf-8')
        assessment = (REPORT_DIR / 'assessment.html').read_text(encoding='utf-8')
        self.assertIn('assessment.html', guide)
        self.assertIn('公开脱敏版', assessment)
        self.assertIn('1500 — 2000 — 2500', assessment)
        self.assertIn('1255 — 1550 — 1855', assessment)


if __name__ == '__main__':
    unittest.main()
