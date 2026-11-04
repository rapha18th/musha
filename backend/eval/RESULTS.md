# Proof engine results

120 labelled receipt photos run through the real engine in one shop over seven weeks. gemini-3.8-flash reads every photo. The goods photo and the counter camera are left out, so the receipt decides alone: Match 25, Price 15, Duplicate 20 and Freshness 10.

| Failure | Cases | Precision | Recall | Oracle P / R |
|---|---|---|---|---|
| Inflated | 20 | 1.00 | 1.00 | 1.00 / 1.00 |
| Wrong payee | 20 | 1.00 | 1.00 | 1.00 / 1.00 |
| Duplicate | 20 | 1.00 | 1.00 | 1.00 / 1.00 |
| Stale | 20 | 1.00 | 1.00 | 1.00 / 1.00 |

- Genuine receipts released: 40 of 40. Flawed receipts released: 0 of 80.
- Money: 40 of 40 releases captured the requested goods to the cent (US$2,256.20 captured against US$2,256.20 printed for the goods). 6 receipts also carried a carrier bag, which stayed with the shop.
- Upload to decision: median 7.0 s, p90 55.7 s. The engine itself takes 435 ms; the rest is the model reading the photo.
- Reading, over 110 photos: total 100%, payee 100%, receipt number 96%, date 100%, every line 100%.

Decisions by case:

| Expected | Release | Ask | Hold |
|---|---|---|---|
| Genuine | 40 | 0 | 0 |
| Inflated | 0 | 20 | 0 |
| Wrong payee | 0 | 20 | 0 |
| Duplicate | 0 | 20 | 0 |
| Stale | 0 | 20 | 0 |

## Held out

A second set of 120, drawn from seed 7 and read by Gemini after the engine changes below, to check they hold on cases nobody studied. Precision / recall: Inflated 1.00 / 1.00. Wrong payee 1.00 / 1.00. Duplicate 1.00 / 1.00. Stale 1.00 / 1.00. Genuine released: 40 of 40. Flawed released: 0 of 80. Money exact to the cent: 40 of 40. Upload to decision: median 6.2 s, p90 13.7 s. Reading, over 110 photos: total 100%, payee 100%, receipt number 96%, date 100%.

## The set

40 genuine, 20 inflated, 20 wrong payee, 20 duplicate and 20 stale, from `make_cases.py` with seed 2026.
Ten products from six suppliers, each with its own till: its name as printed, its numbering, its font.
Photos vary in tilt (up to 14 degrees), light (dim, warm, bright, a hand's shadow) and camera.
Inflated receipts charge 25% to 60% over the usual price, with totals that match their lines.
Wrong payees come from the shop's other suppliers, from strangers, and from names that share words
with the payee. Duplicates are the photo sent again (10) or the same slip photographed again (10).
Stale receipts are dated before approval (12) or arrive more than 7 days after it (8).
Each flawed receipt carries one flaw. A detection counts when the decision is something other than
release and the matching check failed: Price, the payee part of Match, Duplicate or Freshness.

## What the first run showed

The first run used the engine as built, with the same Gemini readings. It released 40 of 80 flawed
receipts and 37 of 40 genuine ones. Precision / recall: inflated 1.00 / 1.00, wrong payee n/a / 0.00,
duplicate 1.00 / 1.00, stale n/a / 0.00. Four changes followed.

1. A contradiction holds the release. With the photo and the camera out, a receipt from another
   payee scored 90 and a stale one 86, above the release line of 85: the weights let one failed check
   through whenever the rest pass. Now another payee, another item, lines that miss the total, a
   price more than 20% over the usual, a receipt sent before, or a date outside the window turns a
   release into one question. The money goes once an answer eases the concern.
2. Payee names. The whole-name comparison took Mbare General Dealers for Magaba General Dealers
   (2 of 20 wrong payees). The leading word, which names the business, must now agree as well.
3. Item names. A till printed CKNG OIL 2LT, and 3 genuine cooking oil receipts went to a question.
   A word now also matches its consonant skeleton.
4. Receipt numbers. Gemini read No. 8850 as 8850 on 5 photos. A number now matches when one reading
   drops a printed prefix and keeps at least four digits. It guards the duplicate check by number;
   this set's duplicates were caught by photo or number either way.

## What review changed

A review of the engine found ways money could move on weak evidence. Each fix has a test. The hold
pays for the requested goods alone, and other goods on the bill go to a question. A receipt in another
currency goes to a question, then to the owner. A goods photo sent before scores zero. Only an earlier
receipt counts as the original of a duplicate. Each request gets one question. A flagged request
waits for the owner. Values the manager typed wait for the owner's tap. The counter camera counts once
it sees the delivery. Here the first fix moves money: 6 genuine receipts in each set carry a carrier
bag of 15 to 45 cents, and the capture leaves the bag out. Every other number holds.

## Limits

The receipts are rendered: clean thermal print, photographed by a simulated phone. Gemini reads
them almost perfectly, so these numbers measure the engine's judgement more than reading under
hard conditions. Worn, crumpled and handwritten receipts are the next set to build. Every flawed
receipt here lands on a question to the manager (scores 71 to 90); the owner sees it once the
manager answers. Reading times come from a shared Google Cloud project: its quota was busy while
the main set was read and quieter for the held-out set.

## Run it

```
cd backend
.venv/Scripts/python eval/make_cases.py            # cases.json, from seed 2026
.venv/Scripts/python eval/run_eval.py --cached     # saved Gemini readings, no key
.venv/Scripts/python eval/run_eval.py --project <gcp project>   # Gemini reads new photos
.venv/Scripts/python eval/make_cases.py --images out/   # every photo, to look at
```

Measured 2026-11-01 with gemini-3.8-flash. Per-case rows are in results.json; readings in readings.json.
