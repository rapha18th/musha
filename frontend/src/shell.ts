import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, setShopKey, type Keys, type Shop } from "./api";

export type Route = { name: "landing" | "shop" | "counter" };

function parse(): Route {
  const h = window.location.hash.replace(/^#\/?/, "");
  if (h.startsWith("shop")) return { name: "shop" };
  if (h.startsWith("counter")) return { name: "counter" };
  return { name: "landing" };
}

export function useRoute(): Route {
  const [route, setRoute] = useState(parse);
  useEffect(() => {
    const on = () => {
      setRoute(parse());
    };
    window.addEventListener("hashchange", on);
    return () => {
      window.removeEventListener("hashchange", on);
    };
  }, []);
  return route;
}

export function go(path: string) {
  window.location.hash = path;
}

const SHOP_KEY = "musha.shop";
const THEME_KEY = "musha.theme";

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string | null) {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    // private windows keep the shop for this page only
  }
}

/** One shop creation at a time for the whole page: StrictMode's second effect, a retry and a remount share it. */
let creating: Promise<Shop> | null = null;

const keysKey = (id: number) => `musha.keys.${id}`;
const joinedKey = (id: number) => `musha.joined.${id}`;

/** The keys of a shop this browser made. */
export function keysOf(id: number): Keys | null {
  try {
    const k = JSON.parse(read(keysKey(id)) || "null");
    return k && typeof k.owner === "string" && typeof k.counter === "string" ? (k as Keys) : null;
  } catch {
    return null;
  }
}

/** The counter phone's page for this shop. It carries the counter key, so another device opens it. */
export function counterLink(id: number, absolute = true): string {
  const key = keysOf(id)?.counter;
  const hash = `#/counter?shop=${id}${key ? `&key=${encodeURIComponent(key)}` : ""}`;
  return absolute ? `${window.location.origin}${window.location.pathname}${hash}` : hash;
}

/** The widest key this browser holds for the shop goes with every call, beside the cookie. */
function holdKey(id: number) {
  setShopKey(keysOf(id)?.owner || read(joinedKey(id)) || "");
}

/** A link's key joins this device to its shop once; then the key leaves the address bar. */
async function joinFromLink(id: number, key: string) {
  try {
    await api.join(id, key);
    write(joinedKey(id), key);
  } catch (e) {
    // a key for another shop, or a shop the server has lost, opens nothing; a server that is away tries again
    if (!(e instanceof ApiError && (e.status === 403 || e.status === 404 || e.status === 422))) throw e;
  }
  const [path, query = ""] = window.location.hash.split("?");
  const params = new URLSearchParams(query);
  params.delete("key");
  const rest = params.toString();
  try {
    window.history.replaceState(window.history.state, "", `${window.location.pathname}${window.location.search}${path}${rest ? `?${rest}` : ""}`);
  } catch {
    // the address keeps the key; the shop opens the same
  }
}

/**
 * Every visitor plays a shop of their own: a `?shop=` link, the remembered shop, or a fresh one.
 * The console needs the owner's key; the counter page opens with the counter phone's.
 */
async function resolveShop(need: "owner" | "counter"): Promise<number> {
  const params = new URLSearchParams(window.location.hash.split("?")[1] || "");
  const fromLink = params.get("shop");
  const linkKey = params.get("key");
  if (fromLink && /^\d+$/.test(fromLink) && linkKey) await joinFromLink(Number(fromLink), linkKey);
  const remembered = fromLink || read(SHOP_KEY);
  if (remembered && /^\d+$/.test(remembered)) {
    const id = Number(remembered);
    holdKey(id);
    try {
      const shop = await api.shop(id);
      const owner = !shop.role || shop.role === "owner";
      if (owner || need === "counter") {
        if (owner) write(SHOP_KEY, remembered);
        return id;
      }
    } catch (e) {
      // A shop the server has lost, or one this browser holds no key to, starts over;
      // a server that is away keeps the shop for the next try.
      if (!(e instanceof ApiError && (e.status === 403 || e.status === 404 || e.status === 422))) throw e;
    }
  }
  if (!creating) {
    const made = api.newShop();
    creating = made;
    made.catch(() => {
      if (creating === made) creating = null;
    });
  }
  const shop = await creating;
  if (shop.keys) write(keysKey(shop.id), JSON.stringify(shop.keys));
  write(SHOP_KEY, String(shop.id));
  holdKey(shop.id);
  return shop.id;
}

/**
 * Runs `load` until it answers, waiting 1, 2, 4, 8 and then 15 seconds between tries.
 * `failures` counts the misses so far; `retry` tries again at once.
 */
export function useRetry<T>(load: () => Promise<T>, on = true): { value?: T; failures: number; retry: () => void } {
  const [value, setValue] = useState<T>();
  const [failures, setFailures] = useState(0);
  const [round, setRound] = useState(0);
  const fn = useRef(load);
  fn.current = load;
  const done = value !== undefined;
  useEffect(() => {
    if (!on || done) return;
    let live = true;
    let timer = 0;
    let misses = 0;
    const attempt = () => {
      fn.current().then((v) => {
        if (live) setValue(v);
      }, () => {
        if (!live) return;
        misses += 1;
        setFailures((f) => f + 1);
        timer = window.setTimeout(attempt, Math.min(15000, 1000 * 2 ** (misses - 1)));
      });
    };
    attempt();
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [on, done, round]);
  const retry = useCallback(() => setRound((r) => r + 1), []);
  return { value, failures, retry };
}

export function useShopId(need: "owner" | "counter" = "owner") {
  return useRetry(() => resolveShop(need));
}

export function startOver() {
  write(SHOP_KEY, null);
  creating = null;
}

/** Light for everyone by default; dark is a remembered choice. */
export function useTheme(): [string, () => void] {
  const [theme, setTheme] = useState(() => read(THEME_KEY) || "light");
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);
  const toggle = useCallback(() => {
    setTheme((t) => {
      const next = t === "dark" ? "light" : "dark";
      write(THEME_KEY, next);
      return next;
    });
  }, []);
  return [theme, toggle];
}
