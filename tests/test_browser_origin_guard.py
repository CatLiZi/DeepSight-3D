"""Local API/browser origin checks; every actuator here is simulated."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from locator_demo.web.app import create_app


class BrowserOriginGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app=create_app(simulated=True,settings_path=Path(self.temp.name)/'settings.json')
        self.runtime=self.app.state.runtime;self.addCleanup(self.runtime.shutdown)
        self.runtime.features=replace(self.runtime.features,audio_enabled=False,visual_enabled=False,camera_enabled=False)

    def test_cross_origin_form_cannot_start_automatic_control(self):
        with TestClient(self.app) as client:
            r=client.post('/api/demo/start',headers={'Origin':'https://outside.example'},data='')
        self.assertEqual(r.status_code,403);self.assertFalse(self.runtime.running)

    def test_cross_origin_json_cannot_move_the_head(self):
        self.runtime.jog=Mock()
        with TestClient(self.app) as client:
            r=client.post('/api/head/jog',headers={'Origin':'https://outside.example'},
                          json={'direction':'left','amount':20,'time_ms':300})
        self.assertEqual(r.status_code,403);self.runtime.jog.assert_not_called()

    def test_same_origin_and_originless_local_cli_are_supported(self):
        with TestClient(self.app) as client:
            for headers in ({'Origin':'http://testserver'},{}):
                with self.subTest(headers=headers):
                    self.assertEqual(client.post('/api/demo/start',headers=headers).status_code,200)
                    self.assertTrue(self.runtime.running)
                    self.assertEqual(client.post('/api/demo/stop',headers=headers).status_code,200)

    def test_foreign_host_cannot_become_trusted_by_matching_origin(self):
        with TestClient(self.app) as client:
            r=client.post('/api/demo/start',headers={'Host':'outside.example','Origin':'http://outside.example'})
        self.assertEqual(r.status_code,403);self.assertFalse(self.runtime.running)

    def test_null_invalid_and_mismatched_origins_are_rejected(self):
        with TestClient(self.app) as client:
            for origin in ('null','https://testserver','http://testserver:1234','http://testserver.evil.example','http://testserver/path'):
                with self.subTest(origin=origin):
                    self.assertEqual(client.post('/api/demo/start',headers={'Origin':origin}).status_code,403)

    def test_same_origin_websocket_receives_state(self):
        with TestClient(self.app) as client:
            with client.websocket_connect('/ws',headers={'Origin':'http://testserver'}) as socket:
                self.assertEqual(socket.receive_json()['algorithm_revision'],'R53')

    def test_cross_origin_websocket_is_closed_without_state(self):
        with TestClient(self.app) as client:
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect('/ws',headers={'Origin':'https://outside.example'}) as socket:
                    socket.receive_json()

    def test_loopback_host_and_matching_port_are_accepted(self):
        with TestClient(self.app,base_url='http://127.0.0.1:8010') as client:
            response=client.post('/api/demo/start',headers={'Origin':'http://127.0.0.1:8010'})
            self.assertEqual(response.status_code,200)

    def test_ipv6_loopback_and_matching_origin_are_accepted(self):
        with TestClient(self.app,base_url='http://[::1]:8010') as client:
            response=client.post('/api/demo/start',headers={'Origin':'http://[::1]:8010'})
            self.assertEqual(response.status_code,200)

    def test_cross_origin_cannot_read_live_state(self):
        with TestClient(self.app) as client:
            response=client.get('/api/state',headers={'Origin':'https://outside.example'})
            self.assertEqual(response.status_code,403)
            self.assertNotIn('visual',response.json())


if __name__=='__main__':unittest.main()
