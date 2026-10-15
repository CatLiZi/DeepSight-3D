import asyncio
import unittest
from unittest.mock import patch

from locator_demo.web.app import create_app


class AppShutdownTests(unittest.TestCase):
    def test_shutdown_event_stops_runtime_components(self):
        app = create_app(simulated=True)
        self.assertGreater(len(app.router.on_shutdown), 0)

        runtime = app.state.runtime
        with patch.object(runtime, "shutdown", wraps=runtime.shutdown) as shutdown_mock:
            for handler in list(app.router.on_shutdown):
                if asyncio.iscoroutinefunction(handler):
                    asyncio.run(handler())
                else:
                    handler()

            shutdown_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
