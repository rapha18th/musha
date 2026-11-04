import { lazy, Suspense, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import type { Config, Entry, Shop, SpendRequest } from "./api";
import { ownerApi, type Alert, type OwnerRequest, type Week as WeekData } from "./owner/api";
import { Today } from "./owner/Today";
import { CallShop } from "./owner/Call";
import { Spinner } from "./owner/parts";
import { canApprove, openItems, usePoll, useStored, when } from "./owner/lib";
import "./owner/owner.css";

// Week and Books carry AG Grid, Money AG Grid and AG Charts Enterprise, and Agent the mandate; each loads the
// first time its tab opens.
const loadWeek = () => import("./owner/Week");
const loadBooks = () => import("./owner/Books");
const loadMoney = () => import("./owner/Money");
const loadAgent = () => import("./owner/Agent");
const Week = lazy(() => loadWeek().then((m) => ({ default: m.Week })));
const Books = lazy(() => loadBooks().then((m) => ({ default: m.Books })));
const Money = lazy(() => loadMoney().then((m) => ({ default: m.Money })));
const Agent = lazy(() => loadAgent().then((m) => ({ default: m.Agent })));

const TABS = ["today", "week", "books", "money", "agent"] as const;
type Tab = (typeof TABS)[number];
const TAB_LABEL: Record<Tab, string> = { today: "Today", week: "Week", books: "Books", money: "Money", agent: "Agent" };

type Props = {
  shop: Shop;
  requests: SpendRequest[];
  entries: Entry[];
  refresh: () => Promise<void>;
  config: Config;
};

/** Chipo's console: one shop, five views, one focal point in each. */
export function Owner({ shop, requests, entries, refresh, config }: Props) {
  const [tab, setTab] = useStored<Tab>("musha.ownerTab", "today", TABS);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [week, setWeek] = useState<WeekData>();
  const sandbox = config.paypal_mode === "sandbox" && !!config.paypal_client_id;
  const reqs = requests as OwnerRequest[];

  const reloadAlerts = usePoll(async () => {
    setAlerts(await ownerApi.alerts(shop.id));
  }, 6000, [shop.id]);
  // The week keeps pace with alerts while its tab is open, and idles slower elsewhere.
  const reloadWeek = usePoll(async () => {
    const next = await ownerApi.week(shop.id);
    setWeek((prev) => (prev && JSON.stringify(prev) === JSON.stringify(next) ? prev : next));
  }, tab === "week" ? 6000 : 30000, [shop.id]);

  // A closed day, a new gap or a new ledger line changes the week's figures: read them again at once.
  const alertKeys = alerts.map((a) => a.id).join(",");
  const lastEntry = entries.length ? entries[entries.length - 1].id : 0;
  const signal = `${alertKeys}|${entries.length}:${lastEntry}`;
  const seen = useRef(signal);
  useEffect(() => {
    if (seen.current === signal) return;
    seen.current = signal;
    reloadWeek();
  }, [signal, reloadWeek]);

  // A request opened or moved, from the phone or the console: alerts that answer to it read again at once.
  const requestSignal = reqs.map((r) => `${r.id}:${r.status}`).join(",");
  const seenRequests = useRef(requestSignal);
  useEffect(() => {
    if (seenRequests.current === requestSignal) return;
    seenRequests.current = requestSignal;
    reloadAlerts();
  }, [requestSignal, reloadAlerts]);

  // The tabs that wait behind Today load once the console is up, so a first click opens them at once.
  // A visitor who asked to save data fetches each tab on its first open.
  useEffect(() => {
    if ((navigator as Navigator & { connection?: { saveData?: boolean } }).connection?.saveData) return;
    const t = window.setTimeout(() => {
      Promise.all([loadWeek(), loadBooks(), loadAgent()]).catch(() => {});
    }, 4000);
    return () => {
      window.clearTimeout(t);
    };
  }, []);

  const needs = useMemo(() => {
    const open = openItems(reqs);
    return reqs.some((r) => r.status === "flagged" || canApprove(r))
      || alerts.some((a) => (a.kind === "low_stock" && !open.has(String(a.data.item ?? "").trim().toLowerCase()))
        || a.kind === "cash_gap" || a.kind === "stock_gap");
  }, [reqs, alerts]);

  const refreshAll = async () => {
    await Promise.all([refresh(), reloadAlerts(), reloadWeek()]);
  };

  // Arrow keys move along the tabs and open the one they land on; Home and End jump to the ends.
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const onTabKey = (e: KeyboardEvent<HTMLButtonElement>) => {
    const i = TABS.indexOf(tab);
    const to = e.key === "ArrowRight" ? (i + 1) % TABS.length
      : e.key === "ArrowLeft" ? (i - 1 + TABS.length) % TABS.length
        : e.key === "Home" ? 0 : e.key === "End" ? TABS.length - 1 : -1;
    if (to < 0) return;
    e.preventDefault();
    setTab(TABS[to]);
    tabRefs.current[to]?.focus();
  };

  return (
    <div className="owner o-console">
      <header className="o-head">
        <div>
          <h1>{shop.name}</h1>
          <p className="o-muted">
            {shop.place} · run by {shop.manager.name}
            {shop.camera_seen_at ? ` · counter camera seen ${when(shop.camera_seen_at)}` : ""}
          </p>
        </div>
        <CallShop shop={shop} />
      </header>

      <nav className="o-tabs" role="tablist" aria-label="Console">
        {TABS.map((t, i) => (
          <button key={t} ref={(el) => {
            tabRefs.current[i] = el;
          }} role="tab" id={`o-tab-${t}`} aria-controls="o-panel" aria-selected={tab === t} tabIndex={tab === t ? 0 : -1}
            className="o-tab" onClick={() => setTab(t)} onKeyDown={onTabKey}>
            {TAB_LABEL[t]}
            {t === "today" && needs && tab !== "today" && <span className="o-tab-dot" aria-label="Something needs you" />}
          </button>
        ))}
      </nav>

      <div className="o-panel" role="tabpanel" id="o-panel" aria-labelledby={`o-tab-${tab}`} key={tab}>
        {tab === "today" && (
          <Today shop={shop} requests={reqs} alerts={alerts} facts={week?.facts} sandbox={sandbox}
            refresh={refreshAll} reloadAlerts={reloadAlerts} />
        )}
        <Suspense fallback={<div className="o-loading"><Spinner /> Opening {TAB_LABEL[tab]}</div>}>
          {tab === "week" && (
            <Week shop={shop} week={week} alerts={alerts} entries={entries} refresh={refreshAll}
              reloadAlerts={reloadAlerts} reloadWeek={reloadWeek} />
          )}
          {tab === "books" && <Books shop={shop} entries={entries} requests={reqs} />}
          {tab === "money" && <Money shop={shop} entries={entries} week={week} config={config} />}
          {tab === "agent" && <Agent shop={shop} sandbox={sandbox} onChange={refreshAll} />}
        </Suspense>
      </div>
    </div>
  );
}
