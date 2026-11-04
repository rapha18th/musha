import { useEffect, useRef, useState, type MouseEvent } from "react";
import { createPortal } from "react-dom";
import { Close, MicFill, Pause, Play } from "./icons";
import { seconds } from "./util";

export type MediaKind = "image" | "audio" | "pdf" | "other";
type Resolved = { kind: MediaKind; src: string };

const resolved = new Map<string, Promise<Resolved>>();

function kindOf(type: string): MediaKind {
  if (type.startsWith("image/")) return "image";
  if (type.startsWith("audio/") || type === "video/webm") return "audio";
  if (type === "application/pdf") return "pdf";
  return "other";
}

/** What an attachment is, read once from its own content type. */
function resolve(url: string): Promise<Resolved> {
  let p = resolved.get(url);
  if (!p) {
    p = fetch(url).then(async (r) => {
      if (!r.ok) throw new Error(String(r.status));
      const type = (r.headers.get("Content-Type") || "").split(";")[0];
      return { kind: kindOf(type), src: URL.createObjectURL(await r.blob()) };
    });
    p.catch(() => resolved.delete(url));
    resolved.set(url, p);
  }
  return p;
}

export function useMedia(url: string | null, known?: MediaKind): Resolved | null {
  const [got, setGot] = useState<Resolved | null>(known && url ? { kind: known, src: url } : null);
  useEffect(() => {
    if (!url) return;
    if (known) {
      setGot({ kind: known, src: url });
      return;
    }
    let live = true;
    resolve(url).then((r) => {
      if (live) setGot(r);
    }).catch(() => {
      if (live) setGot({ kind: "other", src: url });
    });
    return () => {
      live = false;
    };
  }, [url, known]);
  return got;
}

/** A photo in a bubble. Tap to see it whole. */
export function Photo({ src, alt }: { src: string; alt: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" className="ph-photo" onClick={() => setOpen(true)} aria-label={`Open ${alt}`}>
        <img src={src} alt={alt} loading="lazy" />
      </button>
      {open && <Lightbox src={src} alt={alt} close={() => setOpen(false)} />}
    </>
  );
}

export function Lightbox({ src, alt, close }: { src: string; alt: string; close: () => void }) {
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", on);
    return () => {
      window.removeEventListener("keydown", on);
    };
  }, [close]);
  return createPortal(
    <div className="ph-lightbox" role="dialog" aria-label={alt} onClick={close}>
      <img src={src} alt={alt} onClick={(e) => e.stopPropagation()} />
      <button type="button" className="ph-lightbox-close" onClick={close} aria-label="Close"><Close size={22} /></button>
    </div>,
    document.body,
  );
}

// Voice notes

const BARS = 30;
const shapes = new Map<string, Promise<{ peaks: number[]; duration: number }>>();

/** The note's loudness in 30 bars and its true length, decoded once. */
function shape(src: string): Promise<{ peaks: number[]; duration: number }> {
  let p = shapes.get(src);
  if (!p) {
    p = (async () => {
      const data = await (await fetch(src)).arrayBuffer();
      const ctx = new OfflineAudioContext(1, 1, 44100);
      const audio = await ctx.decodeAudioData(data);
      const ch = audio.getChannelData(0);
      const step = Math.max(1, Math.floor(ch.length / BARS));
      const raw: number[] = [];
      for (let b = 0; b < BARS; b++) {
        let peak = 0;
        for (let i = b * step; i < Math.min(ch.length, (b + 1) * step); i += 16) {
          peak = Math.max(peak, Math.abs(ch[i]));
        }
        raw.push(peak);
      }
      const top = Math.max(...raw, 0.01);
      return { peaks: raw.map((v) => Math.max(0.12, v / top)), duration: audio.duration };
    })();
    p.catch(() => shapes.delete(src));
    shapes.set(src, p);
  }
  return p;
}

const FLAT = Array.from({ length: BARS }, (_, i) => 0.25 + 0.2 * Math.abs(Math.sin(i * 1.7)));

/** Who recorded a note, drawn beside it: their initial, and a mic that turns blue once Musha heard it. */
export type Voice = { initial: string; heard: boolean };

export function VoiceNote({ src, knownMs, voice }: { src: string; knownMs?: number; voice?: Voice }) {
  const audio = useRef<HTMLAudioElement>(null);
  const [peaks, setPeaks] = useState<number[]>(FLAT);
  const [duration, setDuration] = useState((knownMs || 0) / 1000);
  const [at, setAt] = useState(0);
  const [playing, setPlaying] = useState(false);

  useEffect(() => {
    let live = true;
    shape(src).then((s) => {
      if (!live) return;
      setPeaks(s.peaks);
      if (Number.isFinite(s.duration) && s.duration > 0) setDuration(s.duration);
    }).catch(() => {
      // the bars stay decorative; the player still plays
    });
    return () => {
      live = false;
    };
  }, [src]);

  const toggle = () => {
    const a = audio.current;
    if (!a) return;
    if (a.paused) {
      a.play().catch(() => setPlaying(false));
    } else {
      a.pause();
    }
  };

  const seek = (e: MouseEvent<HTMLDivElement>) => {
    const a = audio.current;
    if (!a || !duration) return;
    const box = e.currentTarget.getBoundingClientRect();
    a.currentTime = Math.min(duration, Math.max(0, ((e.clientX - box.left) / box.width) * duration));
    setAt(a.currentTime);
  };

  const done = duration ? Math.min(1, at / duration) : 0;
  return (
    <div className={`ph-voice${voice ? " ph-voice-own" : ""}`}>
      {voice && (
        <span className="ph-voice-face" aria-hidden="true">
          {voice.initial}
          <i className={voice.heard ? "heard" : undefined}><MicFill size={12} /></i>
        </span>
      )}
      <button type="button" className="ph-voice-play" onClick={toggle} aria-label={playing ? "Pause voice note" : "Play voice note"}>
        {playing ? <Pause size={20} /> : <Play size={20} />}
      </button>
      <div className="ph-voice-track">
        <div className="ph-voice-bars" onClick={seek} role="presentation">
          {peaks.map((h, i) => (
            <span key={i} style={{ height: `${Math.round(h * 100)}%` }} className={i / peaks.length < done ? "on" : ""} />
          ))}
        </div>
        <span className="ph-voice-knob" style={{ left: `${(done * 100).toFixed(1)}%` }} aria-hidden="true" />
      </div>
      <span className="ph-voice-time">{seconds((playing || at ? at : duration) * 1000)}</span>
      <audio
        ref={audio}
        src={src}
        preload="none"
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => {
          setPlaying(false);
          setAt(0);
        }}
        onTimeUpdate={(e) => setAt(e.currentTarget.currentTime)}
      />
    </div>
  );
}

/** Any attachment on a line: a photo, a voice note, or a link to the file. */
export function MediaOf({ url, known, knownMs, alt, voice }: {
  url: string;
  known?: MediaKind;
  knownMs?: number;
  alt: string;
  /** Farai's own note: her initial beside the player. */
  voice?: Voice;
}) {
  const m = useMedia(url, known);
  if (!m) return <div className="ph-media-wait" aria-label="Loading" />;
  if (m.kind === "image") return <Photo src={m.src} alt={alt} />;
  if (m.kind === "audio") return <VoiceNote src={m.src} knownMs={knownMs} voice={voice} />;
  return <a className="ph-file" href={m.src} target="_blank" rel="noreferrer">Open the file</a>;
}
