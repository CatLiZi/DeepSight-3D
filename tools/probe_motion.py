"""Save an offline, fake-actuator replay of the same smooth hardware adapter."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from locator_demo.head import AxisConfig, HeadPose
from locator_demo.motion import SmoothHeadHardware
from locator_demo.settings import ControlConfig

clock = [0.0]
commands = []
head = SimpleNamespace(yaw=AxisConfig(servo_id=0), pitch=AxisConfig(servo_id=2),
                       move_to=lambda pose, **kwargs: commands.append({'t': clock[0], 'yaw': pose.yaw, 'pitch': pose.pitch, **{'time_ms': kwargs['time_ms']}}))
config = ControlConfig()
adapter = SmoothHeadHardware(head, initial=HeadPose(1500, 1500), config=config, clock=lambda: clock[0], autostart=False)
goals = {0: HeadPose(1700, 1400), 40: HeadPose(1340, 1630), 150: HeadPose(1640, 1490)}
trace = []
for index in range(451):
    clock[0] = index / 50
    if index in goals:
        adapter.move_to(goals[index], time_ms=100)
    state = adapter.planner.sample(clock[0])
    trace.append([clock[0], *state[0], *state[1], adapter._goal.yaw, adapter._goal.pitch])
    adapter.step()
values = np.array(trace)
velocity = max(abs(values[:, 2]).max(), abs(values[:, 5]).max())
acceleration = max(abs(values[:, 3]).max(), abs(values[:, 6]).max())
jerk = max(abs(np.diff(values[:, 3]) / .02).max(), abs(np.diff(values[:, 6]) / .02).max())
report = {'scope': 'Offline replay of SmoothHeadHardware with a fake actuator; no serial I/O or physical motion',
          'physical_test': False, 'sample_hz': 50, 'units': 'servo position units, not degrees',
          'columns': ['t', 'yaw', 'yaw_velocity', 'yaw_acceleration', 'pitch', 'pitch_velocity', 'pitch_acceleration', 'goal_yaw', 'goal_pitch'],
          'samples': trace, 'position_commands': commands,
          'measured_planner_maxima': {'velocity': float(velocity), 'acceleration': float(acceleration), 'jerk_from_acceleration': float(jerk)},
          'limits': {'velocity': config.max_velocity, 'acceleration': config.max_acceleration, 'jerk': config.max_jerk}}
assert velocity <= config.max_velocity + 1e-6
assert acceleration <= config.max_acceleration + 1e-6
assert jerk <= config.max_jerk + 1e-6
assert all(1200 - 1e-6 <= row[index] <= 1800 + 1e-6 for row in trace for index in (1, 4))
destination = PROJECT.parent / 'hardware_inventory/stage1/motion-replay.json'
destination.write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps({'samples': len(trace), 'commands': len(commands), 'maxima': report['measured_planner_maxima'], 'physical_test': False}))
