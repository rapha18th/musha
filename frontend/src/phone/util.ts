import type { Message, SpendRequest } from "../api";
import { qty } from "../words";

const LANGUAGES: Record<string, string> = { en: "English", sn: "Shona", nd: "Ndebele", zu: "isiZulu" };

export function languageName(code: string): string {
  return LANGUAGES[code] || code || "English";
}

export function clock(iso: string | number | Date): string {
  return new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

function dayKey(d: Date): string {
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

export function sameDay(a: string, b: string): boolean {
  return dayKey(new Date(a)) === dayKey(new Date(b));
}

/** "Today", "Yesterday", or "Monday 26 October". */
export function dayLabel(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (dayKey(d) === dayKey(now)) return "Today";
  if (dayKey(d) === dayKey(y)) return "Yesterday";
  return d.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
}

/** "40 trays of eggs" for stock, the item itself for anything else. */
export function describe(r: Pick<SpendRequest, "kind" | "quantity" | "unit" | "item"> & { origin?: string }): string {
  if (r.origin === "wages") return r.item ? r.item[0].toUpperCase() + r.item.slice(1) : "Wages";
  return r.kind === "stock" ? `${qty(r.quantity, r.unit)} of ${r.item}` : r.item;
}

export type Tone = "clay" | "warn" | "leaf" | "danger" | "muted";

/** How a request's status reads on Farai's phone. */
export function statusOf(status: string, owner: string): { label: string; tone: Tone } {
  switch (status) {
    case "requested":
      return { label: `With ${owner}`, tone: "clay" };
    case "approving":
      return { label: `${owner} is approving`, tone: "clay" };
    case "agent_pending":
      return { label: "PayPal is answering", tone: "muted" };
    case "reviewing":
      return { label: "PayPal is reviewing", tone: "muted" };
    case "held":
      return { label: "Held. Pay and send proof", tone: "warn" };
    case "asking":
      return { label: "Musha asked a question", tone: "warn" };
    case "capturing":
      return { label: "PayPal is confirming", tone: "muted" };
    case "captured":
      return { label: "Released", tone: "leaf" };
    case "part_captured":
      return { label: "Part released", tone: "leaf" };
    case "voided":
      return { label: `Returned to ${owner}`, tone: "muted" };
    case "flagged":
      return { label: `${owner} is checking`, tone: "danger" };
    default:
      return { label: status, tone: "muted" };
  }
}

/** Is this Musha's line asking the request's open question? Its English matches the proof's question. */
export function asks(m: Message, r: SpendRequest | undefined): boolean {
  if (!r || r.status !== "asking" || m.sender !== "musha" || m.request_id !== r.id) return false;
  const q = [...r.proofs].reverse().find((p) => p.question)?.question?.trim();
  return !q || (m.text_en || "").trim() === q || m.text.trim() === q;
}

/** Requests that take proof from the phone: money held, or a question waiting. */
export const PROVABLE = ["held", "asking"];

/** Musha's last read of this receipt failed and it asked for the numbers: the next receipt goes with them typed. */
export function wantsNumbers(r: SpendRequest): boolean {
  if (r.status !== "asking") return false;
  const read = [...r.proofs].reverse().find((p) => p.question)?.checks?.find((c) => c.key === "read");
  return !!read && !read.passed;
}

/** A receipt goes with typed numbers while the model is off, or after Musha asked for them. */
export function typesNumbers(r: SpendRequest, ai: boolean): boolean {
  return !ai || wantsNumbers(r);
}

export function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

export function seconds(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export function cap(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}
