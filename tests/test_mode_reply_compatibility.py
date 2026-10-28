"""Replay real MOD replies; reject echoes, wrong axes and unsupported modes."""
import unittest
from unittest.mock import patch

from locator_demo.head import AxisConfig
from locator_demo.servo_bus import ZPServoBus
from test_servo_bus import FakeSerial


class ModeReplyCompatibilityTests(unittest.TestCase):
    def bus(self,reply,sid=0,positions=(1375,1375)):
        serial=FakeSerial();serial.response_map[f'#{sid:03d}PMOD!']=reply
        serial.response_map[f'#{sid:03d}PRAD!']=f'#{sid:03d}P{positions[0]:04d}!'.encode()
        bus=ZPServoBus(serial_factory=lambda *a,**k:serial);bus._serial=serial
        self.addCleanup(bus.close)
        return bus,serial

    def test_actual_logged_reply_is_mode_one(self):
        bus,s=self.bus(b'#000MOD1!')
        self.assertEqual(bus.read_mode(0),1)
        self.assertEqual(bus.diagnostics()['transactions'][-1]['outcome'],'reply')
        self.assertEqual(s.writes,['#000PMOD!'])

    def test_original_pmod_variant_remains_supported(self):
        bus,_=self.bus(b'#000PMOD1!')
        self.assertEqual(bus.read_mode(0),1)

    def test_wrong_axis_echo_and_noise_do_not_hide_real_target_reply(self):
        bus,_=self.bus(b'#000PMOD!#002MOD5!noise#000MOD1!')
        self.assertEqual(bus.read_mode(0),1)

    def test_wrong_axis_alone_is_not_accepted(self):
        bus,_=self.bus(b'#002MOD1!')
        with patch('locator_demo.servo_bus.time.monotonic',side_effect=[1.,1.,2.,2.]):
            self.assertIsNone(bus.read_mode(0))

    def test_truncated_reply_and_query_echo_are_not_modes(self):
        for reply in (b'#000MOD1',b'#000PMOD!',b'#000MOD!',b'#000MODx!'):
            bus,_=self.bus(reply)
            with patch('locator_demo.servo_bus.time.monotonic',side_effect=[1.,1.,2.,2.]):
                self.assertIsNone(bus.read_mode(0))

    def test_motor_mode_is_detected_but_never_activated(self):
        bus,s=self.bus(b'#000MOD5!')
        with self.assertRaisesRegex(RuntimeError,'expected 1'):
            bus.activate_axis(AxisConfig(0,1500,800,2200))
        self.assertFalse(any('PULR' in text for text in s.writes))

    def test_actual_mode_one_can_complete_safe_activation_without_mode_write(self):
        bus,s=self.bus(b'#000MOD1!')
        with patch('locator_demo.servo_bus.time.sleep'):
            self.assertEqual(bus.activate_axis(AxisConfig(0,1500,800,2200),expected_position=1375),1375)
        self.assertEqual(s.writes,['#000PRAD!','#000PMOD!','#000PULR!','#000PRAD!'])
        self.assertFalse(any('PMOD1!' in text for text in s.writes))

    def test_pitch_axis_accepts_the_same_observed_variant(self):
        bus,_=self.bus(b'#002MOD1!',sid=2,positions=(1612,1612))
        self.assertEqual(bus.read_mode(2),1)


if __name__=='__main__':unittest.main()
