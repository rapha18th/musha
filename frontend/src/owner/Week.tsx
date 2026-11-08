import { useCallback, useMemo, useState } from "react";
import { AgGridReact } from "ag-grid-react";
import type { ColDef, ICellRendererParams } from "ag-grid-community";
import { usd, type Entry, type Shop } from "../api";
import { ownerApi, type Alert, type CashRow, type Product, type StockRow, type Week as WeekData } from "./api";
import { AskNote, askLabel, askState, freshest, type AskState } from "./Ask";
import { ErrorLine, Spinner, Tile, useArrived } from "./parts";
import { cap, clock, dollars, qtyParts, useAction, useCompact, useDocTheme, usePoll, weekday, when } from "./lib";
import { gridTheme } from "./grid";

type Props = {
  shop: Shop;
  week?: WeekData;
  alerts: Alert[];
  entries: Entry[];
  refresh: () => Promise<void>;
  reloadAlerts: () => Promise<void>;
  reloadWeek: () => Promise<void>;
};

type Asked = { asked_at?: string | null; answer?: string | null; answered_at?: string | null; waiting?: boolean };

/**
 * Ask the manager the ready question: through the alert when one carries it, else as a plain line.
 * Through the alert, the question waits on the answer and the row offers no second ask until it comes.
 * A closed alert takes no question, so the plain line goes to the manager's thread, once from this page.
 * Every time shown is the server's: the alert's asked_at, or the line's own.
 */
function useAskFarai(shop: Shop, alerts: Alert[], reload: () => void) {
  const [sent, setSent] = useState<Record<string, AskState>>({});
  const [lines, setLines] = useState<Record<string, boolean>>({});
  const [just, setJust] = useState(""); // the key this page asked last: its waiting line takes the button's focus
  const [busy, setBusy] = useState<string>("");
  const [error, setError] = useState("");
  const stateOf = useCallback((key: string, row?: Asked): AskState => {
    const alert = alerts.find((a) => a.key === key);
    return freshest(alert && askState(alert.data, alert.waiting), row && askState(row, row.waiting), sent[key]);
  }, [alerts, sent]);
  const canAsk = useCallback((key: string, s: AskState) => !s.waiting && !lines[key], [lines]);
  const ask = useCallback(async (key: string, question: string, row?: Asked) => {
    if (busy || !canAsk(key, stateOf(key, row))) return;
    setBusy(key);
    setError("");
    try {
      const alert = alerts.find((a) => a.key === key);
      if (alert) {
        const r = await ownerApi.act(alert.id, "ask");
        setSent((s) => ({ ...s, [key]: askState(r.alert.data, r.alert.waiting) }));
      } else {
        const m = await ownerApi.toManager(shop.id, question);
        setSent((s) => ({ ...s, [key]: { askedAt: m.at, waiting: false } }));
        setLines((l) => ({ ...l, [key]: true }));
      }
      setJust(key);
      reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }, [alerts, busy, canAsk, reload, shop.id, stateOf]);
  return useMemo(() => ({ ask, stateOf, canAsk, busy, error, just }), [ask, stateOf, canAsk, busy, error, just]);
}

/** "US$23.00 short", "US$5.00 over", "Even": the gap in words, so a shortfall reads as one at a glance. */
const gapWords = (cents: number | null) =>
  (cents == null ? "" : cents === 0 ? "Even" : `${usd(Math.abs(cents))} ${cents < 0 ? "short" : "over"}`);

const cashKey = (shopId: number, day: string) => `cash_gap:${shopId}:${day}`;

export function Week({ shop, week, alerts, entries, refresh, reloadAlerts, reloadWeek }: Props) {
  const theme = useDocTheme();
  const [box, narrow] = useCompact<HTMLDivElement>(720);
  // Below 860 px the row's Ask button moves under the grid with its question, so every figure keeps its room.
  const [box2, tight] = useCompact<HTMLDivElement>(860);
  const frame = useCallback((el: HTMLDivElement | null) => {
    box(el);
    box2(el);
  }, [box, box2]);
  const reload = useCallback(() => {
    void reloadAlerts();
    void reloadWeek();
  }, [reloadAlerts, reloadWeek]);
  const asker = useAskFarai(shop, alerts, reload);
  const manager = shop.manager.name;
  const [products, setProducts] = useState<Product[]>();
  // The shelf reads again whenever the week's figures move.
  usePoll(async () => {
    setProducts(await ownerApi.products(shop.id));
  }, 30000, [shop.id, week]);

  const cols = useMemo<ColDef<CashRow>[]>(() => [
    {
      field: "day", headerName: "Day", minWidth: narrow ? 62 : 96, flex: narrow ? 0.8 : 1.1,
      valueFormatter: (p) => (narrow ? weekday(p.value).split(" ").slice(0, 2).join(" ") : weekday(p.value)),
    },
    { field: "opening_cents", headerName: "Opening", type: "rightAligned", minWidth: 84, flex: 1, hide: narrow, valueFormatter: (p) => usd(p.value) },
    { field: "cash_sales_cents", headerName: "Cash sales", type: "rightAligned", minWidth: 92, flex: 1, hide: narrow, valueFormatter: (p) => usd(p.value) },
    { field: "cash_expenses_cents", headerName: "Spent", type: "rightAligned", minWidth: 70, flex: 1, hide: narrow, valueFormatter: (p) => usd(p.value) },
    { field: "banked_cents", headerName: "Banked", type: "rightAligned", minWidth: 84, flex: 1, hide: narrow, valueFormatter: (p) => usd(p.value) },
    { field: "expected_close_cents", headerName: "Expected", type: "rightAligned", minWidth: narrow ? 80 : 96, flex: 1, valueFormatter: (p) => usd(p.value) },
    { field: "counted_cents", headerName: "Counted", type: "rightAligned", minWidth: narrow ? 80 : 96, flex: 1, valueFormatter: (p) => (p.value == null ? "Open" : usd(p.value)) },
    {
      field: "gap_cents", headerName: "Gap", type: "rightAligned", minWidth: narrow ? 104 : 124, flex: 1.2,
      valueFormatter: (p) => gapWords(p.value),
      cellClassRules: { "o-cell-short": (p) => (p.value ?? 0) < 0, "o-cell-over": (p) => (p.value ?? 0) > 0, "o-cell-even": (p) => p.value === 0 },
    },
    {
      colId: "ask", headerName: "", minWidth: 104, flex: 1.1, sortable: false, hide: tight,
      cellRenderer: (p: ICellRendererParams<CashRow>) => {
        const row = p.data;
        if (!row?.question) return null;
        const key = cashKey(shop.id, row.day);
        const s = asker.stateOf(key, row);
        // Waiting, the cell keeps the time; the line under the grid says whose answer it waits for.
        if (s.waiting && s.askedAt) {
          return <span className="o-cell-note" title={`Waiting for ${manager}'s response`}>Asked {clock(s.askedAt)}</span>;
        }
        if (s.answer) return <span className="o-cell-note">{manager} answered</span>;
        if (!asker.canAsk(key, s)) return <span className="o-cell-note">Asked {clock(s.askedAt)}</span>;
        return (
          <button className="o-btn o-outline o-small" disabled={!!asker.busy} onClick={() => void asker.ask(key, row.question!, row)}>
            {asker.busy === key ? <Spinner /> : null}{askLabel(s, manager)}
          </button>
        );
      },
    },
  ], [shop.id, manager, asker, narrow, tight]);

  if (!week) return <div className="o-loading"><Spinner /> Reading the week</div>;
  const f = week.facts;
  const shortDays = f.cash.gaps.filter((g) => g.gap_cents < 0);
  const stockGaps = f.stock_gaps;
  const questions = week.cash.filter((r) => r.question);

  return (
    <div className="o-week" ref={frame}>
      <p className="o-range">{weekday(week.from)} to {weekday(week.to)} · {f.cash.days_closed} days closed</p>
      <div className="o-tiles">
        <Tile label="Sales" value={usd(f.sales_cents)} />
        <Tile label={`Margin, ${f.margin_pct}%`} value={usd(f.margin_cents)} tone={f.margin_cents >= 0 ? "leaf" : "danger"} />
        <Tile label={shortDays.length ? `Cash gap, ${shortDays.length} ${shortDays.length === 1 ? "day" : "days"}` : "Cash gap"}
          value={f.cash.gap_cents === 0 ? usd(0) : `${f.cash.gap_cents < 0 ? "−" : "+"}${usd(Math.abs(f.cash.gap_cents))}`}
          tone={f.cash.gap_cents < 0 ? "danger" : undefined} />
        <Tile label="Stock gaps" value={String(stockGaps.length)} tone={stockGaps.length ? "danger" : undefined}
          note={stockGaps.length ? stockGaps.map((s) => `${s.item} ${s.gap}`).slice(0, 2).join(", ") : "Shelves match the books"} />
      </div>

      <section className="o-section">
        <div className="o-section-head">
          <h2>Expected cash by day</h2>
          <span className="o-muted o-small-text">Each day opens on the last count.</span>
        </div>
        <div className="o-grid ag-theme-mode" data-ag-theme-mode={theme}>
          <AgGridReact<CashRow> theme={gridTheme} rowData={week.cash} columnDefs={cols} domLayout="autoHeight"
            getRowId={(p) => p.data.day} suppressCellFocus />
        </div>
        {/* An answered gap, or one whose question waits, shows under the grid at every width; an open one, when its Ask button moves here. */}
        {questions.map((row) => ({ row, key: cashKey(shop.id, row.day) }))
          .map(({ row, key }) => ({ row, key, s: asker.stateOf(key, row) }))
          .filter(({ s }) => tight || !!s.answer || s.waiting)
          .map(({ row, key, s }) => (
            <div key={row.day} className="o-mx-detail o-gap-q">
              <p><b>{row.day_name}.</b> Expected {usd(row.expected_close_cents)}, counted {row.counted_cents == null ? "later" : usd(row.counted_cents)}.</p>
              <p className="o-quote">{row.question}</p>
              <AskNote state={s} manager={manager} focus={asker.just === key} />
              {asker.canAsk(key, s) && (
                <div className="o-row">
                  <button className="o-btn o-outline o-small" disabled={!!asker.busy} onClick={() => void asker.ask(key, row.question!, row)}>
                    {asker.busy === key ? <Spinner /> : null}{askLabel(s, manager)}
                  </button>
                </div>
              )}
            </div>
          ))}
        <ErrorLine error={asker.error} />
      </section>

      <section className="o-section">
        <div className="o-section-head">
          <h2>Stock by day and item</h2>
          <span className="o-muted o-small-text">Shelf count against the books.</span>
        </div>
        <StockMatrix rows={week.stock} shop={shop} asker={asker} />
      </section>

      {products && products.length > 0 && <Shelf products={products} />}

      <Wages shop={shop} entries={entries} paidThisWeek={f.wages_cents} refresh={refresh} />
    </div>
  );
}

function StockMatrix({ rows, shop, asker }: { rows: StockRow[]; shop: Shop; asker: ReturnType<typeof useAskFarai> }) {
  const days = Array.from(new Set(rows.map((r) => r.day)));
  const items = Array.from(new Set(rows.map((r) => r.item)));
  const cell = (item: string, d: string) => rows.find((r) => r.item === item && r.day === d);
  const [picked, setPicked] = useState<StockRow | null>(() => rows.find((r) => r.question) ?? null);
  const key = picked ? `stock_gap:${shop.id}:${picked.item}:${picked.day}` : "";
  const fresh = picked ? cell(picked.item, picked.day) : undefined; // the row as the week reads it now
  const s = picked ? asker.stateOf(key, fresh ?? picked) : undefined;
  const manager = shop.manager.name;
  const button = s && picked?.question && asker.canAsk(key, s) && (
    <button className="o-btn o-outline o-small" disabled={!!asker.busy} onClick={() => void asker.ask(key, picked.question!, fresh ?? picked)}>
      {asker.busy === key ? <Spinner /> : null}{askLabel(s, manager)}
    </button>
  );
  return (
    <div className="o-matrix-wrap">
      <div className="o-matrix" style={{ gridTemplateColumns: `minmax(64px, 1.4fr) repeat(${days.length}, minmax(28px, 1fr))` }} role="table">
        <span className="o-mx-head" role="columnheader" />
        {days.map((d) => <span key={d} className="o-mx-head" role="columnheader">{weekday(d).split(" ")[0]}</span>)}
        {items.map((item) => (
          <div key={item} className="o-mx-row" role="row">
            <span className="o-mx-item" role="rowheader">{cap(item)}</span>
            {days.map((d) => {
              const r = cell(item, d);
              const gap = r?.gap;
              const cls = gap == null ? "o-mx-open" : gap < 0 ? "o-mx-short" : gap > 0 ? "o-mx-over" : "o-mx-even";
              return (
                <button key={d} role="cell" className={`o-mx-cell ${cls}${picked && r && picked.day === r.day && picked.item === r.item ? " o-mx-picked" : ""}`}
                  onClick={() => r && setPicked(r)}
                  title={r ? `${r.day_name}: expected ${r.expected}, counted ${r.counted ?? "open"}` : ""}>
                  {gap == null ? "" : gap === 0 ? "" : gap > 0 ? `+${gap}` : `−${Math.abs(gap)}`}
                </button>
              );
            })}
          </div>
        ))}
      </div>
      {picked && (
        <div className="o-mx-detail">
          <p>
            <b>{cap(picked.item)}, {picked.day_name}.</b> Opened on {picked.opening}, {picked.stock_in} in, {picked.sold} sold.
            Expected {picked.expected}, counted {picked.counted ?? "later"}.
          </p>
          {picked.question && s && (
            !s.askedAt && !s.answer ? (
              <div className="o-row">
                <p className="o-quote">{picked.question}</p>
                {button}
              </div>
            ) : (
              <>
                <p className="o-quote">{picked.question}</p>
                <AskNote state={s} manager={manager} focus={asker.just === key} />
                {button && <div className="o-row">{button}</div>}
              </>
            )
          )}
        </div>
      )}
    </div>
  );
}

const COVER = 7;

/** Stock on hand from the last shelf count, and how many days it lasts at the shop's pace. */
function Shelf({ products }: { products: Product[] }) {
  const on = useArrived();
  const rows = [...products].sort((a, b) => (a.days_left ?? 99) - (b.days_left ?? 99));
  return (
    <section className="o-section">
      <div className="o-section-head">
        <h2>On the shelf</h2>
        <span className="o-muted o-small-text">The last shelf count, with what came in and sold since.</span>
      </div>
      <ul className="o-shelf-list">
        {rows.map((p) => {
          const days = p.days_left;
          const pct = days == null ? 0 : Math.min(100, (days / COVER) * 100);
          return (
            <li key={p.id} className={`o-shelf-row o-tone-${p.low ? "warn" : "leaf"}`}>
              <span className="o-shelf-name">{cap(p.name)}</span>
              <span className="o-shelf-qty"><b>{qtyParts(p.on_hand, p.unit)[0]}</b> {qtyParts(p.on_hand, p.unit)[1]}</span>
              <span className="o-shelf-bar" aria-hidden="true"><i style={{ width: on ? `${pct}%` : "0%" }} /></span>
              <span className="o-shelf-days">
                {days == null ? "Quiet for 14 days" :`${days} ${days === 1 ? "day" : "days"}`}
                <small>{p.avg_daily} a day</small>
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function Wages({ shop, entries, paidThisWeek, refresh }: {
  shop: Shop;
  entries: Entry[];
  paidThisWeek: number;
  refresh: () => Promise<void>;
}) {
  const last = [...entries].reverse().find((e) => e.kind === "wages");
  const [amount, setAmount] = useState(last ? dollars(last.amount_cents) : "");
  const [note, setNote] = useState(`Wages for ${shop.manager.name}`);
  const [result, setResult] = useState("");
  const { busy, error, run } = useAction();
  const valid = /^\d+(\.\d{1,2})?$/.test(amount.trim()) && Number(amount) > 0;
  return (
    <section className="o-section">
      <div className="o-section-head">
        <h2>Wages</h2>
        <span className="o-muted o-small-text">You approve them with PayPal. Musha pays {shop.manager.name} through PayPal Payouts.</span>
      </div>
      <div className="o-card o-wages">
        <div className="o-wages-now">
          <span className="o-tile-label">Paid to {shop.manager.name} this week</span>
          <span className="o-amount">{usd(paidThisWeek)}</span>
          {last && <span className="o-muted o-small-text">Last paid {when(last.at)}, {usd(last.amount_cents)}</span>}
        </div>
        <form className="o-wages-form" onSubmit={(e) => {
          e.preventDefault();
          if (!valid) return;
          setResult("");
          run(async () => {
            const r = await ownerApi.wages(shop.id, amount.trim(), note.trim());
            setResult(r.status === "requested" || r.status === "approving"
              ? `Approve ${usd(r.request.amount_cents)} for ${shop.manager.name} with PayPal under Today.`
              : `Wages for ${shop.manager.name}, ${usd(r.request.amount_cents)}: ${r.status.replace(/_/g, " ")}.`);
            await refresh();
          });
        }}>
          <label className="o-field">
            <span>Amount</span>
            <span className="o-money-input"><i>US$</i><input value={amount} inputMode="decimal" onChange={(e) => setAmount(e.target.value)} placeholder="0.00" /></span>
          </label>
          <label className="o-field o-grow">
            <span>Note</span>
            <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={120} />
          </label>
          <button className="o-btn o-primary" type="submit" disabled={busy || !valid}>
            {busy ? <Spinner /> : null}Pay {shop.manager.name}{valid ? ` ${usd(Math.round(Number(amount) * 100))}` : ""}
          </button>
        </form>
        {result && <p className="o-fine o-leaf">{result}</p>}
        <ErrorLine error={error} />
      </div>
    </section>
  );
}
