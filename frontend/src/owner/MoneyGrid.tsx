import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { AgGridReact, type CustomStatusPanelProps } from "ag-grid-react";
import {
  ClientSideRowModelApiModule, ClientSideRowModelModule, ColumnApiModule, DateFilterModule, NumberFilterModule, QuickFilterModule,
  RowApiModule, TooltipModule, ValidationModule,
  type ColDef, type ColumnState, type ExcelCell, type ExcelRow, type ExcelStyle, type FilterModel, type GridApi,
  type ICellRendererParams, type IRowNode, type Module, type RowClickedEvent,
} from "ag-grid-community";
import { ExcelExportModule, MasterDetailModule, RowGroupingModule, SetFilterModule, StatusBarModule } from "ag-grid-enterprise";
import { AgCharts } from "ag-charts-react";
import type { AgCartesianChartOptions, AgChartTheme } from "ag-charts-community";
import { usd, type Shop } from "../api";
import { band } from "./parts";
import { cap, useDocTheme, when } from "./lib";
import { gridTheme } from "./grid";
import {
  EVENT_LABEL, GROUP_LABEL, STATE, dayLabel, weekLabel, type Group, type Measure, type MoneyProof, type MoneyRow,
  type Palette, type PayPalEvent, type Spec,
} from "./moneyData";

/** The Enterprise features this grid uses, for this grid alone: Books and Week keep their community set. */
const MODULES: Module[] = [
  ClientSideRowModelModule, ClientSideRowModelApiModule, ColumnApiModule, RowApiModule, QuickFilterModule, TooltipModule, DateFilterModule,
  NumberFilterModule, SetFilterModule, RowGroupingModule, MasterDetailModule, StatusBarModule, ExcelExportModule,
  ...(import.meta.env.DEV ? [ValidationModule] : []), // readable errors while developing
];

/** What the cell renderers inside the grid read: the chart theme, the house colours and the two names. */
type Ctx = { chart: AgChartTheme; palette: Palette; owner: string; manager: string; narrow: boolean };
const MoneyCtx = createContext<Ctx | null>(null);
const useMoneyCtx = () => useContext(MoneyCtx)!;

export type GroupSum = { key: string; label: string } & Record<Measure, number>;

const KIND: Record<string, string> = { stock: "Stock", expense: "Expense", asset: "Asset", wages: "Wages" };
const GROUPS: (Group | null)[] = ["week", "supplier", "item", null];
const SORT_COL: Record<string, string> = { approved: "approved", supplier: "supplier", item: "title" };
const EVENT_TONE: Record<string, string> = {
  authorization: "warn", reauthorization: "warn", capture: "leaf", void: "clay", return: "clay", payout: "leaf",
};

/** A group's name the way Chipo reads it: "Week of 2 Nov", "Released, change back", "Wages". */
function groupName(field: string | null | undefined, key: string): string {
  if (field === "week") return weekLabel(key);
  if (field === "state") return STATE[key as MoneyRow["state"]]?.label ?? key;
  if (field === "kind") return KIND[key] ?? cap(key);
  return key || "Other";
}

/** A row's day against the date filter's day, both at local midnight. */
function dayCompare(filter: Date, cell: string): number {
  const [y, m, d] = String(cell).split("-").map(Number);
  const at = new Date(y, m - 1, d).getTime();
  return at < filter.getTime() ? -1 : at > filter.getTime() ? 1 : 0;
}

/** The spec as the grid's own filter model: set filters for names, a date range, and "has" as an amount over zero. */
function filterModel(spec: Spec, today: string): FilterModel {
  const f = spec.filter;
  const model: FilterModel = {};
  if (f.since || f.until) {
    model.approved = { filterType: "date", type: "inRange", dateFrom: `${f.since ?? "2020-01-01"} 00:00:00`, dateTo: `${f.until ?? today} 00:00:00` };
  }
  const sets: [keyof Spec["filter"], string][] = [["items", "item"], ["suppliers", "supplier"], ["by", "by"], ["kinds", "kind"], ["states", "state"]];
  for (const [name, col] of sets) {
    const values = f[name] as string[];
    if (values.length) model[col] = { filterType: "set", values };
  }
  if (f.has) model[f.has] = { filterType: "number", type: "greaterThan", filter: 0 };
  return model;
}

/** Newest first, groups by their newest day; a spec's sort when it names one. */
function sortState(spec: Spec | null): ColumnState[] {
  const s = spec?.sort;
  return [s ? { colId: SORT_COL[s.column] ?? s.column, sort: s.direction } : { colId: "approved", sort: "desc" }];
}

const money = (cents: number | null | undefined) => (cents ? usd(cents) : "");
/** On a phone the header carries the currency and each cell the figure alone, as in Books. */
const figure = (cents: number | null | undefined) => (cents ? usd(cents).replace(/^US\$/, "") : "");

type Props = {
  shop: Shop;
  rows: MoneyRow[];
  today: string;
  group: Group | null;
  spec: Spec | null;
  chart: AgChartTheme;
  palette: Palette;
  setGroup: (g: Group | null) => void;
  onGroups: (groups: GroupSum[]) => void;
  narrow: boolean;
};

/** Every request PayPal held money for, grouped by week or supplier. A row opens its PayPal record and its proof. */
export function MoneyGrid({ shop, rows, today, group, spec, chart, palette, setGroup, onGroups, narrow }: Props) {
  const theme = useDocTheme();
  const [api, setApi] = useState<GridApi<MoneyRow>>();
  const [quick, setQuick] = useState("");
  const opened = useRef(false);
  const ctx = useMemo<Ctx>(() => ({ chart, palette, owner: shop.owner.name, manager: shop.manager.name, narrow }),
    [chart, palette, shop.owner.name, shop.manager.name, narrow]);

  // Request, supplier, the three amounts and the proof fit the console's 796 px; the day, who asked and the
  // PayPal state read in each request's second line. Hidden columns carry the filters, the groups and the export.
  const cols = useMemo<ColDef<MoneyRow>[]>(() => [
    {
      colId: "title", field: "title", headerName: "Request", showRowGroup: true, cellRenderer: "agGroupCellRenderer",
      cellRendererParams: { innerRenderer: RequestCell, suppressCount: true },
      minWidth: narrow ? 168 : 236, flex: 2.6,
      getQuickFilterText: (p) => `${p.data?.title} ${p.data?.supplier} ${p.data?.by} ${p.data?.authorization_id}`,
    },
    // Grouped by week from the first draw, so only the week groups open by default and every request stays closed.
    { colId: "week", field: "week", headerName: "Week", hide: true, enableRowGroup: true, initialRowGroup: true },
    {
      // A group carries its newest day, so sorting by day puts the newest week first.
      colId: "approved", field: "approved_day", headerName: "Approved", hide: true, cellClass: "day",
      aggFunc: (p) => p.values.reduce((a: string, b: string) => (b > a ? b : a), ""),
      filter: "agDateColumnFilter", filterParams: { comparator: dayCompare, inRangeInclusive: true },
    },
    { colId: "supplier", field: "supplier", headerName: "Supplier", minWidth: 132, flex: 1.1, hide: narrow, enableRowGroup: true, filter: "agSetColumnFilter" },
    { colId: "item", field: "item", headerName: "Item", hide: true, enableRowGroup: true, filter: "agSetColumnFilter" },
    {
      colId: "kind", field: "kind", headerName: "Kind", hide: true, enableRowGroup: true, filter: "agSetColumnFilter",
      filterParams: { valueFormatter: (p: { value: string }) => KIND[p.value] ?? p.value },
    },
    { colId: "by", field: "by", headerName: "Asked by", hide: true, enableRowGroup: true, filter: "agSetColumnFilter" },
    {
      colId: "held", field: "held_cents", headerName: "Held", type: "rightAligned", aggFunc: "sum", minWidth: 96, flex: 0.9, hide: narrow,
      valueFormatter: (p) => money(p.value), cellClass: ["o-num", "money"], filter: "agNumberColumnFilter",
    },
    {
      colId: "released", field: "released_cents", headerName: "Released", type: "rightAligned", aggFunc: "sum", minWidth: narrow ? 86 : 96, flex: 0.9,
      valueFormatter: (p) => (narrow ? figure(p.value) : money(p.value)), cellClass: ["o-num", "o-mv-in", "money"], filter: "agNumberColumnFilter",
    },
    {
      colId: "returned", field: "returned_cents", headerName: narrow ? "Back" : "Returned", type: "rightAligned", aggFunc: "sum", minWidth: narrow ? 72 : 96, flex: 0.9,
      valueFormatter: (p) => (narrow ? figure(p.value) : money(p.value)), cellClass: ["o-num", "o-mv-back", "money"], filter: "agNumberColumnFilter",
    },
    { colId: "open", field: "open_cents", headerName: "Held now", hide: true, aggFunc: "sum", filter: "agNumberColumnFilter", cellClass: "money" },
    { colId: "score", field: "score", headerName: "Proof", minWidth: 92, flex: 0.6, hide: narrow, cellRenderer: ScoreCell, filter: "agNumberColumnFilter" },
    {
      colId: "state", field: "state", headerName: "PayPal", hide: true, enableRowGroup: true,
      filter: "agSetColumnFilter", filterParams: { valueFormatter: (p: { value: string }) => STATE[p.value as MoneyRow["state"]]?.label ?? p.value },
    },
    { colId: "paypal", field: "authorization_id", headerName: "PayPal authorization", hide: true },
  ], [narrow]);

  // The spec, the grouping and the sort go to the grid through its own APIs.
  useEffect(() => {
    if (!api) return;
    api.applyColumnState({ state: group ? [{ colId: group, rowGroup: true, rowGroupIndex: 0 }] : [], defaultState: { rowGroup: false } });
    api.setFilterModel(spec ? filterModel(spec, today) : null);
    api.applyColumnState({ state: sortState(spec), defaultState: { sort: null } });
  }, [api, spec, group, today]);

  // Each change of the grid's model reads its top groups back, for the chart beside Musha's answer.
  useEffect(() => {
    if (!api) return;
    const read = () => {
      const out: GroupSum[] = [];
      api.forEachNodeAfterFilterAndSort((node: IRowNode<MoneyRow>) => {
        if (!node.group || node.level !== 0) return;
        const agg = (node.aggData ?? {}) as Record<string, number>;
        out.push({ key: String(node.key), label: groupName(node.field, String(node.key)),
          held: agg.held ?? 0, released: agg.released ?? 0, returned: agg.returned ?? 0, open: agg.open ?? 0 });
      });
      onGroups(out);
    };
    read();
    api.addEventListener("modelUpdated", read);
    return () => {
      if (!api.isDestroyed()) api.removeEventListener("modelUpdated", read);
    };
  }, [api, onGroups]);

  // The newest proven buy opens once, so its PayPal record and proof show on arrival.
  const firstOpen = useCallback(() => {
    if (!api || opened.current || !rows.length) return;
    opened.current = true;
    const newest = [...rows].filter((r) => r.proof).sort((a, b) => b.approved_at.localeCompare(a.approved_at))[0];
    const node = newest && api.getRowNode(String(newest.id));
    if (node) node.setExpanded(true);
  }, [api, rows]);
  useEffect(firstOpen, [firstOpen]);

  const onRowClicked = useCallback((e: RowClickedEvent<MoneyRow>) => {
    const target = e.event?.target as HTMLElement | undefined;
    if (e.node.group || !e.node.master || target?.closest(".ag-group-contracted, .ag-group-expanded, button, a, code")) return;
    e.node.setExpanded(!e.node.expanded);
  }, []);

  const exportExcel = () => {
    api?.exportDataAsExcel({
      fileName: `${shop.name.replace(/[^\w ]+/g, "").trim()} money.xlsx`,
      sheetName: "PayPal",
      author: "Musha",
      columnKeys: ["title", "approved", "supplier", "by", "held", "released", "returned", "score", "state", "paypal"],
      processCellCallback: (p) => {
        const id = p.column.getColId();
        if (["held", "released", "returned", "open"].includes(id)) return p.value == null ? "" : (p.value / 100).toFixed(2);
        if (id === "approved") return p.value ? `${p.value}T00:00:00` : "";
        if (id === "state") return STATE[p.value as MoneyRow["state"]]?.label ?? p.value ?? "";
        if (id === "paypal") return p.value || "Opening books";
        return p.value ?? "";
      },
      processRowGroupCallback: (p) => groupName(p.node.field, String(p.node.key)),
      getCustomContentBelowRow: (p) => (p.node.data && !p.node.group ? eventRows(p.node.data, shop) : undefined),
    });
  };

  return (
    <MoneyCtx.Provider value={ctx}>
      <div className="o-mv-gridbar">
        <div className="o-mv-seg" role="group" aria-label="Group the requests">
          {GROUPS.map((g) => (
            <button key={g ?? "none"} type="button" aria-pressed={group === g} onClick={() => setGroup(g)}>
              {g ? GROUP_LABEL[g] : "Every request"}
            </button>
          ))}
        </div>
        <input className="o-search" type="search" value={quick} onChange={(e) => setQuick(e.target.value)}
          placeholder="Search the money" aria-label="Search the money" />
        <button className="o-btn o-ghost" type="button" onClick={exportExcel} disabled={!api}>
          <svg className="o-mv-xl" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4h10l6 6v10H4z M14 4v6h6 M8 13l4 5 M12 13l-4 5" /></svg>
          Export to Excel
        </button>
      </div>
      <div className={`o-grid o-mv-grid ag-theme-mode${narrow ? " o-grid-narrow" : ""}`} data-ag-theme-mode={theme}>
        <AgGridReact<MoneyRow> theme={gridTheme} modules={MODULES} rowData={rows} columnDefs={cols}
          getRowId={(p) => String(p.data.id)} domLayout="autoHeight" quickFilterText={quick}
          groupDisplayType="custom" groupDefaultExpanded={1} groupAllowUnbalanced
          masterDetail detailRowAutoHeight detailCellRenderer={Journey} isRowMaster={(d) => !!d?.events.length}
          getRowHeight={(p) => (p.node.group ? 46 : narrow ? 58 : 54)}
          excelStyles={EXCEL_STYLES} statusBar={{ statusPanels: [{ statusPanel: Totals, align: "left" }] }}
          defaultColDef={{ sortable: true, resizable: false, suppressHeaderMenuButton: true, suppressHeaderFilterButton: true }}
          suppressAggFuncInHeader
          onGridReady={(e) => setApi(e.api)} onFirstDataRendered={firstOpen} onRowClicked={onRowClicked}
          suppressCellFocus enableCellTextSelection animateRows />
      </div>
    </MoneyCtx.Provider>
  );
}

const EXCEL_STYLES: ExcelStyle[] = [
  { id: "header", font: { bold: true, color: "#A4471F" }, interior: { color: "#F4ECE0", pattern: "Solid" } },
  { id: "money", dataType: "Number", numberFormat: { format: '"US$"#,##0.00' } },
  { id: "day", dataType: "DateTime", numberFormat: { format: "d mmm yyyy" } },
  { id: "stamp", dataType: "DateTime", numberFormat: { format: "d mmm yyyy hh:mm" } },
  { id: "event", font: { color: "#6B6054" } },
  { id: "eventHead", font: { bold: true, color: "#6B6054" } },
];

/** Under each request in the Excel file: its PayPal events, one line each, with PayPal's own id. */
function eventRows(row: MoneyRow, shop: Shop): ExcelRow[] {
  const text = (value: string, styleId = "event"): ExcelCell => ({ data: { type: "String", value }, styleId });
  return [
    { outlineLevel: 1, cells: [text(""), text("PayPal event", "eventHead"), text("PayPal id", "eventHead"), text("Amount", "eventHead"), text("When", "eventHead")] },
    ...row.events.map((e) => ({
      outlineLevel: 1,
      cells: [
        text(""), text(eventLabel(e, shop)), text(e.paypal_id || "Opening books"),
        { data: { type: "Number" as const, value: (e.amount_cents / 100).toFixed(2) }, styleId: "money" },
        { data: { type: "DateTime" as const, value: e.at.slice(0, 19) }, styleId: "stamp" },
      ],
    })),
  ];
}

function eventLabel(e: PayPalEvent, shop: Shop): string {
  if (e.kind === "payout") return e.source === "wages" ? `Wages to ${shop.manager.name}'s PayPal` : EVENT_LABEL.payout;
  if (e.kind === "return") return `The rest went back to ${shop.owner.name}`;
  return EVENT_LABEL[e.kind];
}

function RequestCell(p: ICellRendererParams<MoneyRow>) {
  const { narrow } = useMoneyCtx();
  if (p.node.group) {
    const n = p.node.allChildrenCount ?? 0;
    return (
      <span className="o-mv-group">
        <b>{groupName(p.node.field, String(p.node.key))}</b>
        {!narrow && <small>{n} {n === 1 ? "request" : "requests"}</small>}
      </span>
    );
  }
  const d = p.data;
  if (!d) return null;
  const s = STATE[d.state];
  return (
    <span className="o-two o-mv-req">
      <span>{d.title}</span>
      <small>
        <i className={`o-dot o-tone-${s.tone}`} aria-hidden="true" />
        {s.label} · {dayLabel(d.approved_day)}
      </small>
    </span>
  );
}

function ScoreCell(p: ICellRendererParams<MoneyRow, number | null>) {
  if (p.node.group || p.value == null) return null;
  return (
    <span className={`o-mv-score o-tone-${band(p.value)}`} title={`Proof score ${p.value} of 100`}>
      <span className="o-mv-score-track" aria-hidden="true"><i style={{ width: `${p.value}%` }} /></span>
      <b>{p.value}</b>
    </span>
  );
}

/** The status bar: what the rows on show add up to, read again as the filters change. */
function Totals(props: CustomStatusPanelProps) {
  const [t, setT] = useState({ n: 0, held: 0, released: 0, returned: 0, open: 0 });
  useEffect(() => {
    const api = props.api;
    const read = () => {
      const next = { n: 0, held: 0, released: 0, returned: 0, open: 0 };
      api.forEachNodeAfterFilter((node: IRowNode<MoneyRow>) => {
        const d = node.data;
        if (node.group || !d) return;
        next.n += 1;
        next.held += d.held_cents;
        next.released += d.released_cents;
        next.returned += d.returned_cents;
        next.open += d.open_cents;
      });
      setT(next);
    };
    read();
    api.addEventListener("modelUpdated", read);
    return () => {
      if (!api.isDestroyed()) api.removeEventListener("modelUpdated", read);
    };
  }, [props.api]);
  return (
    <div className="o-mv-totals" aria-live="polite">
      <span><b>{t.n}</b> {t.n === 1 ? "request" : "requests"}</span>
      <span>Held <b>{usd(t.held)}</b></span>
      <span className="o-tone-leaf">Released <b>{usd(t.released)}</b></span>
      <span className="o-tone-clay">Returned <b>{usd(t.returned)}</b></span>
      {t.open > 0 && <span className="o-tone-warn">Held now <b>{usd(t.open)}</b></span>}
    </div>
  );
}

/** A request's PayPal record, step by step with PayPal's own ids, beside the proof that released it. */
function Journey(p: ICellRendererParams<MoneyRow>) {
  const { owner, manager } = useMoneyCtx();
  const d = p.data;
  if (!d) return null;
  const shopLike = { owner: { name: owner }, manager: { name: manager } } as Shop;
  return (
    <div className="o-mv-journey">
      <section className="o-mv-trail" aria-label="PayPal's record">
        <h4>PayPal's record</h4>
        <ol>
          {d.events.map((e, i) => (
            <li key={`${e.kind}${i}`} className={`o-tone-${EVENT_TONE[e.kind]}`}>
              <i aria-hidden="true" />
              <span className="o-mv-step">
                <b>{eventLabel(e, shopLike)}</b>
                <small>
                  <code title={e.paypal_id ? "PayPal's id" : "From the shop's opening books"}>{e.paypal_id || "Opening books"}</code>
                  {when(e.at)}{e.kind === "payout" && e.status ? ` · ${cap(e.status)}` : ""}
                </small>
              </span>
              <b className="o-num">{usd(e.amount_cents)}</b>
            </li>
          ))}
        </ol>
        <dl className="o-mv-facts">
          <div><dt>Asked by</dt><dd>{d.by}</dd></div>
          <div><dt>Approved by</dt><dd>{d.approved_by}, {when(d.approved_at)}</dd></div>
          {d.supplier && <div><dt>{d.kind === "wages" ? "Paid to" : "Supplier"}</dt><dd>{d.supplier}</dd></div>}
          <div><dt>PayPal authorization</dt><dd><code>{d.authorization_id || "Opening books"}</code></dd></div>
        </dl>
      </section>
      {d.proof ? <ProofCard proof={d.proof} /> : (
        <section className="o-mv-proof o-mv-proof-wait">
          <h4>The proof</h4>
          <p className="o-muted">{d.kind === "wages" ? `Wages reach ${manager} by PayPal Payouts once PayPal holds them.`
            : d.state === "held" || d.state === "review" ? `Musha scores the receipt when ${manager} sends it.`
              : `${owner} decided this one on the console.`}</p>
        </section>
      )}
    </div>
  );
}

/** The checks behind one decision as bars: points earned, points missed, and checks that sat this one out. */
export function ProofCard({ proof }: { proof: MoneyProof }) {
  const { chart, palette: p } = useMoneyCtx();
  const read = proof.checks.find((c) => c.key === "read");
  const data = proof.checks.filter((c) => c.max > 0).map((c) => ({
    label: c.label,
    earned: c.applies ? c.points : 0,
    missed: c.applies ? Math.max(0, c.max - c.points) : 0,
    off: c.applies ? 0 : c.max,
    reason: c.reason,
    max: c.max,
  }));
  type Row = (typeof data)[number];
  const tip = {
    renderer: ({ datum }: { datum: Row }) => ({ title: datum.label, data: [{ label: "Points", value: `${datum.earned} of ${datum.max}` }, { label: "Why", value: datum.reason }] }),
  };
  const options: AgCartesianChartOptions = {
    theme: chart,
    data,
    height: data.length * 30 + 28,
    minWidth: 0,
    padding: { top: 4, right: 8, bottom: 4, left: 0 },
    series: [
      { type: "bar", direction: "horizontal", xKey: "label", yKey: "earned", yName: "Earned", stacked: true, fill: p.leaf, cornerRadius: 4, tooltip: tip },
      { type: "bar", direction: "horizontal", xKey: "label", yKey: "missed", yName: "Missed", stacked: true, fill: p.dangerSoft, stroke: p.danger, strokeWidth: 1, cornerRadius: 4, tooltip: tip },
      { type: "bar", direction: "horizontal", xKey: "label", yKey: "off", yName: "Sat out", stacked: true, fill: p.sand, cornerRadius: 4, tooltip: tip },
    ],
    axes: {
      x: { type: "category", position: "left", line: { enabled: false }, label: { color: p.ink, fontSize: 12 } },
      y: { type: "number", position: "bottom", min: 0, max: 25, interval: { step: 5 }, label: { color: p.muted, fontSize: 11 } },
    },
    legend: { enabled: false },
  };
  const tone = band(proof.score);
  return (
    <section className="o-mv-proof" aria-label="The proof">
      <div className="o-mv-proof-head">
        <h4>The proof</h4>
        <span className={`o-mv-proof-score o-tone-${tone}`}><b>{proof.score}</b> of 100</span>
      </div>
      {read && <p className={`o-mv-gate ${read.passed ? "o-leaf" : "o-danger"}`}>{read.passed ? "Receipt read" : "Receipt unread"}: {read.reason}</p>}
      <AgCharts options={options} />
      <p className="o-fine">{proof.summary}</p>
    </section>
  );
}
