# Demo: talking tracks (90 s)

Two ready-made calls. Pick one with `python scripts/dev_demo.py table` or
`python scripts/dev_demo.py isp`, or pass any goal as free text. The ticker lines below are
typical hints, not fixed output: the model writes them live from what was said.

Setup before recording: bridge running (`hermes ghost serve` or `scripts/dev_demo.py`),
`OPENAI_API_KEY` set, screen split into the Hermes chat (left) and the You page (right), a
second device or a friend on the Them link, headphones on.

## A. Haggle with the internet provider (`isp`)

The goal the demo starts with:

> get a lower price on my home internet. I pay $89/month for 500 Mbps, customer 4 years,
> contract ended in October. FastNet offers 1 Gbps at $55/month for 12 months. Target
> $60/month or less, no new contract. Walk away: cancel and switch. Name Winch, account 4471
> with Northwind Fiber retention

Script for the friend playing **Northwind Fiber** (Them link):

1. "Thank you for calling Northwind Fiber, how can I help?"
2. When asked for a better price: "I can do $79 a month on a new 2-year contract."
3. After a no: "OK, $70 a month for 6 months."
4. When cancelling comes up: "Let me transfer you to retention… I can offer $60 a month for
   12 months, no contract."
5. Only if the caller asks about fees: "There's a $10 monthly router fee, but I can waive it."
6. When asked for a confirmation: "Your confirmation number is NF-2291."

| Time | On screen | Say |
|---|---|---|
| 0:00 | Hermes chat | "Everyone hates calling their ISP to haggle. Now I don't do it alone, and the AI never joins the call." |
| 0:08 | `/ghost start get a lower price on my home internet … with Northwind Fiber retention` | "I give it my bill, my competitor offer, my target and my walk-away. No approval cards." |
| 0:15 | Open You; friend opens Them | "Ghost is muted to them. Only I hear it." |
| 0:20 | Them: "$79 a month on a new 2-year contract." Ticker: `they want a 2-year contract → decline, mention FastNet's $55 for 1 Gbps` | "First offer. It tells me not to take it, and gives me my best card." |
| 0:32 | Them: "$70 for 6 months." Ticker: `$70 is only for 6 months → ask the price after, say you're ready to cancel` | "It catches the promo trap." |
| 0:44 | Them: "Retention… $60 for 12 months, no contract." Ticker: `$60 hits your target → ask about fees before you agree` | "It knows my target, but never makes me say it out loud." |
| 0:54 | Tap **Speak hint** | "Can't read? I hear it in my earpiece, and my mic goes silent while it plays." |
| 1:00 | Them: "$10 router fee, but I can waive it." Ticker: `they offered to waive the $10 router fee → accept, ask for a confirmation number` | |
| 1:08 | Them: "Confirmation NF-2291." Hang up | |
| 1:12 | One summary: Agreed / Amount `$60/month for 12 months, no contract` / Open / Draft SMS | "From $89 to $60, no contract, fee waived. $348 a year back, and it's all written down." |
| 1:24 | `/ghost status` | "Silent by default. It only speaks to them after `/ghost intervene allow`, and it says it's an AI first." |
| 1:30 | End | "Hermes never joined the call. I did the talking; it did the thinking." |

## B. Book a table (`table`)

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
