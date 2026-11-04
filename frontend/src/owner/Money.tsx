import { useMemo, useRef, useState } from "react";
import { AgCharts, AgGauge } from "ag-charts-react";
import type { AgCartesianChartOptions, AgChartTheme, AgPolarChartOptions, AgRadialGaugeOptions } from "ag-charts-community";
import { usd, type Entry, type Shop } from "../api";
import type { Week } from "./api";
import { MoneyGrid, type GroupSum } from "./MoneyGrid";
import { ErrorLine, Spinner } from "./parts";
import { useCompact, useDocTheme, usePoll, weekday, when } from "./lib";
import {
  GROUP_LABEL, MEASURE_LABEL, chartTheme, dayLabel, licence, mix, moneyApi, readPalette, weekLabel, type Answer, type Group,
  type Measure, type MoneyConfig, type MoneyData, type OpenHold, type Palette, type RiverWeek,
} from "./moneyData";
import "./money.css";

type Props = { shop: Shop; entries: Entry[]; week?: Week; config: MoneyConfig };

/** Chipo's money at PayPal: where each week's holds went, the clock on every open hold, every request with
 *  its PayPal record and proof, and the till against the books. Musha reads her questions into the grid. */
export function Money({ shop, entries, week, config }: Props) {
  licence(config);
  const theme = useDocTheme();
  const palette = useMemo(() => readPalette(), [theme]); // eslint-disable-line react-hooks/exhaustive-deps
  const chart = useMemo(() => chartTheme(palette, theme === "dark"), [palette, theme]);
  const [box, narrow] = useCompact<HTMLDivElement>(700);
  const [data, setData] = useState<MoneyData>();
  const [error, setError] = useState("");
  const signal = `${entries.length}:${entries.length ? entries[entries.length - 1].id : 0}`;
  const seen = useRef("");
  usePoll(async () => {
    try {
      const next = await moneyApi.money(shop.id);
      const key = JSON.stringify(next.rows) + JSON.stringify(next.holds.open.map((h) => h.request_id));
      if (key !== seen.current) {
        seen.current = key;
        setData(next);
      }
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, 15000, [shop.id, signal]);

  const [answer, setAnswer] = useState<Answer | null>(null);
  const [group, setGroup] = useState<Group | null>("week");
  const [groups, setGroups] = useState<GroupSum[]>([]);
  const onAnswer = (a: Answer | null) => {
    setAnswer(a);
    setGroup(a ? a.spec.group : "week");
  };

  if (!data) {
    return <div className="o-loading">{error ? <ErrorLine error={error} /> : <><Spinner /> Reading PayPal's record</>}</div>;
  }
  const t = data.totals;
  return (
    <div className="o-mv" ref={box}>
      <AskMoney shop={shop} data={data} answer={answer} onAnswer={onAnswer} groups={groups} chart={chart} palette={palette} />

      <div className="o-mv-board">
        <section className="o-mv-card o-mv-river" aria-label="The money river">
          <div className="o-mv-card-head">
            <div>
              <p className="o-eyebrow">The money river</p>
              <h2>Where each week's holds went</h2>
            </div>
            {/* The figures carry the river's key: each label wears its band's colour. */}
            <dl className="o-mv-figures">
              <div className="o-tone-leaf"><dt><i aria-hidden="true" />Released</dt><dd>{usd(t.released_cents)}</dd></div>
              <div className="o-tone-clay"><dt><i aria-hidden="true" />Returned</dt><dd>{usd(t.returned_cents)}</dd></div>
              <div className="o-tone-warn"><dt><i className="o-mv-hatch" aria-hidden="true" />Held now</dt><dd>{usd(t.open_cents)}</dd></div>
            </dl>
          </div>
          <River weeks={data.river} chart={chart} palette={palette} owner={shop.owner.name} />
          <p className="o-fine">
            {t.authorizations
              ? `${t.authorizations} PayPal ${t.authorizations === 1 ? "hold" : "holds"}, ${t.captures} ${t.captures === 1 ? "capture" : "captures"} and ${t.voids} ${t.voids === 1 ? "return" : "returns"}, each booked under PayPal's own id.`
              : "Each PayPal hold is booked under PayPal's own id."}
            {t.opening ? ` The opening books add ${t.opening} ${t.opening === 1 ? "buy" : "buys"}.` : ""}
          </p>
        </section>
        <HoldClock holds={data.holds} chart={chart} palette={palette} owner={shop.owner.name} />
      </div>

      <section className="o-section">
        <div className="o-section-head">
          <h2>Every PayPal movement</h2>
          <span className="o-muted o-small-text">A row opens its PayPal record and its proof.</span>
        </div>
        <MoneyGrid shop={shop} rows={data.rows} today={data.now.slice(0, 10)} group={group} setGroup={setGroup}
          spec={answer?.spec ?? null} chart={chart} palette={palette} onGroups={setGroups} narrow={narrow} />
      </section>

      <CashGap week={week} chart={chart} palette={palette} manager={shop.manager.name} />
    </div>
  );
}

/** Chipo asks in her own words; Musha answers in a sentence and sets the grid and a chart to show it. */
function AskMoney({ shop, data, answer, onAnswer, groups, chart, palette }: {
  shop: Shop; data: MoneyData; answer: Answer | null; onAnswer: (a: Answer | null) => void; groups: GroupSum[];
  chart: AgChartTheme; palette: Palette;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  // Examples from the shop's own rows: the item that took the most money, and the supplier paid most.
  const chips = useMemo(() => {
    const most = (key: "item" | "supplier") => {
      const sums = new Map<string, number>();
      for (const r of data.rows) if (r.kind !== "wages") sums.set(r[key], (sums.get(r[key]) ?? 0) + r.released_cents);
      return [...sums.entries()].sort((a, b) => b[1] - a[1])[0]?.[0];
    };
    const item = most("item");
    return [
      "What did PayPal return to me this month, by supplier?",
      data.totals.open_cents ? "What does PayPal hold right now?" : "",
      item ? `What went to the shop for ${item}, week by week?` : "",
      "Show the share released per item",
    ].filter(Boolean);
  }, [data]);

  const ask = async (q: string) => {
    const question = q.trim();
    if (!question || busy) return;
    setBusy(question);
    setError("");
    try {
      onAnswer(await moneyApi.ask(shop.id, question));
      setText("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };

  return (
    <section className="o-mv-ask" aria-label="Ask Musha about the money">
      <form className="o-mv-ask-form" onSubmit={(e) => {
        e.preventDefault();
        ask(text);
      }}>
        <span className="o-musha-mark" aria-hidden="true">M</span>
        <input value={text} onChange={(e) => setText(e.target.value)} maxLength={300}
          placeholder="Ask Musha about the money" aria-label="Ask Musha about the money" />
        <button className="o-btn o-primary" type="submit" disabled={!text.trim() || !!busy}>
          {busy ? <Spinner /> : null}Ask
        </button>
      </form>
      {!answer && !busy && (
        <div className="o-chips o-mv-chips">
          {chips.map((c) => <button key={c} type="button" className="o-chip" onClick={() => ask(c)}>{c}</button>)}
        </div>
      )}
      {busy && <p className="o-fine o-mv-asking"><Spinner /> Musha is reading “{busy}”</p>}
      <ErrorLine error={error} />
      {answer && !busy && (
        <div className="o-mv-answer">
          <div className="o-mv-answer-text">
            <p className="o-eyebrow">{answer.spec.title}</p>
            <p className="o-mv-answer-line">{answer.answer}</p>
            <p className="o-fine">
              “{answer.question}” · {answer.by === "model" ? "Read by Musha" : "Read by the books' rules"}
              {answer.spec.group ? ` · grouped by ${GROUP_LABEL[answer.spec.group].toLowerCase()}` : ""}
            </p>
            <button type="button" className="o-textbtn" onClick={() => onAnswer(null)}>Show every movement</button>
          </div>
          {answer.spec.chart && groups.length > 0 && (
            <AnswerChart type={answer.spec.chart.type} measure={answer.spec.chart.measure} groups={groups} chart={chart} palette={palette} />
          )}
        </div>
      )}
    </section>
  );
}

const MEASURE_TONE = (p: Palette): Record<Measure, string> => ({ held: p.muted, released: p.leaf, returned: p.clay, open: p.warn });

/** The answer as a chart, drawn from the grid's own groups after Musha's filter, so the two always agree. */
function AnswerChart({ type, measure, groups, chart, palette }: {
  type: "bar" | "donut"; measure: Measure; groups: GroupSum[]; chart: AgChartTheme; palette: Palette;
}) {
  const data = groups.filter((g) => g[measure] > 0).map((g) => ({ name: g.label, value: g[measure] / 100 }));
  if (!data.length) return null;
  const fill = MEASURE_TONE(palette)[measure];
  if (type === "donut") {
    const total = data.reduce((a, d) => a + d.value, 0);
    const shades = [fill, palette.clay, palette.warn, palette.muted, palette.leaf, palette.danger];
    const options: AgPolarChartOptions = {
      theme: chart, data, height: 220, minWidth: 0,
      series: [{
        type: "donut", angleKey: "value", legendItemKey: "name", innerRadiusRatio: 0.64, fills: shades, strokes: [palette.paper],
        strokeWidth: 2, innerLabels: [{ text: usd(Math.round(total * 100)), fontSize: 18, fontWeight: 600, color: palette.ink },
          { text: MEASURE_LABEL[measure], fontSize: 12, color: palette.muted }],
        tooltip: { renderer: ({ datum }) => ({ title: datum.name, data: [{ label: MEASURE_LABEL[measure], value: usd(Math.round(datum.value * 100)) }] }) },
      }],
      legend: { position: "right", item: { marker: { shape: "circle" } } },
    };
    return <div className="o-mv-answer-chart"><AgCharts options={options} /></div>;
  }
  const options: AgCartesianChartOptions = {
    theme: chart, data, height: Math.max(120, data.length * 38 + 30), minWidth: 0,
    series: [{
      type: "bar", direction: "horizontal", xKey: "name", yKey: "value", yName: MEASURE_LABEL[measure], fill, cornerRadius: 6,
      label: { formatter: ({ value }) => usd(Math.round(value * 100)), placement: "inside-end", color: palette.paper, fontWeight: 600 },
      tooltip: { renderer: ({ datum }) => ({ title: datum.name, data: [{ label: MEASURE_LABEL[measure], value: usd(Math.round(datum.value * 100)) }] }) },
    }],
    axes: {
      x: { type: "category", position: "left", line: { enabled: false } },
      y: { type: "number", position: "bottom", label: { formatter: ({ value }) => `US$${value}` } },
    },
    legend: { enabled: false },
  };
  return <div className="o-mv-answer-chart"><AgCharts options={options} /></div>;
}

/** Held, released and returned by the week each hold began: three bands that add up to what PayPal held. */
function River({ weeks, chart, palette: p, owner }: { weeks: RiverWeek[]; chart: AgChartTheme; palette: Palette; owner: string }) {
  if (!weeks.length) return <p className="o-muted o-mv-empty">The river fills with the first PayPal hold.</p>;
  const data = weeks.map((w) => ({
    week: dayLabel(w.week), when: weekLabel(w.week),
    released: w.released_cents / 100, returned: w.returned_cents / 100, open: w.open_cents / 100, raw: w,
  }));
  type D = (typeof data)[number];
  const ids = (list: string[]) => {
    const real = list.filter((x) => x !== "opening books");
    const opening = list.length - real.length;
    return [real.slice(0, 2).join(", ") + (real.length > 2 ? ` and ${real.length - 2} more` : ""), opening ? `${opening} from the opening books` : ""]
      .filter(Boolean).join("; ") || "None";
  };
  const tip = (band: "released" | "returned" | "open") => ({
    renderer: ({ datum }: { datum: D }) => ({
      title: datum.when,
      data: [
        { label: band === "released" ? "Released" : band === "returned" ? "Returned" : "Held now", value: usd(Math.round(datum[band] * 100)) },
        band === "released" ? { label: "Captures", value: ids(datum.raw.captures) }
          : band === "returned" ? { label: "Voids and returns", value: ids(datum.raw.voids) }
            : { label: "Authorizations", value: ids(datum.raw.authorizations) },
      ],
    }),
  });
  // Settled money reads solid, deep at the surface and pale at the bed; money PayPal still holds is hatched.
  const flow = (color: string) => ({
    type: "gradient" as const, rotation: 90,
    colorStops: [{ color, stop: 0 }, { color: mix(color, p.paper, 0.4), stop: 1 }],
  });
  const area = {
    type: "area" as const, xKey: "week", stacked: true, strokeWidth: 2, interpolation: { type: "smooth" as const },
    marker: { enabled: true, size: 7, stroke: p.paper, strokeWidth: 2 },
  };
  const options: AgCartesianChartOptions = {
    theme: chart, data, height: 236, minWidth: 0, padding: { top: 8, right: 22, bottom: 4, left: 4 },
    series: [
      { ...area, yKey: "released", yName: "Released to the shop", fill: flow(p.leaf), stroke: p.leaf, marker: { ...area.marker, fill: p.leaf }, tooltip: tip("released") },
      { ...area, yKey: "returned", yName: `Returned to ${owner}`, fill: flow(p.clay), stroke: p.clay, marker: { ...area.marker, fill: p.clay }, tooltip: tip("returned") },
      {
        ...area, yKey: "open", yName: "Held at PayPal", stroke: p.warn, marker: { ...area.marker, fill: p.warn }, tooltip: tip("open"),
        fill: { type: "pattern", pattern: "forward-slanted-lines", stroke: p.warn, strokeWidth: 2, width: 8, height: 8, backgroundFill: p.warnSoft },
      },
    ],
    axes: {
      x: { type: "category", position: "bottom", line: { enabled: false }, label: { color: p.muted } },
      y: { type: "number", position: "left", label: { color: p.muted, formatter: ({ value }) => `US$${value}` }, gridLine: { style: [{ stroke: p.line, lineDash: [3, 4] }] } },
    },
    legend: { enabled: false },
  };
  return <AgCharts options={options} />;
}

/** A hold's life in minutes, hours or days. */
function span(hours: number): string {
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))} min`;
  return hours < 48 ? `${hours} h` : `${(hours / 24).toFixed(1)} days`;
}

/** Each open hold against PayPal's 29 days, with the 3-day honor period and Musha's return marked on the dial. */
function HoldClock({ holds, chart, palette, owner }: { holds: MoneyData["holds"]; chart: AgChartTheme; palette: Palette; owner: string }) {
  const shown = holds.open.slice(0, 2);
  return (
    <section className="o-mv-card o-mv-clock" aria-label="The hold clock">
      <p className="o-eyebrow">The hold clock</p>
      <h2>{shown.length ? `${holds.open.length} ${holds.open.length === 1 ? "hold" : "holds"} at PayPal` : "Every hold is closed"}</h2>
      {shown.length ? (
        <div className="o-mv-dials">
          {shown.map((h) => <Dial key={h.request_id} hold={h} chart={chart} palette={palette} />)}
        </div>
      ) : (
        <div className="o-mv-calm">
          {holds.median_hours != null ? (
            <>
              <p className="o-mv-calm-figure">{span(holds.median_hours)}</p>
              <p className="o-muted">The median time PayPal held {owner}'s money, across {holds.closed} closed {holds.closed === 1 ? "hold" : "holds"}{holds.median_hours < 72 ? ", well inside the 3-day honor period" : ""}.</p>
            </>
          ) : (
            <p className="o-muted">PayPal holds nothing for {owner} now. Each new hold gets its dial here.</p>
          )}
        </div>
      )}
      <ul className="o-mv-key">
        <li><i className="o-mv-key-honor" aria-hidden="true" />Honor period, 3 days</li>
        <li><i className="o-mv-key-life" aria-hidden="true" />Musha renews it once</li>
        <li><i className="o-mv-key-end" aria-hidden="true" />Back to {owner} before day 29</li>
      </ul>
    </section>
  );
}

function Dial({ hold: h, chart, palette: p }: { hold: OpenHold; chart: AgChartTheme; palette: Palette }) {
  const life = h.life_days || 29;
  const options: AgRadialGaugeOptions = {
    // minWidth 0 on every chart here: the card sets the width, and AG Charts' own minimum overflows a phone
    type: "radial-gauge", theme: chart, height: 168, minWidth: 0, value: Math.min(life, h.days_held),
    scale: {
      min: 0, max: life, fillMode: "discrete",
      fills: [{ color: p.leafSoft, stop: 3 }, { color: p.sand, stop: Math.max(3, life - 0.5) }, { color: p.dangerSoft, stop: life }],
      label: { enabled: false },
    },
    bar: { fill: h.state === "flagged" ? p.danger : p.warn },
    innerRadiusRatio: 0.78, cornerRadius: 99, cornerMode: "container",
    targets: [
      { value: 3, shape: "line", placement: "middle", size: 22, stroke: p.leaf, strokeWidth: 2 },
      { value: Math.max(3, life - 0.5), shape: "line", placement: "middle", size: 22, stroke: p.danger, strokeWidth: 2 },
    ],
    label: { text: String(Math.floor(h.days_left)), color: p.ink, fontFamily: "Fraunces, Georgia, serif", fontWeight: 600 },
    secondaryLabel: { text: Math.floor(h.days_left) === 1 ? "day left" : "days left", color: p.muted },
    tooltip: { enabled: false },
  };
  const honor = new Date(h.honor_until) > new Date();
  return (
    <div className="o-mv-dial">
      <AgGauge options={options} />
      <p className="o-mv-dial-what"><b>{h.title}</b> · {usd(h.amount_cents)}</p>
      <p className="o-fine">{honor ? `Honor period until ${when(h.honor_until)}` : h.renew_at && !h.reauthorized_at ? `Musha renews it ${when(h.renew_at)}` : `Musha returns it ${when(h.void_at)} unless it is spent`}</p>
      <code className="o-mv-id" title="PayPal authorization">{h.authorization_id}</code>
    </div>
  );
}

/** What the till should hold each day against what Farai counted, the gap marked in its colour. */
function CashGap({ week, chart, palette: p, manager }: { week?: Week; chart: AgChartTheme; palette: Palette; manager: string }) {
  const rows = week?.cash ?? [];
  const data = rows.map((r) => ({
    day: weekday(r.day).split(" ").slice(0, 2).join(" "), name: r.day_name,
    expected: r.expected_close_cents / 100, counted: r.counted_cents == null ? undefined : r.counted_cents / 100,
    gap: r.gap_cents, answer: r.answer ?? "",
  }));
  type D = (typeof data)[number];
  const short = rows.filter((r) => (r.gap_cents ?? 0) < -100);
  const tone = (gap: number | null) => (gap == null ? p.sand : gap < -100 ? p.danger : gap > 100 ? p.warn : p.leaf);
  const options: AgCartesianChartOptions = {
    theme: chart, data, height: 220, minWidth: 0,
    series: [
      {
        type: "bar", xKey: "day", yKey: "counted", yName: "Counted", cornerRadius: 6, fill: p.leaf,
        itemStyler: ({ datum }) => ({ fill: tone((datum as D).gap) }),
        tooltip: {
          renderer: ({ datum }: { datum: D }) => ({
            title: datum.name,
            data: [
              { label: "Expected", value: usd(Math.round(datum.expected * 100)) },
              { label: "Counted", value: datum.counted == null ? "Open" : usd(Math.round(datum.counted * 100)) },
              ...(datum.gap ? [{ label: "Gap", value: `${usd(Math.abs(datum.gap))} ${datum.gap < 0 ? "short" : "over"}` }] : []),
              ...(datum.answer ? [{ label: `${manager} said`, value: datum.answer }] : []),
            ],
          }),
        },
      },
      {
        type: "line", xKey: "day", yKey: "expected", yName: "Expected", stroke: p.ink, strokeWidth: 1.5, lineDash: [4, 4],
        marker: { shape: "circle", size: 7, fill: p.paper, stroke: p.ink, strokeWidth: 1.5 },
      },
    ],
    axes: {
      x: { type: "category", position: "bottom", line: { enabled: false }, label: { color: p.muted } },
      y: { type: "number", position: "left", label: { color: p.muted, formatter: ({ value }) => `US$${value}` }, gridLine: { style: [{ stroke: p.line, lineDash: [3, 4] }] } },
    },
    legend: { position: "bottom", item: { marker: { shape: "circle" } } },
  };
  return (
    <section className="o-section">
      <div className="o-section-head">
        <h2>Cash gap</h2>
        <span className="o-muted o-small-text">Expected cash in the till against {manager}'s count, by day.</span>
      </div>
      <div className="o-mv-card">
        {rows.length ? <AgCharts options={options} /> : <div className="o-loading"><Spinner /> Reading the week</div>}
        {short.map((r) => (
          <p key={r.day} className="o-mv-gapline">
            <b className="o-danger">{r.day_name}, {usd(Math.abs(r.gap_cents ?? 0))} short.</b>{" "}
            {r.answer ? <>{manager} answered: <span className="o-quote">{r.answer}</span></> : r.question}
          </p>
        ))}
      </div>
    </section>
  );
}

