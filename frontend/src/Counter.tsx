import { CounterCam } from "./phone/CounterCam";
import "./phone/phone.css";

/** The counter camera page, for a phone propped on the shop counter. */
export function Counter({ shopId }: { shopId: number }) {
  return (
    <div className="cc-page">
      <header className="cc-top">
        <a className="cc-brand" href={`#/shop?shop=${shopId}`}>Musha</a>
        <span>Counter camera</span>
      </header>
      <main className="cc-main">
        <CounterCam shopId={shopId} />
      </main>
    </div>
  );
}

export default Counter;
