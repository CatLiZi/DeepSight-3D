"""Wide yaw range across settings, controllers, wire commands and web UI; fake motion only."""
from dataclasses import asdict, replace
from pathlib import Path
import json
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from fastapi.testclient import TestClient

from locator_demo.head import AxisConfig, DirectionConfig, HeadControllerLogic, HeadPose
from locator_demo.motion import SmoothHeadHardware, SmoothMotionPlanner
from locator_demo.servo_bus import TwoAxisHeadHardware, ZPServoBus
from locator_demo.settings import AxisLimitsConfig, SavedSettings, load_settings, save_settings, ControlConfig, validate_axis_limits
from locator_demo.web.app import AxisLimitsConfigRequest, DemoRuntime, RuntimeConfig, create_app
from test_servo_bus import FakeSerial
from test_stage1_runtime import FakeAudio, FakeVision

PROJECT = Path(__file__).resolve().parents[1]


class YawRangeTests(unittest.TestCase):
    def test_default_axes_and_request_agree_and_pitch_stays_narrow(self):
        for limits in (AxisLimitsConfig(), AxisLimitsConfigRequest()):
            self.assertEqual((limits.yaw_min,limits.yaw_center,limits.yaw_max),(800,1500,2200))
            self.assertEqual((limits.pitch_min,limits.pitch_max),(1200,1800))
        logic=HeadControllerLogic()
        self.assertEqual(logic.clamp_pose(HeadPose(700,1900)),HeadPose(800,1800))
        self.assertEqual(logic.clamp_pose(HeadPose(2300,1100)),HeadPose(2200,1200))

    def test_legacy_file_without_limits_gets_wide_yaw_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'settings.json';path.write_text('{}')
            limits=load_settings(path).axis_limits
            self.assertEqual((limits.yaw_min,limits.yaw_max),(800,2200))

    def test_local_and_example_profiles_keep_separate_axis_ranges(self):
        # The example has generic values; the installed assembly has its own
        # calibrated centre. A valid operator calibration must not fail tests
        # simply because it differs from the 2026-10-26 assembly.
        example=load_settings(PROJECT/'locator_demo_settings.example.json').axis_limits
        self.assertEqual((example.yaw_min,example.yaw_center,example.yaw_max),(800,1500,2200))
        self.assertEqual((example.pitch_min,example.pitch_max),(1200,1800))
        path=PROJECT/'locator_demo_settings.json'
        if not path.is_file(): path=PROJECT/'configs/panda-final-r43.json'
        saved=json.loads(path.read_text(encoding='utf-8'))['axis_limits']
        validate_axis_limits(saved)
        self.assertEqual(asdict(load_settings(path).axis_limits),saved)

    def test_custom_explicit_narrow_limits_are_still_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'settings.json'
            limits=AxisLimitsConfig(yaw_min=1000,yaw_center=1450,yaw_max=2050,pitch_center=1553)
            save_settings(path,SavedSettings(axis_limits=limits))
            self.assertEqual(load_settings(path).axis_limits,limits)

    def test_manual_and_visual_steps_can_pass_the_old_yaw_edges(self):
        logic=HeadControllerLogic()
        self.assertEqual(logic.jog(HeadPose(1200,1553),'left',60).yaw,1140)
        self.assertEqual(logic.jog(HeadPose(1800,1553),'right',60).yaw,1860)
        self.assertEqual(logic.apply_visual_error(HeadPose(2100,1553),.5,0).yaw,2200)
        self.assertEqual(logic.apply_visual_error(HeadPose(900,1553),-.5,0).yaw,800)
        self.assertEqual(logic.jog(HeadPose(820,1553),'left',60),HeadPose(800,1553))
        self.assertEqual(logic.jog(HeadPose(2180,1553),'right',60),HeadPose(2200,1553))

    def test_presets_reach_full_range_and_follow_axis_sign(self):
        for sign,edges in ((-1,(800,2200)),(1,(2200,800))):
            logic=HeadControllerLogic(direction=DirectionConfig(yaw_left_sign=sign))
            self.assertEqual(logic.pose_for_audio_direction('left_back').yaw,edges[0])
            self.assertEqual(logic.pose_for_audio_direction('right_back').yaw,edges[1])
            self.assertEqual(logic.pose_for_audio_direction('center').yaw,1500)
        self.assertEqual(HeadControllerLogic().pose_for_audio_direction('left_front').yaw,1150)

    def test_asymmetric_presets_use_the_correct_side_and_pitch_center(self):
        logic=HeadControllerLogic(yaw=AxisConfig(0,1400,800,2200),pitch=AxisConfig(2,1553))
        self.assertEqual(logic.pose_for_audio_direction('left_front'),HeadPose(1100,1553))
        self.assertEqual(logic.pose_for_audio_direction('right_front'),HeadPose(1800,1553))

    def test_full_width_trajectory_keeps_velocity_acceleration_and_position_bounds(self):
        planner=SmoothMotionPlanner(HeadPose(800,1553))
        self.assertTrue(planner.retarget(HeadPose(2200,1553),0))
        samples=np.array([planner.sample(t) for t in np.linspace(0,planner.duration,1001)])
        self.assertGreaterEqual(samples[:,0,0].min(),800-1e-6)
        self.assertLessEqual(samples[:,0,0].max(),2200+1e-6)
        self.assertLessEqual(abs(samples[:,:,1]).max(),180.0001)
        self.assertLessEqual(abs(samples[:,:,2]).max(),600.0001)
        np.testing.assert_allclose(samples[-1,:,0],[2200,1553],atol=1e-6)

    def test_actual_wire_commands_use_wide_yaw_and_narrow_pitch(self):
        serial=FakeSerial();bus=ZPServoBus(serial_factory=lambda *a,**k:serial);bus.arm=Mock()
        bus.activate_axis=Mock(side_effect=lambda axis,**kw: kw.get('expected_position') or axis.center)
        head=TwoAxisHeadHardware(bus,yaw=AxisConfig(0,1500,800,2200),pitch=AxisConfig(2,1553,1200,1800))
        with patch('locator_demo.servo_bus.list_serial_ports',return_value=[]):
            head.move_to(HeadPose(799,1801),time_ms=900)
            head.move_stream(HeadPose(2201,1199),time_ms=40,current=HeadPose(800,1800))
        self.assertIn('#000P0800T0900!',serial.writes)
        self.assertIn('#002P1800T0900!',serial.writes)
        self.assertIn('#000P2200T0040!#002P1200T0040!',serial.writes)
        bus.close()

    def test_smooth_adapter_emits_beyond_old_limits_without_exceeding_new_ones(self):
        commands=[];now=[0.]
        head=SimpleNamespace(yaw=AxisConfig(0,1500,800,2200),pitch=AxisConfig(2,1553),
            move_to=lambda pose,**kwargs:commands.append(pose))
        smooth=SmoothHeadHardware(head,initial=HeadPose(1500,1553),config=ControlConfig(),clock=lambda:now[0],autostart=False)
        smooth.move_to(HeadPose(2200,1553),time_ms=100)
        for t in np.arange(.02,15,.02):now[0]=t;smooth.step()
        self.assertEqual(commands[-1],HeadPose(2200,1553))
        self.assertTrue(any(p.yaw>1800 for p in commands))
        self.assertTrue(all(800<=p.yaw<=2200 and p.pitch==1553 for p in commands))


class YawRuntimeTests(unittest.TestCase):
    def runtime(self):
        with patch('locator_demo.web.app.WasapiArrayAudio',FakeAudio),patch('locator_demo.web.app.StereoVisionTracker',FakeVision),patch('locator_demo.web.app.choose_servo_port',return_value=None):
            r=DemoRuntime(RuntimeConfig())
        r.config.yaw_id=0;r.config.pitch_id=2
        r.logic=r._make_logic();r.head=r._make_head()
        self.addCleanup(r.shutdown)
        return r

    def test_startup_reads_wide_yaw_positions_without_moving(self):
        for yaw in (800,900,2100,2200):
            r=self.runtime()
            with patch.object(r.bus,'read_position',side_effect=[yaw,1553]),patch.object(r.bus,'_write') as write:
                r.sync_motion_pose()
                self.assertEqual(r.pose,HeadPose(yaw,1553));write.assert_not_called()

    def test_startup_rejects_beyond_wide_yaw_or_narrow_pitch(self):
        for yaw,pitch in ((799,1553),(2201,1553),(1500,1801)):
            r=self.runtime()
            with patch.object(r.bus,'read_position',side_effect=[yaw,pitch]),patch.object(r.bus,'_write') as write:
                with self.assertRaisesRegex(RuntimeError,'limits'):r.sync_motion_pose()
                write.assert_not_called()

    def test_live_range_change_pauses_motion_and_requires_new_feedback(self):
        r=self.runtime();r.running=True;r._motion_pose_synced=True
        with patch.object(r.bus,'_write') as write:
            session=r.update_axis_limits_config(AxisLimitsConfigRequest(pitch_center=1553))
        self.assertFalse(r.running);self.assertFalse(r._motion_pose_synced);write.assert_not_called()
        self.assertEqual(session['head_presets']['left_back'],{'yaw':800,'pitch':1553})

    def test_api_persists_wide_limits_and_exposes_shared_presets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'settings.json';app=create_app(simulated=True,settings_path=path)
            r=app.state.runtime;self.addCleanup(r.shutdown)
            with TestClient(app) as client:
                response=client.post('/api/config/axis-limits',json={'yaw_min':800,'yaw_center':1500,'yaw_max':2200,
                    'pitch_min':1200,'pitch_center':1553,'pitch_max':1800})
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json()['head_presets']['right_back'],{'yaw':2200,'pitch':1553})
                self.assertFalse(response.json()['running'])
            self.assertEqual(load_settings(path).axis_limits,AxisLimitsConfig(pitch_center=1553))

    def test_diagnostic_route_cannot_bypass_either_axis_limit(self):
        app=create_app(simulated=True);r=app.state.runtime;self.addCleanup(r.shutdown)
        r.config.yaw_id=0;r.config.pitch_id=2
        with TestClient(app) as client:
            for sid,position in ((0,799),(0,2201),(2,1801),(99,1500)):
                r.bus.moves.clear()
                result=client.post('/api/servo/direct-move',json={'servo_id':sid,'position':position}).json()
                self.assertTrue(result['error']);self.assertEqual(r.bus.moves,[])
            for position in (800,2200):
                result=client.post('/api/servo/direct-move',json={'servo_id':0,'position':position}).json()
                self.assertIsNone(result['error']);self.assertEqual(r.bus.moves[-1][1],position)

    def test_frontend_slider_fields_and_buttons_consume_the_same_backend_limits(self):
        node=shutil.which('node')
        if node is None:
            candidate=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
            if candidate.exists():node=str(candidate)
        if not node:self.skipTest('Node runtime is required for the UI binding check')
        app=create_app(simulated=True);r=app.state.runtime;self.addCleanup(r.shutdown)
        r.axis_limits=AxisLimitsConfig(pitch_center=1553);r.logic=r._make_logic()
        session=r.session();source=(PROJECT/'src/locator_demo/web/static/app.js').read_text(encoding='utf-8')
        def js_function(name):
            start=source.index('function '+name+'(')
            match=re.search(r'\n(?:async )?function |\nwindow.addEventListener',source[start+1:])
            return source[start:start+1+match.start()] if match else source[start:]
        fixture=json.dumps({'limits':session['axis_limits'],'presets':session['head_presets']})
        script='''const assert=require('node:assert/strict');const fixture=FIXTURE;
const nodes={};const state={};const $=id=>nodes[id]??={value:1500};
const buttons=Object.keys(fixture.presets).map(name=>({dataset:{posePreset:name},disabled:true,classList:{contains:()=>false}}));
const document={querySelectorAll:()=>buttons};
FUNCTIONS
applyAxisLimitsConfig(fixture.limits);applyHeadPresets(fixture.presets);
assert.equal($('yaw-input').min,800);assert.equal($('yaw-input').max,2200);
assert.equal($('pitch-input').min,1200);assert.equal($('pitch-input').max,1800);
assert.equal($('yaw-min-input').value,800);assert.equal($('yaw-max-input').value,2200);
for(const b of buttons){assert.equal(Number(b.dataset.yaw),fixture.presets[b.dataset.posePreset].yaw);assert.equal(Number(b.dataset.pitch),1553);assert.equal(b.disabled,false);}
console.log('UI bindings passed');'''.replace('FIXTURE',fixture).replace('FUNCTIONS','\n'.join(js_function(n) for n in ('clamp','applyAxisLimitsConfig','applyHeadPresets')))
        result=subprocess.run([node,'-e',script],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
