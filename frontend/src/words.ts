/** "tray" from "trays", "loaf" from "loaves", "2 kg pack" from "2 kg packs": the last word made singular. */
function singular(unit: string): string {
  return unit.replace(/(\w+)$/, (w) => {
    if (/ves$/i.test(w)) return w.slice(0, -3) + "f";
    if (/ies$/i.test(w)) return w.slice(0, -3) + "y";
    if (/(ch|sh|x|ss)es$/i.test(w)) return w.slice(0, -2);
    if (/[^s]s$/i.test(w)) return w.slice(0, -1);
    return w;
  });
}

/**
 * A count in the shop's unit: "40 trays", "1 tray", and "24 × 2 L bottles" when the unit
 * starts with its own number. Split into the count and the rest, so the count can be set in bold.
 */
export function qtyParts(q: number, unit: string): [string, string] {
  const u = (Math.abs(q) === 1 ? singular(unit.trim()) : unit.trim());
  if (!u) return [String(q), ""];
  return [String(q), /^\d/.test(u) ? `× ${u}` : u];
}

export const qty = (q: number, unit: string) => qtyParts(q, unit).filter(Boolean).join(" ");
