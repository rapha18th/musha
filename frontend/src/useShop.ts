import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Entry, type Message, type Shop, type SpendRequest } from "./api";
import { lostSession } from "./shell";

export type ShopData = {
  shop?: Shop;
  requests: SpendRequest[];
  entries: Entry[];
  messages: Message[];
  /** The first thread load has landed. */
  linesIn: boolean;
  /** Data loads that missed in a row: before the first one lands, the waiting view says so. */
  misses: number;
  /** The server turned the session away: the page goes back to sign in. */
  lost: boolean;
  refresh: () => Promise<void>;
};

/**
 * One shop's data, polled: each poll starts once the last one settles. The console reads the books;
 * the manager's page reads the thread.
 */
export function useShop(shopId: number, { books = false, thread = false }: { books?: boolean; thread?: boolean }): ShopData {
  const [shop, setShop] = useState<Shop>();
  const [requests, setRequests] = useState<SpendRequest[]>([]);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [linesIn, setLinesIn] = useState(false);
  const [misses, setMisses] = useState(0);
  const [lost, setLost] = useState(false);

  // Each load is numbered; a result older than one already shown is dropped, so state never steps back.
  const seq = useRef({ data: 0, dataShown: 0, lines: 0, linesShown: 0 });

  // Messages load on their own: with the model on they wait for translations.
  const loadMessages = useCallback(async () => {
    if (!thread) return;
    const n = ++seq.current.lines;
    const m = await api.messages(shopId);
    if (n < seq.current.linesShown) return;
    seq.current.linesShown = n;
    setMessages(m);
    setLinesIn(true);
  }, [shopId, thread]);

  const loadData = useCallback(async () => {
    const n = ++seq.current.data;
    const [s, r, l] = await Promise.all([
      api.shop(shopId), api.requests(shopId), books ? api.ledger(shopId) : Promise.resolve<Entry[]>([]),
    ]);
    if (n < seq.current.dataShown) return;
    seq.current.dataShown = n;
    setShop(s);
    setRequests(r);
    setEntries(l);
  }, [shopId, books]);

  const refresh = useCallback(async () => {
    loadMessages().catch(() => {});
    await loadData();
  }, [loadData, loadMessages]);

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
        if (lostSession(e)) {
          live = false;
          setLost(true);
        }
        setMisses((m) => m + 1);
        throw e;
      }
    }, 0);
    if (thread) every(loadMessages, 1);
    return () => {
      live = false;
      timers.forEach(clearTimeout);
    };
  }, [loadData, loadMessages, thread]);

  return { shop, requests, entries, messages, linesIn, misses, lost, refresh };
}
