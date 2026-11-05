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
              <p className="cc-consent-text">Open the counter link on this phone. The owner's Connect your shop gives it as a link and a code to scan.</p>
              <p className="cc-fine">The shop's manager can <Link to="/sign-in">sign in</Link> and start the camera from the dashboard.</p>
            </div>
          </div>
        ) : <CounterCam shopId={shopId} />}
      </main>
    </div>
  );
}

export default Counter;
