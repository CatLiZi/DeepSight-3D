"""Reproduce 240 known-angle acoustic goal checks without accessing hardware."""
from dataclasses import replace
from pathlib import Path
import argparse,json,random,sys

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'src'))
from locator_demo.audio import AudioEstimate,AudioDirection
from locator_demo.dominant_sound import DominantSoundLock


def observation(angle,now,sequence):
    return AudioEstimate(AudioDirection.LEFT if angle>0 else AudioDirection.RIGHT,.95,0.,.5,
        azimuth_deg=angle,speech_confidence=.95,doa_confidence=.95,temporal_stability=.95,
        azimuth_std_deg=6.,observed_monotonic=now,sample_sequence=sequence,noise_state='speech')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path);args=parser.parse_args()
    rng=random.Random(2026110450);scale=2000/270;errors=[]
    for case in range(240):
        step,window=(220,340) if case%2 else (160,280)
        yaw=rng.randint(1450,2450);angle=rng.uniform(-85,85);sign=1 if case%3 else -1
        ideal=max(1404,min(2500,yaw+sign*scale*angle));expected=round(ideal)
        lock=DominantSoundLock()
        for i in range(14):
            now=10+i*.04
            state=lock.update(observation(angle,now,i+1),now=now,yaw=yaw,
                units_per_degree=scale,left_sign=sign,yaw_min=1404,yaw_max=2500,command_window=window)
        assert state['locked'] and state['goal_yaw']==expected
        command=yaw
        for i in range(10):
            now=11+i*.1
            unknown=replace(observation(0,now,50+i),direction=AudioDirection.UNKNOWN,
                confidence=0.,speech_confidence=0.,doa_confidence=0.,noise_state='no_voice_activity')
            state=lock.update(unknown,now=now,yaw=command,units_per_degree=scale,
                left_sign=sign,yaw_min=1404,yaw_max=2500,command_window=window)
            assert state['goal_yaw']==expected and not state['fresh_core_observation']
            next_yaw=lock.next_yaw(command,max_step=step)
            assert 1404<=next_yaw<=2500 and abs(next_yaw-command)<=step
            command=next_yaw
        assert command==expected
        errors.append(abs(expected-ideal)/scale)
    result={'cases':240,'successful':True,'max_rounding_error_deg':max(errors),'hardware_io':False,
            'scope':'known simulated input; not a measurement of physical angle or sound accuracy'}
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result))


if __name__=='__main__':main()
