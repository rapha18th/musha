import { usd, type Shop } from "../api";
import { buysLine, matchedBuys, recordFacts, sinceLine, type ManagerRecord } from "../record";
import "./record.css";

/**
 * The manager's record on the owner's console: every buy PayPal released on the manager's proof since
 * the first, the total, the receipts on file and PayPal's part. Every figure comes from the books;
 * `matched` holds the request ids of the releases the last match of PayPal to the books found.
 */
export function RecordTile({ shop, record, matched }: { shop: Shop; record?: ManagerRecord; matched?: Set<number> }) {
  if (!record) return null;
  const name = shop.manager.name;
  return (
    <section className="o-record" aria-label={`${name}'s record`}>
      <div className="o-record-head">
        <p className="o-eyebrow">{name}'s record</p>
        <span className="o-record-seal" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M7 3h10v18l-2.5-1.6L12 21l-2.5-1.6L7 21z" /><path d="M9.5 8h5M9.5 11.5h5M9.5 15h3" /></svg>
        </span>
      </div>
      {record.buys === 0 ? (
        <p className="o-record-empty">{name}'s record starts with the first buy PayPal releases on {name}'s proof.</p>
      ) : (
        <>
          <div className="o-record-main">
            <p className="o-record-count">
              <span className="o-record-n">{record.buys}</span>
              <span className="o-record-what">
                <b>{buysLine(record).replace(/^\d+ /, "")}</b>
                <span>{sinceLine(record)}</span>
              </span>
            </p>
            <p className="o-record-total">
              <span className="o-record-value">{usd(record.total_cents)}</span>
              <span className="o-record-label">released to the shop</span>
            </p>
          </div>
          <ul className="o-record-facts">
            {recordFacts(record, matched && matchedBuys(record, matched)).map((f) => (
              <li key={f}>
                <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m3.5 8.5 3 3 6-7" /></svg>
                {f}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
