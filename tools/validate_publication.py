"""Validate the current release artifacts without accessing physical hardware."""
from __future__ import annotations
import argparse,ast,hashlib,json,subprocess,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


def require(condition,message):
    if not condition:
        raise ValueError(message)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()

    frontend_run=subprocess.run([sys.executable,str(ROOT/'tools/validate_frontend.py')],
                                cwd=ROOT,capture_output=True,text=True,encoding='utf-8',check=True)
    frontend=json.loads(frontend_run.stdout)
    require(frontend.get('successful') is True,'Frontend checks failed')

    from locator_demo.settings import load_settings
    from locator_demo.stereo_calibration import StereoRectifier,alignment_profile_path
    rig=load_settings(ROOT/'configs/panda-assembled-r51.example.json')
    limits=rig.axis_limits
    require((limits.yaw_min,limits.yaw_center,limits.yaw_max)==(1404,1952,2500),'Unexpected yaw limits')
    require((limits.pitch_min,limits.pitch_center,limits.pitch_max)==(1255,1555,1855),'Unexpected pitch limits')
    audio=rig.audio_capture
    require(audio.localization_mode=='four_mic' and audio.channels==4 and audio.wasapi_raw,'Four-channel RAW profile missing')
    require((audio.ear_left_channel,audio.ear_right_channel,audio.body_left_channel,audio.body_right_channel)==(2,3,1,4),'Microphone mapping differs')
    require(audio.ear_mapping_confirmed and audio.body_mapping_confirmed,'Microphone mapping is not confirmed')
    require(abs(audio.body_yaw_units_per_degree-2000/270)<1e-9 and audio.body_yaw_zero_position==1952,'Yaw geometry differs')
    require(not any((rig.features.audio_enabled,rig.features.visual_enabled,rig.features.camera_enabled)),'Example feature switches should start off')

    profile_path=alignment_profile_path(ROOT/rig.camera.stereo_calibration_path)
    profile=json.loads(profile_path.read_text(encoding='utf-8'))
    require(profile['metric_geometry_enabled'] is False,'Alignment preview must not claim metric depth')
    rectifier=StereoRectifier(profile,expected_size=(1920,1080),rotations=(180,180),allow_alignment_only=True)
    require(rectifier.metric_ready is False,'Alignment preview unexpectedly enables metric depth')
    require((ROOT/'output/calibration/stereo-charuco-A4-20mm.pdf').read_bytes().startswith(b'%PDF'),'Packaged board PDF missing')

    model=json.loads((ROOT/'models/silero_vad.source.json').read_text(encoding='utf-8'))
    require(hashlib.sha256((ROOT/'models/silero_vad.onnx').read_bytes()).hexdigest()==model['sha256'],'Silero model checksum differs')
    manifest=json.loads((ROOT/'hardware/cad/panda_source_manifest.json').read_text(encoding='utf-8'))
    for item in manifest['files']:
        path=ROOT/item['path']
        require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==item['sha256'],
                'CAD source checksum differs: '+item['path'])

    syntax=ast.parse((ROOT/'src/locator_demo/web/app.py').read_text(encoding='utf-8'))
    revisions=[v.value for n in ast.walk(syntax) if isinstance(n,ast.Dict)
               for k,v in zip(n.keys,n.values) if isinstance(k,ast.Constant)
               and k.value=='algorithm_revision' and isinstance(v,ast.Constant)]
    require('R51' in revisions,'Source is not the expected R51 revision')

    result={'successful':True,'revision':'R51','frontend_tests':frontend['tests'],
            'four_mic_profile_valid':True,'sensor_features_default_off':True,
            'physical_yaw_scale_configured':True,'alignment_metric_output_enabled':False,
            'board_pdf_present':True,'silero_model_digest_valid':True,'cad_sources_digest_valid':True,
            'hardware_io':False,'physical_calibration_verified':False}
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'publication-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
