import { useCallback, useEffect, useRef, useState } from "react";
import { rereading } from "../api";
import type { OwnerRequest } from "./api";

/** Words for each request status, and the colour family that carries its meaning. */
export const STATUS: Record<string, { label: string; tone: "clay" | "warn" | "leaf" | "danger" | "calm" | "busy" }> = {
  requested: { label: "Waiting for you", tone: "clay" },
  approving: { label: "Approving", tone: "clay" },
  agent_pending: { label: "PayPal is answering", tone: "busy" },
  reviewing: { label: "PayPal is reviewing", tone: "busy" },
  held: { label: "Held, waiting for proof", tone: "warn" },
  asking: { label: "Musha asked a question", tone: "warn" },
  rereading: { label: "Waiting for the receipt again", tone: "warn" },
  paid: { label: "Paid", tone: "leaf" },
  capturing: { label: "PayPal is confirming", tone: "busy" },
  captured: { label: "Released", tone: "leaf" },
  part_captured: { label: "Released, change returned", tone: "leaf" },
  voided: { label: "Returned to you", tone: "calm" },
  flagged: { label: "Held for you", tone: "danger" },
};

export const statusOf = (s: string) => STATUS[s] ?? { label: s, tone: "calm" as const };

/** The status word a request shows: wages read paid once captured, and a busy reader's wait reads plainly. */
export function shownStatus(r: OwnerRequest): string {
  if (r.origin === "wages" && (r.status === "captured" || r.status === "part_captured")) return "paid";
  return rereading(r) ? "rereading" : r.status;
}

export const KIND_LABEL: Record<string, string> = { stock: "Stock", expense: "Expense", asset: "Asset" };

export const canApprove = (r: OwnerRequest) => r.status === "requested" || r.status === "approving";

/** Statuses where a request is over: its money released, split or returned. */
export const SETTLED = ["captured", "part_captured", "voided"];

/** Items with a request still in motion. A low-stock prompt for one of these waits: the request answers it. */
export function openItems(requests: { item: string; status: string }[]): Set<string> {
  return new Set(requests.filter((r) => !SETTLED.includes(r.status)).map((r) => r.item.trim().toLowerCase()));
}

import { qty } from "../words";

export { qty, qtyParts } from "../words";

export function titleOf(r: { kind: string; item: string; quantity: number; unit: string }): string {
  if ((r.kind === "stock" && r.quantity > 0) || r.quantity > 1) {
    return r.unit.trim() ? `${qty(r.quantity, r.unit)} of ${r.item}` : `${r.quantity} ${r.item}`;
  }
  return cap(r.item);
}

export const cap = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

export const dollars = (cents: number) => (cents / 100).toFixed(2);

const TIME = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit" });
const DAY = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });
const WEEKDAY = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short" });

export const clock = (iso: string | null | undefined) => (iso ? TIME.format(new Date(iso)) : "");
export const day = (iso: string | null | undefined) => (iso ? DAY.format(new Date(iso)) : "");
export const weekday = (iso: string) => WEEKDAY.format(new Date(iso.length === 10 ? `${iso}T12:00:00` : iso));
const LONG_DAY = new Intl.DateTimeFormat("en-GB", { weekday: "long", day: "numeric", month: "long" });
/** "Thursday 29 October". */
export const longDay = (iso: string) => LONG_DAY.format(new Date(iso.length === 10 ? `${iso}T12:00:00` : iso)).replace(",", "");

/** "13:01 today", "09:20 on 20 Oct". */
export function when(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return `${clock(iso)} today`;
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return `${clock(iso)} yesterday`;
  return `${clock(iso)} on ${day(iso)}`;
}

/** Runs `fn` now and every `ms` while mounted. Returns a manual trigger. */
export function usePoll(fn: () => Promise<unknown> | void, ms: number, deps: unknown[] = []) {
  const ref = useRef(fn);
  ref.current = fn;
  const run = useCallback(() => Promise.resolve(ref.current()).then(() => undefined, () => undefined), []);
  useEffect(() => {
    run();
    const t = window.setInterval(run, ms);
    return () => {
      window.clearInterval(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, run, ...deps]);
  return run;
}

/** "2026-10-24" and "09:20" as printed on a receipt, read back as "24 Oct 2026, 09:20". */
export function printed(date: unknown, time: unknown): string {
  const d = typeof date === "string" ? date.trim() : "";
  const t = typeof time === "string" ? time.trim() : "";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(d);
  const shown = m
    ? new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" })
      .format(new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])))
    : d;
  return [shown, t].filter(Boolean).join(", ");
}

/** True while the element is narrower than `px`: a grid picks its columns from the room it has. */
export function useCompact<T extends HTMLElement>(px: number): [(el: T | null) => void, boolean] {
  const [el, setEl] = useState<T | null>(null);
  const [compact, setCompact] = useState(() => typeof window !== "undefined" && window.innerWidth < px);
  useEffect(() => {
    if (!el) return;
    const obs = new ResizeObserver(([e]) => {
      setCompact(e.contentRect.width < px);
    });
    obs.observe(el);
    return () => {
      obs.disconnect();
    };
  }, [el, px]);
  return [setEl, compact];
}

/** The page theme as shell.ts sets it on <html data-theme>. */
export function useDocTheme(): "light" | "dark" {
  const read = () => (document.documentElement.dataset.theme === "dark" ? "dark" : "light");
  const [theme, setTheme] = useState<"light" | "dark">(read);
  useEffect(() => {
    const obs = new MutationObserver(() => {
      setTheme(read());
    });
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => {
      obs.disconnect();
    };
  }, []);
  return theme;
}

/** A per-viewer convenience kept in localStorage; the page works the same without it. */
export function useStored<T extends string>(key: string, initial: T, allowed: readonly T[]): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const v = window.localStorage.getItem(key) as T | null;
      return v && allowed.includes(v) ? v : initial;
    } catch {
      return initial;
    }
  });
  const set = useCallback((v: T) => {
    setValue(v);
    try {
      window.localStorage.setItem(key, v);
    } catch {
      // the choice lasts for this page only
    }
  }, [key]);
  return [value, set];
}

/** Async action state: busy flag and error line, for buttons that call the API. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const run = useCallback(async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await fn();
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setBusy(false);
    }
  }, []);
  return { busy, error, setError, run };
}
