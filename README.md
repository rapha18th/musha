# Musha

**Run your business at home from anywhere.**

Millions of people abroad fund businesses run by family at home: tuck shops, market stalls, salons, poultry runs. Zimbabweans alone sent home US$2.45 billion through formal channels in 2025. Once the money lands, the sender loses sight of it.

Musha gives the owner abroad a seat at the table. The manager at home asks for money in their own words, on a WhatsApp-style chat. The owner approves with PayPal, and the money is **held**. Musha reads the evidence (the receipt, a photo of the goods, the counter camera) and decides how much to **release**. Unspent money **returns** to the owner. Every step posts to the shop's books.

The demo family: Chipo, a nurse in Leicester, owns a tuck shop in Mbare, Harare. Her brother Farai runs it.

## How it works

| Move | What happens | PayPal |
|---|---|---|
| **Alert** | Musha sees stock running low and tells the owner first, with a suggested request. | |
| **Fund** | The manager asks in their own language, by text or voice note. Musha translates, checks the price against past buys, and recommends. The owner approves. | Orders v2 with `intent: AUTHORIZE` places a hold. |
| **Delegate** | The owner can let Musha's agent approve inside a mandate: kinds of spend, a limit per request and per week, known suppliers, usual prices. Outside it, the agent asks. | Vault v3 saves the owner's PayPal once. The agent places merchant-initiated holds with the vault ID. |
| **Prove** | The proof engine reads the receipt and the photos, checks them, scores them, and decides: release, ask one question, or hold for the owner. | Partial capture with `final_capture`, or a void. |
| **Release** | What the evidence proves goes to the shop. | Payouts sends the captured amount to the shop's PayPal. |
| **Run** | The owner asks how the week went, looks in on the counter, reads expected cash against counted cash, and pays wages. | Wages are a request the owner approves with PayPal; Musha captures the hold and pays the manager by Payouts. Transaction Search reconciles PayPal with the books. |

Holds stay alive on their own. A job reauthorizes each hold once, an hour after the 3-day honor period, and voids it 12 hours before the 29-day limit. Locally the web process runs the job every 30 minutes (`MUSHA_JOBS_EVERY_MINUTES`). On Render a cron calls it every 10.

## The proof engine

The manager sends a receipt. Read is the gate, and six scored checks follow. Their points add up to a score out of 100.

| Check | What Musha looks at | Points |
|---|---|---|
| Read | Payee, receipt number, date, lines, total and currency, read from the photo by Gemini | Gate |
| Match | Payee, item, quantity, currency and total against the approved request; lines that add up; other goods on the bill | 25 |
| Price | Unit price against the median of the shop's last eight buys of the item, from that supplier when it has them | 15 |
| Duplicate | Receipt number, file hash and a perceptual hash against every earlier receipt in the shop | 20 |
| Freshness | Dated between approval and upload, inside the 7 days after approval | 10 |
| Photo | The goods are in the photo, at roughly the right quantity, in a photo new to the shop | 10 |
| Camera | The counter camera saw the goods arrive inside the 7 days after approval | 20 |

A receipt Musha can read opens the gate; an unreadable one brings a request for a sharper photo. The Photo check applies to stock and assets. The Camera check counts once the camera sees the delivery. The score scales over the checks that apply.

- **85 and above:** PayPal captures what the receipt shows for the requested item, capped at the hold, and returns the rest.
- **60 to 84:** Musha asks the manager one question, then scores again with the answer.
- **Below 60:** the money stays held, and the owner sees why. She releases an amount or returns it all.

Each request gets one question. After it, the next score releases the money or sends the request to the owner.

Some evidence contradicts the request: another payee or item, other goods on the bill, another currency, lines that miss the total, a price more than 20% over the usual, a receipt or photo sent before, or a date outside the window. A contradiction turns a release into the one question, whatever the score. An answer can ease the price, the date or the goods photo. A wrong payee, a duplicate, other goods, another currency and every other contradiction go to the owner.

Values the manager typed while the reader was off wait for the owner's tap. Every decision carries its reasons, and the owner sees every attachment behind it.

## Measured

Two labelled sets measure the decisions that move money. Each runs through the app's own code: the same prompts, schemas and rules, in a seeded shop.

**Receipts.** 120 receipt photos in one shop over seven weeks, made by `backend/eval/make_cases.py` from seed 2026: 40 genuine, 20 inflated, 20 wrong payee, 20 duplicate and 20 stale. Each flawed receipt carries one flaw. Ten products come from six suppliers, each with its own till, photographed at varied tilt and light. Gemini 3.8 Flash reads every photo. The goods photo and the counter camera are left out, so the receipt decides alone. A second set of 120, drawn from seed 7, was held out and read after the main set had shaped the engine.

| Receipts | Main set (tuned on) | Held out (seed 7) |
|---|---|---|
| Genuine receipts released | 40 of 40 | 40 of 40 |
| Flawed receipts released | 0 of 80 | 0 of 80 |
| Each flaw caught, precision / recall | 1.00 / 1.00 | 1.00 / 1.00 |
| Releases exact to the cent | 40 of 40 | 40 of 40 |
| Upload to decision, median | 7.0 s | 6.2 s |

Every flawed receipt went to a question for the manager. The releases captured US$2,256.20 for the requested goods in the main set and US$2,391.65 in the held-out set. Six receipts in each set also carried a carrier bag, and the capture left the bag out. Gemini read the payee, date, lines and total on all 110 distinct photos of each set, and the receipt number on 95.5%. A perfect reading of what is printed gives the same decisions. The engine itself takes 435 ms; the rest is the model reading the photo.

**Counter camera.** 112 counter stills, drawn by an image model from one picture of the shop's quiet counter, with goods, people, light, angle and occlusion varied. Gemini 3.8 Flash reads each still through the app's own prompt, and the app's delivery check then asks about every product the shop sells. The prompt changed twice while the first three sets were studied. The test set was drawn after the last change and read once.

| Counter camera | Stills | Deliveries confirmed | False alarms |
|---|---|---|---|
| Main (tuned on) | 60 | 30 of 30 | 1 of 30 |
| Probe (tuned on) | 12 | 5 of 6 | 0 of 6 |
| Fresh (tuned on) | 20 | 10 of 10 | 0 of 10 |
| Test (held out) | 20 | 10 of 10 | 0 of 10 |

Product by product, the delivery check confirmed 10 of 10 test deliveries, with 0 false confirmations in 70 checks for other goods. A read takes 5.6 s at the median. Seven of the 112 reads ran past the app's 40 s limit, and the app reads such a still again when it scores a request. Counted as misses, the test set confirms 7 of 10.

**Limits.** Both sets are synthetic. The receipts are clean thermal print photographed by a simulated phone, every still shows the same counter, and the held-out camera set holds 10 deliveries and 10 other scenes. Worn and handwritten receipts and other counters are the next sets.

Detail, case by case: [backend/eval/RESULTS.md](backend/eval/RESULTS.md) and [backend/eval/camera/RESULTS.md](backend/eval/camera/RESULTS.md).

## Eyes on the counter

A phone propped on the counter becomes a camera for the owner. A pixel gate in the browser sends a still when the picture changes, and one at least every three minutes. A second gate on the server passes a still to Gemini when enough of it moved. Gemini reads each still into one line: open, busy, shelf low, a delivery arriving. The Camera check counts a delivery the camera saw, and the owner can **Look in** for a fresh picture from anywhere. The manager agrees to the camera on the counter page before the first still, and the owner's console shows when. A sign at the counter says the camera is there. Stills that show a delivery are kept 30 days, the rest 2 to 7 days, and a still that repeats the last one is left unkept.

## The books always agree with the money

Every ledger line carries a unique reference built from PayPal's own IDs (`capture:<id>`, `returned:<authorization>`, `stock:<request>`). The API response and the PayPal webhook both post to the same references, so the books stay exact whichever arrives first, and a replayed webhook leaves them as they are. Every PayPal POST carries a `PayPal-Request-Id`, so a retry moves money once. A capture PayPal reports as pending books when its webhook arrives. Voids go to the parent authorization, which PayPal requires after a reauthorization.

## AI in Musha

Gemini 3.8 Flash does the reading and the writing:

- **Receipts:** structured reading of the photo into payee, number, date, lines, total and currency.
- **Goods photos and counter stills:** what is in the picture, roughly how many, and whether a delivery is arriving.
- **Language bridge:** voice notes transcribed, the manager's words translated for the owner, and Musha's replies translated back. Text only; Musha speaks through the chat.
- **Requests:** one message parsed into one or more typed requests against the shop's own products and suppliers.
- **One question:** when the evidence is unclear, Musha writes the single question that settles it, and weighs the answer.
- **Answers:** the owner asks about the week, and Musha answers from the books.
- **Reconciliation:** plain code matches PayPal's transactions to the ledger, and an agent with the PayPal Agent Toolkit's read-only tools writes the owner's summary.

Money decisions run in plain code: the score, the decision rule and the mandate are deterministic and explained. The model reads and writes; the rules decide.

## Try it

Rehearsal mode runs every step below on a laptop, with a stand-in answering for PayPal (see [Run it locally](#run-it-locally)). Chipo's console fills the left of the page and Farai's phone the right. On a narrow screen, the two names at the top switch between them.

1. Press **Open the shop**. Today opens on eggs running low.
2. On Farai's phone, press **Ask for money**. Musha fills in a week of eggs at the usual price: 40 trays from Mbare wholesale for US$180.00. Press **Send to Chipo**. With Gemini on, Farai can type the request or send a voice note instead, in Shona or English.
3. In Chipo's console, press **Approve US$180.00**. PayPal holds the money.
4. On the phone, press **Send proof for eggs**, open **Goods photo** and send the **Sample photo**. Press **Send proof for eggs** again and send the **Genuine** receipt.
5. Musha reads the receipt, scores it, and releases US$172.00 to the shop. The other US$8.00 goes back to Chipo. With the model off, the sample's numbers arrive as typed values, and Chipo presses **Release US$172.00**.
6. Ask for cooking oil the same way, approve it, and send the **Wrong payee** sample. The receipt names another supplier, so Musha asks Farai one question. Reply in the chat. A wrong payee stays a concern after any answer, so the money waits for Chipo: **Release** or **Return all**.
7. On the phone, press **Close the day**. Musha fills in what the books expect. Count a few dollars less in **Cash counted**, close, and the gap reaches Chipo's Today with a question for Farai.
8. In Chipo's console, press **Look in**. On Farai's phone, open **Counter** and press **Start the camera**, which records Farai's agreement to it, or send a staged scene such as **Delivery**. The new still reaches Chipo with Musha's reading of it. **Copy the link** opens the counter camera on a second phone, with that phone's own key.
9. Open **Agent** and press **Save PayPal for Musha**. The agent now approves inside Chipo's terms: stock up to US$200 a request and US$500 a week, from suppliers she has paid before, within 10% of usual prices. Ask for sugar from the phone, and **Recent decisions** shows the approval and why. Outside the terms, Musha asks Chipo.
10. Open **Books** and press **Match PayPal to the books**. Musha sets PayPal's transactions beside the ledger, line for line.

## Run it locally

You need Python 3.12 and Node 22 or later.

```bash
cp .env.example .env
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # macOS/Linux: .venv/bin/python
.venv/Scripts/python -m uvicorn main:app --port 8000
```

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5174 and press **Open the shop**. Each browser gets a shop of its own, seeded with two weeks of trading, and keys of its own: the owner's stays in this browser, and the counter link carries the counter phone's.

**Rehearsal mode.** With `MUSHA_PAYPAL=standin`, a stand-in answers every PayPal call in memory, so every screen works before you have sandbox keys. `.env.example` starts in rehearsal mode, and a banner says so.

**Sandbox mode.** Set `MUSHA_PAYPAL=sandbox` and fill the PayPal keys in `.env`:

1. In the [PayPal developer dashboard](https://developer.paypal.com/dashboard/), create a sandbox REST app on a US sandbox business account. Under its features, keep **Accept payments** on with **Vault** selected, and turn on **Payouts** and **Transaction Search**.
2. Create two sandbox personal accounts: one for the owner (approves holds), one for the manager (receives released money and wages).
3. Put the client ID, secret and both emails in `.env` (`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `MUSHA_OWNER_EMAIL`, `MUSHA_MANAGER_EMAIL`), then start the API. A fresh start picks up a token with the reporting scope Transaction Search needs.

**Gemini.** Set `GEMINI_API_KEY`, or `GOOGLE_CLOUD_PROJECT` with application default credentials for Vertex AI. `MUSHA_GEMINI_MODEL` picks the model, `gemini-3.8-flash` by default. Musha also runs on plain rules alone: the manager types the receipt total, the checks run on what was typed, and the owner taps to release.

**Agent Toolkit.** `.venv/Scripts/python -m pip install --no-deps paypal-agent-toolkit==1.11.0` gives the reconciliation agent the toolkit's own tool definitions. Musha declares the same read-only tools itself when the package is absent.

`npm run build` puts the web app in `frontend/dist`, and the API then serves it at http://localhost:8000.

### Webhooks

Set `PAYPAL_WEBHOOK_ID` and point the sandbox app's webhook at `https://<your-host>/api/paypal/webhook` with these events:

- `PAYMENT.AUTHORIZATION.CREATED`, `PAYMENT.AUTHORIZATION.VOIDED`
- `PAYMENT.CAPTURE.COMPLETED`, `PAYMENT.CAPTURE.DECLINED`, `PAYMENT.CAPTURE.REVERSED`
- `PAYMENT.PAYOUTSBATCH.SUCCESS`, `PAYMENT.PAYOUTSBATCH.DENIED`
- `PAYMENT.PAYOUTS-ITEM.SUCCEEDED`, `.UNCLAIMED`, `.FAILED`, `.RETURNED`, `.BLOCKED`, `.HELD`, `.CANCELED`, `.REFUNDED`
- `VAULT.PAYMENT-TOKEN.DELETED`

Musha verifies every event with PayPal before it touches the books, and books each event once.

### Tests

```bash
cd backend
.venv/Scripts/python -m pytest -q
```

The sandbox is mocked in the tests, so they run offline.

### Evaluation

Both evals rerun offline from the saved Gemini readings:

```bash
cd backend
.venv/Scripts/python eval/run_eval.py --cached          # receipts: the main set and the held-out set
.venv/Scripts/python eval/camera/run_eval.py --cached   # counter stills: all four sets
```

Live, Gemini on Vertex AI reads every photo the saved readings lack. Move `readings.json` aside to read a whole set again.

```bash
.venv/Scripts/python eval/run_eval.py --project <gcp-project>   # application default credentials
.venv/Scripts/python eval/camera/run_eval.py                    # GOOGLE_CLOUD_PROJECT, application default credentials
```

Each run rewrites its `results.json` and `RESULTS.md`. `eval/make_cases.py --images <folder>` writes every receipt photo to look at.

## Deploy on Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/rapha18th/musha)

`render.yaml` describes the whole stack: one web service for the API and the built web app, a Basic Postgres database, and a cron that runs the hold job every 10 minutes and keeps the free web service awake. Create it with the button or from **Dashboard > New > Blueprint**. Render asks for the PayPal client ID, secret and webhook ID, the two sandbox emails and the Gemini key. The first start seeds the demo shop.

## Stack

FastAPI, SQLAlchemy (SQLite locally, Postgres on Render), httpx, Pillow, Google Gen AI SDK, the PayPal Agent Toolkit; React 19, Vite, AG Grid, the PayPal JS SDK v6.

## Licence

MIT. See [LICENSE](LICENSE).
