import { useEffect, useRef, useState, type ChangeEvent, type FormEvent, type KeyboardEvent, type PointerEvent } from "react";
import type { SpendRequest } from "../api";
import { VARIANTS, type Variant } from "../phone/api";
import { Box, Clip, Close, Clock, Doc, Gallery, MicFill, SendFill, Spark } from "../phone/icons";
import { describe, seconds, typesNumbers } from "../phone/util";

export type Attach = {
  targets: SpendRequest[];
  /** The model reads receipts. Without it, or once Musha asks for the numbers, a receipt photo goes with them typed. */
  ai: boolean;
  onFile: (kind: "receipt" | "goods", req: SpendRequest, file: File) => void;
  onTyped: (req: SpendRequest, file: File) => void;
  onSample: (variant: Variant, req: SpendRequest) => void;
  onGoods: (req: SpendRequest) => void;
};

const SHORT_MS = 450; // a press shorter than this is a tap: it cancels
const AWAY_PX = 70; // slide this far off the mic to cancel

function recordingType(): string {
  if (typeof MediaRecorder === "undefined") return "";
  for (const t of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"]) {
    if (MediaRecorder.isTypeSupported?.(t)) return t;
  }
  return "";
}

/** The message box: words, a voice note held on the mic, and proof from the clip. */
export function Composer({ onText, onVoice, attach, reply, clearReply, say, focus, focused }: {
  onText: (text: string) => void;
  onVoice: (audio: Blob, ms: number) => void;
  attach: Attach;
  /** Musha's open question; `sending` while the answer to it is on its way. */
  reply: { text: string; sending: boolean } | null;
  clearReply: () => void;
  say: (line: string) => void;
  /** Another view sent the manager here to write: the message box takes focus once, then says so. */
  focus: boolean;
  focused: () => void;
}) {
  const [text, setText] = useState("");
  const [menu, setMenu] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!focus) return;
    input.current?.focus({ preventScroll: true });
    focused();
  }, [focus, focused]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const t = text.trim();
    if (!t) return;
    onText(t);
    setText("");
    input.current?.focus();
  };

  const voice = useVoice(onVoice, say);
  const canRecord = typeof navigator !== "undefined" && !!navigator.mediaDevices && typeof MediaRecorder !== "undefined";
  const typed = !!text.trim() && !voice.recording;

  return (
    <div className="m-compose-wrap">
      {reply && (
        <div className={`m-replyto${reply.sending ? " m-sending" : ""}`} role="status">
          <div>
            <strong>{reply.sending ? "Your answer is on its way" : "Reply to Musha"}</strong>
            <span>{reply.text}</span>
          </div>
          {reply.sending ? (
            <span className="m-replyto-wait" aria-hidden="true"><Clock size={16} /></span>
          ) : (
            <button type="button" className="m-x m-x-small" onClick={clearReply} aria-label="Clear the reply"><Close size={14} /></button>
          )}
        </div>
      )}
      <form className="m-compose" onSubmit={submit}>
        <button type="button" className="m-icon-btn" aria-label="Attach proof" aria-haspopup="menu" aria-expanded={menu}
          onClick={() => setMenu((v) => !v)}>
          <Clip size={20} />
        </button>
        {voice.recording ? (
          <div className={`m-recording${voice.away ? " m-away" : ""}${voice.rolling ? "" : " m-waking"}`} role="status">
            <span className="m-rec-dot" />
            <span className="m-rec-time">{seconds(voice.elapsed)}</span>
            <span className="m-rec-hint">
              {voice.away ? "Release to cancel" : voice.rolling ? "Release to send. Slide away to cancel." : "Starting the microphone"}
            </span>
          </div>
        ) : (
          <input
            ref={input}
            className="m-input"
            value={text}
            placeholder="Write to Musha"
            aria-label="Message to Musha"
            onChange={(e) => setText(e.target.value)}
            enterKeyHint="send"
            maxLength={2000}
          />
        )}
        {typed ? (
          <button type="submit" className="m-round" aria-label="Send"><SendFill size={20} /></button>
        ) : (
          <button
            type="button"
            className={`m-round m-mic${voice.recording ? " m-live" : ""}`}
            aria-label="Hold to record a voice note"
            disabled={!canRecord}
            title={canRecord ? "Hold to record" : "Voice notes need a microphone"}
            onPointerDown={voice.down}
            onPointerMove={voice.move}
            onPointerUp={voice.up}
            onPointerCancel={voice.cancel}
            onKeyDown={voice.keyDown}
            onKeyUp={voice.keyUp}
            onContextMenu={(e) => e.preventDefault()}
          >
            <MicFill size={20} />
          </button>
        )}
      </form>
      {menu && <AttachMenu attach={attach} close={() => setMenu(false)} />}
    </div>
  );
}

/** Press and hold to record; release to send; a quick tap or a slide away cancels. */
function useVoice(onVoice: (audio: Blob, ms: number) => void, say: (line: string) => void) {
  type Rec = { mr?: MediaRecorder; stream?: MediaStream; chunks: Blob[]; started: number; released: boolean; cancelled: boolean };
  const rec = useRef<Rec | null>(null);
  const origin = useRef<{ x: number; y: number } | null>(null);
  const [recording, setRecording] = useState(false);
  const [rolling, setRolling] = useState(false); // the microphone is on and the note is being kept
  const [away, setAway] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const send = useRef(onVoice);
  send.current = onVoice;

  useEffect(() => {
    if (!rolling) return;
    const t = setInterval(() => {
      const r = rec.current;
      if (r) setElapsed(Date.now() - r.started);
    }, 200);
    return () => {
      clearInterval(t);
    };
  }, [rolling]);

  useEffect(() => {
    return () => {
      const r = rec.current;
      if (r) {
        r.cancelled = true;
        r.released = true;
        if (r.mr && r.mr.state !== "inactive") r.mr.stop();
        r.stream?.getTracks().forEach((t) => t.stop());
      }
    };
  }, []);

  const begin = async () => {
    if (rec.current) return;
    const r: Rec = { chunks: [], started: Date.now(), released: false, cancelled: false };
    rec.current = r;
    setRecording(true);
    setRolling(false);
    setAway(false);
    setElapsed(0);
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      rec.current = null;
      setRecording(false);
      say("Allow the microphone to send voice notes.");
      return;
    }
    if (r.released) {
      // The press ended while the browser asked about the microphone: the next press records.
      stream.getTracks().forEach((t) => t.stop());
      say(Date.now() - r.started > 1500 ? "The microphone is ready. Hold the mic to record." : "Hold the mic to record. Release to send.");
      return;
    }
    const type = recordingType();
    const mr = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
    mr.ondataavailable = (e) => {
      if (e.data.size) r.chunks.push(e.data);
    };
    mr.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      const ms = Date.now() - r.started;
      if (!r.cancelled && r.chunks.length) {
        send.current(new Blob(r.chunks, { type: (mr.mimeType || type || "audio/webm").split(";")[0] }), ms);
      }
    };
    r.mr = mr;
    r.stream = stream;
    r.started = Date.now();
    mr.start(250);
    setRolling(true);
  };

  const end = (cancel: boolean) => {
    const r = rec.current;
    if (!r) return;
    r.released = true;
    const held = Date.now() - r.started;
    r.cancelled = cancel || !r.mr || held < SHORT_MS;
    if (r.mr && r.mr.state !== "inactive") r.mr.stop();
    rec.current = null;
    origin.current = null;
    setRecording(false);
    setRolling(false);
    setAway(false);
    if (r.mr && held < SHORT_MS && !cancel) say("Hold the mic to record. Release to send.");
  };

  return {
    recording,
    rolling,
    away,
    elapsed,
    down: (e: PointerEvent<HTMLButtonElement>) => {
      if (e.button !== 0) return;
      e.preventDefault();
      e.currentTarget.setPointerCapture?.(e.pointerId);
      origin.current = { x: e.clientX, y: e.clientY };
      void begin();
    },
    move: (e: PointerEvent<HTMLButtonElement>) => {
      const o = origin.current;
      if (!o) return;
      setAway(Math.hypot(e.clientX - o.x, e.clientY - o.y) > AWAY_PX);
    },
    up: (e: PointerEvent<HTMLButtonElement>) => {
      const o = origin.current;
      const far = !!o && Math.hypot(e.clientX - o.x, e.clientY - o.y) > AWAY_PX;
      end(far);
    },
    cancel: () => end(true),
    keyDown: (e: KeyboardEvent<HTMLButtonElement>) => {
      if ((e.key === " " || e.key === "Enter") && !e.repeat) {
        e.preventDefault();
        void begin();
      }
    },
    keyUp: (e: KeyboardEvent<HTMLButtonElement>) => {
      if (e.key === " " || e.key === "Enter") {
        e.preventDefault();
        end(false);
      }
    },
  };
}

/** Proof from the clip: a receipt or goods photo from the device, or a sample, for one request waiting for it. */
function AttachMenu({ attach, close }: { attach: Attach; close: () => void }) {
  const { targets } = attach;
  const [id, setId] = useState(targets[0]?.id);
  const [samples, setSamples] = useState(false);
  const receipt = useRef<HTMLInputElement>(null);
  const goods = useRef<HTMLInputElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const req = targets.find((r) => r.id === id) || targets[0];
  const shut = useRef(close);
  shut.current = close;

  useEffect(() => {
    panel.current?.querySelector<HTMLElement>("select, button")?.focus({ preventScroll: true });
    const on = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") shut.current();
    };
    window.addEventListener("keydown", on);
    return () => {
      window.removeEventListener("keydown", on);
    };
  }, []);

  const picked = (kind: "receipt" | "goods") => (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (f && req) {
      if (kind === "receipt" && typesNumbers(req, attach.ai)) attach.onTyped(req, f);
      else attach.onFile(kind, req, f);
      close();
    }
  };

  return (
    <>
      <div className="m-scrim" onClick={close} />
      <div className="m-menu" role="menu" aria-label="Attach proof" ref={panel}>
        {req ? (
          <>
            <div className="m-menu-for">
              <span>Proof for</span>
              {targets.length > 1 ? (
                <select className="o-select" value={req.id} onChange={(e) => setId(Number(e.target.value))} aria-label="Request">
                  {targets.map((r) => <option key={r.id} value={r.id}>{describe(r)}</option>)}
                </select>
              ) : (
                <strong>{describe(req)}</strong>
              )}
            </div>
            <div className="m-menu-list">
              <button type="button" role="menuitem" className="m-menu-item" onClick={() => receipt.current?.click()}>
                <Doc size={20} /><span><strong>Receipt photo</strong><small>A photo or a PDF</small></span>
              </button>
              <button type="button" role="menuitem" className="m-menu-item" onClick={() => goods.current?.click()}>
                <Gallery size={20} /><span><strong>Goods photo</strong><small>The {req.item} as they came</small></span>
              </button>
              <button type="button" role="menuitem" className="m-menu-item" aria-expanded={samples} onClick={() => setSamples((v) => !v)}>
                <Spark size={20} /><span><strong>Sample receipt</strong><small>Five receipts that test Musha</small></span>
              </button>
              {samples && (
                <div className="m-variants" role="group" aria-label="Sample receipts">
                  {VARIANTS.map((v) => (
                    <button key={v.key} type="button" role="menuitem" onClick={() => {
                      attach.onSample(v.key, req);
                      close();
                    }}>
                      <strong>{v.label}</strong><small>{v.hint}</small>
                    </button>
                  ))}
                </div>
              )}
              <button type="button" role="menuitem" className="m-menu-item" onClick={() => {
                attach.onGoods(req);
                close();
              }}>
                <Box size={20} /><span><strong>Sample goods photo</strong><small>The {req.item} on the counter</small></span>
              </button>
            </div>
            <p className="m-menu-hint">
              {typesNumbers(req, attach.ai)
                ? "A receipt photo goes with its total, number and date. You type them next."
                : "Musha reads the receipt photo."}
            </p>
            <input ref={receipt} type="file" accept="image/*,application/pdf" hidden onChange={picked("receipt")} />
            <input ref={goods} type="file" accept="image/*" hidden onChange={picked("goods")} />
          </>
        ) : (
          <p className="m-menu-empty">Proof goes with an approved request. Ask for money first, then send the receipt here once the money is held.</p>
        )}
      </div>
    </>
  );
}
