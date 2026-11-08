import { useState } from "react";
import { usd } from "../api";
import { buysLine, recordFacts, sinceLine, type ManagerRecord } from "../record";
import { cap, day } from "../owner/lib";
import "../owner/record.css";

const SHOWN = 8;

/**
 * The manager's record: every buy PayPal released on her proof, the total, the receipts on file and PayPal's
 * part. The same figures as the record on the owner's console, from the same books.
 */
export function Record({ record, owner }: { record?: ManagerRecord; owner: string }) {
  const [all, setAll] = useState(false);
  if (!record) return <div className="o-loading"><span className="o-spin" aria-hidden="true" /> Reading your record</div>;
  const items = [...record.items].sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
  return (
    <div className="m-recordview">
      <section className="o-record m-record" aria-label="Your record">
        <div className="o-record-head">
          <p className="o-eyebrow">Your record</p>
          <span className="o-record-seal" aria-hidden="true">
            <svg viewBox="0 0 24 24"><path d="M7 3h10v18l-2.5-1.6L12 21l-2.5-1.6L7 21z" /><path d="M9.5 8h5M9.5 11.5h5M9.5 15h3" /></svg>
          </span>
        </div>
        {record.buys === 0 ? (
          <p className="o-record-empty">Your record starts with the first buy PayPal releases on your proof.</p>
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
              {recordFacts(record).map((f) => (
                <li key={f}>
                  <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m3.5 8.5 3 3 6-7" /></svg>
                  {f}
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      {items.length > 0 && (
        <section className="o-section">
          <div className="o-section-head">
            <h2>Every buy</h2>
            <span className="o-muted o-small-text">Newest first, from the shop's books.</span>
          </div>
          <ul className="m-buys">
            {(all ? items : items.slice(0, SHOWN)).map((b) => (
              <li key={`${b.request_id}-${b.at}`} className="m-buy">
                <span className="m-buy-main">
                  <b>{cap(b.what || b.item)}</b>
                  <small>{[b.payee, day(b.at)].filter(Boolean).join(" · ")}</small>
                </span>
                <span className="m-buy-tags">
                  {b.receipt && <span className="o-pill o-tone-leaf">Receipt</span>}
                  {b.paypal && <span className="o-pill o-tone-calm">PayPal</span>}
                  {b.released_by === "owner" && <span className="o-pill o-tone-clay">Released by {owner}</span>}
                  {b.opening && <span className="o-pill o-tone-calm">Opening books</span>}
                </span>
                <b className="m-buy-amount o-num">{usd(b.amount_cents)}</b>
              </li>
            ))}
          </ul>
          {items.length > SHOWN && (
            <button type="button" className="o-textbtn o-more" onClick={() => setAll(!all)}>
              {all ? "Show the latest" : `Show ${items.length - SHOWN} earlier`}
            </button>
          )}
        </section>
      )}
    </div>
  );
}
