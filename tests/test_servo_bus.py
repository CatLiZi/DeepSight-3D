import unittest

from locator_demo.head import AxisConfig, HeadPose
from locator_demo.servo_bus import SerialPortCandidate, ZPServoBus, TwoAxisHeadHardware, choose_servo_port


class FakeSerial:
    def __init__(self):
        self.is_open = True
        self.writes = []
        self.responses = []
        self.response_map = {}

    def write(self, data):
        command = data.decode("ascii")
        self.writes.append(command)
        if command in self.response_map:
            self.responses.append(self.response_map[command])

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass

    def read(self, size=1):
        if not self.responses:
            return b""
        response = self.responses[0]
        if not response:
            self.responses.pop(0)
            return b""
        chunk = response[:size]
        self.responses[0] = response[size:]
        return chunk

    def close(self):
        self.is_open = False


class ServoBusTests(unittest.TestCase):
    def test_reads_position_and_sets_mode_before_move(self):
        fake = FakeSerial()
        fake.responses = [b"#002P1500!", b"#OK!"]
        bus = ZPServoBus(serial_factory=lambda *_args, **_kwargs: fake)

        self.assertEqual(bus.read_position(2), 1500)
        bus.move(2, 1400, 3000)

        self.assertEqual(fake.writes, ["#002PRAD!", "#002PULR!", "#002PMOD1!", "#002P1400T3000!"])

    def test_scans_ids_using_pid(self):
        fake = FakeSerial()
        fake.response_map = {"#002PID!": b"#002P!"}
        bus = ZPServoBus(serial_factory=lambda *_args, **_kwargs: fake)

        self.assertEqual(bus.scan_ids([1, 2, 3]), [2])

    def test_two_axis_hardware_skips_unknown_yaw(self):
        fake = FakeSerial()
        fake.responses = [b"#OK!"]
        bus = ZPServoBus(serial_factory=lambda *_args, **_kwargs: fake)
        head = TwoAxisHeadHardware(
            bus,
            yaw=None,
            pitch=AxisConfig(servo_id=2, minimum=1200, maximum=1800),
        )

        head.move_to(HeadPose(yaw=1700, pitch=1400), time_ms=1000)

        self.assertEqual(fake.writes, ["#002PULR!", "#002PMOD1!", "#002P1400T1000!"])

    def test_two_axis_hardware_skips_axes_that_did_not_change(self):
        fake = FakeSerial()
        fake.responses = [b"#OK!"]
        bus = ZPServoBus(serial_factory=lambda *_args, **_kwargs: fake)
        head = TwoAxisHeadHardware(
            bus,
            yaw=AxisConfig(servo_id=0, minimum=1200, maximum=1800),
            pitch=AxisConfig(servo_id=2, minimum=1200, maximum=1800),
        )

        head.move_to(HeadPose(yaw=1510, pitch=1500), time_ms=500, current=HeadPose(yaw=1500, pitch=1500))

        self.assertEqual(fake.writes, ["#000PULR!", "#000PMOD1!", "#000P1510T0500!"])

    def test_servo_bus_arms_each_servo_once_for_repeated_moves(self):
        fake = FakeSerial()
        bus = ZPServoBus(serial_factory=lambda *_args, **_kwargs: fake)

        bus.move(2, 1510, 500)
        bus.move(2, 1520, 500)

        self.assertEqual(fake.writes, ["#002PULR!", "#002PMOD1!", "#002P1510T0500!", "#002P1520T0500!"])

    def test_two_axis_hardware_uses_cached_pitch_arm_by_default(self):
        fake = FakeSerial()
        bus = ZPServoBus(serial_factory=lambda *_args, **_kwargs: fake)
        head = TwoAxisHeadHardware(
            bus,
            yaw=AxisConfig(servo_id=0, minimum=1200, maximum=1800),
            pitch=AxisConfig(servo_id=2, minimum=1200, maximum=1800),
        )

        head.move_to(HeadPose(yaw=1500, pitch=1450), time_ms=500, current=HeadPose(yaw=1500, pitch=1500))
        head.move_to(HeadPose(yaw=1500, pitch=1550), time_ms=500, current=HeadPose(yaw=1500, pitch=1450))

        self.assertEqual(
            fake.writes,
            [
                "#002PULR!",
                "#002PMOD1!",
                "#002P1450T0500!",
                "#002P1550T0500!",
            ],
        )

    def test_two_axis_hardware_can_force_rearm_pitch_for_repeated_moves(self):
        fake = FakeSerial()
        bus = ZPServoBus(serial_factory=lambda *_args, **_kwargs: fake)
        head = TwoAxisHeadHardware(
            bus,
            yaw=AxisConfig(servo_id=0, minimum=1200, maximum=1800),
            pitch=AxisConfig(servo_id=2, minimum=1200, maximum=1800),
            force_arm_pitch=True,
        )

        head.move_to(HeadPose(yaw=1500, pitch=1450), time_ms=500, current=HeadPose(yaw=1500, pitch=1500))
        head.move_to(HeadPose(yaw=1500, pitch=1550), time_ms=500, current=HeadPose(yaw=1500, pitch=1450))

        self.assertEqual(
            fake.writes,
            [
                "#002PULR!",
                "#002PMOD1!",
                "#002P1450T0500!",
                "#002PULR!",
                "#002PMOD1!",
                "#002P1550T0500!",
            ],
        )

    def test_choose_servo_port_prefers_ch340(self):
        import locator_demo.servo_bus as servo_bus

        original = servo_bus.list_serial_ports
        servo_bus.list_serial_ports = lambda: [
            SerialPortCandidate("COM8", "Bluetooth", ""),
            SerialPortCandidate("COM15", "USB-SERIAL CH340", "USB VID:PID=1A86:7523"),
        ]
        try:
            self.assertEqual(choose_servo_port("COM14"), "COM15")
        finally:
            servo_bus.list_serial_ports = original


if __name__ == "__main__":
    unittest.main()
