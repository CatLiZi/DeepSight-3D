"""Exercise the real application acquisition and inference, without servo motion."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time
import wave

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from locator_demo.web.app import DemoRuntime, RuntimeConfig


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=float, default=30)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    runtime = DemoRuntime(RuntimeConfig(settings_path=PROJECT / 'locator_demo_settings.json'))
    assert not runtime.running
    assert runtime.bus._serial is None
    report = {'started': datetime.now().isoformat(), 'automatic_motion': False, 'servo_io': False, 'snapshots': []}
    try:
        runtime.start_control_loop()
        deadline = time.perf_counter() + 35
        while time.perf_counter() < deadline:
            metrics = runtime.snapshot(include_frame=False)
            if (metrics['visual']['capture']['ok'] and metrics['audio']['capture']['ok']
                    and metrics['visual']['status']['ok']
                    and metrics['visual']['capture']['processed_frames'] >= 10):
                break
            time.sleep(.05)
        else:
            raise RuntimeError('Application capture and model did not warm up within 35 seconds')
        report['measurement_start_condition'] = 'camera/audio active and at least 10 completed model frames; model initialization excluded'
        runtime.audio.capture.start_recording(args.output / 'runtime-full-4ch.wav')
        start = time.perf_counter()
        first = runtime.vision.capture.latest().sequence
        while time.perf_counter() - start < args.seconds:
            report['snapshots'].append({'elapsed_s': time.perf_counter() - start,
                                        'state': runtime.snapshot(include_frame=False)})
            time.sleep(.5)
        elapsed = time.perf_counter() - start
        final = runtime.vision.capture.latest()
        report['measurement'] = {'wall_s': elapsed, 'frames': final.sequence - first,
                                  'capture_fps': (final.sequence - first) / elapsed}
        report['final_state'] = runtime.snapshot(include_frame=False)
        (args.output / 'runtime-stereo.jpg').write_bytes(final.jpeg)
        runtime.audio.capture.save_recent(args.output / 'runtime-recent-4ch.wav', seconds=3)
    except Exception as exc:
        report['error'] = str(exc)
    finally:
        runtime.shutdown()
        report['shutdown'] = {'control_thread_stopped': not runtime.control_loop_active,
                               'camera_process_closed': runtime.vision.capture._process is None,
                               'audio_stream_closed': runtime.audio.capture._stream is None,
                               'serial_never_opened': runtime.bus._serial is None}
        recording = args.output / 'runtime-full-4ch.wav'
        if recording.exists():
            with wave.open(str(recording), 'rb') as wav:
                report['recording'] = {'channels': wav.getnchannels(), 'sample_rate': wav.getframerate(),
                                        'sample_bits': wav.getsampwidth() * 8, 'frames': wav.getnframes()}
        report['audio_after_stop'] = runtime.audio.capture.metrics()
        (args.output / 'validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'measurement': report.get('measurement'), 'error': report.get('error'),
                      'recording': report.get('recording'), 'shutdown': report['shutdown'],
                      'final_camera': report.get('final_state', {}).get('visual', {}).get('capture'),
                      'final_audio': report.get('final_state', {}).get('audio', {}).get('capture')}, ensure_ascii=True), flush=True)
    raise SystemExit(bool(report.get('error')))


if __name__ == '__main__':
    main()
