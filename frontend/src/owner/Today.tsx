import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { usd, type Shop } from "../api";
import {
  ownerApi, type Alert, type CameraStatus, type CashGapData, type LowStockData, type OwnerRequest,
  type ProofAlertData, type StockGapData, type WeekFacts,
} from "./api";
import { AskMusha } from "./AskMusha";
import { Lightbox, Thumbs, evidenceOf } from "./Evidence";
import { LookIn } from "./LookIn";
import { RecordTile } from "./Record";
import { settledSignal, useRecord } from "../record";
import { Decline, RequestCard, useAssessment } from "./RequestCard";
import type { CheckRow } from "./api";
import { Approve, Decide, ErrorLine, MoneyBar, ScoreRing, Spinner, StatusPill, type Outcome } from "./parts";
import { canApprove, cap, clock, day, longDay, openItems, qty, SETTLED, shownStatus, titleOf, useAction, when } from "./lib";
import { WATCHING, checkOf, contradicts, engineProof, sentBefore, typedHold, typedLine, watching } from "./signals";
import { HardMark } from "./RequestCard";

const SETTLED_SHOWN = 3;
/** How long the card that confirms an approval or a decision stays on top. */
const DONE_MS = 7000;
const ACTIVE_ORDER = ["flagged", "requested", "approving", "asking", "held", "agent_pending", "reviewing", "capturing"];

type Props = {
  shop: Shop;
  requests: OwnerRequest[];
  alerts: Alert[];
  facts?: WeekFacts;
  sandbox: boolean;
  refresh: () => Promise<void>;
  reloadAlerts?: () => Promise<void>;
};

type Focus =
  | { kind: "flagged"; req: OwnerRequest; alert?: Alert }
  | { kind: "approve"; req: OwnerRequest; alert?: Alert }
  | { kind: "low"; alert: Alert }
  | { kind: "gap"; alert: Alert }
  | { kind: "calm" };

type Item = Alert | OwnerRequest;

/** The single thing that needs the owner now, in priority order, and everything that waits behind it. */
function pick(requests: OwnerRequest[], alerts: Alert[]): { focus: Focus; queue: Item[] } {
  const flagged = requests.filter((r) => r.status === "flagged");
  const waiting = requests.filter(canApprove);
  // An item with a request in motion needs no second prompt to fund it: the request leads.
  const open = openItems(requests);
  const low = alerts.filter((a) => a.kind === "low_stock" && !open.has(String(a.data.item ?? "").trim().toLowerCase()));
  const gaps = alerts.filter((a) => a.kind === "cash_gap" || a.kind === "stock_gap");
  const linked = (r: OwnerRequest, kind: string) => alerts.find((a) => a.kind === kind && a.request_id === r.id);
  const queue: Exclude<Focus, { kind: "calm" }>[] = [
    ...flagged.map((req) => ({ kind: "flagged" as const, req, alert: linked(req, "proof") })),
    ...waiting.map((req) => ({ kind: "approve" as const, req, alert: linked(req, "mandate") })),
    ...low.map((alert) => ({ kind: "low" as const, alert })),
    ...gaps.map((alert) => ({ kind: "gap" as const, alert })),
  ];
  const focus = queue[0] ?? { kind: "calm" as const };
  return { focus, queue: queue.map((f) => ("req" in f ? f.req : f.alert)) };
}

export function Today({ shop, requests, alerts, facts, sandbox, refresh }: Props) {
  const [camera, setCamera] = useState<CameraStatus>();
  const [flash, setFlash] = useState<number | null>(null);
  const [allDone, setAllDone] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const { focus, queue } = useMemo(() => pick(requests, alerts), [requests, alerts]);
  // When the confirming card leaves with focus inside it, focus moves to the card that takes its place.
  const focal = useRef<HTMLDivElement>(null);
  const handOff = useRef(false);
  const next = () => {
    handOff.current = !!focal.current?.contains(document.activeElement);
    setOutcome(null);
  };
  useEffect(() => {
    if (outcome || !handOff.current) return;
    handOff.current = false;
    const card = focal.current?.querySelector<HTMLElement>(".o-focal");
    const title = card?.querySelector<HTMLElement>(".o-focal-title");
    if (title) {
      title.tabIndex = -1;
      title.focus({ preventScroll: true });
    }
  }, [outcome]);
  // While the confirming card holds the top, what would lead waits in the list beneath it.
  const rest = outcome
    ? queue.filter((x) => !("status" in x && x.id === outcome.req.id))
    : queue.slice(1);
  const live = outcome ? requests.find((r) => r.id === outcome.req.id) : undefined;
  const active = requests
    .filter((r) => ACTIVE_ORDER.includes(r.status))
    .sort((a, b) => ACTIVE_ORDER.indexOf(a.status) - ACTIVE_ORDER.indexOf(b.status) || b.id - a.id);
  const done = requests.filter((r) => !ACTIVE_ORDER.includes(r.status));
  const both = refresh;
  const record = useRecord(shop.id, settledSignal(requests));

  const goTo = (id: number) => {
    setFlash(id);
    const el = document.getElementById(`req-${id}`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    window.setTimeout(() => {
      setFlash(null);
    }, 1600);
  };

  const suggestions = useMemo(() => {
    const out = ["How did the shop do this week?"];
    // The newest gap: the day the owner most likely wants to hear about.
    const gap = facts?.cash.gaps[facts.cash.gaps.length - 1];
    if (gap) out.push(`What happened to the cash on ${gap.day_name.split(" ")[0]}?`);
    const low = facts?.stock.find((s) => s.low);
    if (low) out.push(`When do we run out of ${low.item}?`);
    const top = facts?.items.slice().sort((a, b) => b.margin_cents - a.margin_cents)[0];
    if (top && out.length < 3) out.push(`How much did ${top.item} make this week?`);
    return out;
  }, [facts]);

  return (
    <div className="o-today">
      <div ref={focal} className="o-focal-slot">
        {outcome ? (
          <DoneFocal key={`${outcome.req.id}:${outcome.action}`} outcome={outcome} live={live} shop={shop}
            onNext={next} />
        ) : (
          <FocalCard focus={focus} shop={shop} sandbox={sandbox} refresh={both} camera={camera} facts={facts}
            goTo={goTo} onDone={setOutcome} />
        )}
      </div>

      {rest.length > 0 && (
        <ul className="o-next" aria-label="Also waiting">
          {rest.map((x) => ("status" in x
            ? <NextRequest key={`r${x.id}`} req={x} goTo={goTo} />
            : <NextAlert key={`a${x.id}`} alert={x} shop={shop} refresh={both} />))}
        </ul>
      )}

      <LookIn shop={shop} onStatus={setCamera} />

      <RecordTile shop={shop} record={record} />

      <section className="o-section">
        <div className="o-section-head">
          <h2>Requests</h2>
          {active.length > 0 && <span className="o-count">{active.length} open</span>}
        </div>
        {requests.length === 0 && <p className="o-muted">{shop.manager.name}'s requests appear here.</p>}
        <div className="o-stack">
          {active.map((r) => (
            <RequestCard key={r.id} req={r} shop={shop} sandbox={sandbox} refresh={both} highlight={flash === r.id} />
          ))}
        </div>
        {done.length > 0 && (
          <>
            <h3 className="o-sub">Settled</h3>
            <div className="o-stack">
              {(allDone ? done : done.slice(0, SETTLED_SHOWN)).map((r) => (
                <RequestCard key={r.id} req={r} shop={shop} sandbox={sandbox} refresh={both} highlight={flash === r.id} />
              ))}
            </div>
            {done.length > SETTLED_SHOWN && (
              <button className="o-textbtn o-more" onClick={() => setAllDone(!allDone)}>
                {allDone ? "Show the latest" : `Show ${done.length - SETTLED_SHOWN} earlier`}
              </button>
            )}
          </>
        )}
      </section>

      <section className="o-section">
        <div className="o-section-head">
          <h2>Ask Musha</h2>
          <span className="o-muted o-small-text">Answers come from the books.</span>
        </div>
        <AskMusha shop={shop} suggestions={suggestions} />
      </section>
    </div>
  );
}

function FocalCard({ focus, shop, sandbox, refresh, camera, facts, goTo, onDone }: {
  focus: Focus;
  shop: Shop;
  sandbox: boolean;
  refresh: () => Promise<void>;
  camera?: CameraStatus;
  facts?: WeekFacts;
  goTo: (id: number) => void;
  onDone: (o: Outcome) => void;
}) {
  if (focus.kind === "flagged") return <FlaggedFocal req={focus.req} alert={focus.alert} shop={shop} refresh={refresh} goTo={goTo} onDone={onDone} />;
  if (focus.kind === "approve") return <ApproveFocal req={focus.req} alert={focus.alert} shop={shop} sandbox={sandbox} refresh={refresh} onDone={onDone} />;
  if (focus.kind === "low") return <LowFocal alert={focus.alert} shop={shop} refresh={refresh} />;
  if (focus.kind === "gap") return <GapFocal alert={focus.alert} shop={shop} refresh={refresh} />;
  const f = camera?.latest;
  return (
    <section className="o-focal o-tone-leaf" aria-live="polite">
      <p className="o-eyebrow">All well</p>
      <h2 className="o-focal-title">{shop.name} is in order.</h2>
      {facts && (
        <div className="o-figure">
          <span className="o-big">{usd(facts.sales_cents)}</span>
          <span className="o-muted">in sales, {facts.from === facts.to ? "today" : "the last seven days"}</span>
        </div>
      )}
      {f?.line && <p className="o-focal-body">Counter camera at {clock(f.at)}: {cap(f.line)}.</p>}
    </section>
  );
}

function FlaggedFocal({ req, alert, shop, refresh, goTo, onDone }: {
  req: OwnerRequest;
  alert?: Alert;
  shop: Shop;
  refresh: () => Promise<void>;
  goTo: (id: number) => void;
  onDone: (o: Outcome) => void;
}) {
  const last = engineProof(req, shop.owner.name) ?? req.proofs[req.proofs.length - 1];
  const data = alert?.data as ProofAlertData | undefined;
  const score = data?.score ?? last?.score ?? 0;
  const held = req.hold?.amount_cents ?? req.amount_cents;
  // Contradictions first: they are why a high score still waits.
  const failing = ((last?.checks ?? []) as CheckRow[]).filter((c) => c.applies && !c.passed)
    .sort((a, b) => Number(contradicts(b)) - Number(contradicts(a)));
  const cam = checkOf(last, "camera");
  const typed = typedHold(req, alert, shop.owner.name);
  const evidence = useMemo(() => evidenceOf(req), [req]);
  const [shown, setShown] = useState<number | null>(null);
  return (
    <section className="o-focal o-tone-danger" aria-live="polite">
      <p className="o-eyebrow">Held for you</p>
      <div className="o-focal-grid">
        <div className="o-focal-main">
          <h2 className="o-focal-title">Check the receipt for {titleOf(req)}.</h2>
          {typed && <p className="o-note o-typed">{typedLine(shop.manager.name)}</p>}
          <ul className="o-reasons">
            {failing.slice(0, 3).map((c) => (
              <li key={c.key}>{contradicts(c) && <HardMark earlier={sentBefore(c)} />}{c.reason}</li>
            ))}
          </ul>
          {cam && watching(cam) && <p className="o-watching"><span className="o-watch-dot" aria-hidden="true" />{WATCHING}</p>}
        </div>
        <div className="o-focal-side">
          <ScoreRing score={score} size={88} />
        </div>
      </div>
      {evidence.length > 0 && (
        <div className="o-focal-evidence">
          <Thumbs items={evidence} onOpen={setShown} />
        </div>
      )}
      <div className="o-figure">
        <span className="o-big">{usd(held)}</span>
        <span className="o-muted">stays held at PayPal until you decide</span>
      </div>
      <Decide req={req} refresh={refresh} onDone={onDone} />
      <button className="o-textbtn" onClick={() => goTo(req.id)}>See every check</button>
      {shown !== null && (
        <Lightbox items={evidence} index={shown} setIndex={setShown} onClose={() => setShown(null)}
          req={req} manager={shop.manager.name} />
      )}
    </section>
  );
}

function ApproveFocal({ req, alert, shop, sandbox, refresh, onDone }: {
  req: OwnerRequest;
  alert?: Alert;
  shop: Shop;
  sandbox: boolean;
  refresh: () => Promise<void>;
  onDone: (o: Outcome) => void;
}) {
  const a = useAssessment(req);
  const inferred = req.inferred ?? [];
  const wages = req.origin === "wages";
  const who = req.origin === "alert" ? "From a low-stock alert" : req.origin === "musha" ? "Musha asks"
    : `${shop.manager.name} asks`;
  return (
    <section className="o-focal o-tone-clay" aria-live="polite">
      <p className="o-eyebrow">Waiting for you</p>
      <h2 className="o-focal-title">
        {wages ? `Wages for ${shop.manager.name}.` : `${who}: ${titleOf(req)}${req.payee ? ` from ${req.payee}` : ""}.`}
      </h2>
      <div className="o-figure">
        <span className="o-big">{usd(req.amount_cents)}</span>
        <span className="o-muted">
          {wages
            ? `Musha pays ${shop.manager.name} by PayPal Payouts once PayPal holds it`
            : inferred.includes("amount") && !a?.recommendation.startsWith("Musha's estimate")
              ? "Musha's estimate from past buys"
              : `${cap(req.kind)}, asked ${when(req.created_at)}`}
        </span>
      </div>
      {req.origin === "musha" && req.words && <p className="o-focal-body">{req.words}</p>}
      {a?.recommendation && <p className="o-musha"><span className="o-musha-mark" aria-hidden="true">M</span>{a.recommendation}</p>}
      {alert?.body && <p className="o-focal-body"><b className="o-ink">Why the agent asks you.</b> {alert.body}</p>}
      <Approve req={req} sandbox={sandbox} refresh={refresh} big onDone={onDone} />
      <Decline req={req} refresh={refresh} />
    </section>
  );
}

const HOLDING = ["held", "asking", "flagged"];

/**
 * The payoff, on top for a moment: what PayPal now holds after an approval, or how a decided hold
 * split between the shop and the owner. Then the next thing that needs the owner takes over.
 */
function DoneFocal({ outcome, live, shop, onNext }: {
  outcome: Outcome;
  live?: OwnerRequest;
  shop: Shop;
  onNext: () => void;
}) {
  const card = useRef<HTMLElement>(null);
  const next = useRef(onNext);
  next.current = onNext;
  const paused = usePausableTimer(card, DONE_MS, () => next.current());

  // The polled request once it has moved past the action; the action's own reply until then.
  const before = outcome.action === "approve" ? ["requested", "approving", outcome.before] : [outcome.before];
  const req = live && !before.includes(live.status) ? live : outcome.req;
  const hold = req.hold;
  const settled = !!hold && SETTLED.includes(req.status);
  const released = settled ? hold.captured_cents : outcome.released;
  const returned = settled ? hold.amount_cents - hold.captured_cents : outcome.returned;
  const manager = shop.manager.name;

  let eyebrow: string;
  let tone: string;
  let title: string;
  let body = "";
  const wages = req.origin === "wages";
  if (outcome.action === "approve" && wages) {
    eyebrow = settled ? "Paid" : "Approved";
    tone = settled ? "leaf" : "warn";
    title = settled ? `${usd(released)} of wages for ${manager}.` : `PayPal holds ${usd(outcome.held)} of wages.`;
    body = settled ? `PayPal Payouts sends it to ${manager}.` : `Musha pays ${manager} once PayPal holds it.`;
  } else if (outcome.action === "approve") {
    const holds = HOLDING.includes(req.status);
    eyebrow = "Approved";
    tone = "warn";
    title = holds ? `PayPal holds ${usd(outcome.held)}.` : `PayPal is reviewing ${usd(outcome.held)}.`;
    body = holds
      ? `${manager} pays, then sends ${req.kind === "expense" ? "the receipt" : "the receipt and a photo"}. Proof releases the money.`
      : `Musha tells ${manager} once PayPal holds it.`;
  } else if (released > 0) {
    eyebrow = "Released";
    tone = "leaf";
    title = returned > 0 ? `${usd(released)} released. ${usd(returned)} back to you.` : `${usd(released)} released to the shop.`;
  } else {
    eyebrow = "Returned";
    tone = "clay";
    title = `${usd(returned)} back to you.`;
  }

  return (
    <section ref={card} tabIndex={-1} className={`o-focal o-justdone o-tone-${tone}`} aria-live="polite"
      data-paused={paused || undefined} style={{ ["--done-ms" as string]: `${DONE_MS}ms` }}>
      <div className="o-justdone-head">
        <p className="o-eyebrow">{eyebrow}</p>
        <button className="o-btn o-quiet o-small" onClick={onNext}>Next</button>
      </div>
      <h2 className="o-focal-title">{title}</h2>
      {outcome.action === "approve"
        ? <p className="o-focal-body">{body}</p>
        : <MoneyBar released={released} returned={returned} bare />}
      <div className="o-justdone-meta">
        <StatusPill status={shownStatus(req)} />
        <span>{titleOf(req)}{req.payee && !wages ? `, ${req.payee}` : ""}</span>
        {outcome.action === "approve" && !settled && hold?.expires_at && <span>Held until {day(hold.expires_at)}</span>}
      </div>
      <span className="o-justdone-timer" aria-hidden="true" />
    </section>
  );
}

/**
 * Runs `done` once `ms` have passed, counting only while the pointer is off the card and focus is outside it.
 * The card takes focus itself when it opens; that focus leaves the timer running. Returns true while paused.
 */
function usePausableTimer(card: RefObject<HTMLElement | null>, ms: number, done: () => void): boolean {
  const [paused, setPaused] = useState(false);
  const fire = useRef(done);
  fire.current = done;
  useEffect(() => {
    const el = card.current;
    if (!el) return;
    let left = ms;
    let since = 0;
    let timer = 0;
    let hover = false;
    let inside = false;
    let stopped = true;
    const start = () => {
      since = performance.now();
      timer = window.setTimeout(() => {
        fire.current();
      }, Math.max(0, left));
    };
    const sync = () => {
      const stop = hover || inside;
      if (stop === stopped) return;
      stopped = stop;
      if (stop) {
        window.clearTimeout(timer);
        left -= performance.now() - since;
      } else {
        start();
      }
      setPaused(stop);
    };
    const enter = () => {
      hover = true;
      sync();
    };
    const leave = () => {
      hover = false;
      sync();
    };
    const focusIn = (e: FocusEvent) => {
      inside = e.target !== el;
      sync();
    };
    const focusOut = (e: FocusEvent) => {
      const to = e.relatedTarget as Node | null;
      inside = !!to && to !== el && el.contains(to);
      sync();
    };
    el.focus({ preventScroll: true });
    hover = el.matches(":hover");
    stopped = false;
    if (hover) {
      stopped = true;
      setPaused(true);
    } else {
      start();
    }
    el.addEventListener("pointerenter", enter);
    el.addEventListener("pointerleave", leave);
    el.addEventListener("focusin", focusIn);
    el.addEventListener("focusout", focusOut);
    return () => {
      window.clearTimeout(timer);
      el.removeEventListener("pointerenter", enter);
      el.removeEventListener("pointerleave", leave);
      el.removeEventListener("focusin", focusIn);
      el.removeEventListener("focusout", focusOut);
    };
  }, [card, ms]);
  return paused;
}

function LowFocal({ alert, shop, refresh }: { alert: Alert; shop: Shop; refresh: () => Promise<void> }) {
  const d = alert.data as LowStockData;
  const { busy, error, run } = useAction();
  const [which, setWhich] = useState("");
  const act = (action: "fund" | "ask") => {
    setWhich(action);
    run(async () => {
      await ownerApi.act(alert.id, action);
      await refresh();
    });
  };
  const days = d.days_left ?? 0;
  return (
    <section className="o-focal o-tone-warn" aria-live="polite">
      <p className="o-eyebrow">Running low</p>
      <h2 className="o-focal-title">{cap(d.item)} run out in {days} {days === 1 ? "day" : "days"}.</h2>
      <p className="o-focal-body">
        {qty(d.on_hand, d.unit)} on hand. The shop sells about {d.avg_daily} a day.
        Musha suggests {qty(d.quantity, d.unit)} from {d.payee}.
      </p>
      <div className="o-figure">
        <span className="o-big">{usd(d.amount_cents)}</span>
        <span className="o-muted">covers a week at this pace</span>
      </div>
      <div className="o-row">
        <button className="o-btn o-primary o-large" disabled={busy} onClick={() => act("fund")}>
          {busy && which === "fund" ? <Spinner /> : null}Fund it
        </button>
        <button className="o-btn o-ghost" disabled={busy} onClick={() => act("ask")}>
          {busy && which === "ask" ? <Spinner /> : null}Ask {shop.manager.name}
        </button>
        <Dismiss alert={alert} refresh={refresh} />
      </div>
      {d.asked_at && <p className="o-fine">You asked {shop.manager.name} {when(d.asked_at)}.</p>}
      <Answered alert={alert} shop={shop} />
      <ErrorLine error={error} />
    </section>
  );
}

function GapFocal({ alert, shop, refresh }: { alert: Alert; shop: Shop; refresh: () => Promise<void> }) {
  const { busy, error, run } = useAction();
  const cash = alert.kind === "cash_gap";
  const d = alert.data as CashGapData & StockGapData;
  const on = d.day ? longDay(d.day) : "";
  const over = cash ? d.gap_cents > 0 : d.gap > 0;
  const title = cash
    ? `The till came up ${over ? "over" : "short"}${on ? ` on ${on}` : ""}.`
    : `${cap(d.item)} came up ${over ? "over" : "short"} on the shelf${on ? ` on ${on}` : ""}.`;
  return (
    <section className={`o-focal o-tone-${over ? "warn" : "danger"}`} aria-live="polite">
      <p className="o-eyebrow">{cash ? "Cash gap" : "Stock gap"}</p>
      <h2 className="o-focal-title">{title}</h2>
      <div className="o-figure">
        <span className={`o-big ${over ? "o-warn" : "o-danger"}`}>{cash ? usd(Math.abs(d.gap_cents)) : qty(Math.abs(d.gap), d.unit)}</span>
        <span className="o-muted">
          {cash
            ? `Counted ${usd(d.counted_cents)}. The books expected ${usd(d.expected_cents)}.`
            : `Counted ${d.counted}. The books expected ${d.expected}.`}
        </span>
      </div>
      <figure className="o-ready">
        <figcaption>Musha's question for {shop.manager.name}</figcaption>
        <blockquote>{d.question}</blockquote>
      </figure>
      <div className="o-row">
        <button className="o-btn o-primary" disabled={busy} onClick={() => run(async () => {
          await ownerApi.act(alert.id, "ask");
          await refresh();
        })}>
          {busy ? <Spinner /> : null}Ask {shop.manager.name}
        </button>
        <Dismiss alert={alert} refresh={refresh} />
      </div>
      {d.asked_at && <p className="o-fine">You asked {shop.manager.name} {when(d.asked_at)}.</p>}
      <Answered alert={alert} shop={shop} />
      <ErrorLine error={error} />
    </section>
  );
}

/** The manager's answer to the owner's question on an alert, in English. */
function Answered({ alert, shop }: { alert: Alert; shop: Shop }) {
  const said = alert.data.answer_en ?? alert.data.answer;
  if (typeof said !== "string" || !said.trim()) return null;
  const at = typeof alert.data.answered_at === "string" ? ` ${when(alert.data.answered_at)}` : "";
  return <p className="o-fine"><b className="o-ink">{shop.manager.name} answered{at}:</b> {said}</p>;
}

function Dismiss({ alert, refresh, small }: { alert: Alert; refresh: () => Promise<void>; small?: boolean }) {
  const { busy, run } = useAction();
  return (
    <button className={`o-btn o-quiet${small ? " o-small" : ""}`} disabled={busy} onClick={() => run(async () => {
      await ownerApi.dismiss(alert.id);
      await refresh();
    })}>Dismiss</button>
  );
}

function NextRequest({ req, goTo }: { req: OwnerRequest; goTo: (id: number) => void }) {
  return (
    <li className="o-next-row">
      <span className={`o-dot o-tone-${req.status === "flagged" ? "danger" : "clay"}`} aria-hidden="true" />
      <span className="o-next-text">{req.status === "flagged" ? "Held for you" : "Waiting for you"}: {titleOf(req)}, {usd(req.amount_cents)}</span>
      <button className="o-btn o-outline o-small" onClick={() => goTo(req.id)}>Open</button>
    </li>
  );
}

function NextAlert({ alert, shop, refresh }: { alert: Alert; shop: Shop; refresh: () => Promise<void> }) {
  const { busy, error, run } = useAction();
  const asked = typeof alert.data.asked_at === "string";
  const answer = alert.data.answer_en ?? alert.data.answer;
  const said = typeof answer === "string" && answer.trim() ? answer.trim() : "";
  const tone = alert.kind === "low_stock" ? "warn" : "danger";
  return (
    <li className="o-next-row">
      <span className={`o-dot o-tone-${tone}`} aria-hidden="true" />
      <span className="o-next-text">
        {alert.title}
        {asked && !said && <span className="o-muted"> · asked {when(alert.data.asked_at as string)}</span>}
        {said && <span className="o-muted"> · {shop.manager.name} answered: {said}</span>}
        {error && <span className="o-danger"> · {error}</span>}
      </span>
      <span className="o-next-actions">
        {alert.kind === "low_stock" && (
          <button className="o-btn o-outline o-small" disabled={busy} onClick={() => run(async () => {
            await ownerApi.act(alert.id, "fund");
            await refresh();
          })}>Fund it</button>
        )}
        <button className={`o-btn o-small ${alert.kind === "low_stock" || asked ? "o-quiet" : "o-outline"}`} disabled={busy} onClick={() => run(async () => {
          await ownerApi.act(alert.id, "ask");
          await refresh();
        })}>{asked ? "Ask again" : `Ask ${shop.manager.name}`}</button>
        <Dismiss alert={alert} refresh={refresh} small />
      </span>
    </li>
  );
}
