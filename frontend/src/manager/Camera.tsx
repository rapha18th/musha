import { lazy, Suspense, useCallback, useState } from "react";
import type { Shop } from "../api";
import { phone, SCENES, type CameraStatus, type Frame } from "../phone/api";
import { cap, usePoll, when } from "../owner/lib";
import { Lightbox } from "./Media";

// Connect your shop is the shop's one sheet, shared with the owner's console; it loads the first time the manager opens it.
const Connect = lazy(() => import("../owner/Connect").then((m) => ({ default: m.Connect })));

const KIND: Record<Frame["kind"], string> = { sample: "Still", look_in: "Look in", staged: "Staged scene", feed: "Video feed" };

/**
 * The counter camera, as the shop's camera sees it: whether it watches, the newest still and Musha's reading
 * of it, and the stills before. The camera itself is a phone or an IP camera at the counter; Connect your shop
 * opens the shop's Connect sheet with the code it scans.
 */
export function Camera({ shop }: { shop: Shop }) {
  const [status, setStatus] = useState<CameraStatus>();
  const [frames, setFrames] = useState<Frame[]>([]);
  const [connecting, setConnecting] = useState(false);
  const [shown, setShown] = useState<Frame | null>(null);
  const [staging, setStaging] = useState("");
  const [error, setError] = useState("");

  const load = usePoll(async () => {
    const [s, fs] = await Promise.all([phone.camera(shop.id), phone.frames(shop.id, 12)]);
    setStatus(s);
    setFrames(fs.filter((f) => f.id != null && f.url));
  }, 10000, [shop.id]);

  const closeConnect = useCallback(() => setConnecting(false), []);

  const stage = async (scene: string) => {
    setStaging(scene);
    setError("");
    try {
      await phone.staged(shop.id, scene);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    setStaging("");
  };

  const f = status?.latest ?? frames[0] ?? null;
  const read = frames.find((x) => x.line) ?? (f?.line ? f : null);
  const r = read?.reading;
  const shelves = r ? r.shelves.filter((s) => s.level === "low") : [];
  const watching = !!status?.online;

  return (
    <div className="m-camera">
      <section className="o-card o-lookin m-cam" aria-label="Counter camera">
        <button type="button" className="o-lookin-still" onClick={() => f?.url && setShown(f)} disabled={!f?.url}
          aria-label="Open the newest still">
          {f?.url ? <img key={f.id ?? f.at} src={f.url} alt={f.line || "The counter"} /> : <span className="o-muted">The first still lands here.</span>}
          {f && <span className="m-cam-tag">{KIND[f.kind] ?? "Still"}</span>}
        </button>
        <div className="o-lookin-text">
          <p className="o-eyebrow">
            <span className={`o-dot${watching ? " o-on" : ""}`} aria-hidden="true" />
            {!status ? "Counter camera" : watching ? "Counter camera, watching"
              : status.seen_at ? `Counter camera, last seen ${when(status.seen_at)}` : "Counter camera, waiting for its first still"}
          </p>
          <p className="o-lookin-line">{read?.line ? cap(read.line) : "Musha reads each still into one line."}</p>
          {read && <p className="o-muted">Musha read this {when(read.at)}{read.kind === "staged" ? ", from a staged scene" : read.kind === "feed" ? ", from the video feed" : ""}.</p>}
          {r && (r.people > 0 || r.delivery.seen || shelves.length > 0) && (
            <ul className="m-facts">
              {r.people > 0 && <li>{r.people === 1 ? "1 person" : `${r.people} people`}</li>}
              {r.delivery.seen && (
                <li className="m-fact-in">
                  {r.delivery.goods || "Goods"} arriving{r.delivery.quantity_estimate ? `, about ${r.delivery.quantity_estimate}` : ""}
                </li>
              )}
              {shelves.map((s) => <li key={s.item} className="m-fact-low">{cap(s.item)} shelf low</li>)}
            </ul>
          )}
          <div className="o-row">
            <button type="button" className="o-btn o-primary" onClick={() => setConnecting(true)} aria-haspopup="dialog">Connect your shop</button>
          </div>
          {status && (
            <p className="o-fine">
              {status.consented
                ? `You agreed to the counter camera${status.consent_at ? ` ${when(status.consent_at)}` : ""}. ${shop.owner.name} can look in any time.`
                : "The camera's terms show on the counter page. Its first still waits for your yes."}
            </p>
          )}
          {error && <p className="o-error" role="alert">{error}</p>}
        </div>
      </section>

      {shop.sandbox && (
        <section className="o-section">
          <div className="o-section-head">
            <h2>Staged scenes</h2>
            <span className="o-muted o-small-text">Ready stills of the sandbox shop's counter. Musha reads each one.</span>
          </div>
          <div className="m-scenes">
            {SCENES.map((s) => (
              <button key={s.key} type="button" className="o-btn o-ghost" onClick={() => void stage(s.key)} disabled={!!staging} aria-busy={staging === s.key}>
                {staging === s.key ? <><span className="o-spin" aria-hidden="true" /> Sending</> : s.label}
              </button>
            ))}
          </div>
        </section>
      )}

      {frames.length > 0 && (
        <section className="o-section">
          <div className="o-section-head">
            <h2>Recent stills</h2>
            <span className="o-muted o-small-text">Musha keeps a still that shows a delivery for 30 days.</span>
          </div>
          <ul className="m-stills">
            {frames.map((x) => (
              <li key={x.id ?? x.at}>
                <button type="button" className="m-still" onClick={() => setShown(x)} aria-label={`Open the still from ${when(x.at)}`}>
                  <img src={x.url || undefined} alt="" loading="lazy" />
                  <span className="m-still-text">
                    <b>{when(x.at)}</b>
                    <small>{x.line ? cap(x.line) : KIND[x.kind] ?? "Still"}</small>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {shown?.url && (
        <Lightbox src={shown.url} alt={shown.line || "A still from the counter"}
          caption={`${cap(when(shown.at))}${shown.line ? `. ${cap(shown.line)}` : ""}`} close={() => setShown(null)} />
      )}
      {connecting && (
        <Suspense fallback={null}>
          <Connect shopId={shop.id} role="manager" onClose={closeConnect} onChange={() => void load()} />
        </Suspense>
      )}
    </div>
  );
}
