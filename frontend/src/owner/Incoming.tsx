import { useCallback, useEffect, useRef, useState } from "react";
import type { Shop } from "../api";
import { callApi, tone, type Ring, type RingPoll } from "./shopCall";

const RING_MS = 45_000; // the server counts a ring unanswered after this long

/** A handset; `down` turns it for hanging up. */
export function Phone({ down = false }: { down?: boolean }) {
  return (
    <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" style={down ? { transform: "rotate(135deg)" } : undefined}>
      <path fill="currentColor" d="M6.6 10.8a15.1 15.1 0 0 0 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.1.4 2.3.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1A17 17 0 0 1 3 4c0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.5.6 3.6.1.3 0 .7-.2 1l-2.3 2.2Z" />
    </svg>
  );
}

/** A microphone; `off` strikes it through. */
export function Mic({ off = false }: { off?: boolean }) {
  return (
    <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
      {off && <path d="M4 4l16 16" />}
    </svg>
  );
}
const POLL_MS = 4000;
const HIDDEN_POLL_MS = 12_000;

/**
 * Watches for the shop's ring: a held receipt, a day closed with a cash gap, or the counter camera seeing the
 * shop close. Polls while the console is open, faster while the tab is in view, and once more as it comes back.
 */
export function useRing(shopId: number) {
  const [poll, setPoll] = useState<RingPoll>();
  const [gone, setGone] = useState<Set<number>>(() => new Set());
  const told = useRef(new Set<number>());

  useEffect(() => {
    let live = true;
    let timer = 0;
    let busy = false;
    const tick = async () => {
      if (busy) return;
      busy = true;
      window.clearTimeout(timer);
      try {
        const next = await callApi.ring(shopId);
        if (live) setPoll(next);
      } catch {
        // the next poll tries again
      } finally {
        busy = false;
        if (live) timer = window.setTimeout(tick, document.hidden ? HIDDEN_POLL_MS : POLL_MS);
      }
    };
    tick();
    const wake = () => {
      if (!document.hidden) tick();
    };
    document.addEventListener("visibilitychange", wake);
    return () => {
      live = false;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", wake);
    };
  }, [shopId]);

  const r = poll?.ring;
  const ring = r && r.state === "ringing" && !gone.has(r.id) ? r : null;

  // With the tab out of view, the ring also reaches the system's notifications, when the owner allowed them.
  useEffect(() => {
    if (!ring || told.current.has(ring.id)) return;
    told.current.add(ring.id);
    if (!document.hidden || typeof Notification === "undefined" || Notification.permission !== "granted") return;
    try {
      const n = new Notification("Your shop is calling", { body: ring.reason, tag: `musha-ring-${ring.id}`, icon: "/favicon.svg" });
      n.onclick = () => {
        window.focus();
        n.close();
      };
    } catch {
      // a browser that shows notifications from a service worker alone keeps the ring in the page
    }
  }, [ring]);

  const clear = useCallback((id: number) => {
    setGone((g) => new Set(g).add(id));
  }, []);
  const later = useCallback(async (id: number) => {
    clear(id);
    await callApi.later(shopId, id).catch(() => undefined);
  }, [clear, shopId]);

  return { ring, info: poll?.call, onCall: !!poll?.on_call, clear, later };
}

/** The shop is calling: who, why in one line, its picture, and Answer or Later. A tone rings in the page. */
export function Incoming({ shop, ring, onAnswer, onLater, onMissed }: {
  shop: Shop;
  ring: Ring;
  onAnswer: () => void;
  onLater: () => void;
  /** The ring rang out unanswered. */
  onMissed: () => void;
}) {
  const answerRef = useRef<HTMLButtonElement>(null);
  const missed = useRef(onMissed);
  missed.current = onMissed;
  useEffect(() => {
    answerRef.current?.focus();
  }, []);

  // A tone, two short rings and a pause, as a phone rings in Leicester. A page that has yet to be touched starts
  // it on the first touch.
  useEffect(() => {
    let ctx: AudioContext | null = null;
    let stop: (() => void) | null = null;
    try {
      ctx = new AudioContext();
      stop = tone(ctx, 0.12);
    } catch {
      ctx = null;
    }
    const wake = () => {
      ctx?.resume().catch(() => undefined);
    };
    window.addEventListener("pointerdown", wake, { once: true });
    return () => {
      window.removeEventListener("pointerdown", wake);
      stop?.();
      window.setTimeout(() => ctx?.close().catch(() => undefined), 200);
    };
  }, []);

  // A ring that rang out goes quietly; the alert stays on Today.
  useEffect(() => {
    const left = RING_MS - (Date.now() - new Date(ring.at).getTime());
    const t = window.setTimeout(() => missed.current(), Math.max(1000, left));
    return () => {
      window.clearTimeout(t);
    };
  }, [ring.at]);

  return (
    <div className="c-scrim" role="alertdialog" aria-modal="true" aria-labelledby="c-in-title" aria-describedby="c-in-reason"
      onKeyDown={(e) => {
        if (e.key === "Escape") onLater();
      }}>
      <section className="c-sheet c-incoming">
        <p className="c-eyebrow"><span className="c-dot c-dot-ring" aria-hidden="true" />Incoming call</p>
        <h2 id="c-in-title" className="c-name">{shop.name}</h2>
        <p className="c-sub">{shop.place} · Musha speaks for the shop</p>
        {ring.picture && (
          <figure className="c-in-pic">
            <img src={ring.picture} alt={ring.kind === "proof" ? "The receipt" : "The counter camera"} />
          </figure>
        )}
        <p id="c-in-reason" className="c-reason">{ring.reason}</p>
        <div className="c-actions">
          <div className="c-act">
            <button className="c-round c-later" onClick={onLater} aria-label="Later">
              <Phone down />
            </button>
            <span aria-hidden="true">Later</span>
          </div>
          <div className="c-act">
            <button ref={answerRef} className="c-round c-answer" onClick={onAnswer} aria-label="Answer">
              <Phone />
            </button>
            <span aria-hidden="true">Answer</span>
          </div>
        </div>
      </section>
    </div>
  );
}
