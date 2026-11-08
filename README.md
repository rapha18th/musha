# Musha

**Run your business at home from anywhere.**

Millions of people abroad fund businesses run by family at home: tuck shops, market stalls, salons, poultry runs. Zimbabweans alone sent home US$2.45 billion through formal channels in 2025. Once the money lands, the sender loses sight of it.

Musha gives the owner abroad a seat at the table. The manager at home asks for money in their own words, on their dashboard or on WhatsApp. The owner approves with PayPal, and the money is **held**. Musha reads the evidence (the receipt, a photo of the goods, the counter camera) and decides how much to **release**. Unspent money **returns** to the owner. Every step posts to the shop's books.

The sandbox family: Chipo, a nurse in Leicester, owns a tuck shop in Mbare, Harare. Her sister Farai runs it. Each signs in to a dashboard of her own. Chipo's console shows the money, the proof and the books. Farai's dashboard shows what needs her today, her chat with Musha, the day's close and the counter camera. The same chat runs on her WhatsApp.

## How it works

| Move | What happens | PayPal |
|---|---|---|
| **Alert** | Musha sees stock running low and tells the owner first, with a suggested request. | |
| **Fund** | The manager asks in their own language, by text or voice note. Musha translates, checks the price against past buys, and recommends. The owner approves. | Orders v2 with `intent: AUTHORIZE` places a hold. |
| **Delegate** | The owner can let Musha's agent approve inside a mandate: kinds of spend, a limit per request and per week, known suppliers, usual prices. Outside it, the agent asks. | Vault v3 saves the owner's PayPal once. The agent places merchant-initiated holds with the vault ID. |
| **Prove** | The proof engine reads the receipt and the photos, checks them, scores them, and decides: release, ask one question, or hold for the owner. | Partial capture with `final_capture`, or a void. |
| **Release** | What the evidence proves goes to the shop. | Payouts sends the captured amount to the shop's PayPal. |
| **Run** | The owner calls the shop, looks in on the counter, reads expected cash against counted cash, and pays wages. The shop calls her when the money needs her. | Wages are a request the owner approves with PayPal; Musha captures the hold and pays the manager by Payouts. Transaction Search reconciles PayPal with the books. |

**Musha asks for stock itself.** With the agent on, an item that falls under two days of stock becomes Musha's own request: the usual pack, from the usual supplier, at the usual price. The owner reads what Musha asked for and why. Inside the owner's terms the agent approves it, PayPal holds the money, and the manager is told to buy. Outside them, the request waits for the owner, filled in for one tap. Each item carries one open request at a time. After the owner declines Musha's request, dismisses the item's alert or returns all of its hold, Musha asks for that item again once new stock arrives and runs low. **Musha asks for stock when it runs low**, a switch on the Agent tab, starts on. The hold job asks for the shops someone used in the last day.

Holds stay alive on their own. A job reauthorizes each hold once, after the 3-day honor period, and voids it in the last 12 hours before the 29-day limit. The same job sends any payout PayPal has yet to accept, and asks PayPal after each payout still on its way, so a run without webhooks closes its payouts too. Locally the web process runs the job every 30 minutes (`MUSHA_JOBS_EVERY_MINUTES`). On Cloud Run, Cloud Scheduler calls it every three hours.

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

## Connect your shop

**Connect your shop** opens one sheet from both dashboards: beside **Look in** on the owner's console, and on the manager's **Counter camera** tab. The dashboard hands out the shop's counter link as a code. The device at the counter takes the code, and the camera runs on that device. The sheet gives the counter its camera in one of three ways, and shows what is connected, its last still and when it came.

- **A phone.** The sheet shows a code. Scan it with the phone that stays at the counter: an old Android phone with Chrome is enough. The counter page opens on that phone. The manager reads the terms there and presses **Start the camera**. **Copy the link** gives the same address to send by message, and **Share the link** opens the browser's share sheet where it has one.
- **An IP camera.** [`counter-bridge/`](counter-bridge/) runs on a computer on the camera's network: a Raspberry Pi, a mini PC or a laptop. It reads the RTSP stream with OpenCV, sends a still when the picture changes and one every three minutes, and answers Look in within five seconds. The sheet fills in the command with the shop's counter link and holds the stream addresses for Hikvision, Dahua and ONVIF cameras. `--agree --name` with the manager's name records the manager's agreement. A video file works as a camera too.
- **A video feed.** Footage of a shop counter loops through the camera's own path, one still every 30 seconds, while a dashboard or a counter page is open. Musha reads each still once, and later loops reuse the reading. The feed stops itself after 30 quiet minutes, and a still from a phone or the bridge takes over from it. Footage counts in the sandbox shop, the way staged scenes count. The footage comes from Pexels: "Bustling African Market Stall Scene in Daylight" by Khanh Hoang Minh 2 and "Lagos market women trading" by Shedrack Salami ([credits](backend/musha/assets/feed/CREDITS)).

**Disconnect** sits on the owner's sheet. It stops the feed and gives the shop a new counter link. Phones and bridges holding the old link stop, and the owner's and the manager's sign-ins stay as they are.

## Call your shop

The owner presses **Call the shop** and talks to it. Musha answers for the shop, in English, from the shop's own data: the money PayPal holds, the open requests and where each proof stands, today's and yesterday's sales and margin, the till against the count, the stock running low, and the counter camera's last still. During the call Musha looks things up with five tools:

| Tool | What it does |
|---|---|
| `look` | Puts the newest counter still on screen and asks the counter camera for a fresh one, which Musha describes when it lands |
| `requests` | Reads the open and recent requests, and shows a receipt when the owner names an item |
| `books` | Reads sales, margin and cash for today, yesterday, a date or the week |
| `stock` | Gives the days of stock left and the suggested order |
| `ask_farai` | Sends the manager a line in her thread, in the owner's words, translated into the manager's language |

Money moves on the console's buttons. When the owner wants to approve, release or return, Musha names the button on her screen.

**The shop calls the owner.** A receipt Musha holds for her, a day closed with a cash gap, or the counter camera seeing the shop close during trading hours rings her console, with the reason in one line and its picture. **Answer** opens the call on that reason. **Later** sends it away, and the alert waits on Today. Each alert rings once, within 15 minutes of arriving, and the shop keeps three quiet minutes after a ring. With the console in a background tab, the ring also reaches the system's notifications once the owner allows them.

The browser holds one WebSocket to Musha's server, and the server holds the Gemini Live session, so every key stays on the server. The call opens to the owner's session alone. A call runs five minutes, and Musha says goodbye 25 seconds before the end. A shop takes 6 calls an hour, one connection 12, and the server 120 a day.

## Sign-in

The owner and the manager each sign in with an email and a password, and each lands on a dashboard of her own: the owner's console at `/owner`, the manager's dashboard at `/manager`. Each has **Sign out**.

- **Passwords.** scrypt, with a fresh salt for each account, compared in constant time. A wrong email and a wrong password get the same answer and cost the same hash. Sign-in has a budget of tries per client.
- **Sessions.** A random token in an HttpOnly cookie, Secure on https and SameSite=Lax. The server keeps the token's SHA-256 alone. A session lasts 30 days, and Sign out deletes it on the server.
- **Seats.** Every route keeps its role. The owner's session opens every route of her own shop. The manager's opens the manager's and the counter's routes of her own shop, and Connect your shop. A session opens its own shop alone.
- **The counter phone.** A device, so it carries the shop's counter key in its link. Connect your shop hands out that link from either dashboard, and the owner retires it with Disconnect.

**Create your shop.** Sign-up opens a new, empty shop with its owner and its manager, and signs the owner in. The owner sends the manager an invite link. The link sets the manager's own sign-in, works once and lasts 14 days. Until the manager signs in, Today in the console makes a fresh link. Sign-up has two budgets: six new shops an hour for each client, and `MUSHA_SHOPS_PER_HOUR` for every client together.

## The manager's dashboard

The manager's dashboard at `/manager` is built like the owner's console: the same header, tabs, cards and buttons, in light and dark. It fills a phone's screen, and on a wide window the views take two columns. Five tabs hold the shop.

- **Today** leads with the one thing that needs the manager now: Musha's question about a receipt, the owner's question with the time she asked, a new line from the owner, a held request waiting for proof, stock running low, then the day's close. The rest of what waits sits under it, then the open and settled requests.
- **Messages** is the thread with Musha, and the owner's lines arrive in it under her name. The manager types or records a voice note in Shona or English, and each line reads in either language. Each request sits in the thread as a card with its amount and status, with **Reply** and **Send proof**. The clip sends a receipt photo, a goods photo, or one of five sample receipts.
- **Close the day** shows today's close and the week day by day: expected cash against counted, stock gaps, and Musha's questions.
- **Counter camera** shows whether the camera watches, the newest still with Musha's reading, and recent stills. **Connect your shop** opens the shop's sheet with its code. The sandbox shop adds staged scenes.
- **Record** lists every buy PayPal released on the manager's proof, each with its receipt.

**Ask for money**, **Send proof** and **Close the day** open as dialogs: centred on a desktop, a sheet from the bottom on a phone. While the tab sits in the background, new lines from Musha and the owner show as a count in its title.

## On WhatsApp

The dashboard holds every line, and WhatsApp carries the same thread to the manager's phone for convenience, through the WhatsApp Business Cloud API. Four settings turn it on: `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET` and `WHATSAPP_VERIFY_TOKEN`. The manager's number comes from the sign-up form. It goes live once the manager proves it: **Link your WhatsApp** on the dashboard's Today gives a six-digit code, and the manager sends it to Musha from that number. **Use another number** fixes a number the owner wrote wrong, and the new one waits for its own code. One shop at most holds a proved number, so a sign-up that names another person's number reaches nobody. The sandbox shop's manager takes the number in `MUSHA_MANAGER_PHONE`, set by whoever runs Musha, and it counts as proved. **Messages** says when the thread is live on WhatsApp.

- **Out.** Every line Musha or the owner writes in the manager's thread also goes to the manager's WhatsApp number, in the manager's language, with the owner's lines under the owner's name. Each line is kept before it is sent, so it goes out once.
- **In.** The manager's texts, voice notes, photos and PDFs reach `/api/whatsapp/webhook` and land in the same thread through the handlers the dashboard uses. Musha marks each one read as it hears it, and the manager sees Musha typing until the reply comes. A text or a voice note goes the way the composer's go, and a reply to the owner's question answers it. A photo goes to the proof engine like an upload: to the request Musha's line was about, the one its caption names, or the one request waiting for proof. With several waiting, Musha asks which.
- **Checks.** Musha checks Meta's `X-Hub-Signature-256` over the raw body before it reads anything, answers 200, and hears each message once by its WhatsApp id. Musha hears the proved numbers of its shops' managers, and a link code from the number it belongs to, and drops the rest.
- **The 24-hour window.** WhatsApp takes free text within 24 hours of the manager's last message to the shop. Past that, a line goes as the approved template `musha_update` while PayPal holds money that waits on the manager's proof, and other lines wait in the dashboard.

With the four settings empty, the dashboard carries the thread alone, and every screen works the same. The WhatsApp simulator in `whatsapp-sim/` plays Meta's side on one computer: a phone open on the chat with Musha's test number, Meta's signed webhooks into Musha, and Meta's Graph API for Musha's replies, with Meta's rules for the token, the recipient list and the 24-hour window. [See the WhatsApp line work](#see-the-whatsapp-line-work) has the command and the steps. [Go live on WhatsApp](deploy/README.md#go-live-on-whatsapp) has the steps for Meta itself: the Meta app and its token, the manager's number on the test number's list, the four secrets, the webhook, and the template.

## The books always agree with the money

Every ledger line carries a unique reference built from PayPal's own IDs (`capture:<id>`, `returned:<authorization>`, `stock:<request>`). The API response and the PayPal webhook both post to the same references, so the books stay exact whichever arrives first, and a replayed webhook leaves them as they are. Every PayPal POST carries a `PayPal-Request-Id`, so a retry moves money once. A capture PayPal reports as pending books when its webhook arrives. Voids go to the parent authorization, which PayPal requires after a reauthorization.

## The Money view

**Money**, beside Books, puts every PayPal movement behind the shop on one screen. It reads what the books already hold: each hold with its authorization ID, each capture and return under PayPal's own IDs, each payout, and the proof behind each release.

- **The money river.** Each week's holds as three bands: released to the shop, returned to the owner, and still held at PayPal. The bands add up to what PayPal held that week, and each band lists the capture and void IDs behind it.
- **The hold clock.** A dial for each open authorization against PayPal's 29 days, with the 3-day honor period and Musha's return before the limit marked on it.
- **Every PayPal movement.** One row per request, grouped by week, supplier or item, with totals in the status bar. A row opens its PayPal record (authorization, reauthorization, capture, void, payout, each with its ID) beside its proof card, the checks drawn as bars. **Export to Excel** writes each request with its PayPal events beneath it.
- **Cash gap.** Expected cash against the manager's count, day by day, each gap in its colour with the manager's answer.

**Ask Musha about the money.** The owner asks in her own words: "What did PayPal return to me this month, by supplier?" Gemini turns the question into a small view spec: a filter, a grouping, a sort and a chart. Musha checks the spec against the grid's columns and the shop's own suppliers and items, and checks every amount in the answer against the rows the spec selects. The grid applies the spec through its own filter and column APIs, and the chart draws from the grid's groups. With the model off, rules read the common questions the same way every time.

The view runs on AG Grid Enterprise (row grouping, master/detail, set filters, a custom status bar, Excel export) and AG Charts Enterprise (stacked areas with gradient and pattern fills, a radial gauge), themed from Musha's own tokens in light and dark. The licence key lives in the server's environment as `AG_GRID_KEY` and reaches the browser at runtime from `/api/config`, so the key stays out of the built bundle. With an empty key the view runs the same and shows the trial watermark. The view loads the first time its tab opens.

## AI in Musha

Gemini 3.8 Flash does the reading and the writing:

- **Receipts:** structured reading of the photo into payee, number, date, lines, total and currency.
- **Goods photos and counter stills:** what is in the picture, roughly how many, and whether a delivery is arriving.
- **Language bridge:** voice notes transcribed, the manager's words translated for the owner, and Musha's replies translated back, all as text in the chat.
- **The shop call:** Gemini Live speaks for the shop in English, answers from a briefing of the books, and runs the call's five tools. With `GEMINI_API_KEY` it runs on `gemini-3.8-live`, on Vertex AI on `gemini-live-2.5-flash`, and `MUSHA_LIVE_MODEL` names another.
- **Requests:** one message parsed into one or more typed requests against the shop's own products and suppliers.
- **One question:** when the evidence is unclear, Musha writes the single question that settles it, and weighs the answer.
- **Answers:** the owner asks about the week, and Musha answers from the books.
- **The Money view:** a question about the money becomes a view of the grid: a filter, a grouping, a sort and a chart.
- **Reconciliation:** plain code matches PayPal's transactions to the ledger, and an agent with the PayPal Agent Toolkit's read-only tools writes the owner's summary.

Money decisions run in plain code: the score, the decision rule and the mandate are deterministic and explained. The model reads and writes; the rules decide.

## Try it

The hosted app at **https://musha-383490753931.us-central1.run.app** runs on the PayPal sandbox with Gemini on. Press **Sign in** there, or go straight to https://musha-383490753931.us-central1.run.app/sign-in. The sandbox shop, Chipo's Tuck Shop, holds two weeks of trading, a sign-in for the owner and one for the manager:

| Who | Email | Password |
|---|---|---|
| The owner | `chipo@example.com` | In the submission notes |
| The manager | `farai@example.com` | In the submission notes |

These are Musha's own sandbox sign-ins, and their passwords sit in the submission notes beside the PayPal sandbox personal account that PayPal's sandbox window asks for. Everyone who signs in shares the sandbox shop, so it shows what others did before you. Once its history is a day old and nobody has used it for three hours, Musha seeds it again under the same sign-ins, and Today opens on eggs running low once more. **Create your shop** gives anyone an empty shop of their own.

Sign in as the owner in one browser and as the manager in another, or in a private window, and set them side by side. Both dashboards work on a phone and on a desktop. A local run starts in rehearsal mode, with a stand-in answering for PayPal (see [Run it locally](#run-it-locally)).

1. Sign in as the owner. Today opens on eggs running low.
2. Sign in as the manager and press **Ask for money**. Musha fills in a week of eggs at the usual price: 40 trays from Mbare wholesale for US$180.00. Press **Send to Chipo**. With Gemini on, the manager can type the request in **Messages** or hold the mic for a voice note instead, in Shona or English.
3. In the owner's console, approve the US$180.00. On the sandbox, the PayPal button opens PayPal's sandbox window: sign in with a PayPal sandbox personal account, such as the one in the submission notes, and approve. In rehearsal mode, press **Approve US$180.00**. PayPal holds the money.
4. On the manager's Today, press **Send proof** on the eggs. Choose **Goods photo** and send the **Sample photo**. Press **Send proof** again and send the **Genuine** receipt.
5. Musha reads the receipt, scores it, and releases US$172.00 to the shop. The other US$8.00 goes back to the owner. With the model off, the sample's numbers arrive as typed values, and the owner presses **Release US$172.00**.
6. In the owner's console, press **Connect your shop** beside **Look in**. Under **A video feed**, press **Connect the video feed**. A still of a shop counter arrives every 30 seconds with Musha's reading of it. **A phone** holds the code for a phone at the counter, and **An IP camera** holds the bridge command. On the manager's **Counter camera** tab, the same stills arrive with Musha's reading, staged scenes such as **Delivery** add their own, and **Connect your shop** opens the same sheet.
7. Press **Call the shop**. Ask how the shop is doing, then what the counter camera sees: the still appears on the call, and a fresh one follows. Ask Musha to tell the manager something, and the line reaches the manager's **Messages**. The call runs on Gemini: a local run needs `GEMINI_API_KEY` or a Vertex AI project.
8. Ask for cooking oil the same way, approve it, and send the **Wrong payee** sample. The receipt names another supplier, so Musha asks the manager one question, and it leads the manager's Today. Press **Answer Musha** and answer. A wrong payee stays a concern after any answer, so the money waits for the owner, and the shop calls her. Press **Answer** to hear why, then **Release** or **Return all** on the console.
9. On the manager's dashboard, press **Close the day**. Musha fills in what the books expect. Count a few dollars less in **Cash counted** and close. The gap reaches the owner's Today with a question for the manager, and the shop rings the owner about it. After any ring, the shop waits three minutes before the next.
10. On the owner's Today, press **Ask Farai** on the gap. The button gives way to the time of the question: "Asked 18:42 today, waiting for Farai's response." The question leads the manager's Today. **Answer Chipo** opens **Messages** with a reply to the question. Once the manager answers, the answer takes the place of the wait on the owner's Today and in **Week**, with **Ask again**. A reply to the question's line on WhatsApp answers it the same way.
11. Open **Week**, put an amount in **Wages** and press **Pay Farai**. Approve it on Today the way the eggs were approved. Musha captures the hold and pays the manager by Payouts.
12. Open **Agent** and press **Save PayPal for Musha**. The agent now approves inside the owner's terms: stock up to US$200 a request and US$500 a week, from suppliers she has paid before, within 10% of usual prices. Ask for sugar from the manager's dashboard, and **Recent decisions** shows the approval and why. Outside the terms, Musha asks the owner.
13. Open **Books**. The **By** column names who made each line, and **Farai's record** counts every buy PayPal released on the manager's proof, each with its receipt. Press **Match PayPal to the books**. Musha sets PayPal's transactions beside the ledger, line for line. On the sandbox, PayPal lists a transaction up to three hours after it happens.
14. Press **Sign out**, then **Create your shop**. Fill in you, your shop and who runs it. The shop opens empty, with an invite link for its manager. Open the link in another browser and set the manager's sign-in. The manager's dashboard opens, with Musha's first line in **Messages**. With WhatsApp on, **Link your WhatsApp** on Today gives the code that puts the same chat on the manager's WhatsApp. Staged scenes belong to the sandbox shop, so a new shop's **Counter camera** shows the camera alone.

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

Open http://localhost:5174 and press **Sign in**. The API's first start seeds the sandbox shop, with two weeks of trading and the sign-ins `chipo@example.com` and `farai@example.com`. Their passwords come from `MUSHA_OWNER_SIGNIN_PASSWORD` and `MUSHA_MANAGER_SIGNIN_PASSWORD` in `.env`, 10 characters or more. Left empty, the first start makes random ones and prints them once to the API's log. `.venv/Scripts/python -m musha.seed` resets the local database to the sandbox shop and its sign-ins. **Create your shop** makes an empty shop of your own, with an invite link for its manager.

**Rehearsal mode.** With `MUSHA_PAYPAL=standin`, a stand-in answers every PayPal call in memory, so every screen works before you have sandbox keys. `.env.example` starts in rehearsal mode, and a banner says so.

**Sandbox mode.** Set `MUSHA_PAYPAL=sandbox` and fill the PayPal keys in `.env`:

1. In the [PayPal developer dashboard](https://developer.paypal.com/dashboard/), create a sandbox REST app on a US sandbox business account. Under its features, keep **Accept payments** on with **Vault** selected, and turn on **Payouts** and **Transaction Search**.
2. Create two sandbox personal accounts: one for the owner (approves holds), one for the manager (receives released money and wages).
3. Put the client ID, secret and both emails in `.env` (`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `MUSHA_OWNER_EMAIL`, `MUSHA_MANAGER_EMAIL`), then start the API. A fresh start picks up a token with the reporting scope Transaction Search needs.

**Gemini.** Set `GEMINI_API_KEY`, or `GOOGLE_CLOUD_PROJECT` with application default credentials for Vertex AI. `MUSHA_GEMINI_MODEL` picks the model, `gemini-3.8-flash` by default. Musha also runs on plain rules alone: the manager types the receipt total, the checks run on what was typed, and the owner taps to release.

**The shop call.** The call takes the Gemini settings above. With `GEMINI_API_KEY` it runs on `gemini-3.8-live` through the Gemini API, which picks up in about two seconds. With `GOOGLE_CLOUD_PROJECT` alone it runs on `gemini-live-2.5-flash` on Vertex AI, which picks up in about eight. `MUSHA_LIVE_MODEL` names another Live model. The Vite dev server passes the call's WebSocket through to the API.

**Counter bridge.** On the computer beside the camera, `pip install -r counter-bridge/requirements.txt` and run the command from **Connect your shop**. [counter-bridge/README.md](counter-bridge/README.md) has the stream addresses, a service file and the options.

**Agent Toolkit.** `.venv/Scripts/python -m pip install --no-deps paypal-agent-toolkit==1.11.0` gives the reconciliation agent the toolkit's own tool definitions. Musha declares the same read-only tools itself when the package is absent.

`npm run build` puts the web app in `frontend/dist`, and the API then serves it at http://localhost:8000.

### Webhooks

Set `PAYPAL_WEBHOOK_ID` and point the sandbox app's webhook at `https://<your-host>/api/paypal/webhook` with these events:

- `PAYMENT.AUTHORIZATION.CREATED`, `PAYMENT.AUTHORIZATION.VOIDED`
- `PAYMENT.CAPTURE.COMPLETED`, `PAYMENT.CAPTURE.DECLINED`, `PAYMENT.CAPTURE.REVERSED`
- `PAYMENT.PAYOUTSBATCH.SUCCESS`, `PAYMENT.PAYOUTSBATCH.DENIED`
- `PAYMENT.PAYOUTS-ITEM.SUCCEEDED`, `.UNCLAIMED`, `.FAILED`, `.RETURNED`, `.BLOCKED`, `.HELD`, `.CANCELED`, `.REFUNDED`
- `VAULT.PAYMENT-TOKEN.DELETED`

Musha verifies every event with PayPal before it touches the books, and books each event once. On Cloud Run, `deploy/deploy.ps1` registers this webhook for the service URL and stores its ID. A local run, which PayPal's webhooks never reach, closes its payouts through the hold job: it asks PayPal after each payout still on its way and books what PayPal reports.

### See the WhatsApp line work

The WhatsApp simulator in `whatsapp-sim/` plays Meta's side on your computer, so the WhatsApp line runs end to end on one machine. It draws the manager's phone in WhatsApp's look, open on the chat with Musha's test number. Each message sent from it becomes the webhook Meta sends: the same JSON, signed with `X-Hub-Signature-256` under the app secret, posted to `/api/whatsapp/webhook`. Musha's replies come back through the simulator's Graph API, followed by the sent, delivered and read statuses Meta posts. Musha's read receipt turns the phone's own ticks blue and shows Musha typing until its reply, for 25 seconds at most, as Meta shows it.

```bash
backend/.venv/Scripts/python whatsapp-sim/run.py   # macOS/Linux: backend/.venv/bin/python
```

The runner starts the simulator on port 8090 and the API on port 8000. It gives both a fictional token, phone number ID, app secret and verify token, points `WHATSAPP_GRAPH_BASE` at the simulator, and sets `MUSHA_MANAGER_PHONE` to the simulated phone's number. The API keeps these shops in `backend/musha-sim.db`. Once both answer, the simulator makes Meta's webhook check, and the runner prints the result. The rest of `.env` applies as usual.

1. Open the phone at http://127.0.0.1:8090, and the dashboards at http://localhost:5174 (`npm run dev`) or http://localhost:8000.
2. From the phone, send `40 trays of eggs from Mbare wholesale, US$180.00`. The request lands in the manager's thread and on the owner's console, and Musha's answer reaches the phone in the manager's language.
3. Approve the request as the owner. Musha's line reaches the phone.
4. From the attach menu, send **Sample goods** and **Sample receipt**, in either order. Each carries its caption, Goods or Receipt, so Musha files the photo as the goods or the receipt. Both go to the proof engine like an upload from the dashboard.

**The wire** beside the phone lists each webhook and Graph API call with its payload. **Hide the wire** in the phone's menu leaves the phone alone on the page, for a recording. The simulator keeps Meta's rules: the bearer token, the phone number ID, the test number's recipient list and the 24-hour window. A text past the window fails with error 131047, and Musha sends the line again as the template. `backend/tests/test_whatsapp_sim.py` runs the same line in one process. The simulator stays on your computer. The hosted service talks to Meta.

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

## Run on Google Cloud

The hosted app runs on Cloud Run at https://musha-383490753931.us-central1.run.app. One service, built from the `Dockerfile`, serves the API and the web app. SQLite keeps the shops, with every receipt, photo, voice note and counter still inside it as bytes. [Litestream](https://litestream.io) copies each change to a Cloud Storage bucket within a second, and a request that writes waits for that copy before it replies, for up to three seconds. Each new instance restores the database from the bucket before it serves, so the shops outlive every instance. The first start seeds the sandbox shop and its two sign-ins. Cloud Scheduler calls the hold job every three hours. Secret Manager keeps the PayPal secret, the jobs key, the Gemini key, the two PayPal sandbox emails, the passwords of the two sandbox sign-ins, and the WhatsApp settings once they exist.

```powershell
gcloud auth login
gcloud config set project <your-project>
powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -Plan        # every step, read-only
powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -SmokeTest   # build, deploy, prove the restore
```

The script reads `.env` at run time and passes each secret to gcloud on stdin. It turns on the APIs, makes the bucket, the service account and the secrets, registers the PayPal sandbox webhook for the service URL, builds the image with Cloud Build and deploys it. The same command ships the next build. The service runs one instance at most, so the database has one writer. The instance keeps its CPU between requests, because Musha finishes receipt reads and translations after it answers. Its disk lives in memory, so sign-up makes 30 new shops an hour for every visitor together (`MUSHA_SHOPS_PER_HOUR`). [deploy/README.md](deploy/README.md) lists every setting.

**Cost.** With the CPU kept on, Cloud Run bills an instance for its whole life, and an idle instance stays up to 15 minutes after its last request. The free tier covers 240,000 vCPU-seconds a month: about 66 hours of the 1 vCPU, 1 GiB instance. The hold job's eight wakes a day can use up to 60 of those hours by themselves. Each hour past the free tier costs about US$0.07, so a month with the instance up around the clock costs about US$47. Secret Manager and Cloud Build stay inside their free tiers at Musha's traffic. The replica fits inside Cloud Storage's free 5 GB, because the database lives in the instance's 1 GiB of memory. Litestream's uploads and listings can pass the 5,000 free Class A operations a month, at US$0.005 per 1,000 after them. Artifact Registry keeps the two newest images, at about US$0.10 a GB a month past the billing account's free 0.5 GB. The Scheduler job is free among a billing account's first three jobs and costs US$0.10 a month after them. `-Budget` adds a US$5 monthly budget with email alerts, and `-CpuThrottling` switches to request-based billing, which bills only while requests run.

## Stack

FastAPI, SQLAlchemy (SQLite with Litestream, or Postgres through `DATABASE_URL`), httpx, Pillow, Google Gen AI SDK with Gemini Live, the PayPal Agent Toolkit, the WhatsApp Business Cloud API; React 19, Vite, AG Grid Enterprise and AG Charts Enterprise, the PayPal JS SDK v6, Web Audio worklets for the call; OpenCV in the counter bridge; Cloud Run, Cloud Storage, Cloud Scheduler and Secret Manager.

## Licence

MIT. See [LICENSE](LICENSE).
