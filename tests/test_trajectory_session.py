"""A smooth trajectory must not cross serial sessions; fake handles only."""
from dataclasses import asdict
import threading
import time
import unittest
from unittest.mock import Mock, patch

from locator_demo.head import AxisConfig, HeadPose
from locator_demo.motion import SmoothHeadHardware
from locator_demo.servo_bus import TwoAxisHeadHardware, ZPServoBus
from locator_demo.settings import ControlConfig
from test_servo_bus import FakeSerial


class TrajectorySessionTests(unittest.TestCase):
    def setUp(self):
        ports=patch('locator_demo.servo_bus.list_serial_ports', return_value=[])
        sleep=patch('locator_demo.servo_bus.time.sleep')
        ports.start();sleep.start()
        self.addCleanup(ports.stop);self.addCleanup(sleep.stop)

    def fixture(self, *, autostart=False):
        old,new=FakeSerial(),FakeSerial()
        for serial in (old,new):
            serial.response_map={'#000PRAD!':b'#000P1500!','#002PRAD!':b'#002P1553!',
                '#000PMOD!':b'#000PMOD1!','#002PMOD!':b'#002PMOD1!'}
        factory=Mock(side_effect=[old,new])
        bus=ZPServoBus(serial_factory=factory)
        self.addCleanup(bus.close)
        self.assertEqual(bus.read_position(0),1500)
        self.assertEqual(bus.read_position(2),1553)
        bus.arm(0);bus.arm(2)
        head=TwoAxisHeadHardware(bus,yaw=AxisConfig(0,1500,800,2200),pitch=AxisConfig(2,1553,1200,1800))
        head._verified_axes={0:bus.motion_session(),2:bus.motion_session()}
        now=[0.]
        motion=SmoothHeadHardware(head,initial=HeadPose(1500,1553),config=ControlConfig(),
                                  clock=lambda:now[0],autostart=autostart)
        self.addCleanup(motion.stop)
        old.writes.clear()
        return bus,head,motion,now,old,new,factory

    def start(self, motion, now):
        motion.move_to(HeadPose(1700,1600),time_ms=100)
        now[0]=.2;motion.step()

    def test_closed_handle_stops_before_implicit_reopen_or_activation(self):
        bus,head,motion,now,old,new,factory=self.fixture()
        self.start(motion,now);previous=motion._last_sent
        old.is_open=False;now[0]=.4
        with self.assertRaisesRegex(RuntimeError,'fresh position'):
            motion.step()
        self.assertEqual(factory.call_count,1)
        self.assertEqual(new.writes,[])
        self.assertFalse(motion.is_moving)
        self.assertEqual(motion.estimated_pose,previous)
        self.assertEqual(motion.metrics()['goal_pose'],asdict(previous))

    def test_readonly_reopen_cannot_authorize_an_old_trajectory(self):
        bus,head,motion,now,old,new,factory=self.fixture()
        self.start(motion,now)
        old.is_open=False
        self.assertEqual(bus.read_position(0),1500)
        before=list(new.writes);self.assertEqual(before,['#000PRAD!'])
        now[0]=.4
        with self.assertRaisesRegex(RuntimeError,'fresh position'):
            motion.step()
        self.assertEqual(new.writes,before)

    def test_explicit_close_and_reopen_also_invalidates_old_trajectory(self):
        bus,head,motion,now,old,new,factory=self.fixture()
        self.start(motion,now);bus.close();bus.connect();now[0]=.4
        with self.assertRaisesRegex(RuntimeError,'fresh position'):
            motion.step()
        self.assertEqual(new.writes,[])

    def test_reused_python_handle_still_counts_as_a_new_serial_session(self):
        bus,head,motion,now,old,new,factory=self.fixture()
        self.start(motion,now);bus.close()

        def reuse(*args,**kwargs):
            old.is_open=True
            return old

        factory.side_effect=reuse;bus.connect();before=list(old.writes);now[0]=.4
        with self.assertRaisesRegex(RuntimeError,'fresh position'):
            motion.step()
        self.assertEqual(old.writes,before)

    def test_disconnected_before_preparation_cannot_start_a_worker_trajectory(self):
        bus,head,motion,now,old,new,factory=self.fixture(autostart=True)
        old.is_open=False
        with patch.object(head,'prepare',wraps=head.prepare) as prepare:
            motion.move_to(HeadPose(1700,1600),time_ms=100)
            motion._worker.join(.3)
            motion.stop()
            prepare.assert_not_called()
        self.assertIn('fresh position',motion.metrics()['error'])
        self.assertEqual(factory.call_count,1)
        self.assertEqual(new.writes,[])

    def test_disconnect_during_preparation_does_not_implicitly_open_a_new_handle(self):
        bus,head,motion,now,old,new,factory=self.fixture(autostart=True)
        original=head.prepare_motion

        def prepare(*args):
            old.is_open=False
            original(*args)

        with patch.object(head,'prepare_motion',side_effect=prepare):
            motion.move_to(HeadPose(1700,1600),time_ms=100)
            motion._worker.join(.3);motion.stop()
        self.assertIn('fresh position',motion.metrics()['error'])
        self.assertEqual(factory.call_count,1)
        self.assertEqual(new.writes,[])

    def test_cold_unbound_adapter_requires_explicit_position_synchronization(self):
        serial=FakeSerial();factory=Mock(return_value=serial)
        bus=ZPServoBus(serial_factory=factory);self.addCleanup(bus.close)
        head=TwoAxisHeadHardware(bus,yaw=AxisConfig(0),pitch=AxisConfig(2))
        motion=SmoothHeadHardware(head,initial=HeadPose(1500,1500),config=ControlConfig(),autostart=False)
        self.addCleanup(motion.stop)
        motion.move_to(HeadPose(1550,1550),time_ms=100)
        with self.assertRaisesRegex(RuntimeError,'fresh position'):
            motion.step(motion.planner.started+.5)
        factory.assert_not_called();self.assertEqual(serial.writes,[])

    def test_stream_write_failure_is_latched_even_in_manual_step_mode(self):
        bus,head,motion,now,old,new,factory=self.fixture()
        self.start(motion,now);previous=motion._last_sent
        old.write=Mock(side_effect=IOError('write disconnected'));now[0]=.4
        with self.assertRaisesRegex(IOError,'disconnected'):
            motion.step()
        self.assertFalse(motion.is_moving)
        self.assertEqual(motion.estimated_pose,previous)
        with self.assertRaisesRegex(RuntimeError,'fresh position'):
            motion.move_to(HeadPose(1600,1580),time_ms=100)

    def test_fresh_reading_does_not_unlock_the_old_faulted_adapter(self):
        bus,head,motion,now,old,new,factory=self.fixture()
        self.start(motion,now);old.is_open=False;now[0]=.4
        with self.assertRaises(RuntimeError):motion.step()
        self.assertEqual(bus.read_position(0),1500)
        self.assertEqual(bus.read_position(2),1553)
        before=list(new.writes)
        with self.assertRaisesRegex(RuntimeError,'fresh position'):
            motion.move_to(HeadPose(1600,1580),time_ms=100)
        self.assertEqual(new.writes,before)
        fresh=SmoothHeadHardware(head,initial=HeadPose(1500,1553),config=ControlConfig(),
                                 clock=lambda:now[0],autostart=False)
        self.addCleanup(fresh.stop)
        fresh.move_to(HeadPose(1520,1560),time_ms=100)
        now[0]=.9;fresh.step()
        self.assertTrue(new.writes[len(before):])
        self.assertEqual(fresh.metrics()['goal_pose'],{'yaw':1520,'pitch':1560})
        self.assertNotIn(1700,[fresh.planner.target.yaw])

    def test_normal_stream_does_not_query_feedback_or_repeat_activation(self):
        bus,head,motion,now,old,new,factory=self.fixture()
        motion.move_to(HeadPose(1700,1600),time_ms=100)
        for n in range(1,151):now[0]=n*.02;motion.step()
        self.assertTrue(old.writes)
        self.assertTrue(all('T0040!' in command for command in old.writes))
        self.assertFalse(any('PRAD!' in command or 'PULR!' in command for command in old.writes))
        self.assertEqual(motion.estimated_pose,HeadPose(1700,1600))
        self.assertEqual(factory.call_count,1)

    def test_binding_snapshot_does_not_wait_for_an_unrelated_io_transaction(self):
        bus,head,motion,now,old,new,factory=self.fixture()
        busy,release=threading.Event(),threading.Event()

        def transaction():
            with bus._io_lock:
                busy.set();release.wait(1)

        worker=threading.Thread(target=transaction);worker.start()
        try:
            self.assertTrue(busy.wait(.5));start=time.perf_counter()
            fresh=SmoothHeadHardware(head,initial=HeadPose(1500,1553),config=ControlConfig(),autostart=False)
            self.assertLess(time.perf_counter()-start,.15)
            fresh.stop()
        finally:
            release.set();worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_connection_replacement_cannot_interleave_with_a_checked_frame(self):
        bus,head,motion,now,old,new,factory=self.fixture()
        self.start(motion,now)
        entered,release,replacing,replaced=(threading.Event() for _ in range(4))
        errors=[];original=head.move_stream

        def sender(*args,**kwargs):
            entered.set()
            if not release.wait(1):raise RuntimeError('test frame barrier timeout')
            return original(*args,**kwargs)

        def step():
            try:motion.step(.4)
            except Exception as exc:errors.append(str(exc))

        def replace_connection():
            replacing.set();bus.close();bus.connect();replaced.set()

        with patch.object(head,'move_stream',side_effect=sender):
            writer=threading.Thread(target=step);writer.start()
            self.assertTrue(entered.wait(.5))
            opener=threading.Thread(target=replace_connection);opener.start()
            try:
                self.assertTrue(replacing.wait(.5))
                self.assertFalse(replaced.wait(.05))
            finally:
                release.set();writer.join(1);opener.join(1)
        self.assertFalse(writer.is_alive() or opener.is_alive())
        self.assertEqual(errors,[])
        self.assertTrue(replaced.is_set())
        self.assertEqual(new.writes,[])
        with self.assertRaisesRegex(RuntimeError,'fresh position'):
            motion.step(.6)


if __name__=='__main__':unittest.main()
