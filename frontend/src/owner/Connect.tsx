import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Shop } from "../api";
import { ErrorLine, Spinner } from "./parts";
import { cap, clock, usePoll, when } from "./lib";
import { counterUrl, setCounterKey } from "../shell";
import {
  BRIDGE_GUIDE, CAMERAS, bridgeCommand, connectApi, qrPath,
  type Connection, type Source,
} from "./connection";
import "./connect.css";

type Way = Source;

const WAYS: { key: Way; label: string }[] = [
  { key: "phone", label: "A phone" },
  { key: "bridge", label: "An IP camera" },
  { key: "feed", label: "A video feed" },
];

const SOURCE_NAME: Record<Source, string> = {
  phone: "The counter phone",
  bridge: "An IP camera, through the bridge",
  feed: "The sample video feed",
};

/** Connect your shop: three ways to give the counter its camera, the connection now, and Disconnect. */
export function Connect({ shop, onClose, onChange }: { shop: Shop; onClose: () => void; onChange?: () => void }) {
  const [view, setView] = useState<Connection>();
  const [way, setWay] = useState<Way | null>(null);
  const [busy, setBusy] = useState<"" | "feed" | "disconnect">("");
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState("");
  const close = useRef<HTMLButtonElement>(null);
  const changed = useRef(onChange);
  changed.current = onChange;

  usePoll(async () => {
    const v = await connectApi.view(shop.id);
    setView(v);
    setWay((w) => w ?? v.source ?? "phone");
  }, 3000, [shop.id]);

  useEffect(() => {
    const back = document.activeElement as HTMLElement | null;
    close.current?.focus({ preventScroll: true });
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const on = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", on);
    return () => {
      document.body.style.overflow = prev;
      window.removeEventListener("keydown", on);
      back?.focus?.({ preventScroll: true });
    };
  }, [onClose]);

  const act = async (what: "feed" | "disconnect") => {
    setBusy(what);
    setError("");
    try {
      const v = what === "feed" ? await connectApi.feed(shop.id) : await connectApi.disconnect(shop.id);
      setView(v);
      setConfirming(false);
      changed.current?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    setBusy("");
  };

  // After Disconnect the shop has a new counter key: every "Copy the link" in the console carries it.
  useEffect(() => {
    if (view?.counter_key) setCounterKey(shop.id, view.counter_key);
  }, [view?.counter_key, shop.id]);

  const link = view ? counterUrl(shop.id, view.counter_key) : "";

  return createPortal(
    <div className="cn-veil" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <section className="cn-sheet" role="dialog" aria-modal="true" aria-labelledby="cn-title">
        <header className="cn-head">
          <div>
            <h2 id="cn-title">Connect your shop</h2>
            <p>Give {shop.name} its eyes on the counter. One camera at a time.</p>
          </div>
          <button ref={close} type="button" className="cn-close" onClick={onClose} aria-label="Close">
            <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" /></svg>
          </button>
        </header>

        {view ? <Now view={view} confirming={confirming} setConfirming={setConfirming} busy={busy}
          disconnect={() => void act("disconnect")} /> : <div className="cn-now cn-loading"><Spinner /> Reading the connection</div>}

        <div className="cn-ways" role="tablist" aria-label="Ways to connect">
          {WAYS.map((w) => (
            <button key={w.key} type="button" role="tab" aria-selected={way === w.key} className="cn-way"
              onClick={() => setWay(w.key)}>
              {w.label}
              {view?.source === w.key && view.online && <><span className="cn-way-dot" aria-hidden="true" /><span className="cn-sr">, connected</span></>}
            </button>
          ))}
        </div>

        <div className="cn-body" role="tabpanel">
          {view && way === "phone" && <Phone link={link} manager={view.manager} />}
          {view && way === "bridge" && <Bridge link={link} manager={view.manager} agreed={!!view.consented} />}
          {view && way === "feed" && <Feed view={view} busy={busy === "feed"} connect={() => void act("feed")} />}
        </div>
        <ErrorLine error={error} />
      </section>
    </div>,
    document.body,
  );
}

function Now({ view, confirming, setConfirming, busy, disconnect }: {
  view: Connection;
  confirming: boolean;
  setConfirming: (on: boolean) => void;
  busy: string;
  disconnect: () => void;
}) {
  const f = view.latest;
  const source = view.source;
  const feedOff = source === "feed" && !view.feed.on;
  const connected = !!source && !feedOff;
  const live = connected && view.online;
  return (
    <div className="cn-now" aria-live="polite">
      <div className="cn-now-still">
        {f?.url ? <img src={f.url} alt={f.line || "The counter"} /> : <span>The first still lands here</span>}
      </div>
      <div className="cn-now-text">
        <p className="cn-eyebrow"><span className={`cn-dot${live ? " cn-on" : ""}`} aria-hidden="true" />
          {connected ? (live ? "Connected" : "Connected, quiet") : "Nothing connected"}</p>
        <p className="cn-source">{feedOff ? "The video feed stopped" : source ? SOURCE_NAME[source] : "Choose a way below"}</p>
        {f && <p className="cn-fine">Last still {when(f.at)}{f.line ? `: ${cap(f.line)}` : ""}</p>}
        {source === "feed" && view.feed.on && (
          <p className="cn-fine">A still every {view.feed.pace_seconds} seconds while this page is open.</p>
        )}
        {feedOff && view.feed.stopped_at && (
          <p className="cn-fine">It stopped at {clock(view.feed.stopped_at)} after {view.feed.idle_minutes} quiet minutes. Connect it again below.</p>
        )}
        {connected && !confirming && (
          <button type="button" className="cn-text cn-danger" onClick={() => setConfirming(true)}>Disconnect</button>
        )}
        {confirming && (
          <div className="cn-confirm">
            <p>The counter link changes. Phones and bridges holding the old one stop.</p>
            <div className="cn-row">
              <button type="button" className="o-btn o-small cn-btn-danger" onClick={disconnect} disabled={busy === "disconnect"}>
                {busy === "disconnect" ? <><Spinner /> Disconnecting</> : "Disconnect"}
              </button>
              <button type="button" className="o-btn o-small o-ghost" onClick={() => setConfirming(false)}>Keep it</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function useCopy(): [string, (key: string, text: string) => void] {
  const [copied, setCopied] = useState("");
  const copy = (key: string, text: string) => {
    navigator.clipboard?.writeText(text).then(() => {
      setCopied(key);
      window.setTimeout(() => setCopied(""), 1800);
    }).catch(() => {});
  };
  return [copied, copy];
}

function Phone({ link, manager }: { link: string; manager: string }) {
  const code = useMemo(() => (link ? qrPath(link) : null), [link]);
  const [copied, copy] = useCopy();
  return (
    <div className="cn-phone">
      <figure className="cn-qr">
        {code && (
          <svg viewBox={`0 0 ${code.size} ${code.size}`} role="img" aria-label="Code for the counter link" shapeRendering="crispEdges">
            <rect width={code.size} height={code.size} fill="#ffffff" />
            <path d={code.d} fill="#14110d" />
          </svg>
        )}
      </figure>
      <div className="cn-steps">
        <ol>
          <li>Scan the code with the phone that stays at the counter. An old Android phone with Chrome is enough.</li>
          <li>{manager} reads the terms and presses <b>Start the camera</b>.</li>
          <li>Prop it on a charger, facing the counter and the door.</li>
        </ol>
        <p className="cn-mono">{link}</p>
        <div className="cn-row">
          <button type="button" className="o-btn o-small o-outline" onClick={() => copy("link", link)}>
            {copied === "link" ? "Link copied" : "Copy the link"}
          </button>
          <a className="o-btn o-small o-ghost" href={link} target="_blank" rel="noreferrer">Open it here</a>
        </div>
      </div>
    </div>
  );
}

function Bridge({ link, manager, agreed }: { link: string; manager: string; agreed: boolean }) {
  const [kind, setKind] = useState(CAMERAS[0].key);
  const [copied, copy] = useCopy();
  const camera = CAMERAS.find((c) => c.key === kind) ?? CAMERAS[0];
  const parts = bridgeCommand(link, camera.rtsp, agreed ? null : manager);
  const command = parts.join(" ");
  return (
    <ol className="cn-bridge">
      <li>
        <p>Copy the <b>counter-bridge</b> folder from the repo to a computer beside the camera: a Raspberry Pi, a mini PC or a laptop on the same network.</p>
        <pre className="cn-cmd">pip install -r requirements.txt</pre>
      </li>
      <li>
        <p>Choose the camera, then put its own address, user and password in the RTSP address.</p>
        <div className="cn-kinds" role="radiogroup" aria-label="Camera">
          {CAMERAS.map((c) => (
            <button key={c.key} type="button" role="radio" aria-checked={kind === c.key} className="cn-kind" onClick={() => setKind(c.key)}>
              {c.label}
            </button>
          ))}
        </div>
        <pre className="cn-cmd">
          {parts.map((part, i) => (
            <Fragment key={i}>{i ? " " : ""}<span className={part.length > 24 ? "cn-long" : "cn-tok"}>{part}</span></Fragment>
          ))}
        </pre>
        <div className="cn-row">
          <button type="button" className="o-btn o-small o-outline" onClick={() => copy("cmd", command)}>
            {copied === "cmd" ? "Command copied" : "Copy the command"}
          </button>
          <a className="cn-text" href={BRIDGE_GUIDE} target="_blank" rel="noreferrer">The bridge guide</a>
        </div>
      </li>
      <li>
        <p>
          {agreed ? `${manager} has agreed to the counter camera. ` : `The first run prints the terms, and --agree records ${manager}'s yes. `}
          A line such as <code>still sent (first)</code> means the shop can see.
        </p>
      </li>
    </ol>
  );
}

function Feed({ view, busy, connect }: { view: Connection; busy: boolean; connect: () => void }) {
  const on = view.feed.on;
  return (
    <div className="cn-feed">
      <p className="cn-lede">Footage of a shop counter plays through Musha's own camera path. Musha reads each still as it reads the counter phone's.</p>
      <ul className="cn-facts">
        <li>{view.feed.stills} stills, one every {view.feed.pace_seconds} seconds, looping.</li>
        <li>It runs while this console or a counter page is open, and stops itself after {view.feed.idle_minutes} quiet minutes.</li>
        <li>Each still is read once. Later loops reuse the reading.</li>
        <li>Footage counts in the sandbox shop alone, the way staged scenes count.</li>
      </ul>
      {on ? (
        <p className="cn-on-line"><span className="cn-dot cn-on" aria-hidden="true" /> The feed is on since {clock(view.feed.since)}. Still {view.feed.next} of {view.feed.stills} is next.</p>
      ) : (
        <div className="cn-row">
          <button type="button" className="o-btn o-primary" onClick={connect} disabled={busy}>
            {busy ? <><Spinner /> Connecting</> : "Connect the video feed"}
          </button>
        </div>
      )}
      {view.feed.credit && <p className="cn-fine">{view.feed.credit}</p>}
    </div>
  );
}
