"""Run self-contained JavaScript display checks; no physical devices."""
import argparse,json,subprocess,sys
from pathlib import Path

PROJECT=Path(__file__).resolve().parents[1]
STATIC=PROJECT/'src/locator_demo/web/static'
GROUPS=[('test_audio_view.js',['audio_view.js']),('test_dominant_sound_view.js',['audio_view.js']),
 ('test_demo_ownership_view.js',['ui_shell.js']),('test_device_status_view.js',['ui_shell.js']),
 ('test_settings_feedback.js',['app.js']),('test_fixation_view.js',['fixation_view.js']),
 ('test_array_view.js',['array_view.js']),('test_audio_policy_view.js',['audio_view.js','ui_shell.js'])]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path);args=parser.parse_args()
    groups=[]
    for script,targets in GROUPS:
        result=subprocess.run(['node',str(PROJECT/'tools'/script)]+[str(STATIC/name) for name in targets],
                              check=True,capture_output=True,text=True,encoding='utf-8')
        groups.append(json.loads(result.stdout))
    for name in ('app.js','ui_shell.js','audio_view.js','fixation_view.js','array_view.js'):
        subprocess.run(['node','--check',str(STATIC/name)],check=True,capture_output=True)
    summary={'successful':True,'tests':sum(row['tests'] for row in groups)+5,'groups':groups,'hardware_io':False}
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'successful':True,'tests':summary['tests'],'hardware_io':False}))

if __name__=='__main__':main()
