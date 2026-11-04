import { useEffect, useMemo, useRef, useState, type ChangeEvent, type FormEvent, type ReactNode } from "react";
import { api, usd, type Shop, type SpendRequest } from "../api";
import { decimal, phone, toCents, VARIANTS, type Closed, type Kind, type Printed, type Product, type Suggested, type Variant, type Week } from "./api";
import { Box, Camera, Close, Receipt } from "./icons";
import { cap, describe, statusOf, todayUtc, typesNumbers } from "./util";
import { qty } from "../words";

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Everything outside `el` goes inert while it is open: the siblings of `el` and of each of its ancestors.
 * Returns the undo. Elements that were inert already stay inert.
 */
function isolate(el: HTMLElement, keep: (n: Element) => boolean): () => void {
  const made: Element[] = [];
  for (let node: HTMLElement | null = el; node && node !== document.body; node = node.parentElement) {
    const parent: HTMLElement | null = node.parentElement;
    if (!parent) break;
    for (const sib of Array.from(parent.children)) {
      if (sib === node || keep(sib) || sib.hasAttribute("inert")) continue;
      sib.setAttribute("inert", "");
      made.push(sib);
    }
  }
  return () => {
    made.forEach((n) => n.removeAttribute("inert"));
  };
}

/** A sheet that rises inside the phone. It keeps focus while open and hands it back to its opener on close. */
export function Sheet({ title, sub, close, children, footer }: {
  title: string;
  sub?: ReactNode;
  close: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", on);
    return () => {
      window.removeEventListener("keydown", on);
    };
  }, [close]);

  useEffect(() => {
    const el = panel.current;
    if (!el) return;
    const opener = document.activeElement instanceof HTMLElement && document.activeElement !== document.body
      ? document.activeElement : null;
    // The page behind the scrim leaves the tab order and the accessibility tree.
    const undo = isolate(el, (n) => n.classList.contains("ph-scrim"));
    el.focus({ preventScroll: true });
    const trap = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const items = Array.from(el.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((n) => n.offsetParent !== null || n === document.activeElement);
      if (!items.length) {
        e.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const at = document.activeElement;
      if (e.shiftKey && (at === first || at === el)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && at === last) {
        e.preventDefault();
        first.focus();
      }
    };
    el.addEventListener("keydown", trap);
    return () => {
      el.removeEventListener("keydown", trap);
      undo();
      // Back to the button that opened the sheet; when it has gone, to the phone's message box.
      const back = opener?.isConnected ? opener : document.querySelector<HTMLElement>(".ph-compose .ph-field input");
      back?.focus({ preventScroll: true });
    };
  }, []);

  return (
    <>
      <div className="ph-scrim" onClick={close} />
      <section className="ph-sheet" role="dialog" aria-modal="true" aria-label={title} ref={panel} tabIndex={-1}>
        <span className="ph-grab" aria-hidden="true" />
        <header className="ph-sheet-head">
          <div>
            <h3>{title}</h3>
            {sub && <p>{sub}</p>}
          </div>
          <button type="button" className="ph-x" onClick={close} aria-label="Close"><Close /></button>
        </header>
        <div className="ph-sheet-body">{children}</div>
        {footer && <footer className="ph-sheet-foot">{footer}</footer>}
      </section>
    </>
  );
}

function Segmented<T extends string>({ value, options, onChange, label }: {
  value: T;
  options: { key: T; label: string }[];
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <div className="ph-seg" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button key={o.key} type="button" role="radio" aria-checked={value === o.key} onClick={() => onChange(o.key)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

const Loading = () => <div className="ph-sheet-loading"><span /><span /><span /></div>;

// Ask for money

type Form = { kind: Kind; item: string; quantity: string; unit: string; amount: string; payee: string };

const KINDS: { key: Kind; label: string }[] = [
  { key: "stock", label: "Stock" },
  { key: "expense", label: "Expense" },
  { key: "asset", label: "Asset" },
];

function fromSuggestion(s: Suggested): Form {
  return { kind: s.kind, item: s.item, quantity: String(s.quantity), unit: s.unit, amount: decimal(s.amount_cents), payee: s.payee };
}

/** The usual price of a product for a quantity, rounded up to whole dollars like Musha's suggestions. */
function estimate(p: Product, quantity: number): string {
  const each = p.suggested.quantity ? p.suggested.amount_cents / p.suggested.quantity : 0;
  return decimal(Math.ceil((quantity * each) / 100) * 100);
}

/** Requests still in motion: an item among these needs no second ask. */
const SETTLED = ["captured", "part_captured", "voided"];

export function AskSheet({ shop, requests, close, done }: {
  shop: Shop;
  requests: SpendRequest[];
  close: () => void;
  done: (made: SpendRequest) => Promise<void>;
}) {
  const [products, setProducts] = useState<Product[]>();
  const [hint, setHint] = useState<{ item: string; days: number | null } | null>(null);
  const [f, setF] = useState<Form>({ kind: "stock", item: "", quantity: "1", unit: "", amount: "", payee: "" });
  const touched = useRef(new Set<keyof Form>());
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const open = useMemo(
    () => new Set(requests.filter((r) => !SETTLED.includes(r.status)).map((r) => r.item.trim().toLowerCase())),
    [requests],
  );
  const openRef = useRef(open);
  openRef.current = open;

  useEffect(() => {
    let live = true;
    Promise.all([phone.products(shop.id), phone.alerts(shop.id).catch(() => [])]).then(([ps, alerts]) => {
      if (!live) return;
      setProducts(ps);
      // The newest low-stock suggestion, else the product running out first; items already asked for wait.
      const asked = (item: string) => openRef.current.has(item.trim().toLowerCase());
      const alert = alerts.find((a) => a.kind === "low_stock" && !a.request_id && (a.data as Suggested).item && !asked((a.data as Suggested).item));
      const low = [...ps].filter((p) => p.low && !asked(p.name)).sort((a, b) => (a.days_left ?? 99) - (b.days_left ?? 99))[0];
      const s = alert ? (alert.data as Suggested) : low?.suggested;
      if (s) {
        setF(fromSuggestion(s));
        setHint({ item: s.item, days: (alert?.data.days_left as number | undefined) ?? low?.days_left ?? null });
      }
    }).catch((e) => {
      if (live) setError(String(e.message || e));
    });
    return () => {
      live = false;
    };
  }, [shop.id]);

  const product = (name: string) => products?.find((p) => p.name.toLowerCase() === name.trim().toLowerCase());
  const payees = useMemo(() => [...new Set((products || []).map((p) => p.usual_payee).filter(Boolean))], [products]);

  const set = (k: keyof Form) => (e: ChangeEvent<HTMLInputElement>) => {
    touched.current.add(k);
    const v = e.target.value;
    setF((old) => {
      const next = { ...old, [k]: v };
      const p = next.kind === "stock" ? product(next.item) : undefined;
      if (p && k === "item") {
        next.unit = p.unit;
        if (!touched.current.has("payee")) next.payee = p.usual_payee;
      }
      const q = parseInt(next.quantity, 10);
      if (p && (k === "item" || k === "quantity") && !touched.current.has("amount") && q > 0) {
        next.amount = estimate(p, q);
      }
      return next;
    });
  };

  const pick = (p: Product) => {
    touched.current.clear();
    setF(fromSuggestion(p.suggested));
  };

  const kind = (k: Kind) => {
    touched.current.clear();
    if (k === "stock") {
      const s = hint && product(hint.item);
      setF(s ? fromSuggestion(s.suggested) : { kind: "stock", item: "", quantity: "1", unit: "", amount: "", payee: "" });
    } else {
      setF({ kind: k, item: "", quantity: "1", unit: "units", amount: "", payee: "" });
    }
  };

  const quantity = parseInt(f.quantity, 10);
  const cents = toCents(f.amount);
  const ready = !!f.item.trim() && quantity > 0 && !!cents && cents > 0;
  const suggested = f.kind === "stock" && hint && f.item.trim().toLowerCase() === hint.item.toLowerCase();
  const before = f.item.trim()
    ? requests.find((r) => !SETTLED.includes(r.status) && r.item.trim().toLowerCase() === f.item.trim().toLowerCase())
    : undefined;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!ready || sending) return;
    setSending(true);
    setError("");
    try {
      const made = await api.ask(shop.id, {
        kind: f.kind, item: f.item.trim(), quantity, unit: f.unit.trim() || "units",
        amount: decimal(cents!), payee: f.payee.trim(), words: "",
      });
      await done(made);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setSending(false);
    }
  };

  const footer = products && (
    <>
      {error && <p className="ph-error" role="alert">{error}</p>}
      <button type="submit" form="ph-ask" className="ph-primary" disabled={!ready || sending}>
        {sending ? "Sending" : ready ? `Send to ${shop.owner.name} · ${usd(cents!)}` : `Send to ${shop.owner.name}`}
      </button>
      <p className="ph-fine">PayPal holds the money once {shop.owner.name} approves. Proof releases it.</p>
    </>
  );

  return (
    <Sheet title="Ask for money" sub={`${shop.owner.name} approves it from ${shop.owner.city}.`} close={close} footer={footer}>
      {!products ? (error ? <p className="ph-error" role="alert">{error}</p> : <Loading />) : (
        <form className="ph-form" onSubmit={submit} id="ph-ask">
          <Segmented label="Kind" value={f.kind} options={KINDS} onChange={kind} />
          {before ? (
            <p className="ph-why ph-why-warn">
              You asked for {describe(before)} already, {usd(before.amount_cents)}. {statusOf(before.status, shop.owner.name).label}.
            </p>
          ) : suggested && hint && (
            <p className="ph-why">
              {hint.days != null ? `${hint.days} days of ${hint.item} left. ` : ""}Musha filled in a week of stock at the usual price.
            </p>
          )}
          <label className="ph-l wide">
            <span>{f.kind === "stock" ? "Item" : "What it is"}</span>
            <input value={f.item} onChange={set("item")} list={f.kind === "stock" ? "ph-products" : undefined} autoComplete="off" required />
          </label>
          {f.kind === "stock" && (
            <div className="ph-picks wide" role="group" aria-label="The shop's goods">
              {products.map((p) => {
                const asked = open.has(p.name.trim().toLowerCase());
                return (
                  <button key={p.id} type="button" aria-pressed={f.item.trim().toLowerCase() === p.name.toLowerCase()}
                    className={asked ? "asked" : p.low ? "low" : undefined} onClick={() => pick(p)}>
                    {p.name}
                    {asked ? <small>asked</small> : p.days_left != null && <small>{p.days_left} days</small>}
                  </button>
                );
              })}
            </div>
          )}
          <datalist id="ph-products">{products.map((p) => <option key={p.id} value={p.name} />)}</datalist>
          <label className="ph-l">
            <span>Quantity</span>
            <input value={f.quantity} onChange={set("quantity")} inputMode="numeric" pattern="[0-9]*" required />
          </label>
          <label className="ph-l">
            <span>Unit</span>
            <input value={f.unit} onChange={set("unit")} />
          </label>
          <label className="ph-l wide ph-money">
            <span>Amount</span>
            <div><em>US$</em><input value={f.amount} onChange={set("amount")} inputMode="decimal" required /></div>
          </label>
          <label className="ph-l wide">
            <span>Pay to</span>
            <input value={f.payee} onChange={set("payee")} list="ph-payees" autoComplete="off" />
          </label>
          <datalist id="ph-payees">{payees.map((p) => <option key={p} value={p} />)}</datalist>
        </form>
      )}
    </Sheet>
  );
}

// Send proof

export function ProofSheet({ req, ai, start, close, onFile, onSample, onGoods }: {
  req: SpendRequest;
  ai: boolean;
  /** A receipt chosen already, from the attach menu: the sheet opens on its numbers. */
  start?: File;
  close: () => void;
  onFile: (kind: "receipt" | "goods", file: File, printed?: Partial<Printed>) => void;
  onSample: (variant: Variant) => void;
  onGoods: () => void;
}) {
  const [kind, setKind] = useState<"receipt" | "goods">("receipt");
  const [file, setFile] = useState<File | undefined>(start);
  const [url, setUrl] = useState("");
  const [typed, setTyped] = useState<Partial<Printed>>({});
  const input = useRef<HTMLInputElement>(null);
  const question = [...req.proofs].reverse().find((p) => p.question)?.question;
  const held = req.hold?.amount_cents ?? req.amount_cents;
  const total = toCents(typed.total || "") || 0;

  useEffect(() => {
    if (!file) return;
    const u = URL.createObjectURL(file);
    setUrl(u);
    return () => {
      URL.revokeObjectURL(u);
    };
  }, [file]);

  const picked = (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    if (kind === "receipt" && typesNumbers(req, ai)) {
      setFile(f); // Musha checks what the manager types: the model is off, or it asked for the numbers
    } else {
      onFile(kind, f);
      close();
    }
  };

  return (
    <Sheet title={`Proof for ${describe(req)}`} sub={`${usd(held)} held. Musha checks the receipt and releases the money.`} close={close}>
      {question && req.status === "asking" && (
        <p className="ph-asked"><strong>Musha asked</strong>{question}</p>
      )}
      {file ? (
        <form className="ph-form" onSubmit={(e) => {
          e.preventDefault();
          if (!total) return;
          onFile("receipt", file, { ...typed, total: decimal(total) });
          close();
        }}>
          {url && file.type.startsWith("image/") && <img className="ph-preview wide" src={url} alt="The receipt you chose" />}
          <p className="ph-fine wide">Type what the receipt says. Musha checks these numbers.</p>
          <label className="ph-l wide ph-money">
            <span>Total</span>
            <div><em>US$</em><input value={typed.total || ""} inputMode="decimal" onChange={(e) => setTyped({ ...typed, total: e.target.value })} /></div>
          </label>
          <label className="ph-l">
            <span>Receipt number</span>
            <input value={typed.receipt_no || ""} onChange={(e) => setTyped({ ...typed, receipt_no: e.target.value })} />
          </label>
          <label className="ph-l">
            <span>Date</span>
            <input type="date" value={typed.date || ""} onChange={(e) => setTyped({ ...typed, date: e.target.value })} />
          </label>
          <button type="submit" className="ph-primary wide" disabled={!total}>
            {total ? `Send the receipt · ${usd(total)}` : "Type the total to send"}
          </button>
          <button type="button" className="ph-ghost wide" onClick={() => setFile(undefined)}>Choose another</button>
        </form>
      ) : (
        <div className="ph-proof">
          <Segmented label="Proof" value={kind} onChange={setKind}
            options={[{ key: "receipt", label: "Receipt" }, { key: "goods", label: "Goods photo" }]} />
          <button type="button" className="ph-take" onClick={() => input.current?.click()}>
            <Camera size={26} />
            <strong>{kind === "receipt" ? "Photograph the receipt" : `Photograph the ${req.item}`}</strong>
            <small>{kind === "receipt" && typesNumbers(req, ai) ? "Then type its total, number and date" : "Camera or files"}</small>
          </button>
          <input ref={input} type="file" hidden accept={kind === "receipt" ? "image/*,application/pdf" : "image/*"} onChange={picked} />
          <div className="ph-samples">
            <span className="ph-samples-head">Samples</span>
            {kind === "receipt" ? VARIANTS.map((v) => (
              <button key={v.key} type="button" onClick={() => {
                onSample(v.key);
                close();
              }}>
                <Receipt size={18} />
                <span><strong>{v.label}</strong><small>{v.hint}</small></span>
              </button>
            )) : (
              <button type="button" onClick={() => {
                onGoods();
                close();
              }}>
                <Box size={18} />
                <span><strong>Sample photo</strong><small>The {req.item} on the counter</small></span>
              </button>
            )}
          </div>
        </div>
      )}
    </Sheet>
  );
}

// Close the day

type Row = { p: Product; sold: string; shelf: string; shelfSet: boolean };

export function CloseSheet({ shop, close, done }: { shop: Shop; close: () => void; done: () => Promise<void> }) {
  const [rows, setRows] = useState<Row[]>();
  const [week, setWeek] = useState<Week>();
  const [counted, setCounted] = useState("");
  const [countedSet, setCountedSet] = useState(false);
  const [banked, setBanked] = useState("0.00");
  const [spent, setSpent] = useState("0.00");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<Closed>();

  useEffect(() => {
    let live = true;
    Promise.all([phone.products(shop.id), phone.week(shop.id)]).then(([ps, w]) => {
      if (!live) return;
      setWeek(w);
      setRows(ps.map((p) => {
        const sold = Math.min(Math.round(p.avg_daily), Math.max(0, p.on_hand));
        return { p, sold: String(sold), shelf: String(Math.max(0, p.on_hand - sold)), shelfSet: false };
      }));
    }).catch((e) => {
      if (live) setError(String(e.message || e));
    });
    return () => {
      live = false;
    };
  }, [shop.id]);

  const last = week?.cash[week.cash.length - 1];
  const closedToday = !!last && last.day === todayUtc() && last.closed;
  // The till opens today on yesterday's count, or on what the books expected when nobody counted.
  const opening = last ? last.counted_cents ?? last.expected_close_cents : 0;
  const sales = (rows || []).reduce((n, r) => n + (parseInt(r.sold, 10) || 0) * r.p.sell_cents, 0);
  const expected = opening + sales - (toCents(spent) || 0) - (toCents(banked) || 0);
  const shownCounted = countedSet ? counted : decimal(Math.max(0, expected));

  const setRow = (i: number, k: "sold" | "shelf") => (e: ChangeEvent<HTMLInputElement>) => {
    const v = e.target.value.replace(/[^0-9]/g, "");
    setRows((rs) => rs!.map((r, j) => {
      if (j !== i) return r;
      if (k === "shelf") return { ...r, shelf: v, shelfSet: true };
      const sold = parseInt(v, 10) || 0;
      return { ...r, sold: v, shelf: r.shelfSet ? r.shelf : String(Math.max(0, r.p.on_hand - sold)) };
    }));
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!rows || sending) return;
    const c = toCents(shownCounted);
    if (c == null) {
      setError(`Type the cash counted, for example ${decimal(Math.max(0, expected))}.`);
      return;
    }
    setSending(true);
    setError("");
    try {
      const out = await phone.closeDay(shop.id, {
        units_sold: Object.fromEntries(rows.map((r) => [r.p.name, parseInt(r.sold, 10) || 0])),
        stock_counts: Object.fromEntries(rows.map((r) => [r.p.name, parseInt(r.shelf, 10) || 0])),
        counted_cash: decimal(c),
        banked: decimal(toCents(banked) || 0),
        cash_expenses: decimal(toCents(spent) || 0),
      });
      setResult(out);
      await done();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
    setSending(false);
  };

  if (result) return <ClosedView shop={shop} result={result} close={close} />;
  const ready = !!rows && !!week && !closedToday;
  return (
    <Sheet title="Close the day" sub="Musha filled in what the books expect. Change what the count says." close={close}
      footer={ready && (
        <>
          {error && <p className="ph-error" role="alert">{error}</p>}
          <button type="submit" form="ph-close" className="ph-primary" disabled={sending}>{sending ? "Closing" : `Close the day at ${usd(toCents(shownCounted) ?? 0)}`}</button>
        </>
      )}>
      {!rows || !week ? (error ? <p className="ph-error" role="alert">{error}</p> : <Loading />) : closedToday ? (
        <p className="ph-why">Today is closed. The till counted {usd(last!.counted_cents ?? 0)}.</p>
      ) : (
        <form className="ph-close" onSubmit={submit} id="ph-close">
          <div className="ph-table" role="table" aria-label="Units sold and shelf counts">
            <div className="ph-tr ph-th" role="row">
              <span role="columnheader">Item</span>
              <span role="columnheader">Sold</span>
              <span role="columnheader">Shelf</span>
            </div>
            {rows.map((r, i) => (
              <div className="ph-tr" role="row" key={r.p.id}>
                <span role="cell" className="ph-item">{r.p.name}<small>{r.p.unit}</small></span>
                <input role="cell" aria-label={`${r.p.name} sold`} value={r.sold} inputMode="numeric" onChange={setRow(i, "sold")} />
                <input role="cell" aria-label={`${r.p.name} on the shelf`} value={r.shelf} inputMode="numeric" onChange={setRow(i, "shelf")} />
              </div>
            ))}
          </div>
          <div className="ph-cash">
            <label className="ph-l ph-money">
              <span>Banked</span>
              <div><em>US$</em><input value={banked} inputMode="decimal" onChange={(e) => setBanked(e.target.value)} /></div>
            </label>
            <label className="ph-l ph-money">
              <span>Paid from the till</span>
              <div><em>US$</em><input value={spent} inputMode="decimal" onChange={(e) => setSpent(e.target.value)} /></div>
            </label>
          </div>
          <div className="ph-expect">
            <span>The books expect in the till</span>
            <strong>{usd(expected)}</strong>
            <small>{usd(opening)} at opening, {usd(sales)} in sales</small>
          </div>
          <label className="ph-l ph-money ph-count">
            <span>Cash counted</span>
            <div><em>US$</em><input value={shownCounted} inputMode="decimal" onChange={(e) => {
              setCountedSet(true);
              setCounted(e.target.value);
            }} /></div>
          </label>
        </form>
      )}
    </Sheet>
  );
}

function ClosedView({ shop, result, close }: { shop: Shop; result: Closed; close: () => void }) {
  const { cash, stock } = result;
  const gap = cash.gap_cents ?? 0;
  const even = Math.abs(gap) < 100;
  const gaps = stock.filter((s) => s.gap);
  return (
    <Sheet title="Day closed" close={close} footer={<button type="button" className="ph-primary" onClick={close}>Done</button>}>
      <div className="ph-result">
        <span className="ph-result-label">Counted</span>
        <strong className="ph-result-big">{usd(cash.counted_cents ?? 0)}</strong>
        <span className={`ph-result-line ${even ? "good" : "bad"}`}>
          {even ? "The till matches the books." : `${usd(Math.abs(gap))} ${gap < 0 ? "short" : "over"}. The books expected ${usd(cash.expected_close_cents)}.`}
        </span>
        {gaps.length > 0 && (
          <ul className="ph-gaps">
            {gaps.map((s) => (
              <li key={s.item}>{cap(s.item)}: {qty(Math.abs(s.gap!), s.unit)} {s.gap! < 0 ? "short" : "over"} on the shelf.</li>
            ))}
          </ul>
        )}
        {(!even || gaps.length > 0) && <p className="ph-fine">Musha shows this to {shop.owner.name}.</p>}
      </div>
    </Sheet>
  );
}
