import { useEffect, useRef, useState } from "react";
import { ownerApi, type Alert } from "./api";
import { useAction, when } from "./lib";
import "./ask.css";

/**
 * The owner's question to the manager on an alert or a gap. Once she asks, the ask button goes away and
 * the line reads "Asked 10:02 today, waiting for Farai's response" until the answer comes; then the
 * answer shows, with Ask again. The server says whether a question waits (alert_view, with_answers).
 */
export type AskState = {
  /** When the owner asked: the server's time, shown in the viewer's own. */
  askedAt?: string;
  /** The question waits on the manager's answer. The console offers no second ask meanwhile. */
  waiting: boolean;
  /** The manager's answer, in English, once it came, and when it came. */
  answer?: string;
  answeredAt?: string;
};

type Fields = { asked_at?: unknown; answer?: unknown; answer_en?: unknown; answered_at?: unknown };

const text = (v: unknown) => (typeof v === "string" && v.trim() ? v.trim() : undefined);
const ms = (iso?: string) => (iso ? Date.parse(iso) : NaN);

/** The state from an alert's data or a week row. `waiting` is the server's word; without it, an ask with no answer since waits. */
export function askState(fields: Fields, waiting?: boolean): AskState {
  const askedAt = text(fields.asked_at);
  const answeredAt = text(fields.answered_at);
  return {
    askedAt,
    answer: text(fields.answer_en) ?? text(fields.answer),
    answeredAt,
    waiting: waiting ?? (!!askedAt && !(ms(answeredAt) >= ms(askedAt))),
  };
}

/** Of a few readings of one question, the one with the latest ask. A tie keeps the earlier reading. */
export function freshest(...states: (AskState | undefined)[]): AskState {
  let best: AskState | undefined;
  for (const s of states) {
    if (s && (!best || (!best.askedAt && s.askedAt) || ms(s.askedAt) > ms(best.askedAt))) best = s;
  }
  return best ?? { waiting: false };
}

export const askLabel = (s: AskState, manager: string) => (s.askedAt ? "Ask again" : `Ask ${manager}`);

/**
 * Ask the manager about an alert. The act's own reply stands until the polled alert carries the same
 * question, so the wait shows at once. While the question waits, `ask` does nothing. `justAsked` is
 * true once this page asked, so the waiting line may take the focus the vanished button held.
 */
export function useAskManager(alert: Alert, refresh: () => Promise<void>) {
  const { busy, error, run } = useAction();
  const [reply, setReply] = useState<{ id: number; state: AskState }>();
  const mine = reply?.id === alert.id ? reply : undefined;
  const state = freshest(askState(alert.data, alert.waiting), mine?.state);
  const ask = () => {
    if (state.waiting || busy) return;
    void run(async () => {
      const r = await ownerApi.act(alert.id, "ask");
      setReply({ id: alert.id, state: askState(r.alert.data, r.alert.waiting) });
      await refresh();
    });
  };
  return { ...state, ask, busy, error, justAsked: !!mine };
}

/**
 * "Asked 10:02 today, waiting for Farai's response." Inline, it stands in the row where the ask button
 * was. With `focus`, it takes the focus the button held, so a keyboard keeps its place.
 */
export function AskWaiting({ state, manager, inline, focus }: {
  state: AskState;
  manager: string;
  inline?: boolean;
  focus?: boolean;
}) {
  const line = useRef<HTMLParagraphElement>(null);
  const show = state.waiting && !!state.askedAt;
  useEffect(() => {
    const lost = !document.activeElement || document.activeElement === document.body;
    if (show && focus && lost) line.current?.focus({ preventScroll: true });
  }, [show, focus]);
  if (!show) return null;
  return (
    <p ref={line} tabIndex={-1} className={`o-watching o-asked${inline ? " o-asked-inline" : ""}`}>
      <span className="o-watch-dot" aria-hidden="true" />Asked {when(state.askedAt)}, waiting for {manager}'s response.
    </p>
  );
}

/**
 * What came of the owner's question: the manager's answer, the wait with the time she asked, or the
 * time she asked once a day passed without an answer. `waitingLine` false leaves the wait to an inline
 * AskWaiting in the button's row.
 */
export function AskNote({ state, manager, focus, waitingLine = true }: {
  state: AskState;
  manager: string;
  focus?: boolean;
  waitingLine?: boolean;
}) {
  const { askedAt, answer, answeredAt, waiting } = state;
  const earlier = !!answer && !!askedAt && ms(answeredAt) < ms(askedAt); // an answer to an ask before this one
  return (
    <>
      {answer && (
        <p className="o-fine"><b className="o-ink">{manager} answered{answeredAt ? ` ${when(answeredAt)}` : ""}:</b> {answer}</p>
      )}
      {waitingLine && <AskWaiting state={state} manager={manager} focus={focus} />}
      {!waiting && askedAt && (!answer || earlier) && <p className="o-fine">You asked {manager} {when(askedAt)}.</p>}
    </>
  );
}
