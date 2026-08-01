#!/usr/bin/env node
/**
 * WhatsApp side of the call follow-up agent.
 *
 * Uses Baileys, which speaks the WhatsApp Web protocol as your own account.
 * This is NOT an official WhatsApp API and it is against WhatsApp's terms of
 * service. Accounts using it can be banned. You picked this knowingly; the
 * README says it again.
 *
 * Setup:
 *   npm install
 *   npm start            -> scan the QR code with WhatsApp > Linked devices
 *
 * Config comes from the project-level .env, same file the Python side reads.
 * Auth lives in ../data/whatsapp-auth/ - treat that folder like a password.
 *
 * The brain (`uv run python -m app`) must be running: this process decides
 * nothing on its own, it only detects calls and sends what it is told to.
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import makeWASocket, {
  DisconnectReason,
  fetchLatestBaileysVersion,
  jidNormalizedUser,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import dotenv from 'dotenv';
import pino from 'pino';
import qrcode from 'qrcode-terminal';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, '..');
dotenv.config({ path: path.join(ROOT, '.env'), quiet: true });

const DATA_DIR = path.join(ROOT, 'data');
const AUTH_DIR = path.join(DATA_DIR, 'whatsapp-auth');
const HISTORY_PATH = path.join(DATA_DIR, 'whatsapp-history.json');

const BRAIN_URL = `http://${process.env.BRAIN_HOST || '127.0.0.1'}:${
  process.env.BRAIN_PORT || 8787
}`;
const HISTORY_MESSAGES = Number(process.env.HISTORY_MESSAGES || 10);
// How long after the call to hold off, giving you a chance to reply yourself.
const GRACE_MS = Number(process.env.REPLY_GRACE_SECONDS || 30) * 1000;

// Local time, to line up with the Python side's log. toISOString() is UTC and
// makes the two logs look hours apart when read side by side.
function stamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return (
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  );
}

const log = (...parts) => console.log(stamp(), '[whatsapp]', ...parts);

// --------------------------------------------------------------------------
// history - Baileys has no built-in store any more, so we keep a tiny one
// --------------------------------------------------------------------------

/** @type {Map<string, {name?: string, messages: {from: string, text: string}[]}>} */
const chats = new Map();
let saveTimer = null;

function loadHistory() {
  try {
    const raw = JSON.parse(fs.readFileSync(HISTORY_PATH, 'utf8'));
    for (const [jid, entry] of Object.entries(raw)) chats.set(jid, entry);
    log(`loaded history for ${chats.size} chats`);
  } catch {
    /* first run */
  }
}

function saveHistory() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    fs.mkdirSync(DATA_DIR, { recursive: true });
    const out = Object.fromEntries(chats);
    fs.writeFile(HISTORY_PATH, JSON.stringify(out), (err) => {
      if (err) log('could not save history:', err.message);
    });
  }, 2000);
}

function textOf(message) {
  if (!message) return '';
  return (
    message.conversation ||
    message.extendedTextMessage?.text ||
    message.imageMessage?.caption ||
    message.videoMessage?.caption ||
    message.documentMessage?.caption ||
    message.ephemeralMessage?.message?.conversation ||
    message.ephemeralMessage?.message?.extendedTextMessage?.text ||
    ''
  ).trim();
}

function remember(jid, from, text, pushName) {
  if (!text) return;
  const entry = chats.get(jid) || { messages: [] };
  if (pushName && from === 'them') entry.name = pushName;
  // When you answer someone yourself there is nothing left to follow up on,
  // and their unanswered run starts over.
  if (from === 'me') {
    entry.lastOutgoingAt = Date.now();
    entry.unanswered = 0;
  } else {
    entry.unanswered = (entry.unanswered || 0) + 1;
  }
  entry.messages.push({ from, text: text.slice(0, 500) });
  if (entry.messages.length > HISTORY_MESSAGES) {
    entry.messages = entry.messages.slice(-HISTORY_MESSAGES);
  }
  chats.set(jid, entry);
  saveHistory();
}

function displayName(jid) {
  return chats.get(jid)?.name || `+${jid.split('@')[0]}`;
}

/** Did you already message this person yourself since the call started? */
function repliedSince(jid, sinceMs) {
  return (chats.get(jid)?.lastOutgoingAt || 0) > sinceMs;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// --------------------------------------------------------------------------
// burst replies: "he's busy, this is an automated reply" after N unanswered
// --------------------------------------------------------------------------

const BURST_ENABLED = (process.env.BURST_REPLY_ENABLED || 'false').toLowerCase() === 'true';
const BURST_THRESHOLD = Number(process.env.BURST_THRESHOLD || 5);

/**
 * Fires once when the unanswered run hits the threshold exactly, so a long
 * conversation does not re-trigger on every further message. The run resets the
 * moment you reply, and the brain enforces its own multi-hour cooldown on top.
 */
async function maybeBurstReply(sock, jid) {
  if (!BURST_ENABLED) return;
  const entry = chats.get(jid);
  if (!entry || entry.unanswered !== BURST_THRESHOLD) return;

  const name = displayName(jid);
  log(`${entry.unanswered} unanswered messages from ${name}`);

  const decision = await askBrain(
    {
      platform: 'whatsapp',
      contact_id: jid,
      contact_name: name,
      count: entry.unanswered,
      history: entry.messages || [],
    },
    '/burst',
  );
  if (!decision.send) {
    log(`  no burst reply to ${name}: ${decision.skip_reason}`);
    return;
  }
  try {
    await sock.sendMessage(jid, { text: decision.text });
    remember(jid, 'me', decision.text);
    // remember() zeroed the run; the brain's cooldown is what prevents a repeat.
    log(`  burst reply sent to ${name}`);
  } catch (err) {
    log(`  burst reply to ${name} failed:`, err.message);
  }
}

// --------------------------------------------------------------------------
// brain
// --------------------------------------------------------------------------

async function askBrain(payload, path = '/followup') {
  try {
    const res = await fetch(`${BRAIN_URL}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(90_000),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (err) {
    log('brain unreachable:', err.message);
    return { send: false, text: '', skip_reason: 'brain unreachable' };
  }
}

// --------------------------------------------------------------------------
// socket
// --------------------------------------------------------------------------

const handledCalls = new Set();

// Baileys derives status from the call node's tag (Utils/generics.js):
//   offer/offer_notice          -> 'offer'
//   terminate + reason=timeout  -> 'timeout'   (rang out)
//   terminate (any other reason) -> 'terminate' (accepted, rejected, or the
//                                   caller hung up - the node does not say which)
//   reject                      -> 'reject'    (only when THIS client rejects)
//   accept                      -> 'accept'
//
// Rejecting on your phone reaches us as a bare 'terminate', not 'reject' -
// which is why only listening for reject/timeout caught nothing. So we track
// offers and treat any ending we never saw accepted as unanswered.
const ENDING_STATUSES = new Set(['timeout', 'reject', 'terminate']);
const REASONS = { timeout: 'missed', reject: 'rejected', terminate: 'missed' };

// Set WHATSAPP_TERMINATE_AS_MISSED=false if answering on your phone does not
// relay an 'accept' here and you start getting follow-ups after real calls.
const TERMINATE_AS_MISSED =
  (process.env.WHATSAPP_TERMINATE_AS_MISSED || 'true').toLowerCase() !== 'false';

// id -> { jid, isVideo, isGroup, accepted }
const activeCalls = new Map();

/** Calls may address you by LID rather than phone JID; prefer the phone one. */
function callerJid(call) {
  const candidates = [call.from, call.chatId].filter(Boolean).map(jidNormalizedUser);
  return candidates.find((j) => j.endsWith('@s.whatsapp.net')) || candidates[0] || '';
}

// --------------------------------------------------------------------------
// LID -> phone JID
//
// WhatsApp increasingly addresses people by an opaque LID (209384756102938@lid)
// instead of their phone JID. Calls arrive that way, which makes the caller
// unrecognisable to an allow list written in phone numbers. There is no lookup
// API for this in Baileys, so we build the map two ways: resolve the numbers in
// ALLOW/BLOCK up front via onWhatsApp(), and learn from senderLid/senderPn on
// any message that arrives.
// --------------------------------------------------------------------------

const LID_MAP_PATH = path.join(DATA_DIR, 'whatsapp-lid-map.json');
/** @type {Map<string, string>} lid jid -> phone jid */
const lidMap = new Map();

function loadLidMap() {
  try {
    const raw = JSON.parse(fs.readFileSync(LID_MAP_PATH, 'utf8'));
    for (const [lid, pn] of Object.entries(raw)) lidMap.set(lid, pn);
    if (lidMap.size) log(`loaded ${lidMap.size} LID mappings`);
  } catch {
    /* first run */
  }
}

function saveLidMap() {
  fs.mkdirSync(DATA_DIR, { recursive: true });
  fs.writeFile(LID_MAP_PATH, JSON.stringify(Object.fromEntries(lidMap)), (err) => {
    if (err) log('could not save LID map:', err.message);
  });
}

function rememberLid(lid, pn) {
  if (!lid || !pn) return;
  const lidJid = jidNormalizedUser(lid);
  const pnJid = jidNormalizedUser(pn);
  if (!lidJid.endsWith('@lid') || lidMap.get(lidJid) === pnJid) return;
  lidMap.set(lidJid, pnJid);
  saveLidMap();
  log(`learned LID mapping: ${lidJid} -> ${pnJid}`);
}

/** Turn a @lid jid into the phone jid when we know it. */
function resolveJid(jid) {
  return jid.endsWith('@lid') ? lidMap.get(jid) || jid : jid;
}

/** Phone numbers listed in ALLOW/BLOCK, so we can pre-resolve their LIDs. */
function configuredNumbers() {
  const raw = `${process.env.ALLOW || ''},${process.env.BLOCK || ''}`;
  return [...new Set(raw.split(',').map((s) => s.replace(/[^0-9]/g, '')).filter((s) => s.length >= 8))];
}

async function preresolveLids(sock) {
  const numbers = configuredNumbers();
  if (!numbers.length) return;
  try {
    const results = (await sock.onWhatsApp(...numbers)) || [];
    for (const r of results) {
      if (r?.lid && r?.jid) rememberLid(String(r.lid), String(r.jid));
    }
    log(`pre-resolved ${results.length}/${numbers.length} configured numbers to LIDs`);
  } catch (err) {
    log('LID pre-resolution failed (allow list may not match LID callers):', err.message);
  }
}

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    auth: state,
    logger: pino({ level: 'silent' }),
    markOnlineOnConnect: false, // don't steal notifications from your phone
    syncFullHistory: false,
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      console.log('\nScan this with WhatsApp > Settings > Linked devices:\n');
      qrcode.generate(qr, { small: true });
    }
    if (connection === 'open') {
      log(`connected as ${sock.user?.id?.split(':')[0] || 'unknown'}; brain at ${BRAIN_URL}`);
      preresolveLids(sock);
    }
    if (connection === 'close') {
      const status = new Boom(lastDisconnect?.error)?.output?.statusCode;
      if (status === DisconnectReason.loggedOut) {
        log('logged out on the phone. Delete ../data/whatsapp-auth and re-scan the QR.');
        process.exit(1);
      }
      log(`connection closed (${status}); reconnecting in 5s`);
      setTimeout(start, 5000);
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return;
    for (const msg of messages) {
      // Message keys carry both address forms; free LID mapping.
      rememberLid(msg.key.senderLid, msg.key.senderPn);
      const jid = resolveJid(jidNormalizedUser(msg.key.remoteJid || ''));
      if (!jid.endsWith('@s.whatsapp.net')) continue; // skip groups + status
      const fromMe = Boolean(msg.key.fromMe);
      remember(jid, fromMe ? 'me' : 'them', textOf(msg.message), msg.pushName);
      if (!fromMe) await maybeBurstReply(sock, jid);
    }
  });

  sock.ev.on('call', async (events) => {
    for (const call of events) {
      const rawJid = callerJid(call);
      const jid = resolveJid(rawJid);
      // Log every event unconditionally. Silent skips here were exactly why a
      // real missed call left no trace at all in this log.
      log(
        `call event: status=${call.status} from=${jid || '?'}` +
          `${jid !== rawJid ? ` (via ${rawJid})` : ''} id=${call.id}` +
          `${call.isVideo ? ' video' : ''}${call.isGroup ? ' group' : ''}` +
          `${call.offline ? ' offline' : ''}`,
      );

      if (call.status === 'offer') {
        activeCalls.set(call.id, {
          jid,
          isVideo: Boolean(call.isVideo),
          isGroup: Boolean(call.isGroup),
          accepted: false,
          startedAt: Date.now(),
        });
        continue;
      }
      if (call.status === 'accept') {
        const state = activeCalls.get(call.id);
        if (state) state.accepted = true;
        log(`  call answered, no follow-up`);
        continue;
      }
      if (!ENDING_STATUSES.has(call.status)) continue; // 'ringing'

      const state = activeCalls.get(call.id);
      activeCalls.delete(call.id);

      if (state?.accepted) {
        log(`  ended after being answered, no follow-up`);
        continue;
      }
      if (call.status === 'terminate' && !TERMINATE_AS_MISSED) {
        log(`  'terminate' ignored (WHATSAPP_TERMINATE_AS_MISSED=false)`);
        continue;
      }

      const reason = REASONS[call.status];
      if (state?.isGroup || call.isGroup) {
        log(`  group call, skipping`);
        continue;
      }
      if (handledCalls.has(call.id)) continue;
      handledCalls.add(call.id);
      setTimeout(() => handledCalls.delete(call.id), 10 * 60 * 1000);

      const contactJid = state?.jid || jid;
      if (!contactJid) {
        log(`  no caller jid on the event, cannot follow up`);
        continue;
      }

      const name = displayName(contactJid);
      const isVideo = state?.isVideo ?? Boolean(call.isVideo);
      const startedAt = state?.startedAt ?? Date.now();

      if (repliedSince(contactJid, startedAt)) {
        log(`  you already replied to ${name} yourself, standing down`);
        continue;
      }

      log(`  unanswered ${isVideo ? 'video ' : ''}call from ${name} (${reason})`);

      const decision = await askBrain({
        platform: 'whatsapp',
        contact_id: contactJid,
        contact_name: name,
        reason,
        video: isVideo,
        history: chats.get(contactJid)?.messages || [],
      });

      if (!decision.send) {
        log(`  not sending to ${name}: ${decision.skip_reason}`);
        continue;
      }

      // Give yourself a window to answer by hand. Generation already ate part
      // of it, so this waits for the remainder rather than adding on top.
      const remaining = GRACE_MS - (Date.now() - startedAt);
      if (remaining > 0) await sleep(remaining);

      if (repliedSince(contactJid, startedAt)) {
        log(`  you replied to ${name} while it was thinking, not sending`);
        continue;
      }

      try {
        await sock.sendMessage(contactJid, { text: decision.text });
        remember(contactJid, 'me', decision.text);
        log(`  sent to ${name}: ${decision.text}`);
      } catch (err) {
        log(`  send to ${name} failed:`, err.message);
      }
    }
  });
}

loadHistory();
loadLidMap();
start().catch((err) => {
  log('fatal:', err);
  process.exit(1);
});
