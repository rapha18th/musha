import { useMemo, useState } from "react";
import { rereading, usd, type Message, type Shop, type SpendRequest } from "../api";
import type { Alert, Product, Week } from "../phone/api";
import { asks, cap, describe, statusOf, todayUtc, wantsNumbers } from "../phone/util";
import { day, when } from "../owner/lib";
import { qty } from "../words";
import { LinkWhatsApp } from "./LinkWhatsApp";
import { daysLeft, SETTLED, toneOf } from "./words";

const DAY_MS = 24 * 60 * 60 * 1000;
const SETTLED_SHOWN = 3;

/** Something that waits on the manager, in the order Today offers it. */
export type Need =
  | { kind: "question"; req: SpendRequest; line?: Message }
  | { kind: "asked"; alert: Alert; line?: Message }
  | { kind: "note"; line: Message }
  | { kind: "proof"; req: SpendRequest }
  | { kind: "low"; product: Product }
  | { kind: "close" };

export type Actions = {
  ask: (item?: string) => void;
  proof: (req: SpendRequest) => void;
  close: () => void;
  /** Open Messages with the message box ready; with a Musha line, the next words answer it. */
  answer: (line?: Message) => void;
};

const stamp = (v: unknown) => (typeof v === "string" ? Date.parse(v) : NaN);

/** "2 days of eggs left", "1 day of bread left", from the shelf's days left at this pace. */
const daysOf = (name: string, left: number | null) => cap(`${daysLeft(left)} of ${name} left`);

/** The owner's questions on her alerts from the last day that wait on the manager's answer. */
export function ownerAsks(alerts: Alert[]): Alert[] {
  const now = Date.now();
  return alerts.filter((a) => {
    if (a.waiting !== undefined) return a.waiting && !a.dismissed;
    const at = stamp(a.data.asked_at);
    if (a.dismissed || !Number.isFinite(at) || now - at > DAY_MS) return false;
    const answered = stamp(a.data.answered_at);
    return !Number.isFinite(answered) || answered < at;
  });
}

/** Everything that waits on the manager, most urgent first. */
export function needsOf({ reqs, messages, alerts, products, week }: {
  reqs: SpendRequest[];
  messages: Message[];
  alerts: Alert[];
  products: Product[];
  week?: Week;
}): Need[] {
  const out: Need[] = [];
  // Musha's questions about a receipt: the newest line that asks it, in her own language.
  for (const r of reqs.filter((x) => x.status === "asking")) {
    const line = [...messages].reverse().find((m) => asks(m, r));
    out.push({ kind: "question", req: r, line });
  }
  // The owner's questions, put to the manager from an alert on her console.
  const byId = new Map(messages.map((m) => [m.id, m]));
  for (const a of ownerAsks(alerts)) {
    const id = typeof a.data.asked_message_id === "number" ? a.data.asked_message_id : undefined;
    out.push({ kind: "asked", alert: a, line: id ? byId.get(id) : undefined });
  }
  // The owner's own words, while the manager has written nothing since.
  const lastOwner = [...messages].reverse().find((m) => m.sender === "owner");
  if (lastOwner && Date.now() - Date.parse(lastOwner.at) < DAY_MS
    && !messages.some((m) => m.sender === "manager" && m.id > lastOwner.id)) {
    out.push({ kind: "note", line: lastOwner });
  }
  for (const r of reqs.filter((x) => x.status === "held" && x.origin !== "wages").sort((a, b) => a.id - b.id)) {
    out.push({ kind: "proof", req: r });
  }
  const open = new Set(reqs.filter((r) => !SETTLED.includes(r.status)).map((r) => r.item.trim().toLowerCase()));
  for (const p of [...products].filter((x) => x.low && !open.has(x.name.trim().toLowerCase()))
    .sort((a, b) => (a.days_left ?? 99) - (b.days_left ?? 99))) {
    out.push({ kind: "low", product: p });
  }
  const last = week?.cash[week.cash.length - 1];
  if (week && !(last && last.day === todayUtc() && last.closed)) out.push({ kind: "close" });
  return out;
}

/** Needs that call for the manager's hand now; the day's close and low stock can wait for the evening. */
export const urgent = (needs: Need[]) => needs.some((n) => n.kind === "question" || n.kind === "asked" || n.kind === "note" || n.kind === "proof");

/** Today: the one thing that needs the manager now, what waits behind it, and her requests. */
export function Today({ shop, needs, reqs, week, actions }: {
  shop: Shop;
  needs: Need[];
  reqs: SpendRequest[];
  week?: Week;
  actions: Actions;
}) {
  const [allDone, setAllDone] = useState(false);
  const focus = needs[0];
  const rest = needs.slice(1);
  const open = useMemo(() => reqs.filter((r) => !SETTLED.includes(r.status)), [reqs]);
  const done = useMemo(() => reqs.filter((r) => SETTLED.includes(r.status)), [reqs]);

  return (
    <div className="m-today">
      {focus ? <Focal need={focus} shop={shop} actions={actions} /> : <Calm shop={shop} week={week} />}

      {rest.length > 0 && (
        <ul className="o-next" aria-label="Also waiting">
          {rest.map((n) => <NextRow key={keyOf(n)} need={n} shop={shop} actions={actions} />)}
        </ul>
      )}

      {shop.whatsapp === "link" && <LinkWhatsApp shop={shop} />}

      <section className="o-section">
        <div className="o-section-head">
          <h2>Your requests</h2>
          {open.length > 0 && <span className="o-count">{open.length} open</span>}
        </div>
        {reqs.length === 0 && (
          <p className="o-muted">Your requests to {shop.owner.name} appear here. Ask for money, or write to Musha in your own words.</p>
        )}
        {open.length > 0 && (
          <ul className="m-reqs">
            {open.map((r) => <RequestRow key={r.id} req={r} shop={shop} actions={actions} />)}
          </ul>
        )}
        {done.length > 0 && (
          <>
            <h3 className="o-sub">Settled</h3>
            <ul className="m-reqs">
              {(allDone ? done : done.slice(0, SETTLED_SHOWN)).map((r) => <RequestRow key={r.id} req={r} shop={shop} actions={actions} />)}
            </ul>
            {done.length > SETTLED_SHOWN && (
              <button type="button" className="o-textbtn o-more" onClick={() => setAllDone(!allDone)}>
                {allDone ? "Show the latest" : `Show ${done.length - SETTLED_SHOWN} earlier`}
              </button>
            )}
          </>
        )}
      </section>
    </div>
  );
}

function keyOf(n: Need): string {
  if (n.kind === "question" || n.kind === "proof") return `${n.kind}${n.req.id}`;
  if (n.kind === "asked") return `asked${n.alert.id}`;
  if (n.kind === "note") return `note${n.line.id}`;
  if (n.kind === "low") return `low${n.product.id}`;
  return n.kind;
}

/** Words in the manager's language, with the English under them when Musha translated. */
function Said({ line, fallback }: { line?: Message; fallback: string }) {
  const text = line?.text || fallback;
  const english = line && line.text_en && line.text_en.trim() !== line.text.trim() ? line.text_en : "";
  return (
    <>
      <blockquote>{text}</blockquote>
      {english && <p className="m-en" lang="en">{english}</p>}
    </>
  );
}

/** The owner's question in a few words: what it asks about. */
function subjectOf(a: Alert): string {
  const d = a.data as { item?: string; day?: string };
  if (a.kind === "low_stock" && d.item) return `the ${d.item} stock`;
  if (a.kind === "cash_gap") return d.day ? `the till on ${day(d.day)}` : "the till";
  if (a.kind === "stock_gap" && d.item) return d.day ? `the ${d.item} count on ${day(d.day)}` : `the ${d.item} count`;
  return a.title.charAt(0).toLowerCase() + a.title.slice(1);
}

function Focal({ need, shop, actions }: { need: Need; shop: Shop; actions: Actions }) {
  const owner = shop.owner.name;
  if (need.kind === "question") {
    const r = need.req;
    const again = rereading(r) || wantsNumbers(r);
    const question = [...r.proofs].reverse().find((p) => p.question)?.question || "";
    return (
      <section className="o-focal o-tone-warn" aria-live="polite">
        <p className="o-eyebrow">Musha asks</p>
        <h2 className="o-focal-title">A question about {describe(r)}.</h2>
        <figure className="o-ready">
          <figcaption>Musha's question</figcaption>
          <Said line={need.line} fallback={question} />
        </figure>
        <div className="o-figure">
          <span className="o-big">{usd(r.hold?.amount_cents ?? r.amount_cents)}</span>
          <span className="o-muted">stays held until Musha has your answer</span>
        </div>
        <div className="o-row">
          {again ? (
            <button type="button" className="o-btn o-primary o-large" onClick={() => actions.proof(r)}>Send the receipt again</button>
          ) : (
            <button type="button" className="o-btn o-primary o-large" onClick={() => actions.answer(need.line)}>Answer Musha</button>
          )}
          {again && <button type="button" className="o-btn o-ghost" onClick={() => actions.answer(need.line)}>Answer in words</button>}
        </div>
      </section>
    );
  }
  if (need.kind === "asked") {
    const a = need.alert;
    const asked = typeof a.data.asked_at === "string" ? a.data.asked_at : null;
    return (
      <section className="o-focal o-tone-clay" aria-live="polite">
        <p className="o-eyebrow">Waiting for your answer</p>
        <h2 className="o-focal-title">{owner} asks about {subjectOf(a)}.</h2>
        <figure className="o-ready">
          <figcaption>Asked {when(asked)}</figcaption>
          <Said line={need.line} fallback={typeof a.data.question === "string" ? a.data.question : a.title} />
        </figure>
        <div className="o-row">
          <button type="button" className="o-btn o-primary o-large" onClick={() => actions.answer(need.line)}>Answer {owner}</button>
          {a.kind === "low_stock" && <button type="button" className="o-btn o-ghost" onClick={() => actions.ask(String(a.data.item ?? ""))}>Ask for money</button>}
        </div>
        <p className="o-fine">Write the answer to Musha in your own words. Musha passes it to {owner} in English.</p>
      </section>
    );
  }
  if (need.kind === "note") {
    return (
      <section className="o-focal o-tone-clay" aria-live="polite">
        <p className="o-eyebrow">New message</p>
        <h2 className="o-focal-title">A message from {owner}.</h2>
        <figure className="o-ready">
          <figcaption>{cap(when(need.line.at))}</figcaption>
          <Said line={need.line} fallback={need.line.text} />
        </figure>
        <div className="o-row">
          <button type="button" className="o-btn o-primary o-large" onClick={() => actions.answer()}>Reply</button>
        </div>
      </section>
    );
  }
  if (need.kind === "proof") {
    const r = need.req;
    const held = r.hold?.amount_cents ?? r.amount_cents;
    return (
      <section className="o-focal o-tone-warn" aria-live="polite">
        <p className="o-eyebrow">Held for you</p>
        <h2 className="o-focal-title">Buy {describe(r)}{r.payee ? ` from ${r.payee}` : ""}, then send the receipt.</h2>
        <div className="o-figure">
          <span className="o-big">{usd(held)}</span>
          <span className="o-muted">held at PayPal{r.hold?.expires_at ? ` until ${day(r.hold.expires_at)}` : ""}</span>
        </div>
        <p className="o-focal-body">
          Musha checks the receipt{r.kind === "expense" ? "" : " and a photo of the goods"}, and PayPal releases what it proves. The rest goes back to {owner}.
        </p>
        <div className="o-row">
          <button type="button" className="o-btn o-primary o-large" onClick={() => actions.proof(r)}>Send proof</button>
        </div>
      </section>
    );
  }
  if (need.kind === "low") {
    const p = need.product;
    return (
      <section className="o-focal o-tone-warn" aria-live="polite">
        <p className="o-eyebrow">Running low</p>
        <h2 className="o-focal-title">{daysOf(p.name, p.days_left)}.</h2>
        <p className="o-focal-body">
          {qty(p.on_hand, p.unit)} on hand. The shop sells about {Math.round(p.avg_daily)} a day.
          Musha suggests {qty(p.suggested.quantity, p.suggested.unit)}{p.suggested.payee ? ` from ${p.suggested.payee}` : ""}.
        </p>
        <div className="o-figure">
          <span className="o-big">{usd(p.suggested.amount_cents)}</span>
          <span className="o-muted">covers a week at this pace</span>
        </div>
        <div className="o-row">
          <button type="button" className="o-btn o-primary o-large" onClick={() => actions.ask(p.name)}>Ask for money</button>
        </div>
      </section>
    );
  }
  return (
    <section className="o-focal o-tone-clay" aria-live="polite">
      <p className="o-eyebrow">Today</p>
      <h2 className="o-focal-title">Close the day when the shop shuts.</h2>
      <p className="o-focal-body">Count the till and the shelves. Musha fills in what the books expect, and you change what the count says.</p>
      <div className="o-row">
        <button type="button" className="o-btn o-primary o-large" onClick={actions.close}>Close the day</button>
      </div>
    </section>
  );
}

function Calm({ shop, week }: { shop: Shop; week?: Week }) {
  const last = week?.cash[week.cash.length - 1];
  return (
    <section className="o-focal o-tone-leaf" aria-live="polite">
      <p className="o-eyebrow">All well</p>
      <h2 className="o-focal-title">{shop.name} is in order.</h2>
      {last?.closed && last.counted_cents != null && (
        <div className="o-figure">
          <span className="o-big">{usd(last.counted_cents)}</span>
          <span className="o-muted">counted in the till today</span>
        </div>
      )}
      <p className="o-focal-body">Musha writes here when {shop.owner.name} or a request needs you.</p>
    </section>
  );
}

function NextRow({ need, shop, actions }: { need: Need; shop: Shop; actions: Actions }) {
  const owner = shop.owner.name;
  let tone = "clay";
  let text: string;
  let button: { label: string; run: () => void; quiet?: boolean };
  switch (need.kind) {
    case "question":
      tone = "warn";
      text = `Musha asks about ${describe(need.req)}`;
      button = rereading(need.req) || wantsNumbers(need.req)
        ? { label: "Send the receipt again", run: () => actions.proof(need.req) }
        : { label: "Answer", run: () => actions.answer(need.line) };
      break;
    case "asked":
      text = `${owner} asks about ${subjectOf(need.alert)}`;
      button = { label: "Answer", run: () => actions.answer(need.line) };
      break;
    case "note":
      text = `${owner} wrote ${when(need.line.at)}`;
      button = { label: "Reply", run: () => actions.answer() };
      break;
    case "proof":
      tone = "warn";
      text = `Held: ${describe(need.req)}, ${usd(need.req.hold?.amount_cents ?? need.req.amount_cents)}`;
      button = { label: "Send proof", run: () => actions.proof(need.req) };
      break;
    case "low":
      tone = "warn";
      text = daysOf(need.product.name, need.product.days_left);
      button = { label: "Ask for money", run: () => actions.ask(need.product.name) };
      break;
    default:
      text = "Close the day when the shop shuts";
      button = { label: "Close the day", run: actions.close, quiet: true };
  }
  return (
    <li className="o-next-row">
      <span className={`o-dot o-tone-${tone}`} aria-hidden="true" />
      <span className="o-next-text">{text}</span>
      <span className="o-next-actions">
        <button type="button" className={`o-btn o-small ${button.quiet ? "o-ghost" : "o-outline"}`} onClick={button.run}>{button.label}</button>
      </span>
    </li>
  );
}

function RequestRow({ req, shop, actions }: { req: SpendRequest; shop: Shop; actions: Actions }) {
  const s = statusOf(req.status, shop.owner.name);
  const released = (req.status === "captured" || req.status === "part_captured") && req.hold ? req.hold.captured_cents : null;
  const label = req.proving ? "Musha is reading the receipt" : rereading(req) ? "Send the receipt again"
    : released != null && req.hold && req.hold.amount_cents > released ? `Released, ${usd(req.hold.amount_cents - released)} back to ${shop.owner.name}`
      : s.label;
  const provable = req.origin !== "wages" && (req.status === "held" || (req.status === "asking" && (rereading(req) || wantsNumbers(req))));
  return (
    <li className="m-req-row">
      <div className="m-req-row-main">
        <span className="m-req-row-what">{describe(req)}</span>
        <span className="o-muted m-req-row-meta">
          {req.payee ? `${req.payee} · ` : ""}{cap(when(req.created_at))}
        </span>
      </div>
      <span className="m-req-row-amount o-num">{usd(released ?? req.amount_cents)}</span>
      <span className={`o-pill o-tone-${toneOf(s.tone)}`}>{label}</span>
      {provable && (
        <button type="button" className="o-btn o-small o-primary m-req-row-act" onClick={() => actions.proof(req)}>Send proof</button>
      )}
      {req.status === "asking" && !provable && (
        <button type="button" className="o-btn o-small o-outline m-req-row-act" onClick={() => actions.answer()}>Answer</button>
      )}
    </li>
  );
}
