// The chat's doodle wallpaper: small line drawings of a tuck shop's day, tiled behind the thread.
// Drawn here on a 24 grid and scattered over one 300 px tile.

const GLYPHS: Record<string, string> = {
  house: "M4 11.5 12 4.5l8 7M6 9.8V20h12V9.8M10 20v-5h4v5",
  egg: "M12 3.2c3.4 0 6.2 6 6.2 10a6.2 6.2 0 0 1-12.4 0c0-4 2.8-10 6.2-10Z",
  bread: "M6 20V11a3.6 3.6 0 0 1-1-6.6C6 3.8 8 3.5 12 3.5s6 .3 7 .9A3.6 3.6 0 0 1 18 11v9H6ZM10 8v2.5M14 8v2.5",
  bottle: "M10 2.8h4v3.4l2 3V20a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V9.2l2-3V2.8ZM8 13h8",
  coin: "M12 3.5a8.5 8.5 0 1 1 0 17 8.5 8.5 0 0 1 0-17ZM14.6 9.3c-.6-.8-1.5-1.2-2.6-1.2-1.4 0-2.5.8-2.5 1.9 0 2.6 5 1.4 5 4.1 0 1.1-1.1 1.9-2.5 1.9-1.1 0-2-.4-2.6-1.2M12 6.6v10.8",
  chat: "M4.5 5h15a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H10l-4.5 3.5V16h-1a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1ZM8 9.5h8M8 12.5h5",
  heart: "M12 19.5s-7-4.3-7-9.8a3.9 3.9 0 0 1 7-2.4 3.9 3.9 0 0 1 7 2.4c0 5.5-7 9.8-7 9.8Z",
  star: "m12 3.8 2.5 5.1 5.6.8-4 3.9 1 5.6L12 16.6l-5.1 2.6 1-5.6-4-3.9 5.6-.8L12 3.8Z",
  leaf: "M5 19C5 10.5 10.5 5 19 5c0 8.5-5.5 14-14 14ZM5 19l7.5-7.5",
  phone: "M8.5 3h7A1.5 1.5 0 0 1 17 4.5v15a1.5 1.5 0 0 1-1.5 1.5h-7A1.5 1.5 0 0 1 7 19.5v-15A1.5 1.5 0 0 1 8.5 3ZM11 18h2",
  basket: "M3.5 10h17l-2.2 9.2a1 1 0 0 1-1 .8H6.7a1 1 0 0 1-1-.8L3.5 10ZM8 10l3-5.5M16 10l-3-5.5M9 13v3.5M12 13v3.5M15 13v3.5",
  sun: "M12 8.3a3.7 3.7 0 1 1 0 7.4 3.7 3.7 0 0 1 0-7.4ZM12 2.8v2.2M12 19v2.2M2.8 12H5M19 12h2.2M5.5 5.5 7 7M17 17l1.5 1.5M5.5 18.5 7 17M17 7l1.5-1.5",
  receipt: "M6 3h12v18l-2-1.4-2 1.4-2-1.4-2 1.4-2-1.4L6 21V3ZM9 8h6M9 11.5h6M9 15h3.5",
  cup: "M5 8h11v6.5A4.5 4.5 0 0 1 11.5 19h-2A4.5 4.5 0 0 1 5 14.5V8ZM16 10h1.8a2 2 0 0 1 0 4H16M8.5 3.5v2M12 3.5v2",
  tomato: "M12 7.5c-3.8-1.8-7.5.8-7 5.6.5 4.7 3.8 6.9 7 6 3.2.9 6.5-1.3 7-6 .5-4.8-3.2-7.4-7-5.6ZM12 7.5c0-1.9.9-3.2 3-4M9.5 6.5 12 7.5l2.5-1",
  key: "M8 10.5a4 4 0 1 1 0 .1ZM11.8 11.8 20 20M16.5 16.5l2-2M18.5 18.5l1.5-1.5",
  bag: "M5.5 8h13l-1 12.5h-11L5.5 8ZM9 8V6.5a3 3 0 0 1 6 0V8",
};

type Place = [glyph: string, x: number, y: number, rotate: number, scale: number];

// Each glyph once or twice, tilted, with room between them.
const PLACES: Place[] = [
  ["house", 14, 10, -8, 1.15],
  ["egg", 92, 22, 14, 1],
  ["coin", 168, 6, -12, 1.05],
  ["chat", 240, 26, 8, 1.1],
  ["bread", 38, 78, 10, 1.1],
  ["star", 120, 84, -16, .9],
  ["bottle", 196, 76, 18, 1.1],
  ["heart", 262, 104, -10, .85],
  ["leaf", 6, 146, 20, 1],
  ["phone", 82, 150, -14, 1],
  ["basket", 150, 146, 6, 1.15],
  ["sun", 226, 158, 0, 1.05],
  ["receipt", 32, 216, -6, 1.05],
  ["cup", 104, 222, 12, 1.05],
  ["tomato", 176, 214, -10, 1],
  ["key", 246, 230, 24, .95],
  ["bag", 266, 270, -8, .8],
  ["egg", 140, 272, -24, .75],
];

// Small marks in the gaps, the way a doodle page fills its corners.
const DOTS: [number, number, number][] = [
  [66, 8, 1.6], [146, 54, 1.3], [226, 6, 1.4], [292, 64, 1.2], [70, 124, 1.4], [132, 118, 1.1],
  [208, 128, 1.5], [290, 190, 1.4], [16, 196, 1.2], [88, 196, 1.2], [160, 194, 1.3], [222, 210, 1.1],
  [12, 276, 1.4], [76, 268, 1.3], [210, 276, 1.4], [292, 150, 1.1],
];

function tile(stroke: string, opacity: number): string {
  const marks = PLACES.map(([g, x, y, r, s]) =>
    `<path transform="translate(${x} ${y}) rotate(${r} 12 12) scale(${s})" d="${GLYPHS[g]}"/>`).join("");
  const dots = DOTS.map(([x, y, r]) => `<circle cx="${x}" cy="${y}" r="${r}"/>`).join("");
  return `<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300" viewBox="0 0 300 300">`
    + `<g fill="none" stroke="${stroke}" stroke-opacity="${opacity}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${marks}</g>`
    + `<g fill="${stroke}" fill-opacity="${opacity}">${dots}</g></svg>`;
}

const url = (svg: string) => `url("data:image/svg+xml,${encodeURIComponent(svg)}")`;

/** The wallpaper for each theme, as CSS custom properties for the phone. */
export const WALLPAPER = {
  "--wa-wall-light": url(tile("#5c4a2e", 0.15)),
  "--wa-wall-dark": url(tile("#e9edef", 0.06)),
} as Record<string, string>;
