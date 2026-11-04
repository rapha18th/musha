import { usd } from "../api";
import { buysLine, recordFacts, sinceLine, type ManagerRecord } from "../record";
import "./record.css";

/**
 * Farai's record in the chat, a card from Musha among the lines: the same figures as the record on
 * Chipo's console, from the same books.
 */
export function RecordCard({ record }: { record: ManagerRecord }) {
  return (
    <div className="ph-record-row">
      <section className="ph-record" aria-label="Your record">
        <div className="ph-record-head">
          <span className="ph-record-seal" aria-hidden="true">
            <svg viewBox="0 0 24 24"><path d="M7 3h10v18l-2.5-1.6L12 21l-2.5-1.6L7 21z" /><path d="M9.5 8h5M9.5 11.5h5M9.5 15h3" /></svg>
          </span>
          <span className="ph-record-title">Your record</span>
        </div>
        {record.buys === 0 ? (
          <p className="ph-record-line">Your record starts with the first buy PayPal releases on your proof.</p>
        ) : (
          <>
            <p className="ph-record-main">
              <b>{buysLine(record)}</b> {sinceLine(record)}
            </p>
            <p className="ph-record-total">{usd(record.total_cents)} <span>released to the shop</span></p>
            <ul className="ph-record-facts">
              {recordFacts(record).map((f) => <li key={f}>{f}</li>)}
            </ul>
          </>
        )}
      </section>
    </div>
  );
}
