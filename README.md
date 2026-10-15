# Musha

**Run your business at home from anywhere.**

Millions of people abroad fund businesses run by family at home: tuck shops, market stalls, kombis, poultry runs. Zimbabweans alone sent home US$2.45 billion through formal channels in 2025. Once the money lands, the sender loses sight of it.

Musha gives the owner abroad a seat at the table. The manager at home asks for stock on WhatsApp. The owner approves with PayPal, and the money is **held**. Proof of purchase decides how much is **released**. Unspent money **returns** to the owner. Every step posts to the shop's books.

## How it works

| Move | What happens | PayPal |
|---|---|---|
| **Fund** | The manager asks for stock in their own words. The owner approves. | Orders v2 with `intent: AUTHORIZE` places a hold. |
| **Prove** | The receipt is checked against the request. Musha releases what was spent and returns the rest. | Partial capture with `final_capture`, or a void. |
| **Run** | The owner reads the books and pays wages. | Payouts. Webhooks keep the books and the money in step. |

Holds stay alive on their own: a job reauthorizes a hold once the 3-day honor period ends, and closes it before the 29-day limit.

### The books always agree with the money

Every ledger line carries a unique reference built from PayPal's own IDs (`capture:<id>`, `returned:<authorization>`, `stock:<request>`). The API response and the PayPal webhook both post to the same references, so the books stay exact whichever arrives first, and a replayed webhook changes nothing. Every PayPal POST carries a `PayPal-Request-Id`, so a retry never moves money twice.

## Built so far

- The full money loop against the PayPal sandbox: authorize, partial capture, void, reauthorize, Payouts, verified webhooks.
- The owner's console: requests, approval with the PayPal JS SDK (v6), the shop's books in AG Grid, wages.
- The manager's WhatsApp, simulated in the browser so anyone can play both sides.
- Tests for every money path, with the sandbox mocked.

## Next

- **Proof engine:** AI reads the receipt photo, matches it to the request, checks prices against history, catches duplicates and stale receipts, and turns a score into a release, a question or a hold.
- **Counter camera:** the manager's phone on the counter confirms the delivery arrived.
- **Language bridge:** Shona voice notes in, English for the owner, and back.
- **Expected cash:** the cash and stock that should exist, against what is reported.
- **Owner's agent:** approvals inside a spending limit the owner sets, on the PayPal Agent Toolkit.

## Run it

You need Python 3.12, Node 22 and a PayPal developer account.

1. In the [PayPal developer dashboard](https://developer.paypal.com/dashboard/), create a sandbox app and two sandbox personal accounts: one for the owner, one for the manager.
2. Copy `.env.example` to `.env` and fill in the client ID, secret and the two sandbox emails.
3. Start the API:

```bash
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # macOS/Linux: .venv/bin/python
.venv/Scripts/python -m musha.seed
.venv/Scripts/python -m uvicorn main:app --port 8000
```

4. Start the web app in a second terminal, then open http://localhost:5174:

```bash
cd frontend
npm install
npm run dev
```

5. On Farai's phone, tap **Ask for stock**. On Chipo's console, approve with PayPal using the owner's sandbox login.

`npm run build` puts the web app in `frontend/dist`, and the API then serves it at http://localhost:8000.

### Webhooks

Set `PAYPAL_WEBHOOK_ID` and point the sandbox app's webhook at `https://<your-host>/api/paypal/webhook` with these events: `PAYMENT.CAPTURE.COMPLETED`, `PAYMENT.AUTHORIZATION.VOIDED`, `PAYMENT.PAYOUTSBATCH.SUCCESS`, `PAYMENT.PAYOUTSBATCH.DENIED`. Musha verifies every event with PayPal before it touches the books.

### Tests

```bash
cd backend
.venv/Scripts/python -m pytest -q
```

## Stack

FastAPI, SQLAlchemy (SQLite locally, Postgres in production), httpx; React, Vite, AG Grid, the PayPal JS SDK v6.

## Licence

MIT. See [LICENSE](LICENSE).
