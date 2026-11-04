import { useEffect, useRef, useState } from "react";
import type { Message, Shop } from "../api";
import { ownerApi } from "./api";
import { clock, usePoll } from "./lib";

type Pending = { id: string; text: string; error?: string };

/** The owner's thread with Musha: answers come from the books. A model call can take half a minute;
 *  the question shows at once and the composer stays open. */
export function AskMusha({ shop, suggestions }: { shop: Shop; suggestions: string[] }) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [pending, setPending] = useState<Pending[]>([]);
  const [text, setText] = useState("");
  const box = useRef<HTMLDivElement>(null);

  const load = usePoll(async () => {
    setMessages(await ownerApi.ownerMessages(shop.id));
  }, 5000, [shop.id]);

  useEffect(() => {
    const el = box.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
    }
  }, [messages.length, pending.length]);

  const send = async (q: string) => {
    const question = q.trim();
    if (!question) return;
    const id = crypto.randomUUID();
    setPending((p) => [...p, { id, text: question }]);
    setText("");
    try {
      await ownerApi.ask(shop.id, question);
      setPending((p) => p.filter((x) => x.id !== id));
      load();
    } catch (e) {
      setPending((p) => p.map((x) => (x.id === id ? { ...x, error: e instanceof Error ? e.message : String(e) } : x)));
    }
  };

  const thinking = pending.some((p) => !p.error);
  const asked = new Set(messages.filter((m) => m.sender === "owner").map((m) => m.text.trim()));
  const chips = suggestions.filter((s) => !asked.has(s)).slice(0, 3);

  return (
    <section className="o-card o-ask" aria-label="Ask Musha">
      <div className="o-ask-thread" ref={box} aria-live="polite">
        {messages.map((m) => (
          <div key={m.id} className={`o-bubble o-from-${m.sender === "owner" ? "me" : "musha"}`}>
            <p>{m.text}</p>
            <time>{clock(m.at)}</time>
          </div>
        ))}
        {pending.map((p) => (
          <div key={p.id} className="o-bubble o-from-me o-pending">
            <p>{p.text}</p>
            {p.error ? (
              <button className="o-textbtn o-danger" onClick={() => {
                setPending((all) => all.filter((x) => x.id !== p.id));
                send(p.text);
              }}>Send again</button>
            ) : <time>Sent</time>}
          </div>
        ))}
        {thinking && (
          <div className="o-bubble o-from-musha o-typing" aria-label="Musha is reading the books">
            <span /><span /><span />
          </div>
        )}
      </div>
      {chips.length > 0 && (
        <div className="o-chips">
          {chips.map((c) => <button key={c} className="o-chip" onClick={() => send(c)}>{c}</button>)}
        </div>
      )}
      <form className="o-ask-form" onSubmit={(e) => {
        e.preventDefault();
        send(text);
      }}>
        <input value={text} onChange={(e) => setText(e.target.value)} maxLength={500}
          placeholder={`Ask about ${shop.name}`} aria-label="Ask Musha" />
        <button className="o-btn o-primary" type="submit" disabled={!text.trim()}>Ask</button>
      </form>
    </section>
  );
}
