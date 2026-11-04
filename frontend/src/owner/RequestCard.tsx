import { useEffect, useMemo, useState } from "react";
import { usd, type Proof, type Shop } from "../api";
import { ownerApi, type Assessment, type CheckRow, type OwnerRequest } from "./api";
import { Lightbox, Thumbs, evidenceOf } from "./Evidence";
import { Approve, Decide, KindChip, MoneyBar, ScoreRing, Spinner, StatusPill } from "./parts";
import { canApprove, day, shownStatus, titleOf, when } from "./lib";
import { WATCHING, contradicts, highScoreLine, sentBefore, typedHold, typedLine, watching } from "./signals";

const LANGUAGE: Record<string, string> = { sn: "Shona", nd: "Ndebele", zu: "isiZulu", en: "English" };
const CLOSED = ["captured", "part_captured", "voided"];
const ASSESS = ["requested", "approving", "agent_pending", "reviewing"];

/** Musha's one line for the approval card, from the shop's own past buys. */
export function useAssessment(req: OwnerRequest): Assessment | undefined {
  const [a, setA] = useState<Assessment>();
  const live = ASSESS.includes(req.status);
  useEffect(() => {
    if (!live) return;
    let on = true;
    ownerApi.assessment(req.id).then((x) => {
      if (on) setA(x);
    }).catch(() => undefined);
    return () => {
      on = false;
    };
  }, [req.id, req.status, req.amount_cents, req.payee, live]);
  return a;
}

export function Words({ req, manager }: { req: OwnerRequest; manager: string }) {
  const translated = !!req.words_en && req.language !== "en" && req.words_en !== req.words;
  const [original, setOriginal] = useState(false);
  if (!req.words) return null;
  const text = translated && !original ? req.words_en : req.words;
  return (
    <figure className="o-words">
      <blockquote>{text}</blockquote>
      <figcaption>
        <span>{manager}</span>
        {translated && (
          <button className="o-textbtn" onClick={() => setOriginal(!original)}>
            {original ? "English" : `Original, ${LANGUAGE[req.language] ?? req.language}`}
          </button>
        )}
      </figcaption>
    </figure>
  );
}

type Props = {
  req: OwnerRequest;
  shop: Shop;
  sandbox: boolean;
  refresh: () => Promise<void>;
  highlight?: boolean;
};

export function RequestCard({ req, shop, sandbox, refresh, highlight }: Props) {
  const a = useAssessment(req);
  const closed = CLOSED.includes(req.status);
  const [open, setOpen] = useState(!closed);
  useEffect(() => {
    if (!closed) setOpen(true);
  }, [closed]);
  const inferred = req.inferred ?? [];
  const manager = shop.manager.name;
  const asker = req.origin === "alert" ? "From a low-stock alert"
    : req.origin === "owner" || req.origin === "wages" ? `Asked by ${shop.owner.name}` : `Asked by ${manager}`;
  const hold = req.hold;
  const lastDecision = req.proofs[req.proofs.length - 1]?.decision;

  return (
    <article id={`req-${req.id}`} className={`o-card o-req${highlight ? " o-flash" : ""}`}>
      <header className="o-req-head">
        <div className="o-req-tags">
          <KindChip kind={req.kind} />
          <StatusPill status={shownStatus(req)} />
        </div>
        <div className="o-req-main">
          <div className="o-req-what">
            <h3>{titleOf(req)}</h3>
            <p className="o-muted">
              {req.payee ? <>{req.payee}{inferred.includes("payee") && <span className="o-guess"> Musha's pick</span>} · </> : null}
              {asker}, {when(req.created_at)}
            </p>
          </div>
          <div className="o-req-amount">
            <span className="o-amount">{usd(req.amount_cents)}</span>
            {inferred.includes("amount") && (
              <span className="o-guess">{a?.recommendation.startsWith("Musha's estimate") ? "Musha's estimate" : "Musha's estimate from past buys"}</span>
            )}
            {inferred.includes("quantity") && (
              <span className="o-guess">{inferred.includes("amount") ? "About a week of stock" : "Count worked out from the amount"}</span>
            )}
          </div>
        </div>
      </header>

      {req.origin !== "wages" && <Words req={req} manager={manager} />}

      {req.proving && <p className="o-fine o-proving"><Spinner /> Musha is reading the receipt.</p>}

      {typedHold(req, undefined, shop.owner.name) && <p className="o-note o-typed">{typedLine(manager)}</p>}

      {a?.recommendation && ASSESS.includes(req.status) && (
        <p className="o-musha"><span className="o-musha-mark" aria-hidden="true">M</span>{a.recommendation}</p>
      )}

      {canApprove(req) && <Approve req={req} sandbox={sandbox} refresh={refresh} />}

      {req.approved_by === "agent" && req.approved_at && (
        <p className="o-agent-line">Approved by Musha inside your limit.</p>
      )}

      {hold && closed && !(open && lastDecision === "release") && (
        <MoneyBar released={hold.captured_cents} returned={hold.amount_cents - hold.captured_cents} owner="you"
          to={req.origin === "wages" ? manager : undefined} />
      )}

      {(hold || req.proofs.length > 0) && req.origin !== "wages" && (
        closed ? (
          <>
            <button className="o-textbtn o-toggle" onClick={() => setOpen(!open)} aria-expanded={open}>
              {open ? "Hide the proof" : "Show the proof"}
            </button>
            {open && <Timeline req={req} shop={shop} refresh={refresh} />}
          </>
        ) : (
          <Timeline req={req} shop={shop} refresh={refresh} />
        )
      )}
    </article>
  );
}

/** Rounds show once there are two: the first scoring, then the answer round after Musha's one question. */
function roundLabel(round: number, rounds: number): string {
  if (round >= 2) return "Answer round";
  return rounds > 1 ? "Round 1" : "";
}

function ownerDecided(p: Proof, owner: string) {
  return p.decision === "return" || (p.decision === "release" && p.summary.startsWith(`${owner} `));
}

export function Timeline({ req, shop, refresh }: { req: OwnerRequest; shop: Shop; refresh: () => Promise<void> }) {
  const evidence = useMemo(() => evidenceOf(req), [req]);
  const [shown, setShown] = useState<number | null>(null);
  const owner = shop.owner.name;
  const manager = shop.manager.name;
  const hold = req.hold;
  const engine = req.proofs.filter((p) => !ownerDecided(p, owner));
  const rounds = engine.length;

  return (
    <ol className="o-timeline">
      <li className="o-step o-done">
        <h4>Approved</h4>
        <p>
          {req.approved_by === "agent" ? "Musha's agent" : owner}, {when(req.approved_at)}.
          {hold && hold.status !== "voided" && !CLOSED.includes(req.status) && (
            <> PayPal holds {usd(hold.amount_cents)} until {day(hold.expires_at)}.</>
          )}
        </p>
      </li>

      <li className={`o-step${evidence.length ? " o-done" : " o-wait"}`}>
        <h4>Evidence</h4>
        {evidence.length ? (
          <Thumbs items={evidence} onOpen={setShown} />
        ) : (
          <p className="o-muted">Waiting for {manager}'s receipt{req.kind !== "expense" ? " and a photo of the goods" : ""}.</p>
        )}
      </li>

      {req.proofs.map((p) => (
        ownerDecided(p, owner) ? (
          <li key={p.id} className="o-step o-done">
            <h4>Your decision</h4>
            <p>{p.summary}</p>
          </li>
        ) : (
          <Round key={p.id} p={p} req={req} label={roundLabel(p.round, rounds)} last={p === req.proofs[req.proofs.length - 1]}
            manager={manager} owner={owner} refresh={refresh} />
        )
      ))}

      {shown !== null && (
        <Lightbox items={evidence} index={shown} setIndex={setShown} onClose={() => setShown(null)} req={req} manager={manager} />
      )}
    </ol>
  );
}

function Round({ p, req, label, last, manager, owner, refresh }: {
  p: Proof;
  req: OwnerRequest;
  label: string;
  last: boolean;
  manager: string;
  owner: string;
  refresh: () => Promise<void>;
}) {
  const checks = p.checks as CheckRow[];
  const held = req.hold?.amount_cents ?? req.amount_cents;
  const failing = checks.filter((c) => c.applies && !c.passed);
  const why = highScoreLine(p);
  return (
    <>
      <li className="o-step o-done">
        <h4>Checks{label ? <span className="o-muted"> · {label}</span> : null}</h4>
        <div className="o-checks">
          <ScoreRing score={p.score} />
          <ul className="o-check-list">
            {checks.map((c) => <CheckLine key={c.key} c={c} />)}
          </ul>
        </div>
      </li>
      <li className={`o-step o-decision o-dec-${p.decision}`}>
        <h4>{p.decision === "release" ? "Released" : p.decision === "ask" ? "Musha asked one question" : "Held for you"}</h4>
        {p.decision === "release" && (
          <MoneyBar released={Math.min(p.spent_cents, held)} returned={held - Math.min(p.spent_cents, held)} owner="you" />
        )}
        {why && <p className="o-why">{why}</p>}
        {p.decision === "ask" && (
          <div className="o-qa">
            <p className="o-q"><b>Musha</b>{p.question}</p>
            {p.answer ? <p className="o-a"><b>{manager}</b>{p.answer}</p> : <p className="o-muted">Waiting for {manager}'s answer.</p>}
          </div>
        )}
        {p.decision === "hold" && (
          <>
            <ul className="o-reasons">
              {(failing.length ? failing : checks.filter((c) => !c.passed)).map((c) => <li key={c.key}>{c.reason}</li>)}
            </ul>
            {last && req.status === "flagged" ? (
              <>
                <p className="o-muted">{usd(held)} stays held until you decide, {owner}.</p>
                <Decide req={req} refresh={refresh} />
              </>
            ) : null}
          </>
        )}
      </li>
    </>
  );
}

const fmt = (n: number) => (Number.isInteger(n) ? String(n) : n.toFixed(1));

/** One check: its points, a mark when its evidence contradicts the request, and the camera while it watches. */
function CheckLine({ c }: { c: CheckRow }) {
  const watch = watching(c);
  const hard = contradicts(c);
  const earlier = sentBefore(c);
  const cls = watch ? " o-watch" : `${c.applies ? "" : " o-na"}${c.applies && !c.passed ? " o-miss" : ""}`;
  return (
    <li className={`o-check${cls}`}>
      <span className="o-check-dot" aria-hidden="true" />
      <span className="o-check-label">{c.label}</span>
      <span className="o-check-reason">
        {hard && <HardMark earlier={earlier} />}
        {watch ? WATCHING : c.reason}
      </span>
      <span className="o-check-pts">
        {watch ? "Watching" : !c.applies ? "Unscored" : c.max ? `${fmt(c.points)}/${c.max}` : c.passed ? "Pass" : "Fail"}
      </span>
    </li>
  );
}

/** A small mark on a check that contradicts the request: it names the earlier request for evidence sent before. */
export function HardMark({ earlier }: { earlier: number | null }) {
  return (
    <span className="o-mark" title="This contradicts the request. Musha asks before any release.">
      {earlier ? `Sent before, request ${earlier}` : "Contradicts"}
    </span>
  );
}
