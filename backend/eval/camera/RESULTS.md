# Counter camera: the delivery check

Drawn counter stills, read by gemini-3.8-flash through `camera.read`: the app's own prompt, schema and clean-up. Each still then takes the app's path. It is ingested as a counter sample in a fresh sandbox shop, and the delivery check is asked about every product the shop sells.

**Held out: 10 of 10 deliveries confirmed, 0 of 10 false alarms.**

**The 60-still main set: 30 of 30 deliveries confirmed, 1 of 30 false alarms.**

Counting a read past the app's 40 s limit as unread, deliveries confirmed: test 7 of 10; main 27 of 30. The app drops such a read at ingest and reads the still again when the delivery check scores a request.

| Set | Stills | Deliveries confirmed | Within 40 s | Goods right when seen | False alarms | Median read |
|---|---|---|---|---|---|---|
| Main: tuned on | 60 | 30 of 30 (100.0%) | 27 of 30 | 30 of 30 | 1 of 30 (3.3%) | 6.09 s |
| Probe: tuned on | 12 | 5 of 6 (83.3%) | 5 of 6 | 5 of 5 | 0 of 6 (0.0%) | 5.86 s |
| Fresh: tuned on | 20 | 10 of 10 (100.0%) | 9 of 10 | 10 of 10 | 0 of 10 (0.0%) | 6.13 s |
| Test: held out | 20 | 10 of 10 (100.0%) | 7 of 10 | 10 of 10 | 0 of 10 (0.0%) | 4.54 s |

- Confirmed: the reading says a delivery is seen, and its goods match the labelled product.
- False alarm: a scene with nothing arriving that the reading calls a delivery.
- Through the app, product by product: main 30 of 30 deliveries confirmed, 1 of 210 other checks confirmed wrongly; probe 5 of 6 deliveries confirmed, 0 of 42 other checks confirmed wrongly; fresh 10 of 10 deliveries confirmed, 0 of 70 other checks confirmed wrongly; test 10 of 10 deliveries confirmed, 0 of 70 other checks confirmed wrongly.
- A false alarm lends the Camera check's 20 points to a request for the same goods in the same window, and that can move money. A receipt with the quantity off, sent before the goods photo, scores 83, so Musha asks the manager one question. With a false camera hit it scores 86, and PayPal captures the money. A receipt that contradicts the request waits for an answer whatever its score.

Stills: gemini-3.1-flash-image drew each one from a single picture of the quiet counter (gemini-3-pro-image when it failed), as a 512 px JPEG under 60 KB. Goods and amounts come from the seeded products. People, light, the phone's angle and occlusion vary. Each set was checked by eye before it was read. A still was redrawn only when its picture missed its label: a supplier drawn behind the counter, or a customer leaving drawn facing into the shop. That happened to fresh f03, f05, f07 and f11 and to test t02, t12, t15 and t18. Every still is read once per prompt. The held-out set is small, 10 deliveries and 10 other scenes, and every still shows the same counter.

| Scene with nothing arriving | Main | Probe | Fresh | Test |
|---|---|---|---|---|
| sale | 1 false of 8 | 0 false of 3 | 0 false of 3 | 0 false of 4 |
| restock | 0 false of 6 | 0 false of 2 | 0 false of 2 | 0 false of 2 |
| empty | 0 false of 6 |  | 0 false of 2 | 0 false of 1 |
| shopping | 0 false of 5 | 0 false of 1 | 0 false of 2 | 0 false of 2 |
| closed | 0 false of 5 |  | 0 false of 1 | 0 false of 1 |

| Delivered goods | Main | Probe | Fresh | Test |
|---|---|---|---|---|
| eggs | 8 of 8 | 2 of 2 | 3 of 3 | 3 of 3 |
| bread | 8 of 8 | 1 of 1 | 3 of 3 | 3 of 3 |
| cooking oil | 7 of 7 | 2 of 2 | 2 of 2 | 2 of 2 |
| sugar | 7 of 7 | 0 of 1 | 2 of 2 | 2 of 2 |

## Misreads

- `n05-sale` (main): read as eggs arriving. The customer faces into the shop with two egg trays. One still leaves her direction open.
- `h04-sugar` (probe): read as nothing arriving. An elderly woman in a church uniform stands at the counter beside a bale of sugar. The second prompt confirmed it.

## The prompt

1. `ef7a9ee0bb80`. The first prompt missed three main deliveries, each brought by a woman carrying a baby on her back. It called three manager restocks and one sale deliveries. The probe stills were drawn to test those failures. Read: main 27 of 30 confirmed and 4 of 30 false; probe 4 of 6 confirmed and 2 of 6 false.
2. `1db0194c2c6c`. The second prompt says a delivery comes from outside the shop, through the front, from someone on the customer side, and that anyone can be a supplier. It names a customer carrying goods out and the manager filling shelves from a carton or the store room as a sale and a restock. The fresh stills were held out from it. Read: main 30 of 30 confirmed and 1 of 30 false; probe 6 of 6 confirmed and 1 of 6 false; fresh 10 of 10 confirmed and 2 of 10 false.
3. `f9420fe76880` (this run). The third prompt adds direction. Goods that arrive move towards the counter. A person walking from the counter towards the street with goods is a customer taking a purchase home. It was written after the fresh stills showed two such customers read as deliveries. The test stills were drawn after it was fixed and read once. Read: main 30 of 30 confirmed and 1 of 30 false; probe 5 of 6 confirmed and 0 of 6 false; fresh 10 of 10 confirmed and 0 of 10 false; test 10 of 10 confirmed and 0 of 10 false.

readings.json holds the full text of every prompt.

## Time and the gate

- Reads: median 5.6 s, p90 12.3 s over 112 stills. 7 ran past the app's 40 s limit, taking 42 to 94 s: `d01-eggs: 94.17 s`, `d02-bread: 93.86 s`, `d03-cooking-oil: 94.48 s`, `f04-sugar: 42.44 s`, `t01-eggs: 70.31 s`, `t02-bread: 70.11 s`, `t03-cooking-oil: 74.08 s`. The slow reads gather at the start of a run, when three reads leave a fresh process together. The app drops a read past its limit, and the delivery check reads the still again on demand.
- The pixel gate at 0.05 passed 28 of 30 main deliveries; 6 of 6 probe deliveries; 10 of 10 fresh deliveries; 10 of 10 test deliveries. The delivery check reads up to three unread stills in its window, so a delivery under the gate is checked too.

## Run it

```
backend/.venv/Scripts/python backend/eval/camera/run_eval.py --cached   # from readings.json, offline
backend/.venv/Scripts/python backend/eval/camera/run_eval.py            # reads what the cache lacks
backend/.venv/Scripts/python backend/eval/camera/make_eval_set.py       # draws missing stills
```
