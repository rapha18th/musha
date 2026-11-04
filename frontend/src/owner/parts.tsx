import { useEffect, useId, useState } from "react";
import { PayPalOneTimePaymentButton } from "@paypal/react-paypal-js/sdk-v6";
import { api, usd } from "../api";
import { ownerApi, type OwnerRequest } from "./api";
import { KIND_LABEL, statusOf, useAction } from "./lib";
import { centsOf, foreignCurrency } from "./signals";

/** True one frame after mount, so CSS transitions run from their empty state. */
export function useArrived(): boolean {
  const [on, setOn] = useState(false);
  useEffect(() => {
    const id = window.requestAnimationFrame(() => {
      setOn(true);
    });
    return () => {
      window.cancelAnimationFrame(id);
    };
  }, []);
  return on;
}

export const band = (score: number) => (score >= 85 ? "leaf" : score >= 60 ? "warn" : "danger");

export function ScoreRing({ score, size = 72 }: { score: number; size?: number }) {
  const on = useArrived();
  const r = 15.5;
  const c = 2 * Math.PI * r;
  const shown = on ? Math.max(0, Math.min(100, score)) : 0;
  return (
    <div className={`o-ring o-tone-${band(score)}`} style={{ width: size, height: size }} role="img" aria-label={`Score ${score} of 100`}>
      <svg viewBox="0 0 36 36" aria-hidden="true">
        <circle className="o-ring-track" cx="18" cy="18" r={r} />
        <circle className="o-ring-fill" cx="18" cy="18" r={r} strokeDasharray={c} strokeDashoffset={c * (1 - shown / 100)} />
      </svg>
      <span className="o-ring-num" style={{ fontSize: Math.round(size * 0.3) }}>{score}</span>
    </div>
  );
}

/** The held money split into what reached the shop and what went back to the owner. `bare` leaves the words to the caller. */
export function MoneyBar({ released, returned, owner, bare, to = "the shop" }: {
  released: number;
  returned: number;
  owner?: string;
  bare?: boolean;
  /** Where the released money went: the shop, or the manager for wages. */
  to?: string;
}) {
  const on = useArrived();
  const total = released + returned || 1;
  const pct = (released / total) * 100;
  return (
    <div className="o-money">
      <div className="o-money-bar" aria-hidden="true">
        <span className="o-money-in" style={{ width: on ? `${pct}%` : "0%" }} />
        <span className="o-money-back" style={{ width: on ? `${100 - pct}%` : "0%" }} />
      </div>
      {!bare && (
        <p className="o-money-line">
          {released > 0 && <span className="o-leaf"><b>{usd(released)}</b> {to === "the shop" ? "released to the shop" : `paid to ${to}`}.</span>}
          {returned > 0 && <span> <b>{usd(returned)}</b> back to {owner ? owner : "you"}.</span>}
        </p>
      )}
    </div>
  );
}

export function StatusPill({ status }: { status: string }) {
  const s = statusOf(status);
  return <span className={`o-pill o-tone-${s.tone}`}>{s.label}</span>;
}

export function KindChip({ kind }: { kind: string }) {
  return <span className="o-kind">{KIND_LABEL[kind] ?? kind}</span>;
}

export function Spinner() {
  return <span className="o-spin" aria-hidden="true" />;
}

export function ErrorLine({ error }: { error: string }) {
  if (!error) return null;
  return <p className="o-error" role="alert">{error}</p>;
}

/** What the owner just did, with the figures for the card that confirms it. Cents throughout. */
export type Outcome = {
  action: "approve" | "release" | "return";
  req: OwnerRequest;
  before: string;
  held: number;
  released: number;
  returned: number;
};

const approved = (before: OwnerRequest, after: OwnerRequest): Outcome => ({
  action: "approve", req: after, before: before.status,
  held: after.hold?.amount_cents ?? after.amount_cents, released: 0, returned: 0,
});

/** PayPal's button in sandbox mode; a plain button that runs the same calls in rehearsal. */
export function Approve({ req, sandbox, refresh, big, onDone }: {
  req: OwnerRequest;
  sandbox: boolean;
  refresh: () => Promise<void>;
  big?: boolean;
  onDone?: (o: Outcome) => void;
}) {
  const { busy, error, setError, run } = useAction();
  if (sandbox) {
    return (
      <div className="o-approve">
        <div className="o-paypal">
          <PayPalOneTimePaymentButton
            presentationMode="auto"
            createOrder={async () => {
              const { order_id } = await api.order(req.id);
              return { orderId: order_id };
            }}
            onApprove={async () => {
              const after = await api.authorize(req.id);
              onDone?.(approved(req, after));
              await refresh();
            }}
            onError={(e: unknown) => {
              setError(e instanceof Error ? e.message : String(e));
            }}
          />
        </div>
        <p className="o-fine">{req.origin === "wages"
          ? `PayPal holds ${usd(req.amount_cents)}, then Musha pays it out.`
          : `PayPal holds ${usd(req.amount_cents)}. Proof releases it.`}</p>
        <ErrorLine error={error} />
      </div>
    );
  }
  return (
    <div className="o-approve">
      <button
        className={`o-btn o-primary${big ? " o-large" : ""}`}
        disabled={busy}
        onClick={() => run(async () => {
          await api.order(req.id);
          const after = await api.authorize(req.id);
          onDone?.(approved(req, after));
          await refresh();
        })}
      >
        {busy ? <><Spinner /> Holding {usd(req.amount_cents)}</> : <>Approve {usd(req.amount_cents)}</>}
      </button>
      <p className="o-fine">{req.origin === "wages"
        ? "PayPal holds the money, then Musha pays it out."
        : "PayPal holds the money. Proof releases it."}</p>
      <ErrorLine error={error} />
    </div>
  );
}

/**
 * The owner's own hand on a flagged proof: release the receipt total (capped at the hold) or return all.
 * A receipt in another currency records nothing spent, so the owner types the dollar amount to release.
 */
export function Decide({ req, refresh, onDone }: { req: OwnerRequest; refresh: () => Promise<void>; onDone?: (o: Outcome) => void }) {
  const { busy, error, run } = useAction();
  const [which, setWhich] = useState<"release" | "return" | "">("");
  const [typed, setTyped] = useState("");
  // The focal card and the request card can both show this form: each copy gets its own ids.
  const uid = useId();
  const held = req.hold?.amount_cents ?? req.amount_cents;
  const last = req.proofs[req.proofs.length - 1];
  const foreign = foreignCurrency(req, last);
  const cents = typed.trim() ? centsOf(typed) : null;
  const fits = cents !== null && cents > 0 && cents <= held;
  const release = foreign ? (fits ? cents : 0) : Math.min(last?.spent_cents || held, held);
  const go = (action: "release" | "return") => {
    if (action === "release" && release <= 0) return;
    setWhich(action);
    run(async () => {
      const v = await ownerApi.decide(req.id, action, action === "release" ? (release / 100).toFixed(2) : undefined);
      const out = action === "release" ? release : 0;
      onDone?.({ action, req: v.request, before: req.status, held, released: out, returned: held - out });
      await refresh();
    });
  };
  const field = `decide-${req.id}-${uid.replace(/[^\w-]/g, "")}`;
  return (
    <form className="o-decide" onSubmit={(e) => {
      e.preventDefault();
      go("release");
    }}>
      {foreign && (
        <div className="o-convert">
          <p className="o-convert-why" id={`${field}-why`}>
            The receipt is in {foreign}. Type what the {req.item} cost in US dollars.
          </p>
          <label className="o-field" htmlFor={field}>
            <span>Release to the shop</span>
            <span className="o-money-input">
              <i>US$</i>
              <input id={field} value={typed} inputMode="decimal" autoComplete="off" placeholder="0.00"
                aria-describedby={`${field}-why`} aria-invalid={!!typed.trim() && !fits}
                onChange={(e) => setTyped(e.target.value)} />
            </span>
          </label>
          {typed.trim() && !fits && (
            <p className="o-fine o-danger o-convert-hint">Type an amount from US$0.01 to {usd(held)}.</p>
          )}
        </div>
      )}
      <div className="o-row">
        <button type="submit" className="o-btn o-primary" disabled={busy || release <= 0}>
          {busy && which === "release" ? <Spinner /> : null}{release > 0 ? `Release ${usd(release)}` : "Release"}
        </button>
        <button type="button" className="o-btn o-ghost" disabled={busy} onClick={() => go("return")}>
          {busy && which === "return" ? <Spinner /> : null}Return all
        </button>
      </div>
      {release > 0 && release < held && <p className="o-fine">The other {usd(held - release)} goes back to you.</p>}
      <ErrorLine error={error} />
    </form>
  );
}

/** A figure with its label, and a short note under it. */
export function Tile({ label, value, tone, note }: { label: string; value: string; tone?: "leaf" | "danger" | "warn"; note?: string }) {
  return (
    <div className={`o-tile${tone ? ` o-tile-${tone}` : ""}`}>
      <span className="o-tile-label">{label}</span>
      <span className="o-tile-value">{value}</span>
      {note && <span className="o-tile-note">{note}</span>}
    </div>
  );
}
