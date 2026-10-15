export type Books = {
  held_cents: number;
  funded_cents: number;
  returned_cents: number;
  wages_cents: number;
  sales_cents: number;
  stock: Record<string, number>;
};

export type Shop = {
  id: number;
  name: string;
  place: string;
  currency: string;
  owner: { name: string; city: string };
  manager: { name: string; language: string };
  books: Books;
};

export type Hold = {
  authorization_id: string;
  amount_cents: number;
  captured_cents: number;
  status: string;
  expires_at: string;
};

export type StockRequest = {
  id: number;
  item: string;
  quantity: number;
  unit: string;
  amount_cents: number;
  supplier: string;
  words: string;
  status: string;
  order_id: string;
  created_at: string;
  hold: Hold | null;
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
};

export type Message = {
  id: number;
  sender: "manager" | "musha" | "owner";
  text: string;
  request_id: number | null;
  at: string;
};

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    throw new Error(detail);
  }
  return body as T;
}

const post = <T,>(path: string, body?: unknown) =>
  call<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  config: () => call<{ paypal_client_id: string; currency: string }>("/api/config"),
  shop: (id: number) => call<Shop>(`/api/shops/${id}`),
  requests: (id: number) => call<StockRequest[]>(`/api/shops/${id}/requests`),
  ledger: (id: number) => call<Entry[]>(`/api/shops/${id}/ledger`),
  messages: (id: number) => call<Message[]>(`/api/shops/${id}/messages`),
  ask: (id: number, body: { item: string; quantity: number; unit: string; amount: string; supplier: string; words: string }) =>
    post<StockRequest>(`/api/shops/${id}/requests`, body),
  say: (id: number, text: string) => post<{ id: number }>(`/api/shops/${id}/messages`, { text }),
  order: (requestId: number) => post<{ order_id: string }>(`/api/requests/${requestId}/order`),
  authorize: (requestId: number) => post<StockRequest>(`/api/requests/${requestId}/authorize`),
  settle: (requestId: number, spent: string, reason = "") =>
    post<unknown>(`/api/requests/${requestId}/settle`, { spent, reason }),
  wages: (id: number, amount: string, note: string) => post<unknown>(`/api/shops/${id}/wages`, { amount, note }),
};

export const usd = (cents: number) =>
  `US$${(cents / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
