import { Component, lazy, Suspense, useEffect, type ReactNode } from "react";
import { api } from "./api";
import { Landing } from "./Landing";
import { go, home, useCounterShop, useRetry, useRoute, useSession, useTheme } from "./shell";
import { Waiting } from "./Waiting";

// The landing paints from the entry chunk alone; the forms, each dashboard and the counter load behind it.
const SignIn = lazy(() => import("./Auth").then((m) => ({ default: m.SignIn })));
const SignUp = lazy(() => import("./Auth").then((m) => ({ default: m.SignUp })));
const InvitePage = lazy(() => import("./Auth").then((m) => ({ default: m.InvitePage })));
const OwnerPage = lazy(() => import("./OwnerPage"));
const ManagerPage = lazy(() => import("./ManagerPage"));
const Counter = lazy(() => import("./Counter"));

export default function App() {
  const route = useRoute();
  const [theme, toggleTheme] = useTheme();
  const themed = { theme, toggleTheme };
  if (route.name === "landing") return <Landing {...themed} />;
  return (
    <Reloads>
      <Suspense fallback={<Waiting />}>
        {route.name === "sign-in" && <SignIn {...themed} />}
        {route.name === "sign-up" && <SignUp {...themed} />}
        {route.name === "invite" && <InvitePage token={route.token} {...themed} />}
        {(route.name === "owner" || route.name === "manager") && <Dashboard role={route.name} {...themed} />}
        {route.name === "counter" && <CounterOpening />}
      </Suspense>
    </Reloads>
  );
}

/**
 * A person's own page. Nobody signed in goes to Sign in; the owner at /manager, or the manager at /owner,
 * goes to their own. The console waits for the app's settings, PayPal's among them.
 */
function Dashboard({ role, theme, toggleTheme }: { role: "owner" | "manager"; theme: string; toggleTheme: () => void }) {
  const { session, failures, retry } = useSession();
  const config = useRetry(api.config);
  useEffect(() => {
    if (session === null) go("/sign-in", true);
    else if (session && session.person.role !== role) go(home(session), true);
  }, [session, role]);
  const ready = !!session && session.person.role === role && (role === "manager" || !!config.value);
  if (!ready) {
    return (
      <Waiting failures={Math.max(failures, config.failures)} retry={() => {
        retry();
        config.retry();
      }} />
    );
  }
  return role === "owner"
    ? <OwnerPage session={session!} config={config.value!} theme={theme} toggleTheme={toggleTheme} />
    : <ManagerPage session={session!} theme={theme} toggleTheme={toggleTheme} />;
}

/** The counter phone's page: its shop from the counter link, trying again until the server answers. */
function CounterOpening() {
  const shop = useCounterShop();
  if (shop.value === undefined) return <Waiting failures={shop.failures} retry={shop.retry} />;
  return <Counter shopId={shop.value} />;
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
