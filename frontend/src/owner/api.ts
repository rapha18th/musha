/** The owner's console talks to these endpoints. Shapes follow backend/musha/routes. Money is integer cents. */
import { call, post, type Attachment, type Message, type Proof, type SpendRequest } from "../api";

export type OwnerRequest = SpendRequest & { inferred?: string[] };

export type CheckRow = Proof["checks"][number] & { data?: Record<string, unknown> };

export type LowStockData = {
  kind: string;
  item: string;
  quantity: number;
  unit: string;
  amount_cents: number;
  payee: string;
  on_hand: number;
  days_left: number | null;
  avg_daily: number;
  asked_at?: string;
};

export type CashGapData = {
  day: string;
  gap_cents: number;
  question: string;
  expected_cents: number;
  counted_cents: number;
  asked_at?: string;
};

export type StockGapData = {
  day: string;
  item: string;
  unit: string;
  gap: number;
  question: string;
  expected: number;
  counted: number;
  asked_at?: string;
};

export type ProofAlertData = {
  request_id: number;
  proof_id: number;
  score: number;
  failed: string[];
  held_cents: number;
  spent_cents: number;
  /** The manager typed the receipt values while the reader was off; the engine held a release for the owner. */
  typed?: boolean;
};

export type Alert = {
  id: number;
  key: string;
  kind: "low_stock" | "cash_gap" | "stock_gap" | "proof" | "mandate" | string;
  title: string;
  body: string;
  data: Record<string, unknown>;
  request_id: number | null;
  created_at: string;
  dismissed: boolean;
};

export type ActResult = { alert: Alert; message: Message | null; request: OwnerRequest | null };

export type Product = {
  id: number;
  name: string;
  unit: string;
  sell_cents: number;
  usual_payee: string;
  pack: number;
  on_hand: number;
  avg_daily: number;
  days_left: number | null;
  low: boolean;
  suggested: { kind: string; item: string; quantity: number; unit: string; amount_cents: number; payee: string };
};

export type CashRow = {
  day: string;
  day_name: string;
  closed: boolean;
  opening_cents: number;
  cash_sales_cents: number;
  cash_expenses_cents: number;
  banked_cents: number;
  expected_close_cents: number;
  counted_cents: number | null;
  gap_cents: number | null;
  question: string | null;
  /** The manager's answer to the gap's question, in English, once it came. */
  answer?: string | null;
  answered_at?: string | null;
};

export type StockRow = {
  day: string;
  day_name: string;
  item: string;
  unit: string;
  opening: number;
  stock_in: number;
  sold: number;
  expected: number;
  counted: number | null;
  gap: number | null;
  question: string | null;
  answer?: string | null;
  answered_at?: string | null;
};

export type WeekFacts = {
  shop: string;
  place: string;
  owner: string;
  manager: string;
  currency: string;
  from: string;
  to: string;
  sales_cents: number;
  cogs_cents: number;
  margin_cents: number;
  margin_pct: number;
  items: { item: string; unit: string; sold: number; sales_cents: number; cogs_cents: number; margin_cents: number }[];
  stock: { item: string; unit: string; on_hand: number; avg_daily: number; days_left: number | null; low: boolean }[];
  cash: {
    days_closed: number;
    gap_cents: number;
    by_day: { day: string; day_name: string; expected_cents: number; counted_cents: number; gap_cents: number }[];
    gaps: { day: string; day_name: string; gap_cents: number }[];
  };
  stock_gaps: { day: string; day_name: string; item: string; unit: string; gap: number }[];
  banked_cents: number;
  money: { held_cents: number; released_cents: number; returned_cents: number };
  wages_cents: number;
  open_requests: { id: number; kind: string; item: string; amount_cents: number; status: string }[];
  camera: { at: string; line: string } | null;
};

export type Week = { from: string; to: string; cash: CashRow[]; stock: StockRow[]; facts: WeekFacts };

export type Frame = {
  id: number;
  at: string;
  kind: string;
  content_type: string;
  change: number;
  read: boolean;
  line: string;
  reading: {
    open: boolean;
    people: number;
    busy: boolean;
    shelves: { item: string; level: "low" | "ok" | "full" }[];
    delivery: { seen: boolean; goods: string; quantity_estimate: number };
    line: string;
  } | null;
  url: string;
};

export type CameraStatus = {
  online: boolean;
  seen_at: string | null;
  consent: string;
  consented?: boolean;
  consent_line?: string;
  latest: Frame | null;
  look_in_pending: boolean;
  look_in_requested_at: string | null;
  sample_seconds: number;
};

export type Decision = {
  request_id: number | null;
  item: string;
  amount_cents: number;
  decision: "approved" | "waiting" | "asked";
  /** Who asked for the request: "musha" for stock Musha asked for itself. */
  origin: string;
  status: string;
  at: string | null;
  line: string;
};

export type Mandate = {
  shop_id: number;
  active: boolean;
  saved: boolean;
  ready: boolean;
  payer_email: string | null;
  limit_cents: number;
  weekly_cap_cents: number;
  kinds: string[];
  known_payees_only: boolean;
  price_tolerance_pct: number;
  /** Musha asks for stock itself when it runs low, while the agent is on. */
  restock: boolean;
  week: { since: string; agent_cents: number; left_cents: number };
  decisions: Decision[];
  updated_at: string | null;
};

export type MandateIn = Partial<{
  active: boolean;
  limit: string;
  weekly_cap: string;
  kinds: string[];
  known_payees_only: boolean;
  price_tolerance_pct: number;
  restock: boolean;
}>;

export type Assessment = {
  inside: boolean;
  reasons: string[];
  checks: { key: string; label: string; ok: boolean; detail: string }[];
  price: { unit_cents: number; median_cents: number; change_pct: number } | null;
  recommendation: string;
};

export type Reconcile = {
  window: { start: string; end: string };
  matched: { kind: string; ref: string; paypal_id: string; code: string; amount_cents: number; item: string; request_id: number | null; at: string; note?: string }[];
  missing_in_books: { paypal_id: string; reference: string; code: string; what: string; amount_cents: number; status: string; at: string; invoice_id: string }[];
  missing_in_paypal: { kind: string; ref: string; paypal_id: string; amount_cents: number; item: string; request_id: number | null; at: string }[];
  mismatched: { kind: string; ref: string; paypal_id: string; code: string; book_cents: number; paypal_cents: number; reason: string; amount_cents: number; item: string; request_id: number | null; at: string }[];
  summary: string;
  by: "rules" | "model";
  agent: null | { tools: string; rounds: number; calls: { tool: string; args: unknown; ok: boolean }[] };
};

export type Verdict = { proof: Proof | null; request: OwnerRequest };

const put = <T,>(path: string, body: unknown) => call<T>(path, { method: "PUT", body: JSON.stringify(body) });
const del = <T,>(path: string) => call<T>(path, { method: "DELETE" });

export const ownerApi = {
  alerts: (shopId: number) => call<Alert[]>(`/api/shops/${shopId}/alerts`),
  act: (alertId: number, action: "fund" | "ask") => post<ActResult>(`/api/alerts/${alertId}/act`, { action }),
  dismiss: (alertId: number) => post<Alert>(`/api/alerts/${alertId}/dismiss`),
  products: (shopId: number) => call<Product[]>(`/api/shops/${shopId}/products`),
  week: (shopId: number) => call<Week>(`/api/shops/${shopId}/week`),
  camera: (shopId: number) => call<CameraStatus>(`/api/shops/${shopId}/camera`),
  lookIn: (shopId: number) => post<CameraStatus>(`/api/shops/${shopId}/look-in`),
  ownerMessages: (shopId: number) => call<Message[]>(`/api/shops/${shopId}/owner/messages`),
  ask: (shopId: number, question: string) =>
    post<{ question: Message; answer: Message }>(`/api/shops/${shopId}/ask`, { question }),
  toManager: (shopId: number, text: string) => post<Message>(`/api/shops/${shopId}/owner/to-manager`, { text }),
  assessment: (requestId: number) => call<Assessment>(`/api/requests/${requestId}/assessment`),
  /** Says no to a request Musha asked for itself, while it waits for the owner. */
  decline: (requestId: number) => post<OwnerRequest>(`/api/requests/${requestId}/decline`),
  decide: (requestId: number, action: "release" | "return", amount?: string) =>
    post<Verdict>(`/api/requests/${requestId}/decide`, amount ? { action, amount } : { action }),
  mandate: (shopId: number) => call<Mandate>(`/api/shops/${shopId}/mandate`),
  setMandate: (shopId: number, body: MandateIn) => put<Mandate>(`/api/shops/${shopId}/mandate`, body),
  setupToken: (shopId: number) => post<{ id: string }>(`/api/shops/${shopId}/mandate/setup-token`),
  paymentToken: (shopId: number, vaultSetupToken: string) =>
    post<{ saved: boolean }>(`/api/shops/${shopId}/mandate/payment-token`, { vault_setup_token: vaultSetupToken }),
  removeVault: (shopId: number) => del<Mandate>(`/api/shops/${shopId}/mandate/vault`),
  /** With `agent` false, the exact match at once; with it on, the agent's summary as well. */
  reconcile: (shopId: number, agent = true) =>
    post<Reconcile>(`/api/shops/${shopId}/reconcile${agent ? "" : "?agent=false"}`),
  /** Wages are a request the owner approves with PayPal; Musha pays the manager once PayPal holds it. */
  wages: (shopId: number, amount: string, note: string) =>
    post<{ request_id: number; status: string; batch_id: string; request: OwnerRequest }>(`/api/shops/${shopId}/wages`, { amount, note }),
};

export type Evidence = {
  key: string;
  kind: Attachment["kind"] | "camera";
  url: string;
  label: string;
  at: string | null;
  reading: Record<string, unknown> | null;
  note?: string;
  /** A short mark on the thumbnail, such as "Sent before", with the engine's line behind it. */
  flag?: string;
  flagNote?: string;
};
