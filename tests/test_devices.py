import unittest

from locator_demo.devices import parse_dshow_devices


class DirectShowDeviceTests(unittest.TestCase):
    def test_parse_devices_and_alternative_names(self):
        text = """
[dshow @ 000] "Integrated Camera" (video)
[dshow @ 000]   Alternative name "@device_pnp_video"
[dshow @ 000] "麦克风 (Realtek(R) Audio)" (audio)
[dshow @ 000]   Alternative name "@device_cm_audio"
"""

        devices = parse_dshow_devices(text)

        self.assertEqual(devices[0].name, "Integrated Camera")
        self.assertEqual(devices[0].kind, "video")
        self.assertEqual(devices[0].alternative_name, "@device_pnp_video")
        self.assertEqual(devices[1].kind, "audio")


if __name__ == "__main__":
    unittest.main()
