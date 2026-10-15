import { useCallback, useEffect, useState } from "react";
import { PayPalProvider } from "@paypal/react-paypal-js/sdk-v6";
import { api, type Entry, type Message, type Shop, type StockRequest } from "./api";
import { Owner } from "./Owner";
import { Phone } from "./Phone";

const SHOP_ID = 1;

export default function App() {
  const [shop, setShop] = useState<Shop>();
  const [requests, setRequests] = useState<StockRequest[]>([]);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [clientId, setClientId] = useState<string>();
  const [view, setView] = useState<"owner" | "phone">("owner");

  const refresh = useCallback(async () => {
    const [s, r, l, m] = await Promise.all([
      api.shop(SHOP_ID), api.requests(SHOP_ID), api.ledger(SHOP_ID), api.messages(SHOP_ID),
    ]);
    setShop(s);
    setRequests(r);
    setEntries(l);
    setMessages(m);
  }, []);

  useEffect(() => {
    api.config().then((c) => setClientId(c.paypal_client_id || ""));
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 3000);
    return () => {
      clearInterval(t);
    };
  }, [refresh]);

  if (!shop) {
    return <div className="loading">Musha</div>;
  }

  const owner = <Owner shop={shop} requests={requests} entries={entries} refresh={refresh} paypalReady={!!clientId} />;

  return (
    <div className="app" data-view={view}>
      <header className="top">
        <div className="brand">
          <span className="mark">Musha</span>
          <span className="tag">Run your business at home from anywhere</span>
        </div>
        <nav className="switch" aria-label="View">
          <button aria-pressed={view === "owner"} onClick={() => setView("owner")}>{shop.owner.name}, {shop.owner.city}</button>
          <button aria-pressed={view === "phone"} onClick={() => setView("phone")}>{shop.manager.name}, Mbare</button>
        </nav>
      </header>
      <main className="stage">
        <section className="owner-pane">
          {clientId ? (
            <PayPalProvider clientId={clientId} environment="sandbox" components={["paypal-payments"]} pageType="checkout">
              {owner}
            </PayPalProvider>
          ) : owner}
        </section>
        <aside className="phone-pane">
          <Phone shop={shop} messages={messages} refresh={refresh} />
        </aside>
      </main>
    </div>
  );
}
