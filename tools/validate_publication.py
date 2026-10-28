"""Offline publication checks: frontend contracts, assets and saved rig settings."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'src/locator_demo/web/static'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    node = shutil.which('node')
    if node is None:
        raise RuntimeError('Node.js is required for the frontend checks')
    groups = []
    for script, target in [
        ('test_audio_view.js', 'audio_view.js'),
        ('test_dominant_sound_view.js', 'audio_view.js'),
        ('test_demo_ownership_view.js', 'ui_shell.js'),
        ('test_device_status_view.js', 'ui_shell.js'),
        ('test_settings_feedback.js', 'app.js'),
        ('test_fixation_view.js', 'fixation_view.js'),
    ]:
        result = subprocess.run([node, str(ROOT/'tools'/script), str(STATIC/target)],
                                cwd=ROOT, capture_output=True, text=True,
                                encoding='utf-8', timeout=30, check=True)
        data = json.loads(result.stdout)
        assert data['successful'], script
        groups.append({'script':script, **data})
    for name in ['app.js','ui_shell.js','audio_view.js','fixation_view.js']:
        subprocess.run([node,'--check',str(STATIC/name)],check=True,
                       capture_output=True,text=True,encoding='utf-8',timeout=20)
    groups.append({'successful':True,'tests':4,'cases':['JavaScript syntax checks']})

    sys.path.insert(0,str(ROOT/'src'))
    from locator_demo.settings import load_settings
    rig = load_settings(ROOT/'configs/panda-final-r43.json')
    assert (rig.axis_limits.yaw_min,rig.axis_limits.yaw_center,rig.axis_limits.yaw_max)==(1404,1952,2500)
    assert (rig.axis_limits.pitch_min,rig.axis_limits.pitch_center,rig.axis_limits.pitch_max)==(1255,1555,1855)
    assert (rig.audio_capture.ear_left_channel,rig.audio_capture.ear_right_channel)==(2,3)
    assert rig.audio_capture.channels==4 and rig.audio_capture.wasapi_raw
    profile=json.loads((ROOT/'calibration/stereo_camera-alignment.json').read_text(encoding='utf-8'))
    from locator_demo.stereo_calibration import StereoRectifier
    assert profile['metric_geometry_enabled'] is False
    assert StereoRectifier(profile, expected_size=(1920,1080), rotations=(180,180),
                           allow_alignment_only=True).metric_ready is False
    assert (ROOT/'output/calibration/stereo-charuco-A4-20mm.pdf').read_bytes().startswith(b'%PDF')
    model=json.loads((ROOT/'models/silero_vad.source.json').read_text(encoding='utf-8'))
    assert hashlib.sha256((ROOT/'models/silero_vad.onnx').read_bytes()).hexdigest()==model['sha256']
    result={'successful':True,'frontend_tests':sum(g['tests'] for g in groups),
            'groups':groups,'saved_rig_config_valid':True,'model_digest_valid':True,
            'alignment_metric_output_enabled':False,'print_asset_present':True,
            'hardware_io':False}
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'frontend-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='groups'}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
