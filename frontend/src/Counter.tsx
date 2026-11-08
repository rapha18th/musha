import { Link } from "./Bar";
import { CounterCam } from "./phone/CounterCam";
import "./phone/phone.css";

/** The counter camera page, for a phone propped on the shop counter. Without a shop, it asks for the counter link. */
export function Counter({ shopId }: { shopId: number | null }) {
  return (
    <div className="cc-page">
      <header className="cc-top">
        <Link className="cc-brand" to="/">Musha</Link>
        <span>Counter camera</span>
      </header>
      <main className="cc-main">
        {shopId === null ? (
          <div className="cc cc-consent">
            <div className="cc-consent-in" role="status">
              <h2>Counter camera</h2>
              <p className="cc-consent-text">Open the counter link on this phone. Connect your shop gives it as a code to scan and a link to copy.</p>
              <p className="cc-fine">The owner and the manager each find it on their dashboard once they <Link to="/sign-in">sign in</Link>.</p>
            </div>
          </div>
        ) : <CounterCam shopId={shopId} />}
      </main>
    </div>
  );
}

export default Counter;
