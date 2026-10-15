import { useMemo, useState } from "react";
import { PayPalOneTimePaymentButton } from "@paypal/react-paypal-js/sdk-v6";
import { AgGridReact } from "ag-grid-react";
import { AllCommunityModule, ModuleRegistry, themeQuartz, type ColDef } from "ag-grid-community";
import { api, usd, type Entry, type Shop, type StockRequest } from "./api";

ModuleRegistry.registerModules([AllCommunityModule]);

const gridTheme = themeQuartz.withParams({
  fontFamily: "Inter, system-ui, sans-serif",
  fontSize: 13,
  foregroundColor: "#211d18",
  backgroundColor: "#fffdf9",
  headerBackgroundColor: "#f4ece0",
  borderColor: "#e8ddcd",
  accentColor: "#b4532a",
  rowHoverColor: "#faf3e8",
  wrapperBorderRadius: 14,
});

const KIND: Record<string, string> = {
  funds_held: "Held",
  funds_captured: "Released to shop",
  funds_released: "Returned to you",
  stock_in: "Stock in",
  sale: "Sales",
  wages: "Wages",
  expense: "Expense",
  banked: "Banked",
};

const STATUS: Record<string, string> = {
  requested: "Waiting for you",
  approving: "Approving",
  held: "Held, waiting for proof",
  captured: "Done",
  part_captured: "Done, change returned",
  voided: "Closed, money returned",
};

type Props = {
  shop: Shop;
  requests: StockRequest[];
  entries: Entry[];
  refresh: () => Promise<void>;
  paypalReady: boolean;
};

export function Owner({ shop, requests, entries, refresh, paypalReady }: Props) {
  const b = shop.books;
  return (
    <div className="owner">
      <div className="shop-head">
        <h1>{shop.name}</h1>
        <p>{shop.place} · run by {shop.manager.name}</p>
      </div>

      <div className="tiles">
        <Tile label="Held for stock" value={usd(b.held_cents)} />
        <Tile label="Released to the shop" value={usd(b.funded_cents)} />
        <Tile label="Returned to you" value={usd(b.returned_cents)} />
        <Tile label="Eggs on hand" value={`${b.stock.eggs ?? 0} trays`} />
      </div>

      <h2>Requests</h2>
      {requests.length === 0 && <p className="quiet">{shop.manager.name}'s stock requests appear here.</p>}
      <div className="cards">
        {requests.map((r) => (
          <RequestCard key={r.id} req={r} refresh={refresh} paypalReady={paypalReady} owner={shop.owner.name} />
        ))}
      </div>

      <h2>Books</h2>
      <Ledger entries={entries} />

      <Wages shop={shop} refresh={refresh} />
    </div>
  );
}

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <div className="tile">
      <span className="tile-value">{value}</span>
      <span className="tile-label">{label}</span>
    </div>
  );
}

function RequestCard({ req, refresh, paypalReady, owner }: { req: StockRequest; refresh: () => Promise<void>; paypalReady: boolean; owner: string }) {
  const [error, setError] = useState("");
  const hold = req.hold;
  return (
    <article className={`card status-${req.status}`}>
      <div className="card-top">
        <div>
          <h3>{req.quantity} {req.unit} of {req.item}</h3>
          <p className="meta">{usd(req.amount_cents)}{req.supplier ? ` · ${req.supplier}` : ""}</p>
        </div>
        <span className="pill">{STATUS[req.status] ?? req.status}</span>
      </div>
      {req.words && <blockquote>“{req.words}”</blockquote>}

      {(req.status === "requested" || req.status === "approving") && (
        paypalReady ? (
          <div className="approve">
            <PayPalOneTimePaymentButton
              createOrder={async () => {
                const { order_id } = await api.order(req.id);
                return { orderId: order_id };
              }}
              onApprove={async () => {
                await api.authorize(req.id);
                await refresh();
              }}
              onError={(e: unknown) => {
                setError(String(e));
              }}
            />
            <p className="fine">PayPal holds the money. {owner} pays only what the receipt proves.</p>
          </div>
        ) : (
          <p className="fine">Add a PayPal sandbox client ID to approve with PayPal.</p>
        )
      )}

      {hold && hold.status !== "held" && (
        <div className="split">
          <div className="bar">
            <span className="bar-in" style={{ width: `${(hold.captured_cents / hold.amount_cents) * 100}%` }} />
          </div>
          <p className="meta">
            {usd(hold.captured_cents)} released · {usd(hold.amount_cents - hold.captured_cents)} returned
          </p>
        </div>
      )}

      {hold && hold.status === "held" && <Settle req={req} refresh={refresh} onError={setError} />}
      {error && <p className="error">{error}</p>}
    </article>
  );
}

// Stands in for the proof engine until receipts are read automatically.
function Settle({ req, refresh, onError }: { req: StockRequest; refresh: () => Promise<void>; onError: (e: string) => void }) {
  const [spent, setSpent] = useState((req.amount_cents / 100).toFixed(2));
  const [busy, setBusy] = useState(false);
  const go = async (amount: string, reason: string) => {
    setBusy(true);
    try {
      await api.settle(req.id, amount, reason);
      await refresh();
    } catch (e) {
      onError(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="settle">
      <label>
        Receipt total
        <input value={spent} inputMode="decimal" onChange={(e) => setSpent(e.target.value)} />
      </label>
      <button disabled={busy} onClick={() => go(spent, "receipt checked by hand")}>Release</button>
      <button className="ghost" disabled={busy} onClick={() => go("0", "closed by owner")}>Return all</button>
    </div>
  );
}

function Ledger({ entries }: { entries: Entry[] }) {
  const columns = useMemo<ColDef<Entry>[]>(() => [
    { field: "at", headerName: "When", width: 130, valueFormatter: (p) => new Date(p.value).toLocaleDateString("en-GB", { day: "numeric", month: "short" }) },
    { field: "kind", headerName: "Line", width: 160, valueFormatter: (p) => KIND[p.value] ?? p.value },
    { field: "memo", headerName: "Detail", flex: 1, minWidth: 180 },
    { field: "quantity", headerName: "Qty", width: 80, valueFormatter: (p) => (p.value ? String(p.value) : "") },
    { field: "amount_cents", headerName: "Amount", width: 120, type: "rightAligned", valueFormatter: (p) => usd(p.value) },
    { field: "source", headerName: "Posted by", width: 110 },
  ], []);
  const rows = useMemo(() => [...entries].reverse(), [entries]);
  return (
    <div className="grid">
      <AgGridReact<Entry> theme={gridTheme} rowData={rows} columnDefs={columns} domLayout="autoHeight"
        pagination paginationPageSize={10} paginationPageSizeSelector={false} />
    </div>
  );
}

function Wages({ shop, refresh }: { shop: Shop; refresh: () => Promise<void> }) {
  const [amount, setAmount] = useState("60.00");
  const [state, setState] = useState("");
  return (
    <div className="wages">
      <h2>Wages</h2>
      <div className="settle">
        <label>
          {shop.manager.name}'s wages
          <input value={amount} inputMode="decimal" onChange={(e) => setAmount(e.target.value)} />
        </label>
        <button onClick={async () => {
          setState("Sending");
          try {
            await api.wages(shop.id, amount, `Wages for ${shop.manager.name}`);
            setState("Sent by PayPal Payouts");
            await refresh();
          } catch (e) {
            setState(String(e));
          }
        }}>Pay {shop.manager.name}</button>
        {state && <span className="fine">{state}</span>}
      </div>
    </div>
  );
}
