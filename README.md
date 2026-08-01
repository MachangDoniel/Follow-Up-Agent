# call-followup

Automatically texts back anyone who calls you on **Telegram** or **WhatsApp**
when you don't take the call — whether it rang out or you rejected it. The
message is written by your local LM Studio model from the recent conversation
with that person, so it isn't the same canned line every time.

```
  Telegram call ──> app/telegram_watcher.py ──┐
                                              ├──> Decider ──> LM Studio
  WhatsApp call ──> whatsapp/watcher.js ──HTTP┘      │         (localhost:1234)
                                                     ├──> sqlite audit trail
                                                     └──> "send this" / "don't"
```

Two processes. `python -m app` runs the Telegram watcher **and** the HTTP brain;
the Node WhatsApp watcher is separate because Baileys is Node-only. Every
decision — should we reply, what should it say, have we already replied — lives
in `app/decide.py`, so both platforms behave identically.

---

## Read this before you run the WhatsApp side

WhatsApp gives personal accounts no official way to see call events.
`whatsapp/watcher.js` uses [Baileys](https://github.com/WhiskeySockets/Baileys),
which reimplements the WhatsApp Web protocol and links as a device via the same
QR flow as web.whatsapp.com.

**This violates WhatsApp's terms of service and your number can be banned.**
Auto-replying to calls is precisely the pattern their anti-spam systems look
for. If that number matters to you, test on a spare one.

The Telegram side has no such problem — Telegram publishes MTProto and user
account automation is allowed.

Also: this only sees calls placed *inside* Telegram and WhatsApp. Ordinary
cellular calls never reach either app, so nothing here can trigger on them.
Catching those needs an Android app (`CallScreeningService`); iOS won't allow
it at all.

---

## Setup

```bash
cp .env.example .env
```

Fill in `.env`:

- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` from https://my.telegram.org → API
  development tools. (Or set `TELEGRAM_ENABLED=false` to run WhatsApp only.)
- `YOUR_NAME` and `PERSONA` — these shape how the messages sound.

Then check everything before trusting it with real calls:

```bash
uv run python scripts/selftest.py
```

It validates config, reaches LM Studio, confirms your model is loaded, writes
to the database, and generates a sample follow-up for a fake missed call. It
sends nothing.

### Run it

```bash
uv run python -m app
```

First Telegram run asks for your phone number and the login code Telegram
sends. After that the session is cached in `data/telegram.session` — **that
file is a full login to your account**; don't commit or copy it around.

Then, in a second terminal:

```bash
cd whatsapp && npm install && npm start
```

Scan the QR with WhatsApp → Settings → Linked devices. Auth is cached in
`data/whatsapp-auth/`, same warning as above. Keep your phone online — a linked
device stops working if the phone stays offline too long.

`DRY_RUN=true` is the default: it generates messages and logs exactly what it
*would* have sent, without sending. Leave it on for a few real calls until
you're happy with the wording, then flip it to `false`.

---

## Configuration

Everything lives in `.env`. Real environment variables override the file.

| Variable | Default | What it does |
| --- | --- | --- |
| `DRY_RUN` | `true` | Generate and log, but never send. |
| `YOUR_NAME` | — | Who the message is from. Used in the prompt. |
| `PERSONA` | — | Free-text style note, e.g. `lowercase is fine, keep it brief`. |
| `COOLDOWN_MINUTES` | `30` | Don't message the same person twice inside this window. |
| `MAX_CHARS` | `300` | Hard cap on the generated message. |
| `HISTORY_MESSAGES` | `10` | How much recent chat to feed the model for context. |
| `FALLBACK_TEXT` | … | Sent when LM Studio is down or the output fails the guardrail. |
| `SIGNOFF` | empty | Appended verbatim on its own line to every message, including the fallback. Empty disables it. |
| `ALLOW` | empty | If non-empty, **only** these contacts get a reply. Substring match on name or id. |
| `BLOCK` | empty | These contacts never get a reply. |
| `QUIET_HOURS` | `23:00-07:00` | Comma-separated `HH:MM-HH:MM`. Wrapping past midnight works. |
| `MAX_TOKENS` | `2000` | Generation budget. Keep it generous — see below. |
| `TELEGRAM_ENABLED` | `true` | Set false to run WhatsApp only. |

Sensible first configuration: leave `DRY_RUN` on, and put one or two people you
trust in `ALLOW` so the first live test can't reach anyone else.

### A note on reasoning models

`google/gemma-4-e4b` — and `qwen3.5`, `deepseek-r1`, anything that thinks before
answering — spends most of its budget on hidden reasoning returned in
`reasoning_content`, not `content`. At a 200-token budget this model burned 171
tokens thinking and returned an **empty** message, which looks like "the LLM is
broken" but is really just truncation.

Worse, the reasoning length is **nondeterministic**. The same prompt to
gemma-4-e4b produced 227 reasoning tokens on one run and blew past 700 on
another, so a budget that works most of the time still fails intermittently and
silently drops you onto the fallback text. Hence `MAX_TOKENS=2000`, and a config
check that refuses anything under 400. `app/compose.py` also strips
`<think>…</think>` blocks in case a model inlines its scratchpad into the reply.

---

## How the detection actually works

**WhatsApp** looks clean and isn't. Baileys emits a `call` event whose status is
one of `offer | ringing | timeout | reject | accept | terminate`, and it is
tempting to just listen for `reject` and `timeout`. That catches nothing.

Baileys derives the status from the call node's tag, and `reject` is only
produced when *this client* rejects a call. Declining on your phone arrives here
as a bare `terminate` — the same status you get when a call is answered or the
caller hangs up, with nothing in the node to tell them apart.

So the watcher tracks `offer` events and treats any ending it never saw an
`accept` for as unanswered. `timeout` maps to *missed*, `reject` to *rejected*,
and a bare `terminate` to *missed*.

The residual risk: if answering on your phone doesn't relay an `accept` to this
client, a real conversation would be followed by a "sorry I missed you". If you
see that, set `WHATSAPP_TERMINATE_AS_MISSED=false` in `.env` — you'll then only
catch calls that genuinely rang out, and lose phone-side rejections.

Every call event is logged unconditionally to `logs/whatsapp.out.log`, including
the ones that get skipped and why. Silent skips are what made the first real
missed call leave no trace at all.

### LIDs

WhatsApp increasingly addresses people by an opaque **LID** rather than their
phone JID. A real call from a contact arrives as:

```
call event: status=offer from=209384756102938@lid
```

Nothing in that identifies the caller, so an allow list written in phone numbers
matches nothing and every call gets skipped. Baileys exposes no lookup for it.

The watcher builds the mapping two ways: on connect it resolves every phone
number in `ALLOW`/`BLOCK` through `onWhatsApp()`, which returns each contact's
`lid` alongside their `jid`; and it learns opportunistically from the
`senderLid`/`senderPn` pair on any incoming message. The map persists to
`data/whatsapp-lid-map.json`.

This means **phone numbers in `ALLOW` only cover LID callers after a successful
connect**. If you see `LID pre-resolution failed` in the log, the allow list is
back to matching nothing on calls.

**Telegram** takes more work. `PhoneCallDiscarded` carries only a call id, a
reason and a duration — *not* the caller — so `telegram_watcher.py` caches
`call_id -> caller` when `PhoneCallRequested` first arrives and looks it up on
discard. Calls you place produce `PhoneCallWaiting` rather than
`PhoneCallRequested`, so outgoing calls are filtered out for free.

One genuine limitation: Telegram reports `PhoneCallDiscardReasonHangup` both
when *you* reject a call and when the *caller* gives up and cancels. The watcher
treats both as unanswered and follows up, so someone who dials and immediately
cancels may still get a "sorry I missed you". No field distinguishes the two —
the rejection happens on your phone, not in this client. If that bothers you,
drop `PhoneCallDiscardReasonHangup` from `DISCARD_REASONS` in
`app/telegram_watcher.py`; you'll then only catch rang-out and declined-as-busy.

## When the model is used, and when it isn't

| Situation | What gets sent |
| --- | --- |
| Missed call, **no** prior conversation | `FALLBACK_TEXT`, instantly |
| Missed call, **some** conversation | model-written, in your voice, `SIGNOFF` appended |
| Burst of unanswered messages | model-written, assistant voice, no sign-off |

The rule is simply *is there context worth using*. With an empty chat the model
has nothing to personalise from, so it would spend 15-30s producing something no
better than the fixed line — the fixed line goes out immediately instead. A
burst always has context, since their own messages are the context.

If the model fails or trips the guardrail, every path falls back to fixed text,
so a reply always goes out.

## Burst replies

A second, separate feature: after someone sends several messages in a row with
no reply from you, send one fixed "he's busy, this is an automated reply". Off
by default.

```
BURST_REPLY_ENABLED=false
BURST_THRESHOLD=5        # consecutive incoming messages with no reply from you
BURST_COOLDOWN_HOURS=6   # per contact, independent of the call cooldown
BURST_TEXT=Hi, this is an automated reply — ...
```

The reply is written by the model in an **assistant voice** — their own
messages give it real context to work with. The prompt forbids it from answering
their questions, agreeing to anything, or committing to anything on your behalf;
it may only acknowledge the topic and say you're busy. `BURST_TEXT` is the
fallback if the model fails or trips the guardrail.

`SIGNOFF` is not appended here — the message already says it isn't you.

"Unread" isn't observable (read state lives on your phone), so what's actually
counted is **consecutive incoming messages since your last outgoing one**. The
run resets the moment you reply. It fires *exactly at* the threshold, so a long
conversation doesn't re-trigger on every further message, and the per-contact
cooldown is the backstop.

`ALLOW`, `BLOCK` and `QUIET_HOURS` apply here too. The cooldown is tracked
separately from call follow-ups (`kind` column), so one can't suppress the other.

**Why it's off by default:** this is considerably spammier than call follow-ups
and much closer to the bulk-automation pattern WhatsApp's anti-spam actually
targets. Turning it on raises the ban risk on your number beyond what the call
feature carries.

### Sign-off

`SIGNOFF` is appended after generation, not requested from the model — asking a
model to sign reliably doesn't work, it drops the signature whenever the message
feels too short to warrant one. So the prompt explicitly tells it *not* to sign,
and the code stamps the signature on afterwards.

`MAX_CHARS` covers the whole message including the signature: the body budget is
reduced by the signature length, so a long reply can't push the total over. If
the model signs anyway, the duplicate is detected (ignoring case, spacing and
punctuation) and not stamped twice.

## Guardrails

Generated text is stripped of wrapping quotes and "Here's a message:"
preambles, truncated at a sentence boundary, and rejected outright if it
contains AI tells (*as an AI*) or an unfilled `[your name]` placeholder — in
which case `FALLBACK_TEXT` goes out instead. If LM Studio is unreachable the
agent still replies, using the fallback, rather than going silent.

Every decision is written to `data/followups.sqlite3`, sent or skipped, with the
reason. So you can always see what it did:

```bash
curl -s localhost:8787/recent | python3 -m json.tool
```

The cooldown is derived from that table, so restarting won't let someone get
messaged twice in a row. A send that fails still burns the cooldown — the safe
direction, since a duplicate storm at someone's phone is worse than one missing
follow-up.

## Running it for real

Both processes die when you close their terminal. To keep them up across
terminal closes, logouts and reboots, install them as launchd user agents:

```bash
./scripts/service.sh install
```

Stop the foreground copies (Ctrl+C in both tabs) first, or the new brain won't
be able to bind port 8787.

```bash
./scripts/service.sh status              # both services + brain health + recent log
./scripts/service.sh logs                # follow both logs
./scripts/service.sh restart whatsapp    # every command takes: all | brain | whatsapp
./scripts/service.sh stop                # disarm without uninstalling
./scripts/service.sh uninstall           # remove from login entirely
```

They restart on crash but stay down after a clean stop, throttled to one
restart per minute. Node's stdout lands in `logs/whatsapp.out.log`, which is
the only way to see WhatsApp connection state once it's not in a terminal.

## Layout

```
app/
  __main__.py          entry point: brain + Telegram watcher in one loop
  config.py            .env -> frozen Settings dataclass
  models.py            MissedCall / Message / Decision
  decide.py            the single decision path both platforms use
  gating.py            allow / block / quiet hours / cooldown (pure)
  compose.py           prompt building, output cleaning, guardrails
  llm.py               async LM Studio client
  store.py             sqlite cooldown + audit trail
  brain.py             HTTP endpoint for the Node watcher
  telegram_watcher.py  Telethon MTProto call detection
whatsapp/watcher.js    Baileys call detection (separate process)
scripts/selftest.py    pre-flight check
scripts/service.sh     launchd management for both processes
```
