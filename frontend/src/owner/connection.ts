/** Connect your shop: the owner's and the manager's sheet talks to these routes (backend/musha/routes/camera.py and core.py). */
import { call, post } from "../api";
import type { CameraStatus, Frame } from "./api";

export type Source = "phone" | "bridge" | "feed";

export type FeedState = {
  on: boolean;
  since: string | null;
  /** When the feed stopped itself, after half an hour with nobody watching. */
  stopped_at: string | null;
  stills: number;
  next: number;
  pace_seconds: number;
  idle_minutes: number;
  watched: boolean;
  credit: string;
};

export type Connection = CameraStatus & {
  source: Source | null;
  feed: FeedState;
  counter_key: string;
  /** The names the sheet speaks of: the shop, its owner and its manager. */
  shop: string;
  owner: string;
  manager: string;
  latest: Frame | null;
};

export const connectApi = {
  view: (shopId: number) => call<Connection>(`/api/shops/${shopId}/camera/connect`),
  feed: (shopId: number) => post<Connection>(`/api/shops/${shopId}/camera/feed`),
  disconnect: (shopId: number) => post<Connection>(`/api/shops/${shopId}/camera/disconnect`),
};

export const BRIDGE_GUIDE = "https://github.com/rapha18th/musha/tree/main/counter-bridge";

export type CameraKind = { key: string; label: string; rtsp: string };

/** The RTSP address of common cameras: the sub stream, which is plenty for a 640 px still. */
export const CAMERAS: CameraKind[] = [
  { key: "hikvision", label: "Hikvision", rtsp: "rtsp://admin:password@192.168.1.64:554/Streaming/Channels/102" },
  { key: "dahua", label: "Dahua", rtsp: "rtsp://admin:password@192.168.1.108:554/cam/realmonitor?channel=1&subtype=1" },
  { key: "onvif", label: "ONVIF", rtsp: "rtsp://user:password@CAMERA-IP:554/STREAM-PATH" },
  { key: "file", label: "A video file", rtsp: "shop.mp4" },
];

/** The bridge's command, with the counter link filled in. The first run records the manager's agreement. */
export function bridgeCommand(link: string, camera: string, agree: string | null): string[] {
  const name = agree ? ["--agree", "--name", `"${agree.replace(/"/g, "")}"`] : [];
  return ["python", "counter_bridge.py", "--link", `"${link}"`, "--camera", `"${camera}"`, ...name];
}

// QR codes: byte mode, error correction level M, versions 1 to 20. After Nayuki's QR Code generator (MIT).

const ECC_PER_BLOCK = [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26];
const BLOCKS = [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16];
const FORMAT_M = 0; // the format bits' code for level M

function rawModules(ver: number): number {
  let n = (16 * ver + 128) * ver + 64;
  if (ver >= 2) {
    const align = Math.floor(ver / 7) + 2;
    n -= (25 * align - 10) * align - 55;
    if (ver >= 7) n -= 36;
  }
  return n;
}

const dataCodewords = (ver: number) => Math.floor(rawModules(ver) / 8) - ECC_PER_BLOCK[ver] * BLOCKS[ver];

function gfMul(x: number, y: number): number {
  let z = 0;
  for (let i = 7; i >= 0; i--) {
    z = (z << 1) ^ ((z >>> 7) * 0x11d);
    z ^= ((y >>> i) & 1) * x;
  }
  return z & 0xff;
}

function rsDivisor(degree: number): number[] {
  const out = new Array<number>(degree).fill(0);
  out[degree - 1] = 1;
  let root = 1;
  for (let i = 0; i < degree; i++) {
    for (let j = 0; j < degree; j++) {
      out[j] = gfMul(out[j], root);
      if (j + 1 < degree) out[j] ^= out[j + 1];
    }
    root = gfMul(root, 0x02);
  }
  return out;
}

function rsRemainder(data: number[], divisor: number[]): number[] {
  const out = divisor.map(() => 0);
  for (const b of data) {
    const factor = b ^ (out.shift() as number);
    out.push(0);
    divisor.forEach((c, i) => {
      out[i] ^= gfMul(c, factor);
    });
  }
  return out;
}

function alignments(ver: number, size: number): number[] {
  if (ver === 1) return [];
  const n = Math.floor(ver / 7) + 2;
  const step = Math.floor((ver * 8 + n * 3 + 5) / (n * 4 - 4)) * 2;
  const out = [6];
  for (let pos = size - 7; out.length < n; pos -= step) out.splice(1, 0, pos);
  return out;
}

const bit = (x: number, i: number) => ((x >>> i) & 1) !== 0;

const MASKS: ((x: number, y: number) => boolean)[] = [
  (x, y) => (x + y) % 2 === 0,
  (_x, y) => y % 2 === 0,
  (x) => x % 3 === 0,
  (x, y) => (x + y) % 3 === 0,
  (x, y) => (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0,
  (x, y) => ((x * y) % 2) + ((x * y) % 3) === 0,
  (x, y) => (((x * y) % 2) + ((x * y) % 3)) % 2 === 0,
  (x, y) => (((x + y) % 2) + ((x * y) % 3)) % 2 === 0,
];

/** The QR code for `text` as rows of modules, true for dark, without the quiet zone. */
export function qr(text: string): boolean[][] {
  const bytes = Array.from(new TextEncoder().encode(text));
  let ver = 1;
  for (; ver <= 20; ver++) {
    const count = ver < 10 ? 8 : 16;
    if (4 + count + bytes.length * 8 <= dataCodewords(ver) * 8) break;
  }
  if (ver > 20) throw new Error("The link is too long for a code.");
  const capacity = dataCodewords(ver) * 8;

  // the bit stream: mode, length, the bytes, a terminator, then padding
  const bits: number[] = [];
  const put = (value: number, len: number) => {
    for (let i = len - 1; i >= 0; i--) bits.push((value >>> i) & 1);
  };
  put(0b0100, 4);
  put(bytes.length, ver < 10 ? 8 : 16);
  bytes.forEach((b) => put(b, 8));
  put(0, Math.min(4, capacity - bits.length));
  put(0, (8 - (bits.length % 8)) % 8);
  for (let pad = 0xec; bits.length < capacity; pad ^= 0xec ^ 0x11) put(pad, 8);
  const data: number[] = [];
  for (let i = 0; i < bits.length; i += 8) data.push(bits.slice(i, i + 8).reduce((a, b) => (a << 1) | b, 0));

  // error correction, block by block, interleaved
  const nBlocks = BLOCKS[ver];
  const eccLen = ECC_PER_BLOCK[ver];
  const raw = Math.floor(rawModules(ver) / 8);
  const nShort = nBlocks - (raw % nBlocks);
  const shortLen = Math.floor(raw / nBlocks);
  const divisor = rsDivisor(eccLen);
  const blocks: number[][] = [];
  for (let i = 0, k = 0; i < nBlocks; i++) {
    const dat = data.slice(k, k + shortLen - eccLen + (i < nShort ? 0 : 1));
    k += dat.length;
    const ecc = rsRemainder(dat, divisor);
    if (i < nShort) dat.push(0);
    blocks.push(dat.concat(ecc));
  }
  const words: number[] = [];
  for (let i = 0; i < blocks[0].length; i++) {
    blocks.forEach((b, j) => {
      if (i !== shortLen - eccLen || j >= nShort) words.push(b[i]);
    });
  }

  // the grid and its fixed patterns
  const size = ver * 4 + 17;
  const dark = Array.from({ length: size }, () => new Array<boolean>(size).fill(false));
  const fixed = Array.from({ length: size }, () => new Array<boolean>(size).fill(false));
  const set = (x: number, y: number, on: boolean) => {
    dark[y][x] = on;
    fixed[y][x] = true;
  };
  for (let i = 0; i < size; i++) {
    set(6, i, i % 2 === 0);
    set(i, 6, i % 2 === 0);
  }
  for (const [cx, cy] of [[3, 3], [size - 4, 3], [3, size - 4]]) {
    for (let dy = -4; dy <= 4; dy++) {
      for (let dx = -4; dx <= 4; dx++) {
        const x = cx + dx;
        const y = cy + dy;
        const d = Math.max(Math.abs(dx), Math.abs(dy));
        if (x >= 0 && x < size && y >= 0 && y < size) set(x, y, d !== 2 && d !== 4);
      }
    }
  }
  const al = alignments(ver, size);
  al.forEach((ay, i) => al.forEach((ax, j) => {
    if ((i === 0 && j === 0) || (i === 0 && j === al.length - 1) || (i === al.length - 1 && j === 0)) return;
    for (let dy = -2; dy <= 2; dy++) {
      for (let dx = -2; dx <= 2; dx++) set(ax + dx, ay + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
    }
  }));
  const format = (mask: number) => {
    const d = (FORMAT_M << 3) | mask;
    let rem = d;
    for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
    const b = ((d << 10) | rem) ^ 0x5412;
    for (let i = 0; i <= 5; i++) set(8, i, bit(b, i));
    set(8, 7, bit(b, 6));
    set(8, 8, bit(b, 7));
    set(7, 8, bit(b, 8));
    for (let i = 9; i < 15; i++) set(14 - i, 8, bit(b, i));
    for (let i = 0; i < 8; i++) set(size - 1 - i, 8, bit(b, i));
    for (let i = 8; i < 15; i++) set(8, size - 15 + i, bit(b, i));
    set(8, size - 8, true);
  };
  format(0); // reserves the format area before the data goes in
  if (ver >= 7) {
    let rem = ver;
    for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
    const b = (ver << 12) | rem;
    for (let i = 0; i < 18; i++) {
      const a = size - 11 + (i % 3);
      const c = Math.floor(i / 3);
      set(a, c, bit(b, i));
      set(c, a, bit(b, i));
    }
  }

  // the codewords, in the zigzag
  let n = 0;
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    for (let v = 0; v < size; v++) {
      for (let j = 0; j < 2; j++) {
        const x = right - j;
        const y = ((right + 1) & 2) === 0 ? size - 1 - v : v;
        if (!fixed[y][x] && n < words.length * 8) {
          dark[y][x] = bit(words[n >>> 3], 7 - (n & 7));
          n++;
        }
      }
    }
  }

  // the mask with the lowest penalty
  const masked = (m: number) => dark.map((row, y) => row.map((on, x) => (fixed[y][x] ? on : on !== MASKS[m](x, y))));
  let best = 0;
  let bestScore = Infinity;
  for (let m = 0; m < 8; m++) {
    format(m);
    const score = penalty(masked(m));
    if (score < bestScore) {
      best = m;
      bestScore = score;
    }
  }
  format(best);
  return masked(best);
}

function penalty(g: boolean[][]): number {
  const size = g.length;
  let score = 0;
  const lines: string[] = [];
  for (let i = 0; i < size; i++) {
    lines.push(g[i].map((on) => (on ? "1" : "0")).join(""));
    lines.push(g.map((row) => (row[i] ? "1" : "0")).join(""));
  }
  for (const line of lines) {
    for (const run of line.match(/0{5,}|1{5,}/g) || []) score += 3 + run.length - 5;
    for (const pattern of ["10111010000", "00001011101"]) {
      for (let at = line.indexOf(pattern); at >= 0; at = line.indexOf(pattern, at + 1)) score += 40;
    }
  }
  let darkCount = 0;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      if (g[y][x]) darkCount++;
      if (x + 1 < size && y + 1 < size && g[y][x] === g[y][x + 1] && g[y][x] === g[y + 1][x] && g[y][x] === g[y + 1][x + 1]) score += 3;
    }
  }
  const total = size * size;
  score += (Math.ceil(Math.abs(darkCount * 20 - total * 10) / total) - 1) * 10;
  return score;
}

/** The code as one SVG path, with a quiet zone of four modules. */
export function qrPath(text: string): { size: number; d: string } {
  const g = qr(text);
  const parts: string[] = [];
  g.forEach((row, y) => row.forEach((on, x) => {
    if (on) parts.push(`M${x + 4},${y + 4}h1v1h-1z`);
  }));
  return { size: g.length + 8, d: parts.join("") };
}
