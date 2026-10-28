"""Replay reference speech/noise and injected acoustic delays; no hardware I/O.

The official Silero sample has no local frame labels. Activity rates below
describe this sample, not VAD accuracy. Injected delays test sign and DSP only,
not physical source localization in the room.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import time
import wave

import numpy as np
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from locator_demo.speech_vad import StreamingSpeechVAD
from locator_demo.ear_localization import EarPairConfig, EarPairLocalizer
from locator_demo.audio import AudioDirection


def wav(path):
    with wave.open(str(path), 'rb') as stream:
        assert stream.getsampwidth() == 2
        return stream.getframerate(), np.frombuffer(stream.readframes(stream.getnframes()), '<i2').reshape(-1, stream.getnchannels())


def run_case(pair, *, expected_angle=None):
    vad = StreamingSpeechVAD()
    config = EarPairConfig('ear_pair', 3, 2, True, .13176241517)
    localizer = EarPairLocalizer(config)
    rows=[];start=time.perf_counter()
    for end in range(960, len(pair)+1, 960):
        block=pair[max(0, end-round(localizer.analysis_window_s*48000)):end]
        probability=vad.update(block, sample_start=end-len(block))
        pcm=np.zeros((len(block), 4), np.int16)
        pcm[:, [2, 1]]=block
        estimate=localizer.estimate(pcm,speech_probability=probability)
        accepted=bool(probability is not None and probability>=.5 and estimate.direction!=AudioDirection.UNKNOWN
                      and estimate.temporal_stability>=.5 and estimate.azimuth_std_deg<=25)
        rows.append({'t':end/48000, 'p':probability, 'accepted':accepted,
                     'angle':estimate.azimuth_deg if accepted else None,
                     'reason':estimate.noise_state})
    values=np.array([row['angle'] for row in rows if row['accepted']])
    active=sum(row['p']>=.5 for row in rows)
    result={'seconds':len(pair)/48000, 'windows':len(rows), 'speech_activity_fraction':active/max(1,len(rows)),
            'direction_accepted_windows':len(values), 'direction_when_voice_fraction':len(values)/max(1,active),
            'reasons':dict(Counter(row['reason'] for row in rows)), 'vad':vad.metrics(),
            'processing_s':time.perf_counter()-start, 'timeline':rows}
    if expected_angle is not None:
        errors=np.abs(values-expected_angle)
        result.update(expected_angle_deg=expected_angle, median_abs_error_deg=float(np.median(errors)) if len(errors) else None,
                      p95_abs_error_deg=float(np.percentile(errors,95)) if len(errors) else None,
                      correct_sign_fraction=float(np.mean(values*expected_angle>0)) if len(values) else None)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    rate,speech=wav(args.reference);assert rate==16000 and speech.shape[1]==1
    signal=resample_poly(speech[:,0].astype(float),3,1)
    signal*=.3*32767/max(1.,np.max(np.abs(signal)))
    rng=np.random.default_rng(8812);length=len(signal)
    cases={}
    for delay in (9,-9):
        left=signal.copy();right=np.zeros_like(left)
        if delay>0:right[delay:]=left[:-delay]
        else:right[:delay]=left[-delay:]
        pair=np.stack((left,right),axis=1)+rng.normal(0,9,(length,2))
        angle=float(np.degrees(np.arcsin(delay/48000/(.13176241517/343))))
        cases[f'speech_delay_{delay}']=run_case(np.clip(pair,-32768,32767).astype(np.int16),expected_angle=angle)
    n=48000*5;t=np.arange(n)/48000
    noise=rng.normal(0,550,(n,2));tone=np.stack((1500*np.sin(2*np.pi*440*t),1500*np.sin(2*np.pi*440*(t-.0001))),axis=1)
    cases['silence']=run_case(np.zeros((n,2),np.int16))
    cases['independent_white_noise']=run_case(noise.astype(np.int16))
    cases['narrowband_tone']=run_case(tone.astype(np.int16))
    rub=ROOT.parent/'hardware_inventory/stage2-head-20261023/ear-raw-excerpt-27-30s.wav'
    rate,pcm=wav(rub);assert rate==48000
    cases['recorded_ear_rubbing']=run_case(pcm[:,[2,1]])
    checks={
        'injected_delays_both_signs':all(cases[f'speech_delay_{d}']['correct_sign_fraction']==1. for d in (9,-9)),
        'injected_delay_p95_under_3deg':all((cases[f'speech_delay_{d}']['p95_abs_error_deg'] or 999)<3 for d in (9,-9)),
        'speech_has_accepted_directions':all(cases[f'speech_delay_{d}']['direction_accepted_windows']>100 for d in (9,-9)),
        'synthetic_noise_rejected':all(cases[key]['direction_accepted_windows']==0 for key in ('silence','independent_white_noise','narrowband_tone')),
    }
    result={'scope':__doc__, 'hardware_io':False,'servo_io':False,
            'reference_sha256':hashlib.sha256(args.reference.read_bytes()).hexdigest(),
            'recorded_rubbing_sha256':hashlib.sha256(rub.read_bytes()).hexdigest(),
            'checks':checks, 'cases':cases}
    (args.output/'validation.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({'checks':checks,'cases':{k:{a:b for a,b in v.items() if a not in ('timeline','vad')} for k,v in cases.items()}},indent=2))
    return not all(checks.values())


if __name__=='__main__':raise SystemExit(main())
