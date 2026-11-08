import { useState, type FormEvent } from "react";
import { api, type LinkCode, type Shop } from "../api";

/**
 * The manager proves her WhatsApp number: Musha gives a code, and she sends it from that number. The shop's
 * next poll says the thread is live, and this card goes.
 */
export function LinkWhatsApp({ shop, compact = false }: { shop: Shop; compact?: boolean }) {
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
    <section className={`o-card m-wa${compact ? " m-wa-compact" : ""}`} aria-labelledby={`m-wa-${compact ? "side" : "main"}`}>
      <p className="o-eyebrow"><span className="o-dot o-tone-clay" aria-hidden="true" />WhatsApp</p>
      <h2 id={`m-wa-${compact ? "side" : "main"}`}>Link your WhatsApp</h2>
      {code ? (
        <>
          <p className="m-wa-text">From <strong>{code.phone}</strong>, send this code to Musha{code.to ? <> on <strong>{code.to}</strong></> : null}.</p>
          <p className="m-wa-code" aria-live="polite">{code.code}</p>
          <div className="o-row">
            {code.url && <a className="o-btn o-primary" href={code.url} target="_blank" rel="noreferrer">Open WhatsApp</a>}
            <button type="button" className="o-btn o-ghost" onClick={() => void ask()} disabled={busy}>New code</button>
          </div>
          <p className="o-fine">The code lasts {code.minutes} minutes. This card goes once Musha has it.</p>
        </>
      ) : (
        <>
          <p className="m-wa-text">Musha gives you a code. Send it from your WhatsApp, and this chat runs there too.</p>
          <div className="o-row">
            <button type="button" className="o-btn o-primary" onClick={() => void ask()} disabled={busy}>
              {busy ? <><span className="o-spin" aria-hidden="true" /> Getting a code</> : "Get a code"}
            </button>
          </div>
        </>
      )}
      {other === null ? (
        <p className="o-fine">
          <button type="button" className="o-textbtn" onClick={() => setOther("")}>Use another number</button>
        </p>
      ) : (
        <form className="m-wa-row" onSubmit={change}>
          <input type="tel" inputMode="tel" autoComplete="tel" aria-label="Your WhatsApp number" placeholder="+263 77 123 4567"
            value={other} onChange={(e) => setOther(e.target.value)} />
          <button type="submit" className="o-btn o-outline" disabled={busy || !other.trim()}>Use it</button>
        </form>
      )}
      {error && <p className="o-error" role="alert">{error}</p>}
    </section>
  );
}

/** Where the thread runs besides this dashboard, in one line. */
export function whatsappLine(shop: Shop): string {
  if (shop.whatsapp === "live") return "This chat also runs on your WhatsApp.";
  if (shop.whatsapp === "link") return "Link your WhatsApp, and this chat runs there too.";
  return "The same chat runs on WhatsApp when the shop connects it.";
}
