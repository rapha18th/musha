// The manager's record, read by the owner's console and the manager's phone. Shapes follow
// backend/musha/insights.py (record, line_by). Every number comes from the books.
import { useEffect, useRef, useState } from "react";
import { call, usd } from "./api";

export type RecordBuy = {
  request_id: number;
  item: string;
  what: string;
  payee: string;
  kind: string;
  amount_cents: number;
  at: string;
  receipt: boolean;
  /** The release is booked under PayPal's own capture ID, the one Transaction Search matches. */
  paypal: boolean;
  /** The buy came with the opening books. */
  opening: boolean;
  released_by: "manager" | "owner" | null;
  /** The request's origin: "musha" for stock Musha asked for itself. */
  asked_by: string;
};

export type ManagerRecord = {
  manager: string;
  owner: string;
  buys: number;
  since: string | null;
  total_cents: number;
  receipts: number;
  paypal: number;
  opening: number;
  /** Buys the owner released herself, apart from the proved ones. */
  by_owner: number;
  by_owner_cents: number;
  latest: RecordBuy | null;
  items: RecordBuy[];
};

/** Who each line in the books belongs to, by ledger id: "musha" for a hold Musha asked for and its agent placed. */
export type LineBy = Record<string, "manager" | "owner" | "agent" | "musha">;

export const recordApi = {
  get: (shopId: number) => call<ManagerRecord>(`/api/shops/${shopId}/record`),
  by: (shopId: number) => call<LineBy>(`/api/shops/${shopId}/ledger/by`),
};

const LONG_DAY = new Intl.DateTimeFormat("en-GB", { weekday: "long", day: "numeric", month: "long" });

/** "8 buys proved", "1 buy proved". */
export const buysLine = (r: ManagerRecord) => `${r.buys} ${r.buys === 1 ? "buy" : "buys"} proved`;

/** "since Wednesday 21 October". */
export const sinceLine = (r: ManagerRecord) => (r.since ? `since ${LONG_DAY.format(new Date(r.since)).replace(",", "")}` : "");

/** "US$825.12 released to the shop". */
export const totalLine = (r: ManagerRecord) => `${usd(r.total_cents)} released to the shop`;

/**
 * The record's facts, each a short phrase: the receipts on file, then PayPal's part. `matched` counts the
 * proved buys the last match of PayPal to the books found on PayPal's own list; before a match, and
 * while PayPal lists none of them, the releases read as made through PayPal.
 */
export function recordFacts(r: ManagerRecord, matched?: number): string[] {
  const out = [r.receipts === r.buys ? "Every receipt on file" : `${r.receipts} of ${r.buys} receipts on file`];
  if (r.paypal) {
    // PayPal lists a transaction up to three hours after it happens: until it lists one, the releases read as made.
    if (!matched) out.push(`${r.paypal} released through PayPal`);
    else out.push(matched === r.paypal ? `${matched} matched to PayPal` : `${matched} of ${r.paypal} matched to PayPal`);
  }
  if (r.opening) out.push(`${r.opening} from the opening books`);
  if (r.by_owner) out.push(`${r.by_owner} released by ${r.owner}`);
  return out;
}

/** The proved buys whose release PayPal's own list matched, given the request ids of the matched releases. */
export const matchedBuys = (r: ManagerRecord, released: Set<number>) =>
  r.items.filter((b) => b.released_by === "manager" && b.paypal && released.has(b.request_id)).length;

/**
 * The record for a shop, read again whenever `signal` changes: a request settled or a line reached
 * the books. A failed read keeps the last record on screen.
 */
export function useRecord(shopId: number, signal: string): ManagerRecord | undefined {
  const [record, setRecord] = useState<ManagerRecord>();
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    recordApi.get(shopId).then((r) => {
      if (alive.current) setRecord((prev) => (prev && JSON.stringify(prev) === JSON.stringify(r) ? prev : r));
    }, () => undefined);
  }, [shopId, signal]);
  return record;
}

/** A signal that moves when a request's money settles. */
export const settledSignal = (requests: { id: number; status: string }[]) =>
  requests.filter((r) => r.status === "captured" || r.status === "part_captured" || r.status === "voided")
    .map((r) => `${r.id}:${r.status}`).join(",");
