// The shop call in the browser: one WebSocket to Musha's server, which holds the Gemini Live session.
// The microphone goes up as 16 kHz PCM16 in binary frames; the shop's voice comes down as 24 kHz PCM16.
//
// For a recorder: the microphone is read from navigator.mediaDevices.getUserMedia when a call starts,
// so a page script that replaces getUserMedia speaks into the call. Every server message is JSON with
// a `type`; the shop's voice arrives as {type: "reply.audio", data: base64 PCM16 at 24 kHz}.

import { call, post } from "../api";

export type Ring = {
  id: number;
  kind: "proof" | "cash_gap" | "camera" | string;
  reason: string;
  picture: string | null;
  state: string;
  at: string;
  alert_id: number | null;
};

export type CallInfo = { available: boolean; line: string; seconds: number };

export type RingPoll = { ring: Ring | null; call: CallInfo; on_call: boolean };

export const callApi = {
  ring: (shopId: number) => call<RingPoll>(`/api/shops/${shopId}/ring`),
  later: (shopId: number, ringId: number) => post<{ ring: Ring }>(`/api/shops/${shopId}/rings/${ringId}/later`),
};

export type Phase = "connecting" | "live" | "ended" | "failed";

export type Caption = { id: number; who: "owner" | "shop"; text: string; done: boolean };

export type Shown = { kind: string; url: string; label: string; line: string; at: string | null };

export type CallEvents = {
  phase: (phase: Phase, line?: string) => void;
  captions: (lines: Caption[]) => void;
  shown: (picture: Shown) => void;
  /** The shop is speaking, a tool is running, and how loud each side is (0 to 1). */
  activity: (a: { speaking: boolean; tool: string | null; you: number; shop: number }) => void;
  ending: (secondsLeft: number, line: string) => void;
};

const MIC_RATE = 16000;
const VOICE_RATE = 24000;
const CHUNK = 640; // 40 ms of microphone audio a frame

// Takes the microphone down to 16 kHz and posts 40 ms frames with their loudness. A context that already runs at
// 16 kHz passes samples through; any other rate goes through a 33-tap low-pass (windowed sinc, 7.6 kHz) first, so
// speech above the new limit leaves the band instead of folding back into it.
const CAPTURE = `
class Capture extends AudioWorkletProcessor {
  constructor() {
    super(); this.ratio = sampleRate / ${MIC_RATE};
    const n = this.ratio > 1.01 ? 33 : 1; this.taps = new Float32Array(n);
    if (n === 1) this.taps[0] = 1;
    else {
      const fc = 7600 / sampleRate; let sum = 0;
      for (let i = 0; i < n; i++) {
        const m = i - (n - 1) / 2;
        const s = m === 0 ? 2 * fc : Math.sin(2 * Math.PI * fc * m) / (Math.PI * m);
        const w = 0.42 - 0.5 * Math.cos(2 * Math.PI * i / (n - 1)) + 0.08 * Math.cos(4 * Math.PI * i / (n - 1));
        this.taps[i] = s * w; sum += s * w;
      }
      for (let i = 0; i < n; i++) this.taps[i] /= sum;
    }
    this.hist = new Float32Array(n); this.h = 0; this.seen = 0; this.next = 0;
    this.buf = new Int16Array(${CHUNK}); this.len = 0; this.energy = 0; this.count = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    const n = this.taps.length;
    for (let i = 0; i < ch.length; i++) {
      const v = ch[i]; this.energy += v * v; this.count++;
      this.hist[this.h] = v; this.h = (this.h + 1) % n; this.seen++;
      if (this.seen < this.next) continue;
      this.next += this.ratio;
      let acc = 0;
      for (let k = 0; k < n; k++) acc += this.taps[k] * this.hist[(this.h + k) % n];
      const s = Math.max(-1, Math.min(1, acc));
      this.buf[this.len++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      if (this.len === this.buf.length) {
        const level = Math.sqrt(this.energy / Math.max(1, this.count));
        this.port.postMessage({ pcm: this.buf.buffer, level }, [this.buf.buffer]);
        this.buf = new Int16Array(${CHUNK}); this.len = 0; this.energy = 0; this.count = 0;
      }
    }
    return true;
  }
}
registerProcessor("musha-capture", Capture);`;

// A queue of 24 kHz PCM16 chunks, read out at the context's rate. "flush" empties it at once.
const PLAYBACK = `
class Playback extends AudioWorkletProcessor {
  constructor() { super(); this.q = []; this.cur = null; this.off = 0; this.step = ${VOICE_RATE} / sampleRate;
    this.energy = 0; this.frames = 0; this.playing = false;
    this.port.onmessage = (e) => {
      if (e.data === "flush") { this.q = []; this.cur = null; this.off = 0; return; }
      this.q.push(new Int16Array(e.data));
    }; }
  sample() {
    while (!this.cur || this.off >= this.cur.length) {
      if (!this.q.length) return null;
      this.cur = this.q.shift(); this.off = 0;
    }
    const i = Math.floor(this.off); const a = this.cur[i] / 32768;
    const b = i + 1 < this.cur.length ? this.cur[i + 1] / 32768 : a;
    const v = a + (b - a) * (this.off - i); this.off += this.step; return v;
  }
  process(inputs, outputs) {
    const out = outputs[0][0]; let any = false;
    for (let i = 0; i < out.length; i++) {
      const v = this.sample();
      if (v === null) { out[i] = 0; } else { out[i] = v; any = true; this.energy += v * v; }
    }
    for (let c = 1; c < outputs[0].length; c++) outputs[0][c].set(out);
    this.frames++;
    if (any !== this.playing || this.frames >= 8) {
      this.playing = any;
      this.port.postMessage({ playing: any, level: Math.sqrt(this.energy / (this.frames * out.length)) });
      this.energy = 0; this.frames = 0;
    }
    return true;
  }
}
registerProcessor("musha-playback", Playback);`;

async function worklet(ctx: AudioContext, code: string, name: string): Promise<AudioWorkletNode> {
  const url = URL.createObjectURL(new Blob([code], { type: "application/javascript" }));
  try {
    await ctx.audioWorklet.addModule(url);
  } finally {
    URL.revokeObjectURL(url);
  }
  return new AudioWorkletNode(ctx, name, { outputChannelCount: [1] });
}

function fromBase64(b64: string): ArrayBuffer {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

/** A tone for the phone: the British double ring, at a volume, until stopped. */
export function tone(ctx: AudioContext, volume: number): () => void {
  const gain = ctx.createGain();
  gain.gain.value = 0;
  gain.connect(ctx.destination);
  const oscs = [400, 450].map((f) => {
    const o = ctx.createOscillator();
    o.frequency.value = f;
    o.connect(gain);
    o.start();
    return o;
  });
  const cadence: [number, number][] = [[0, 0.4], [0.6, 1.0]]; // on 0.4 s, off 0.2 s, on 0.4 s, off 2 s
  let next = ctx.currentTime + 0.05;
  const schedule = () => {
    while (next < ctx.currentTime + 6) {
      for (const [a, b] of cadence) {
        gain.gain.setTargetAtTime(volume, next + a, 0.01);
        gain.gain.setTargetAtTime(0, next + b, 0.01);
      }
      next += 3;
    }
  };
  schedule();
  const timer = window.setInterval(schedule, 2000);
  return () => {
    window.clearInterval(timer);
    gain.gain.cancelScheduledValues(ctx.currentTime);
    gain.gain.setTargetAtTime(0, ctx.currentTime, 0.02);
    window.setTimeout(() => {
      oscs.forEach((o) => {
        try {
          o.stop();
        } catch {
          // already stopped
        }
      });
      gain.disconnect();
    }, 120);
  };
}

/** One call to the shop. `start` opens it; `end` hangs up; `mute` holds the microphone back. */
export class ShopCall {
  private ws?: WebSocket;
  private micCtx?: AudioContext;
  private voiceCtx?: AudioContext;
  private mic?: MediaStream;
  private player?: AudioWorkletNode;
  private stopRingback?: () => void;
  private lines: Caption[] = [];
  private nextId = 1;
  private muted = false;
  private live = false;
  private started = false;
  private finished = false;
  private speaking = false;
  private tool: string | null = null;
  private you = 0;
  private shop = 0;

  constructor(private shopId: number, private on: CallEvents, private ringId?: number) {}

  async start() {
    if (this.started || this.finished) return;
    this.started = true;
    this.on.phase("connecting");
    try {
      this.voiceCtx = new AudioContext({ sampleRate: VOICE_RATE });
      // Chrome and Safari resample the microphone into a 16 kHz context themselves; Firefox keeps the device's
      // rate, and the capture worklet takes it down to 16 kHz.
      this.micCtx = new AudioContext({ sampleRate: MIC_RATE });
      await Promise.all([this.voiceCtx.resume(), this.micCtx.resume()]);
      this.stopRingback = tone(this.voiceCtx, 0.05);
      this.player = await worklet(this.voiceCtx, PLAYBACK, "musha-playback");
      this.player.connect(this.voiceCtx.destination);
      this.player.port.onmessage = ({ data }) => {
        this.shop = data.level || 0;
        if (data.playing !== this.speaking) this.speaking = data.playing;
        this.report();
      };
    } catch {
      return this.fail("The call needs this browser's speaker. Reload the page and call again.");
    }
    try {
      // The recorder's hook: a script that replaces getUserMedia speaks into the call.
      this.mic = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch {
      return this.fail("The call needs the microphone. Allow it for this page and call again.");
    }
    if (this.finished) return this.cleanup();
    try {
      let source: MediaStreamAudioSourceNode;
      try {
        source = this.micCtx!.createMediaStreamSource(this.mic);
      } catch {
        this.micCtx!.close().catch(() => undefined);
        this.micCtx = new AudioContext();
        await this.micCtx.resume().catch(() => undefined);
        source = this.micCtx.createMediaStreamSource(this.mic);
      }
      const capture = await worklet(this.micCtx!, CAPTURE, "musha-capture");
      source.connect(capture);
      // A worklet runs only while its graph reaches the speakers; a silent gain keeps it running.
      const hush = this.micCtx!.createGain();
      hush.gain.value = 0;
      capture.connect(hush).connect(this.micCtx!.destination);
      capture.port.onmessage = ({ data }) => {
        this.you = this.muted ? 0 : data.level;
        if (this.live && !this.muted && this.ws?.readyState === WebSocket.OPEN) this.ws.send(data.pcm);
      };
    } catch {
      return this.fail("The call needs the microphone. Allow it for this page and call again.");
    }
    if (this.finished) return this.cleanup();

    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    const url = `${scheme}://${window.location.host}/api/shops/${this.shopId}/call${this.ringId ? `?ring=${this.ringId}` : ""}`;
    // The owner's session cookie goes with the handshake: the call opens to her sign-in.
    const ws = (this.ws = new WebSocket(url, ["musha"]));
    ws.binaryType = "arraybuffer";
    ws.onmessage = (e) => {
      if (typeof e.data !== "string") return;
      let m: { type: string; [k: string]: unknown };
      try {
        m = JSON.parse(e.data);
      } catch {
        return;
      }
      this.heard(m);
    };
    ws.onclose = () => {
      if (!this.finished) this.fail("The line to the shop dropped. Call again.");
    };
  }

  private heard(m: { type: string; [k: string]: unknown }) {
    switch (m.type) {
      case "session.ready":
        this.live = true;
        this.stopRingback?.();
        this.stopRingback = undefined;
        this.on.phase("live");
        break;
      case "reply.audio":
        this.player?.port.postMessage(fromBase64(m.data as string));
        break;
      case "reply.started":
        this.closeLine("owner");
        break;
      case "reply.interrupted":
        this.player?.port.postMessage("flush");
        this.closeLine("shop");
        break;
      case "reply.done":
        this.closeLine("shop");
        this.tool = null;
        this.report();
        break;
      case "transcript.user.delta":
        this.add("owner", m.text as string);
        break;
      case "transcript.agent.delta":
        this.closeLine("owner");
        this.add("shop", m.text as string);
        break;
      case "tool.call":
        this.tool = m.name as string;
        this.report();
        break;
      case "evidence":
        this.on.shown({ kind: String(m.kind), url: String(m.url), label: String(m.label), line: String(m.line || ""),
          at: (m.at as string) || null });
        break;
      case "session.ending":
        this.on.ending(Number(m.seconds_left) || 0, String(m.message || ""));
        break;
      case "session.error":
        this.fail(String(m.message || "The call stopped."));
        break;
      case "session.ended":
        if (!this.finished) {
          this.finished = true;
          this.on.phase("ended");
        }
        this.cleanup();
        break;
    }
  }

  private add(who: Caption["who"], text: string) {
    if (!text) return;
    const n = this.lines.length;
    const last = this.lines[n - 1];
    const join = (l: Caption) => {
      l.text = (l.text + text).replace(/\s+/g, " ");
    };
    if (last && last.who === who && !last.done) {
      join(last);
    } else if (who === "owner" && last && last.who === "shop" && !last.done) {
      // The owner's words can land after the shop starts to answer: they belong before its reply.
      const before = this.lines[n - 2];
      if (before && before.who === "owner") join(before);
      else this.lines.splice(n - 1, 0, { id: this.nextId++, who, text: text.trimStart(), done: true });
    } else {
      this.lines.push({ id: this.nextId++, who, text: text.trimStart(), done: false });
      this.lines = this.lines.slice(-8);
    }
    this.on.captions(this.lines.map((l) => ({ ...l })));
  }

  private closeLine(who: Caption["who"]) {
    const last = this.lines[this.lines.length - 1];
    if (last && last.who === who && !last.done) {
      last.done = true;
      this.on.captions(this.lines.map((l) => ({ ...l })));
    }
  }

  private report() {
    this.on.activity({ speaking: this.speaking, tool: this.tool, you: this.you, shop: this.shop });
  }

  setMuted(on: boolean) {
    this.muted = on;
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: "mute", on }));
  }

  end() {
    if (this.finished) return;
    this.finished = true;
    if (!this.started) return; // a call that never opened has nothing to close
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "end" }));
      const ws = this.ws;
      window.setTimeout(() => {
        if (ws.readyState === WebSocket.OPEN) ws.close(1000);
      }, 2000);
    } else {
      this.ws?.close();
    }
    this.on.phase("ended");
    this.cleanup(false);
  }

  private fail(line: string) {
    if (this.finished) return;
    this.finished = true;
    this.on.phase("failed", line);
    this.cleanup();
  }

  private cleanup(closeSocket = true) {
    this.stopRingback?.();
    this.stopRingback = undefined;
    this.live = false;
    this.mic?.getTracks().forEach((t) => t.stop());
    this.player?.port.postMessage("flush");
    this.micCtx?.close().catch(() => undefined);
    // the shop's last words play out before its speaker closes
    const voice = this.voiceCtx;
    window.setTimeout(() => voice?.close().catch(() => undefined), 400);
    this.mic = this.micCtx = this.voiceCtx = this.player = undefined;
    if (closeSocket && this.ws && this.ws.readyState <= WebSocket.OPEN) this.ws.close(1000);
    this.speaking = false;
    this.you = this.shop = 0;
    this.tool = null;
    this.report();
  }
}
