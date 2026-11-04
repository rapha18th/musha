import { useCallback, useEffect, useLayoutEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { usd, type Attachment } from "../api";
import type { CheckRow, Evidence, OwnerRequest } from "./api";
import { cap, printed, titleOf, when } from "./lib";
import { checkOf, sentBefore } from "./signals";

const LABEL: Record<string, string> = {
  receipt: "Receipt",
  goods: "Goods photo",
  photo: "Photo",
  quote: "Quote",
  voice: "Voice note",
  camera: "Counter camera",
};

/** Every piece of proof behind a request: what Farai sent, then what the counter camera saw. */
export function evidenceOf(req: OwnerRequest): Evidence[] {
  const last = req.proofs[req.proofs.length - 1];
  const photo = checkOf(last, "photo");
  const dup = checkOf(last, "duplicate");
  // The engine's own finding on a piece it saw before, on another request in this shop.
  const before = (a: Attachment): CheckRow | undefined => {
    if (photo && sentBefore(photo) && photo.data?.attachment_id === a.id) return photo;
    if (dup && sentBefore(dup) && a.kind === "receipt" && a.id === last?.receipt_id) return dup;
    return undefined;
  };
  const items: Evidence[] = req.attachments.map((a: Attachment) => {
    const seen = before(a);
    return {
      key: `a${a.id}`,
      kind: a.kind,
      url: a.url,
      label: LABEL[a.kind] ?? cap(a.kind),
      at: a.uploaded_at,
      reading: a.reading,
      ...(seen ? { flag: "Sent before", flagNote: seen.reason } : {}),
    };
  });
  const cam = checkOf(last, "camera");
  const ids = (cam?.applies && Array.isArray(cam.data?.frame_ids) ? (cam.data!.frame_ids as number[]) : []).slice(0, 2);
  for (const id of ids) {
    items.push({
      key: `f${id}`,
      kind: "camera",
      url: `/api/camera/frames/${id}`,
      label: cam?.data?.staged === true ? "Counter camera, staged scene" : LABEL.camera,
      at: typeof cam?.data?.at === "string" ? (cam.data.at as string) : null,
      reading: null,
      note: cam?.reason,
    });
  }
  return items;
}

const isImage = (e: Evidence) => e.kind !== "voice" && e.kind !== "quote";

export function Thumbs({ items, onOpen }: { items: Evidence[]; onOpen: (i: number) => void }) {
  return (
    <div className="o-thumbs">
      {items.map((e, i) => (
        <button key={e.key} className={`o-thumb${e.flag ? " o-thumb-flagged" : ""}`} onClick={() => onOpen(i)}
          aria-label={`Open ${e.label}${e.flag ? `, ${e.flag.toLowerCase()}` : ""}`}>
          {isImage(e) ? <img src={e.url} alt="" loading="lazy" /> : <span className="o-thumb-glyph">{e.kind === "voice" ? "Voice" : "File"}</span>}
          {e.flag && <span className="o-thumb-flag" aria-hidden="true">{e.flag}</span>}
          <span className="o-thumb-label">{e.label}</span>
        </button>
      ))}
    </div>
  );
}

type Line = { name?: string; quantity?: number; unit_price?: number; line_total?: number };

function money(v: unknown): string {
  const n = typeof v === "number" ? v : Number(String(v ?? "").replace(/[^0-9.]/g, ""));
  return Number.isFinite(n) && String(v ?? "") !== "" ? usd(Math.round(n * 100)) : "";
}

function ReceiptReading({ r, manager, flagged }: { r: Record<string, unknown>; manager: string; flagged?: boolean }) {
  const source = String(r.source ?? "");
  const copied = typeof r.copied_from === "number";
  const by = copied ? "Read by Musha" : source === "typed" ? `Typed by ${manager}` : source === "seed" ? "From the books" : "Read by Musha";
  const lines = (Array.isArray(r.items) ? r.items : []) as Line[];
  const rows: [string, string][] = [
    ["Payee", String(r.payee ?? "")],
    ["Receipt number", String(r.receipt_no ?? "")],
    ["Date", printed(r.date, r.time)],
  ];
  return (
    <div className="o-read">
      <p className="o-eyebrow">{by}</p>
      {copied && !flagged && <p className="o-read-flag">This photo matches an earlier receipt in this shop.</p>}
      <dl className="o-dl">
        {rows.filter(([, v]) => v).map(([k, v]) => (
          <div key={k}><dt>{k}</dt><dd>{v}</dd></div>
        ))}
      </dl>
      {lines.length > 0 && (
        <table className="o-lines">
          <tbody>
            {lines.map((l, i) => (
              <tr key={i}>
                <td>{l.quantity ? `${l.quantity} × ` : ""}{l.name}</td>
                <td className="o-num">{money(l.line_total)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="o-read-total">
        <span>Total</span>
        <b>{money(r.total) || "Unreadable"}</b>
      </div>
      {r.legible === false && <p className="o-read-flag">Musha could read only part of this receipt.</p>}
    </div>
  );
}

function GoodsReading({ r }: { r: Record<string, unknown> | null }) {
  const check = (r?.check ?? null) as { seen?: boolean; count?: number; quantity_ok?: boolean; reason?: string } | null;
  if (!check) return <p className="o-muted">Musha looks at the photo when it scores the proof.</p>;
  return (
    <div className="o-read">
      <p className="o-eyebrow">What Musha saw</p>
      <p>{check.reason}</p>
      <dl className="o-dl">
        <div><dt>Goods in view</dt><dd>{check.seen ? "Yes" : "Unclear"}</dd></div>
        {check.count ? <div><dt>Count</dt><dd>About {check.count}</dd></div> : null}
        <div><dt>Quantity</dt><dd>{check.quantity_ok ? "Fits the request" : "Short of the request"}</dd></div>
      </dl>
    </div>
  );
}

function CameraReading({ r }: { r: Record<string, unknown> }) {
  const shelves = (Array.isArray(r.shelves) ? r.shelves : []) as { item: string; level: string }[];
  const d = (r.delivery ?? {}) as { seen?: boolean; goods?: string; quantity_estimate?: number };
  return (
    <div className="o-read">
      <p className="o-eyebrow">What Musha saw</p>
      <p className="o-lookin-line">{cap(String(r.line ?? ""))}</p>
      <dl className="o-dl">
        <div><dt>Shop</dt><dd>{r.open ? "Open" : "Closed"}</dd></div>
        <div><dt>People</dt><dd>{String(r.people ?? 0)}</dd></div>
        {d.seen && <div><dt>Delivery</dt><dd>{cap(d.goods ?? "")}{d.quantity_estimate ? `, about ${d.quantity_estimate}` : ""}</dd></div>}
      </dl>
      {shelves.length > 0 && (
        <ul className="o-shelves">
          {shelves.map((s) => (
            <li key={s.item} className={`o-shelf o-shelf-${s.level}`}><span>{cap(s.item)}</span><b>{s.level}</b></li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function Lightbox({ items, index, setIndex, onClose, req, manager }: {
  items: Evidence[];
  index: number;
  setIndex: (i: number) => void;
  onClose: () => void;
  req?: OwnerRequest;
  manager: string;
}) {
  const close = useRef<HTMLButtonElement>(null);
  const e = items[index];
  const step = useCallback((d: number) => {
    setIndex((index + d + items.length) % items.length);
  }, [index, items.length, setIndex]);

  // Callers pass fresh arrows on every render; the keys read the latest through refs,
  // so the effects below run once per opening and focus stays where the viewer put it.
  const onCloseRef = useRef(onClose);
  const stepRef = useRef(step);
  useLayoutEffect(() => {
    onCloseRef.current = onClose;
    stepRef.current = step;
  });

  useEffect(() => {
    close.current?.focus({ preventScroll: true });
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, []);

  useEffect(() => {
    const on = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") onCloseRef.current();
      if (ev.key === "ArrowRight") stepRef.current(1);
      if (ev.key === "ArrowLeft") stepRef.current(-1);
    };
    window.addEventListener("keydown", on);
    return () => {
      window.removeEventListener("keydown", on);
    };
  }, []);

  if (!e) return null;
  return createPortal(
    <div className="o-lightbox" role="dialog" aria-modal="true" aria-label={e.label} onClick={onClose}>
      <div className="o-lightbox-card" onClick={(ev) => ev.stopPropagation()}>
        <div className="o-lightbox-media">
          {e.kind === "voice" ? (
            <audio controls src={e.url} />
          ) : e.kind === "quote" ? (
            <a className="o-link" href={e.url} target="_blank" rel="noreferrer">Open the quote</a>
          ) : (
            <img src={e.url} alt={e.label} />
          )}
        </div>
        <aside className="o-lightbox-side">
          <div className="o-lightbox-head">
            <div>
              <h3>{e.label}</h3>
              <p className="o-muted">
                {req ? `${titleOf(req)}. ` : ""}{e.at ? cap(when(e.at)) : ""}
              </p>
            </div>
            <button ref={close} className="o-x" onClick={onClose} aria-label="Close">×</button>
          </div>
          {e.flagNote && <p className="o-read-flag">{e.flagNote}</p>}
          {e.kind === "receipt" && e.reading && <ReceiptReading r={e.reading} manager={manager} flagged={!!e.flagNote} />}
          {e.kind === "receipt" && !e.reading && <p className="o-muted">Musha reads the receipt when it scores the proof.</p>}
          {(e.kind === "goods" || e.kind === "photo") && !e.flagNote && <GoodsReading r={e.reading} />}
          {e.kind === "camera" && (e.reading ? <CameraReading r={e.reading} /> : <p>{e.note}</p>)}
          {items.length > 1 && (
            <div className="o-lightbox-nav">
              <button className="o-btn o-ghost o-small" onClick={() => step(-1)} aria-label="Previous">Previous</button>
              <span className="o-muted">{index + 1} of {items.length}</span>
              <button className="o-btn o-ghost o-small" onClick={() => step(1)} aria-label="Next">Next</button>
            </div>
          )}
        </aside>
      </div>
    </div>,
    document.body,
  );
}
