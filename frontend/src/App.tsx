import { Component, lazy, Suspense, type ReactNode } from "react";
import { api } from "./api";
import { Landing } from "./Landing";
import { useRetry, useRoute, useShopId, useTheme } from "./shell";
import { Waiting } from "./Waiting";

// The landing paints from the entry chunk alone; the shop, its console and its grids load behind it.
const Shop = lazy(() => import("./Shop"));
const Counter = lazy(() => import("./Counter"));

export default function App() {
  const route = useRoute();
  const [theme, toggleTheme] = useTheme();
  if (route.name === "landing") {
    return <Landing theme={theme} toggleTheme={toggleTheme} />;
  }
  return (
    <Reloads>
      <Opening route={route.name} theme={theme} toggleTheme={toggleTheme} />
    </Reloads>
  );
}

/** Finds the visitor's shop and the app's settings, trying again until the server answers. */
function Opening({ route, theme, toggleTheme }: { route: "shop" | "counter"; theme: string; toggleTheme: () => void }) {
  const shop = useShopId(route === "counter" ? "counter" : "owner");
  const config = useRetry(api.config, route === "shop");
  const retry = () => {
    shop.retry();
    config.retry();
  };
  const failures = Math.max(shop.failures, config.failures);
  const shopId = shop.value;
  if (!shopId || (route === "shop" && !config.value)) {
    return <Waiting failures={failures} retry={retry} />;
  }
  return (
    <Suspense fallback={<Waiting />}>
      {route === "counter"
        ? <Counter shopId={shopId} />
        : <Shop shopId={shopId} config={config.value!} theme={theme} toggleTheme={toggleTheme} />}
    </Suspense>
  );
}

/** A chunk that fails to load (a new deploy, a dropped connection) offers a reload in place of a blank page. */
class Reloads extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="loading" role="alert">
        <span className="loading-mark">Musha</span>
        <p className="loading-line">Musha needs a fresh copy of the page.</p>
        <button type="button" className="loading-retry" onClick={() => window.location.reload()}>Reload</button>
      </div>
    );
  }
}
