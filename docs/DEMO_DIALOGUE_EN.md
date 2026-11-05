# Two-person tracking demo · three longer turns, under one minute

A stands still on the bear's own left-front side. B stays on its right-front
side and walks slowly sideways **during B's turn**, keeping their face visible.
Use normal conversational volume. Do not speak over each other. Positions are
approximate, not measured angle labels.

Select **Listen / audio primary** for this switching trial. The operator enables
tracking separately; the recording button never starts the motors. Click
**录制60秒**, select the control-console browser tab, and wait for the three-second
preparation cue. Each speaker completes one paragraph before changing turns.

### A · stationary · 3–20 s

Hi! I've been working at my desk all morning, so I'm ready for a short break. I'm thinking about getting some coffee and sitting by the window. Would you like to join me?

### B · moving slowly · 21–38 s

That sounds nice. I could use a break too. I'll walk over slowly while we talk. The window seats look comfortable, and we can decide what to have for lunch.

### A · stationary · 39–54 s

Great, let's take those seats. I'll have a small coffee with milk. We can relax for a few minutes, then head back and finish our work together.

Leave one second of silence at 20–21 s and 38–39 s. Stay quiet for the final
54–60 s so the camera and target change remain visible in the recording.

**90 words total** (33, 30, 27 words per paragraph). At about 115–125 words per minute,
the speech takes roughly 43–47 seconds; preparation and turn pauses keep the dialogue
under one minute. The on-page timing is a cue, not an actual speech label.
If you finish early, wait for the next cue. An observer can mark the real A/B/quiet turns.

For review, the planned-cue events use `three_turns_v2_A1`, `three_turns_v2_B1`
and `three_turns_v2_A2`. These stable IDs refer to the full paragraphs above
and keep the timeline compatible with the existing D1 recording interface.

## Saved materials

Files stay under `recordings/demo/<session-id>/`, which is excluded from Git.
The page provides MP4 and ZIP downloads after saving completes. The ZIP retains:

- `demo.mp4`: the selected control-console tab with ear-pair stereo sound;
- `screen.webm`: the original browser video;
- `audio-4ch.wav`: the same four-channel, 48 kHz, 16-bit PCM used by the algorithm;
- `timeline.jsonl`: displayed target IDs, candidate scores, sound direction,
  control pose, position feedback and observer/plan markers;
- `session.json`: configuration, audio continuity and software timing metadata.

The video shows the console and onboard camera view. To show the outside of
the bear physically moving, record a separate phone view as well. Software
clock alignment is useful for reviewing switches but is not a calibration of
camera exposure, screen latency or microphone ADC latency.

The screen chooser must be confirmed by the person operating the browser.
Choose the control-console **tab** and keep it visible. The backend reuses the
existing RAW stream and does not open a second microphone or change tracking
thresholds, channel mapping, servo range or speed.

For speaker-switching trials, select **Listen / audio primary**. It is mutually exclusive with **Visual tracking / visual primary**. Audio establishes the destination; vision refines the matching face. A newly confirmed sound source can interrupt the visual assist.
