// The manager's phone and the counter camera: client calls and shapes.
// Shared shapes come from ../api; everything here belongs to the phone.
import { call, failure, headersFor, post, type Attachment, type Message, type Proof, type SpendRequest } from "../api";

export type Kind = "stock" | "expense" | "asset";

export type Suggested = {
  kind: Kind;
  item: string;
  quantity: number;
  unit: string;
  amount_cents: number;
  payee: string;
};

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
  suggested: Suggested;
};

export type Alert = {
  id: number;
  key: string;
  kind: "low_stock" | "cash_gap" | "stock_gap" | "proof" | string;
  title: string;
  body: string;
  data: Record<string, unknown>;
  request_id: number | null;
  created_at: string;
  dismissed: boolean;
  /** The owner's question on it waits on the manager's answer. */
  waiting?: boolean;
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
};

export type Week = { from: string; to: string; cash: CashRow[]; stock: StockRow[] };

export type Closed = {
  close: { id: number; day: string; counted_cash_cents: number; banked_cents: number; cash_expenses_cents: number };
  cash: CashRow;
  stock: StockRow[];
  alerts: Alert[];
};

export type Heard = { requests: SpendRequest[]; messages: Message[] };

export type Verdict = {
  proof: Proof | null;
  request: SpendRequest;
  attachment?: Attachment;
  message?: Message;
};

export type Reading = {
  open: boolean;
  people: number;
  busy: boolean;
  shelves: { item: string; level: "low" | "ok" | "full" }[];
  delivery: { seen: boolean; goods: string; quantity_estimate: number };
  line: string;
};

export type Frame = {
  /** null for a still the server left unkept: the same picture as the newest one. */
  id: number | null;
  kept?: boolean;
  at: string;
  kind: "sample" | "look_in" | "staged" | "feed";
  content_type: string;
  change: number;
  read: boolean;
  line: string;
  reading: Reading | null;
  url: string | null;
};

export type CameraStatus = {
  online: boolean;
  seen_at: string | null;
  /** The terms the manager agrees to on the counter page. */
  consent: string;
  consented?: boolean;
  consent_at?: string | null;
  consent_by?: string | null;
  /** For the owner: who agreed to the camera and when, or that Musha waits for the agreement. */
  consent_line?: string;
  latest: Frame | null;
  look_in_pending: boolean;
  look_in_requested_at: string | null;
  sample_seconds: number;
};

export type Beat = { look_in_requested: boolean; sample_seconds: number };

/** What a sample receipt has printed on it, from the sample's headers. */
export type Printed = { total: string; receipt_no: string; date: string; payee: string };

export const VARIANTS = [
  { key: "genuine", label: "Genuine", hint: "The receipt for this buy" },
  { key: "reused", label: "Reused", hint: "Last week's receipt again" },
  { key: "inflated", label: "Inflated", hint: "A higher total" },
  { key: "wrong_payee", label: "Wrong payee", hint: "Another supplier" },
  { key: "stale", label: "Stale", hint: "Dated before approval" },
] as const;

export type Variant = (typeof VARIANTS)[number]["key"];

export const SCENES = [
  { key: "quiet", label: "Quiet" },
  { key: "busy", label: "Busy" },
  { key: "delivery", label: "Delivery" },
  { key: "shelf_low", label: "Shelf low" },
] as const;

/** An HTTP failure that keeps its status, so the phone can tell "still reading" from a real error. */
export class PhoneError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

export const STILL_READING = "Musha is still reading that message.";

async function send<T>(path: string, init: RequestInit): Promise<T> {
  let r: Response;
  try {
    r = await fetch(path, { ...init, headers: headersFor(init, false) });
  } catch {
    throw new PhoneError("The phone is offline.", 0);
  }
  const body: unknown = await r.json().catch(() => undefined);
  if (!r.ok) throw new PhoneError(failure(r.status, body), r.status);
  return body as T;
}

async function blob(path: string, init?: RequestInit): Promise<Response> {
  let r: Response;
  try {
    r = await fetch(path, { ...init, headers: headersFor(init, false) });
  } catch {
    throw new PhoneError("The phone is offline.", 0);
  }
  if (!r.ok) throw new PhoneError(failure(r.status, await r.json().catch(() => undefined)), r.status);
  return r;
}

export const phone = {
  products: (shopId: number) => call<Product[]>(`/api/shops/${shopId}/products`),
  alerts: (shopId: number) => call<Alert[]>(`/api/shops/${shopId}/alerts`),
  week: (shopId: number) => call<Week>(`/api/shops/${shopId}/week`),

  /** The manager writes. The same key goes with every retry of one message. `replyTo` is the line she replied to. */
  say: (shopId: number, text: string, key: string, requestId?: number, replyTo?: number) =>
    send<Heard>(`/api/shops/${shopId}/phone/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": key },
      body: JSON.stringify({
        text, client_id: key, ...(requestId ? { request_id: requestId } : {}), ...(replyTo ? { reply_to: replyTo } : {}),
      }),
    }),

  /** A voice note, with the message's own id as client_id, the request whose question it answers, and the line
   *  she replied to (`replyTo`). */
  voice: (shopId: number, audio: Blob, key: string, requestId?: number, replyTo?: number) => {
    const form = new FormData();
    form.append("audio", audio, `voice${audio.type.includes("mp4") ? ".m4a" : audio.type.includes("ogg") ? ".ogg" : ".webm"}`);
    form.append("client_id", key);
    if (requestId) form.append("request_id", String(requestId));
    if (replyTo) form.append("reply_to", String(replyTo));
    return send<Heard>(`/api/shops/${shopId}/phone/voice`, {
      method: "POST",
      headers: { "Idempotency-Key": key },
      body: form,
    });
  },

  /** A receipt or a photo of the goods against a request. A receipt on a held request is proven at once. */
  evidence: (requestId: number, file: Blob, kind: "receipt" | "goods", printed?: Partial<Printed>) => {
    const form = new FormData();
    const name = file instanceof File ? file.name : `${kind}.jpg`;
    form.append("file", file, name);
    form.append("kind", kind);
    if (printed?.total) form.append("total", printed.total);
    if (printed?.receipt_no) form.append("receipt_no", printed.receipt_no);
    if (printed?.date) form.append("date", printed.date);
    if (printed?.payee) form.append("payee", printed.payee);
    return send<Verdict>(`/api/requests/${requestId}/evidence`, { method: "POST", body: form });
  },

  /** A sample receipt for this request, with what it has printed on it. */
  sample: async (requestId: number, variant: Variant) => {
    const r = await blob(`/api/requests/${requestId}/samples`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ variant }),
    });
    const printed: Printed = {
      total: r.headers.get("X-Receipt-Total") || "",
      receipt_no: r.headers.get("X-Receipt-No") || "",
      date: r.headers.get("X-Receipt-Date") || "",
      payee: r.headers.get("X-Receipt-Payee") || "",
    };
    return { image: await r.blob(), printed };
  },

  /** A sample photo of the goods. */
  goods: async (item: string) => (await blob(`/api/samples/goods/${encodeURIComponent(item)}`)).blob(),

  closeDay: (shopId: number, body: {
    units_sold: Record<string, number>;
    counted_cash: string;
    banked: string;
    cash_expenses: string;
    stock_counts: Record<string, number>;
    note?: string;
  }) => post<Closed>(`/api/shops/${shopId}/close-day`, body),

  camera: (shopId: number) => call<CameraStatus>(`/api/shops/${shopId}/camera`),
  agree: (shopId: number) => post<CameraStatus>(`/api/shops/${shopId}/camera/consent`, {}),
  frames: (shopId: number, limit = 24) => call<Frame[]>(`/api/shops/${shopId}/camera/frames?limit=${limit}`),
  heartbeat: (shopId: number) => post<Beat>(`/api/shops/${shopId}/camera/heartbeat`),
  frame: (shopId: number, image: Blob, kind: "sample" | "look_in") => {
    const form = new FormData();
    form.append("image", image, "still.jpg");
    form.append("kind", kind);
    return post<Frame>(`/api/shops/${shopId}/camera/frames`, form);
  },
  staged: (shopId: number, scene: string) => post<Frame>(`/api/shops/${shopId}/camera/staged`, { scene }),
};

/** Money typed by a person, as the API's decimal string. */
export function decimal(cents: number): string {
  return (cents / 100).toFixed(2);
}

export function toCents(text: string): number | null {
  const n = Number(String(text).replace(/[^0-9.]/g, ""));
  if (!Number.isFinite(n) || String(text).trim() === "") return null;
  return Math.round(n * 100);
}

export function newKey(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return `k-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }
}
