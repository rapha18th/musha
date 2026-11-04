// Line icons for the phone, drawn on a 24 grid with the current text colour.
import type { ReactNode } from "react";
type P = { size?: number };

const svg = (size: number, children: ReactNode, fill = false) => (
  <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" fill={fill ? "currentColor" : "none"}
    stroke={fill ? "none" : "currentColor"} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    {children}
  </svg>
);

export const Clip = ({ size = 22 }: P) =>
  svg(size, <path d="M20.5 11.5 12.4 19.6a5 5 0 0 1-7-7l8.5-8.5a3.3 3.3 0 0 1 4.7 4.7l-8.4 8.4a1.7 1.7 0 0 1-2.4-2.4l7.7-7.7" />);

export const Mic = ({ size = 22 }: P) =>
  svg(size, <>
    <rect x="9" y="3" width="6" height="11" rx="3" />
    <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" />
  </>);

export const Send = ({ size = 20 }: P) =>
  svg(size, <path d="M4.5 12 3 4.5 21 12 3 19.5 4.5 12Zm0 0H12" />);

export const Camera = ({ size = 20 }: P) =>
  svg(size, <>
    <path d="M4 8h3l1.6-2.2h6.8L17 8h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1Z" />
    <circle cx="12" cy="13" r="3.4" />
  </>);

export const Receipt = ({ size = 20 }: P) =>
  svg(size, <>
    <path d="M6 3h12v18l-2-1.4-2 1.4-2-1.4-2 1.4-2-1.4L6 21V3Z" />
    <path d="M9 8h6M9 11.5h6M9 15h3.5" />
  </>);

export const Box = ({ size = 20 }: P) =>
  svg(size, <>
    <path d="M3.5 7.5 12 3l8.5 4.5v9L12 21l-8.5-4.5v-9Z" />
    <path d="M3.5 7.5 12 12l8.5-4.5M12 12v9" />
  </>);

export const Close = ({ size = 18 }: P) => svg(size, <path d="M6 6l12 12M18 6 6 18" />);

export const Play = ({ size = 16 }: P) => svg(size, <path d="M7 4.5v15l12.5-7.5L7 4.5Z" />, true);

export const Pause = ({ size = 16 }: P) =>
  svg(size, <>
    <rect x="6" y="4.5" width="4" height="15" rx="1" />
    <rect x="14" y="4.5" width="4" height="15" rx="1" />
  </>, true);

export const Reply = ({ size = 14 }: P) => svg(size, <path d="M9.5 7 4.5 12l5 5M5 12h9a5.5 5.5 0 0 1 5.5 5.5V19" />);

export const Clock = ({ size = 12 }: P) =>
  svg(size, <>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M12 7.5V12l3 2" />
  </>);

export const Ticks = ({ size = 14 }: P) => svg(size, <path d="M2.5 12.5 6.5 16.5 14 8M10 15.5l1 1L18.5 8" />);

export const Retry = ({ size = 12 }: P) => svg(size, <path d="M20 12a8 8 0 1 1-2.3-5.7M20 4.5v4h-4" />);

export const Chevron = ({ size = 14 }: P) => svg(size, <path d="m9 6 6 6-6 6" />);

export const Link = ({ size = 16 }: P) =>
  svg(size, <path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" />);

// The chat's own glyphs, drawn for Musha's WhatsApp-style phone.

export const Back = ({ size = 22 }: P) => svg(size, <path d="M19.5 12H5M11 5.5 4.5 12l6.5 6.5" />);

export const Video = ({ size = 22 }: P) =>
  svg(size, <>
    <rect x="2.5" y="6" width="13" height="12" rx="2.5" />
    <path d="m15.5 10.2 5.3-3v9.6l-5.3-3" />
  </>);

export const Call = ({ size = 20 }: P) =>
  svg(size, <path d="M6.6 3.5h-2a1.5 1.5 0 0 0-1.5 1.6 16.5 16.5 0 0 0 15.8 15.8 1.5 1.5 0 0 0 1.6-1.5v-2a1.5 1.5 0 0 0-1.2-1.5l-3.2-.7a1.5 1.5 0 0 0-1.4.4l-1.2 1.2a12 12 0 0 1-5.3-5.3l1.2-1.2a1.5 1.5 0 0 0 .4-1.4L8.1 4.7a1.5 1.5 0 0 0-1.5-1.2Z" />);

export const Dots = ({ size = 22 }: P) =>
  svg(size, <>
    <circle cx="12" cy="5.5" r="1.7" />
    <circle cx="12" cy="12" r="1.7" />
    <circle cx="12" cy="18.5" r="1.7" />
  </>, true);

export const Smile = ({ size = 24 }: P) =>
  svg(size, <>
    <circle cx="12" cy="12" r="9" />
    <path d="M8.2 14.2a4.6 4.6 0 0 0 7.6 0" />
    <circle cx="9" cy="9.8" r=".6" fill="currentColor" />
    <circle cx="15" cy="9.8" r=".6" fill="currentColor" />
  </>);

export const MicFill = ({ size = 22 }: P) =>
  svg(size, <>
    <rect x="8.6" y="2.5" width="6.8" height="12" rx="3.4" />
    <path d="M5.6 11.2a6.4 6.4 0 0 0 12.8 0h-1.7a4.7 4.7 0 0 1-9.4 0H5.6Zm5.55 6.85h1.7V21.5h-1.7z" />
  </>, true);

export const SendFill = ({ size = 22 }: P) =>
  svg(size, <path d="M3.4 20.6 21.5 12 3.4 3.4l-.02 6.7L16 12 3.38 13.9l.02 6.7Z" />, true);

export const Tick = ({ size = 16 }: P) => svg(size, <path d="m4.5 12.5 4 4 9-9.5" />);

export const Alert = ({ size = 14 }: P) =>
  svg(size, <>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7.5v5.5M12 16.4v.1" />
  </>);

export const Doc = ({ size = 22 }: P) =>
  svg(size, <>
    <path d="M6 2.8h8.2L19 7.6V20a1.2 1.2 0 0 1-1.2 1.2H6A1.2 1.2 0 0 1 4.8 20V4A1.2 1.2 0 0 1 6 2.8Z" />
    <path d="M14 3v4.8h4.8M8.5 12.5h7M8.5 16h5" />
  </>);

export const Gallery = ({ size = 22 }: P) =>
  svg(size, <>
    <rect x="3" y="4" width="18" height="16" rx="2.5" />
    <circle cx="9" cy="9.5" r="1.8" />
    <path d="m3.5 17.5 5-5 4 4 2.5-2.5 5.5 5.5" />
  </>);

export const Wallet = ({ size = 20 }: P) =>
  svg(size, <>
    <path d="M4 6.5h14.5a1.5 1.5 0 0 1 1.5 1.5v10a1.5 1.5 0 0 1-1.5 1.5H5.5A1.5 1.5 0 0 1 4 18V6.5Zm0 0 11-3v3" />
    <path d="M15.5 13.2h1.5" />
  </>);

export const Spark = ({ size = 22 }: P) =>
  svg(size, <path d="M12 3.5c.6 4.2 2.3 6 6.5 6.5-4.2.6-5.9 2.3-6.5 6.5-.6-4.2-2.3-5.9-6.5-6.5 4.2-.5 5.9-2.3 6.5-6.5ZM18.5 15.5c.3 1.9 1 2.6 2.9 2.9-1.9.3-2.6 1-2.9 2.9-.3-1.9-1-2.6-2.9-2.9 1.9-.3 2.6-1 2.9-2.9Z" />);

/** Musha's own mark, the roofline M, for the chat's avatar. */
export const Mark = ({ size = 22 }: P) => (
  <svg width={size} height={size} viewBox="0 0 64 64" aria-hidden="true" fill="none" stroke="currentColor"
    strokeWidth="6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M14 46V20l18 15 18-15v26" />
  </svg>
);
