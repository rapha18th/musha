import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, type Shop } from "../api";
import { hasCounterKey, useCounterLink } from "../shell";
import { phone, SCENES, type CameraStatus, type Frame } from "./api";
import { Camera, Link } from "./icons";
import { clock } from "./util";

// The pixel gate: 32 by 24 grey pixels, compared by mean absolute difference.
const GATE_W = 32;
const GATE_H = 24;
const GATE_MAD = 6; // grey levels, after the exposure shift is taken out
const TICK_MS = 1000; // the gate looks once a second
const MIN_GAP_MS = 8000; // a change sends at most this often
const FORCE_MS = 3 * 60 * 1000; // and a still goes at least every three minutes
const BEAT_MS = 5000; // the heartbeat, which also carries the owner's Look in
const STILL_W = 640;

const consentKey = (shopId: number) => `musha.counter.consent.${shopId}`;

/** The screen wake lock. The browser lets go of it whenever the page hides. */
type Sentinel = EventTarget & { released: boolean; release: () => Promise<void> };
type Waking = Navigator & { wakeLock?: { request: (t: "screen") => Promise<Sentinel> } };

function readConsent(shopId: number): boolean {
  try {
    return !!window.localStorage.getItem(consentKey(shopId));
  } catch {
    return false;
  }
}

function keepConsent(shopId: number, on = true) {
  try {
    if (on) window.localStorage.setItem(consentKey(shopId), new Date().toISOString());
    else window.localStorage.removeItem(consentKey(shopId));
  } catch {
    // a private window keeps the consent for this visit
  }
}

/** The picture as 32 x 24 grey levels. */
function grey(ctx: CanvasRenderingContext2D, video: HTMLVideoElement): Uint8ClampedArray {
  ctx.drawImage(video, 0, 0, GATE_W, GATE_H);
  const px = ctx.getImageData(0, 0, GATE_W, GATE_H).data;
  const out = new Uint8ClampedArray(GATE_W * GATE_H);
  for (let i = 0; i < out.length; i++) {
    out[i] = 0.299 * px[i * 4] + 0.587 * px[i * 4 + 1] + 0.114 * px[i * 4 + 2];
  }
  return out;
}

/** Mean absolute difference, with the median brightness shift (the phone's exposure) taken out. */
function moved(a: Uint8ClampedArray, b: Uint8ClampedArray): number {
  const diffs = new Array<number>(a.length);
  for (let i = 0; i < a.length; i++) diffs[i] = b[i] - a[i];
  const shift = [...diffs].sort((x, y) => x - y)[(diffs.length - 1) >> 1];
  let sum = 0;
  for (const d of diffs) sum += Math.abs(d - shift);
  return sum / diffs.length;
}

function still(video: HTMLVideoElement): Promise<Blob> {
  const w = Math.min(STILL_W, video.videoWidth || STILL_W);
  const h = Math.round(w * ((video.videoHeight || 480) / (video.videoWidth || 640)));
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  c.getContext("2d")!.drawImage(video, 0, 0, w, h);
  return new Promise((ok, fail) => c.toBlob((b) => (b ? ok(b) : fail(new Error("The camera gave an empty still. Start it again."))), "image/jpeg", 0.8));
}

type Mode = "off" | "starting" | "live";

/** The counter phone: consent, the live camera with its pixel gate, Look in, and staged scenes. */
export function CounterCam({ shopId, embedded = false }: { shopId: number; embedded?: boolean }) {
  const [shop, setShop] = useState<Shop>();
  const [status, setStatus] = useState<CameraStatus>();
  const [consented, setConsented] = useState(() => readConsent(shopId));
  const [mode, setMode] = useState<Mode>("off");
  const [error, setError] = useState("");
  const [latest, setLatest] = useState<Frame | null>(null);
  const [read, setRead] = useState<Frame | null>(null); // the newest still Musha has read
  const [lastSent, setLastSent] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [lookedIn, setLookedIn] = useState<string | null>(null);
  const [staging, setStaging] = useState("");
  const [copied, setCopied] = useState(false);
  const [viewing, setViewing] = useState(false);
  // The owner pressed Disconnect, and the shop's counter key changed: this page's link opens nothing more.
  const [replaced, setReplaced] = useState(false);

  const video = useRef<HTMLVideoElement>(null);
  const stream = useRef<MediaStream | null>(null);
  const wake = useRef<Sentinel | null>(null);
  const gate = useRef<{ base: Uint8ClampedArray | null; sentAt: number; busy: boolean }>({ base: null, sentAt: 0, busy: false });
  const starting = useRef(false);
  const alive = useRef(true);
  // When this page last spoke to the camera record. The server counts any still or beat
  // as the counter phone being on, so for two minutes after that the "on" is this page's own.
  const own = useRef(0);
  const stopRef = useRef<() => void>(() => {});

  // A still the server left unkept (the same picture as the newest one) only marks the time it went.
  const take = useCallback((f: Frame) => {
    const id = f.id;
    if (id == null) return;
    setLatest((old) => (!old || id >= (old.id ?? 0) ? f : old));
    if (f.line) setRead((old) => (!old || id >= (old.id ?? 0) ? f : old));
  }, []);

  // The server keeps the manager's agreement. A phone that agreed on a shop since forgotten asks again.
  const forget = useCallback(() => {
    keepConsent(shopId, false);
    setConsented(false);
  }, [shopId]);

  // The shop and the camera's record, kept fresh while the page is open.
  useEffect(() => {
    let live = true;
    const shut = (e: unknown) => {
      if (live && e instanceof ApiError && e.status === 403) setReplaced(true);
    };
    api.shop(shopId).then((s) => {
      if (live) setShop(s);
    }).catch(shut);
    const load = () => {
      phone.camera(shopId).then((s) => {
        if (!live) return;
        setStatus(s);
        if (s.consented === false && !stream.current) forget();
        if (s.latest) take(s.latest);
      }).catch(shut);
    };
    load();
    phone.frames(shopId, 24).then((fs) => {
      const f = fs.find((x) => x.line);
      if (live && f) take(f);
    }).catch(() => {});
    const t = setInterval(load, 15000);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, [shopId, take, forget]);

  const send = useCallback(async (kind: "sample" | "look_in") => {
    const v = video.current;
    const g = gate.current;
    if (!v || g.busy || !v.videoWidth) return;
    g.busy = true;
    g.sentAt = Date.now();
    setSending(true);
    try {
      const f = await phone.frame(shopId, await still(v), kind);
      own.current = Date.now();
      setLastSent(f.at);
      if (f.kept !== false) take(f);
      if (kind === "look_in") setLookedIn(f.at);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // the server has no agreement on record: the terms come back on screen
        stopRef.current();
        forget();
      }
      if (e instanceof ApiError && e.status === 403) {
        stopRef.current(); // the counter key changed: the camera stops, and the page says why
        setReplaced(true);
      }
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      g.busy = false;
      setSending(false);
    }
  }, [shopId, take, forget]);

  // Keep the screen on while the camera watches. Asked again each time the page comes back.
  const lock = useCallback(async () => {
    const wl = (navigator as Waking).wakeLock;
    if (!wl || (wake.current && !wake.current.released)) return;
    try {
      const w = await wl.request("screen");
      if (!stream.current || !alive.current || (wake.current && !wake.current.released)) {
        w.release().catch(() => {});
        return;
      }
      wake.current = w;
      w.addEventListener("release", () => {
        if (wake.current === w) wake.current = null;
      });
    } catch {
      // the screen may sleep; the camera keeps sending while it is awake
    }
  }, []);

  const stop = useCallback(() => {
    stream.current?.getTracks().forEach((t) => t.stop());
    stream.current = null;
    wake.current?.release().catch(() => {});
    wake.current = null;
    gate.current = { base: null, sentAt: 0, busy: false };
    if (video.current) video.current.srcObject = null;
    setMode("off");
  }, []);
  stopRef.current = stop;

  const start = useCallback(async () => {
    if (starting.current || stream.current) return;
    setError("");
    if (!navigator.mediaDevices?.getUserMedia) {
      setError("Open this page over https to use the camera. The staged scenes work anywhere.");
      return;
    }
    setMode("starting");
    starting.current = true;
    try {
      // Pressing Start is the manager's agreement to the terms on screen; the server keeps it.
      setStatus(await phone.agree(shopId));
      keepConsent(shopId);
      setConsented(true);
    } catch (e) {
      starting.current = false;
      setMode("off");
      setError(e instanceof Error ? e.message : String(e));
      return;
    }
    try {
      const s = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 960 } },
        audio: false,
      });
      starting.current = false;
      if (!alive.current) {
        s.getTracks().forEach((t) => t.stop());
        return;
      }
      stream.current = s;
      const v = video.current;
      if (v) {
        v.srcObject = s;
        await v.play().catch(() => {});
      }
      setMode("live");
      await lock();
    } catch (e) {
      starting.current = false;
      const name = e instanceof DOMException ? e.name : "";
      setError(name === "NotAllowedError"
        ? "The browser blocked the camera. Allow it in the site settings, then start again."
        : name === "NotFoundError"
          ? "This device lacks a camera. Use a staged scene below."
          : "Another app holds the camera. Close it and start again.");
      stop();
    }
  }, [shopId, stop, lock]);

  // The stream meets the video once the live layout is on screen.
  useEffect(() => {
    const v = video.current;
    if (mode === "live" && v && stream.current && v.srcObject !== stream.current) {
      v.srcObject = stream.current;
      v.play().catch(() => {});
    }
  }, [mode]);

  // Watching: the gate once a second, the heartbeat every five.
  useEffect(() => {
    if (mode !== "live") return;
    const c = document.createElement("canvas");
    c.width = GATE_W;
    c.height = GATE_H;
    const ctx = c.getContext("2d", { willReadFrequently: true })!;
    const tick = setInterval(() => {
      const v = video.current;
      const g = gate.current;
      if (!v || !v.videoWidth || g.busy) return;
      const now = Date.now();
      const pic = grey(ctx, v);
      const changed = !g.base || moved(g.base, pic) >= GATE_MAD;
      if ((changed && now - g.sentAt >= MIN_GAP_MS) || now - g.sentAt >= FORCE_MS) {
        g.base = pic;
        void send("sample");
      }
    }, TICK_MS);
    const beat = async () => {
      try {
        const b = await phone.heartbeat(shopId);
        own.current = Date.now();
        if (b.look_in_requested) await send("look_in");
      } catch {
        // the next beat tries again
      }
    };
    void beat();
    const hb = setInterval(beat, BEAT_MS);
    const back = () => {
      if (document.visibilityState === "visible" && stream.current) void lock();
    };
    document.addEventListener("visibilitychange", back);
    return () => {
      clearInterval(tick);
      clearInterval(hb);
      document.removeEventListener("visibilitychange", back);
    };
  }, [mode, send, shopId, lock]);

  // The page that was watching before a reload watches again, once the server confirms the agreement;
  // the phone tab waits for a tap.
  const agreed = status?.consented;
  useEffect(() => {
    if (!embedded && agreed && readConsent(shopId)) void start();
  }, [embedded, shopId, start, agreed]);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      stream.current?.getTracks().forEach((t) => t.stop());
      stream.current = null;
      wake.current?.release().catch(() => {});
      wake.current = null;
    };
  }, []);

  const stage = async (scene: string) => {
    setStaging(scene);
    setError("");
    try {
      take(await phone.staged(shopId, scene));
      own.current = Date.now();
      setViewing(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    setStaging("");
  };

  // The counter key is the owner's to hand out. A page that holds it offers the link to another phone;
  // the manager's dashboard points to the owner's Connect your shop.
  const link = useCounterLink(shopId, false);
  const keyed = hasCounterKey(shopId);
  const copy = () => {
    navigator.clipboard?.writeText(link).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    }).catch(() => {});
  };

  const owner = shop?.owner.name || "The owner";
  const live = mode === "live";
  const line = read?.line || "";
  const elsewhere = !!status?.online && !!status.seen_at && Date.now() - own.current > 120_000;
  const statusLine = live
    ? sending ? "Sending a still" : lastSent ? `Watching. Last sent ${clock(lastSent)}` : "Watching. The first still is on its way"
    : elsewhere ? `The counter phone is on. Last seen ${clock(status!.seen_at!)}` : latest ? `Camera off. Last still ${clock(latest.at)}` : "Camera off";

  if (replaced) {
    return (
      <div className={`cc cc-consent${embedded ? " cc-embedded" : ""}`}>
        <div className="cc-consent-in" role="status">
          <span className="cc-badge"><Camera size={28} /></span>
          <h2>Counter camera</h2>
          <p className="cc-consent-text">The owner gave this shop a new counter link. Ask for it, or scan the new code in Connect your shop.</p>
        </div>
      </div>
    );
  }

  if (!consented && mode === "off" && !viewing) {
    return (
      <div className={`cc cc-consent${embedded ? " cc-embedded" : ""}`}>
        <div className="cc-consent-in">
          <span className="cc-badge"><Camera size={28} /></span>
          <h2>Counter camera</h2>
          {shop && <p className="cc-place">{shop.name}, {shop.place}</p>}
          <p className="cc-consent-text">{status?.consent || "Loading the terms"}</p>
          <button type="button" className="cc-start" onClick={() => void start()} disabled={!status}>Start the camera</button>
          <p className="cc-fine">Musha sends a still when the picture changes, and one every few minutes. {owner} can look in any time.</p>
          {error && <p className="cc-error">{error}</p>}
        </div>
        {shop?.sandbox && <Scenes stage={stage} staging={staging} />}
        {embedded && <OpenOnPhone link={keyed ? link : ""} owner={owner} copy={copy} copied={copied} />}
      </div>
    );
  }

  return (
    <div className={`cc cc-on${embedded ? " cc-embedded" : ""}`}>
      <div className={`cc-picture${live ? " live" : ""}`}>
        <video ref={video} playsInline muted autoPlay aria-label="The counter, live" />
        {!live && latest && <img src={latest.url || undefined} alt={latest.line || "The latest still from the counter"} />}
        {!live && !latest && <div className="cc-blank"><Camera size={30} /></div>}
        {live && <span className="cc-live"><i /> Watching</span>}
        {!live && latest && <span className="cc-tag">{latest.kind === "staged" ? "Staged" : latest.kind === "feed" ? "Video feed" : latest.kind === "look_in" ? "Look in" : "Still"} · {clock(latest.at)}</span>}
        {live && latest && <img className="cc-last" src={latest.url || undefined} alt="The last still sent" />}
      </div>

      <div className="cc-reading">
        <p className="cc-line">{line || "Waiting for the first reading"}</p>
        {read && <span className="cc-when">Musha read this at {clock(read.at)}{read.kind === "staged" ? ", from a staged scene" : read.kind === "feed" ? ", from the video feed" : ""}</span>}
        {read?.reading && <Facts frame={read} />}
        <p className={`cc-status${live ? " on" : ""}`}><i />{statusLine}</p>
        {lookedIn && <p className="cc-lookin">{owner} looked in. A fresh still went at {clock(lookedIn)}.</p>}
        {error && <p className="cc-error">{error}</p>}
      </div>

      <div className="cc-actions">
        {!consented && !live && status && <p className="cc-consent-mini">{status.consent}</p>}
        {live ? (
          <button type="button" className="cc-ghost" onClick={stop}>Stop the camera</button>
        ) : (
          <button type="button" className="cc-start" onClick={() => void start()} disabled={mode === "starting"}>
            {mode === "starting" ? "Starting" : "Start the camera"}
          </button>
        )}
      </div>
      {shop?.sandbox && <Scenes stage={stage} staging={staging} />}
      {embedded && <OpenOnPhone link={keyed ? link : ""} owner={owner} copy={copy} copied={copied} />}
    </div>
  );
}

function Facts({ frame }: { frame: Frame }) {
  const r = frame.reading!;
  const shelves = r.shelves.filter((s) => s.level === "low");
  if (!r.delivery.seen && !shelves.length && !r.people) return null;
  return (
    <ul className="cc-facts">
      {r.people > 0 && <li>{r.people === 1 ? "1 person" : `${r.people} people`}</li>}
      {r.delivery.seen && (
        <li className="cc-fact-in">
          {r.delivery.goods || "Goods"} arriving{r.delivery.quantity_estimate ? `, about ${r.delivery.quantity_estimate}` : ""}
        </li>
      )}
      {shelves.map((s) => (
        <li key={s.item} className="cc-fact-low">{s.item} shelf low</li>
      ))}
    </ul>
  );
}

/** Staged scenes: stills the sandbox shop's counter can send without a camera. The server takes them there alone. */
function Scenes({ stage, staging }: { stage: (scene: string) => void; staging: string }) {
  return (
    <div className="cc-scenes">
      <span>Staged scenes</span>
      <div>
        {SCENES.map((s) => (
          <button key={s.key} type="button" onClick={() => stage(s.key)} disabled={!!staging} aria-busy={staging === s.key}>
            {staging === s.key ? "Sending" : s.label}
          </button>
        ))}
      </div>
    </div>
  );
}

/** The counter link for a phone that stays at the counter. Without the key, the owner's sheet has it. */
function OpenOnPhone({ link, owner, copy, copied }: { link: string; owner: string; copy: () => void; copied: boolean }) {
  if (!link) {
    return <p className="cc-open cc-open-line">{owner} gives the counter phone its own link in Connect your shop.</p>;
  }
  return (
    <div className="cc-open">
      <a href={link} target="_blank" rel="noreferrer"><Link /> Open the counter page on a phone</a>
      <button type="button" onClick={copy}>{copied ? "Copied" : "Copy link"}</button>
    </div>
  );
}
