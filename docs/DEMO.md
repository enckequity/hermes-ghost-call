# Demo: talking track (90 s)

Setup before recording: bridge running (`hermes ghost serve` or `scripts/dev_demo.py`),
`OPENAI_API_KEY` set, screen split into the Hermes chat (left) and the You page (right), a
second device or a friend on the Them link, headphones on.

| Time | On screen | Say |
|---|---|---|
| 0:00 | Hermes chat | "Every AI phone demo has the AI talk for you. This one doesn't. Hermes never joins the call." |
| 0:08 | Type `/ghost start book a table Friday 8pm for 4, budget $80, name Winch` | "I give it my goal. That's the whole setup: no approval cards." |
| 0:15 | Two links appear; open You, friend opens Them | "One link for me, one for the restaurant. Ghost is muted to them." |
| 0:22 | Talk: "Hi, a table for four on Friday at eight?" Them: "Friday's full, only Saturday." | |
| 0:30 | Ticker: `00:14 · they said Saturday only → offer Friday 19:30 or Sunday` | "One line, timestamped. Not a question, not a chatbot. A ticker." |
| 0:40 | Them: "The set menu is 95 per person." Ticker: `$95 is over budget → ask for à la carte` | "It knows my budget, so it nudges." |
| 0:50 | Tap **Speak hint** | "If I can't look, I tap and hear it in my earpiece. My mic goes silent while it plays, so they never hear it." |
| 1:00 | Them: "What name is it under?" Ticker: `they asked for a name → say Winch` | |
| 1:08 | Hang up | |
| 1:12 | One summary: Agreed / Date / Amount / Open / Draft SMS | "One result: what we agreed, the price, what's still open, and an SMS I can send." |
| 1:22 | `/ghost status` | "Silent by default. It only speaks to the other side if I type `/ghost intervene allow`, and then it says it's an AI first." |
| 1:30 | End | "Hermes never joined the call. Earpiece only." |
