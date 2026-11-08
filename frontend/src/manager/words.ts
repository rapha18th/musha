// Small words for the manager's dashboard. The request statuses read as on the phone (../phone/util).
import type { Tone } from "../phone/util";

/** The console's colour family for a status tone: o-tone-clay, o-tone-warn and the rest in owner.css. */
export const toneOf = (t: Tone): string => (t === "muted" ? "calm" : t);

/** Requests that are over: their money released, split or returned, or declined. */
export const SETTLED = ["captured", "part_captured", "voided", "declined"];

/** A shelf's days left at this pace, in whole days: "2 days", "1 day", "under a day". */
export function daysLeft(left: number | null | undefined): string {
  const n = Math.round(left ?? 0);
  return n < 1 ? "under a day" : n === 1 ? "1 day" : `${n} days`;
}
