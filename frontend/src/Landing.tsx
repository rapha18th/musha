import { useEffect } from "react";
import { Link, ThemeButton } from "./Bar";
import { go, home, useSession } from "./shell";

/** The sign-in forms, fetched once the landing has painted or the visitor reaches for a door. */
const warm = () => {
  void import("./Auth").catch(() => {});
};

/** One line, two doors. The roofline is the hold: money kept safe until the proof comes home. */
export function Landing({ theme, toggleTheme }: { theme: string; toggleTheme: () => void }) {
  const { session } = useSession();
  useEffect(() => {
    if (session) go(home(session), true);
  }, [session]);
  useEffect(() => {
    const t = window.setTimeout(warm, 1500);
    return () => {
      window.clearTimeout(t);
    };
  }, []);
  return (
    <div className="landing">
      <ThemeButton theme={theme} toggleTheme={toggleTheme} className="theme corner" />
      <div className="landing-center">
        <svg className="roof" viewBox="0 0 240 90" aria-hidden="true">
          <path d="M12 82 L120 12 L228 82" />
          <rect x="104" y="52" width="32" height="30" rx="3" />
        </svg>
        <h1 className="landing-mark">Musha</h1>
        <p className="landing-line">Run your business at home from anywhere.</p>
        <div className="landing-doors">
          <Link className="landing-go" to="/sign-up" onPointerEnter={warm} onFocus={warm}>Create your shop</Link>
          <Link className="landing-in" to="/sign-in" onPointerEnter={warm} onFocus={warm}>Sign in</Link>
        </div>
        <p className="landing-fine">PayPal holds the money. Proof releases it.</p>
      </div>
    </div>
  );
}
