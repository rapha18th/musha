import { useEffect, useRef, useState } from "react";
import { api, type Message, type Shop } from "./api";

// The manager's WhatsApp, simulated so anyone can play Farai.
export function Phone({ shop, messages, refresh }: { shop: Shop; messages: Message[]; refresh: () => Promise<void> }) {
  const [text, setText] = useState("");
  const [asking, setAsking] = useState(false);
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [messages.length]);

  return (
    <div className="phone">
      <div className="phone-head">
        <span className="avatar">M</span>
        <div>
          <strong>Musha</strong>
          <span>{shop.name}</span>
        </div>
      </div>
      <div className="thread">
        {messages.map((m) => (
          <div key={m.id} className={`bubble from-${m.sender}`}>
            {m.text}
            <time>{new Date(m.at).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}</time>
          </div>
        ))}
        <div ref={end} />
      </div>
      {asking ? (
        <StockSheet shopId={shop.id} owner={shop.owner.name} done={async () => { setAsking(false); await refresh(); }} />
      ) : (
        <form className="composer" onSubmit={async (e) => {
          e.preventDefault();
          if (!text.trim()) return;
          await api.say(shop.id, text.trim());
          setText("");
          await refresh();
        }}>
          <button type="button" className="chip" onClick={() => setAsking(true)}>Ask for stock</button>
          <input value={text} placeholder="Message" onChange={(e) => setText(e.target.value)} />
          <button type="submit" aria-label="Send">➤</button>
        </form>
      )}
    </div>
  );
}

function StockSheet({ shopId, owner, done }: { shopId: number; owner: string; done: () => Promise<void> }) {
  const [f, setF] = useState({
    item: "eggs", quantity: "40", unit: "trays", amount: "180.00", supplier: "Mbare wholesale",
    words: "Tinoda matray makumi mana emazai, madhora zana nemakumi masere.",
  });
  const [error, setError] = useState("");
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) => setF({ ...f, [k]: e.target.value });
  return (
    <form className="sheet" onSubmit={async (e) => {
      e.preventDefault();
      try {
        await api.ask(shopId, { ...f, quantity: Number(f.quantity) });
        await done();
      } catch (err) {
        setError(String(err));
      }
    }}>
      <label className="wide">In your words<input value={f.words} onChange={set("words")} /></label>
      <label>Item<input value={f.item} onChange={set("item")} /></label>
      <label>Quantity<input value={f.quantity} inputMode="numeric" onChange={set("quantity")} /></label>
      <label>Unit<input value={f.unit} onChange={set("unit")} /></label>
      <label>Amount (US$)<input value={f.amount} inputMode="decimal" onChange={set("amount")} /></label>
      <label className="wide">Supplier<input value={f.supplier} onChange={set("supplier")} /></label>
      {error && <p className="error wide">{error}</p>}
      <div className="wide row">
        <button type="submit">Send to {owner}</button>
        <button type="button" className="ghost" onClick={() => done()}>Back</button>
      </div>
    </form>
  );
}
