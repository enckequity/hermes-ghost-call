---
name: ghost-call
description: Coach the user silently through their own phone call with Ghost Call (hints to the user only; Hermes never joins the line).
---

# Ghost Call

Use this when the user says things like "listen to my call", "ghost this", "coach me on the
phone", "help me on this call", or asks for live hints while *they* talk to someone.

Ghost Call is not an AI caller. The user does the talking. Hermes listens and pushes one-line
hints to the user's chat and their own call page; nothing reaches the other party.

## Do

1. Call `ghost_start` with the user's goal in their words plus any facts they gave (date, time,
   party size, budget, name), and `callee_label` if they named who they are calling.
   Example goal: `book a table Friday 8pm for 4, budget $80, name Winch`.
2. Relay the two links exactly as returned: **You** is for the user (headphones on), **Them**
   goes to the other party. In Twilio mode, say their phone will ring first.
3. Then stay quiet. Hints appear by themselves as a ticker. Do not poll `ghost_hint_latest`
   during the call, and do not post commentary of your own.
4. After hangup one summary is posted automatically. If the user asks later, use
   `ghost_summary` or `ghost_status`.

## Never

- Never ask the user questions during a live call ("what should I say?", "approve this?",
  "did you hear that?"). If something is missing, Ghost already hints it once as a fact.
- Never invent mid-call quizzes, option menus or confirmation prompts.
- Never use `ghost_intervene` unless the user explicitly asks you to say something to the other
  party *and* they have typed `/ghost intervene allow` for this session. It is refused otherwise,
  and always refused in cron or unattended runs. Its first words disclose an AI assistant.
- Never start a ghost session from a cron job or without the user asking.

## Commands the user can type

`/ghost start <goal>` · `/ghost status` · `/ghost speak [hint]` · `/ghost intervene allow|deny`
· `/ghost hangup` · `/ghost transcript` · `/ghost log` · `/ghost check`
