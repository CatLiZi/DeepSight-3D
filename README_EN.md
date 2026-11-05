<div align="center">

# DeepSight-3D

**A panda that sees people, listens for direction, and turns toward interaction.**

Stereo vision · Four-microphone localization · Audio-visual selection · Smooth two-axis control

[中文](README.md) · [Quick start](#quick-start) · [Architecture](#architecture) · [Hardware and CAD](#hardware-and-cad) · [Validation and limits](#validation-and-limits)

[Progress report (HTML, Chinese)](docs/report/index.html) · [Assessment and open issues (HTML, Chinese)](docs/report/assessment.html)

[Two-person recording guide (Chinese)](docs/DEMO_RECORDING_CN.md) · [English dialogue under one minute](docs/DEMO_DIALOGUE_EN.md)

<sub>GitHub shows HTML source in its file view. Clone or download the repository and open these privacy-filtered reports in a browser for offline viewing.</sub>

<img src="docs/assets/panda-assembled-cad.png" alt="Panda assembly rendered from the repository head and body Blender source files" width="720" />

<sub>Rendered by combining the V17 head and V25 body at their source-file coordinates. This is a design preview, not a new tolerance or collision certification.</sub>

</div>

## Overview

DeepSight-3D is a panda-shaped research prototype for **selective audio-visual perception and interaction**. It detects and associates faces from stereo images, estimates speech activity and horizontal sound direction from four microphones, selects a target using either audio-primary or visual-primary control, and turns a two-axis head toward that target.

- **See:** detect faces independently in both eyes and associate them using stereo alignment and a shared viewing direction.
- **Listen:** estimate delays from the ear and body pairs, supported by voice activity, frequency reliability, and pose-dependent fusion.
- **Select:** in audio-primary mode, turn toward a confirmed sound and use the matching face for visual refinement; a new confirmed source can interrupt. In visual-primary mode, retain a reliable face and use sound as assistance.
- **Turn:** retain a bounded acoustic destination, approach it with a smooth trajectory, and revoke motion on stop, visual takeover, or feedback fault.

> **R52 update:** [Exclusive audio/visual primary modes](docs/report/primary-modes-r52.html) are implemented; 904 Python regressions and 123 frontend checks pass. R52 was verified loaded after a normal local restart; alternating-speaker behavior still needs a field trial. Capture settings and smooth-motion limits are preserved.

> **Previous status: R51 indoor-demo candidate / research prototype.** The project team reports that the current overall experience is usable. R51 adds local browser-origin protection and fresh-checkout portability fixes, verified in simulation. Absolute acoustic angle, full-pose four-microphone fusion, multiple speakers, and metric depth still need field acceptance; test counts are not physical accuracy.

## Hardware and CAD

### Model views

<table>
<tr><th>Head interior · rear shell hidden</th><th>Body interior · rear shell hidden</th></tr>
<tr><td><img src="docs/assets/head-interior-cad.png" alt="CAD render of the V17 head interior and ear wiring" /></td><td><img src="docs/assets/body-interior-cad.png" alt="CAD render of the V25 body interior and body microphones" /></td></tr>
</table>

The views are rendered from the repository models below. Rear shells are temporarily hidden and existing modeled wiring is shown for inspection. Historical computer paths were subsequently removed from the models; geometry and object structure were checked and preserved. The [ear microphone holder reference](docs/assets/microphone-holder-reference.png) was supplied by the project team; its exact correspondence to a CAD revision has not been independently rechecked.

| Component | Editable source | Contents |
| --- | --- | --- |
| V17 head | [panda_head_only_V17.blend](hardware/cad/head_shell/panda_head_only_V17.blend) | Head shells, ears, camera mounts, and component references |
| V25 body | [panda_body_V25_shells_V16_cylinder.blend](hardware/cad/body_shell/panda_body_V25_shells_V16_cylinder.blend) | Body shells, body microphone holders, and cylinder-base reference |
| Provenance | [panda_source_manifest.json](hardware/cad/panda_source_manifest.json) | File hashes and earlier Blender inspection |

The repository currently contains editable `.blend` sources, **not verified final STL / 3MF exports or slicer projects for this pair of models**. Check export objects, dimensions, fit, and cable clearance before printing. [CAD guide](hardware/cad/README.md)

### Hardware snapshot

| Component | Selection or configuration | Qualification |
| --- | --- | --- |
| Stereo camera | SC233HGS, color global shutter; vendor states 2 MP and 112° lens | Manual focus; the 112° axis is not confirmed. Focus assistance is not optical autofocus |
| Stereo stream | MJPEG side-by-side **3840×1080**, 1920×1080 per eye, 60 fps requested | Historical captures received roughly 33–50 fps; request rate and synchronized exposure error are not verified performance |
| Microphones | YDPI4MIC, four analog MEMS microphones, WASAPI RAW, 48 kHz / 16-bit | USB audio VID:PID `EF09:459D`; all four raw channels are retained |
| Spacing | Ears about 13.55 cm; body pair about 7.55 cm; same-side ear–body about 17.7–17.8 cm | User measurements/estimates, not a complete 3D acoustic calibration |
| Two servo axes | Yaw ID 0: 1500 / 2000 / 2500; Pitch ID 2: 1255 / 1550 / 1855 | Rig **software position units** recorded on 2026-11-04, minimum / control center / maximum; mechanical limits still require field confirmation |
| Servo angle scale | User-provided specification: 2000 / 270 ≈ **7.4074 ticks/degree** | Used for acoustic goals and head/body yaw conversion since R50; not a measured assembled-rig angle calibration |
| Control and UI | Python, FastAPI, local dashboard, 50 Hz smooth trajectory | 50 Hz is command-trajectory frequency, not 50 Hz physical position feedback |

<img src="docs/assets/microphone-layout.svg" alt="Four-microphone layout with left and right named from the bear's perspective" width="100%" />

Left and right are the **bear’s own**: left ear CH2, right ear CH3; left body CH1, right body CH4. The ears move with the head while the body pair remains fixed, so the two coordinate frames must be aligned with pose. [Rig example](configs/panda-assembled-r52.example.json) · [Microphone layout](calibration/microphone_layout.json)

The control center and acoustic geometry reference are distinct: the example uses yaw control center 2000 while the existing head-to-body acoustic reference remains 1952. Recheck the acoustic reference if the physical head/body alignment changes; do not assume the two values are interchangeable.

## Architecture

The [R52 module specification](docs/ALGORITHM_ARCHITECTURE_R52_CN.md) describes the current architecture: parallel perception, mutually exclusive audio-primary / visual-primary ownership, one shared smooth controller, and a separate D1 recording branch.

[Diagram-agent handoff prompt](docs/AGENT_PROMPT_FRAMEWORK_R52_CN.md) · [R52 workflow and validation report](docs/report/primary-modes-r52.html)

<!-- R52_ARCHITECTURE_HANDOFF: replace this historical diagram and its generator, then remove the legacy notice. -->
<details><summary>Historical R51 diagram — does not include the R52 audio-primary takeover path</summary>
<img src="docs/assets/system-architecture.svg" alt="Historical R51 visual-first architecture; R52 exclusive control modes are not yet drawn" width="100%" />
</details>

### Vision: both eyes contribute to the viewing direction

1. Capture a side-by-side stereo frame and detect faces independently in each eye, with multi-scale search.
2. Apply the final-assembly alignment-preview parameters and associate corresponding observations; do not forcibly merge ambiguous pairs.
3. Map a shared steering direction back to each eye. Real **horizontal disparity is retained**; depth is not fabricated to make two previews overlap.

The current common view is approximately **1837×1053 per eye**. Held-out and new board frames showed lower vertical alignment error, but hand-held board flatness, full-field accuracy, and known-distance accuracy still need validation. Metric distance output remains disabled. [Stereo profile](calibration/stereo_camera-alignment.json) · [Stereo notes](docs/BINOCULAR_UPGRADE_CN.md)

### Audio: four channels, two conditionally fused pairs

Raw PCM is processed as an ear pair and a body pair. A pre-trained **Silero VAD** supplies speech support; reliability-weighted GCC delay estimates, competing-peak checks, and temporal consistency produce the bearing. Processed/mixed audio is not substituted for the raw phase. R49 added a bounded speech-state recovery branch without retraining the model.

A body bearing is transformed into head coordinates only when pose and acoustic quality support it; otherwise the system falls back to a reliable ear bearing. This is **pose-aware cooperation between two microphone pairs**, not a fully calibrated six-pair 3D array. It cannot reliably measure sound distance or fully resolve front/back ambiguity. [Four-mic design](docs/FOUR_MIC_FUSION_CN.md) · [Pose alignment](docs/DYNAMIC_MIC_FUSION_CN.md)

### Selection and motion

- **Audio primary:** a confirmed sound establishes a finite yaw goal. After approaching it, associate an actual face in that sector and refine yaw/pitch visually. A newly confirmed source can interrupt the old visual assist.
- **Visual primary:** retain a reliable visible target when sound is absent or conflicting; use relative face size and quality when acquiring. Without a visual owner, an authorized finite sound search remains possible.
- **Exclusive ownership:** select one primary mode while retaining auxiliary perception. Switching modes revokes old intent; both switches off means preview only.
- **Cross-modal association:** compare a reliable sound sector with the whole angular extent of each face, then rank using relative size, visual quality, and continuity. The score is a rule-based association score, not a calibrated speaker probability.
- **Finite target:** a short pause in speech need not erase an authorized destination; a historical blue overlay is not new acoustic evidence.
- **Bounded control:** R50+ converts a bearing with the configured ticks/degree scale while separately limiting each step and the 50 Hz trajectory. Invalid position feedback stops motion; actual mechanical accuracy and smoothness still require field validation.

The system does not currently identify names or voiceprints, transcribe speech, or provide semantic dialogue. Face size is not an acoustic range estimate. [Visual-priority design](docs/VISUAL_PRIORITY_CN.md) · [Acoustic goal geometry](docs/CORE_YAW_GEOMETRY_CN.md)

## Quick start

The example uses Windows PowerShell and Python 3.11. FFmpeg with DirectShow, suitable audio/video and serial drivers, and Node.js for frontend tests are required. Verify dependencies and GPU support on the target machine.

~~~powershell
git clone https://github.com/CatLiZi/DeepSight-3D.git
Set-Location DeepSight-3D
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
~~~

For this assembled rig, copy the example **only on first installation when no local settings file exists**, then verify devices, directions, and safe travel:

~~~powershell
if (-not (Test-Path .\locator_demo_settings.json)) {
    Copy-Item .\configs\panda-assembled-r52.example.json .\locator_demo_settings.json
}
$env:LOCATOR_DEMO_SIM = "0"
python -X utf8 .\run_server.py
~~~

The example starts with camera, audio, and visual feature switches off. Check devices and feedback in the dashboard before enabling what is needed. Serial port assignments can change; use the read-only discovery and confirm the result. Open [http://127.0.0.1:8010/](http://127.0.0.1:8010/) and check before starting automatic motion. Stop the service with `Ctrl+C` in its terminal.

Simulation without physical devices:

~~~powershell
$env:LOCATOR_DEMO_SIM = "1"
python -X utf8 .\run_server.py
~~~

The service listens on `127.0.0.1` by default. R51 rejects cross-origin browser control requests; **this is not authentication against other local processes**, so do not expose the controller directly to a LAN or the internet. Choose compatible ONNX Runtime / PyTorch builds for the target machine. [Current release notes](docs/CURRENT_RELEASE_CN.md)

## Validation and limits

With the existing local Python environment and simulated actuators:

~~~powershell
python -X utf8 tools\validate_regression_repair.py --output .tmp\release-tests
python -X utf8 tools\validate_frontend.py
python -X utf8 tools\validate_geometry_holdout.py
python -X utf8 tools\validate_publication.py --output .tmp\publication-check
~~~

| Evidence | Result / status |
| --- | --- |
| Python regression | **855 passed**, including cross-origin, checkout portability, calibration file, acoustic-goal, and motion-safety checks |
| Frontend checks | **104 passed**, including five JavaScript syntax checks |
| Angle-geometry simulation | 240 independent R50 cases across start positions, directions, and speed profiles; no physical motor was driven |
| Real audio | Existing recording replay shows a continuity improvement; soft speech, motor noise, multiple people, and long distance still need comprehensive field acceptance |
| Stereo | Alignment preview is available; metric scale, full-field error, and known-distance accuracy are not accepted |
| Mechanics | The user reports the overall experience is usable; intermittent feedback, final mechanical limits, and measured smoothness remain to be checked |

These tests do not access the physical servos. Without an angle-measurement tool, first check the bear’s left / center / right response, whether a confirmed short utterance completes its turn, and whether Stop cancels remaining motion. Do not treat approximate human positions as exact 30°/45° ground truth. [Validation record](docs/VALIDATION_R52.md)

## Repository layout

~~~text
DeepSight-3D/
├── src/locator_demo/       # capture, audio, stereo, selection and motion
├── src/locator_demo/web/   # FastAPI, state API and local control dashboard
├── configs/                # rig examples; do not overwrite local settings
├── calibration/            # stereo alignment preview and microphone layout
├── hardware/cad/           # head/body Blender sources and model manifest
├── models/                 # pre-trained vision and voice-activity models
├── tests/                  # synthetic signals and simulated actuator checks
├── tools/                  # calibration, diagnostics, tests and diagram tools
└── docs/                   # guides, validation and project visuals
~~~

Optional TalkNet remains a Git submodule; the current rig uses `rules` by default. To explore it, run `git submodule update --init --recursive` and obtain weights according to the upstream instructions. Third-party components and models retain their own licenses; Silero’s license is in [models/silero_vad.LICENSE.txt](models/silero_vad.LICENSE.txt). The repository does not currently declare a project-wide license.
