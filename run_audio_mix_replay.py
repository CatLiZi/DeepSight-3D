from __future__ import annotations

import argparse
import audioop
import sys
import wave
from array import array
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from locator_demo.audio import AudioDirection
from locator_demo.audio_denoise import save_pcm16_stereo_to_wav
from locator_demo.audio_device import FfmpegAudioLocalizer


def _clamp_sample(value: float) -> int:
    return max(-32768, min(32767, int(round(value))))


def _load_wav_as_stereo_pcm16(path: Path, *, sample_rate: int) -> bytes:
    with wave.open(str(path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sampwidth = wav_file.getsampwidth()
        source_rate = wav_file.getframerate()
        raw = wav_file.readframes(wav_file.getnframes())

    if sampwidth != 2:
        raw = audioop.lin2lin(raw, sampwidth, 2)
        sampwidth = 2

    if channels == 1:
        raw = audioop.tostereo(raw, sampwidth, 1.0, 1.0)
        channels = 2
    elif channels > 2:
        mono = audioop.tomono(raw, sampwidth, 0.5, 0.5)
        raw = audioop.tostereo(mono, sampwidth, 1.0, 1.0)
        channels = 2

    if source_rate != sample_rate:
        raw, _ = audioop.ratecv(raw, sampwidth, channels, source_rate, sample_rate, None)

    return raw


def _apply_pan(stereo_pcm16: bytes, *, left_gain: float, right_gain: float, gain: float) -> bytes:
    samples = array("h")
    samples.frombytes(stereo_pcm16)
    for index in range(0, len(samples), 2):
        samples[index] = _clamp_sample(samples[index] * left_gain * gain)
        if index + 1 < len(samples):
            samples[index + 1] = _clamp_sample(samples[index + 1] * right_gain * gain)
    return samples.tobytes()


def _pan_gains(name: str) -> tuple[float, float]:
    pan = str(name or "center").strip().lower()
    if pan == "left":
        return 1.0, 0.28
    if pan == "right":
        return 0.28, 1.0
    return 1.0, 1.0


def _fit_length(raw: bytes, target_bytes: int, *, loop: bool) -> bytes:
    if len(raw) == target_bytes:
        return raw
    if len(raw) > target_bytes:
        return raw[:target_bytes]
    if not raw:
        return b"\x00" * target_bytes
    if not loop:
        return raw + (b"\x00" * (target_bytes - len(raw)))
    repeats = (target_bytes + len(raw) - 1) // len(raw)
    return (raw * repeats)[:target_bytes]


def _mix_stereo_pcm16(first: bytes, second: bytes) -> bytes:
    mixed = audioop.add(first, second, 2)
    return mixed


def _duration_to_bytes(sample_rate: int, seconds: float) -> int:
    frames = max(1, int(round(sample_rate * seconds)))
    return frames * 2 * 2


def _estimate_text(prefix: str, t_s: float, direction: str, speech: float, energy: float, doa: float, azimuth: float) -> str:
    return (
        f"[{prefix}] t={t_s:6.2f}s direction={direction} speech={speech:.3f} "
        f"energy={energy:.3f} doa={doa:.3f} azimuth={azimuth:.1f}deg"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Mix speech/noise WAV files and replay the locator audio pipeline offline.")
    parser.add_argument("--input", default="", help="Path to a prerecorded mixed WAV file.")
    parser.add_argument("--speech", default="", help="Path to the speech WAV file.")
    parser.add_argument("--noise", default="", help="Path to the noise WAV file.")
    parser.add_argument("--speech-pan", choices=["left", "center", "right"], default="left")
    parser.add_argument("--noise-pan", choices=["left", "center", "right"], default="center")
    parser.add_argument("--speech-gain", type=float, default=1.0)
    parser.add_argument("--noise-gain", type=float, default=0.65)
    parser.add_argument("--duration", type=float, default=0.0, help="Output duration in seconds; 0 means use speech length.")
    parser.add_argument("--loop-noise", action="store_true", help="Loop noise if it is shorter than the target duration.")
    parser.add_argument("--denoise", action="store_true", help="Run the denoise branch before VOICE_DECISION.")
    parser.add_argument("--save-mix", default="", help="Optional output WAV path for the mixed stereo audio.")
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--frame-ms", type=int, default=40)
    args = parser.parse_args()

    if args.input:
        input_path = Path(args.input).expanduser().resolve()
        mixed_raw = _load_wav_as_stereo_pcm16(input_path, sample_rate=args.sample_rate)
        if args.duration > 0:
            target_bytes = _duration_to_bytes(args.sample_rate, args.duration)
            mixed_raw = _fit_length(mixed_raw, target_bytes, loop=False)
    else:
        if not args.speech or not args.noise:
            parser.error("Provide --input for a prerecorded mixed WAV, or provide both --speech and --noise.")
        speech_path = Path(args.speech).expanduser().resolve()
        noise_path = Path(args.noise).expanduser().resolve()

        speech_raw = _load_wav_as_stereo_pcm16(speech_path, sample_rate=args.sample_rate)
        noise_raw = _load_wav_as_stereo_pcm16(noise_path, sample_rate=args.sample_rate)

        if args.duration > 0:
            target_bytes = _duration_to_bytes(args.sample_rate, args.duration)
        else:
            target_bytes = len(speech_raw)

        speech_left, speech_right = _pan_gains(args.speech_pan)
        noise_left, noise_right = _pan_gains(args.noise_pan)
        speech_raw = _apply_pan(speech_raw, left_gain=speech_left, right_gain=speech_right, gain=args.speech_gain)
        noise_raw = _apply_pan(noise_raw, left_gain=noise_left, right_gain=noise_right, gain=args.noise_gain)
        speech_raw = _fit_length(speech_raw, target_bytes, loop=False)
        noise_raw = _fit_length(noise_raw, target_bytes, loop=args.loop_noise)
        mixed_raw = _mix_stereo_pcm16(speech_raw, noise_raw)

    if args.save_mix:
        save_pcm16_stereo_to_wav(Path(args.save_mix), mixed_raw, args.sample_rate)

    localizer = FfmpegAudioLocalizer(
        sample_rate=args.sample_rate,
        frame_ms=args.frame_ms,
        denoise=args.denoise,
        recording_enabled=False,
    )
    frame_bytes = max(1, int(args.sample_rate * args.frame_ms / 1000)) * 2 * 2

    human_frames = 0
    located_frames = 0
    total_frames = 0
    last_summary = ""
    for offset in range(0, len(mixed_raw), frame_bytes):
        frame = mixed_raw[offset : offset + frame_bytes]
        if len(frame) < frame_bytes:
            frame = frame + (b"\x00" * (frame_bytes - len(frame)))
        raw_estimate = localizer._estimate_from_raw(frame)
        denoised_frame = frame
        if args.denoise:
            localizer._ensure_denoiser()
            if localizer._denoiser is not None:
                denoised_frame = localizer._denoiser.process_pcm16(frame, sample_rate=args.sample_rate).pcm16_stereo
        denoised_estimate = localizer._estimate_from_raw(denoised_frame)
        downstream_estimate = localizer._human_voice_double_check(raw_estimate, denoised_estimate)
        smoothed_estimate = localizer._smoother.update(downstream_estimate)
        total_frames += 1
        t_s = offset / float(args.sample_rate * 2 * 2)

        if downstream_estimate.energy > 0:
            human_frames += 1
            print(
                _estimate_text(
                    "DOWNSTREAM",
                    t_s,
                    downstream_estimate.direction.value,
                    downstream_estimate.speech_confidence,
                    downstream_estimate.energy,
                    downstream_estimate.doa_confidence,
                    downstream_estimate.azimuth_deg,
                )
            )
        if smoothed_estimate.direction not in (AudioDirection.UNKNOWN, AudioDirection.CENTER):
            located_frames += 1
            summary = _estimate_text(
                "LOCATED",
                t_s,
                smoothed_estimate.direction.value,
                smoothed_estimate.speech_confidence,
                smoothed_estimate.energy,
                smoothed_estimate.doa_confidence,
                smoothed_estimate.azimuth_deg,
            )
            if summary != last_summary:
                print(summary)
                last_summary = summary

    print(
        f"[SUMMARY] frames={total_frames} human_frames={human_frames} located_frames={located_frames} "
        f"speech_pan={args.speech_pan} noise_pan={args.noise_pan} denoise={int(args.denoise)} input_mode={'single' if args.input else 'mixed'}"
    )
    if args.save_mix:
        print(f"[SUMMARY] mixed_wav={Path(args.save_mix).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())