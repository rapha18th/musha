import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import type { Message, Shop, SpendRequest } from "../api";
import { phone, type Alert, type Product, type Week } from "../phone/api";
import { todayUtc } from "../phone/util";
import { usePoll, useStored } from "../owner/lib";
import { settledSignal, useRecord } from "../record";
import { Camera } from "./Camera";
import { useChat } from "./chat";
import { Close } from "./Close";
import { AskDialog, CloseDialog, ProofDialog } from "./Flows";
import { Messages } from "./Messages";
import { Record } from "./Record";
import { needsOf, Today, urgent, type Actions } from "./Today";
import "../owner/owner.css";
import "./manager.css";

const TABS = ["today", "messages", "close", "camera", "record"] as const;
type Tab = (typeof TABS)[number];
const TAB_LABEL: Record<Tab, string> = {
  today: "Today", messages: "Messages", close: "Close the day", camera: "Counter camera", record: "Record",
};
// A phone's tab row holds five short words.
const TAB_SHORT: Partial<Record<Tab, string>> = { close: "Close", camera: "Camera" };

type Open =
  | { type: "ask"; item?: string }
  | { type: "proof"; requestId: number; file?: File }
  | { type: "close" }
  | null;

/**
 * The manager's dashboard: one shop, five views. Today leads with what needs her now; Messages is her thread
 * with Musha, the same thread that runs on her WhatsApp once she links it.
 */
export function Manager({ shop, requests, messages, loading, refresh }: {
  shop: Shop;
  requests: SpendRequest[];
  messages: Message[];
  /** The thread's first load is on its way. */
  loading: boolean;
  refresh: () => Promise<void>;
}) {
  const [tab, setTab] = useStored<Tab>("musha.managerTab", "today", TABS);
  const [open, setOpen] = useState<Open>(null);
  const [focus, setFocus] = useState(false);
  const focused = useCallback(() => setFocus(false), []);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [week, setWeek] = useState<Week>();
  const chat = useChat({ shop, requests, messages, loading, refresh });
  const record = useRecord(shop.id, settledSignal(chat.reqs));

  const reloadAlerts = usePoll(async () => {
    setAlerts(await phone.alerts(shop.id));
  }, 15000, [shop.id]);
  const reloadProducts = usePoll(async () => {
    const next = await phone.products(shop.id);
    setProducts((prev) => (JSON.stringify(prev) === JSON.stringify(next) ? prev : next));
  }, 60000, [shop.id]);
  const reloadWeek = usePoll(async () => {
    const next = await phone.week(shop.id);
    setWeek((prev) => (prev && JSON.stringify(prev) === JSON.stringify(next) ? prev : next));
  }, 60000, [shop.id]);

  // A request opened or moved, or a new line arrived: the questions and the shelves read again at once.
  const signal = `${chat.reqs.map((r) => `${r.id}:${r.status}`).join(",")}|${messages.length ? messages[messages.length - 1].id : 0}`;
  const seenSignal = useRef(signal);
  useEffect(() => {
    if (seenSignal.current === signal) return;
    seenSignal.current = signal;
    void reloadAlerts();
    void reloadProducts();
  }, [signal, reloadAlerts, reloadProducts]);

  const needs = useMemo(
    () => needsOf({ reqs: chat.reqs, messages: chat.all, alerts, products, week }),
    [chat.reqs, chat.all, alerts, products, week],
  );
  const last = week?.cash[week.cash.length - 1];
  const closedToday = !!last && last.day === todayUtc() && last.closed;

  // Lines from Musha or the owner since the manager last had Messages open.
  const latest = chat.all.length ? chat.all[chat.all.length - 1].id : 0;
  const [seen, setSeen] = useState<number | null>(null);
  useEffect(() => {
    if (loading) return;
    if (seen === null || tab === "messages") setSeen((s) => (s === latest ? s : latest));
  }, [loading, tab, latest, seen]);
  const unread = seen !== null && tab !== "messages" && chat.all.some((m) => m.sender !== "manager" && m.id > seen);

  const actions: Actions = {
    ask: (item) => setOpen({ type: "ask", item }),
    proof: (req) => setOpen({ type: "proof", requestId: req.id }),
    close: () => setOpen({ type: "close" }),
    answer: (line) => {
      if (line) chat.reply(line);
      setTab("messages");
      setFocus(true);
    },
  };

  const shut = useCallback(() => setOpen(null), []);
  // Proof sent from another view goes into the thread: a word says where Musha's reply lands.
  const sent = () => {
    if (tab !== "messages") chat.say("Sent. Musha's reply lands in Messages.");
  };
  const proofFor = open?.type === "proof" ? chat.reqs.find((r) => r.id === open.requestId) : undefined;

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
  // On a phone the tab row scrolls: the open tab stays in view.
  useEffect(() => {
    const el = tabRefs.current[TABS.indexOf(tab)];
    if (el && typeof el.scrollIntoView === "function") {
      el.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }, [tab]);

  return (
    <div className="o-console m-dash" data-tab={tab}>
      <header className="o-head m-head">
        <div>
          <h1>{shop.name}</h1>
          <p className="o-muted">
            {shop.place} · owned by {shop.owner.name}{shop.owner.city ? ` in ${shop.owner.city}` : ""}
            {shop.whatsapp === "live" ? " · your chat runs on WhatsApp too" : ""}
          </p>
        </div>
        <button type="button" className="o-btn o-primary m-head-ask" onClick={() => actions.ask()} aria-haspopup="dialog">Ask for money</button>
      </header>

      <nav className="o-tabs m-tabs" role="tablist" aria-label="Dashboard">
        {TABS.map((t, i) => (
          <button key={t} ref={(el) => {
            tabRefs.current[i] = el;
          }} role="tab" id={`m-tab-${t}`} aria-controls="m-panel" aria-selected={tab === t} tabIndex={tab === t ? 0 : -1}
            className="o-tab" onClick={() => setTab(t)} onKeyDown={onTabKey}>
            {TAB_SHORT[t] ? <><span className="m-tab-long">{TAB_LABEL[t]}</span><span className="m-tab-short">{TAB_SHORT[t]}</span></> : TAB_LABEL[t]}
            {t === "today" && tab !== "today" && urgent(needs) && <span className="o-tab-dot" aria-label="Something needs you" />}
            {t === "messages" && unread && <span className="o-tab-dot" aria-label="New messages" />}
          </button>
        ))}
      </nav>

      <div className="o-panel" role="tabpanel" id="m-panel" aria-labelledby={`m-tab-${tab}`} key={tab}>
        {tab === "today" && <Today shop={shop} needs={needs} reqs={chat.reqs} week={week} actions={actions} />}
        {tab === "messages" && (
          <Messages shop={shop} chat={chat} focus={focus} focused={focused} closedToday={closedToday}
            onAsk={() => actions.ask()} onClose={actions.close}
            onProof={(req, file) => setOpen({ type: "proof", requestId: req.id, file })} />
        )}
        {tab === "close" && <Close shop={shop} week={week} open={actions.close} />}
        {tab === "camera" && <Camera shop={shop} />}
        {tab === "record" && <Record record={record} owner={shop.owner.name} />}
      </div>

      {chat.note && <p className="m-toast" role="status">{chat.note}</p>}

      {open?.type === "ask" && (
        <AskDialog shop={shop} requests={chat.reqs} item={open.item} close={shut} done={async (made) => {
          setOpen(null);
          chat.absorb({ request: made });
          chat.say(`Sent to ${shop.owner.name}. PayPal holds the money once ${shop.owner.name} approves.`);
          await refresh();
        }} />
      )}
      {open?.type === "proof" && proofFor && (
        <ProofDialog
          req={proofFor}
          ai={chat.ai}
          start={open.file}
          close={shut}
          onFile={(kind, file, printed) => {
            chat.sendFile(proofFor, kind, file, printed);
            sent();
          }}
          onSample={(variant) => {
            chat.sendSample(proofFor, variant);
            sent();
          }}
          onGoods={() => {
            chat.sendGoods(proofFor);
            sent();
          }}
        />
      )}
      {open?.type === "close" && (
        <CloseDialog shop={shop} close={shut} done={async () => {
          await Promise.all([reloadWeek(), reloadAlerts(), reloadProducts(), refresh()]);
        }} />
      )}
    </div>
  );
}
