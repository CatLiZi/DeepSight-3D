"""Hardware debug protections tested with a fake bus; never opens serial."""
from pathlib import Path
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from locator_demo.web.servo_debug import DebugMoveRequest, ObservationRequest, ServoDebugSession


class FakeBus:
    def __init__(self):
        self.values = {0: 1496, 2: 1505}
        self.moves = []
        self._armed_ids = set()
        self._io_lock = __import__('threading').RLock()
    def request(self, text, **kwargs):
        sid = int(text[1:4])
        value = self.values.get(sid)
        return f'#{sid:03d}P{value:04d}!' if value is not None else ''
    def close(self): pass
    def arm(self, sid): self._armed_ids.add(sid)
    def move_many(self, positions, time_ms): self.moves.append((positions, time_ms))


class ServoDebugTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        settings = {'port': 'COM3', 'yaw_id': 0, 'pitch_id': 2, 'control': {},
                    'axis_limits': {'yaw_min': 1200, 'yaw_center': 1500, 'yaw_max': 1800,
                                    'pitch_min': 1200, 'pitch_center': 1500, 'pitch_max': 1800}}
        (self.path/'settings.json').write_text(json.dumps(settings), encoding='utf-8')
        self.bus = FakeBus()
        self.now = [10.0]
        self.session = ServoDebugSession(self.path/'settings.json', self.path/'out',
                                        bus=self.bus, clock=lambda: self.now[0])
        self.addCleanup(self.session.shutdown)

    def test_connect_reads_actual_origins_without_moving(self):
        state = self.session.connect()
        self.assertEqual(state['axes']['yaw']['origin'], 1496)
        self.assertEqual(state['axes']['pitch']['position'], 1505)
        self.assertTrue(state['axes']['yaw']['can_move'])
        self.assertEqual(self.bus.moves, [])

    def test_missing_axis_is_disabled_and_not_replaced_with_target(self):
        self.bus.values[0] = None
        state = self.session.connect()
        self.assertFalse(state['axes']['yaw']['can_move'])
        self.assertIsNone(state['axes']['yaw']['position'])
        self.assertTrue(state['axes']['pitch']['can_move'])
        with self.assertRaises(RuntimeError):
            self.session.move(DebugMoveRequest(axis='yaw', delta=30))
        self.assertEqual(self.bus.moves, [])

    def test_out_of_range_target_sends_no_motion_and_does_not_arm(self):
        self.session.connect()
        with self.assertRaises(ValueError):
            self.session.move(DebugMoveRequest(axis='pitch', target=1801))
        self.assertEqual(self.bus.moves, [])
        self.assertFalse(self.bus._armed_ids)

    def test_move_rereads_feedback_before_arming(self):
        self.session.connect()
        self.bus.values[2] = None
        with self.assertRaises(RuntimeError):
            self.session.move(DebugMoveRequest(axis='pitch', delta=30))
        self.assertEqual(self.bus.moves, [])
        self.assertFalse(self.bus._armed_ids)

    def test_stale_feedback_is_not_displayed_as_current(self):
        self.session.connect()
        self.now[0] += 2
        state = self.session.state()
        self.assertFalse(state['axes']['yaw']['fresh'])
        self.assertFalse(state['axes']['yaw']['can_move'])

    def test_stop_during_preparation_cannot_start_a_trajectory(self):
        self.session.connect()
        self.bus.arm = lambda sid: self.session.stop()
        with patch('locator_demo.web.servo_debug.SmoothHeadHardware') as adapter:
            with self.assertRaises(RuntimeError):
                self.session.move(DebugMoveRequest(axis='pitch', delta=30))
            adapter.assert_not_called()
        self.assertEqual(self.bus.moves, [])

    def test_single_axis_adapter_does_not_enable_other_servo(self):
        self.bus.values[0] = None
        self.session.connect()
        with patch('locator_demo.web.servo_debug.SmoothHeadHardware') as adapter:
            self.session.move(DebugMoveRequest(axis='pitch', delta=30))
            head = adapter.call_args.args[0]
            self.assertIsNone(head.yaw)
            self.assertEqual(head.pitch.servo_id, 2)
            self.assertEqual(adapter.call_args.kwargs['initial'].pitch, 1505)
            self.assertEqual(self.bus._armed_ids, {2})

    def test_observation_is_bound_to_actual_move_and_does_not_rewrite_signs(self):
        self.session.moves.append({'id': 1, 'axis': 'pitch', 'from': 1505,
                                   'target': 1535, 'status': 'completed'})
        self.session.observe(ObservationRequest(move_id=1, direction='up', smoothness='smooth'))
        saved = json.loads((self.path/'out/session.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['moves'][0]['observation']['direction'], 'up')
        self.assertFalse(saved['direction_saved_to_runtime'])
        self.assertNotIn('direction', self.session.settings)


if __name__ == '__main__':
    unittest.main()
