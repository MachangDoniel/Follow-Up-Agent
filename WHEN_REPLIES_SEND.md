# When Replies Are Sent — A User Guide

This document walks through the exact conditions under which the agent sends a reply, with real examples.

---

## The Short Version

**Call follow-ups:** When someone calls and you don't answer or reject it, you get an auto-reply sent after a grace window (unless you reply by hand in the meantime).

**Burst replies:** When someone sends 5+ messages in a row with no reply from you, you get one auto-reply saying you're busy (disabled by default).

The agent has several safety gates — if any one says "no", nothing gets sent, even if the others say "yes".

---

## Call Follow-Ups: The Exact Sequence

### What Triggers It

1. Someone calls you on WhatsApp or Telegram
2. The call ends without you answering **or** you actively reject it
3. The call must have ended — ringing alone doesn't trigger anything

### What Happens Next

```
t=0s       Call ends (rejected or unanswered)
t=0s       Agent checks: is this person allowed to get a reply?
           • Is their contact ID known?
           • Are they on the BLOCK list?
           • If ALLOW is configured, are they on it?
           • Is this during quiet hours?
           • Did we message them recently (within COOLDOWN_MINUTES)?
           
t=0-30s    If all checks pass: generate the message (takes 25-85s)
           Meanwhile, you have a grace window to reply by hand
           
t=30s      Check again: did you message them while we were thinking?
           If YES: cancel everything, log "standing down"
           If NO: continue
           
t=30s+     Send the message
```

### Real Example

**You:** receive a WhatsApp call from Al Mahamud at 14:00

**What happens:**
- 14:00:02 – Call ends (rejected). Agent detects it.
- 14:00:02 – Checks: Al Mahamud? ✓ on allow list ✓ not blocked ✓ not in quiet hours ✓ last messaged 40 min ago ✓ passes all gates
- 14:00:02 – Starts generating a message (takes ~30s)
- 14:00:15 – **You type "wait" to him** — agent sees this
- 14:00:32 – Generation finishes, agent checks again: you messaged him? YES → **cancels send**, logs "you already replied yourself"
- **Result:** No auto-reply sent. You're handling it.

**Alternative scenario (you don't message him):**
- 14:00:02 – Same checks, all pass
- 14:00:02 – Starts generating
- 14:00:32 – Finishes. You didn't message him. **Sends the auto-reply.**
- **Result:** Al Mahamud gets the message.

---

## The Safety Gates (In Order)

Every check below is a hard stop. If ANY fails, no message is sent.

### 1. Contact ID must exist
If the call somehow came in with no caller ID, skip it.

### 2. BLOCK list
```
BLOCK=shujoy,某人
```
If the caller is on the block list (substring match on name or ID), skip.

### 3. ALLOW list
```
ALLOW=8801787308211,Mahmud
```
- **If ALLOW is EMPTY:** everyone passes ✓
- **If ALLOW is configured:** only people on the list pass; everyone else is skipped

### 4. Quiet hours
```
QUIET_HOURS=23:00-07:00
```
If current time is between 23:00 and 07:00, skip (even if everything else says yes).

### 5. Cooldown
```
COOLDOWN_MINUTES=5
```
If we messaged this person in the last 5 minutes, skip (prevents repeat auto-replies).

**Important:** Cooldowns are tracked separately by kind (call vs burst), so a burst reply doesn't block a call follow-up and vice versa.

### 6. DRY_RUN flag
```
DRY_RUN=true
```
If `DRY_RUN=true`, generate the message and log what we *would* have sent, but don't actually send it.

---

## Burst Replies: 5+ Unanswered Messages

**Disabled by default.** Requires: `BURST_REPLY_ENABLED=true`

### What Triggers It

1. Someone sends you a message (on WhatsApp or Telegram)
2. You haven't replied to them yet
3. They send message #2, #3, #4, #5 — still no reply from you
4. **At message #5 exactly**, the agent checks all the gates (same as call follow-ups)
5. If all pass: sends one fixed "I'm busy" message

### Why Message #5 Exactly?

So a long conversation doesn't spam them with "I'm busy" repeatedly. If they send #6 while the cooldown is active, no new message is sent.

### Real Example

**Timeline:**
- 10:00 – Shujoy sends: "hey call me"
- 10:02 – Shujoy sends: "urgent"
- 10:04 – Shujoy sends: "you there?"
- 10:06 – Shujoy sends: "hello?"
- 10:08 – Shujoy sends: "????" ← **Agent fires here** (5th message)
  - Checks all gates (allow, block, quiet, cooldown, dry_run)
  - Sends: "Hi, this is an automated reply — I am busy at the moment..."
  - Burst cooldown starts (6 hours by default)
- 10:10 – Shujoy sends: "ok" ← Agent ignores (cooldown still active)
- 10:12 – **You message Shujoy** "sorry, was in a meeting"
  - The unanswered counter resets
- 10:14 – Shujoy sends: "no problem" ← Counter is now 1 (not 5), so no burst reply

---

## The Grace Window and Stand-Down

### Why It Exists

Generation takes 25–85 seconds on a reasoning model. That's plenty of time for you to answer by hand. The grace window gives you that chance.

### How It Works

```
t=0s       Call ends, generation starts
t=0-30s    Grace window open — you can reply
t=30s      Agent finishes generation, checks if you replied
           If YES: cancel send, log "standing down"
           If NO: send
```

### What "Replied By Hand" Means

The agent checks your last message to this contact. If you sent one *after* the call started, the agent assumes you're handling it and cancels the auto-reply.

**Note:** This is best-effort. If your messages sync slowly, the agent might not see your reply immediately, and the message may still go out. In practice, this is rare — most phones sync within seconds.

---

## Configured Examples

### Example 1: Closed Group (Al Mahamud & Shujoy Only)

```
ALLOW=8801787308211,8801904889984,Mahmud,Shujoy
BLOCK=
QUIET_HOURS=23:00-07:00
COOLDOWN_MINUTES=30
DRY_RUN=false
```

**Who gets replies:** Only Al Mahamud, Shujoy, and anyone whose name contains "Mahmud" or "Shujoy"  
**Who gets silence:** Everyone else  
**When:** Any time outside 23:00-07:00, and not within 30 minutes of a previous message to them

### Example 2: Open to Everyone (Current Setup)

```
ALLOW=
BLOCK=
QUIET_HOURS=23:00-07:00
COOLDOWN_MINUTES=5
DRY_RUN=false
```

**Who gets replies:** Everyone  
**Who gets silence:** Nobody (except during quiet hours)  
**When:** Any time outside 23:00-07:00, and not within 5 minutes of a previous message  
**Trade-off:** First-time LID callers (rare) might not have history, but they still get a reply

### Example 3: Test Mode (Safe for First Run)

```
ALLOW=8801787308211,Shujoy
BLOCK=
QUIET_HOURS=
COOLDOWN_MINUTES=1
DRY_RUN=true
```

**Who gets replies:** Only configured contacts, and only to the log  
**Who gets silence:** Everyone else, nothing actually sent  
**When:** Always (no quiet hours), testing as fast as possible (1-min cooldown)

---

## The Audit Trail

Every decision is logged to `data/followups.sqlite3`. Query it to see:

```bash
curl -s http://localhost:8787/recent
```

Returns the last 20 decisions: sent, skipped, and why.

Example output:
```
2026-08-02 14:23:47 | call | sent=1 | Al Mahamud | "sorry i missed..."
2026-08-02 14:22:15 | call | sent=0 | Unknown    | "contact is not on the allow list"
2026-08-02 14:00:33 | call | sent=0 | Al Mahamud | "you already replied yourself, standing down"
```

Read this to debug: "why didn't they get a message?" Answer: check the log.

---

## Troubleshooting: Why No Reply?

Walk through the gates in order. The first "no" is your answer.

| Gate | Check | Config |
| --- | --- | --- |
| Contact ID | Did we recognize the caller? | (logged automatically) |
| BLOCK | Are they on the block list? | `BLOCK=` |
| ALLOW | Are they on the allow list (if configured)? | `ALLOW=` |
| Quiet hours | Is it outside quiet hours? | `QUIET_HOURS=` |
| Cooldown | Did we message them recently? | `COOLDOWN_MINUTES=` |
| DRY_RUN | Is DRY_RUN off? | `DRY_RUN=true` means log only |
| Stand-down | Did you reply by hand? | (automatic check) |
| LLM failure | Did generation succeed? | (fallback text used if not) |

Check the log:
```bash
./scripts/service.sh logs
```

Or:
```bash
curl -s http://localhost:8787/recent
```

---

## What If Everything Passes but Still No Message?

The LLM failed silently (rare). Check:

```bash
grep "falling back to the fixed text" logs/call-followup.log
```

If this line appears, the model rejected its own output or LM Studio was unreachable. The fallback text was used instead. Increase `MAX_TOKENS` if the model is running out of budget.
