# Commands

Everything you can tell this agent to do, in one place.

Two ways to control it:

- **From your phone** — send commands to your Telegram bot. Use this normally.
- **From the terminal** — start/stop the services, run checks, re-link WhatsApp.

---

## 1. Telegram bot commands

Open the chat with your bot and type `/`. Telegram shows the list automatically.

### Turn it on and off

| Command | What happens |
| --- | --- |
| `/off` | Stops all replies. Nothing goes out until you say `/on`. |
| `/off 2h` | Stops for 2 hours, then starts again by itself. Also `30m`, `1d`. |
| `/on` | Start replying again. Also cancels a timed `/off`. |
| `/status` | Shows what it is doing right now. **Start here when unsure.** |

### Turn off one platform only

| Command | What happens |
| --- | --- |
| `/whatsapp off` | WhatsApp calls and messages get no reply. Telegram still works. |
| `/whatsapp on` | Back on. |
| `/telegram off` | Telegram gets no reply. WhatsApp still works. |
| `/telegram on` | Back on. |

### Choose what it replies to

| Command | What happens |
| --- | --- |
| `/calls off` | Missed calls get no follow-up. |
| `/calls on` | Missed calls get a follow-up again. |
| `/burst off` | No reply after 5 unanswered messages. |
| `/burst on` | Reply again after 5 unanswered messages. |
| `/dryrun on` | **Safe mode.** It writes the reply to the log but sends nothing. |
| `/dryrun off` | Sends for real. |

> Use `/dryrun on` when you are testing. You will see exactly what it *would*
> have said, without anyone receiving it.

### People

| Command | What happens |
| --- | --- |
| `/block Shujoy` | Never reply to this person again. |
| `/block +8801787308210` | Works with a number too. |
| `/unblock Shujoy` | Undo it. |
| `/blocked` | Who is blocked right now. |
| `/allow Shujoy` | ⚠️ Reply to **only** this person. Everyone else is ignored. |
| `/unallow Shujoy` | Remove from the allow list. When it is empty, everyone gets replies again. |
| `/allowed` | Show the allow list. |

> **Be careful with `/allow`.** An allow list is a whitelist. The moment one
> person is on it, *nobody else* gets a reply. The bot warns you when the first
> name goes in. `/unallow` them to switch it off again.

### Names

WhatsApp does not always tell the agent who someone is. You can label them:

| Command | What happens |
| --- | --- |
| `/name +8801609982884 Dr. Dinesh` | Save a name for this number. |
| `/name +8801609982884` | Clear the name. |
| `/names` | Names you have saved. |

### See what happened

| Command | What happens |
| --- | --- |
| `/sweep` | Everything in the last hour: messages, calls, who is waiting. |
| `/sweep 3h` | A wider window. Also `30m`, `1d`. |
| `/sweep send` | Reply to everyone who is waiting. |
| `/sweep 3h send` | Same, for a wider window. |
| `/recent` | The last 10 decisions it made, sent or skipped. |
| `/recent 25` | More of them. |

What the symbols in `/sweep` mean:

| | Meaning |
| --- | --- |
| 🔴 | 5+ messages waiting — `/sweep send` will reply |
| 🟡 | A call with no reply — `/sweep send` will reply |
| ⚪️ | Waiting, but under 5 messages — shown only, no reply |
| ✅ | You already replied yourself |
| 🤖 | The bot already replied |

If a row says **time unknown**, WhatsApp told us the count but not when it
happened — that is normal for chats delivered by a history sync, before the
agent ever watched a message arrive in them. They are shown in every window
rather than hidden, so check the age looks sensible before `/sweep send`.

### Start over

| Command | What happens |
| --- | --- |
| `/reset` | Forget every change you made from the bot. Back to what `.env` says. |
| `/help` | The full list again. |

---

## 2. Terminal commands

All of these run from the project folder:

```bash
cd /Users/donieltripura/Projects/Agents/call-followup
```

### Start and stop

```bash
./scripts/service.sh status      # is it running? is it healthy?
./scripts/service.sh restart all # restart both parts
./scripts/service.sh stop all    # stop everything
./scripts/service.sh start all   # start everything
./scripts/service.sh logs        # watch the logs live (Ctrl-C to stop)
```

There are two parts. You can restart just one:

- `brain` — the Python side. Decides what to say. Also runs the Telegram watcher and your bot.
- `whatsapp` — the Node side. Watches WhatsApp and sends messages there.

```bash
./scripts/service.sh restart whatsapp
```

### Check that everything works

```bash
uv run python scripts/selftest.py     # config, model, database, pairing. Sends nothing.
uv run python scripts/test_notify.py  # sends you one fake notification
```

### Re-link WhatsApp

Run this if incoming messages stop working, or you want the chat list refreshed.
The QR code appears in your own terminal, so widen the window first.

```bash
./scripts/relink.sh            # pair again
./scripts/relink.sh --restore  # undo it, go back to the previous link
```

After you see `connected as ...`, wait about a minute, press `Ctrl-C`, then:

```bash
./scripts/service.sh start whatsapp
```

---

## 3. Checking things yourself

```bash
# Is the brain alive?
curl -s localhost:8787/health

# The last decisions, as raw data
curl -s localhost:8787/recent

# What WhatsApp chats does it know about?
curl -s localhost:8788/chats
```

**The most important check.** This counts messages other people sent you that
the agent could actually read. If this number never goes up, WhatsApp
decryption is broken and burst replies cannot work:

```bash
python3 -c "import json;h=json.load(open('data/whatsapp-history.json'));print(sum(1 for e in h.values() for m in e.get('messages',[]) if m['from']!='me'),'incoming messages recorded')"
```

Decryption errors, if you suspect that problem:

```bash
grep -c "Bad MAC" logs/whatsapp.err.log
```

---

## 4. Files you might edit

| File | What it holds |
| --- | --- |
| `.env` | All settings. Secrets live here — never commit it. |
| `fallbacks.txt` | The 50 fixed replies. Edit the wording freely. |
| `data/` | Sessions, database, contact names. **Treat like passwords.** |
| `logs/` | What happened, and why. |

After editing `.env` or `fallbacks.txt`, restart:

```bash
./scripts/service.sh restart all
```

---

## 5. When something is wrong

| Problem | Try this |
| --- | --- |
| Nothing is being sent | `/status` — is it off, paused, or in dry run? |
| One person gets nothing | `/blocked` and `/allowed` — are they excluded? |
| It replied twice to nobody | `/recent` — every decision is recorded with a reason. |
| No reply after 5 messages | Check the incoming-message count in section 3. |
| Bot does not answer commands | `./scripts/service.sh restart brain` |
| WhatsApp not sending | `./scripts/service.sh status`, then `./scripts/relink.sh` if needed |
| Want to stop it right now | `/off` from your phone. Fastest option. |

---

## 6. Two things worth remembering

**`/off` is instant and works from anywhere.** If the agent says something you
did not want, send `/off` first and sort it out afterwards.

**Every decision is recorded**, sent or skipped, with the reason. `/recent`
from your phone, or `curl -s localhost:8787/recent` in the terminal. You never
have to guess what it did.
