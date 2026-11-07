# Ghost Call: launch video (about 80 s, 16:9)

Narrator over a real mini-demo. The call audio, the hints and the summary come from a real run of
`scripts/e2e_call.py --timeline video/timeline.json` (two ElevenLabs voices through the real
bridge, live OpenAI), not from a mock. Still frames, no shake, no flashes; quick cuts.

## Narration (English)

Eight short lines. build.py lays narration, call lines and the whisper end to end from their
real lengths (no gap longer than a breath); the resulting times are in TIMECODES.txt.

| # | Line |
|---|---|
| N1 | Meet Ghost Call. An AI assistant that helps you on your calls, in real time. |
| N2 | You make the call. Hermes listens, and hints only to you. |
| N3 | Hermes hears every word. A hint appears, and only you can see it. |
| N4 | It knows your target price, and never says it out loud. |
| N5 | Sixty, no contract. Hermes has a follow-up. One tap, and it plays in your ear. They never hear it. |
| N6 | Hang up, and get the deal, what's still open, and a message to confirm it. |
| N7 | Two browser links, a real phone line, or any call on speaker. |
| N8 | Ghost Call. Your real-time AI copilot for every call. |

Voices (ElevenLabs, eleven_v4): narrator Sia (female), provider agent Jack John, caller
Chris. The call is voiced first (video/voices/lineNN.pcm), then `scripts/e2e_call.py --clips
video/voices --timeline video/timeline.json` runs the real test on those takes, so every
caption and hint on screen comes from them.

## Storyboard

| Time | Picture | Sound |
|---|---|---|
| 0:00 | Wordmark "Ghost Call", the ghost glyph, "Your real-time AI assistant on every call." | N1 |
| 0:04 | Hermes chat: `/ghost start <goal>` and the plugin's real reply with both links. | N2 |
| 0:09 | Their page (left) and your page (right), the plugin's own HTML. $79 offer, first hint. | Call |
| 0:17 | You answer with the hint (no contract, FastNet $55). | Call |
| 0:24 | The private part of the goal is highlighted. | N3 |
| 0:28 | $60 for 12 months, no contract; hint. | Call, then N4 |
| 0:41 | Tap Speak hint; the whisper plays in your earpiece only, their speaker stays flat. | Whisper |
| 0:50 | Hang up. | |
| 0:51 | Summary card from the run, "$89 → $60 a month". | N5 |
| 0:57 | Three modes with their commands. | N6 |
| 1:02 | Wordmark, "Your real-time AI copilot for every call.", repo URL. | N7 |

## Rules for the build

- One fixed 1920×1080 stage; no camera moves, cuts are short dips.
- Type: Inter Tight (embedded), dark stage, colour only for state.
- Real UI and real data: the pages are the plugin's own HTML; every hint and summary line on
  screen appears in the recorded run.
