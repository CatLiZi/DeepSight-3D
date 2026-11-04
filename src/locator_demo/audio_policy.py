"""Single source of audio admission thresholds; stage roles stay explicit."""
from dataclasses import dataclass,asdict,replace
import math
from numbers import Real


def finite(x):
    return isinstance(x,Real) and not isinstance(x,bool) and math.isfinite(x)


@dataclass(frozen=True)
class AudioDecisionPolicy:
    speech: float=.5
    confidence: float=.2
    doa: float=.2
    required_hits: int=1
    stability: float=.5
    max_spread_deg: float=25.
    freshness_s: float=.35
    noise_learning_max: float=.1

    def __post_init__(self):
        for name,minimum in (('speech',.5),('confidence',.2),('doa',.2)):
            value=getattr(self,name)
            if not finite(value) or not minimum<=value<=1:raise ValueError('Invalid effective audio '+name)
        if isinstance(self.required_hits,bool) or not isinstance(self.required_hits,int) or not 1<=self.required_hits<=5:
            raise ValueError('Audio hits must be 1..5')
        # Quality/clock protections are fixed, not additional UI tuning knobs.
        if (self.stability,self.max_spread_deg,self.freshness_s,self.noise_learning_max)!=(.5,25.,.35,.1):
            raise ValueError('Audio reliability protections cannot be overridden')

    @classmethod
    def from_config(cls,config):
        return cls(speech=max(.5,config.speech_confidence_threshold),
                   confidence=max(.2,config.audio_confidence_threshold),
                   doa=max(.2,config.doa_confidence_threshold),required_hits=config.required_audio_hits)

    @property
    def detection_speech(self):return max(.45,self.speech-.05)

    @property
    def fusion_doa(self):return max(.25,self.doa)

    @property
    def context_recovery_speech(self):return max(.7,self.speech)

    @property
    def context_recovery_period_s(self):return 8.

    @property
    def context_recovery_shape_change(self):return .01

    @property
    def context_recovery_confirmation_s(self):return .12

    def quality_reason(self,audio,*,fusion=False):
        if audio is None:return 'no_audio'
        if getattr(audio.direction,'value',audio.direction)=='unknown':return audio.noise_state or 'direction_unknown'
        if audio.motor_suppressed:return 'motor_acoustic_rejected'
        values=(audio.speech_confidence,audio.confidence,audio.doa_confidence,audio.temporal_stability,audio.azimuth_std_deg,audio.azimuth_deg)
        if not all(finite(v) for v in values) or not all(0<=v<=1 for v in values[:4]):return 'invalid_audio_probability'
        if audio.speech_confidence<self.speech:return 'low_speech_probability'
        if audio.confidence<self.confidence or audio.doa_confidence<(self.fusion_doa if fusion else self.doa):return 'low_direction_confidence'
        if audio.temporal_stability<self.stability:return 'direction_not_stable'
        if not (1 if fusion else 0)<=audio.azimuth_std_deg<=self.max_spread_deg or abs(audio.azimuth_deg)>90:return 'uncertain_direction'
        if audio.noise_state in {'noise','quiet','fan_or_motor','low_band_noise','denoise_guard','ambiguous_delay','low_coherence','direction_transition','stale_audio','sample_clock_mismatch','channels_nearly_identical'}:
            return audio.noise_state
        return 'usable_direction'

    def describe(self):
        return {**asdict(self),'detection_speech':self.detection_speech,'fusion_doa':self.fusion_doa,
                'context_recovery_speech':self.context_recovery_speech,
                'context_recovery_period_s':self.context_recovery_period_s,
                'context_recovery_shape_change':self.context_recovery_shape_change,
                'context_recovery_confirmation_s':self.context_recovery_confirmation_s,
                'schema':'audio_policy_v1','score_kind':'window speech support, not calibrated speaker probability',
                'roles':{'detection':'allow current-window delay analysis',
                         'speech':'reliable voice for motion and face association',
                         'fusion_doa':'extra confidence for combining independent microphone directions',
                         'context_recovery':'independent finite-context lane; stricter voice and fresh acoustic corroboration, never a weaker primary threshold',
                         'noise_learning_max':'continuity branch only; the independent compatibility baseline retains its original noise reference',
                         'required_hits':'minimum fresh observations; core/face dwell protections remain'}}


DEFAULT_AUDIO_POLICY=AudioDecisionPolicy()


def admit_audio(estimate,policy=DEFAULT_AUDIO_POLICY):
    """One runtime admission point before core tracking and face association."""
    reason=policy.quality_reason(estimate)
    if estimate is None:return None,reason
    if reason=='usable_direction':return estimate,reason
    if getattr(estimate.direction,'value',estimate.direction)=='unknown':return estimate,reason
    from .audio import AudioDirection
    return replace(estimate,direction=AudioDirection.UNKNOWN,confidence=0.,noise_state=reason),reason


def explain_audio_decision(*,audio,raw,capture,policy,running,audio_enabled,visual_enabled,
                           moving,fault,core,now,steering_reason=''):
    """Read-only explanation: never used as an actuator authorization."""
    vad=capture.get('speech_vad',{});body_vad=vad.get('body',{})
    recovery=capture.get('context_recovery',{})
    scores=[vad.get('speech_probability'),vad.get('analysis_window_speech_support'),
            body_vad.get('analysis_window_speech_support'),getattr(raw,'speech_confidence',0.),
            recovery.get('voice_support')]
    score=max((v for v in scores if finite(v) and 0<=v<=1),default=0.)
    voice=finite(score) and 0<=score<=1 and score>=policy.detection_speech
    reason=policy.quality_reason(audio)
    if voice and reason=='no_voice_activity':reason='voice_detected_without_direction'
    if voice and reason=='voice_detected_without_direction':
        state=recovery.get('state')
        if state=='confirming_fresh_recovery':reason='context_recovery_pending'
        elif state=='stationary_spectrum':reason='context_recovery_stationary'
        elif state=='voice_below_recovery_requirement':reason='context_recovery_low_voice'
        elif state in ('ambiguous_delay','low_coherence','low_snr','invalid_delay','direction_transition'):reason=state
    if (voice and capture.get('pairs',{}).get('body',{}).get('usable') is True
            and not capture.get('pairs',{}).get('ears',{}).get('usable')
            and capture.get('fusion',{}).get('reason')=='head_body_pose_unavailable'):
        reason='body_pose_unavailable'
    observed=getattr(audio,'observed_monotonic',0.)
    if reason=='usable_direction' and (not finite(observed) or observed<=0 or not -.02<=now-observed<=policy.freshness_s):reason='stale_audio'
    if capture.get('ok') is False or capture.get('sample_clock_valid') is False:reason='capture_unhealthy'
    labels={'usable_direction':'可靠声音方向可用','no_voice_activity':'尚未检出可靠人声',
        'quiet':'输入较弱，尚未形成可靠方向','low_speech_probability':'人声证据未达到转头/选人要求',
        'low_direction_confidence':'方向质量未达到要求','direction_not_stable':'方向尚未稳定',
        'uncertain_direction':'角度不确定度过大','ambiguous_delay':'时差存在竞争峰，暂不选方向',
        'low_coherence':'麦克风信号一致性不足','low_snr':'信噪比不足','direction_transition':'正在确认新的方向',
        'stale_audio':'声音数据已过期','capture_unhealthy':'音频采集状态异常',
        'voice_detected_without_direction':'检出语音证据，但尚无可靠方向',
        'context_recovery_pending':'辅助语音方向正在进行连续确认',
        'context_recovery_stationary':'辅助通道收到近乎固定频谱，暂不作为新的人声方向',
        'context_recovery_low_voice':'主通道未确认，辅助语音尚未达到恢复要求',
        'body_pose_unavailable':'身体方向已检出，但头身角度尚不可换算',
        'motion_acoustic_uncertain':'运动中声学证据不足','invalid_audio_probability':'声音数据数值无效'}
    admitted=reason=='usable_direction'
    if fault:code='servo_fault';label='舵机反馈或控制故障，动作已停止'
    elif not running:code='paused';label='自动转头已暂停；采集和显示继续'
    elif not audio_enabled:code='audio_control_disabled';label='听声转头已关闭'+('，当前以视觉跟随' if visual_enabled else '')
    elif core.get('motion_reason')=='visual_target_has_priority':code='visual_priority';label='视觉目标优先，本次声音未接管转向'
    elif steering_reason=='yaw_angle_calibration_missing':code='yaw_geometry_missing';label='缺少偏航刻度/角度比例，不能生成听声位置目标'
    elif steering_reason=='invalid_pose_model':code='invalid_yaw_geometry';label='偏航角度配置或当前位置无效，听声动作已阻止'
    elif moving and core.get('motion_authorized'):code='finishing_target';label='正在完成已确认目标的转向；短暂停顿不取消目标'
    elif core.get('motion_authorized') and steering_reason in ('core_step_submitted','core_goal_submitted','finishing_core_motion'):
        code='finishing_target';label='正在分段完成已确认目标；声音暂停不缩短最终位置'
    elif steering_reason=='core_goal_reached':code='target_reached';label='已到达核心目标，无需继续转动'
    elif steering_reason=='step_budget_reached':code='search_budget';label='本轮听声搜索次数已达上限，等待新的可靠声音'
    elif steering_reason=='yaw_limit':code='yaw_limit';label='已到当前软件转头边界，停止继续增加角度'
    elif steering_reason=='sound_centered_or_window_limit':code='center_or_search_limit';label='声音已居中或本轮搜索范围已到，无需继续增加步幅'
    elif not admitted:code='waiting_reliable_audio';label=labels.get(reason,'暂未形成可靠声音方向')
    elif not core.get('locked'):code='confirming_core';label='方向可靠，正在确认核心声源'
    elif not core.get('motion_authorized'):code='waiting_motion_permission';label='核心位置已保留，当前转向条件未满足'
    else:code='audio_motion_ready';label='声音和核心目标条件已满足，按控制节奏执行'
    return dict(voice=dict(detected=voice,score=score,threshold=policy.detection_speech),
        direction=dict(usable=admitted,reason=reason,label=labels.get(reason,'方向暂不可用：'+reason)),
        control=dict(code=code,label=label,core_reason=core.get('motion_reason'),steering_reason=steering_reason),
        read_only=True)
