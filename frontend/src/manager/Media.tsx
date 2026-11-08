import { useEffect, useRef, useState, type MouseEvent } from "react";
import { createPortal } from "react-dom";
import { Close, Pause, Play } from "../phone/icons";
import { seconds } from "../phone/util";

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

function useMedia(url: string | null, known?: MediaKind): Resolved | null {
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

/** A photo in a line. Press it to see it whole. */
export function Photo({ src, alt, className = "m-photo" }: { src: string; alt: string; className?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" className={className} onClick={() => setOpen(true)} aria-label={`Open ${alt}`}>
        <img src={src} alt={alt} loading="lazy" />
      </button>
      {open && <Lightbox src={src} alt={alt} close={() => setOpen(false)} />}
    </>
  );
}

export function Lightbox({ src, alt, caption, close }: { src: string; alt: string; caption?: string; close: () => void }) {
  const button = useRef<HTMLButtonElement>(null);
  const shut = useRef(close);
  shut.current = close;
  useEffect(() => {
    const back = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    button.current?.focus({ preventScroll: true });
    const on = (e: KeyboardEvent) => {
      if (e.key === "Escape") shut.current();
    };
    window.addEventListener("keydown", on);
    return () => {
      window.removeEventListener("keydown", on);
      back?.focus({ preventScroll: true });
    };
  }, []);
  return createPortal(
    <div className="m-lightbox" role="dialog" aria-modal="true" aria-label={alt} onClick={close}>
      <figure onClick={(e) => e.stopPropagation()}>
        <img src={src} alt={alt} />
        {caption && <figcaption>{caption}</figcaption>}
      </figure>
      <button ref={button} type="button" className="m-x m-lightbox-close" onClick={close} aria-label="Close"><Close size={20} /></button>
    </div>,
    document.body,
  );
}

// Voice notes

const BARS = 32;
const shapes = new Map<string, Promise<{ peaks: number[]; duration: number }>>();

/** The note's loudness in bars and its true length, decoded once. */
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

export function VoiceNote({ src, knownMs }: { src: string; knownMs?: number }) {
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
    <div className="m-voice">
      <button type="button" className="m-voice-play" onClick={toggle} aria-label={playing ? "Pause the voice note" : "Play the voice note"}>
        {playing ? <Pause size={16} /> : <Play size={16} />}
      </button>
      <div className="m-voice-bars" onClick={seek} role="presentation">
        {peaks.map((h, i) => (
          <span key={i} style={{ height: `${Math.round(h * 100)}%` }} className={i / peaks.length < done ? "on" : ""} />
        ))}
      </div>
      <span className="m-voice-time">{seconds((playing || at ? at : duration) * 1000)}</span>
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
export function MediaOf({ url, known, knownMs, alt }: { url: string; known?: MediaKind; knownMs?: number; alt: string }) {
  const m = useMedia(url, known);
  if (!m) return <div className="m-media-wait" aria-label="Loading" />;
  if (m.kind === "image") return <Photo src={m.src} alt={alt} />;
  if (m.kind === "audio") return <VoiceNote src={m.src} knownMs={knownMs} />;
  return <a className="o-link m-file" href={m.src} target="_blank" rel="noreferrer">Open the file</a>;
}
