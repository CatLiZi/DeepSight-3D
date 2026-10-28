"""Measure current RAW PCM independently of camera/ML; no serial access."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import wave
import numpy as np

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT/'src'))
from locator_demo.capture import WasapiAudioCapture
from locator_demo.settings import load_settings


def analyze(path):
    with wave.open(str(path), 'rb') as wav:
        metadata = dict(channels=wav.getnchannels(), sample_rate=wav.getframerate(),
                        bits=wav.getsampwidth()*8, frames=wav.getnframes())
        values = np.frombuffer(wav.readframes(wav.getnframes()), '<i2').reshape(-1, metadata['channels'])
    x = values.astype(np.float64)
    centered = x-x.mean(axis=0)
    n = min(len(x), 65536)
    power = abs(np.fft.rfft(centered[:n]*np.hanning(n)[:, None], axis=0))**2
    frequencies = np.fft.rfftfreq(n, 1/metadata['sample_rate'])
    band = (frequencies >= 100) & (frequencies <= 4500)
    return dict(**metadata, pcm_sha256=hashlib.sha256(values.tobytes()).hexdigest(),
                rms=np.sqrt(np.mean(centered**2, axis=0)).tolist(),
                dc= x.mean(axis=0).tolist(),
                clipping_fraction=np.mean(abs(x)>=32760, axis=0).tolist(),
                correlation=np.corrcoef(centered.T).tolist(),
                speech_band_fraction=(power[band].sum(axis=0)/np.maximum(power.sum(axis=0), 1e-12)).tolist(),
                lag1_correlation=[float(np.corrcoef(centered[:-1,c], centered[1:,c])[0,1]) for c in range(metadata['channels'])],
                identical_pairs=[dict(channels=[a+1,b+1], identical=bool(np.array_equal(values[:,a], values[:,b])))
                                 for a in range(metadata['channels']) for b in range(a+1,metadata['channels'])])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--seconds', default=8., type=float)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    settings = load_settings(PROJECT/'locator_demo_settings.json').audio_capture
    capture = WasapiAudioCapture(device_name=settings.device_name, channels=settings.channels,
        sample_rate=settings.sample_rate, block_ms=settings.block_ms, usb_vid=settings.usb_vid,
        usb_pid=settings.usb_pid, wasapi_raw=settings.wasapi_raw)
    report = dict(serial_access=False, motion=False)
    try:
        capture.start()
        deadline = time.perf_counter()+5
        while time.perf_counter()<deadline and not capture.metrics()['ok']:
            time.sleep(.05)
        if not capture.metrics()['ok']:
            raise RuntimeError(capture.metrics()['message'])
        time.sleep(.5)
        capture.start_recording(args.output/'raw-4ch.wav')
        start = time.perf_counter()
        first = capture.latest().sample_start+capture.latest().frame_count
        timeline = []
        while time.perf_counter()-start < args.seconds:
            timeline.append(dict(elapsed_s=time.perf_counter()-start, metrics=capture.metrics()))
            time.sleep(.1)
        end = time.perf_counter()
        last = capture.latest().sample_start+capture.latest().frame_count
        capture.stop_recording()
        report.update(duration_s=end-start, sample_frames=last-first,
                      sample_frames_per_wall_second=(last-first)/(end-start),
                      capture=capture.metrics(), timeline=timeline)
    except Exception as exc:
        report['error'] = str(exc)
    finally:
        capture.stop()
    if (args.output/'raw-4ch.wav').is_file():
        report['pcm'] = analyze(args.output/'raw-4ch.wav')
    (args.output/'validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='timeline'}, ensure_ascii=True))
    return bool(report.get('error'))


if __name__ == '__main__':
    raise SystemExit(main())
