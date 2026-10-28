"""Adversarial startup, USB discovery and hotplug checks; fake transports only."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

from locator_demo.capture import WasapiAudioCapture
from locator_demo.head import AxisConfig,HeadPose
from locator_demo.servo_bus import ZPServoBus,TwoAxisHeadHardware,SerialPortCandidate,servo_port_candidates
from locator_demo.web.app import DemoRuntime,RuntimeConfig
from test_servo_bus import FakeSerial


class ActivationTests(unittest.TestCase):
    def fixture(self,positions=(1553,1553),mode=1):
        serial=FakeSerial();bus=ZPServoBus(serial_factory=lambda *a,**k:serial)
        bus._serial=serial;bus._session_generation=1
        bus.read_position=Mock(side_effect=positions);bus.read_mode=Mock(return_value=mode)
        self.addCleanup(bus.close)
        return bus,serial,AxisConfig(2,1553,1200,1800)

    def test_enable_never_rewrites_mode_or_sends_a_position(self):
        bus,s,axis=self.fixture()
        with patch('locator_demo.servo_bus.time.sleep'):
            self.assertEqual(bus.activate_axis(axis,expected_position=1553),1553)
        self.assertEqual(s.writes,['#002PULR!'])

    def test_missing_feedback_cannot_enable_torque(self):
        bus,s,axis=self.fixture((None,))
        with self.assertRaisesRegex(RuntimeError,'feedback'):bus.activate_axis(axis)
        self.assertEqual(s.writes,[])

    def test_878_out_of_range_cannot_enable_torque(self):
        bus,s,axis=self.fixture((878,))
        with self.assertRaisesRegex(RuntimeError,'limits'):bus.activate_axis(axis)
        self.assertEqual(s.writes,[])

    def test_unexpected_mode_is_not_silently_corrected(self):
        for mode in (None,2,3,7):
            bus,s,axis=self.fixture(mode=mode)
            with self.assertRaisesRegex(RuntimeError,'mode rewriting'):bus.activate_axis(axis)
            self.assertEqual(s.writes,[])

    def test_a_changed_position_before_activation_requires_resynchronization(self):
        bus,s,axis=self.fixture((1650,))
        with self.assertRaisesRegex(RuntimeError,'before activation'):bus.activate_axis(axis,expected_position=1553)
        self.assertEqual(s.writes,[])

    def test_torque_enable_without_post_feedback_cannot_launch_a_goal(self):
        bus,s,axis=self.fixture((1553,None))
        with patch('locator_demo.servo_bus.time.sleep'),self.assertRaisesRegex(RuntimeError,'feedback'):
            bus.activate_axis(axis)
        self.assertEqual(s.writes,['#002PULR!']);self.assertNotIn(2,bus._armed_ids)

    def test_a_downward_jump_after_enable_is_latched_before_any_goal(self):
        bus,s,axis=self.fixture((1553,878))
        with patch('locator_demo.servo_bus.time.sleep'),self.assertRaisesRegex(RuntimeError,'unexpectedly'):
            bus.activate_axis(axis)
        self.assertEqual(s.writes,['#002PULR!']);self.assertNotIn(2,bus._armed_ids)

    def test_yaw_only_preparation_does_not_activate_pitch(self):
        bus,s,axis=self.fixture();bus.activate_axis=Mock(return_value=1501)
        head=TwoAxisHeadHardware(bus,yaw=AxisConfig(0,1500,800,2200),pitch=axis)
        result=head.prepare_motion(HeadPose(1500,1553),HeadPose(1540,1553))
        self.assertEqual(result,HeadPose(1501,1553))
        self.assertEqual(bus.activate_axis.call_count,1)
        self.assertEqual(bus.activate_axis.call_args.args[0].servo_id,0)

    def test_verified_streaming_does_not_repeat_queries_or_enable(self):
        bus,s,axis=self.fixture();bus.activate_axis=Mock(return_value=1553)
        head=TwoAxisHeadHardware(bus,yaw=None,pitch=axis)
        head.prepare_motion(HeadPose(1500,1553),HeadPose(1500,1600))
        bus._armed_ids.add(2);s.writes.clear()
        for p in (1554,1555,1556):head.move_stream(HeadPose(1500,p),time_ms=40,current=HeadPose(1500,p-1))
        self.assertEqual(bus.activate_axis.call_count,1)
        self.assertEqual(s.writes,['#002P1554T0040!','#002P1555T0040!','#002P1556T0040!'])

    def test_mode_parser_ignores_other_id_and_command_echo(self):
        s=FakeSerial();s.response_map['#002PMOD!']=b'#002PMOD!#000PMOD3!#002PMOD1!'
        bus=ZPServoBus(serial_factory=lambda *a,**k:s);bus._serial=s
        self.assertEqual(bus.read_mode(2),1);self.assertEqual(s.writes,['#002PMOD!'])


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.r=DemoRuntime(RuntimeConfig(simulated=True,settings_path=Path(self.tmp.name)/'settings.json'))
        self.r.config.port='COM3';self.r.config.yaw_id=0;self.r.config.pitch_id=2
        self.r.bus=SimpleNamespace(read_position=Mock(side_effect=lambda sid:{0:1500,2:1553}[sid]),close=Mock())
        self.r.head=SimpleNamespace(stop=Mock(),cancel=Mock())
        self.addCleanup(self.r.shutdown)
        self.port=SerialPortCandidate('COM3','USB-SERIAL CH340','USB VID:PID=1A86:7523')

    def test_microphone_serial_port_and_unrelated_uart_are_not_candidates(self):
        ports=[self.port,SerialPortCandidate('COM5','YDPI4MIC','USB VID:PID=EF09:459D'),
               SerialPortCandidate('COM1','Communications Port','PNP0501')]
        self.assertEqual(servo_port_candidates(ports),[self.port])

    def test_current_port_requires_both_axis_replies_without_activation(self):
        with patch('locator_demo.web.app.servo_port_candidates',return_value=[self.port]):
            result=self.r.discover_servo_port()
        self.assertTrue(result['ok']);self.assertEqual(self.r.bus.read_position.call_count,2)
        self.assertFalse(self.r.running);self.assertFalse(self.r._motion_pose_synced)

    def test_partial_feedback_is_not_mislabeled_as_success(self):
        self.r.bus.read_position.side_effect=lambda sid:None if sid==0 else 1553
        with patch('locator_demo.web.app.servo_port_candidates',return_value=[self.port]):
            result=self.r.discover_servo_port()
        self.assertFalse(result['ok']);self.assertEqual(result['trials'][0]['positions'],{'0':None,'2':1553})

    def test_renumbered_com_port_is_confirmed_and_saved(self):
        port=SerialPortCandidate('COM8','USB-SERIAL CH340','USB VID:PID=1A86:7523')
        candidate=SimpleNamespace(read_position=Mock(side_effect=lambda sid:{0:1501,2:1554}[sid]),close=Mock())
        with patch('locator_demo.web.app.servo_port_candidates',return_value=[port]),patch('locator_demo.web.app.ZPServoBus',return_value=candidate),patch.object(self.r,'_attach_bus_diagnostics'),patch.object(self.r,'_make_head',return_value=SimpleNamespace(stop=Mock())):
            result=self.r.discover_servo_port()
        self.assertTrue(result['ok']);self.assertEqual(self.r.config.port,'COM8')
        self.assertIs(self.r.bus,candidate);candidate.close.assert_not_called()

    def test_ambiguous_matching_adapters_are_not_chosen_arbitrarily(self):
        ports=[SerialPortCandidate(p,'CH340','USB VID:PID=1A86:7523') for p in ('COM8','COM9')]
        opened=[]
        def make(_):
            b=SimpleNamespace(read_position=Mock(return_value=1500),close=Mock());opened.append(b);return b
        with patch('locator_demo.web.app.servo_port_candidates',return_value=ports),patch('locator_demo.web.app.ZPServoBus',side_effect=make):
            result=self.r.discover_servo_port()
        self.assertFalse(result['ok']);self.assertEqual(result['reason'],'multiple_matching_adapters')
        self.assertEqual(self.r.config.port,'COM3')
        for b in opened:b.close.assert_called_once()

    def test_discovery_cannot_replace_a_running_motion_session(self):
        self.r.running=True
        with self.assertRaisesRegex(RuntimeError,'Stop motion'):self.r.discover_servo_port()
        self.r.bus.read_position.assert_not_called()

    def test_startup_searches_after_renumbering_even_if_old_handle_claims_open(self):
        self.r.config.simulated=False;self.r.bus.connected=True
        new=SerialPortCandidate('COM8','CH340','USB VID:PID=1A86:7523')
        with patch('locator_demo.web.app.servo_port_candidates',return_value=[new]),patch.object(self.r,'discover_servo_port',return_value={'ok':True}) as discover,patch.object(self.r,'_make_head',return_value=SimpleNamespace(stop=Mock())):
            self.r.sync_motion_pose()
        discover.assert_called_once()


class AudioReplugTests(unittest.TestCase):
    def test_failed_open_and_failed_close_leave_no_stale_stream(self):
        stream=SimpleNamespace(start=Mock(side_effect=RuntimeError('removed during start')),
                               close=Mock(side_effect=RuntimeError('invalid endpoint')))
        module=SimpleNamespace(WasapiSettings=lambda **k:SimpleNamespace(_streaminfo=SimpleNamespace(streamOption=0)),
                               RawInputStream=Mock(return_value=stream))
        capture=WasapiAudioCapture(audio_module=module)
        with patch('locator_demo.capture.choose_wasapi_input',return_value={'index':17,'name':'YDPI4MIC'}):capture.start()
        self.assertIsNone(capture._stream);self.assertIn('removed during start',capture._error)

    def test_a_removed_stream_can_raise_on_active_and_close_then_be_reselected(self):
        class Removed:
            @property
            def active(self):raise RuntimeError('USB removed')
            def close(self):raise RuntimeError('invalid endpoint')
        stream=SimpleNamespace(start=Mock(),active=True)
        module=SimpleNamespace(WasapiSettings=lambda **k:SimpleNamespace(_streaminfo=SimpleNamespace(streamOption=0)),
                               RawInputStream=Mock(return_value=stream))
        capture=WasapiAudioCapture(audio_module=module);capture._stream=Removed()
        with patch('locator_demo.capture.choose_wasapi_input',return_value={'index':17,'name':'YDPI4MIC'}):capture.start()
        stream.start.assert_called_once();self.assertIs(capture._stream,stream)
        self.assertEqual(module.RawInputStream.call_args.kwargs['device'],17)
        self.assertEqual(module.RawInputStream.call_args.kwargs['channels'],4)
        self.assertEqual(module.RawInputStream.call_args.kwargs['samplerate'],48000)


if __name__=='__main__':unittest.main()
