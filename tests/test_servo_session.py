"""Servo enable-state belongs to one serial handle; fake transport only."""
import threading
import unittest
from unittest.mock import Mock, patch

from locator_demo.servo_bus import ZPServoBus
from test_servo_bus import FakeSerial


class ServoSessionTests(unittest.TestCase):
    def setUp(self):
        ports = patch('locator_demo.servo_bus.list_serial_ports', return_value=[])
        sleep = patch('locator_demo.servo_bus.time.sleep')
        ports.start(); sleep.start()
        self.addCleanup(ports.stop); self.addCleanup(sleep.stop)

    def reopened_bus(self):
        old, fresh = FakeSerial(), FakeSerial()
        old.is_open = False
        factory = Mock(return_value=fresh)
        bus = ZPServoBus(serial_factory=factory)
        bus._serial, bus._armed_ids = old, {0, 2}
        self.addCleanup(bus.close)
        return bus, fresh, factory

    def test_implicit_reopen_does_not_skip_activation_for_a_cached_axis(self):
        bus, serial, _ = self.reopened_bus()
        bus.move(2, 1560, 300)
        self.assertEqual(serial.writes, ['#002PULR!', '#002P1560T0300!'])

    def test_first_stream_command_arms_both_axes_in_the_new_session(self):
        bus, serial, _ = self.reopened_bus()
        bus.move_many([(0, 1510), (2, 1560)], 40)
        self.assertEqual(serial.writes, ['#000PULR!', '#002PULR!', '#000P1510T0040!#002P1560T0040!'])

    def test_direct_prepare_checks_the_connection_before_trusting_cached_state(self):
        bus, serial, factory = self.reopened_bus()
        bus.arm(0)
        factory.assert_called_once()
        self.assertEqual(serial.writes, ['#000PULR!'])
        self.assertEqual(bus._armed_ids, {0})

    def test_readonly_reopen_clears_old_state_without_activating_either_axis(self):
        bus, serial, _ = self.reopened_bus()
        serial.response_map['#002PRAD!'] = b'#002P1553!'
        self.assertEqual(bus.read_position(2), 1553)
        self.assertEqual(serial.writes, ['#002PRAD!'])
        self.assertEqual(bus._armed_ids, set())
        bus.move(2, 1560, 300)
        self.assertEqual(serial.writes[1:], ['#002PULR!', '#002P1560T0300!'])

    def test_failed_open_does_not_leave_old_activation_evidence(self):
        bus, _, factory = self.reopened_bus()
        factory.side_effect = IOError('device removed')
        with self.assertRaisesRegex(IOError, 'removed'):
            bus.connect()
        self.assertEqual(bus._armed_ids, set())

    def test_normal_connected_commands_keep_the_single_activation_cache(self):
        bus, serial, factory = self.reopened_bus()
        bus.connect()
        bus.move_many([(0, 1510), (2, 1560)], 40)
        bus.move_many([(0, 1520), (2, 1570)], 40)
        self.assertEqual(serial.writes.count('#000PULR!'), 1)
        self.assertEqual(serial.writes.count('#002PULR!'), 1)
        self.assertEqual(serial.writes[-1], '#000P1520T0040!#002P1570T0040!')
        factory.assert_called_once()

    def test_explicit_force_activation_is_still_honoured(self):
        bus, serial, _ = self.reopened_bus()
        bus.connect()
        bus.move(2, 1560, 300)
        bus.move(2, 1570, 300, force_arm=True)
        self.assertEqual(serial.writes.count('#002PULR!'), 2)
        self.assertEqual(serial.writes.count('#002PMOD1!'), 0)

    def test_repeated_disconnects_each_require_new_activation(self):
        first, second, third = FakeSerial(), FakeSerial(), FakeSerial()
        factory = Mock(side_effect=[first, second, third])
        bus = ZPServoBus(serial_factory=factory)
        self.addCleanup(bus.close)
        for serial, position in ((first, 1510), (second, 1520), (third, 1530)):
            bus.move(0, position, 100)
            self.assertEqual(serial.writes[:1], ['#000PULR!'])
            serial.is_open = False
        self.assertEqual(factory.call_count, 3)

    def test_explicit_close_still_discards_activation_cache(self):
        serial = FakeSerial()
        bus = ZPServoBus(serial_factory=lambda *a, **k: serial)
        bus.move(0, 1510, 100)
        bus.close()
        self.assertEqual(bus._armed_ids, set())
        self.assertFalse(bus.connected)

    def test_concurrent_direct_activation_is_one_atomic_operation(self):
        serial = FakeSerial()
        bus = ZPServoBus(serial_factory=lambda *a, **k: serial)
        self.addCleanup(bus.close)
        entered, release, second_started, second_finished = (threading.Event() for _ in range(4))
        calls, errors = [], []
        original_restore = bus.restore_torque

        def restore(servo_id):
            calls.append(servo_id)
            if len(calls) == 1:
                entered.set()
                if not release.wait(1):
                    raise RuntimeError('test synchronization timed out')
            original_restore(servo_id)

        def run(second=False):
            try:
                if second: second_started.set()
                bus.arm(2)
            except Exception as exc:
                errors.append(str(exc))
            finally:
                if second: second_finished.set()

        with patch.object(bus, 'restore_torque', side_effect=restore):
            first = threading.Thread(target=run)
            second = threading.Thread(target=run, args=(True,))
            first.start()
            self.assertTrue(entered.wait(.5))
            second.start()
            self.assertTrue(second_started.wait(.5))
            second_finished.wait(.05)
            release.set()
            first.join(1); second.join(1)
            self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(calls, [2])
        self.assertEqual(serial.writes, ['#002PULR!'])


if __name__ == '__main__':
    unittest.main()
