import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { rereading, usd, type Message, type Shop, type SpendRequest } from "../api";
import { Clock, Reply, Retry, Ticks } from "./icons";
import { MediaOf, type MediaKind } from "./Media";
import { asks, clock, dayLabel, describe, languageName, sameDay, statusOf } from "./util";

export type Pending = {
  key: string;
  kind: "text" | "voice" | "image";
  text: string;
  since: number;
  at: string;
  state: "sending" | "reading" | "failed";
  error?: string;
  requestId?: number;
  blob?: Blob;
  url?: string;
  ms?: number;
};

/** A local file shown for a server line while the server's copy loads. */
export type Local = { url: string; kind: MediaKind; ms?: number };

const CAPTIONS = new Set(["Receipt", "Photo of the goods", "Voice note", "Quote", "Photo"]);
const EVIDENCE = new Set(["Receipt", "Photo of the goods", "Quote", "Photo"]);

export function Thread({ shop, messages, pending, settled, local, requests, typing, replyTo, onReply, onRetry, onDrop }: {
  shop: Shop;
  messages: Message[];
  pending: Pending[];
  /** Server lines that took over a pending line: they keep its key, so the bubble stays put. */
  settled: Map<number, { key: string; at: number }>;
  local: Map<number, Local>;
  requests: SpendRequest[];
  typing: boolean;
  replyTo: number | null;
  onReply: (m: Message) => void;
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
  // The lines grow (a photo loads) or the window shrinks (the phone fits the screen): keep the newest in view.
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
      rows.push(<div key={`d${m.id}`} className="ph-day"><span>{dayLabel(m.at)}</span></div>);
    }
    const first = !prev || prev.sender !== m.sender || !sameDay(prev.at, m.at);
    const req = m.request_id ? byId.get(m.request_id) : undefined;
    const showsStatus = !!req && m.sender === "musha" && statusLine.get(req.id) === m.id;
    const asking = asks(m, req);
    const settle = settled.get(m.id);
    const own = settle && !held.has(settle.key) ? settle : undefined;
    rows.push(
      bubble(own ? own.key : m.id, {
        settled: !!own && own.at >= mounted,
        m,
        shop,
        first,
        english: english.has(m.id),
        flip: () => flip(m.id),
        req: showsStatus ? req : undefined,
        local: m.attachment_id ? local.get(m.attachment_id) : undefined,
        kind: m.attachment_id ? kinds.get(m.attachment_id) ?? (EVIDENCE.has(m.text.trim()) ? undefined : "audio") : undefined,
        canReply: asking,
        replying: replyTo === m.id,
        onReply: () => onReply(m),
      }),
    );
    prev = { at: m.at, sender: m.sender };
  }
  for (const p of pending) {
    if (!prev || !sameDay(prev.at, p.at)) {
      rows.push(<div key={`d${p.key}`} className="ph-day"><span>{dayLabel(p.at)}</span></div>);
    }
    const first = !prev || prev.sender !== "manager";
    rows.push(pendingBubble(p, first, () => onRetry(p), () => onDrop(p)));
    prev = { at: p.at, sender: "manager" };
  }

  return (
    <div
      className="ph-thread"
      ref={box}
      onScroll={(e) => {
        const el = e.currentTarget;
        if (Math.abs(el.scrollTop - placed.current) < 2) return; // our own scroll, or the browser keeping it
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        placed.current = el.scrollTop;
      }}
      aria-live="polite"
    >
      <div className="ph-thread-in" ref={inner}>
        {rows}
        {typing && (
          <div className="ph-b ph-in ph-first ph-typing" aria-label="Musha is typing">
            <span /><span /><span />
          </div>
        )}
      </div>
    </div>
  );
}

/** One bubble. Pending and server lines render through it with the same slots, so when a server line
 *  takes a pending line's key React keeps the element: no second rise, and the photo or note stays loaded. */
function Line({ cls, name, media, body, card, foot, fail }: {
  cls: string;
  name?: ReactNode;
  media?: ReactNode;
  body?: ReactNode;
  card?: ReactNode;
  foot: ReactNode;
  fail?: ReactNode;
}) {
  return (
    <div className={cls}>
      {name}
      {media}
      {body}
      {card}
      {foot}
      {fail}
    </div>
  );
}

/** A server line. A plain function, like pendingBubble, so both give Thread a Line under the same key. */
function bubble(key: string | number, { m, shop, first, settled, english, flip, req, local, kind, canReply, replying, onReply }: {
  m: Message;
  kind?: MediaKind;
  shop: Shop;
  first: boolean;
  settled: boolean;
  english: boolean;
  flip: () => void;
  req?: SpendRequest;
  local?: Local;
  canReply: boolean;
  replying: boolean;
  onReply: () => void;
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
  const guessed = req ? guessedLine(req) : "";
  // Once PayPal moved the money, the card shows what reached the shop.
  const released = req && (req.status === "captured" || req.status === "part_captured") && req.hold
    ? req.hold.captured_cents : null;
  return (
    <Line
      key={key}
      cls={`ph-b ${out ? "ph-out" : "ph-in"}${first ? " ph-first" : ""}${replying ? " ph-replying" : ""}${settled ? " ph-settled" : ""}`}
      name={m.sender === "owner" && first && <span className="ph-name">{shop.owner.name}</span>}
      media={m.attachment_url ? (
        <MediaOf
          url={local?.url || m.attachment_url}
          known={local?.kind ?? kind}
          knownMs={local?.ms}
          alt={m.text_en || m.text || "Attachment"}
        />
      ) : null}
      body={bare ? null : caption ? (
        <p className="ph-caption">{m.text}</p>
      ) : (
        <p className="ph-text" lang={english ? "en" : undefined}>{shown}</p>
      )}
      card={req && status && (
        <div className={`ph-req ph-tone-${status.tone}`}>
          <span className="ph-req-what">{describe(req)}</span>
          <span className="ph-req-amount">{usd(released ?? req.amount_cents)}</span>
          <span className="ph-req-status">
            {released != null && req.hold && req.hold.amount_cents > released
              ? <>Released<em>. {usd(req.hold.amount_cents - released)} back to {shop.owner.name}</em></>
              : req.proving ? "Musha is reading the receipt" : rereading(req) ? "Send the receipt again" : status.label}
          </span>
          {guessed && <span className="ph-req-note">{guessed}</span>}
        </div>
      )}
      foot={(
        <div className="ph-foot">
          {translated && !caption && (
            <button type="button" className="ph-lang" onClick={flip} aria-pressed={english}>
              {english ? own : "English"}
            </button>
          )}
          {canReply && (
            <button type="button" className="ph-lang ph-reply-btn" onClick={onReply}>
              <Reply /> Reply
            </button>
          )}
          <time dateTime={m.at}>{clock(m.at)}</time>
          {out && <span className="ph-ticks"><Ticks /></span>}
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

/** A line on its way. Its slots match Bubble's, so its server copy takes over the same element. */
function pendingBubble(p: Pending, first: boolean, onRetry: () => void, onDrop: () => void) {
  const failed = p.state === "failed";
  return (
    <Line
      key={p.key}
      cls={`ph-b ph-out ph-pending${first ? " ph-first" : ""}${failed ? " ph-failed" : ""}`}
      media={p.kind === "image" ? (p.url ? <MediaOf url={p.url} known="image" alt={p.text} /> : <div className="ph-media-wait" />)
        : p.kind === "voice" && p.url ? <MediaOf url={p.url} known="audio" knownMs={p.ms} alt="Voice note" /> : null}
      body={p.kind === "text" ? <p className="ph-text">{p.text}</p> : p.kind === "image" ? <p className="ph-caption">{p.text}</p> : null}
      foot={(
        <div className="ph-foot">
          {null}
          {null}
          <time dateTime={p.at}>{clock(p.at)}</time>
          <span className="ph-ticks">{failed ? null : <Clock />}</span>
        </div>
      )}
      fail={failed && (
        <div className="ph-failed-row">
          <p className="ph-failed-why">{p.error || "Sending stopped."}</p>
          <button type="button" className="ph-retry" onClick={onRetry}><Retry /> Send again</button>
          <button type="button" className="ph-retry ph-drop" onClick={onDrop}>Delete</button>
        </div>
      )}
    />
  );
}
