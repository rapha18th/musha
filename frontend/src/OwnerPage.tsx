import { useEffect } from "react";
import { PayPalProvider } from "@paypal/react-paypal-js/sdk-v6";
import type { Config, Session } from "./api";
import { Bar } from "./Bar";
import { Owner } from "./Owner";
import { go, setSession } from "./shell";
import { useShop } from "./useShop";
import { Waiting } from "./Waiting";

/** Chipo's page: her console for one shop, the whole width of the window. */
export default function OwnerPage({ session, config, theme, toggleTheme }: {
  session: Session;
  config: Config;
  theme: string;
  toggleTheme: () => void;
}) {
  const { shop, requests, entries, misses, lost, refresh } = useShop(session.shop.id, { books: true });

  useEffect(() => {
    if (!lost) return;
    setSession(null);
    go("/sign-in", true);
  }, [lost]);

  if (!shop) return <Waiting failures={misses} />;

  const standin = config.paypal_mode === "standin";
  const owner = <Owner shop={shop} requests={requests} entries={entries} refresh={refresh} config={config} />;

  return (
    <div className="app app-owner">
      <Bar who={session.person.name} theme={theme} toggleTheme={toggleTheme} />
      {standin && (
        <p className="rehearsal">Rehearsal. A stand-in answers for PayPal. Add sandbox keys to move sandbox money.</p>
      )}
      <main>
        {config.paypal_client_id && !standin ? (
          <PayPalProvider clientId={config.paypal_client_id} environment="sandbox" components={["paypal-payments"]} pageType="checkout">
            {owner}
          </PayPalProvider>
        ) : owner}
      </main>
    </div>
  );
}
