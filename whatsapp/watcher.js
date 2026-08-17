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
import http from 'node:http';
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
import qrimage from 'qrcode';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, '..');
dotenv.config({ path: path.join(ROOT, '.env'), quiet: true });

const DATA_DIR = path.join(ROOT, 'data');
const AUTH_DIR = path.join(DATA_DIR, 'whatsapp-auth');
const HISTORY_PATH = path.join(DATA_DIR, 'whatsapp-history.json');
const QR_PATH = path.join(DATA_DIR, 'whatsapp-qr.png');

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

/**
 * Real content can be wrapped a few layers deep - disappearing messages, view
 * once, a document sent with a caption. Unwrap before looking at anything.
 */
function unwrap(message) {
  return (
    message?.ephemeralMessage?.message ||
    message?.viewOnceMessage?.message ||
    message?.viewOnceMessageV2?.message ||
    message?.viewOnceMessageV2Extension?.message ||
    message?.documentWithCaptionMessage?.message ||
    message ||
    null
  );
}

function textOf(message) {
  const inner = unwrap(message);
  if (!inner) return '';
  return (
    inner.conversation ||
    inner.extendedTextMessage?.text ||
    inner.imageMessage?.caption ||
    inner.videoMessage?.caption ||
    inner.documentMessage?.caption ||
    ''
  ).trim();
}

// A photo with no caption is still someone waiting on you. remember() ignores
// empty text, so media used to count for nothing at all - five stickers in a
// row registered as zero unanswered messages. These stand in for the content so
// the run is counted, and so the model can see what kind of thing arrived.
const MEDIA_LABELS = {
  imageMessage: 'photo',
  videoMessage: 'video',
  ptvMessage: 'video note',
  audioMessage: 'voice message',
  stickerMessage: 'sticker',
  documentMessage: 'document',
  contactMessage: 'contact',
  contactsArrayMessage: 'contacts',
  locationMessage: 'location',
  liveLocationMessage: 'live location',
  pollCreationMessage: 'poll',
  pollCreationMessageV2: 'poll',
  pollCreationMessageV3: 'poll',
  productMessage: 'product',
  orderMessage: 'order',
  eventMessage: 'event',
};

// Not new messages: a reaction to something you sent, a delivery receipt, an
// edit or a delete. Counting these towards a burst would fire replies at
// someone who never actually wrote to you.
const NOT_A_MESSAGE = new Set([
  'reactionMessage',
  'protocolMessage',
  'senderKeyDistributionMessage',
  'pollUpdateMessage',
  'editedMessage',
  'keepInChatMessage',
  'messageContextInfo',
]);

function describeMedia(message) {
  const inner = unwrap(message);
  if (!inner) return '';
  for (const [key, label] of Object.entries(MEDIA_LABELS)) {
    if (inner[key]) return `[${label}]`;
  }
  return '';
}

/** True for reactions, receipts, edits - things that are not a message. */
function isNotAMessage(message) {
  const inner = unwrap(message);
  if (!inner) return true;
  const keys = Object.keys(inner).filter((k) => k !== 'messageContextInfo');
  return keys.length > 0 && keys.every((k) => NOT_A_MESSAGE.has(k));
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
    entry.unread = 0;
  } else {
    entry.unanswered = (entry.unanswered || 0) + 1;
    // A sweep only looks at recent runs, so it needs to know when the last one
    // of these actually landed.
    entry.lastIncomingAt = Date.now();
  }
  entry.messages.push({ from, text: text.slice(0, 500) });
  if (entry.messages.length > HISTORY_MESSAGES) {
    entry.messages = entry.messages.slice(-HISTORY_MESSAGES);
  }
  chats.set(jid, entry);
  saveHistory();
}

// How far back a reconnect backlog is still worth counting. Long enough to
// cover a restart or a short network drop, short enough that a history sync
// cannot resurrect old conversations into the burst counter.
const APPEND_MAX_AGE_MS = 15 * 60 * 1000;

/** messageTimestamp is seconds, and may be a protobuf Long rather than a number. */
function timestampOf(msg) {
  const raw = msg.messageTimestamp;
  if (raw == null) return NaN;
  const seconds = typeof raw === 'object' && typeof raw.toNumber === 'function'
    ? raw.toNumber()
    : Number(raw);
  return Number.isFinite(seconds) ? seconds * 1000 : NaN;
}

/** Message ids we have already counted, so a redelivery cannot count twice. */
const seenMessages = new Map();
const SEEN_TTL_MS = 30 * 60 * 1000;

function alreadySeen(id) {
  if (!id) return false;
  const now = Date.now();
  if (seenMessages.size > 500) {
    for (const [key, at] of seenMessages) {
      if (now - at > SEEN_TTL_MS) seenMessages.delete(key);
    }
  }
  if (seenMessages.has(id)) return true;
  seenMessages.set(id, now);
  return false;
}

// --------------------------------------------------------------------------
// contact names
//
// Two different names exist per contact and they are not interchangeable:
// `notify` is the push name, chosen by the sender and only seen once they
// message you; `name` is what YOU saved them as in your address book. The
// address-book name is the one you would recognise ("Dr. Dinesh Shahbagh
// bkash/nagad"), and WhatsApp only hands it over in contacts events - which
// this watcher previously did not listen for at all.
// --------------------------------------------------------------------------

const CONTACTS_PATH = path.join(DATA_DIR, 'whatsapp-contacts.json');
/** @type {Map<string, string>} jid -> address-book name */
const contactNames = new Map();
let contactsTimer = null;

function loadContacts() {
  try {
    const raw = JSON.parse(fs.readFileSync(CONTACTS_PATH, 'utf8'));
    for (const [jid, name] of Object.entries(raw)) contactNames.set(jid, name);
    if (contactNames.size) log(`loaded ${contactNames.size} contact names`);
  } catch {
    /* first run */
  }
}

function saveContacts() {
  clearTimeout(contactsTimer);
  contactsTimer = setTimeout(() => {
    fs.mkdirSync(DATA_DIR, { recursive: true });
    fs.writeFile(CONTACTS_PATH, JSON.stringify(Object.fromEntries(contactNames)), (err) => {
      if (err) log('could not save contact names:', err.message);
    });
  }, 2000);
}

function rememberContact(contact) {
  if (!contact?.id) return;

  // A Contact record carries both address forms: `lid` and `jid`. That is the
  // lid -> phone direction WhatsApp exposes nowhere else, and it arrives for
  // every contact in the address book at no cost. Ignoring it meant a caller
  // could be in your contacts by name and still show as an unknown LID.
  if (contact.lid && contact.jid) rememberLid(String(contact.lid), String(contact.jid));
  const raw = jidNormalizedUser(String(contact.id));
  if (raw.endsWith('@lid') && contact.jid) rememberLid(raw, String(contact.jid));

  const jid = resolveJid(raw);
  const name = contact.name || contact.verifiedName || contact.notify || '';
  if (!jid || !name || contactNames.get(jid) === name) return;
  contactNames.set(jid, name);
  saveContacts();
}

/**
 * Unread counts, straight from WhatsApp.
 *
 * Our own `unanswered` counter only knows about messages seen while this
 * process was running, so after a restart it under-reports. `unreadCount` is
 * what the badge on your chat list shows, which is what you are actually
 * looking at when you ask who has been left hanging. A sweep takes whichever
 * is larger. Baileys uses -1 for "manually marked unread", hence the clamp.
 */
function rememberChatMeta(chat) {
  if (!chat?.id) return;
  const jid = resolveJid(jidNormalizedUser(String(chat.id)));
  if (!jid.endsWith('@s.whatsapp.net')) return;
  const entry = chats.get(jid) || { messages: [] };
  let touched = false;

  if (typeof chat.unreadCount === 'number') {
    entry.unread = Math.max(0, chat.unreadCount);
    touched = true;
  }
  // Seconds since epoch. Our own lastIncomingAt only exists for messages this
  // process watched arrive, so a chat delivered by a history sync has none -
  // and a sweep with a time window would silently drop it.
  const stamp = chat.conversationTimestamp ?? chat.lastMessageRecvTimestamp;
  const seconds = typeof stamp === 'object' && stamp !== null && typeof stamp.toNumber === 'function'
    ? stamp.toNumber()
    : Number(stamp);
  if (Number.isFinite(seconds) && seconds > 0) {
    entry.lastActivityAt = seconds * 1000;
    touched = true;
  }

  if (!touched) return;
  chats.set(jid, entry);
  saveHistory();
}

function displayName(jid) {
  const saved = contactNames.get(jid);
  if (saved) return saved;
  const known = chats.get(jid)?.name;
  if (known) return known;
  // Never dress a LID up as a phone number. `+87050648854572` looks like a
  // contact you could recognise; it is an opaque WhatsApp id that resolved to
  // nobody, and printing it with a + made an unknown caller look saved.
  if (jid.endsWith('@lid')) return `unknown caller (lid ${jid.split('@')[0]})`;
  return `+${jid.split('@')[0]}`;
}

/** Did you already message this person yourself since the call started? */
function repliedSince(jid, sinceMs) {
  return (chats.get(jid)?.lastOutgoingAt || 0) > sinceMs;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// --------------------------------------------------------------------------
// burst replies: "he's busy, this is an automated reply" after N unanswered
// --------------------------------------------------------------------------

// Whether bursts are enabled is the brain's call (and is switchable from the
// Telegram bot at runtime); this side only needs to know when to ask.
const BURST_THRESHOLD = Number(process.env.BURST_THRESHOLD || 5);

/**
 * Fires once when the unanswered run hits the threshold exactly, so a long
 * conversation does not re-trigger on every further message. The run resets the
 * moment you reply, and the brain enforces its own multi-hour cooldown on top.
 */
async function maybeBurstReply(sock, jid) {
  // No local on/off check: the brain owns that, so /burst on from the Telegram
  // bot takes effect without restarting this process. It answers `send: false`
  // when bursts are off, which costs one local HTTP call at the threshold.
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
  const count = entry.unanswered;
  const history = [...(entry.messages || [])];

  try {
    await sock.sendMessage(jid, { text: decision.text });
    remember(jid, 'me', decision.text);
    // remember() zeroed the run; the brain's cooldown is what prevents a repeat.
    log(`  burst reply sent to ${name}`);
    reportSent({
      platform: 'whatsapp',
      contact_id: jid,
      contact_name: name,
      kind: 'burst',
      text: decision.text,
      occurred_at: Date.now() / 1000,
      count,
      history,
    });
  } catch (err) {
    log(`  burst reply to ${name} failed:`, err.message);
  }
}

// --------------------------------------------------------------------------
// brain
// --------------------------------------------------------------------------

/**
 * Report a message that actually went out, so the brain can notify you.
 *
 * Called after the send, not instead of it: the brain's decision is a promise to
 * send, and a send can still fail. Fire-and-forget - a notification that does
 * not arrive must never hold up or break the watcher.
 */
function reportSent(payload) {
  fetch(`${BRAIN_URL}/sent`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(30_000),
  }).catch((err) => log('  could not report the send for notification:', err.message));
}

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
let controlServer = null;

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

/**
 * Every phone number worth pre-resolving to a LID.
 *
 * Originally this was only ALLOW/BLOCK, which meant an empty ALLOW resolved
 * nothing at all - so a call from anyone WhatsApp addressed by LID showed up as
 * an unidentifiable stranger even when they were someone you talk to daily.
 * Everyone you already have a chat with is included now: onWhatsApp() hands back
 * each number's `lid`, which is the only way to build the lid -> phone direction.
 */
function knownNumbers() {
  const fromChats = [...chats.keys()].filter((jid) => jid.endsWith('@s.whatsapp.net'));
  // The whole address book, not just people you have a chat with. A caller can
  // be saved in your contacts and still arrive as a LID, which is exactly the
  // case that looked like "unknown caller" for someone you speak to often.
  const fromContacts = [...contactNames.keys()].filter((jid) =>
    jid.endsWith('@s.whatsapp.net'),
  );
  const configured = `${process.env.ALLOW || ''},${process.env.BLOCK || ''}`
    .split(',')
    .map((s) => `${s.replace(/[^0-9]/g, '')}@s.whatsapp.net`);

  const already = new Set(lidMap.values());
  return [
    ...new Set(
      [...fromChats, ...fromContacts, ...configured]
        .filter((jid) => !already.has(jid))
        .map((jid) => jid.split('@')[0])
        .filter((digits) => digits.length >= 8),
    ),
  ];
}

async function preresolveLids(sock) {
  const numbers = knownNumbers();
  if (!numbers.length) {
    log('LID map already covers every known number');
    return;
  }
  let resolved = 0;
  // Batched and paced. onWhatsApp is one round trip, an address book is
  // hundreds of numbers, and hammering that endpoint is the kind of thing
  // WhatsApp rate-limits. Only unmapped numbers are asked about, and the map
  // is persisted, so this shrinks to nothing after the first run.
  for (let i = 0; i < numbers.length; i += 20) {
    const batch = numbers.slice(i, i + 20);
    try {
      const results = (await sock.onWhatsApp(...batch)) || [];
      for (const r of results) {
        if (r?.lid && r?.jid) {
          rememberLid(String(r.lid), String(r.jid));
          resolved += 1;
        }
      }
    } catch (err) {
      log('LID pre-resolution failed for a batch:', err.message);
    }
    if (i + 20 < numbers.length) await sleep(1000);
  }
  log(`pre-resolved ${resolved}/${numbers.length} numbers to LIDs (${lidMap.size} mapped total)`);
}

// --------------------------------------------------------------------------
// control server
//
// The brain owns every decision, but only this process holds the socket. So it
// exposes exactly two things on localhost: the chat list to decide from, and a
// way to send a message the brain has already decided on. Deliberately not a
// general-purpose API - localhost-only, and it makes no judgements of its own.
// --------------------------------------------------------------------------

const CONTROL_PORT = Number(process.env.WHATSAPP_CONTROL_PORT || 8788);

function chatList() {
  const out = [];
  for (const [jid, entry] of chats) {
    if (!jid.endsWith('@s.whatsapp.net')) continue;
    out.push({
      contact_id: jid,
      contact_name: displayName(jid),
      unanswered: entry.unanswered || 0,
      unread: entry.unread || 0,
      last_incoming_at: entry.lastIncomingAt || 0,
      last_activity_at: entry.lastActivityAt || 0,
      last_outgoing_at: entry.lastOutgoingAt || 0,
      history: entry.messages || [],
    });
  }
  return out;
}

function startControlServer(sock) {
  const server = http.createServer(async (req, res) => {
    const reply = (code, body) => {
      res.writeHead(code, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(body));
    };
    try {
      if (req.method === 'GET' && req.url === '/chats') return reply(200, { chats: chatList() });

      if (req.method === 'POST' && req.url === '/send') {
        let body = '';
        for await (const chunk of req) body += chunk;
        const { jid, text } = JSON.parse(body || '{}');
        if (!jid || !text) return reply(400, { error: 'jid and text are required' });
        await sock.sendMessage(jid, { text });
        remember(jid, 'me', text);
        log(`  sweep: sent to ${displayName(jid)}`);
        return reply(200, { ok: true });
      }
      return reply(404, { error: 'not found' });
    } catch (err) {
      log('control server error:', err.message);
      return reply(500, { error: err.message });
    }
  });
  server.on('error', (err) => log('control server could not start:', err.message));
  server.listen(CONTROL_PORT, '127.0.0.1', () =>
    log(`control server on 127.0.0.1:${CONTROL_PORT}`),
  );
  return server;
}

/**
 * Ask WhatsApp to resend the chat list and address book.
 *
 * A linked device is only sent this in full when it pairs. On every later
 * connection it gets deltas, so a long-lived session ends up knowing nothing
 * about chats it has not personally watched a message arrive in - which made a
 * sweep blind to exactly the conversations you can see waiting on your phone.
 *
 * resyncAppState re-requests those collections without re-pairing. It is the
 * same mechanism WhatsApp Web uses to recover from a stale local state.
 */
async function resyncMetadata(sock) {
  const collections = ['critical_unblock_low', 'regular_high', 'regular_low', 'regular'];
  for (const collection of collections) {
    try {
      await sock.resyncAppState([collection], true);
    } catch (err) {
      log(`app-state resync of ${collection} failed:`, err.message);
    }
  }
  const unread = [...chats.values()].filter((e) => (e.unread || 0) > 0).length;
  log(
    `app-state resync done: ${chats.size} chats known, ${contactNames.size} names, ` +
      `${unread} with unread messages`,
  );
}

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    auth: state,
    logger: pino({ level: 'silent' }),
    markOnlineOnConnect: false, // don't steal notifications from your phone
    // Asks WhatsApp for the chat list and unread state. Without it a linked
    // device only learns about chats it personally watches a message arrive
    // in, which left /sweep blind to conversations already waiting.
    syncFullHistory: true,
  });

  sock.ev.on('creds.update', saveCreds);

  // Address-book names, so a caller shows up as someone you recognise rather
  // than a bare number. Both events fire during the initial sync and whenever
  // you edit a contact on the phone.
  sock.ev.on('chats.upsert', (list) => list.forEach(rememberChatMeta));
  sock.ev.on('chats.update', (list) => list.forEach(rememberChatMeta));
  sock.ev.on('contacts.upsert', (contacts) => contacts.forEach(rememberContact));
  sock.ev.on('contacts.update', (contacts) => contacts.forEach(rememberContact));
  // With syncFullHistory off, the address book arrives in the app-state payload
  // rather than as contacts.upsert - so listen for both or get neither.
  sock.ev.on('messaging-history.set', ({ contacts, chats: list }) => {
    list?.forEach(rememberChatMeta);
    if (!contacts?.length) return;
    contacts.forEach(rememberContact);
    log(`address book: ${contacts.length} contacts synced`);
  });

  sock.ev.on('connection.update', ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      console.log('\nScan this with WhatsApp > Settings > Linked devices:\n');
      qrcode.generate(qr, { small: true });
      // Also as a PNG: an ASCII QR in a log file is awkward to scan, and this
      // one has to be scanned from a phone.
      qrimage.toFile(QR_PATH, qr, { width: 512, margin: 2 }, (err) => {
        log(err ? `could not write the QR image: ${err.message}` : `QR image written to ${QR_PATH}`);
      });
    }
    if (connection === 'open') {
      log(`connected as ${sock.user?.id?.split(':')[0] || 'unknown'}; brain at ${BRAIN_URL}`);
      resyncMetadata(sock);
      // Detached: hundreds of paced lookups must not delay call handling.
      preresolveLids(sock).catch((err) => log('LID pre-resolution stopped:', err.message));
      if (!controlServer) controlServer = startControlServer(sock);
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
    // 'notify' is a message arriving live. 'append' is the backlog WhatsApp
    // hands over after a reconnect - exactly the window every restart creates.
    // Dropping it meant messages sent while this process was down counted as
    // zero, so an 11-message run could sit at an unanswered count of 0.
    //
    // History sync arrives as 'append' too, and replaying weeks of old chats
    // through the counter would fire bursts at everybody. Hence the age check:
    // catch up on the last few minutes, ignore the archive.
    // Logged unconditionally, including what gets dropped and why - the same
    // rule the call handler follows. A message that silently never arrives is
    // indistinguishable from one that arrived and was discarded, and that
    // ambiguity cost days of guessing about why bursts never fired.
    log(`messages.upsert: type=${type} count=${messages.length}`);

    if (type !== 'notify' && type !== 'append') {
      log(`  ignoring type=${type}`);
      return;
    }

    for (const msg of messages) {
      // Message keys carry both address forms; free LID mapping.
      rememberLid(msg.key.senderLid, msg.key.senderPn);
      const raw = jidNormalizedUser(msg.key.remoteJid || '');
      const jid = resolveJid(raw);
      const fromMe = Boolean(msg.key.fromMe);
      // Media falls back to a label, so a caption-less photo still counts.
      const text = textOf(msg.message) || describeMedia(msg.message);

      if (!jid.endsWith('@s.whatsapp.net')) {
        log(`  skipped ${raw} (group or status)`);
        continue;
      }
      if (type === 'append') {
        const age = Date.now() - timestampOf(msg);
        if (!Number.isFinite(age) || age > APPEND_MAX_AGE_MS) {
          log(`  skipped ${displayName(jid)} (backlog, ${Math.round(age / 60000)}m old)`);
          continue;
        }
      }
      // The same message can arrive live and again in a reconnect backlog;
      // counting it twice would walk the burst counter past its threshold.
      if (alreadySeen(msg.key.id)) {
        log(`  skipped ${displayName(jid)} (already counted)`);
        continue;
      }
      if (!text) {
        const why = isNotAMessage(msg.message)
          ? 'reaction, receipt or edit'
          : `unrecognised type: ${Object.keys(unwrap(msg.message) || {}).join(',')}`;
        log(`  skipped ${displayName(jid)} (${why})`);
        continue;
      }

      log(`  ${fromMe ? 'you ->' : '->'} ${displayName(jid)}: ${text.slice(0, 60)}`);
      remember(jid, fromMe ? 'me' : 'them', text, msg.pushName);
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

      if (call.offline) {
        log(`  offline call event, ignoring to prevent stale/duplicate follow-ups`);
        continue;
      }

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

      // Snapshot the history before remember() appends our own reply to it, so
      // the notification card does not show the reply twice.
      const history = [...(chats.get(contactJid)?.messages || [])];

      try {
        await sock.sendMessage(contactJid, { text: decision.text });
        remember(contactJid, 'me', decision.text);
        log(`  sent to ${name}: ${decision.text}`);
        reportSent({
          platform: 'whatsapp',
          contact_id: contactJid,
          contact_name: name,
          kind: 'call',
          text: decision.text,
          occurred_at: startedAt / 1000, // the brain works in seconds
          reason,
          video: isVideo,
          history,
        });
      } catch (err) {
        log(`  send to ${name} failed:`, err.message);
      }
    }
  });
}

loadHistory();
loadLidMap();
loadContacts();
start().catch((err) => {
  log('fatal:', err);
  process.exit(1);
});
