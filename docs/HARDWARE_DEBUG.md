# Hardware Debug Notes

Use this when the robot is connected but behavior is wrong.

## Servo Bring-Up

1. Power the servo bus before testing movement.
2. Start the server and open the dashboard.
3. Keep autonomous toggles off.
4. Click `扫描舵机`.
5. Save detected IDs:
   - reference yaw: `0`
   - reference pitch: `2`
6. Use the arrow buttons with a small step.
7. If the head moves opposite the button label, change:
   - `左键`: whether left increases or decreases yaw pulse.
   - `上键`: whether up increases or decreases pitch pulse.
8. Save direction; the setting persists.

The software clamps commands to `1200..1800`.

## Camera

The dashboard has two separate switches:

- `摄像头`: opens/closes camera capture.
- `视觉跟踪`: uses camera target data to control servos.

This means preview can stay on while visual tracking is off.

If the camera is stuck:

1. Turn off `摄像头`.
2. Stop the server if needed.
3. Check no video ffmpeg process remains:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -match 'ffmpeg' -and $_.CommandLine -match 'video=|Camera' } |
  Select-Object ProcessId,Name,CommandLine
```

Typical settings:

- ordinary webcam: `1280x720`, crop `不裁切`;
- stereo USB camera: `2560x800`, crop `左半幅`.

## Audio

Use the `声道` control if left/right are reversed.

Debug commands:

```powershell
ffmpeg -hide_banner -list_devices true -f dshow -i dummy
```

Speak close to one microphone at a time. The dashboard should show left/right changes before autonomous sound tracking is enabled.

## Recommended Demo Order

1. Manual servo direction.
2. Camera preview.
3. Audio left/right.
4. Sound tracking only.
5. Visual tracking only.
6. Combined sound plus visual behavior.
