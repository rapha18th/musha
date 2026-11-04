// The Money view's data, licence and chart theme. Loaded with the Money tab alone, so AG Grid Enterprise
// and AG Charts Enterprise stay out of the console's first load.
import {
  AreaSeriesModule, BarSeriesModule, CategoryAxisModule, CrossLinesModule, DonutSeriesModule, LegendModule,
  LineSeriesModule, ModuleRegistry, NumberAxisModule, type AgChartTheme,
} from "ag-charts-community";
import { AnimationModule, LicenseManager as ChartsLicence, RadialGaugeModule } from "ag-charts-enterprise";
import { LicenseManager } from "ag-grid-enterprise";
import { call, post, type Config } from "../api";

/** Only the charts the Money view draws: the river, the answer, the cash gap, the proof bars and the hold clock. */
ModuleRegistry.registerModules([
  AreaSeriesModule, BarSeriesModule, LineSeriesModule, DonutSeriesModule, CategoryAxisModule, NumberAxisModule,
  LegendModule, CrossLinesModule, RadialGaugeModule, AnimationModule,
]);

/** /api/config carries the AG Grid licence at runtime, so the key stays out of the built bundle. */
export type MoneyConfig = Config & { ag_grid_key?: string };

let licensed = false;

/** Sets the AG Grid and AG Charts Enterprise licence once, before the first grid or chart draws.
 *  With an empty key both draw their trial watermark and the view works the same. */
export function licence(config: MoneyConfig) {
  if (licensed) return;
  licensed = true;
  const key = config.ag_grid_key?.trim();
  if (!key) return;
  LicenseManager.setLicenseKey(key);
  ChartsLicence.setLicenseKey(key);
}

export type Measure = "held" | "released" | "returned" | "open";
export type Group = "week" | "supplier" | "item" | "kind" | "by" | "state";
export type EventKind = "authorization" | "reauthorization" | "capture" | "void" | "return" | "payout";

export type PayPalEvent = {
  at: string;
  kind: EventKind;
  /** PayPal's own id: the authorization, the capture or the payout batch. Empty for the opening books. */
  paypal_id: string;
  amount_cents: number;
  status: string;
  source: string;
};

export type ProofCheck = { key: string; label: string; points: number; max: number; applies: boolean; passed: boolean; reason: string };

export type MoneyProof = {
  id: number;
  round: number;
  score: number;
  decision: string;
  spent_cents: number;
  summary: string;
  created_at: string;
  checks: ProofCheck[];
};

/** One request PayPal held money for. */
export type MoneyRow = {
  id: number;
  approved_at: string;
  approved_day: string;
  week: string;
  item: string;
  title: string;
  kind: string;
  supplier: string;
  by: string;
  approved_by: string;
  held_cents: number;
  released_cents: number;
  returned_cents: number;
  open_cents: number;
  state: "review" | "held" | "flagged" | "released" | "part" | "returned";
  status: string;
  authorization_id: string;
  seeded: boolean;
  score: number | null;
  proof: MoneyProof | null;
  events: PayPalEvent[];
};

export type RiverWeek = {
  week: string;
  held_cents: number;
  released_cents: number;
  returned_cents: number;
  open_cents: number;
  captures: string[];
  voids: string[];
  authorizations: string[];
};

export type OpenHold = {
  request_id: number;
  title: string;
  item: string;
  amount_cents: number;
  authorization_id: string;
  state: string;
  authorized_at: string;
  expires_at: string;
  honor_until: string;
  renew_at: string | null;
  void_at: string;
  reauthorized_at: string | null;
  life_days: number;
  days_held: number;
  days_left: number;
};

export type MoneyData = {
  now: string;
  currency: string;
  rows: MoneyRow[];
  river: RiverWeek[];
  holds: { open: OpenHold[]; closed: number; median_hours: number | null };
  /** The events count PayPal's own ids; `opening` counts the opening books' buys, which never touched PayPal. */
  totals: Record<`${Measure}_cents`, number> & { requests: number; authorizations: number; captures: number; voids: number; payouts: number; opening: number };
};

export type Spec = {
  filter: {
    since: string | null;
    until: string | null;
    items: string[];
    suppliers: string[];
    by: string[];
    kinds: string[];
    states: string[];
    has: Measure | null;
  };
  group: Group | null;
  sort: { column: string; direction: "asc" | "desc" } | null;
  chart: { type: "bar" | "donut"; measure: Measure } | null;
  title: string;
};

export type Answer = { question: string; spec: Spec; answer: string; by: "model" | "rules" };

export const moneyApi = {
  money: (shopId: number) => call<MoneyData>(`/api/shops/${shopId}/money`),
  ask: (shopId: number, question: string) => post<Answer>(`/api/shops/${shopId}/money/ask`, { question }),
};

export const MEASURE_LABEL: Record<Measure, string> = {
  held: "Held", released: "Released", returned: "Returned", open: "Held now",
};

export const GROUP_LABEL: Record<Group, string> = {
  week: "Week", supplier: "Supplier", item: "Item", kind: "Kind", by: "Asked by", state: "PayPal",
};

export const STATE: Record<MoneyRow["state"], { label: string; tone: string }> = {
  review: { label: "PayPal is reviewing", tone: "busy" },
  held: { label: "Held at PayPal", tone: "warn" },
  flagged: { label: "Held for you", tone: "danger" },
  released: { label: "Released", tone: "leaf" },
  part: { label: "Released, change back", tone: "leaf" },
  returned: { label: "Returned to you", tone: "clay" },
};

export const EVENT_LABEL: Record<EventKind, string> = {
  authorization: "PayPal held the money",
  reauthorization: "Musha renewed the hold",
  capture: "PayPal released it to the shop",
  void: "PayPal voided the hold",
  return: "The rest went back",
  payout: "Payout to the shop's PayPal",
};

const DAY = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });

/** "2 Nov" from "2026-11-02", read as a calendar day wherever the viewer is. */
export function dayLabel(iso: string): string {
  const d = new Date(`${String(iso).slice(0, 10)}T12:00:00`);
  return Number.isNaN(d.getTime()) ? String(iso) : DAY.format(d);
}
export const weekLabel = (iso: string) => `Week of ${dayLabel(iso)}`;

/** The house colours, read from the page so the charts follow light and dark with the rest of the console. */
export type Palette = Record<
  "ink" | "muted" | "line" | "paper" | "sand" | "cream" | "clay" | "clayInk" | "claySoft" | "leaf" | "leafSoft"
  | "warn" | "warnSoft" | "danger" | "dangerSoft", string>;

export function readPalette(): Palette {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string) => css.getPropertyValue(`--${name}`).trim();
  return {
    ink: v("ink"), muted: v("muted"), line: v("line"), paper: v("paper"), sand: v("sand"), cream: v("cream"),
    clay: v("clay"), clayInk: v("clay-ink"), claySoft: v("clay-soft"), leaf: v("leaf"), leafSoft: v("leaf-soft"),
    warn: v("warn"), warnSoft: v("warn-soft"), danger: v("danger"), dangerSoft: v("danger-soft"),
  };
}

/** AG Charts in Musha's clay theme: Inter, the house inks, paper tooltips, and leaf, clay and amber for the money. */
export function chartTheme(p: Palette, dark: boolean): AgChartTheme {
  return {
    baseTheme: dark ? "ag-default-dark" : "ag-default",
    palette: { fills: [p.leaf, p.clay, p.warn, p.muted, p.danger], strokes: [p.leaf, p.clay, p.warn, p.muted, p.danger] },
    params: {
      fontFamily: "Inter, system-ui, sans-serif",
      fontSize: 12,
      backgroundColor: p.paper,
      foregroundColor: p.ink,
      textColor: p.ink,
      subtleTextColor: p.muted,
      borderColor: p.line,
      gridLineColor: p.line,
      axisLineColor: p.line,
      tooltipBackgroundColor: p.paper,
      tooltipTextColor: p.ink,
      tooltipSubtleTextColor: p.muted,
      tooltipBorder: { color: p.line },
      tooltipBorderRadius: 12,
      chartPadding: 4,
    },
  };
}

/** `a` mixed into `b` by `t` (0 to 1), for hex colours: the canvas draws plain colours. */
export function mix(a: string, b: string, t: number): string {
  const rgb = (h: string) => {
    const x = h.replace("#", "");
    const n = x.length === 3 ? x.split("").map((c) => c + c).join("") : x;
    return [0, 2, 4].map((i) => parseInt(n.slice(i, i + 2), 16));
  };
  const [ca, cb] = [rgb(a), rgb(b)];
  if (ca.some(Number.isNaN) || cb.some(Number.isNaN)) return a;
  return `#${ca.map((v, i) => Math.round(v * t + cb[i] * (1 - t)).toString(16).padStart(2, "0")).join("")}`;
}
