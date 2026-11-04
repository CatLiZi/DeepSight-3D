"""Four-channel acquisition adapter. Geometry-dependent localization is a separate stage."""
from __future__ import annotations

from pathlib import Path
import time
from datetime import datetime
from dataclasses import replace
import inspect

import numpy as np

from .audio import AudioDirection, AudioEstimate
from .audio_device import AudioDeviceStatus
from .capture import WasapiAudioCapture
from .ear_localization import EarPairConfig, EarPairLocalizer
from .speech_vad import StreamingSpeechVAD, update_window_probability
from .pair_fusion import FourMicLocalizer
from .speech_continuity import SpeechContinuityLocalizer
from .speech_recovery import BoundedContextLocalizer
from .audio_policy import DEFAULT_AUDIO_POLICY


class WasapiArrayAudio:

    def __init__(self, *, device_name="YDPI4MIC", channels=4, sample_rate=48000, block_ms=20,
                 usb_vid="EF09", usb_pid="459D", recording_enabled=False, output_dir=None,
                 wasapi_raw=True, ear_config=None,speech_vad=False,body_config=None,pose_provider=None,decision_policy=DEFAULT_AUDIO_POLICY):
        if (channels, sample_rate) != (4, 48000) or not wasapi_raw:
            raise ValueError('Four-mic localization requires 4 channels / 48000 Hz / WASAPI RAW')
        self.capture = WasapiAudioCapture(device_name=device_name, channels=channels, sample_rate=sample_rate,
                                          block_ms=block_ms, usb_vid=usb_vid, usb_pid=usb_pid, wasapi_raw=wasapi_raw)
        self.sample_rate = sample_rate
        self.channels = channels
        self._recording_enabled = recording_enabled
        self._output_dir = Path(output_dir) if output_dir else None
        self.ear_config = ear_config or EarPairConfig()
        self.decision_policy=decision_policy
        self.localizer = EarPairLocalizer(self.ear_config, sample_rate,policy=decision_policy)
        self.four_mic = FourMicLocalizer(self.localizer,body_config,sample_rate) if body_config else None
        self.pose_provider = pose_provider
        self.voice=StreamingSpeechVAD(sample_rate=sample_rate) if speech_vad else None
        self.body_voice=StreamingSpeechVAD(sample_rate=sample_rate) if speech_vad and self.four_mic else None
        if speech_vad and self.four_mic:self.four_mic=BoundedContextLocalizer(SpeechContinuityLocalizer(self.four_mic))
        self.diagnostic_sink = None
        self._last_clock_health = None
        self._last_sequence = 0
        self._last_received = None
        self._last_sample_end = 0
        self._estimate = AudioEstimate(AudioDirection.UNKNOWN, 0, 0, 0, noise_state="ear_mapping_unconfigured")

    @property
    def localization_ready(self):
        localizer = self.four_mic or self.localizer
        return bool(self.ear_config.ready and localizer.diagnostics["signal_valid"])

    def _unknown(self,reason):
        return (self.four_mic or self.localizer)._unknown(reason)

    def _reset_voice(self):
        if self.voice:self.voice.reset()
        if self.body_voice:self.body_voice.reset()
        if isinstance(self.four_mic,BoundedContextLocalizer):self.four_mic.reset_voice_context()

    def read_estimate(self):
        self.capture.start()
        metrics = self.capture.metrics()
        health = metrics.get('sample_clock_valid')
        if health is not None and health != self._last_clock_health:
            previous = self._last_clock_health
            self._last_clock_health = health
            if self.diagnostic_sink and (health is False or previous is False):
                self.diagnostic_sink('audio_capture_fault' if health is False else 'audio_capture_recovered',
                    dict(reason=metrics.get('message'), observed_sample_rate_hz=metrics.get('observed_sample_rate_hz'),
                         requested_sample_rate_hz=self.sample_rate, wasapi_raw=metrics.get('raw_stream_configured')))
        if metrics["ok"] and self._recording_enabled and self._output_dir and self.capture._recorder is None:
            self.capture.start_recording(self._output_dir / ("array-4ch-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".wav"))
        if metrics.get('sample_clock_valid') is False:
            self._reset_voice()
            self._estimate = self._unknown('sample_clock_mismatch')
            return self._estimate
        if not self.ear_config.ready:
            return self._estimate
        # recent() copies one window under the capture lock. Its tail owns the
        # PCM clock and metadata; a separate latest() can race a new callback
        # or reopened stream and attach an old clock to newer PCM.
        window_s = self.localizer.analysis_window_s
        samples = self.capture.recent(window_s)
        sample = samples[-1] if samples else None
        if sample is None or time.perf_counter() - sample.received_monotonic > .25:
            self._reset_voice()
            self._estimate = self._unknown('stale_audio')
            return self._estimate
        end = sample.sample_start + sample.frame_count
        if (self._last_received != sample.received_monotonic and self._last_received is not None
                and (sample.sequence <= self._last_sequence or end <= self._last_sample_end)):
            # A reopened stream resets the PCM clock. Old VAD context must not
            # keep the previous speaker probability until that clock catches up.
            self._last_sequence = 0
            self._unknown('audio_restarted')
            self._reset_voice()
        if sample.sequence != self._last_sequence:
            self._last_received = sample.received_monotonic
            self._last_sample_end = end
            contiguous = (not any(s.discontinuity for s in samples) and
                          all(a.sample_start + a.frame_count == b.sample_start for a, b in zip(samples, samples[1:])))
            self._last_sequence = sample.sequence
            if contiguous:
                raw = b"".join(s.pcm for s in samples)
                values = np.frombuffer(raw, "<i2").reshape(-1, 4)[-round(window_s*self.sample_rate):]
                probability = None
                if self.voice:
                    selected=values[:,np.asarray(self.ear_config.selected)-1]
                    end=sample.sample_start+sample.frame_count
                    probability=update_window_probability(self.voice,selected,sample_start=end-len(values))
                # VAD precedes direction processing: only non-speech can update
                # the noise spectrum, using identical preprocessing for both ears.
                if self.four_mic:
                    body_probability = (update_window_probability(self.body_voice,
                        values[:,np.asarray(self.four_mic.body_config.selected)-1],sample_start=end-len(values))
                        if self.body_voice else None)
                    pose = None
                    if self.pose_provider:
                        try:
                            # Legacy zero-argument providers remain supported;
                            # the runtime provider receives the actual PCM clock.
                            kwargs=dict(observed_monotonic=sample.received_monotonic,
                                        analysis_window_s=len(values)/self.sample_rate)
                            signature=inspect.signature(self.pose_provider)
                            try:signature.bind(**kwargs)
                            except TypeError:kwargs={}
                            pose = self.pose_provider(**kwargs)
                        except Exception:pass  # Pose absence removes body-angle fusion, not ear acquisition.
                    self._estimate = self.four_mic.estimate(values,speech_probability=probability,
                        body_speech_probability=body_probability,sample_end=end,pose=pose,
                        **({'instantaneous_speech_probability':getattr(self.voice,'probability',probability),
                            'instantaneous_body_probability':getattr(self.body_voice,'probability',body_probability)}
                           if isinstance(self.four_mic,(SpeechContinuityLocalizer,BoundedContextLocalizer)) else {}),
                        observed_monotonic=sample.received_monotonic)
                    probability = (self.four_mic.output_speech_probability
                        if isinstance(self.four_mic,BoundedContextLocalizer) else
                        body_probability if self.four_mic.fusion['status']=='body_fallback'
                        else self.four_mic.last_speech_probability)
                else:
                    self._estimate = self.localizer.estimate(values, speech_probability=probability,sample_end=end)
                if self.voice:
                    if probability is not None:
                        backend=('silero_onnx_cpu_bounded_context'
                            if isinstance(self.four_mic,BoundedContextLocalizer) and self.four_mic.used_for_output
                            else 'silero_onnx_cpu')
                        self._estimate=replace(self._estimate,speech_confidence=probability,speech_backend=backend)
                    else:self._estimate=replace(self._estimate,speech_backend='spectral_fallback',confidence=self._estimate.confidence*.7)
            else:
                self._estimate = self._unknown("audio_gap")
                self._reset_voice()
            self._estimate=replace(self._estimate,observed_monotonic=sample.received_monotonic,sample_sequence=sample.sequence)
        return self._estimate

    def status(self):
        metrics = self.capture.metrics()
        localizer = self.four_mic or self.localizer
        return AudioDeviceStatus(metrics["ok"], metrics["device_name"],
                                 metrics["message"] + "; " + localizer.diagnostics["reason"])

    def capture_metrics(self):
        localizer = self.four_mic or self.localizer
        active = sorted(self.ear_config.selected+self.four_mic.body_config.selected) if self.four_mic else self.ear_config.selected
        voice = self.voice.metrics() if self.voice else {'backend':'spectral','available':False}
        if self.four_mic:
            voice = {**voice,'speech_probability':round(self._estimate.speech_confidence,4),
                     'channels':'independent ear and body pairs; four channels, no waveform downmix',
                     'body':self.body_voice.metrics() if self.body_voice else {'backend':'spectral','available':False}}
        extra = ({**self.four_mic.metrics(),
                  'body_left_channel':self.four_mic.body_config.left_channel,
                  'body_right_channel':self.four_mic.body_config.right_channel,
                  'body_spacing_m':self.four_mic.body_config.spacing_m,
                  'body_mapping_confirmed':self.four_mic.body_config.mapping_confirmed,
                  'channel_roles':{str(self.ear_config.left_channel):'ear_left',
                                   str(self.ear_config.right_channel):'ear_right',
                                   str(self.four_mic.body_config.left_channel):'body_left',
                                   str(self.four_mic.body_config.right_channel):'body_right'}}
                 if self.four_mic else {})
        return {**self.capture.metrics(), "localization_ready": self.localization_ready,
                "localization_mode": 'four_mic' if self.four_mic else self.ear_config.mode, "active_channels": active,
                "ignored_channels": [c for c in range(1, 5) if c not in active],
                "ear_left_channel": self.ear_config.left_channel, "ear_right_channel": self.ear_config.right_channel,
                "mapping_confirmed": self.ear_config.mapping_confirmed, "ear_spacing_m": self.ear_config.spacing_m,
                "geometry_calibrated": False, "front_back_resolved": False,
                "localization_message": localizer.diagnostics["reason"],
                "speech_vad":voice,"localization_diagnostics":dict(localizer.diagnostics),**extra}

    def comparison_metrics(self):
        return None

    def set_recording_enabled(self, enabled):
        self._recording_enabled = bool(enabled)
        if not enabled:
            self.capture.stop_recording()

    def set_vad_enabled(self,enabled):
        if self.four_mic:
            baseline=self.four_mic
            while isinstance(baseline,(SpeechContinuityLocalizer,BoundedContextLocalizer)):baseline=baseline.baseline
            self.four_mic=BoundedContextLocalizer(SpeechContinuityLocalizer(baseline)) if enabled else baseline
            self.four_mic._unknown('audio_restarted')
        self.voice=StreamingSpeechVAD(sample_rate=self.sample_rate) if enabled else None
        self.body_voice=StreamingSpeechVAD(sample_rate=self.sample_rate) if enabled and self.four_mic else None
        self._estimate = self._unknown('audio_restarted')

    def set_decision_policy(self,policy):
        if policy==getattr(self,'decision_policy',DEFAULT_AUDIO_POLICY):return
        body_config=self.four_mic.body_config if self.four_mic else None
        self.decision_policy=policy
        self.localizer=EarPairLocalizer(self.ear_config,self.sample_rate,policy=policy)
        self.four_mic=FourMicLocalizer(self.localizer,body_config,self.sample_rate) if body_config else None
        if self.four_mic and self.voice:self.four_mic=BoundedContextLocalizer(SpeechContinuityLocalizer(self.four_mic))
        self._reset_voice()
        self._estimate=self._unknown('audio_policy_changed')

    def recent_mono_audio(self, *, seconds=2.0):
        samples = self.capture.recent(seconds)
        if (not samples or any(s.discontinuity for s in samples) or
                not all(a.sample_start + a.frame_count == b.sample_start for a, b in zip(samples, samples[1:]))):
            return self.sample_rate, []
        raw = b"".join(sample.pcm for sample in samples)
        values = np.frombuffer(raw, dtype="<i2").reshape(-1, self.channels)
        if len(self.ear_config.selected) != 2:
            return self.sample_rate, []
        selected = values[:, np.array(self.ear_config.selected) - 1]
        mono = selected[-round(seconds * self.sample_rate):].astype(np.float32).mean(axis=1) / 32768
        return self.sample_rate, mono.tolist()

    def stop(self):
        self.capture.stop()
