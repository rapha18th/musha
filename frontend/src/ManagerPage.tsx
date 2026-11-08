import { useEffect, useRef, useState } from "react";
import type { Message, Session } from "./api";
import { Bar } from "./Bar";
import { Manager } from "./manager/Manager";
import { go, setSession } from "./shell";
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
    setUnread(messages.filter((m) => m.sender !== "manager" && m.id > after).length);
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

/** Farai's page: her dashboard for the shop she runs, the whole width of the window, phone first. */
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
    <div className="app app-manager">
      <Bar who={session.person.name} theme={theme} toggleTheme={toggleTheme} />
      <main>
        <Manager shop={shop} requests={requests} messages={messages} loading={!linesIn} refresh={refresh} />
      </main>
    </div>
  );
}
