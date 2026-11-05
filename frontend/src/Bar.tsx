import type { AnchorHTMLAttributes, MouseEvent } from "react";
import { go, signOut } from "./shell";

const BASE = (import.meta.env.BASE_URL || "/").replace(/\/$/, "");

/** A link inside the app: a real link for new tabs and copying, a page change in place for a plain click. */
export function Link({ to, onClick, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement> & { to: string }) {
  const open = (e: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(e);
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    go(to);
  };
  return <a href={`${BASE}${to}`} onClick={open} {...rest} />;
}

/** The theme switch, named for the theme it turns on. */
export function ThemeButton({ theme, toggleTheme, className = "theme" }: { theme: string; toggleTheme: () => void; className?: string }) {
  const next = theme === "dark" ? "Light" : "Dark";
  return <button type="button" className={className} onClick={toggleTheme} aria-label={`${next} theme`}>{next}</button>;
}

/** The top of a dashboard: the mark, who is signed in, the theme and Sign out. */
export function Bar({ who, theme, toggleTheme, tag = true }: { who: string; theme: string; toggleTheme: () => void; tag?: boolean }) {
  return (
    <header className="top">
      <Link className="brand" to="/">
        <span className="mark">Musha</span>
        {tag && <span className="tag">Run your business at home from anywhere</span>}
      </Link>
      <div className="top-right">
        <span className="who">{who}</span>
        <ThemeButton theme={theme} toggleTheme={toggleTheme} />
        <button type="button" className="theme signout" onClick={() => void signOut()}>Sign out</button>
      </div>
    </header>
  );
}
