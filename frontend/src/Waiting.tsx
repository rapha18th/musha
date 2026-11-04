/** The word while the shop opens; a line once a try has missed; a button once a few have. */
export function Waiting({ failures = 0, retry }: { failures?: number; retry?: () => void }) {
  return (
    <div className="loading" role="status" aria-live="polite">
      <span className="loading-mark">Musha</span>
      {failures > 0 && (
        <p className="loading-line">
          {failures < 3 ? "Musha is opening the shop." : "The server is waking up. Musha keeps trying."}
        </p>
      )}
      {failures >= 3 && retry && <button type="button" className="loading-retry" onClick={retry}>Try again</button>}
    </div>
  );
}
