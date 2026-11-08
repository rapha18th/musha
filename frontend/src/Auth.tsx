import { useEffect, useId, useState, type FormEvent, type InputHTMLAttributes, type ReactNode } from "react";
import { ApiError, auth, type Invite, type SignUp as SignUpBody } from "./api";
import { Link, ThemeButton } from "./Bar";
import { go, home, setSession, useRetry, useSession } from "./shell";
import "./auth.css";

const MIN_PASSWORD = 10;
const README = "https://github.com/rapha18th/musha#readme";

type Themed = { theme: string; toggleTheme: () => void };

/** A page of its own for one form: the roofline, a title and the card. */
function Page({ title, lede, children, wide = false, theme, toggleTheme }: Themed & {
  title: string;
  lede?: ReactNode;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <div className="au">
      <ThemeButton theme={theme} toggleTheme={toggleTheme} className="theme corner" />
      <main className={`au-card${wide ? " au-wide" : ""}`}>
        <Link to="/" className="au-home" aria-label="Musha, home">
          <svg className="au-roof" viewBox="0 0 240 90" aria-hidden="true">
            <path d="M12 82 L120 12 L228 82" />
            <rect x="104" y="52" width="32" height="30" rx="3" />
          </svg>
          <span className="au-mark">Musha</span>
        </Link>
        <h1 className="au-title">{title}</h1>
        {lede && <p className="au-lede">{lede}</p>}
        {children}
      </main>
    </div>
  );
}

function Field({ label, hint, ...input }: InputHTMLAttributes<HTMLInputElement> & { label: string; hint?: string }) {
  const id = useId();
  return (
    <div className="au-field">
      <label htmlFor={id}>{label}</label>
      <input id={id} aria-describedby={hint ? `${id}-hint` : undefined} {...input} />
      {hint && <p className="au-hint" id={`${id}-hint`}>{hint}</p>}
    </div>
  );
}

/** A password with a Show switch, so a phone keyboard's typo is easy to catch. */
function Password({ label, hint, ...input }: InputHTMLAttributes<HTMLInputElement> & { label: string; hint?: string }) {
  const id = useId();
  const [shown, setShown] = useState(false);
  return (
    <div className="au-field">
      <label htmlFor={id}>{label}</label>
      <div className="au-pass">
        <input id={id} type={shown ? "text" : "password"} aria-describedby={hint ? `${id}-hint` : undefined} {...input} />
        <button type="button" className="au-show" onClick={() => setShown((s) => !s)} aria-pressed={shown}
          aria-label={shown ? "Hide the password" : "Show the password"}>
          {shown ? "Hide" : "Show"}
        </button>
      </div>
      {hint && <p className="au-hint" id={`${id}-hint`}>{hint}</p>}
    </div>
  );
}

function Problem({ text }: { text: string }) {
  return <p className="au-error" role="alert">{text}</p>;
}

const said = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** Sign in: email and password, then the person's own dashboard. */
export function SignIn(props: Themed) {
  const { session } = useSession();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (session) go(home(session), true);
  }, [session]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const s = await auth.signIn(email.trim(), password);
      setSession(s);
      go(home(s), true);
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? "Check the email and password, then try again." : said(err));
      setBusy(false);
    }
  };

  return (
    <Page title="Sign in" {...props}>
      <form className="au-form" onSubmit={submit}>
        <Field label="Email" type="email" autoComplete="username" inputMode="email" required value={email}
          onChange={(e) => setEmail(e.target.value)} />
        <Password label="Password" autoComplete="current-password" required value={password}
          onChange={(e) => setPassword(e.target.value)} />
        {error && <Problem text={error} />}
        <button type="submit" className="au-go" disabled={busy}>{busy ? "Signing in" : "Sign in"}</button>
      </form>
      <p className="au-alt">New to Musha? <Link to="/sign-up">Create your shop</Link></p>
      <p className="au-fine">The sandbox shop's passwords for Chipo and Farai are in the submission notes. The <a href={README} target="_blank" rel="noreferrer">README</a> walks through it.</p>
    </Page>
  );
}

/** "+263 77 123 4567", "00263771234567" or "+263-77-123-4567" as "+263771234567". */
function e164(raw: string): string {
  return raw.trim().replace(/[\s().-]/g, "").replace(/^00/, "+");
}

const E164 = /^\+[1-9]\d{7,14}$/;

type Made = { url: string; manager: string; shop: string };

/** Create your shop: the owner, the shop, and who runs it. The manager gets an invite link to set her own sign-in. */
export function SignUp(props: Themed) {
  const [f, setF] = useState({
    name: "", email: "", password: "", shop_name: "", place: "", manager_name: "", manager_phone: "", manager_email: "",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [made, setMade] = useState<Made>();
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF((x) => ({ ...x, [k]: e.target.value }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const phone = e164(f.manager_phone);
    if (f.password.length < MIN_PASSWORD) {
      setError(`The password needs ${MIN_PASSWORD} characters or more.`);
      return;
    }
    if (!E164.test(phone)) {
      setError("Write the WhatsApp number with its country code, like +263 77 123 4567.");
      return;
    }
    setBusy(true);
    setError("");
    const body: SignUpBody = {
      name: f.name.trim(), email: f.email.trim(), password: f.password, shop_name: f.shop_name.trim(), place: f.place.trim(),
      manager_name: f.manager_name.trim(), manager_phone: phone,
      ...(f.manager_email.trim() ? { manager_email: f.manager_email.trim() } : {}),
    };
    try {
      const out = await auth.signUp(body);
      setSession({ person: out.person, shop: out.shop });
      setMade({ url: new URL(out.invite.url, window.location.origin).href, manager: body.manager_name, shop: out.shop.name });
    } catch (err) {
      setError(said(err));
    }
    setBusy(false);
  };

  if (made) return <Made made={made} {...props} />;

  return (
    <Page title="Create your shop" lede="PayPal holds the money. Your manager runs the shop. Proof releases each buy." wide {...props}>
      <form className="au-form" onSubmit={submit}>
        <fieldset className="au-group">
          <legend>You</legend>
          <Field label="Your name" autoComplete="name" required value={f.name} onChange={set("name")} />
          <Field label="Email" type="email" autoComplete="email" inputMode="email" required value={f.email} onChange={set("email")} />
          <Password label="Password" autoComplete="new-password" required minLength={MIN_PASSWORD} value={f.password}
            onChange={set("password")} hint={`${MIN_PASSWORD} characters or more.`} />
        </fieldset>
        <fieldset className="au-group">
          <legend>Your shop</legend>
          <Field label="Shop name" required value={f.shop_name} onChange={set("shop_name")} />
          <Field label="Where it is" required value={f.place} onChange={set("place")} hint="The area and the city, like Mbare, Harare." />
        </fieldset>
        <fieldset className="au-group">
          <legend>Who runs it</legend>
          <Field label="Manager's name" required value={f.manager_name} onChange={set("manager_name")} />
          <Field label="Manager's WhatsApp number" type="tel" autoComplete="off" inputMode="tel" required
            value={f.manager_phone} onChange={set("manager_phone")} hint="With the country code, like +263 77 123 4567. Musha's chat reaches this number once the manager links it from her dashboard." />
          <Field label="Manager's email (optional)" type="email" autoComplete="off" inputMode="email" value={f.manager_email}
            onChange={set("manager_email")} />
        </fieldset>
        {error && <Problem text={error} />}
        <button type="submit" className="au-go" disabled={busy}>{busy ? "Opening your shop" : "Create your shop"}</button>
      </form>
      <p className="au-alt">Already have a sign-in? <Link to="/sign-in">Sign in</Link></p>
    </Page>
  );
}

/** The shop is open: the manager's invite link to copy and send, then the console. */
function Made({ made, ...props }: Themed & { made: Made }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(made.url);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  };
  const first = made.manager.split(" ")[0];
  return (
    <Page title={`${made.shop} is open`} lede={`Send ${first} this link. It sets up ${first}'s own sign-in and works once, within 14 days.`} {...props}>
      <div className="au-invite">
        <input readOnly value={made.url} aria-label={`${made.manager}'s invite link`} onFocus={(e) => e.currentTarget.select()} />
        <button type="button" className="au-copy" onClick={copy}>{copied ? "Copied" : "Copy"}</button>
      </div>
      <p className="au-status" role="status">{copied ? "The link is on your clipboard." : ""}</p>
      <button type="button" className="au-go" onClick={() => go("/owner", true)}>Open your console</button>
      <p className="au-fine">Today in your console makes a fresh link until {first} signs in.</p>
    </Page>
  );
}

/** An invite from the owner: the manager sets her sign-in and opens her dashboard. */
export function InvitePage({ token, ...props }: Themed & { token: string }) {
  const found = useRetry(() => auth.invite(token).then((v): Invite | null => v, (e) => {
    if (e instanceof ApiError && (e.status === 404 || e.status === 410)) return null;
    throw e;
  }));
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (password.length < MIN_PASSWORD) {
      setError(`The password needs ${MIN_PASSWORD} characters or more.`);
      return;
    }
    setBusy(true);
    setError("");
    try {
      const s = await auth.accept(token, email.trim(), password);
      setSession(s);
      go(home(s), true);
    } catch (err) {
      setError(err instanceof ApiError && err.status === 404 ? "This invite has run out. Ask the shop's owner for a new one." : said(err));
      setBusy(false);
    }
  };

  const invite = found.value;
  if (invite === undefined) {
    return (
      <Page title="Your invite" {...props}>
        <p className="au-lede" role="status">{found.failures > 2 ? "The server is waking up. Musha keeps trying." : "Opening your invite."}</p>
        {found.failures > 2 && <button type="button" className="au-go" onClick={found.retry}>Try again</button>}
      </Page>
    );
  }
  if (invite === null) {
    return (
      <Page title="This invite has run out" lede="An invite works once, within 14 days. Ask the shop's owner for a new one." {...props}>
        <p className="au-alt">Set your sign-in already? <Link to="/sign-in">Sign in</Link></p>
      </Page>
    );
  }
  const first = invite.manager.name.split(" ")[0];
  return (
    <Page title={`Welcome, ${first}`}
      lede={<>{invite.owner.name} asked you to run <strong>{invite.shop.name}</strong> in {invite.shop.place}. Set your sign-in to open your dashboard.</>}
      {...props}>
      <form className="au-form" onSubmit={submit}>
        <Field label="Email" type="email" autoComplete="username" inputMode="email" required value={email}
          onChange={(e) => setEmail(e.target.value)} />
        <Password label="Password" autoComplete="new-password" required minLength={MIN_PASSWORD} value={password}
          onChange={(e) => setPassword(e.target.value)} hint={`${MIN_PASSWORD} characters or more.`} />
        {error && <Problem text={error} />}
        <button type="submit" className="au-go" disabled={busy}>{busy ? "Setting your sign-in" : "Start"}</button>
      </form>
    </Page>
  );
}
