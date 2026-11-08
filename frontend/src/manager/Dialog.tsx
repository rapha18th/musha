import { useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Close } from "../phone/icons";

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Everything outside `el` goes inert while it is open: the siblings of `el` and of each of its ancestors.
 * Returns the undo. Elements that were inert already stay inert.
 */
function isolate(el: HTMLElement): () => void {
  const made: Element[] = [];
  for (let node: HTMLElement | null = el; node && node !== document.body; node = node.parentElement) {
    const parent: HTMLElement | null = node.parentElement;
    if (!parent) break;
    for (const sib of Array.from(parent.children)) {
      if (sib === node || sib.hasAttribute("inert")) continue;
      sib.setAttribute("inert", "");
      made.push(sib);
    }
  }
  return () => {
    made.forEach((n) => n.removeAttribute("inert"));
  };
}

/**
 * The dashboard's dialog: centred on a wide screen, a sheet from the bottom on a phone. It holds focus while
 * open, closes on Escape or a press outside, and hands focus back to the button that opened it.
 */
export function Dialog({ title, sub, close, children, footer }: {
  title: string;
  sub?: ReactNode;
  close: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const panel = useRef<HTMLElement>(null);
  const shut = useRef(close);
  shut.current = close;
  const id = useId();

  useEffect(() => {
    const el = panel.current;
    if (!el) return;
    const opener = document.activeElement instanceof HTMLElement && document.activeElement !== document.body
      ? document.activeElement : null;
    // The page behind the veil leaves the tab order and the accessibility tree.
    const undo = isolate(el);
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    el.focus({ preventScroll: true });
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        shut.current();
        return;
      }
      if (e.key !== "Tab") return;
      const items = Array.from(el.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((n) => n.offsetParent !== null || n === document.activeElement);
      if (!items.length) {
        e.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const at = document.activeElement;
      if (e.shiftKey && (at === first || at === el)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && at === last) {
        e.preventDefault();
        first.focus();
      }
    };
    el.addEventListener("keydown", key);
    return () => {
      el.removeEventListener("keydown", key);
      undo();
      document.body.style.overflow = overflow;
      // Back to the button that opened the dialog; when it has gone, to the message box or the open tab.
      const back = opener?.isConnected ? opener
        : document.querySelector<HTMLElement>(".m-compose .m-input") ?? document.querySelector<HTMLElement>('.m-tabs [aria-selected="true"]');
      back?.focus({ preventScroll: true });
    };
  }, []);

  return createPortal(
    <div className="m-veil" onMouseDown={(e) => {
      if (e.target === e.currentTarget) shut.current();
    }}>
      <section className="m-dialog" role="dialog" aria-modal="true" aria-labelledby={`${id}-t`} ref={panel} tabIndex={-1}>
        <header className="m-dialog-head">
          <div>
            <h2 id={`${id}-t`}>{title}</h2>
            {sub && <p>{sub}</p>}
          </div>
          <button type="button" className="m-x" onClick={() => shut.current()} aria-label="Close"><Close size={18} /></button>
        </header>
        <div className="m-dialog-body">{children}</div>
        {footer && <footer className="m-dialog-foot">{footer}</footer>}
      </section>
    </div>,
    document.body,
  );
}

/** Three dots while a dialog reads what it needs. */
export const Loading = () => <div className="m-loading" role="status" aria-label="Loading"><span /><span /><span /></div>;

/** A row of choices, one pressed. */
export function Segmented<T extends string>({ value, options, onChange, label }: {
  value: T;
  options: { key: T; label: string }[];
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <div className="m-seg" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button key={o.key} type="button" role="radio" aria-checked={value === o.key} onClick={() => onChange(o.key)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}
