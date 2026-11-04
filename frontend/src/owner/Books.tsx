import { useCallback, useMemo, useRef, useState } from "react";
import { AgGridReact } from "ag-grid-react";
import type { ColDef, ICellRendererParams } from "ag-grid-community";
import { usd, type Entry, type Shop } from "../api";
import { ownerApi, type OwnerRequest, type Reconcile } from "./api";
import { Lightbox, evidenceOf } from "./Evidence";
import { ErrorLine, Spinner, Tile } from "./parts";
import { cap, clock, day, useAction, useCompact, useDocTheme, when } from "./lib";
import { gridTheme } from "./grid";

export const LINE: Record<string, string> = {
  funds_held: "Held at PayPal",
  funds_captured: "Released to the shop",
  funds_released: "Returned to you",
  stock_in: "Stock in",
  sale: "Sale",
  wages: "Wages",
  expense: "Expense",
  asset: "Asset",
  banked: "Banked",
  cash_float: "Till float",
};

const TONE: Record<string, string> = {
  funds_held: "warn", funds_captured: "leaf", funds_released: "clay", wages: "leaf",
};

const SOURCE: Record<string, string> = {
  api: "Musha", webhook: "PayPal webhook", job: "Nightly job", seed: "Opening books", manager: "Day close",
};

type Row = Entry & { line: string; posted: string; files: number };

/** "2026-10-31 13:45" in the viewer's time, which spreadsheets read as a date. */
function stamp(iso: string): string {
  const d = new Date(iso);
  const two = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}`;
}

export function Books({ shop, entries, requests }: { shop: Shop; entries: Entry[]; requests: OwnerRequest[] }) {
  const theme = useDocTheme();
  // The wide column set needs 780 px for its minimum widths; below that the two-line set fits.
  const [box, narrow] = useCompact<HTMLDivElement>(780);
  const grid = useRef<AgGridReact<Row>>(null);
  const [quick, setQuick] = useState("");
  const [kind, setKind] = useState("all");
  const [proof, setProof] = useState<{ req: OwnerRequest; index: number } | null>(null);
  const b = shop.books;

  const byId = useMemo(() => new Map(requests.map((r) => [r.id, r])), [requests]);
  const rows = useMemo<Row[]>(() => [...entries].reverse().map((e) => ({
    ...e,
    line: LINE[e.kind] ?? cap(e.kind.replace(/_/g, " ")),
    posted: SOURCE[e.source] ?? cap(e.source),
    files: e.request_id ? (byId.get(e.request_id)?.attachments.length ?? 0) : 0,
  })), [entries, byId]);
  const kinds = useMemo(() => Array.from(new Set(entries.map((e) => e.kind))), [entries]);
  const shown = useMemo(() => (kind === "all" ? rows : rows.filter((r) => r.kind === kind)), [rows, kind]);

  const openProof = useCallback((id: number | null) => {
    const req = id ? byId.get(id) : undefined;
    if (req && req.attachments.length) setProof({ req, index: 0 });
  }, [byId]);

  const cols = useMemo<ColDef<Row>[]>(() => [
    {
      field: "at", headerName: "When", minWidth: narrow ? 80 : 116, maxWidth: narrow ? 80 : undefined, flex: narrow ? undefined : 0.9, sort: "desc",
      valueFormatter: (p) => `${day(p.value)}, ${clock(p.value)}`,
      cellRenderer: narrow ? (p: ICellRendererParams<Row>) => (
        <span className="o-two"><span>{day(p.value)}</span><small>{clock(p.value)}</small></span>
      ) : undefined,
      getQuickFilterText: (p) => day(p.value),
    },
    {
      field: "line", headerName: "Line", minWidth: narrow ? 120 : 180, flex: narrow ? 2 : 1.4,
      cellRenderer: (p: ICellRendererParams<Row>) => (
        narrow ? (
          <span className={`o-line o-two o-tone-${TONE[p.data?.kind ?? ""] ?? "calm"}`}>
            <span><i aria-hidden="true" />{p.value}</span>
            <small>{p.data?.memo}</small>
          </span>
        ) : (
          <span className={`o-line o-tone-${TONE[p.data?.kind ?? ""] ?? "calm"}`} title={p.value}>
            <i aria-hidden="true" /><span className="o-line-text">{p.value}</span>
          </span>
        )
      ),
      getQuickFilterText: (p) => `${p.value} ${p.data?.memo ?? ""}`,
    },
    { field: "memo", headerName: "Detail", minWidth: 150, flex: 1.6, tooltipField: "memo", hide: narrow },
    { field: "quantity", headerName: "Qty", type: "rightAligned", minWidth: 52, flex: 0.3, hide: narrow, valueFormatter: (p) => (p.value ? String(p.value) : "") },
    {
      field: "amount_cents", headerName: narrow ? shop.currency.replace("USD", "US$") : "Amount", type: "rightAligned",
      minWidth: narrow ? 76 : 100, maxWidth: narrow ? 76 : undefined, flex: narrow ? undefined : 0.8,
      valueFormatter: (p) => (p.value ? (narrow ? usd(p.value).replace(/^US\$/, "") : usd(p.value)) : ""), cellClass: ["ag-right-aligned-cell", "o-num"],
      getQuickFilterText: (p) => (p.value / 100).toFixed(2),
    },
    { field: "posted", headerName: "Posted by", minWidth: 118, flex: 0.8, hide: narrow },
    {
      field: "files", headerName: "", width: narrow ? 48 : 60, maxWidth: narrow ? 48 : 60, minWidth: narrow ? 48 : 60, sortable: false, resizable: false,
      cellRenderer: (p: ICellRendererParams<Row>) => (p.value ? (
        <button className="o-clip" onClick={() => openProof(p.data?.request_id ?? null)} aria-label={`Open the evidence, ${p.value} files`}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11.5 12.4 19a5 5 0 0 1-7.1-7.1l7.8-7.8a3.4 3.4 0 0 1 4.8 4.8l-7.8 7.8a1.7 1.7 0 0 1-2.4-2.4l7.1-7.1" /></svg>
          <span>{p.value}</span>
        </button>
      ) : null),
    },
  ], [openProof, narrow, shop.currency]);

  const exportCsv = () => {
    grid.current?.api.exportDataAsCsv({
      fileName: `${shop.name.replace(/[^\w ]+/g, "").trim()} books.csv`,
      columnKeys: ["at", "line", "memo", "quantity", "amount_cents", "posted"],
      processCellCallback: (p) => {
        const id = p.column.getColId();
        if (id === "amount_cents") return ((p.value ?? 0) / 100).toFixed(2);
        if (id === "at") return stamp(p.value);
        if (id === "quantity") return p.value ? String(p.value) : "";
        return p.value ?? "";
      },
      processHeaderCallback: (p) => (p.column.getColId() === "amount_cents" ? `Amount (${shop.currency})` : p.column.getColDef().headerName ?? ""),
    });
  };

  return (
    <div className="o-books" ref={box}>
      <div className="o-tiles">
        <Tile label="Held at PayPal" value={usd(b.held_cents)} tone={b.held_cents ? "warn" : undefined} />
        <Tile label="Released to the shop" value={usd(b.funded_cents)} tone="leaf" />
        <Tile label="Returned to you" value={usd(b.returned_cents)} />
        <Tile label="Wages" value={usd(b.wages_cents)} />
      </div>

      <section className="o-section">
        <div className="o-section-head">
          <h2>The ledger</h2>
          <span className="o-muted o-small-text">{entries.length} lines. The paperclip opens the proof.</span>
        </div>
        <div className="o-gridbar">
          <input className="o-search" type="search" value={quick} onChange={(e) => setQuick(e.target.value)}
            placeholder="Search the books" aria-label="Search the books" />
          <select className="o-select" value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Line kind">
            <option value="all">Every line</option>
            {kinds.map((k) => <option key={k} value={k}>{LINE[k] ?? cap(k)}</option>)}
          </select>
          <button className="o-btn o-ghost" onClick={exportCsv}>Export CSV</button>
        </div>
        <div className={`o-grid ag-theme-mode${narrow ? " o-grid-narrow" : ""}`} data-ag-theme-mode={theme}>
          <AgGridReact<Row> ref={grid} theme={gridTheme} rowData={shown} columnDefs={cols} quickFilterText={quick}
            domLayout="autoHeight" pagination paginationPageSize={12} paginationPageSizeSelector={false}
            rowHeight={narrow ? 56 : undefined}
            getRowId={(p) => String(p.data.id)} suppressCellFocus enableCellTextSelection />
        </div>
      </section>

      <ReconcilePanel shop={shop} />

      {proof && (
        <Lightbox items={evidenceOf(proof.req)} index={proof.index}
          setIndex={(i) => setProof({ ...proof, index: i })} onClose={() => setProof(null)}
          req={proof.req} manager={shop.manager.name} />
      )}
    </div>
  );
}

const WHAT: Record<string, string> = {
  held: "Held", captured: "Released", returned: "Returned", wages: "Wages", release: "Payout to the shop",
  reauthorized: "Hold renewed", reversal: "Reversal", refund: "Refund",
};

function ReconcilePanel({ shop }: { shop: Shop }) {
  const [result, setResult] = useState<Reconcile>();
  const [writing, setWriting] = useState(false);
  const { busy, error, run } = useAction();
  // The exact match lands at once; the agent's summary follows, and the rule's summary stands until it does.
  const go = () => run(async () => {
    setResult(await ownerApi.reconcile(shop.id, false));
    setWriting(true);
    ownerApi.reconcile(shop.id).then(setResult, () => undefined).finally(() => setWriting(false));
  });
  const r = result;
  const problems = r ? r.missing_in_books.length + r.missing_in_paypal.length + r.mismatched.length : 0;
  return (
    <section className="o-section">
      <div className="o-section-head">
        <h2>PayPal and the books</h2>
        <span className="o-muted o-small-text">Seven days of PayPal transactions against every PayPal line.</span>
      </div>
      <div className="o-card o-recon">
        {!r && (
          <div className="o-recon-start">
            <p>Musha reads PayPal's own record and matches each hold, release, return and payout to a line in the books.</p>
            <button className="o-btn o-primary" onClick={go} disabled={busy}>
              {busy ? <><Spinner /> Matching</> : "Match PayPal to the books"}
            </button>
          </div>
        )}
        {r && (
          <>
            <div className="o-recon-tiles">
              <div className="o-recon-n o-tone-leaf"><b>{r.matched.length}</b><span>matched</span></div>
              <div className={`o-recon-n${r.missing_in_books.length ? " o-tone-danger" : ""}`}><b>{r.missing_in_books.length}</b><span>at PayPal only</span></div>
              <div className={`o-recon-n${r.missing_in_paypal.length ? " o-tone-danger" : ""}`}><b>{r.missing_in_paypal.length}</b><span>in the books only</span></div>
              <div className={`o-recon-n${r.mismatched.length ? " o-tone-danger" : ""}`}><b>{r.mismatched.length}</b><span>amounts differ</span></div>
            </div>
            <p className="o-musha"><span className="o-musha-mark" aria-hidden="true">M</span>{r.summary}</p>
            {writing && <p className="o-fine"><Spinner /> Musha is reading PayPal's record for its own summary.</p>}
            {problems > 0 && (
              <ul className="o-recon-list">
                {r.missing_in_books.map((m) => (
                  <li key={`b${m.paypal_id}`} className="o-tone-danger">
                    <span>{cap(m.what)} at PayPal, {m.paypal_id}</span><b>{usd(m.amount_cents)}</b>
                  </li>
                ))}
                {r.missing_in_paypal.map((m) => (
                  <li key={`p${m.ref}`} className="o-tone-danger">
                    <span>{WHAT[m.kind] ?? cap(m.kind)} in the books{m.item ? `, ${m.item}` : ""}</span><b>{usd(m.amount_cents)}</b>
                  </li>
                ))}
                {r.mismatched.map((m) => (
                  <li key={`m${m.ref}`} className="o-tone-danger">
                    <span>{WHAT[m.kind] ?? cap(m.kind)}{m.item ? `, ${m.item}` : ""}: {m.reason}</span>
                    <b>{usd(m.book_cents)} / {usd(m.paypal_cents)}</b>
                  </li>
                ))}
              </ul>
            )}
            {r.matched.length > 0 && (
              <details className="o-recon-more">
                <summary>{r.matched.length} matched lines</summary>
                <ul className="o-recon-list">
                  {r.matched.map((m) => (
                    <li key={`ok${m.ref}${m.paypal_id}`}>
                      <span>{WHAT[m.kind] ?? cap(m.kind)}{m.item ? `, ${m.item}` : ""} · {when(m.at)}</span><b>{usd(m.amount_cents)}</b>
                    </li>
                  ))}
                </ul>
              </details>
            )}
            <div className="o-recon-foot">
              <span className="o-muted o-small-text">
                {day(r.window.start)} to {day(r.window.end)} · {r.by === "model" ? "Summary by Musha" : "Summary by rule"}
                {r.agent ? ` · ${r.agent.tools}, ${r.agent.calls.length} calls` : ""}
              </span>
              <button className="o-btn o-ghost o-small" onClick={go} disabled={busy || writing}>{busy ? <Spinner /> : null}Match again</button>
            </div>
          </>
        )}
        <ErrorLine error={error} />
      </div>
    </section>
  );
}

