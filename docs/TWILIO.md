# Twilio mode: a real phone call

Twilio rings **your** phone first. When you pick up, it rings the other party and joins both
legs into one conference. Each leg streams its inbound audio to the bridge (receive-only), so
Ghost hears both sides. Hints show in the Hermes chat (or the demo terminal); **Speak hint** is a
conference announcement played to your leg only. The other party never hears Ghost unless you
turn on Intervene, and then it says it is an AI first.

## What you need

1. **A Twilio account.** A trial account works, with these limits:
   - it can only call numbers you have verified: add your own phone and the other party's phone
     under *Phone Numbers > Verified Caller IDs*;
   - every call starts with a short trial notice and asks the person to press a key.
2. **A Twilio phone number with Voice** (*Phone Numbers > Buy a number*; the trial credit covers
   the first one).
3. **Voice geo permissions** for the country you call: *Voice > Settings > Geo permissions*
   (for example, tick Israel to call +972 numbers).
4. **Your Account SID and Auth Token** from the Twilio console home page.
5. **A public HTTPS address for the bridge**, so Twilio can reach it. The demo can open one with
   cloudflared (free, no account):

   ```powershell
   winget install --id Cloudflare.cloudflared
   ```

   Open a new terminal afterwards so `cloudflared` is on the PATH.

## demo.env

Add these lines to the git-ignored `demo.env` (your own values, E.164 format):

```
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=+1...        # your Twilio number
GHOST_USER_NUMBER=+972...       # your own mobile: Twilio rings it first
```

## Run

```powershell
.venv\Scripts\python scripts\dev_demo.py isp +972XXXXXXXXX --public
```

`+972XXXXXXXXX` is the other party. `--public` starts cloudflared and uses its HTTPS address.
The demo sets the dialing allowlist to exactly your phone and that number for this run
(emergency, short and premium numbers are always refused). Then:

1. Your phone rings: pick up. You hear "Ghost is listening, muted to the other party".
2. The other phone rings: they pick up, you talk.
3. Hints print in the terminal. Type `speak` to hear the latest one in your ear only.
4. Type `hangup` (or just hang up): the summary prints once.

## Costs

Twilio bills each leg per minute (two legs per call), plus the media streams; prices depend on
the country you call. OpenAI bills transcription per minute of audio and a few cents of hints.
