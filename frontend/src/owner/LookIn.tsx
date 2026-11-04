import { useCallback, useEffect, useRef, useState } from "react";
import type { Shop } from "../api";
import { ownerApi, type CameraStatus, type Evidence } from "./api";
import { Lightbox } from "./Evidence";
import { ErrorLine, Spinner } from "./parts";
import { cap, clock, usePoll, when } from "./lib";
import { counterLink } from "../shell";
import { Connect } from "./Connect";
import type { Source } from "./connection";

const WAIT_MS = 120_000;
const SOURCE_LABEL: Record<Source, string> = { phone: "Counter camera", bridge: "IP camera", feed: "Video feed" };

/** The newest counter still, and a button that asks the counter phone for a fresh one. */
export function LookIn({ shop, onStatus }: { shop: Shop; onStatus?: (s: CameraStatus) => void }) {
  const [status, setStatus] = useState<CameraStatus>();
  const [waiting, setWaiting] = useState<{ since: number; from: number | null } | null>(null);
  const [fresh, setFresh] = useState(false);
  const [late, setLate] = useState(false);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const statusRef = useRef(onStatus);
  statusRef.current = onStatus;

  const poll = usePoll(async () => {
    const s = await ownerApi.camera(shop.id);
    setStatus(s);
    statusRef.current?.(s);
    if (waiting) {
      if (s.latest && s.latest.id !== waiting.from) {
        setWaiting(null);
        setFresh(true);
      } else if (Date.now() - waiting.since > WAIT_MS) {
        setWaiting(null);
        setLate(true);
      }
    }
  }, waiting ? 2000 : 15000, [shop.id, waiting]);

  useEffect(() => {
    if (!fresh) return;
    const t = window.setTimeout(() => {
      setFresh(false);
    }, 8000);
    return () => {
      window.clearTimeout(t);
    };
  }, [fresh]);

  const ask = async () => {
    setError("");
    setLate(false);
    setFresh(false);
    try {
      const s = await ownerApi.lookIn(shop.id);
      setStatus(s);
      setWaiting({ since: Date.now(), from: s.latest?.id ?? null });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const closeConnect = useCallback(() => setConnecting(false), []);

  const link = counterLink(shop.id);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
      window.setTimeout(() => {
        setCopied(false);
      }, 2000);
    } catch {
      setCopied(false);
    }
  };

  const f = status?.latest;
  const source = (status as (CameraStatus & { source?: Source | null; feed?: { on: boolean } }) | undefined);
  const label = source?.source && (source.source !== "feed" || source.feed?.on)
    ? SOURCE_LABEL[source.source] : "Counter camera";
  const item: Evidence[] = f ? [{
    key: `f${f.id}`, kind: "camera", url: f.url, label: "Counter camera", at: f.at, reading: f.reading,
    note: f.line,
  }] : [];

  return (
    <section className="o-card o-lookin" aria-label="Look in">
      <button className={`o-lookin-still${fresh ? " o-fresh" : ""}`} onClick={() => f && setOpen(true)} disabled={!f}
        aria-label="Open the counter still">
        {f ? <img key={f.id} src={f.url} alt={f.line || "The counter"} /> : <span className="o-muted">The first still lands here.</span>}
        {waiting && <span className="o-lookin-veil"><Spinner /></span>}
        {fresh && <span className="o-lookin-badge">New</span>}
      </button>
      <div className="o-lookin-text">
        <p className="o-eyebrow">
          <span className={`o-dot${status?.online ? " o-on" : ""}`} aria-hidden="true" />
          {label}{status?.online ? ", watching" : status?.seen_at ? `, last seen ${when(status.seen_at)}` : ""}
        </p>
        <p className="o-lookin-line">{f?.line ? cap(f.line) : "Waiting for the first still."}</p>
        {f && <p className="o-muted">{cap(when(f.at))}</p>}
        <div className="o-row">
          <button className="o-btn o-outline" onClick={ask} disabled={!!waiting}>
            {waiting ? <><Spinner /> Asking the counter phone</> : "Look in"}
          </button>
          <button className="o-btn o-ghost" onClick={() => setConnecting(true)} aria-haspopup="dialog">
            Connect your shop
          </button>
        </div>
        {late && (
          <p className="o-fine">The counter phone was last seen at {clock(status?.seen_at)}. It sends a still as soon as it is open.</p>
        )}
        <p className="o-fine o-lookin-link">
          <a className="o-link" href={counterLink(shop.id, false)} target="_blank" rel="noreferrer">Open the counter camera</a>
          {" "}on the phone at the counter.{" "}
          <button className="o-textbtn" onClick={copy}>{copied ? "Link copied" : "Copy the link"}</button>
        </p>
        {status?.consent_line && <p className="o-fine">{status.consent_line}</p>}
        <ErrorLine error={error} />
      </div>
      {open && f && (
        <Lightbox items={item} index={0} setIndex={() => undefined} onClose={() => setOpen(false)} manager={shop.manager.name} />
      )}
      {connecting && <Connect shop={shop} onClose={closeConnect} onChange={() => void poll()} />}
    </section>
  );
}
