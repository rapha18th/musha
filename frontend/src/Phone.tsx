import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type Message, type Shop, type SpendRequest } from "./api";
import { newKey, phone, PhoneError, STILL_READING, type Heard, type Printed, type Variant, type Week } from "./phone/api";
import { Composer } from "./phone/Composer";
import { CounterCam } from "./phone/CounterCam";
import { AskSheet, CloseSheet, ProofSheet } from "./phone/Sheets";
import { Thread, type Local, type Pending } from "./phone/Thread";
import { asks, clock, PROVABLE, todayUtc, typesNumbers } from "./phone/util";
import { Back, Call, Dots, Mark, Video } from "./phone/icons";
import { WALLPAPER } from "./phone/wallpaper";
import "./phone/phone.css";
import "./phone/whatsapp.css";

type SheetOpen = { type: "ask" } | { type: "proof"; requestId: number; file?: File } | { type: "close" } | null;

const RETRY_MS = 4000; // while Musha is still reading the first copy of a message
const RETRIES = 25;
const AHEAD_MS = 10000; // a request a POST returned stands in for the polled one this long at most
const CAPTIONS = new Set(["Receipt", "Photo of the goods"]);

type Source = () => Promise<{ file: Blob; printed?: Partial<Printed> }>;
/** What a POST from the phone returns: lines for the thread and the requests it touched. */
type Answer = Heard | { message?: Message; request?: SpendRequest };
type Upload = () => Promise<{ message?: Message; request?: SpendRequest }>;
/** The question the manager's next words answer: a Musha line, or the request's question before its line arrives.
 *  `sending` while an answer to it is on its way. */
type Question = { id: number | null; key: number; requestId: number; text: string; sending: boolean };
/** A server line that carries a line this phone sent, with the pending line's key and when it settled. */
export type Settled = Map<number, { key: string; at: number }>;

/** Is this server line the copy of a pending line? */
function carries(p: Pending, x: Message, evidence: Set<number>): boolean {
  if (x.sender !== "manager" || x.id <= p.since) return false;
  if (p.kind === "text") return !x.attachment_id && x.text.trim() === p.text.trim();
  if (p.kind === "voice") return !!x.attachment_id && !evidence.has(x.attachment_id) && !CAPTIONS.has(x.text);
  return !!x.attachment_id && x.request_id === p.requestId && x.text === p.text;
}

/** The manager's own line in what a POST returned. */
function lineOf(p: Pending, out: Answer): Message | undefined {
  if (!("messages" in out)) return out.message;
  return out.messages.find((m) => m.sender === "manager" && (
    p.kind === "voice" ? !!m.attachment_id : !m.attachment_id && m.text.trim() === p.text.trim()));
}

/** Farai's phone: a WhatsApp-style chat with Musha in her own language, and the counter camera.
 *  Simulated here; the WhatsApp Business Platform is the next wire for the live chat. */
export function Phone({ shop, requests, messages, loading = false, refresh }: {
  shop: Shop;
  requests: SpendRequest[];
  messages: Message[];
  /** The thread's first load is on its way: Musha shows as typing. */
  loading?: boolean;
  refresh: () => Promise<void>;
}) {
  const [tab, setTab] = useState<"chat" | "counter">("chat");
  const [fresh, setFresh] = useState<Message[]>([]);
  const [pending, setPending] = useState<Pending[]>([]);
  const [sheet, setSheet] = useState<SheetOpen>(null);
  const [replyTo, setReplyTo] = useState<number | null>(null);
  const [dismissed, setDismissed] = useState<Set<number>>(new Set());
  const [ai, setAi] = useState(false);
  const [week, setWeek] = useState<Week>();
  const [note, setNote] = useState("");
  const [now, setNow] = useState(() => new Date());
  // Files this phone sent, kept by attachment id so a line keeps showing the local copy once it is on the server.
  const [kept, setKept] = useState<Map<number, Local>>(() => new Map());
  // Server lines that took over a pending line keep its place on screen, so a sent line never rises twice.
  const [settled, setSettled] = useState<Settled>(() => new Map());
  const alive = useRef(true);
  const tries = useRef(new Map<string, number>());
  const urls = useRef(new Map<string, string>());
  const frame = useRef<HTMLDivElement>(null);

  // On a small screen the phone fills what is left of the window below the header.
  useEffect(() => {
    const el = frame.current;
    if (!el) return;
    const fit = () => {
      if (window.innerWidth >= 960) {
        el.style.removeProperty("--ph-h");
        return;
      }
      const r = el.getBoundingClientRect();
      if (!r.width) return;
      const below = el.nextElementSibling instanceof HTMLElement ? el.nextElementSibling.offsetHeight + 8 : 0;
      const h = `${Math.max(480, Math.round(window.innerHeight - (r.top + window.scrollY) - 12 - below))}px`;
      if (el.style.getPropertyValue("--ph-h") !== h) el.style.setProperty("--ph-h", h);
    };
    fit();
    // The new height lands on the next frame, so the observer never sees its own change in the same loop.
    let frameId = 0;
    const later = () => {
      cancelAnimationFrame(frameId);
      frameId = requestAnimationFrame(fit);
    };
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(later);
    ro?.observe(el);
    window.addEventListener("resize", fit);
    return () => {
      cancelAnimationFrame(frameId);
      ro?.disconnect();
      window.removeEventListener("resize", fit);
    };
  }, []);

  useEffect(() => {
    alive.current = true;
    api.config().then((c) => {
      if (alive.current) setAi(c.ai.enabled);
    }).catch(() => {});
    const t = setInterval(() => setNow(new Date()), 30000);
    return () => {
      alive.current = false;
      clearInterval(t);
    };
  }, []);

  const loadWeek = useCallback(() => {
    phone.week(shop.id).then((w) => {
      if (alive.current) setWeek(w);
    }).catch(() => {});
  }, [shop.id]);
  useEffect(() => {
    loadWeek();
  }, [loadWeek]);

  // Requests a POST just returned run ahead of the next poll: a question answered stops asking at once.
  const [ahead, setAhead] = useState<Map<number, { r: SpendRequest; at: number }>>(() => new Map());
  useEffect(() => {
    setAhead((m) => {
      if (!m.size) return m;
      const next = new Map(m);
      for (const r of requests) {
        const a = next.get(r.id);
        if (a && (a.r.status === r.status || Date.now() - a.at > AHEAD_MS)) next.delete(r.id);
      }
      return next.size === m.size ? m : next;
    });
  }, [requests]);
  const reqs = useMemo(() => {
    if (!ahead.size) return requests;
    const seen = new Set(requests.map((r) => r.id));
    const extra = [...ahead.values()].map((a) => a.r).filter((r) => !seen.has(r.id));
    return [...extra, ...requests.map((r) => ahead.get(r.id)?.r ?? r)].sort((a, b) => b.id - a.id);
  }, [requests, ahead]);

  // Lines from the server, joined with lines a POST just returned.
  const all = useMemo(() => {
    const ids = new Set(messages.map((m) => m.id));
    const extra = fresh.filter((m) => !ids.has(m.id));
    return extra.length ? [...messages, ...extra].sort((a, b) => a.id - b.id) : messages;
  }, [messages, fresh]);

  useEffect(() => {
    const ids = new Set(messages.map((m) => m.id));
    setFresh((f) => (f.some((m) => ids.has(m.id)) ? f.filter((m) => !ids.has(m.id)) : f));
  }, [messages]);

  const lastId = all.length ? all[all.length - 1].id : 0;

  // Evidence attachments, known from the requests; a manager line with any other attachment is a voice note.
  const evidenceIds = useMemo(() => {
    const s = new Set<number>();
    for (const r of reqs) for (const a of r.attachments) if (a.kind !== "voice") s.add(a.id);
    return s;
  }, [reqs]);

  // A pending line hides once its own copy is on the server; its local file plays in that line.
  // Musha may answer in the thread before the POST returns (it still translates): then the dots stop.
  const { shown, local, answered, claims } = useMemo(() => {
    const claimed = new Set<number>();
    const local = new Map<number, Local>(kept);
    const shown: Pending[] = [];
    const answered = new Set<string>();
    const claims = new Map<number, string>();
    for (const p of pending) {
      const m = all.find((x) => !claimed.has(x.id) && (settled.get(x.id)?.key ?? p.key) === p.key && carries(p, x, evidenceIds));
      if (m) {
        claimed.add(m.id);
        if (all.some((x) => x.sender === "musha" && x.id > m.id)) answered.add(p.key);
        if (m.attachment_id && p.url) local.set(m.attachment_id, { url: p.url, kind: p.kind === "voice" ? "audio" : "image", ms: p.ms });
        if (p.state !== "failed") {
          claims.set(m.id, p.key);
          continue;
        }
      }
      shown.push(p);
    }
    return { shown, local, answered, claims };
  }, [pending, all, evidenceIds, kept, settled]);

  // A line the poll brought before its POST returned keeps the pending line's place from then on.
  useEffect(() => {
    if (!claims.size) return;
    setSettled((s) => {
      let next: Settled | null = null;
      for (const [id, key] of claims) {
        if (!s.has(id)) (next ??= new Map(s)).set(id, { key, at: Date.now() });
      }
      return next ?? s;
    });
  }, [claims]);

  const typing = loading || pending.some((p) => p.state !== "failed" && !answered.has(p.key));

  const patch = (key: string, change: Partial<Pending>) =>
    setPending((ps) => ps.map((p) => (p.key === key ? { ...p, ...change } : p)));

  const absorb = (out: Answer) => {
    const lines = "messages" in out ? out.messages : out.message ? [out.message] : [];
    if (lines.length) setFresh((f) => [...f, ...lines.filter((m) => !f.some((x) => x.id === m.id))]);
    const touched = "requests" in out ? out.requests : out.request ? [out.request] : [];
    if (touched.length) {
      const at = Date.now();
      setAhead((m) => {
        const next = new Map(m);
        for (const r of touched) next.set(r.id, { r, at });
        return next;
      });
    }
  };

  const finish = (p: Pending) => {
    tries.current.delete(p.key);
    urls.current.delete(p.key);
    setPending((ps) => ps.filter((x) => x.key !== p.key));
    void refresh();
  };

  /** The server line takes the pending line's place, and the sent file stays on screen for it. */
  const keep = (p: Pending, out: Answer) => {
    const line = lineOf(p, out);
    if (!line) return;
    setSettled((s) => (s.has(line.id) ? s : new Map(s).set(line.id, { key: p.key, at: Date.now() })));
    const url = urls.current.get(p.key);
    const id = line.attachment_id;
    if (!url || !id) return;
    setKept((k) => new Map(k).set(id, { url, kind: p.kind === "voice" ? "audio" : "image", ms: p.ms }));
  };

  /** Send a pending line. Text and voice notes keep their key across retries, so Musha hears them once. */
  const run = async (p: Pending, upload?: Upload) => {
    patch(p.key, { state: "sending", error: undefined });
    try {
      let out: Answer | undefined;
      if (p.kind === "text") out = await phone.say(shop.id, p.text, p.key, p.requestId);
      else if (p.kind === "voice") out = await phone.voice(shop.id, p.blob!, p.key, p.requestId);
      else if (upload) out = await upload();
      if (out) {
        absorb(out);
        keep(p, out);
      }
      if (alive.current) finish(p);
    } catch (e) {
      if (!alive.current) return;
      const n = (tries.current.get(p.key) || 0) + 1;
      tries.current.set(p.key, n);
      if (e instanceof PhoneError && e.status === 409 && e.message === STILL_READING && n < RETRIES) {
        patch(p.key, { state: "reading" });
        setTimeout(() => {
          if (alive.current) void run(p, upload);
        }, RETRY_MS);
        return;
      }
      patch(p.key, { state: "failed", error: e instanceof Error ? e.message : String(e) });
      void refresh();
    }
  };

  const uploads = useRef(new Map<string, Upload>());

  const add = (p: Pending, upload?: Upload) => {
    setPending((ps) => [...ps, p]);
    if (upload) uploads.current.set(p.key, upload);
    void run(p, upload);
  };

  const base = () => ({ key: newKey(), since: lastId, at: new Date().toISOString(), state: "sending" as const });

  // The request a reply answers: the Musha line the manager chose, while its request is asking.
  const byId = useMemo(() => new Map(reqs.map((r) => [r.id, r])), [reqs]);
  const question = useMemo<Question | null>(() => {
    const asking = (m: Message) => !!m.request_id && asks(m, byId.get(m.request_id));
    const live = pending.filter((p) => p.state !== "failed");
    // An answer to the request on its way: the banner waits with it.
    const sending = (rid: number) => live.some((p) => p.requestId === rid);
    // Anything else the manager sent after the question means she moved on.
    const movedOn = (rid: number, after: (p: Pending) => boolean) => live.some((p) => p.requestId !== rid && after(p));
    const of = (m: Message): Question =>
      ({ id: m.id, key: m.id, requestId: m.request_id!, text: m.text || m.text_en, sending: sending(m.request_id!) });
    if (replyTo != null) {
      const m = all.find((x) => x.id === replyTo);
      if (m && asking(m)) return of(m);
    }
    // The newest question. The manager's answers to it keep it open until its request moves on.
    let only: number | null = null;
    for (let i = all.length - 1; i >= 0; i--) {
      const m = all[i];
      if (m.sender === "manager") {
        const r = m.request_id ? byId.get(m.request_id) : undefined;
        if (!r || r.status !== "asking" || (only !== null && only !== r.id)) break;
        only = r.id;
        continue;
      }
      if (!asking(m) || (only !== null && m.request_id !== only)) continue;
      if (movedOn(m.request_id!, (p) => p.since >= m.id)) break;
      if (!dismissed.has(m.id)) return of(m);
    }
    // Musha's question can reach the request before its line reaches the thread (the line waits for its
    // translation). The phone knows it from the request, so the next words still answer it.
    for (const r of reqs) {
      if (r.status !== "asking" || dismissed.has(-r.id) || all.some((m) => asks(m, r))) continue;
      const p = [...r.proofs].reverse().find((x) => x.question);
      if (!p) continue;
      const asked = Date.parse(p.created_at);
      if (all.some((m) => m.sender === "manager" && m.request_id !== r.id && Date.parse(m.at) > asked)) continue;
      if (movedOn(r.id, (x) => Date.parse(x.at) > asked)) continue;
      return { id: null, key: -r.id, requestId: r.id, text: p.question, sending: sending(r.id) };
    }
    return null;
  }, [all, replyTo, byId, dismissed, pending, reqs]);

  // The next words answer the open question, unless an answer to it is on its way already.
  const answering = question && !question.sending ? question.requestId : undefined;

  const sendText = (text: string) => {
    add({ ...base(), kind: "text", text, requestId: answering });
    setReplyTo(null);
  };

  const sendVoice = (audio: Blob, ms: number) => {
    const p: Pending = { ...base(), kind: "voice", text: "Voice note", blob: audio, url: URL.createObjectURL(audio), ms, requestId: answering };
    urls.current.set(p.key, p.url!);
    add(p);
    setReplyTo(null);
  };

  /** A receipt or goods photo against a request. The bubble shows at once; the file may still be on its way. */
  const sendEvidence = (req: SpendRequest, kind: "receipt" | "goods", source: Source, url?: string) => {
    const p: Pending = { ...base(), kind: "image", text: kind === "receipt" ? "Receipt" : "Photo of the goods", requestId: req.id, url };
    if (url) urls.current.set(p.key, url);
    let got: Awaited<ReturnType<Source>> | undefined;
    add(p, async () => {
      if (!got) {
        got = await source();
        if (!url && got.file.type.startsWith("image/")) {
          const made = URL.createObjectURL(got.file);
          urls.current.set(p.key, made);
          patch(p.key, { url: made });
        }
      }
      return phone.evidence(req.id, got.file, kind, got.printed);
    });
  };

  const sendFile = (req: SpendRequest, kind: "receipt" | "goods", file: File, printed?: Partial<Printed>) =>
    sendEvidence(req, kind, async () => ({ file, printed }), file.type.startsWith("image/") ? URL.createObjectURL(file) : undefined);

  const sendSample = (req: SpendRequest, variant: Variant) =>
    sendEvidence(req, "receipt", async () => {
      const { image, printed } = await phone.sample(req.id, variant);
      // With the model on, Musha reads the receipt itself. The printed lines stand in for typed numbers
      // while it is off, or once Musha asked for them.
      return { file: image, printed: typesNumbers(req, ai) ? printed : undefined };
    });

  const sendGoods = (req: SpendRequest) =>
    sendEvidence(req, "goods", async () => {
      try {
        return { file: await phone.goods(req.item) };
      } catch {
        throw new Error(`Sample photos cover the shop's own goods. Photograph the ${req.item}.`);
      }
    });

  const drop = (p: Pending) => {
    tries.current.delete(p.key);
    uploads.current.delete(p.key);
    urls.current.delete(p.key);
    setPending((ps) => ps.filter((x) => x.key !== p.key));
  };

  const retry = (p: Pending) => {
    tries.current.delete(p.key);
    void run(p, uploads.current.get(p.key));
  };

  useEffect(() => {
    if (!note) return;
    const t = setTimeout(() => setNote(""), 5000);
    return () => {
      clearTimeout(t);
    };
  }, [note]);

  const targets = useMemo(
    () => reqs.filter((r) => PROVABLE.includes(r.status) && r.origin !== "wages").sort((a, b) => b.id - a.id),
    [reqs],
  );
  const proofFor = sheet?.type === "proof" ? reqs.find((r) => r.id === sheet.requestId) : undefined;
  const lastDay = week?.cash[week.cash.length - 1];
  const closedToday = !!lastDay && lastDay.day === todayUtc() && lastDay.closed;
  const close = useCallback(() => setSheet(null), []);
  // The camera in the bar opens the proof sheet for the newest request waiting for proof.
  const camera = () => {
    const r = targets[0];
    if (r) setSheet({ type: "proof", requestId: r.id });
    else setNote("Proof goes with an approved request. Ask for money first.");
  };

  return (
    <>
      <div className="ph" data-tab={tab} ref={frame} style={WALLPAPER}>
        <div className="ph-status" aria-hidden="true">
          <span>{clock(now)}</span>
          <span className="ph-island" />
          <span className="ph-signal"><i /><i /><i /><i /></span>
        </div>
        <header className="ph-head">
          <div className="ph-bar">
            <span className="ph-back" aria-hidden="true"><Back /></span>
            <span className="ph-avatar" aria-hidden="true"><Mark size={22} /></span>
            <div className="ph-who">
              <strong>Musha</strong>
              <span className={typing ? "ph-typing-label" : undefined}>{typing ? "typing…" : "online"}</span>
            </div>
            <span className="ph-head-icons" aria-hidden="true"><Video /><Call /><Dots /></span>
          </div>
          <nav className="ph-tabs" aria-label="Phone">
            <button type="button" aria-pressed={tab === "chat"} onClick={() => setTab("chat")}>Chat</button>
            <button type="button" aria-pressed={tab === "counter"} onClick={() => setTab("counter")}>Counter</button>
          </nav>
        </header>

        {tab === "counter" ? (
          <div className="ph-body ph-counter">
            <CounterCam shopId={shop.id} embedded />
          </div>
        ) : (
          <div className="ph-body ph-chat">
            <Thread
              shop={shop}
              messages={all}
              pending={shown}
              settled={settled}
              local={local}
              requests={reqs}
              typing={typing}
              replyTo={question?.id ?? null}
              onReply={(m) => {
                setReplyTo(m.id);
                setDismissed((d) => {
                  const n = new Set(d);
                  n.delete(m.id);
                  return n;
                });
              }}
              onProof={(r) => setSheet({ type: "proof", requestId: r.id })}
              onRetry={retry}
              onDrop={drop}
            />
            {note && <p className="ph-note" role="status">{note}</p>}
            <div className="ph-chips" role="toolbar" aria-label="Quick actions">
              <button type="button" className="ph-chip ph-chip-main" onClick={() => setSheet({ type: "ask" })}>Ask for money</button>
              {targets.map((r) => (
                <button key={r.id} type="button" className="ph-chip ph-chip-proof" onClick={() => setSheet({ type: "proof", requestId: r.id })}>
                  Send proof for {r.item}
                </button>
              ))}
              {!closedToday && (
                <button type="button" className="ph-chip" onClick={() => setSheet({ type: "close" })}>Close the day</button>
              )}
            </div>
            <Composer
              onText={sendText}
              onVoice={sendVoice}
              onCamera={camera}
              say={setNote}
              reply={question ? { text: question.text, sending: question.sending } : null}
              clearReply={() => {
                if (question) setDismissed((d) => new Set(d).add(question.key));
                setReplyTo(null);
              }}
              attach={{
                targets,
                ai,
                onFile: (kind, req, file) => sendFile(req, kind, file),
                // With the model off, Musha checks the numbers the manager types from the receipt.
                onTyped: (req, file) => setSheet({ type: "proof", requestId: req.id, file }),
                onSample: (variant, req) => sendSample(req, variant),
                onGoods: (req) => sendGoods(req),
              }}
            />
            {sheet?.type === "ask" && (
              <AskSheet shop={shop} requests={reqs} close={close} done={async (made) => {
                setSheet(null);
                absorb({ request: made });
                await refresh();
              }} />
            )}
            {sheet?.type === "proof" && proofFor && (
              <ProofSheet
                req={proofFor}
                ai={ai}
                start={sheet.file}
                close={close}
                onFile={(kind, file, printed) => sendFile(proofFor, kind, file, printed)}
                onSample={(variant) => sendSample(proofFor, variant)}
                onGoods={() => sendGoods(proofFor)}
              />
            )}
            {sheet?.type === "close" && (
              <CloseSheet shop={shop} close={close} done={async () => {
                loadWeek();
                await refresh();
              }} />
            )}
          </div>
        )}
      </div>
      <p className="ph-under">Musha's chat, simulated here.</p>
    </>
  );
}
