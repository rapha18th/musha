export type Config = {
  paypal_client_id: string;
  paypal_mode: "sandbox" | "standin";
  currency: string;
  ai: { enabled: boolean; model: string };
};

export type Books = {
  held_cents: number;
  funded_cents: number;
  returned_cents: number;
  wages_cents: number;
  sales_cents: number;
  expenses_cents: number;
  assets_cents: number;
  banked_cents: number;
  stock: Record<string, number>;
};

export type Shop = {
  id: number;
  name: string;
  place: string;
  currency: string;
  owner: { name: string; city: string; language: string };
  manager: { name: string; city: string; language: string };
  camera_seen_at: string | null;
  books: Books;
  /** A new visitor's shop comes with its keys: the owner's, the manager's and the counter phone's. */
  keys?: Keys;
  /** What the caller's key opens in this shop: owner, manager or counter. */
  role?: "owner" | "manager" | "counter";
};

export type Keys = { owner: string; manager: string; counter: string };

export type Hold = {
  authorization_id: string;
  amount_cents: number;
  captured_cents: number;
  status: string;
  authorized_at: string;
  expires_at: string;
  reauthorized_at: string | null;
};

export type Attachment = {
  id: number;
  kind: "receipt" | "goods" | "quote" | "voice" | "photo";
  content_type: string;
  size: number;
  request_id: number | null;
  uploaded_at: string;
  url: string;
  reading: Record<string, unknown> | null;
};

export type Check = {
  key: string;
  label: string;
  points: number;
  max: number;
  applies: boolean;
  passed: boolean;
  reason: string;
};

export type Proof = {
  id: number;
  round: number;
  score: number;
  decision: "release" | "ask" | "hold" | string;
  spent_cents: number;
  checks: Check[];
  question: string;
  answer: string;
  summary: string;
  receipt_id: number | null;
  elapsed_ms: number;
  created_at: string;
};

export type SpendRequest = {
  id: number;
  kind: "stock" | "expense" | "asset";
  item: string;
  quantity: number;
  unit: string;
  amount_cents: number;
  payee: string;
  words: string;
  words_en: string;
  language: string;
  origin: string;
  status: string;
  /** True while Musha reads a receipt for this request. */
  proving?: boolean;
  approved_by: string;
  order_id: string;
  created_at: string;
  approved_at: string | null;
  hold: Hold | null;
  attachments: Attachment[];
  proofs: Proof[];
};

export type Entry = {
  id: number;
  at: string;
  kind: string;
  amount_cents: number;
  item: string;
  quantity: number;
  memo: string;
  source: string;
  request_id: number | null;
};

export type Message = {
  id: number;
  thread: "manager" | "owner";
  sender: "manager" | "musha" | "owner";
  text: string;
  text_en: string;
  language: string;
  request_id: number | null;
  attachment_id: number | null;
  attachment_url: string | null;
  at: string;
};

export type NewRequest = {
  kind: string;
  item: string;
  quantity: number;
  unit: string;
  amount: string;
  payee: string;
  words: string;
};

/** An HTTP failure with a sentence the visitor can read, and the status for code that needs it. */
export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

export const OFFLINE = "Musha lost the connection. Try again.";

/**
 * The sentence for a failed response. The server's own detail when it is a sentence; otherwise one chosen
 * by status, so a plain-text 500 or a JSON object never reaches the screen.
 */
export function failure(status: number, body: unknown): string {
  const detail = body && typeof body === "object" ? (body as { detail?: unknown }).detail : undefined;
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  if (status === 502 && detail && typeof detail === "object" && "paypal_status" in detail) {
    return "PayPal answered with an error. Try again in a minute.";
  }
  if (status === 502 || status === 503 || status === 504) return "Musha is waking up. Try again in a minute.";
  if (status === 422) {
    // FastAPI's validation list: name the first field it points at
    const loc = Array.isArray(detail) ? (detail[0] as { loc?: unknown[] } | undefined)?.loc : undefined;
    const field = Array.isArray(loc) ? loc.filter((x) => typeof x === "string" && x !== "body").pop() : undefined;
    return field ? `Check the ${String(field).replace(/_/g, " ")} and send it again.` : "Check the details and send it again.";
  }
  if (status === 404) return "Musha lost track of that. Reload the page.";
  if (status === 429) return "Musha is busy. Try again in a minute.";
  return "Musha hit a snag. Try again.";
}

/**
 * The key this page holds for its shop. The browser's cookie carries it already; the header is the
 * same key for a browser that keeps no cookies.
 */
let shopKey = "";

export function setShopKey(key: string) {
  shopKey = key;
}

/** Headers for a call: the JSON type unless the body is a form, and the shop's key when the page holds one. */
export function headersFor(init?: RequestInit, json = true): HeadersInit {
  const isForm = init?.body instanceof FormData;
  return {
    ...(json && !isForm ? { "Content-Type": "application/json" } : {}),
    ...(shopKey ? { "X-Shop-Key": shopKey } : {}),
    ...((init?.headers as Record<string, string>) || {}),
  };
}

export async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let r: Response;
  try {
    r = await fetch(path, { ...init, headers: headersFor(init) });
  } catch {
    throw new ApiError(OFFLINE, 0);
  }
  const body: unknown = await r.json().catch(() => undefined);
  if (!r.ok) throw new ApiError(failure(r.status, body), r.status);
  return body as T;
}

export const post = <T,>(path: string, body?: unknown) =>
  call<T>(path, {
    method: "POST",
    body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body),
  });

export const api = {
  config: () => call<Config>("/api/config"),
  newShop: () => post<Shop>("/api/demo/shops"),
  join: (id: number, key: string) => post<{ role: string; shop: Shop }>(`/api/shops/${id}/join`, { key }),
  shop: (id: number) => call<Shop>(`/api/shops/${id}`),
  requests: (id: number) => call<SpendRequest[]>(`/api/shops/${id}/requests`),
  ledger: (id: number) => call<Entry[]>(`/api/shops/${id}/ledger`),
  messages: (id: number, thread: "manager" | "owner" = "manager") =>
    call<Message[]>(`/api/shops/${id}/messages?thread=${thread}`),
  ask: (id: number, body: NewRequest) => post<SpendRequest>(`/api/shops/${id}/requests`, body),
  order: (requestId: number) => post<{ order_id: string }>(`/api/requests/${requestId}/order`),
  authorize: (requestId: number) => post<SpendRequest>(`/api/requests/${requestId}/authorize`),
  settle: (requestId: number, spent: string, reason = "") =>
    post<unknown>(`/api/requests/${requestId}/settle`, { spent, reason }),
  wages: (id: number, amount: string, note: string) => post<unknown>(`/api/shops/${id}/wages`, { amount, note }),
};

/** The last word on the request was the busy reader's note: Musha waits for the same receipt again. */
export function rereading(r: SpendRequest): boolean {
  const last = r.proofs[r.proofs.length - 1];
  if (r.status !== "asking" || !last || last.decision !== "ask") return false;
  const read = last.checks.find((c) => c.key === "read") as (Check & { data?: Record<string, unknown> }) | undefined;
  return !!read?.data?.retry && !!read?.data?.asked;
}

export const usd = (cents: number) =>
  `US$${(cents / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
