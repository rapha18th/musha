import { useEffect, useRef, type RefObject } from "react";
import { usd, type Shop, type SpendRequest } from "../api";
import { describe, statusOf } from "../phone/util";
import type { Chat } from "./chat";
import { Composer } from "./Composer";
import { LinkWhatsApp, whatsappLine } from "./LinkWhatsApp";
import { Thread } from "./Thread";
import { SETTLED, toneOf } from "./words";

const MIN_H = 440;
const MAX_H = 920;

/** The chat stands as tall as the window allows below the dashboard's head, so the message box stays in view. */
function useFit(el: RefObject<HTMLElement | null>) {
  useEffect(() => {
    const box = el.current;
    if (!box) return;
    const fit = () => {
      const r = box.getBoundingClientRect();
      if (!r.width) return;
      const h = `${Math.min(MAX_H, Math.max(MIN_H, Math.round(window.innerHeight - (r.top + window.scrollY) - 16)))}px`;
      if (box.style.getPropertyValue("--m-chat-h") !== h) box.style.setProperty("--m-chat-h", h);
    };
    fit();
    // The new height lands on the next frame, so the observer never sees its own change in the same loop.
    let frame = 0;
    const later = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(fit);
    };
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(later);
    ro?.observe(document.body);
    window.addEventListener("resize", later);
    return () => {
      cancelAnimationFrame(frame);
      ro?.disconnect();
      window.removeEventListener("resize", later);
    };
  }, [el]);
}

/** Messages: the manager's thread with Musha, her quick actions, and where the thread also runs. */
export function Messages({ shop, chat, focus, focused, onAsk, onProof, onClose, closedToday }: {
  shop: Shop;
  chat: Chat;
  /** Today sent the manager here to answer: the message box takes focus. */
  focus: boolean;
  focused: () => void;
  onAsk: () => void;
  onProof: (req: SpendRequest, file?: File) => void;
  onClose: () => void;
  closedToday: boolean;
}) {
  const card = useRef<HTMLElement>(null);
  useFit(card);
  const { question, targets } = chat;
  const open = chat.reqs.filter((r) => !SETTLED.includes(r.status));

  return (
    <div className="m-messages">
      <section className="m-chat" ref={card} aria-label="Messages with Musha">
        <header className="m-chat-head">
          <span className="o-musha-mark m-chat-mark" aria-hidden="true">M</span>
          <div>
            <strong>Musha</strong>
            <span className={chat.typing ? "m-chat-typing" : undefined}>{chat.typing ? "Writing" : whatsappLine(shop)}</span>
          </div>
        </header>
        <Thread
          shop={shop}
          messages={chat.all}
          pending={chat.shown}
          settled={chat.settled}
          local={chat.local}
          requests={chat.reqs}
          typing={chat.typing}
          replyTo={question?.id ?? chat.replying?.id ?? null}
          onReply={chat.reply}
          onProof={(r) => onProof(r)}
          onRetry={chat.retry}
          onDrop={chat.drop}
        />
        <div className="m-chips" role="toolbar" aria-label="Quick actions">
          <button type="button" className="o-chip m-chip-main" onClick={onAsk}>Ask for money</button>
          {targets.map((r) => (
            <button key={r.id} type="button" className="o-chip" onClick={() => onProof(r)}>Send proof for {r.item}</button>
          ))}
          {!closedToday && <button type="button" className="o-chip" onClick={onClose}>Close the day</button>}
        </div>
        <Composer
          onText={chat.sendText}
          onVoice={chat.sendVoice}
          say={chat.say}
          focus={focus}
          focused={focused}
          reply={question ? { text: question.text, sending: question.sending }
            : chat.replying ? { text: chat.replying.text, sending: false } : null}
          clearReply={chat.clearReply}
          attach={{
            targets,
            ai: chat.ai,
            onFile: (kind, req, file) => chat.sendFile(req, kind, file),
            // With the model off, Musha checks the numbers the manager types from the receipt.
            onTyped: (req, file) => onProof(req, file),
            onSample: (variant, req) => chat.sendSample(req, variant),
            onGoods: (req) => chat.sendGoods(req),
          }}
        />
      </section>

      <aside className="m-side" aria-label="Beside the chat">
        {shop.whatsapp === "link" ? <LinkWhatsApp shop={shop} compact /> : (
          <section className="o-card m-side-card">
            <p className="o-eyebrow">
              <span className={`o-dot${shop.whatsapp === "live" ? " o-on" : ""}`} aria-hidden="true" />WhatsApp
            </p>
            <p className="m-side-line">{whatsappLine(shop)}</p>
            {shop.whatsapp === "live" && <p className="o-fine">Write here or on WhatsApp. Both land in the same thread.</p>}
          </section>
        )}
        <section className="o-card m-side-card">
          <p className="o-eyebrow">Open requests</p>
          {open.length === 0 ? (
            <p className="o-fine">Write what the shop needs, or press Ask for money.</p>
          ) : (
            <ul className="m-side-list">
              {open.map((r) => {
                const s = statusOf(r.status, shop.owner.name);
                return (
                  <li key={r.id}>
                    <span className="m-side-what">{describe(r)}</span>
                    <span className="o-num">{usd(r.hold?.amount_cents ?? r.amount_cents)}</span>
                    <span className={`o-pill o-tone-${toneOf(s.tone)}`}>{r.proving ? "Musha is reading" : s.label}</span>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
        <p className="o-fine m-side-tip">Hold the mic to send a voice note. Musha writes it down and answers in your language.</p>
      </aside>
    </div>
  );
}
