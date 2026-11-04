import { useCallback, useEffect, useRef, useState } from "react";
import { PayPalProvider } from "@paypal/react-paypal-js/sdk-v6";
import { api, type Config, type Entry, type Message, type Shop as ShopData, type SpendRequest } from "./api";
import { Owner } from "./Owner";
import { Phone } from "./Phone";
import { Waiting } from "./Waiting";

/** One shop on one page: Chipo's console on the left, Farai's phone on the right. */
export default function Shop({ shopId, config, theme, toggleTheme }: {
  shopId: number;
  config: Config;
  theme: string;
  toggleTheme: () => void;
}) {
  const [shop, setShop] = useState<ShopData>();
  const [requests, setRequests] = useState<SpendRequest[]>([]);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [linesIn, setLinesIn] = useState(false); // the first thread load has landed
  const [view, setView] = useState<"owner" | "phone">("owner");
  // Data loads that missed in a row: before the first one lands, the waiting view says so.
  const [misses, setMisses] = useState(0);

  // Each load is numbered; a result older than one already shown is dropped, so state never steps back.
  const seq = useRef({ data: 0, dataShown: 0, lines: 0, linesShown: 0 });

  // Messages load on their own: with the model on they wait for translations.
  const loadMessages = useCallback(async () => {
    const n = ++seq.current.lines;
    const m = await api.messages(shopId);
    if (n < seq.current.linesShown) return;
    seq.current.linesShown = n;
    setMessages(m);
    setLinesIn(true);
  }, [shopId]);

  const loadData = useCallback(async () => {
    const n = ++seq.current.data;
    const [s, r, l] = await Promise.all([api.shop(shopId), api.requests(shopId), api.ledger(shopId)]);
    if (n < seq.current.dataShown) return;
    seq.current.dataShown = n;
    setShop(s);
    setRequests(r);
    setEntries(l);
  }, [shopId]);

  const refresh = useCallback(async () => {
    loadMessages().catch(() => {});
    await loadData();
  }, [loadData, loadMessages]);

  // Each poll starts once the last one settles.
  useEffect(() => {
    let live = true;
    const timers: ReturnType<typeof setTimeout>[] = [];
    const every = (load: () => Promise<void>, slot: number) => {
      const go = () => {
        load().catch(() => {}).finally(() => {
          if (live) timers[slot] = setTimeout(go, 3000);
        });
      };
      go();
    };
    every(async () => {
      try {
        await loadData();
        setMisses(0);
      } catch (e) {
        setMisses((m) => m + 1);
        throw e;
      }
    }, 0);
    every(loadMessages, 1);
    return () => {
      live = false;
      timers.forEach(clearTimeout);
    };
  }, [loadData, loadMessages]);

  if (!shop) return <Waiting failures={misses} />;

  const standin = config.paypal_mode === "standin";
  const next = theme === "dark" ? "Light" : "Dark";
  const owner = (
    <Owner shop={shop} requests={requests} entries={entries} refresh={refresh} config={config} />
  );

  return (
    <div className="app" data-view={view}>
      <header className="top">
        <a className="brand" href="#/">
          <span className="mark">Musha</span>
          <span className="tag">Run your business at home from anywhere</span>
        </a>
        <div className="top-right">
          <nav className="switch" aria-label="View">
            <button aria-pressed={view === "owner"} onClick={() => setView("owner")}>{shop.owner.name}, {shop.owner.city}</button>
            <button aria-pressed={view === "phone"} onClick={() => setView("phone")}>{shop.manager.name}, {shop.place.split(",")[0]}</button>
          </nav>
          <button className="theme" onClick={toggleTheme} aria-label={`${next} theme`}>{next}</button>
        </div>
      </header>
      {standin && (
        <p className="rehearsal">Rehearsal. A stand-in answers for PayPal. Add sandbox keys to move sandbox money.</p>
      )}
      <main className="stage">
        <section className="owner-pane">
          {config.paypal_client_id && !standin ? (
            <PayPalProvider clientId={config.paypal_client_id} environment="sandbox" components={["paypal-payments"]} pageType="checkout">
              {owner}
            </PayPalProvider>
          ) : owner}
        </section>
        <aside className="phone-pane">
          <Phone shop={shop} requests={requests} messages={messages} loading={!linesIn} refresh={refresh} />
        </aside>
      </main>
    </div>
  );
}
