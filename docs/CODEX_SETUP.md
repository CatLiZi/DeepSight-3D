# Codex Setup Guide

This file is written for Codex or another coding agent taking over a fresh clone.

## One-Line User Prompt

```text
Clone git@github.com:landert-elon/positioning_robots.git, read docs/CODEX_SETUP.md, install dependencies, run tests, start the demo, and help me configure my microphone, camera, yaw servo, pitch servo, movement direction, and audio channel mapping.
```

## Goal

Bring up the positioning robot demo on a Windows machine with minimal manual guessing. The project should run in simulation mode first, then real hardware mode.

## Expected Environment

- Windows 10/11 PowerShell.
- Python 3.10+.
- Git with SSH access to GitHub.
- `ffmpeg` on `PATH`.
- Optional but useful: `node` for JavaScript syntax checks.
- Real hardware when available:
  - CH340 USB serial adapter for the servo bus.
  - Zhongling/ZP bus servos.
  - Stereo microphone.
  - DirectShow camera.

## Clone

```powershell
git clone git@github.com:landert-elon/positioning_robots.git
cd positioning_robots
```

If SSH is not configured, verify:

```powershell
ssh -T git@github.com
```

## Install Dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If `ffmpeg` is missing:

```powershell
winget install Gyan.FFmpeg
```

Restart the shell after installing ffmpeg.

## Verify Software

```powershell
$env:PYTHONPATH="$PWD\src"
python -m unittest discover -s tests -v
node --check src\locator_demo\web\static\app.js
```

If Node.js is unavailable, skip the `node --check` step and mention that it was not run.

## Run In Simulation First

```powershell
$env:LOCATOR_DEMO_SIM='1'
python run_server.py
```

Open:

```text
http://127.0.0.1:8010/
```

Confirm the dashboard loads, WebSocket reconnect works, and manual controls update simulated pose values.

## Run With Real Hardware

Stop the simulated server, then:

```powershell
Remove-Item Env:\LOCATOR_DEMO_SIM -ErrorAction SilentlyContinue
python run_server.py
```

Open:

```text
http://127.0.0.1:8010/
```

Recommended real-hardware order:

1. Leave `摄像头`, `听声转头`, and `视觉跟踪` toggles off while checking servos.
2. Use `扫描舵机` to find connected IDs.
3. Save `Yaw ID` and `Pitch ID`. Known reference values: yaw `0`, pitch `2`.
4. Press manual arrow buttons with a small step. If left/up are reversed, change the direction dropdowns and save.
5. Enable `摄像头`, choose the camera device, choose resolution/crop, then save.
6. Enable `听声转头`; speak near each microphone side. If left/right are reversed, set `声道` to `左右互换`.
7. Enable `视觉跟踪` only after camera preview and manual servo direction are correct.

## Device Discovery Commands

List DirectShow devices:

```powershell
ffmpeg -hide_banner -list_devices true -f dshow -i dummy
```

List camera modes:

```powershell
ffmpeg -hide_banner -list_options true -f dshow -i video="Integrated Camera"
```

List serial ports:

```powershell
[System.IO.Ports.SerialPort]::GetPortNames()
```

## Persistent Settings

Runtime settings are saved in:

```text
locator_demo_settings.json
```

This file is ignored by git because it is machine-specific. To reset calibration, stop the server and delete that file.

Use `locator_demo_settings.example.json` as a reference.

## Important API Endpoints

- `GET /api/session`: current configuration and hardware status.
- `GET /api/devices`: DirectShow camera/audio devices.
- `POST /api/config/servos`: serial port and yaw/pitch IDs.
- `POST /api/config/direction`: movement direction and manual step.
- `POST /api/config/features`: camera/audio/visual toggles.
- `POST /api/config/camera`: camera device, size, and crop.
- `POST /api/config/audio`: audio channel swap.
- `POST /api/head/jog`: manual arrow movement.
- `POST /api/head/move`: direct yaw/pitch pulse target.

## Handoff Checklist

Before telling the user setup is complete:

1. Run Python tests.
2. Start the server.
3. Open the dashboard or at least call `GET /api/session`.
4. Confirm settings can be saved.
5. Confirm no unwanted camera `ffmpeg` process remains after turning camera off or stopping the server.
6. Tell the user exactly which checks passed and which hardware checks still need physical confirmation.
