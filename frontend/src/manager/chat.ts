import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type Message, type Shop, type SpendRequest } from "../api";
import { newKey, phone, PhoneError, STILL_READING, type Heard, type Printed, type Variant } from "../phone/api";
import { asks, PROVABLE, typesNumbers } from "../phone/util";
import type { MediaKind } from "./Media";

const RETRY_MS = 4000; // while Musha is still reading the first copy of a message
const RETRIES = 25;
const AHEAD_MS = 10000; // a request a POST returned stands in for the polled one this long at most
const NOTE_MS = 5000;
const CAPTIONS = new Set(["Receipt", "Photo of the goods"]);

/** A line the manager sent that the server has yet to hold. */
export type Pending = {
  key: string;
  kind: "text" | "voice" | "image";
  text: string;
  since: number;
  at: string;
  state: "sending" | "reading" | "failed";
  error?: string;
  requestId?: number;
  /** The line this one replies to, when it asks about no request. */
  replyTo?: number;
  blob?: Blob;
  url?: string;
  ms?: number;
};

/** A local file shown for a server line while the server's copy loads. */
export type Local = { url: string; kind: MediaKind; ms?: number };

/** A server line that carries a line this dashboard sent, with the pending line's key and when it settled. */
export type Settled = Map<number, { key: string; at: number }>;

/** The question the manager's next words answer: a Musha line, or the request's question before its line arrives.
 *  `sending` while an answer to it is on its way. */
export type Question = { id: number | null; key: number; requestId: number; text: string; sending: boolean };

type Source = () => Promise<{ file: Blob; printed?: Partial<Printed> }>;
/** What a POST from the dashboard returns: lines for the thread and the requests it touched. */
type Answer = Heard | { message?: Message; request?: SpendRequest };
type Upload = () => Promise<{ message?: Message; request?: SpendRequest }>;

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

/**
 * The manager's thread with Musha and everything she sends into it: words, voice notes, receipts and photos.
 * A sent line shows at once and keeps its place when the server's copy arrives. Requests a POST returned run
 * ahead of the next poll, so a question answered stops asking at once. Once she proves her WhatsApp number,
 * the same thread reaches it and her replies there land here too.
 */
export function useChat({ shop, requests, messages, loading, refresh }: {
  shop: Shop;
  requests: SpendRequest[];
  messages: Message[];
  /** The thread's first load is on its way: Musha shows as typing. */
  loading: boolean;
  refresh: () => Promise<void>;
}) {
  const [fresh, setFresh] = useState<Message[]>([]);
  const [pending, setPending] = useState<Pending[]>([]);
  const [replyTo, setReplyTo] = useState<number | null>(null);
  const [dismissed, setDismissed] = useState<Set<number>>(new Set());
  const [ai, setAi] = useState(false);
  const [note, setNote] = useState("");
  // Files this dashboard sent, kept by attachment id so a line keeps showing the local copy once it is on the server.
  const [kept, setKept] = useState<Map<number, Local>>(() => new Map());
  // Server lines that took over a pending line keep its place on screen, so a sent line never rises twice.
  const [settled, setSettled] = useState<Settled>(() => new Map());
  const alive = useRef(true);
  const tries = useRef(new Map<string, number>());
  const urls = useRef(new Map<string, string>());
  const uploads = useRef(new Map<string, Upload>());

  useEffect(() => {
    alive.current = true;
    api.config().then((c) => {
      if (alive.current) setAi(c.ai.enabled);
    }).catch(() => {});
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    if (!note) return;
    const t = setTimeout(() => setNote(""), NOTE_MS);
    return () => {
      clearTimeout(t);
    };
  }, [note]);

  // Requests a POST just returned run ahead of the next poll.
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

  const absorb = useCallback((out: Answer) => {
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
  }, []);

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
      if (p.kind === "text") out = await phone.say(shop.id, p.text, p.key, p.requestId, p.replyTo);
      else if (p.kind === "voice") out = await phone.voice(shop.id, p.blob!, p.key, p.requestId, p.replyTo);
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
      if (m && !m.request_id) return null; // a reply to a line of its own, such as the owner's question
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
    // translation). The dashboard knows it from the request, so the next words still answer it.
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

  // The line the manager chose to reply to when it asks about no request: the owner's question on an alert.
  // Its id goes with the words, so the answer reaches the owner's question with the model off too.
  const replying = useMemo(() => {
    if (replyTo == null || question) return null;
    const m = all.find((x) => x.id === replyTo);
    return m && !m.request_id ? { id: m.id, text: m.text || m.text_en } : null;
  }, [all, replyTo, question]);

  const sendText = (text: string) => {
    add({ ...base(), kind: "text", text, requestId: answering, replyTo: replying?.id });
    setReplyTo(null);
  };

  const sendVoice = (audio: Blob, ms: number) => {
    const p: Pending = {
      ...base(), kind: "voice", text: "Voice note", blob: audio, url: URL.createObjectURL(audio), ms,
      requestId: answering, replyTo: replying?.id,
    };
    urls.current.set(p.key, p.url!);
    add(p);
    setReplyTo(null);
  };

  /** A receipt or goods photo against a request. The line shows at once; the file may still be on its way. */
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

  /** Reply to this Musha line: the next words answer its question. */
  const reply = (m: Message) => {
    setReplyTo(m.id);
    setDismissed((d) => {
      if (!d.has(m.id)) return d;
      const n = new Set(d);
      n.delete(m.id);
      return n;
    });
  };

  const clearReply = () => {
    if (question) setDismissed((d) => new Set(d).add(question.key));
    setReplyTo(null);
  };

  // Requests that take proof: money held, or a question waiting. Wages need none.
  const targets = useMemo(
    () => reqs.filter((r) => PROVABLE.includes(r.status) && r.origin !== "wages").sort((a, b) => b.id - a.id),
    [reqs],
  );

  return {
    all, shown, local, settled, reqs, targets, typing, question, replying, ai, note, say: setNote,
    reply, clearReply, absorb, sendText, sendVoice, sendFile, sendSample, sendGoods, retry, drop,
  };
}

export type Chat = ReturnType<typeof useChat>;
