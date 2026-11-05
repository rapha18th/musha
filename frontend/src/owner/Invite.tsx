import { useState } from "react";
import { ApiError, auth, type Shop } from "../api";
import { ErrorLine, Spinner } from "./parts";

/**
 * A new invite link for the manager, for a link lost or past its 14 days. The server answers 409 once
 * the manager has her sign-in, and the block says so.
 */
export function InviteManager({ shop }: { shop: Shop }) {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const [joined, setJoined] = useState(false);
  const name = shop.manager.name;

  const make = async () => {
    setBusy(true);
    setError("");
    try {
      const out = await auth.newInvite(shop.id);
      setUrl(new URL(out.invite.url, window.location.origin).href);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setJoined(true);
      else setError(e instanceof Error ? e.message : String(e));
    }
    setBusy(false);
  };

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      window.setTimeout(() => {
        setCopied(false);
      }, 2000);
    } catch {
      setCopied(false);
    }
  };

  if (joined) return <p className="o-fine o-leaf">{name} has a sign-in. {name}'s requests arrive here.</p>;

  return (
    <div className="o-invite">
      <p className="o-muted">{name} signs in from your invite link. A new link replaces the last one and works once, within 14 days.</p>
      {url ? (
        <div className="o-invite-link">
          <input readOnly value={url} aria-label={`${name}'s invite link`} onFocus={(e) => e.currentTarget.select()} />
          <button type="button" className="o-btn o-small o-outline" onClick={copy}>{copied ? "Copied" : "Copy"}</button>
        </div>
      ) : (
        <button type="button" className="o-btn o-small o-outline" onClick={make} disabled={busy}>
          {busy ? <><Spinner /> Making the link</> : `New invite link for ${name}`}
        </button>
      )}
      <ErrorLine error={error} />
    </div>
  );
}
