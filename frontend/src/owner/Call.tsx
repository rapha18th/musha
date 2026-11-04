import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Shop } from "../api";
import { ShopCall, type Caption, type Phase, type Shown } from "./shopCall";
import { Incoming, Mic, Phone, useRing } from "./Incoming";
import { cap, clock } from "./lib";
import "./call.css";

const DOING: Record<string, string> = {
  look: "Looking at the counter camera",
  requests: "Reading the requests",
  books: "Reading the books",
  stock: "Counting the stock",
};

const mmss = (ms: number) => {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

/**
 * The owner's line to her shop: Call the shop, and the shop's own ring when something needs her.
 * Answer opens the call on the reason the shop rang.
 */
export function CallShop({ shop }: { shop: Shop }) {
  const [calling, setCalling] = useState<{ ringId?: number } | null>(null);
  const { ring, info, onCall, clear, later } = useRing(shop.id);
  const [notify, setNotify] = useState(() => typeof Notification !== "undefined" && Notification.permission === "default");
  const off = info?.available === false;

  const askNotify = async () => {
    try {
      await Notification.requestPermission();
    } finally {
      setNotify(false);
    }
  };
  const close = useCallback(() => setCalling(null), []);

  return (
    <div className="c-entry">
      <button className="o-btn o-primary c-callbtn" onClick={() => setCalling({})} disabled={!!calling || onCall || off}>
        <Phone /> Call the shop
      </button>
      {off && <p className="o-fine c-entry-line">{info.line}</p>}
      {!off && notify && (
        <button className="o-textbtn c-entry-line" onClick={askNotify}>Let the shop ring this browser</button>
      )}
      {/* Both screens sit over the whole page, apart from the console's own styles. */}
      {ring && !calling && createPortal(
        <Incoming shop={shop} ring={ring}
          onAnswer={() => {
            clear(ring.id);
            setCalling({ ringId: ring.id });
          }}
          onLater={() => {
            later(ring.id);
          }}
          onMissed={() => {
            clear(ring.id);
          }} />,
        document.body,
      )}
      {calling && createPortal(<CallScreen shop={shop} ringId={calling.ringId} onClose={close} />, document.body)}
    </div>
  );
}

/** A call in progress: who and how long, the picture Musha shows, live captions, mute and end. */
export function CallScreen({ shop, ringId, onClose }: { shop: Shop; ringId?: number; onClose: () => void }) {
  const [phase, setPhase] = useState<Phase>("connecting");
  const [line, setLine] = useState("");
  const [captions, setCaptions] = useState<Caption[]>([]);
  const [shown, setShown] = useState<Shown | null>(null);
  const [act, setAct] = useState<{ speaking: boolean; tool: string | null; you: number; shop: number }>({
    speaking: false, tool: null, you: 0, shop: 0,
  });
  const [muted, setMuted] = useState(false);
  const [ending, setEnding] = useState("");
  const [span, setSpan] = useState<{ from: number | null; to: number | null }>({ from: null, to: null });
  const [now, setNow] = useState(() => Date.now());
  const callRef = useRef<ShopCall | null>(null);
  const endRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let active = true;
    const c = new ShopCall(shop.id, {
      phase: (p, l) => {
        if (!active) return;
        setPhase(p);
        if (l) setLine(l);
        if (p === "live") setSpan({ from: Date.now(), to: null });
        if (p === "ended" || p === "failed") setSpan((s) => ({ ...s, to: Date.now() }));
      },
      captions: (c2) => active && setCaptions(c2),
      shown: (s) => active && setShown(s),
      activity: (a) => active && setAct(a),
      ending: (_, l) => active && setEnding(l),
    }, ringId);
    callRef.current = c;
    // StrictMode mounts twice in development; the call starts once the page settles on one mount.
    const t = window.setTimeout(() => {
      c.start();
    }, 0);
    return () => {
      active = false;
      window.clearTimeout(t);
      c.end();
    };
  }, [shop.id, ringId]);

  useEffect(() => {
    endRef.current?.focus();
  }, []);

  useEffect(() => {
    if (phase !== "live") return;
    const t = window.setInterval(() => setNow(Date.now()), 500);
    return () => {
      window.clearInterval(t);
    };
  }, [phase]);

  const open = phase === "connecting" || phase === "live";
  const elapsed = span.from ? (span.to ?? now) - span.from : 0;
  const word = phase === "connecting" ? "Calling the shop" : phase === "live" ? (act.speaking ? "Musha is speaking" : "On a call")
    : phase === "ended" ? "Call ended" : "Call stopped";
  const doing = act.tool ? (act.tool === "ask_farai" ? `Writing to ${shop.manager.name}` : DOING[act.tool] ?? "") : "";
  const level = Math.min(1, act.shop * 6);

  const mute = () => {
    const next = !muted;
    setMuted(next);
    callRef.current?.setMuted(next);
  };
  const hangUp = () => {
    callRef.current?.end();
  };

  return (
    <div className="c-scrim" role="dialog" aria-modal="true" aria-labelledby="c-title"
      onKeyDown={(e) => {
        if (e.key === "Escape" && !open) onClose();
      }}>
      <section className={`c-sheet c-call c-${phase}`}>
        <header className="c-top">
          <p className="c-eyebrow"><span className={`c-dot c-dot-${phase}`} aria-hidden="true" />{word}</p>
          <span className="c-timer" aria-label={`Call time ${mmss(elapsed)}`}>{mmss(elapsed)}</span>
        </header>
        <h2 id="c-title" className="c-name">{shop.name}</h2>
        <p className="c-sub">{shop.place} · Musha speaks for the shop</p>

        <div className="c-stage">
          {shown ? (
            <figure className="c-shown" key={shown.url}>
              <img src={shown.url} alt={shown.line || shown.label} />
              <figcaption>
                <b>{shown.label}</b>
                {shown.line ? ` · ${cap(shown.line)}` : ""}
                {shown.at ? <span className="o-muted"> · {clock(shown.at)}</span> : null}
              </figcaption>
            </figure>
          ) : (
            <div className={`c-voice${act.speaking ? " c-on" : ""}`} style={{ ["--level" as string]: String(level) }}
              aria-hidden="true">
              <span>M</span>
            </div>
          )}
        </div>

        <div className="c-captions" aria-live="polite">
          {captions.slice(shown ? -3 : -4).map((l) => (
            <p key={l.id} className={`c-line c-from-${l.who}${l.done ? "" : " c-open"}`}>
              <span className="c-who">{l.who === "owner" ? shop.owner.name : "Shop"}</span>
              {l.text}
            </p>
          ))}
          {!captions.length && open && (
            <p className="c-line c-hint">{phase === "connecting" ? "The shop picks up in a few seconds." : "Musha opens with the shop's day."}</p>
          )}
        </div>
        <p className="c-doing" aria-live="polite">{doing ? <><span className="o-spin" aria-hidden="true" /> {doing}</> : ending}</p>
        {phase === "failed" && <p className="o-error" role="alert">{line}</p>}

        <div className="c-actions">
          {open ? (
            <>
              <div className="c-act">
                <button className={`c-round c-mute${muted ? " c-pressed" : ""}`} onClick={mute} aria-pressed={muted}
                  aria-label={muted ? "Unmute" : "Mute"} disabled={phase !== "live"}>
                  <Mic off={muted} />
                </button>
                <span aria-hidden="true">{muted ? "Muted" : "Mute"}</span>
              </div>
              <div className="c-act">
                <button ref={endRef} className="c-round c-end" onClick={hangUp} aria-label="End the call">
                  <Phone down />
                </button>
                <span aria-hidden="true">End</span>
              </div>
            </>
          ) : (
            <button className="o-btn o-ghost o-large" onClick={onClose} autoFocus>Close</button>
          )}
        </div>
      </section>
    </div>
  );
}
