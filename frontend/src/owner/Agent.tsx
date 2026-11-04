import { useEffect, useRef, useState } from "react";
import { PayPalSavePaymentButton } from "@paypal/react-paypal-js/sdk-v6";
import { usd, type Shop } from "../api";
import { ownerApi, type Mandate, type MandateIn } from "./api";
import { ErrorLine, Spinner, useArrived } from "./parts";
import { dollars, useAction, usePoll, when } from "./lib";

const KINDS = ["stock", "expense", "asset"] as const;
const WORD: Record<string, string> = { stock: "stock", expense: "expenses", asset: "assets" };

type Draft = { kinds: string[]; limit: string; cap: string; known: boolean; tolerance: string };

const draftOf = (m: Mandate): Draft => ({
  kinds: m.kinds,
  limit: trim(dollars(m.limit_cents)),
  cap: trim(dollars(m.weekly_cap_cents)),
  known: m.known_payees_only,
  tolerance: String(m.price_tolerance_pct),
});

/** Inline fields grow with their text where the browser supports field-sizing. */
const SIZES = typeof CSS !== "undefined" && CSS.supports("field-sizing", "content");
const trim = (s: string) => s.replace(/\.00$/, "");
const cents = (s: string) => Math.round(Number(s) * 100);
const listed = (xs: string[]) => {
  const w = KINDS.filter((k) => xs.includes(k)).map((k) => WORD[k]);
  return w.length <= 1 ? w.join("") : `${w.slice(0, -1).join(", ")} and ${w[w.length - 1]}`;
};

/** Terms that let the agent do more ask for the owner's PayPal again (the backend clears it). */
function widens(m: Mandate, d: Draft): boolean {
  return cents(d.limit) > m.limit_cents || cents(d.cap) > m.weekly_cap_cents
    || d.kinds.some((k) => !m.kinds.includes(k)) || (m.known_payees_only && !d.known)
    || Number(d.tolerance) > m.price_tolerance_pct;
}

export function Agent({ shop, sandbox, onChange }: { shop: Shop; sandbox: boolean; onChange?: () => void }) {
  const [m, setM] = useState<Mandate>();
  const [draft, setDraft] = useState<Draft>();
  const [note, setNote] = useState("");
  const dirtyRef = useRef(false);
  const save = useAction();
  const flip = useAction();
  const asks = useAction();

  const load = usePoll(async () => {
    const x = await ownerApi.mandate(shop.id);
    setM(x);
    if (!dirtyRef.current) setDraft(draftOf(x));
  }, 8000, [shop.id]);

  if (!m || !draft) return <div className="o-loading"><Spinner /> Reading the mandate</div>;

  const dirty = JSON.stringify(draft) !== JSON.stringify(draftOf(m));
  dirtyRef.current = dirty;
  const valid = Number(draft.limit) > 0 && Number(draft.cap) > 0 && cents(draft.cap) >= cents(draft.limit)
    && draft.kinds.length > 0 && Number(draft.tolerance) >= 0 && Number(draft.tolerance) <= 100
    && /^\d+(\.\d{1,2})?$/.test(draft.limit) && /^\d+(\.\d{1,2})?$/.test(draft.cap) && /^\d{1,3}$/.test(draft.tolerance);
  const wider = dirty && widens(m, draft);
  const set = (patch: Partial<Draft>) => setDraft({ ...draft, ...patch });

  const put = async (body: MandateIn) => {
    const before = m;
    const x = await ownerApi.setMandate(shop.id, body);
    dirtyRef.current = false;
    setM(x);
    setDraft(draftOf(x));
    setNote(before.saved && !x.saved ? "The new terms are wider. Save PayPal again to approve them. The agent is off until then." : "");
    onChange?.();
  };

  const state = m.ready ? { tone: "leaf", word: "On" } : m.active ? { tone: "warn", word: "On, waiting for PayPal" } : { tone: "calm", word: "Off" };

  return (
    <div className="o-agent">
      <section className={`o-focal o-mandate o-tone-${state.tone}`}>
        <div className="o-mandate-top">
          <p className="o-eyebrow"><span className={`o-dot o-tone-${state.tone}`} aria-hidden="true" />Musha's agent · {state.word}</p>
          <label className="o-switch">
            <input type="checkbox" checked={m.active} disabled={flip.busy}
              onChange={(e) => {
                const on = e.target.checked;
                flip.run(() => put({ active: on }));
              }} />
            <span className="o-switch-track" aria-hidden="true"><span /></span>
            <span className="o-switch-label">{m.active ? "On" : "Off"}</span>
          </label>
        </div>

        <p className="o-sentence">
          Musha may approve{" "}
          <KindPicker value={draft.kinds} onChange={(kinds) => set({ kinds })} />{" "}
          up to <MoneyField value={draft.limit} onChange={(limit) => set({ limit })} label="Limit for one request" />{" "}
          a request and <MoneyField value={draft.cap} onChange={(cap) => set({ cap })} label="Weekly cap" /> a week,{" "}
          <span role="button" tabIndex={0} className="o-inline o-inline-flow"
            aria-label={`Payees: ${draft.known ? "suppliers paid before" : "any supplier"}. Switch`}
            onClick={() => set({ known: !draft.known })}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                set({ known: !draft.known });
              }
            }}>
            {draft.known ? "from suppliers I have paid before" : "from any supplier"}
          </span>
          , within{" "}
          <input className="o-inline o-inline-num" value={draft.tolerance} inputMode="numeric" aria-label="Price tolerance in percent"
            style={SIZES ? undefined : { width: `${Math.max(1, draft.tolerance.length) + 0.4}ch` }} onChange={(e) => set({ tolerance: e.target.value.replace(/\D/g, "").slice(0, 3) })} />
          % of usual prices.
        </p>

        <label className="o-switch">
          <input type="checkbox" checked={m.restock} disabled={asks.busy}
            onChange={(e) => {
              const on = e.target.checked;
              asks.run(() => put({ restock: on }));
            }} />
          <span className="o-switch-track" aria-hidden="true"><span /></span>
          <span className="o-switch-label">Musha asks for stock when it runs low</span>
        </label>
        <p className="o-fine">
          {!m.restock ? "Low stock comes to you as an alert."
            : m.active ? "Musha asks for the usual pack at the usual price. Inside your terms the agent approves it. Outside them, it waits for you."
              : "Musha starts asking once the agent is on."}
        </p>

        {dirty && (
          <div className="o-mandate-save">
            <button className="o-btn o-primary" disabled={!valid || save.busy}
              onClick={() => save.run(() => put({
                kinds: draft.kinds, limit: Number(draft.limit).toFixed(2), weekly_cap: Number(draft.cap).toFixed(2),
                known_payees_only: draft.known, price_tolerance_pct: Number(draft.tolerance),
              }))}>
              {save.busy ? <Spinner /> : null}Save the terms
            </button>
            <button className="o-btn o-quiet" onClick={() => {
              dirtyRef.current = false;
              setDraft(draftOf(m));
            }}>Undo</button>
            {!valid && <span className="o-fine o-danger">The weekly cap needs to be at least the limit for one request.</span>}
            {valid && wider && m.saved && <span className="o-fine">Wider terms ask for your PayPal again.</span>}
          </div>
        )}
        {note && <p className="o-note">{note}</p>}
        <ErrorLine error={save.error || flip.error || asks.error} />

        <WeekUse used={m.week.agent_cents} cap={m.weekly_cap_cents} />
      </section>

      <SavePayPal shop={shop} m={m} sandbox={sandbox} reload={load} />

      <section className="o-section">
        <div className="o-section-head">
          <h2>Recent decisions</h2>
          <span className="o-muted o-small-text">Inside the terms, Musha approves. Outside them, Musha asks you.</span>
        </div>
        {m.decisions.length === 0 ? (
          <p className="o-muted">The agent's decisions appear here, each with its reasons.</p>
        ) : (
          <ul className="o-decisions">
            {m.decisions.map((d, i) => (
              <li key={`${d.request_id}-${d.decision}-${i}`} className="o-decision-row">
                <span className={`o-pill o-tone-${d.decision === "approved" ? "leaf" : d.decision === "waiting" ? "busy" : "clay"}`}>
                  {d.decision === "approved" ? "Approved inside" : d.decision === "waiting" ? "Waiting on PayPal" : "Asked you"}
                </span>
                <div className="o-decision-text">
                  <p>
                    <b>{d.item ? d.item[0].toUpperCase() + d.item.slice(1) : "Request"}</b>{d.amount_cents ? `, ${usd(d.amount_cents)}` : ""}{" "}
                    <span className="o-muted">· {d.origin === "musha" ? "Musha asked for it · " : ""}{when(d.at)}</span>
                  </p>
                  {d.line && <p className="o-muted">{d.line}</p>}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function WeekUse({ used, cap }: { used: number; cap: number }) {
  const on = useArrived();
  const pct = cap ? Math.min(100, (used / cap) * 100) : 0;
  return (
    <div className="o-weekuse">
      <div className="o-weekuse-bar" aria-hidden="true"><span style={{ width: on ? `${pct}%` : "0%" }} /></div>
      <p className="o-muted o-small-text">This week the agent committed {usd(used)} of {usd(cap)}. {usd(Math.max(cap - used, 0))} left.</p>
    </div>
  );
}

function MoneyField({ value, onChange, label }: { value: string; onChange: (v: string) => void; label: string }) {
  return (
    <span className="o-inline o-inline-money">
      <i>US$</i>
      <input value={value} inputMode="decimal" aria-label={label} style={SIZES ? undefined : { width: `${Math.max(1, value.length) + 0.2}ch` }}
        onChange={(e) => onChange(e.target.value.replace(/[^\d.]/g, "").slice(0, 9))} />
    </span>
  );
}

function KindPicker({ value, onChange }: { value: string[]; onChange: (v: string[]) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const on = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", on);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", on);
      document.removeEventListener("keydown", key);
    };
  }, [open]);
  return (
    <span className="o-kindpick" ref={ref}>
      <button type="button" className="o-inline o-inline-btn" aria-haspopup="true" aria-expanded={open} onClick={() => setOpen(!open)}>
        {listed(value) || "a kind you pick"}
      </button>
      {open && (
        <span className="o-pop" role="group" aria-label="Kinds Musha may approve">
          {KINDS.map((k) => (
            <label key={k} className="o-pop-row">
              <input type="checkbox" checked={value.includes(k)}
                disabled={value.length === 1 && value.includes(k)}
                onChange={(e) => onChange(e.target.checked ? [...value, k] : value.filter((x) => x !== k))} />
              <span>{WORD[k][0].toUpperCase() + WORD[k].slice(1)}</span>
            </label>
          ))}
        </span>
      )}
    </span>
  );
}

function SavePayPal({ shop, m, sandbox, reload }: { shop: Shop; m: Mandate; sandbox: boolean; reload: () => void }) {
  const { busy, error, setError, run } = useAction();
  return (
    <section className="o-section">
      <div className="o-section-head">
        <h2>PayPal for Musha</h2>
      </div>
      <div className="o-card o-vault">
        {m.saved ? (
          <div className="o-vault-saved">
            <div>
              <p className="o-eyebrow"><span className="o-dot o-tone-leaf" aria-hidden="true" />Saved</p>
              <p className="o-vault-email">{m.payer_email ?? "Your PayPal"}</p>
              <p className="o-muted o-small-text">Musha's agent holds money with this PayPal for requests inside your terms.</p>
            </div>
            <button className="o-btn o-ghost" disabled={busy} onClick={() => run(async () => {
              await ownerApi.removeVault(shop.id);
              reload();
            })}>{busy ? <Spinner /> : null}Remove</button>
          </div>
        ) : (
          <div className="o-vault-ask">
            <p>
              Save your PayPal once. PayPal shows your terms, up to {usd(m.limit_cents)} a request and {usd(m.weekly_cap_cents)} a week,
              and Musha approves inside them while you sleep.
            </p>
            {sandbox ? (
              <div className="o-paypal">
                <PayPalSavePaymentButton
                  presentationMode="auto"
                  createVaultToken={async () => {
                    const { id } = await ownerApi.setupToken(shop.id);
                    return { vaultSetupToken: id };
                  }}
                  onApprove={async (data: { vaultSetupToken: string }) => {
                    await ownerApi.paymentToken(shop.id, data.vaultSetupToken);
                    reload();
                  }}
                  onError={(e: unknown) => {
                    setError(e instanceof Error ? e.message : String(e));
                  }}
                />
              </div>
            ) : (
              <button className="o-btn o-primary" disabled={busy} onClick={() => run(async () => {
                const { id } = await ownerApi.setupToken(shop.id);
                await ownerApi.paymentToken(shop.id, id);
                reload();
              })}>{busy ? <><Spinner /> Saving</> : "Save PayPal for Musha"}</button>
            )}
          </div>
        )}
        <ErrorLine error={error} />
      </div>
    </section>
  );
}
