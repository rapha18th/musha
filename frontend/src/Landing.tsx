import { useEffect } from "react";
import { go } from "./shell";

/** The shop's code, fetched once the landing has painted or the visitor reaches for the door. */
const warm = () => {
  void import("./Shop").catch(() => {});
};

/** One line, one door. The roofline is the hold: money kept safe until the proof comes home. */
export function Landing({ theme, toggleTheme }: { theme: string; toggleTheme: () => void }) {
  const next = theme === "dark" ? "Light" : "Dark";
  useEffect(() => {
    const t = window.setTimeout(warm, 1500);
    return () => {
      window.clearTimeout(t);
    };
  }, []);
  return (
    <div className="landing">
      <button className="theme corner" onClick={toggleTheme} aria-label={`${next} theme`}>{next}</button>
      <div className="landing-center">
        <svg className="roof" viewBox="0 0 240 90" aria-hidden="true">
          <path d="M12 82 L120 12 L228 82" />
          <rect x="104" y="52" width="32" height="30" rx="3" />
        </svg>
        <h1 className="landing-mark">Musha</h1>
        <p className="landing-line">Run your business at home from anywhere.</p>
        <button className="landing-go" onPointerEnter={warm} onFocus={warm} onClick={() => go("/shop")}>Open the shop</button>
        <p className="landing-fine">PayPal holds the money. Proof releases it.</p>
      </div>
    </div>
  );
}
