import { usd, type Shop } from "../api";
import type { CashRow, StockRow, Week } from "../phone/api";
import { cap, todayUtc } from "../phone/util";
import { qty } from "../words";

/** Close the day: today's close, and each day of the week with the till against the books. */
export function Close({ shop, week, open }: { shop: Shop; week?: Week; open: () => void }) {
  if (!week) {
    return <div className="o-loading"><span className="o-spin" aria-hidden="true" /> Reading the week</div>;
  }
  const last = week.cash[week.cash.length - 1];
  const today = last && last.day === todayUtc() ? last : undefined;
  const closed = !!today?.closed;
  const days = [...week.cash].reverse();
  const before = days.find((d) => d.closed);
  const gapsOn = (d: string) => week.stock.filter((s) => s.day === d && s.gap);

  return (
    <div className="m-closeview">
      {closed && today ? <ClosedToday shop={shop} row={today} stock={gapsOn(today.day)} /> : (
        <section className="o-focal o-tone-clay" aria-live="polite">
          <p className="o-eyebrow">Today</p>
          <h2 className="o-focal-title">Close the day when the shop shuts.</h2>
          <p className="o-focal-body">
            Count the till and the shelves. Musha fills in the units sold and the cash the books expect. You change what the count says.
          </p>
          {before && (
            <div className="o-figure">
              <span className="o-big">{usd(before.counted_cents ?? before.expected_close_cents)}</span>
              <span className="o-muted">counted at the last close, {before.day_name}</span>
            </div>
          )}
          <div className="o-row">
            <button type="button" className="o-btn o-primary o-large" onClick={open}>Close the day</button>
          </div>
        </section>
      )}

      <section className="o-section">
        <div className="o-section-head">
          <h2>This week</h2>
          <span className="o-muted o-small-text">The till against the books, day by day.</span>
        </div>
        <ul className="m-days">
          {days.map((d) => <DayRow key={d.day} row={d} stock={gapsOn(d.day)} />)}
        </ul>
      </section>
    </div>
  );
}

function ClosedToday({ shop, row, stock }: { shop: Shop; row: CashRow; stock: StockRow[] }) {
  const gap = row.gap_cents ?? 0;
  const even = Math.abs(gap) < 100;
  return (
    <section className={`o-focal o-tone-${even && !stock.length ? "leaf" : "warn"}`} aria-live="polite">
      <p className="o-eyebrow">Closed</p>
      <h2 className="o-focal-title">Today is closed.</h2>
      <div className="o-figure">
        <span className="o-big">{usd(row.counted_cents ?? 0)}</span>
        <span className="o-muted">counted in the till</span>
      </div>
      <p className={`o-focal-body ${even ? "o-leaf" : "o-danger"}`}>
        {even ? "The till matches the books." : `${usd(Math.abs(gap))} ${gap < 0 ? "short" : "over"}. The books expected ${usd(row.expected_close_cents)}.`}
      </p>
      {stock.length > 0 && (
        <ul className="o-reasons m-stock-gaps">
          {stock.map((s) => <li key={s.item}>{cap(s.item)}: {qty(Math.abs(s.gap!), s.unit)} {s.gap! < 0 ? "short" : "over"} on the shelf.</li>)}
        </ul>
      )}
      {(!even || stock.length > 0) && <p className="o-fine">Musha shows this to {shop.owner.name}.</p>}
    </section>
  );
}

function DayRow({ row, stock }: { row: CashRow; stock: StockRow[] }) {
  const gap = row.gap_cents ?? 0;
  const even = Math.abs(gap) < 100;
  const tone = !row.closed ? "calm" : even ? "leaf" : gap < 0 ? "danger" : "warn";
  const label = !row.closed ? "Open" : even ? "Even" : `${usd(Math.abs(gap))} ${gap < 0 ? "short" : "over"}`;
  return (
    <li className="m-day-row">
      <span className="m-day-name">{row.day_name}</span>
      <span className="m-day-fig">
        <small>Expected</small>
        <b className="o-num">{usd(row.expected_close_cents)}</b>
      </span>
      <span className="m-day-fig">
        <small>Counted</small>
        <b className="o-num">{row.counted_cents == null ? "To count" : usd(row.counted_cents)}</b>
      </span>
      <span className={`o-pill o-tone-${tone}`}>{label}</span>
      {(row.question || stock.length > 0) && (
        <span className="m-day-more">
          {stock.map((s) => `${cap(s.item)} ${qty(Math.abs(s.gap!), s.unit)} ${s.gap! < 0 ? "short" : "over"}`).join(" · ")}
          {row.question && <q>{row.question}</q>}
        </span>
      )}
    </li>
  );
}
