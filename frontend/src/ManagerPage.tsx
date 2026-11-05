import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, type LinkCode, type Message, type Session, type Shop } from "./api";
import { Link, ThemeButton } from "./Bar";
import { Phone } from "./Phone";
import { go, setSession, signOut } from "./shell";
import { useShop } from "./useShop";
import { Waiting } from "./Waiting";

const TITLE = "Musha";

/**
 * Musha's new lines while the tab is out of view, counted in the tab's title until the manager looks.
 * The owner approves from her own device, so the title is how the word reaches a tab in the background.
 */
function useUnread(messages: Message[], loaded: boolean) {
  const [unread, setUnread] = useState(0);
  // The newest line the manager has had in view; null until the thread's first load lands.
  const seen = useRef<number | null>(null);
  const latest = useRef(0);
  latest.current = messages.length ? messages[messages.length - 1].id : 0;

  useEffect(() => {
    if (!loaded) return;
    if (seen.current === null || !document.hidden) {
      seen.current = latest.current;
      setUnread(0);
      return;
    }
    const after = seen.current;
    setUnread(messages.filter((m) => m.sender === "musha" && m.id > after).length);
  }, [messages, loaded]);

  useEffect(() => {
    const back = () => {
      if (document.hidden || seen.current === null) return;
      seen.current = latest.current;
      setUnread(0);
    };
    document.addEventListener("visibilitychange", back);
    return () => {
      document.removeEventListener("visibilitychange", back);
    };
  }, []);

  useEffect(() => {
    document.title = unread ? `(${unread}) ${TITLE}` : TITLE;
  }, [unread]);
  useEffect(() => () => {
    document.title = TITLE;
  }, []);
}

/**
 * The manager proves her WhatsApp number: Musha gives a code, and she sends it from that number. The shop's
 * next poll says the thread is live, and this card goes.
 */
function LinkWhatsApp({ shop }: { shop: Shop }) {
  const [code, setCode] = useState<LinkCode>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // Another number, while the manager types it; null while she keeps the one the owner gave.
  const [other, setOther] = useState<string | null>(null);

  const ask = async (phone = "") => {
    setBusy(true);
    setError("");
    try {
      setCode(await api.linkWhatsApp(shop.id, phone));
      setOther(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    setBusy(false);
  };
  const change = (e: FormEvent) => {
    e.preventDefault();
    if (other?.trim()) void ask(other);
  };

  return (
    <section className="mg-wa" aria-labelledby="mg-wa-title">
      <h2 id="mg-wa-title">Link your WhatsApp</h2>
      {code ? (
        <>
          <p>From <strong>{code.phone}</strong>, send this code to Musha{code.to ? <> on <strong>{code.to}</strong></> : null}.</p>
          <p className="mg-wa-code" aria-live="polite">{code.code}</p>
          {code.url && <a className="mg-wa-go" href={code.url} target="_blank" rel="noreferrer">Open WhatsApp</a>}
          <p className="mg-wa-fine">
            The code lasts {code.minutes} minutes.{" "}
            <button type="button" className="mg-wa-quiet" onClick={() => void ask()} disabled={busy}>New code</button>
          </p>
        </>
      ) : (
        <>
          <p>Musha gives you a code. Send it from your WhatsApp, and this chat runs there too.</p>
          <button type="button" className="mg-wa-go" onClick={() => void ask()} disabled={busy}>
            {busy ? "Getting a code" : "Get a code"}
          </button>
        </>
      )}
      {other === null ? (
        <p className="mg-wa-fine">
          <button type="button" className="mg-wa-quiet" onClick={() => setOther("")}>Use another number</button>
        </p>
      ) : (
        <form className="mg-wa-row" onSubmit={change}>
          <input type="tel" inputMode="tel" autoComplete="tel" aria-label="Your WhatsApp number" placeholder="+263 77 123 4567"
            value={other} onChange={(e) => setOther(e.target.value)} />
          <button type="submit" disabled={busy || !other.trim()}>Use it</button>
        </form>
      )}
      {error && <p className="mg-wa-error" role="alert">{error}</p>}
    </section>
  );
}

/** Farai's page: her chat with Musha, the requests and proofs inside it, and the counter camera. Phone first. */
export default function ManagerPage({ session, theme, toggleTheme }: {
  session: Session;
  theme: string;
  toggleTheme: () => void;
}) {
  const { shop, requests, messages, linesIn, misses, lost, refresh } = useShop(session.shop.id, { thread: true });
  useUnread(messages, linesIn);

  useEffect(() => {
    if (!lost) return;
    setSession(null);
    go("/sign-in", true);
  }, [lost]);

  if (!shop) return <Waiting failures={misses} />;

  return (
    <div className="mg">
      <header className="mg-top">
        <Link className="mg-brand" to="/">Musha</Link>
        <span className="mg-shop" title={`${shop.name}, ${shop.place}`}>{shop.name}</span>
        <div className="mg-actions">
          <ThemeButton theme={theme} toggleTheme={toggleTheme} />
          <button type="button" className="theme" onClick={() => void signOut()}>Sign out</button>
        </div>
      </header>
      <main className="mg-main">
        <h1 className="mg-title">{session.person.name}'s chat with Musha</h1>
        <Phone shop={shop} requests={requests} messages={messages} loading={!linesIn} refresh={refresh} />
        {shop.whatsapp === "link" && <LinkWhatsApp shop={shop} />}
      </main>
    </div>
  );
}
