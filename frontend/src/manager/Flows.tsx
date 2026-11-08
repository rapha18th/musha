import { useEffect, useMemo, useRef, useState, type ChangeEvent, type FormEvent } from "react";
import { api, usd, type Shop, type SpendRequest } from "../api";
import { decimal, phone, toCents, VARIANTS, type Closed, type Kind, type Printed, type Product, type Suggested, type Variant, type Week } from "../phone/api";
import { Box, Camera, Receipt } from "../phone/icons";
import { cap, describe, statusOf, todayUtc, typesNumbers } from "../phone/util";
import { qty } from "../words";
import { Dialog, Loading, Segmented } from "./Dialog";
import { daysLeft, SETTLED } from "./words";

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

/**
 * Ask the owner for money. Musha fills in the newest low-stock suggestion, or the product named in `item`
 * when the dashboard opened the dialog for it. The owner approves with PayPal, and the money is held.
 */
export function AskDialog({ shop, requests, item, close, done }: {
  shop: Shop;
  requests: SpendRequest[];
  /** A product to fill in, from Today's low-stock row. */
  item?: string;
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
  const wanted = useRef(item);

  useEffect(() => {
    let live = true;
    Promise.all([phone.products(shop.id), phone.alerts(shop.id).catch(() => [])]).then(([ps, alerts]) => {
      if (!live) return;
      setProducts(ps);
      // The product asked for, else the newest low-stock suggestion, else the product running out first;
      // items already asked for wait.
      const asked = (name: string) => openRef.current.has(name.trim().toLowerCase());
      const named = wanted.current ? ps.find((p) => p.name.toLowerCase() === wanted.current!.trim().toLowerCase()) : undefined;
      const alert = alerts.find((a) => a.kind === "low_stock" && !a.request_id && (a.data as Suggested).item && !asked((a.data as Suggested).item));
      const low = [...ps].filter((p) => p.low && !asked(p.name)).sort((a, b) => (a.days_left ?? 99) - (b.days_left ?? 99))[0];
      const s = named ? named.suggested : alert ? (alert.data as Suggested) : low?.suggested;
      if (s) {
        setF(fromSuggestion(s));
        const days = named ? named.days_left : (alert?.data.days_left as number | undefined) ?? low?.days_left ?? null;
        setHint({ item: s.item, days });
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
      {error && <p className="o-error" role="alert">{error}</p>}
      <button type="submit" form="m-ask" className="o-btn o-primary o-large m-wide" disabled={!ready || sending}>
        {sending ? <><span className="o-spin" aria-hidden="true" /> Sending</> : ready ? `Send to ${shop.owner.name} · ${usd(cents!)}` : `Send to ${shop.owner.name}`}
      </button>
      <p className="o-fine">PayPal holds the money once {shop.owner.name} approves. Proof releases it.</p>
    </>
  );

  return (
    <Dialog title="Ask for money" sub={`${shop.owner.name} approves it${shop.owner.city ? ` from ${shop.owner.city}` : ""}.`} close={close} footer={footer}>
      {!products ? (error ? <p className="o-error" role="alert">{error}</p> : <Loading />) : (
        <form className="m-form" onSubmit={submit} id="m-ask">
          <Segmented label="Kind" value={f.kind} options={KINDS} onChange={kind} />
          {before ? (
            <p className="m-why m-why-warn">
              {before.origin === "manager" ? "You" : before.origin === "musha" ? "Musha" : shop.owner.name} asked for {describe(before)} already, {usd(before.amount_cents)}. {statusOf(before.status, shop.owner.name).label}.
            </p>
          ) : suggested && hint && (
            <p className="m-why">
              {hint.days != null ? `${cap(daysLeft(hint.days))} of ${hint.item} left. ` : ""}Musha filled in a week of stock at the usual price.
            </p>
          )}
          <label className="m-field m-full">
            <span>{f.kind === "stock" ? "Item" : "What it is"}</span>
            <input value={f.item} onChange={set("item")} list={f.kind === "stock" ? "m-products" : undefined} autoComplete="off" required />
          </label>
          {f.kind === "stock" && (
            <div className="m-picks m-full" role="group" aria-label="The shop's goods">
              {products.map((p) => {
                const asked = open.has(p.name.trim().toLowerCase());
                return (
                  <button key={p.id} type="button" aria-pressed={f.item.trim().toLowerCase() === p.name.toLowerCase()}
                    className={asked ? "m-asked" : p.low ? "m-low" : undefined} onClick={() => pick(p)}>
                    {p.name}
                    {asked ? <small>asked</small> : p.days_left != null && <small>{daysLeft(p.days_left)}</small>}
                  </button>
                );
              })}
            </div>
          )}
          <datalist id="m-products">{products.map((p) => <option key={p.id} value={p.name} />)}</datalist>
          <label className="m-field">
            <span>Quantity</span>
            <input value={f.quantity} onChange={set("quantity")} inputMode="numeric" pattern="[0-9]*" required />
          </label>
          <label className="m-field">
            <span>Unit</span>
            <input value={f.unit} onChange={set("unit")} />
          </label>
          <label className="m-field m-full m-money">
            <span>Amount</span>
            <span className="m-money-box"><em>US$</em><input value={f.amount} onChange={set("amount")} inputMode="decimal" required /></span>
          </label>
          <label className="m-field m-full">
            <span>Pay to</span>
            <input value={f.payee} onChange={set("payee")} list="m-payees" autoComplete="off" />
          </label>
          <datalist id="m-payees">{payees.map((p) => <option key={p} value={p} />)}</datalist>
        </form>
      )}
    </Dialog>
  );
}

// Send proof

/** The receipt or a photo of the goods for a held request. A sample receipt tests Musha's checks. */
export function ProofDialog({ req, ai, start, close, onFile, onSample, onGoods }: {
  req: SpendRequest;
  ai: boolean;
  /** A receipt chosen already, from the clip: the dialog opens on its numbers. */
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

  const footer = file ? (
    <>
      <button type="submit" form="m-proof" className="o-btn o-primary o-large m-wide" disabled={!total}>
        {total ? `Send the receipt · ${usd(total)}` : "Type the total to send"}
      </button>
      <button type="button" className="o-btn o-ghost m-wide" onClick={() => setFile(undefined)}>Choose another</button>
    </>
  ) : undefined;

  return (
    <Dialog title={`Proof for ${describe(req)}`} sub={`${usd(held)} held. Musha checks the receipt and releases the money.`} close={close} footer={footer}>
      {question && req.status === "asking" && (
        <figure className="o-ready m-asked-q">
          <figcaption>Musha asked</figcaption>
          <blockquote>{question}</blockquote>
        </figure>
      )}
      {file ? (
        <form className="m-form" id="m-proof" onSubmit={(e) => {
          e.preventDefault();
          if (!total) return;
          onFile("receipt", file, { ...typed, total: decimal(total) });
          close();
        }}>
          {url && file.type.startsWith("image/") && <img className="m-preview m-full" src={url} alt="The receipt you chose" />}
          <p className="o-fine m-full">Type what the receipt says. Musha checks these numbers.</p>
          <label className="m-field m-full m-money">
            <span>Total</span>
            <span className="m-money-box"><em>US$</em><input value={typed.total || ""} inputMode="decimal" onChange={(e) => setTyped({ ...typed, total: e.target.value })} /></span>
          </label>
          <label className="m-field">
            <span>Receipt number</span>
            <input value={typed.receipt_no || ""} onChange={(e) => setTyped({ ...typed, receipt_no: e.target.value })} />
          </label>
          <label className="m-field">
            <span>Date</span>
            <input type="date" value={typed.date || ""} onChange={(e) => setTyped({ ...typed, date: e.target.value })} />
          </label>
        </form>
      ) : (
        <div className="m-proof">
          <Segmented label="Proof" value={kind} onChange={setKind}
            options={[{ key: "receipt", label: "Receipt" }, { key: "goods", label: "Goods photo" }]} />
          <button type="button" className="m-take" onClick={() => input.current?.click()}>
            <Camera size={26} />
            <strong>{kind === "receipt" ? "Add the receipt photo" : `Add a photo of the ${req.item}`}</strong>
            <small>{kind === "receipt" && typesNumbers(req, ai) ? "Then type its total, number and date" : "From the camera roll or your files"}</small>
          </button>
          <input ref={input} type="file" hidden accept={kind === "receipt" ? "image/*,application/pdf" : "image/*"} onChange={picked} />
          <div className="m-samples">
            <p className="m-samples-head">Samples</p>
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
    </Dialog>
  );
}

// Close the day

type Row = { p: Product; sold: string; shelf: string; shelfSet: boolean };

/** Units sold and shelf counts per product, the cash banked and paid out, and the till counted. */
export function CloseDialog({ shop, close, done }: { shop: Shop; close: () => void; done: () => Promise<void> }) {
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
    <Dialog title="Close the day" sub="Musha filled in what the books expect. Change what the count says." close={close}
      footer={ready && (
        <>
          {error && <p className="o-error" role="alert">{error}</p>}
          <button type="submit" form="m-close" className="o-btn o-primary o-large m-wide" disabled={sending}>
            {sending ? <><span className="o-spin" aria-hidden="true" /> Closing</> : `Close the day at ${usd(toCents(shownCounted) ?? 0)}`}
          </button>
        </>
      )}>
      {!rows || !week ? (error ? <p className="o-error" role="alert">{error}</p> : <Loading />) : closedToday ? (
        <p className="m-why">Today is closed. The till counted {usd(last!.counted_cents ?? 0)}.</p>
      ) : (
        <form className="m-close" onSubmit={submit} id="m-close">
          <div className="m-table" role="table" aria-label="Units sold and shelf counts">
            <div className="m-tr m-th" role="row">
              <span role="columnheader">Item</span>
              <span role="columnheader">Sold</span>
              <span role="columnheader">Shelf</span>
            </div>
            {rows.map((r, i) => (
              <div className="m-tr" role="row" key={r.p.id}>
                <span role="cell" className="m-item">{cap(r.p.name)}<small>{r.p.unit}</small></span>
                <span role="cell"><input aria-label={`${r.p.name} sold`} value={r.sold} inputMode="numeric" onChange={setRow(i, "sold")} /></span>
                <span role="cell"><input aria-label={`${r.p.name} on the shelf`} value={r.shelf} inputMode="numeric" onChange={setRow(i, "shelf")} /></span>
              </div>
            ))}
          </div>
          <div className="m-cash">
            <label className="m-field m-money">
              <span>Banked</span>
              <span className="m-money-box"><em>US$</em><input value={banked} inputMode="decimal" onChange={(e) => setBanked(e.target.value)} /></span>
            </label>
            <label className="m-field m-money">
              <span>Paid from the till</span>
              <span className="m-money-box"><em>US$</em><input value={spent} inputMode="decimal" onChange={(e) => setSpent(e.target.value)} /></span>
            </label>
          </div>
          <div className="m-expect">
            <span>The books expect in the till</span>
            <strong>{usd(expected)}</strong>
            <small>{usd(opening)} at opening, {usd(sales)} in sales</small>
          </div>
          <label className="m-field m-money m-count">
            <span>Cash counted</span>
            <span className="m-money-box"><em>US$</em><input value={shownCounted} inputMode="decimal" onChange={(e) => {
              setCountedSet(true);
              setCounted(e.target.value);
            }} /></span>
          </label>
        </form>
      )}
    </Dialog>
  );
}

function ClosedView({ shop, result, close }: { shop: Shop; result: Closed; close: () => void }) {
  const { cash, stock } = result;
  const gap = cash.gap_cents ?? 0;
  const even = Math.abs(gap) < 100;
  const gaps = stock.filter((s) => s.gap);
  return (
    <Dialog title="Day closed" close={close} footer={<button type="button" className="o-btn o-primary o-large m-wide" onClick={close}>Done</button>}>
      <div className="m-result">
        <span className="o-eyebrow">Counted</span>
        <strong className="o-big">{usd(cash.counted_cents ?? 0)}</strong>
        <span className={`m-result-line ${even ? "o-leaf" : "o-danger"}`}>
          {even ? "The till matches the books." : `${usd(Math.abs(gap))} ${gap < 0 ? "short" : "over"}. The books expected ${usd(cash.expected_close_cents)}.`}
        </span>
        {gaps.length > 0 && (
          <ul className="m-gaps">
            {gaps.map((s) => (
              <li key={s.item}>{cap(s.item)}: {qty(Math.abs(s.gap!), s.unit)} {s.gap! < 0 ? "short" : "over"} on the shelf.</li>
            ))}
          </ul>
        )}
        {(!even || gaps.length > 0) && <p className="o-fine">Musha shows this to {shop.owner.name}.</p>}
      </div>
    </Dialog>
  );
}
