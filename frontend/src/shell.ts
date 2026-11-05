import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, auth, call, setShopKey, type Session } from "./api";

export type Route =
  | { name: "landing" | "sign-in" | "sign-up" | "owner" | "manager" | "counter" }
  | { name: "invite"; token: string };

const BASE = import.meta.env.BASE_URL || "/";
const MOVED = "musha:route";

/** The path under the app's base: "/owner" from "/owner", whatever the base. */
function local(): string {
  const p = window.location.pathname;
  return "/" + (p.startsWith(BASE) ? p.slice(BASE.length) : p.replace(/^\//, ""));
}

/** Hash routes from the first builds open their path: "#/shop" is the console, "#/invite/x" an invite. */
function fromHash(h: string): string | null {
  if (h.startsWith("shop")) return "/owner";
  if (/^(owner|manager|sign-in|sign-up|invite\/)/.test(h)) return `/${h.split("?")[0]}`;
  return null;
}

function parse(): Route {
  const h = window.location.hash.replace(/^#\/?/, "");
  // The counter link stays a hash link: a counter phone and the bridge read it as it always was.
  if (h.startsWith("counter")) return { name: "counter" };
  const path = local().replace(/\/+$/, "") || "/";
  const invite = /^\/invite\/([^/]+)$/.exec(path);
  if (invite) return { name: "invite", token: decodeURIComponent(invite[1]) };
  switch (path) {
    case "/sign-in": return { name: "sign-in" };
    case "/sign-up": return { name: "sign-up" };
    case "/owner": return { name: "owner" };
    case "/manager": return { name: "manager" };
    case "/counter": return { name: "counter" };
    default: return { name: "landing" };
  }
}

/** Move to a path in the app. `replace` keeps the step out of the back button. */
export function go(path: string, replace = false) {
  const url = `${BASE.replace(/\/$/, "")}${path}`;
  try {
    if (replace) window.history.replaceState(null, "", url);
    else window.history.pushState(null, "", url);
  } catch {
    window.location.assign(url);
    return;
  }
  window.dispatchEvent(new Event(MOVED));
  window.scrollTo(0, 0);
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => {
    const old = fromHash(window.location.hash.replace(/^#\/?/, ""));
    if (old) go(old, true);
    return parse();
  });
  useEffect(() => {
    const on = () => {
      setRoute(parse());
    };
    window.addEventListener("popstate", on);
    window.addEventListener("hashchange", on);
    window.addEventListener(MOVED, on);
    return () => {
      window.removeEventListener("popstate", on);
      window.removeEventListener("hashchange", on);
      window.removeEventListener(MOVED, on);
    };
  }, []);
  return route;
}

/** Each person's own page. */
export const home = (s: Session) => (s.person.role === "owner" ? "/owner" : "/manager");

// The session, known once per page: undefined until the server answers, null for nobody signed in.
let known: Session | null | undefined;
let asking: Promise<Session | null> | null = null;
const watchers = new Set<() => void>();

export function setSession(s: Session | null) {
  known = s;
  watchers.forEach((w) => w());
}

/** Who is signed in. A server that is away is asked again; a 401 or 403 means nobody. */
function whoIsIn(): Promise<Session | null> {
  if (known !== undefined) return Promise.resolve(known);
  if (!asking) {
    const made = auth.me().then((s) => s, (e) => {
      if (e instanceof ApiError && (e.status === 401 || e.status === 403)) return null;
      throw e;
    });
    asking = made;
    made.then((s) => {
      setSession(s);
    }, () => undefined).finally(() => {
      if (asking === made) asking = null;
    });
  }
  return asking;
}

/** The session for this page, asked for until the server answers. */
export function useSession(): { session: Session | null | undefined; failures: number; retry: () => void } {
  const [, bump] = useState(0);
  useEffect(() => {
    const w = () => bump((n) => n + 1);
    watchers.add(w);
    return () => {
      watchers.delete(w);
    };
  }, []);
  const asked = useRetry(whoIsIn, known === undefined);
  return { session: known, failures: asked.failures, retry: asked.retry };
}

/** Sign out here and on the server; the landing follows. The page forgets the shop's counter key with it. */
export async function signOut() {
  try {
    await auth.signOut();
  } catch {
    // the cookie may already be gone; the page signs out the same
  }
  counterKeys.clear();
  setSession(null);
  go("/", true);
}

/** A dashboard's data call answered 401 or 403: the session ended elsewhere. */
export function lostSession(e: unknown): boolean {
  return e instanceof ApiError && (e.status === 401 || e.status === 403);
}

const SHOP_KEY = "musha.counter.shop";
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

const joinedKey = (id: number) => `musha.joined.${id}`;

/** The counter page's link, carrying the shop's counter key, so a phone at the counter opens it without a sign-in. */
export function counterUrl(id: number, key: string): string {
  const hash = `#/counter?shop=${id}${key ? `&key=${encodeURIComponent(key)}` : ""}`;
  return `${window.location.origin}${BASE}${hash}`;
}

// Counter keys by shop, from the owner's Connect view. A Disconnect gives the shop a new one.
const counterKeys = new Map<number, string>();
const keyWatchers = new Set<() => void>();

export function setCounterKey(id: number, key: string) {
  if (!key || counterKeys.get(id) === key) return;
  counterKeys.set(id, key);
  keyWatchers.forEach((w) => w());
}

/**
 * The counter page's link for this shop. The owner's sign-in reads the counter key; a sign-in that
 * cannot read it gets the page's own address, which opens on a device signed in to the shop.
 */
export function useCounterLink(id: number, ask = true): string {
  const [, bump] = useState(0);
  useEffect(() => {
    const w = () => bump((n) => n + 1);
    keyWatchers.add(w);
    if (ask && !counterKeys.has(id)) {
      call<{ counter_key?: string }>(`/api/shops/${id}/camera/connect`).then((v) => {
        if (v.counter_key) setCounterKey(id, v.counter_key);
      }, () => undefined);
    }
    return () => {
      keyWatchers.delete(w);
    };
  }, [id, ask]);
  return counterUrl(id, counterKeys.get(id) || "");
}

/** Whether this page holds the shop's counter key, so its counter link opens on any phone. */
export const hasCounterKey = (id: number) => counterKeys.has(id);

/** The joined counter key goes with every call from the counter page, beside the cookie. */
function holdKey(id: number) {
  setShopKey(read(joinedKey(id)) || "");
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
 * The counter page's shop: the counter link's, the one this phone used last, or the signed-in person's.
 * Null when this device holds none of them: the page asks for the counter link.
 */
async function resolveCounter(): Promise<number | null> {
  const params = new URLSearchParams(window.location.hash.split("?")[1] || window.location.search);
  const fromLink = params.get("shop");
  const linkKey = params.get("key");
  if (fromLink && /^\d+$/.test(fromLink) && linkKey) await joinFromLink(Number(fromLink), linkKey);
  let remembered = fromLink && /^\d+$/.test(fromLink) ? fromLink : read(SHOP_KEY);
  if (!remembered || !/^\d+$/.test(remembered)) {
    const s = await whoIsIn();
    if (!s) return null;
    remembered = String(s.shop.id);
  }
  const id = Number(remembered);
  holdKey(id);
  try {
    await api.shop(id);
    write(SHOP_KEY, remembered);
    return id;
  } catch (e) {
    // A server that is away keeps the shop for the next try.
    if (!(e instanceof ApiError && (e.status === 401 || e.status === 403 || e.status === 404 || e.status === 422))) throw e;
    // A counter key the owner replaced keeps its shop: the counter page says the link changed. A link with
    // no key, on a phone that never joined, asks for the counter link.
    if (e.status === 403 && (linkKey || read(joinedKey(id)))) return id;
    write(SHOP_KEY, null);
    return null;
  }
}

/**
 * Runs `load` until it answers, waiting 1, 2, 4, 8 and then 15 seconds between tries.
 * `failures` counts the misses so far; `retry` tries again at once.
 */
export function useRetry<T>(load: () => Promise<T>, on = true): { value?: T; failures: number; retry: () => void } {
  const [value, setValue] = useState<T>();
  const [failures, setFailures] = useState(0);
  const [round, setRound] = useState(0);
  const [settled, setSettled] = useState(false);
  const fn = useRef(load);
  fn.current = load;
  useEffect(() => {
    if (!on || settled) return;
    let live = true;
    let timer = 0;
    let misses = 0;
    const attempt = () => {
      fn.current().then((v) => {
        if (!live) return;
        setValue(v);
        setSettled(true);
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
  }, [on, settled, round]);
  const retry = useCallback(() => setRound((r) => r + 1), []);
  return { value, failures, retry };
}

/** The counter page's shop id, or null when the page needs the counter link. */
export function useCounterShop() {
  return useRetry(resolveCounter);
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
