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
