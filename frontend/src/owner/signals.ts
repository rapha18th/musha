/** What the proof engine's checks say beyond points: the signals the console reads off a proof. */
import type { Proof } from "../api";
import type { Alert, CheckRow, OwnerRequest, ProofAlertData } from "./api";

/** At this score the engine releases on its own, unless a check contradicts the request. */
export const RELEASE_AT = 85;

export const WATCHING = "The counter camera is watching for the delivery.";

export const checkOf = (p: Proof | undefined, key: string) =>
  p?.checks.find((c) => c.key === key) as CheckRow | undefined;

/** The engine's newest proof. The owner's own decision copies its checks and stays out of this. */
export function engineProof(req: OwnerRequest, owner?: string): Proof | undefined {
  for (let i = req.proofs.length - 1; i >= 0; i--) {
    const p = req.proofs[i];
    const mine = p.decision === "return" || (p.decision === "release" && !!owner && p.summary.startsWith(`${owner} `));
    if (!mine) return p;
  }
  return undefined;
}

/** The counter phone has stills from the window and has yet to see the delivery. */
export const watching = (c: CheckRow) => c.key === "camera" && !c.applies && c.data?.waiting === true;

/** A failing check whose evidence contradicts the request: the engine asks before any release. */
export const contradicts = (c: CheckRow) => c.applies && !c.passed && c.data?.hard === true;

/** The earlier request a photo or a receipt was sent for, when the engine caught it a second time. */
export function sentBefore(c: CheckRow): number | null {
  const id = c.data?.request_id;
  if (typeof id !== "number") return null;
  if (c.key === "photo" && c.data?.copy_of) return id;
  if (c.key === "duplicate" && c.data?.how) return id;
  return null;
}

/** The receipt's currency when it differs from the shop's. The engine then records nothing spent. */
export function foreignCurrency(req: OwnerRequest, p: Proof | undefined): string | null {
  const match = checkOf(p, "match");
  const failed = match?.data?.failed;
  if (!Array.isArray(failed) || !failed.includes("currency")) return null;
  const said = /receipt is in ([A-Za-z$]{2,5})\b/.exec(match?.reason ?? "");
  if (said) return said[1];
  const read = req.attachments.find((a) => a.id === p?.receipt_id)?.reading?.currency;
  return typeof read === "string" && read.trim() ? read.trim().toUpperCase() : "another currency";
}

/** The manager typed the receipt values while the reader was off, and the engine held the release. */
export function typedHold(req: OwnerRequest, alert?: Alert, owner?: string): boolean {
  if (req.status !== "flagged") return false;
  const d = alert?.data as ProofAlertData | undefined;
  if (typeof d?.typed === "boolean") return d.typed;
  const p = engineProof(req, owner);
  return p?.decision === "hold" && checkOf(p, "read")?.data?.source === "typed";
}

export const typedLine = (manager: string) =>
  `${manager} typed these values while the receipt reader was off. Check them against the receipt photo, then decide.`;

/** Why a score at the release mark still asked a question, or still waits for the owner. */
export function highScoreLine(p: Proof): string {
  const hard = (p.checks as CheckRow[]).filter(contradicts);
  if (p.score < RELEASE_AT || !hard.length || (p.decision !== "ask" && p.decision !== "hold")) return "";
  const what = hard.length === 1 ? `The ${hard[0].label.toLowerCase()} check contradicts` : `${hard.length} checks contradict`;
  return p.decision === "ask"
    ? `A score of ${p.score} releases on its own. ${what} the request, so Musha asked first.`
    : `A score of ${p.score} releases on its own. ${what} the request, so the money waits for you.`;
}

/** "171", "171.5", "1,200.00" or "US$171" as cents; null for anything else. */
export function centsOf(text: string): number | null {
  let t = text.trim().replace(/^(us)?\$\s*/i, "");
  if (/^\d{1,3}(,\d{3})+(\.\d*)?$/.test(t)) t = t.replace(/,/g, "");
  else if (/^\d+,\d{1,2}$/.test(t)) t = t.replace(",", ".");
  if (!/^\d+(\.\d{0,2})?$/.test(t) && !/^\.\d{1,2}$/.test(t)) return null;
  return Math.round(parseFloat(t) * 100);
}
