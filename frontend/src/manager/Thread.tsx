import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { rereading, usd, type Message, type Shop, type SpendRequest } from "../api";
import { Box, Receipt, Reply, Retry, Wallet } from "../phone/icons";
import { asks, clock, dayLabel, describe, languageName, PROVABLE, sameDay, statusOf } from "../phone/util";
import { MediaOf, type MediaKind } from "./Media";
import type { Local, Pending, Settled } from "./chat";
import { toneOf } from "./words";

const CAPTIONS = new Set(["Receipt", "Photo of the goods", "Voice note", "Quote", "Photo"]);
const EVIDENCE = new Set(["Receipt", "Photo of the goods", "Quote", "Photo"]);

/** Where a line the manager sent stands: on its way, with Musha while it reads, delivered, or read. */
type Seen = "sending" | "sent" | "failed" | "delivered" | "read";
const SEEN_LABEL: Record<Seen, string> = { sending: "Sending", sent: "Sent", failed: "Stopped", delivered: "Delivered", read: "Read" };

function SeenMark({ state }: { state: Seen }) {
  return <span className={`m-seen m-seen-${state}`}>{SEEN_LABEL[state]}</span>;
}

/** The manager's thread with Musha: Musha's lines, the owner's, her own, and a card for each request's status. */
export function Thread({ shop, messages, pending, settled, local, requests, typing, replyTo, onReply, onProof, onRetry, onDrop }: {
  shop: Shop;
  messages: Message[];
  pending: Pending[];
  /** Server lines that took over a pending line: they keep its key, so the line stays put. */
  settled: Settled;
  local: Map<number, Local>;
  requests: SpendRequest[];
  typing: boolean;
  replyTo: number | null;
  onReply: (m: Message) => void;
  /** Musha's card for a held request carries a button that opens Send proof. */
  onProof: (r: SpendRequest) => void;
  onRetry: (p: Pending) => void;
  onDrop: (p: Pending) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const inner = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const placed = useRef(0);
  const [english, setEnglish] = useState<Set<number>>(new Set());
  // Lines settled after this thread came on screen skip the rise: they rose once as pending lines.
  const [mounted] = useState(() => Date.now());
  const byId = useMemo(() => new Map(requests.map((r) => [r.id, r])), [requests]);
  // What each evidence file is, from the requests; a manager line's other attachment is a voice note.
  const kinds = useMemo(() => {
    const out = new Map<number, MediaKind>();
    for (const r of requests) {
      for (const a of r.attachments) {
        out.set(a.id, a.content_type.startsWith("image/") ? "image" : a.content_type.startsWith("audio/") ? "audio" : "other");
      }
    }
    return out;
  }, [requests]);

  // The newest Musha line for each request carries that request's live status.
  const statusLine = useMemo(() => {
    const out = new Map<number, number>();
    for (const m of messages) if (m.request_id && m.sender === "musha") out.set(m.request_id, m.id);
    return out;
  }, [messages]);

  // Musha has read every line the manager sent before its newest reply.
  const readTo = useMemo(() => {
    let id = 0;
    for (const m of messages) if (m.sender === "musha" && m.id > id) id = m.id;
    return id;
  }, [messages]);

  // Stay at the newest line while the reader is there; leave them be when they scroll back.
  const toBottom = () => {
    const el = box.current;
    if (el && stick.current) {
      el.scrollTop = el.scrollHeight;
      placed.current = el.scrollTop;
    }
  };
  useLayoutEffect(() => {
    toBottom();
  }, [messages.length, pending.length, typing]);
  // The lines grow (a photo loads) or the window changes: keep the newest in view.
  useEffect(() => {
    const el = inner.current;
    if (!el || !box.current || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      toBottom();
    });
    ro.observe(el);
    ro.observe(box.current);
    return () => {
      ro.disconnect();
    };
  }, []);

  const flip = (id: number) =>
    setEnglish((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const rows: ReactNode[] = [];
  const held = new Set(pending.map((p) => p.key));
  let prev: { at: string; sender: string } | null = null;
  for (const m of messages) {
    if (!prev || !sameDay(prev.at, m.at)) {
      rows.push(<div key={`d${m.id}`} className="m-day"><span>{dayLabel(m.at)}</span></div>);
    }
    const first = !prev || prev.sender !== m.sender || !sameDay(prev.at, m.at);
    const req = m.request_id ? byId.get(m.request_id) : undefined;
    const showsStatus = !!req && m.sender === "musha" && statusLine.get(req.id) === m.id;
    const asking = asks(m, req);
    const settle = settled.get(m.id);
    const own = settle && !held.has(settle.key) ? settle : undefined;
    const provable = showsStatus && !!req && PROVABLE.includes(req.status) && req.origin !== "wages";
    rows.push(
      line(own ? own.key : m.id, {
        settled: !!own && own.at >= mounted,
        m,
        shop,
        first,
        english: english.has(m.id),
        flip: () => flip(m.id),
        req: showsStatus ? req : undefined,
        local: m.attachment_id ? local.get(m.attachment_id) : undefined,
        kind: m.attachment_id ? kinds.get(m.attachment_id) ?? (EVIDENCE.has(m.text.trim()) ? undefined : "audio") : undefined,
        seen: m.id < readTo ? "read" : "delivered",
        canReply: asking,
        replying: replyTo === m.id,
        onReply: () => onReply(m),
        // A question Musha asked takes words; a request waiting for its receipt takes proof.
        onProof: provable && req && (!asking || rereading(req)) ? () => onProof(req) : undefined,
      }),
    );
    prev = { at: m.at, sender: m.sender };
  }
  for (const p of pending) {
    if (!prev || !sameDay(prev.at, p.at)) {
      rows.push(<div key={`d${p.key}`} className="m-day"><span>{dayLabel(p.at)}</span></div>);
    }
    const first = !prev || prev.sender !== "manager";
    rows.push(pendingLine(p, first, () => onRetry(p), () => onDrop(p)));
    prev = { at: p.at, sender: "manager" };
  }

  return (
    <div
      className="m-thread"
      ref={box}
      tabIndex={0}
      aria-label={`Messages with Musha${shop.whatsapp === "live" ? ", also on WhatsApp" : ""}`}
      onScroll={(e) => {
        const el = e.currentTarget;
        if (Math.abs(el.scrollTop - placed.current) < 2) return; // our own scroll, or the browser keeping it
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        placed.current = el.scrollTop;
      }}
    >
      <div className="m-thread-in" ref={inner} aria-live="polite">
        {rows.length === 0 && !typing && <p className="m-thread-empty">Write to Musha in your own words. Ask for stock, answer a question, or send a voice note.</p>}
        {rows}
        {typing && (
          <div className="m-b m-from-musha m-first m-typing" role="status" aria-label="Musha is writing">
            <span /><span /><span />
          </div>
        )}
      </div>
    </div>
  );
}

/** One line. Pending and server lines render through it with the same slots, so when a server line
 *  takes a pending line's key React keeps the element: no second rise, and the photo or note stays loaded. */
function Line({ cls, who, media, body, card, foot, fail, actions }: {
  cls: string;
  who?: ReactNode;
  media?: ReactNode;
  body?: ReactNode;
  card?: ReactNode;
  foot: ReactNode;
  fail?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className={cls}>
      {who}
      {media}
      {body}
      {card}
      {foot}
      {fail}
      {actions}
    </div>
  );
}

/** The kind of spend, drawn on the request's card. */
function kindIcon(req: SpendRequest) {
  if (req.origin === "wages") return <Wallet size={18} />;
  return req.kind === "stock" ? <Box size={18} /> : <Receipt size={18} />;
}

/** A server line. A plain function, like pendingLine, so both give Thread a Line under the same key. */
function line(key: string | number, { m, shop, first, settled, english, flip, req, local, kind, seen, canReply, replying, onReply, onProof }: {
  m: Message;
  kind?: MediaKind;
  shop: Shop;
  first: boolean;
  settled: boolean;
  english: boolean;
  flip: () => void;
  req?: SpendRequest;
  local?: Local;
  seen: Seen;
  canReply: boolean;
  replying: boolean;
  onReply: () => void;
  onProof?: () => void;
}) {
  const out = m.sender === "manager";
  const translated = !!m.text_en && m.text_en.trim() !== m.text.trim();
  const shown = english && translated ? m.text_en : m.text;
  const caption = m.attachment_url && CAPTIONS.has(m.text.trim());
  // A voice note's player says it all; its words show when Musha wrote them down.
  const bare = caption && (local?.kind ?? kind) === "audio";
  // The language this line was written in, and the one the toggle leads to.
  const own = out ? languageName(m.language) : languageName(shop.manager.language);
  const status = req && statusOf(req.status, shop.owner.name);
  // What Musha filled in, or that Musha asked for the request itself when the shelf ran low.
  const guessed = req ? guessedLine(req) || (req.origin === "musha" ? "Musha asked for it" : "") : "";
  // Once PayPal moved the money, the card shows what reached the shop.
  const released = req && (req.status === "captured" || req.status === "part_captured") && req.hold
    ? req.hold.captured_cents : null;
  const who = m.sender === "owner" ? shop.owner.name : m.sender === "musha" ? "Musha" : "";
  return (
    <Line
      key={key}
      cls={`m-b m-from-${m.sender}${first ? " m-first" : ""}${replying ? " m-replying" : ""}${settled ? " m-settled" : ""}${bare ? " m-b-voice" : ""}${m.attachment_url && !bare ? " m-b-media" : ""}`}
      who={first && who ? <span className="m-who">{who}</span> : undefined}
      media={m.attachment_url ? (
        <MediaOf url={local?.url || m.attachment_url} known={local?.kind ?? kind} knownMs={local?.ms} alt={m.text_en || m.text || "Attachment"} />
      ) : null}
      body={bare ? null : caption ? (
        <p className="m-caption">{m.text}</p>
      ) : (
        <p className="m-text" lang={english ? "en" : undefined}>{shown}</p>
      )}
      card={req && status && (
        <div className={`m-req o-tone-${toneOf(status.tone)}`}>
          <span className="m-req-icon" aria-hidden="true">{kindIcon(req)}</span>
          <span className="m-req-what">{describe(req)}</span>
          <span className="m-req-amount">{usd(released ?? req.amount_cents)}</span>
          <span className="m-req-status">
            {released != null && req.hold && req.hold.amount_cents > released
              ? <>Released<em>. {usd(req.hold.amount_cents - released)} back to {shop.owner.name}</em></>
              : req.proving ? "Musha is reading the receipt" : rereading(req) ? "Send the receipt again" : status.label}
          </span>
          {guessed && <span className="m-req-note">{guessed}</span>}
        </div>
      )}
      foot={(
        <div className="m-foot">
          {translated && !caption ? (
            <button type="button" className="m-lang" onClick={flip} aria-pressed={english}>
              {english ? own : "English"}
            </button>
          ) : null}
          <span className="m-meta">
            <time dateTime={m.at}>{clock(m.at)}</time>
            {out && <SeenMark state={seen} />}
          </span>
        </div>
      )}
      actions={(canReply || onProof) && (
        <div className="m-actions">
          {canReply && (
            <button type="button" className="o-btn o-small o-outline" onClick={onReply}>
              <Reply size={15} /> Reply
            </button>
          )}
          {onProof && req && (
            <button type="button" className="o-btn o-small o-primary" onClick={onProof}>
              <Receipt size={15} /> Send proof
            </button>
          )}
        </div>
      )}
    />
  );
}

/** What Musha filled in on a request, said once on its card while the owner decides. */
function guessedLine(req: SpendRequest & { inferred?: string[] }): string {
  const by = req.inferred || [];
  if (!["requested", "approving", "agent_pending", "reviewing"].includes(req.status)) return "";
  const amount = by.includes("amount");
  const count = by.includes("quantity");
  const payee = by.includes("payee") && !!req.payee;
  const what = count
    ? amount ? "Musha's estimate from past buys: about a week of stock" : "Musha worked out the count from the amount"
    : amount ? "Musha's estimate from past buys" : "";
  if (what && payee) return `${what}, paid to ${req.payee}.`;
  if (what) return `${what}.`;
  if (payee) return `Musha picked the payee, ${req.payee}.`;
  return "";
}

/** A line on its way. Its slots match line()'s, so its server copy takes over the same element. */
function pendingLine(p: Pending, first: boolean, onRetry: () => void, onDrop: () => void) {
  const failed = p.state === "failed";
  return (
    <Line
      key={p.key}
      cls={`m-b m-from-manager m-pending${first ? " m-first" : ""}${failed ? " m-failed" : ""}${p.kind === "voice" ? " m-b-voice" : ""}${p.kind === "image" ? " m-b-media" : ""}`}
      media={p.kind === "image" ? (p.url ? <MediaOf url={p.url} known="image" alt={p.text} /> : <div className="m-media-wait" />)
        : p.kind === "voice" && p.url ? <MediaOf url={p.url} known="audio" knownMs={p.ms} alt="Voice note" /> : null}
      body={p.kind === "text" ? <p className="m-text">{p.text}</p> : p.kind === "image" ? <p className="m-caption">{p.text}</p> : null}
      foot={(
        <div className="m-foot">
          {null}
          <span className="m-meta">
            <time dateTime={p.at}>{clock(p.at)}</time>
            <SeenMark state={failed ? "failed" : p.state === "reading" ? "sent" : "sending"} />
          </span>
        </div>
      )}
      fail={failed && (
        <div className="m-failed-row">
          <p className="m-failed-why">{p.error || "Sending stopped."}</p>
          <button type="button" className="o-textbtn o-danger" onClick={onRetry}><Retry /> Send again</button>
          <button type="button" className="o-textbtn m-drop" onClick={onDrop}>Delete</button>
        </div>
      )}
    />
  );
}
