"""Measure the proof engine on the labelled receipt set (plan 8.2).

Every case in cases.json runs through the real engine in one SQLite shop, in the order the
photos arrive: the request is approved and held through the PayPal stand-in, the manager's photo
is stored as evidence, and proof.prove scores it and acts. The counter camera and the goods
photo are left out, so these numbers measure the receipt alone. Questions come from templates.

Gemini reads each photo once. Readings are kept in readings.json by the photo's sha256, so a
rerun calls nothing. Two readers go over the same cases:
  gemini  the live model's reading of each photo
  oracle  exactly what is printed: the engine's own logic with a perfect reading
A second set from another seed (7) is held out: Gemini reads it, and it checks that engine changes
made while studying the main set's results hold on cases nobody looked at.

    backend/.venv/Scripts/python backend/eval/run_eval.py --project <gcp project>   # reads new photos live
    backend/.venv/Scripts/python backend/eval/run_eval.py --cached                  # saved readings, no key
    backend/.venv/Scripts/python backend/eval/run_eval.py --report                  # RESULTS.md from results.json

A photo rendered on another machine can differ by a few bytes; --cached then finds its reading
by the photo's case id. Writes results.json and RESULTS.md beside this script.
"""
import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import make_cases  # noqa: E402
from musha import camera, evidence, flows, proof  # noqa: E402
from musha.ai import Blob  # noqa: E402
from musha.db import Base, make_engine, make_sessionmaker  # noqa: E402
from musha.models import JsonText, Person, PricePoint, Product, Shop  # noqa: E402
from musha.receipts import reading_of  # noqa: E402
from musha.standin import StandInPayPal  # noqa: E402

CASES = HERE / "cases.json"
READINGS = HERE / "readings.json"
RESULTS = HERE / "results.json"
REPORT = HERE / "RESULTS.md"
CLASSES = ("genuine", "inflated", "wrong_payee", "duplicate", "stale")
FLAWS = CLASSES[1:]
NAMES = {"genuine": "Genuine", "inflated": "Inflated", "wrong_payee": "Wrong payee", "duplicate": "Duplicate",
         "stale": "Stale"}
WAITS = (0, 15, 45, 90)  # seconds before each try at a reading; a shared project hits quota now and then
HELD_OUT = 7  # the seed of the set kept back to check engine changes


def parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prompt_key() -> str:
    """Readings belong to the engine's prompt: a new prompt deserves new readings."""
    return sha(json.dumps([proof.READ_SYSTEM, proof.READ_SCHEMA], sort_keys=True).encode())[:12]


# The photos

def _render(case: dict) -> tuple[str, bytes]:
    return case["id"], make_cases.photo_of(case, {})


def render_all(cases: list[dict], workers: int) -> dict[str, bytes]:
    """Each case's photo; a same-photo duplicate gets its source's bytes."""
    own = [c for c in cases if not c["photo"].get("copy_of")]
    with ThreadPoolExecutor(max_workers=workers) as pool:  # Pillow lets go of the GIL while it draws
        photos = dict(pool.map(_render, own))
    for c in cases:
        if c["photo"].get("copy_of"):
            photos[c["id"]] = photos[c["photo"]["copy_of"]]
    return photos


def origin(case: dict) -> str:
    """The photo's name: its set's seed and the case it was taken for."""
    return f"{case['set']}/{case['photo'].get('copy_of') or case['id']}"


def load_set(seed: int | None = None) -> dict:
    """The main set from cases.json, or another seed's set drawn afresh."""
    data = json.loads(CASES.read_text(encoding="utf-8")) if CASES.exists() else make_cases.build()
    if seed is not None and seed != data["seed"]:
        data = make_cases.build(seed)
    for c in data["cases"]:
        c["set"] = data["seed"]
    return data


# Readings

class Readings:
    def __init__(self, path: Path):
        self.path = path
        saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.model = saved.get("model", "")
        self.prompt = saved.get("prompt", "")
        self.items: dict[str, dict] = saved.get("readings", {})
        self.by_photo = {v["photo"]: k for k, v in self.items.items()}

    def find(self, digest: str, photo: str) -> tuple[dict | None, bool]:
        """The saved reading of this photo, and whether it was found by case id alone."""
        if digest in self.items:
            return self.items[digest], False
        if photo in self.by_photo:
            return self.items[self.by_photo[photo]], True
        return None, False

    def add(self, digest: str, photo: str, reading: dict, seconds: float, tries: int):
        self.items[digest] = {"photo": photo, "seconds": round(seconds, 2), "tries": tries,
                              "read_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "reading": reading}
        self.by_photo[photo] = digest

    def save(self):
        body = {"model": self.model, "prompt": self.prompt, "count": len(self.items),
                "readings": dict(sorted(self.items.items(), key=lambda kv: kv[1]["photo"]))}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=1), encoding="utf-8")
        tmp.replace(self.path)


class Failures(logging.Handler):
    """Counts the model calls musha.ai gave up on, by the kind of error."""
    def __init__(self):
        super().__init__(logging.WARNING)
        self.kinds: dict[str, int] = {}

    def emit(self, record):
        text = record.getMessage()
        m = re.search(r"\b(429|500|502|503|504)\b|RESOURCE_EXHAUSTED|DEADLINE_EXCEEDED|Timeout|timed out", text)
        kind = m.group(0) if m else "other"
        self.kinds[kind] = self.kinds.get(kind, 0) + 1


async def read_missing(pairs: list[tuple[dict, bytes]], readings: Readings, gemini, concurrency: int) -> dict:
    """Gemini reads every photo that has no saved reading, with the engine's own prompt."""
    todo, seen = [], set(readings.items)
    for case, data in pairs:
        digest = sha(data)
        if digest not in seen:
            seen.add(digest)
            todo.append((origin(case), data, digest))
    stats = {"photos": len(todo), "read": 0, "unread": 0, "tries": 0}
    if not todo:
        return stats
    print(f"Gemini reads {len(todo)} photos, {concurrency} at a time", flush=True)
    gate = asyncio.Semaphore(concurrency)

    async def one(photo: str, data: bytes, digest: str):
        async with gate:
            for i, wait in enumerate(WAITS):
                await asyncio.sleep(wait)
                stats["tries"] += 1
                started = time.perf_counter()
                got = await gemini.json(proof.READ_TASK, ["Read this receipt.", Blob(data, "image/jpeg")],
                                        proof.READ_SCHEMA, system=proof.READ_SYSTEM)
                if isinstance(got, dict):
                    readings.add(digest, photo, got, time.perf_counter() - started, i + 1)
                    readings.save()
                    stats["read"] += 1
                    print(f"  read {photo:<20} {time.perf_counter() - started:5.1f} s"
                          f"  ({stats['read']}/{len(todo)})", flush=True)
                    return
            stats["unread"] += 1
            print(f"  UNREAD {photo}", flush=True)

    await asyncio.gather(*(one(*t) for t in todo))
    return stats


class Reader:
    """Stands in for the model inside the engine. Receipts get the saved Gemini reading, or the
    printed truth for the oracle; every other call gets None, so questions use their templates."""
    enabled = True

    def __init__(self, kind: str, readings: Readings | None):
        self.kind = kind
        self.model = readings.model if readings else "oracle"
        self.readings = readings
        self.case: dict = {}
        self.calls: list[dict] = []
        self.by_id_fallbacks = 0

    async def json(self, task, parts, schema, *, system=""):
        if task != proof.READ_TASK:
            return None
        if self.kind == "oracle":
            self.calls.append({"seconds": 0.0})
            return reading_of(make_cases.spec_of(self.case["receipt"]))
        blob = next(p for p in parts if isinstance(p, Blob))
        saved, by_id = self.readings.find(sha(blob.data), origin(self.case))
        if saved is None:
            self.calls.append({"seconds": 0.0, "missing": True})
            return None
        self.by_id_fallbacks += by_id
        self.calls.append({"seconds": saved["seconds"]})
        return dict(saved["reading"])

    async def text(self, task, parts, *, system=""):
        return None


# The shop and the engine

class Clock:
    """Shop time for the run: each case sets it to its own moment, and it runs on from there."""
    def __init__(self):
        self.at = datetime.now(timezone.utc)
        self.mark = time.perf_counter()

    def set(self, at: datetime):
        self.at, self.mark = at, time.perf_counter()

    def now(self) -> datetime:
        return self.at + timedelta(seconds=time.perf_counter() - self.mark)


async def no_camera(session, model, shop, *, item, since, until):
    return None


async def no_photo(session, model, req):
    return proof.Check("photo", applies=False, reason="The goods photo is left out of this measurement.")


def build_shop(s, spec: dict) -> Shop:
    owner = Person(role="owner", name=spec["owner"], city="Leicester", email="owner@example.com")
    manager = Person(role="manager", name=spec["manager"], city="Harare", language="sn", email="manager@example.com")
    s.add_all([owner, manager])
    s.flush()
    shop = Shop(name=spec["name"], place=spec["place"], currency="USD", owner_id=owner.id, manager_id=manager.id)
    s.add(shop)
    s.flush()
    for p in spec["products"]:
        s.add(Product(shop_id=shop.id, **p))
    for p in spec["price_history"]:
        s.add(PricePoint(shop_id=shop.id, item=p["item"], supplier=p["supplier"], unit_cents=p["unit_cents"],
                         bought_at=parse(p["bought_at"])))
    s.commit()
    return shop


def words(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(s or "").lower()))


def read_marks(got: dict, receipt: dict) -> dict:
    """What the reader got right, against what is printed."""
    truth = reading_of(make_cases.spec_of(receipt))
    lines = got.get("items") or []
    printed = truth["items"]
    lines_ok = len(lines) == len(printed) and all(
        isinstance(a, dict) and proof._number(a.get("quantity")) == b["quantity"]
        and proof._cents(a.get("line_total")) == round(b["line_total"] * 100) for a, b in zip(lines, printed))
    return {"total": proof._cents(got.get("total")) == receipt["total_cents"],
            "payee": words(got.get("payee")) == words(receipt["payee"]),
            "receipt_no": proof._norm_no(str(got.get("receipt_no") or "")) == proof._norm_no(receipt["receipt_no"]),
            "date": proof._day(got.get("date")) == datetime.fromisoformat(receipt["at"]).date(),
            "lines": lines_ok}


def goods_cents(receipt: dict) -> int:
    """What the receipt charges for the requested goods: the lines printed under the goods' own label.
    The generator prints that label first; a carrier bag after it is no part of the request."""
    label = receipt["lines"][0]["name"]
    return sum(x["quantity"] * x["unit_cents"] for x in receipt["lines"] if x["name"] == label)


async def run_case(s, pp, shop, reader: Reader, clock: Clock, case: dict, photo: bytes) -> dict:
    r = case["request"]
    approved = parse(r["approved_at"])
    clock.set(approved - timedelta(minutes=10))
    req = flows.create_request(s, shop, item=r["item"], quantity=r["quantity"], unit=r["unit"],
                               amount_cents=r["hold_cents"], payee=r["payee"], kind=r["kind"])
    await flows.open_order(s, pp, req)
    clock.set(approved)
    await flows.authorize(s, pp, req)
    uploaded = parse(case["uploaded_at"])
    clock.set(uploaded)
    a = evidence.store_attachment(s, shop, kind="receipt", data=photo, content_type="image/jpeg", request_id=req.id)
    a.uploaded_at = uploaded
    s.commit()
    reader.case, reader.calls = case, []
    started = time.perf_counter()
    p = await proof.prove(s, pp, reader, req)
    engine = time.perf_counter() - started
    read_seconds = sum(c["seconds"] for c in reader.calls)

    checks = {c["key"]: c for c in JsonText.load(p.checks)}
    failed = [k for k, c in checks.items() if c["applies"] and not c["passed"]]
    match_failed = checks["match"].get("data", {}).get("failed", [])
    caught = {"inflated": "price" in failed, "wrong_payee": "payee" in match_failed,
              "duplicate": "duplicate" in failed, "stale": "freshness" in failed}
    detected = sorted(k for k, hit in caught.items() if hit and p.decision != "release")
    got = JsonText.load(a.reading, {}) or {}
    total = case["receipt"]["total_cents"]
    goods = goods_cents(case["receipt"])
    captured = req.hold.captured_cents
    return {
        "id": case["id"], "expected": case["expected"], "variant": case["variant"],
        "decision": p.decision, "score": p.score, "failed": failed, "match_failed": match_failed,
        "detected": detected, "reasons": [checks[k]["reason"] for k in failed],
        "hold_cents": req.hold.amount_cents, "receipt_total_cents": total, "goods_cents": goods,
        "captured_cents": captured,
        "money_exact": p.decision != "release" or captured == min(goods, req.hold.amount_cents),
        "read": {"source": got.get("source", ""), "copied": "copied_from" in got, "called": bool(reader.calls),
                 **({} if not got or "copied_from" in got else read_marks(got, case["receipt"]))},
        "read_seconds": round(read_seconds, 2), "engine_seconds": round(engine, 3),
        "seconds": round(read_seconds + engine, 2),
    }


async def run_reader(kind: str, data: dict, photos: dict, readings: Readings | None, work: Path) -> tuple[list, int]:
    db = work / f"{kind}-{data['seed']}.db"
    engine = make_engine(f"sqlite:///{db.as_posix()}")
    Base.metadata.create_all(engine)
    sessions = make_sessionmaker(engine)
    reader = Reader(kind, readings)
    clock = Clock()
    saved = (camera.delivery_check, proof.photo_check, flows.now, proof.now)
    camera.delivery_check, proof.photo_check, flows.now, proof.now = no_camera, no_photo, clock.now, clock.now
    rows = []
    try:
        with sessions() as s:
            shop = build_shop(s, data["shop"])
            pp = StandInPayPal()
            for case in data["cases"]:
                row = await run_case(s, pp, shop, reader, clock, case, photos[case["id"]])
                rows.append(row)
                mark = "ok  " if (row["decision"] == "release") == (row["expected"] == "genuine") else "MISS"
                print(f"{kind:<6} {mark} {row['id']:<16} {row['decision']:<8} {row['score']:>3}"
                      f"  failed={','.join(row['failed']) or '-'}", flush=True)
    finally:
        camera.delivery_check, proof.photo_check, flows.now, proof.now = saved
        engine.dispose()
    return rows, reader.by_id_fallbacks


# The numbers

def pct(n: int, d: int) -> float | None:
    return round(n / d, 3) if d else None


def nearest_rank(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, -(-int(q * 100) * len(ordered) // 100) - 1)]


def metrics(rows: list[dict]) -> dict:
    out = {"cases": len(rows), "by_failure": {}}
    for flaw in FLAWS:
        tp = sum(1 for r in rows if r["expected"] == flaw and flaw in r["detected"])
        fp = sum(1 for r in rows if r["expected"] != flaw and flaw in r["detected"])
        fn = sum(1 for r in rows if r["expected"] == flaw) - tp
        out["by_failure"][flaw] = {"cases": tp + fn, "tp": tp, "fp": fp, "fn": fn,
                                   "precision": pct(tp, tp + fp), "recall": pct(tp, tp + fn)}
    genuine = [r for r in rows if r["expected"] == "genuine"]
    flawed = [r for r in rows if r["expected"] != "genuine"]
    released = [r for r in genuine if r["decision"] == "release"]
    out["genuine_released"] = {"n": len(released), "of": len(genuine), "rate": pct(len(released), len(genuine))}
    out["flawed_released"] = {"n": sum(r["decision"] == "release" for r in flawed), "of": len(flawed),
                              "ids": [r["id"] for r in flawed if r["decision"] == "release"]}
    exact = [r for r in released if r["captured_cents"] == min(_goods(r), r["hold_cents"])]
    out["money_exact"] = {"n": len(exact), "of": len(released),
                          "captured_cents": sum(r["captured_cents"] for r in released),
                          "receipt_cents": sum(r["receipt_total_cents"] for r in released),
                          "goods_cents": sum(_goods(r) for r in released),
                          "other_lines": [r["id"] for r in released if _goods(r) != r["receipt_total_cents"]],
                          "off": [r["id"] for r in released if r not in exact]}
    fresh = [r for r in rows if "total" in r["read"]]
    out["read_accuracy"] = {"photos": len(fresh), **{k: pct(sum(r["read"][k] for r in fresh), len(fresh))
                                                     for k in ("total", "payee", "receipt_no", "date", "lines")}}
    out["unread"] = [r["id"] for r in rows if r["failed"][:1] == ["read"]]
    seconds = [r["seconds"] for r in rows]
    out["seconds"] = {"median": round(statistics.median(seconds), 2), "p90": nearest_rank(seconds, 0.9),
                      "engine_median": round(statistics.median(r["engine_seconds"] for r in rows), 3)}
    out["confusion"] = {c: {d: sum(1 for r in rows if r["expected"] == c and r["decision"] == d)
                            for d in ("release", "ask", "hold")} for c in CLASSES}
    out["caught_by"] = {c: {f: sum(1 for r in rows if r["expected"] == c and f in r["detected"]) for f in FLAWS}
                        for c in CLASSES}
    out["misses"] = [{"id": r["id"], "variant": r["variant"], "decision": r["decision"], "score": r["score"],
                      "failed": r["failed"], "reasons": r["reasons"]}
                     for r in rows if (r["decision"] == "release") != (r["expected"] == "genuine")
                     or (r["expected"] != "genuine" and r["expected"] not in r["detected"])]
    return out


def _goods(row: dict) -> int:
    return row.get("goods_cents", row["receipt_total_cents"])  # rows saved before goods_cents was kept


def fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def share(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def report(results: dict) -> str:
    g = results["readers"].get("gemini") or results["readers"]["oracle"]
    o = results["readers"].get("oracle")
    m = g["metrics"]
    who = results["model"] if "gemini" in results["readers"] else "the oracle"
    lines = [
        "# Proof engine results",
        "",
        f"{m['cases']} labelled receipt photos run through the real engine in one shop over seven weeks. "
        f"{who} reads every photo. The goods photo and the counter camera are left out, so the receipt "
        f"decides alone: Match 25, Price 15, Duplicate 20 and Freshness 10.",
        "",
        "| Failure | Cases | Precision | Recall |" + (" Oracle P / R |" if o else ""),
        "|---|---|---|---|" + ("---|" if o else ""),
    ]
    for flaw in FLAWS:
        v = m["by_failure"][flaw]
        row = f"| {NAMES[flaw]} | {v['cases']} | {fmt(v['precision'])} | {fmt(v['recall'])} |"
        if o:
            w = o["metrics"]["by_failure"][flaw]
            row += f" {fmt(w['precision'])} / {fmt(w['recall'])} |"
        lines.append(row)
    gr, fr, me = m["genuine_released"], m["flawed_released"], m["money_exact"]
    ra, sec = m["read_accuracy"], m["seconds"]
    lines += [
        "",
        f"- Genuine receipts released: {gr['n']} of {gr['of']}. Flawed receipts released: {fr['n']} of {fr['of']}.",
        f"- Money: {me['n']} of {me['of']} releases captured the requested goods to the cent "
        f"(US${me['captured_cents'] / 100:,.2f} captured against US${me.get('goods_cents', me['receipt_cents']) / 100:,.2f} "
        f"printed for the goods)." + (f" {len(me['other_lines'])} receipts also carried a carrier bag, which stayed "
                                      f"with the shop." if me.get("other_lines") else ""),
        f"- Upload to decision: median {sec['median']:.1f} s, p90 {sec['p90']:.1f} s. "
        f"The engine itself takes {sec['engine_median'] * 1000:.0f} ms; the rest is the model reading the photo.",
        f"- Reading, over {ra['photos']} photos: total {share(ra['total'])}, payee {share(ra['payee'])}, "
        f"receipt number {share(ra['receipt_no'])}, date {share(ra['date'])}, every line {share(ra['lines'])}.",
    ]
    if m["unread"]:
        lines.append(f"- Unread photos (Musha asked for a clearer one): {', '.join(m['unread'])}.")
    lines += ["", "Decisions by case:", "", "| Expected | Release | Ask | Hold |", "|---|---|---|---|"]
    for c in CLASSES:
        d = m["confusion"][c]
        lines.append(f"| {NAMES[c]} | {d['release']} | {d['ask']} | {d['hold']} |")
    if m["misses"]:
        lines += ["", "Misses:", ""]
        for x in m["misses"]:
            lines.append(f"- {x['id']} ({x['variant']}): {x['decision']} at {x['score']}. "
                         + (" ".join(x["reasons"]) or "Every check passed."))
    h = results.get("held_out")
    if h:
        hm = h["metrics"]
        pr = ". ".join(f"{NAMES[f]} {fmt(v['precision'])} / {fmt(v['recall'])}" for f, v in hm["by_failure"].items())
        lines += ["", "## Held out", "",
                  f"A second set of {hm['cases']}, drawn from seed {h['seed']} and read by Gemini after the engine "
                  f"changes below, to check they hold on cases nobody studied. Precision / recall: {pr}. "
                  f"Genuine released: {hm['genuine_released']['n']} of {hm['genuine_released']['of']}. "
                  f"Flawed released: {hm['flawed_released']['n']} of {hm['flawed_released']['of']}. "
                  f"Money exact to the cent: {hm['money_exact']['n']} of {hm['money_exact']['of']}. "
                  f"Upload to decision: median {hm['seconds']['median']:.1f} s, p90 {hm['seconds']['p90']:.1f} s. "
                  f"Reading, over {hm['read_accuracy']['photos']} photos: total {share(hm['read_accuracy']['total'])}, "
                  f"payee {share(hm['read_accuracy']['payee'])}, receipt number "
                  f"{share(hm['read_accuracy']['receipt_no'])}, date {share(hm['read_accuracy']['date'])}."]
        for x in hm["misses"]:
            lines.append(f"- {x['id']} ({x['variant']}): {x['decision']} at {x['score']}. "
                         + (" ".join(x["reasons"]) or "Every check passed."))
    lines += ["", NOTES.strip(), "",
              "## Run it", "",
              "```",
              "cd backend",
              ".venv/Scripts/python eval/make_cases.py            # cases.json, from seed 2026",
              ".venv/Scripts/python eval/run_eval.py --cached     # saved Gemini readings, no key",
              ".venv/Scripts/python eval/run_eval.py --project <gcp project>   # Gemini reads new photos",
              ".venv/Scripts/python eval/make_cases.py --images out/   # every photo, to look at",
              "```",
              "",
              f"Measured {results['generated'][:10]} with {results['model']}. "
              f"Per-case rows are in results.json; readings in readings.json.",
              ""]
    return "\n".join(lines)


NOTES = """
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
"""


async def run(args) -> dict:
    data = load_set()
    held = load_set(args.held_out) if args.held_out else None
    sets = [d for d in (data, held) if d]
    if args.only:
        keep = set(args.only.split(","))
        for d in sets:
            d["cases"] = [c for c in d["cases"] if c["id"] in keep]
    photos = {}
    for d in sets:
        print(f"Rendering {len(d['cases'])} photos of set {d['seed']}", flush=True)
        started = time.perf_counter()
        photos[d["seed"]] = render_all(d["cases"], args.workers)
        print(f"Rendered in {time.perf_counter() - started:.0f} s", flush=True)

    readers = [r for r in args.readers.split(",") if r]
    readings = Readings(READINGS)
    service = {}
    if "gemini" in readers:
        if readings.prompt and readings.prompt != prompt_key():
            print("The engine's reading prompt changed since readings.json was written.", flush=True)
        if not args.cached:
            if not args.project:
                sys.exit("Live reading needs --project (or GOOGLE_CLOUD_PROJECT). Use --cached for saved readings.")
            from musha.ai import Gemini
            from musha.config import Settings
            settings = Settings(paypal_client_id="", paypal_client_secret="", paypal_webhook_id="",
                                database_url="sqlite://", currency="USD", jobs_key="", gemini_project=args.project,
                                gemini_location=args.location, gemini_model=args.model)
            failures = Failures()
            log = logging.getLogger("musha.ai")
            log.addHandler(failures)
            log.setLevel(logging.WARNING)
            log.propagate = False
            readings.model, readings.prompt = args.model, prompt_key()
            pairs = [(c, photos[d["seed"]][c["id"]]) for d in sets for c in d["cases"]]
            service = await read_missing(pairs, readings, Gemini(settings), args.concurrency)
            service["errors"] = failures.kinds
            readings.save()

    work = Path(tempfile.mkdtemp(prefix="musha-eval-"))
    saved = list(readings.items.values())
    reads = [v["seconds"] for v in saved]
    results = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "model": readings.model or "oracle", "seed": data["seed"], "cases": len(data["cases"]),
               "left_out": ["photo", "camera"], "this_run": service,
               "readings": {"photos": len(saved), "tries": sum(v["tries"] for v in saved),
                            "seconds_median": round(statistics.median(reads), 2) if reads else None,
                            "seconds_p90": nearest_rank(reads, 0.9)},
               "readers": {}, "held_out": None}
    for kind in readers:
        rows, by_id = await run_reader(kind, data, photos[data["seed"]], readings if kind == "gemini" else None, work)
        results["readers"][kind] = {"metrics": {**metrics(rows), "readings_found_by_case_id": by_id}, "cases": rows}
    if held:
        kind = readers[0]
        rows, by_id = await run_reader(kind, held, photos[held["seed"]], readings if kind == "gemini" else None, work)
        results["held_out"] = {"seed": held["seed"], "reader": kind,
                               "metrics": {**metrics(rows), "readings_found_by_case_id": by_id}, "cases": rows}
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cached", action="store_true", help="use readings.json only; never call the model")
    ap.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT", ""))
    ap.add_argument("--location", default=os.getenv("GOOGLE_CLOUD_LOCATION", "global"))
    ap.add_argument("--model", default=os.getenv("MUSHA_GEMINI_MODEL", "gemini-3.8-flash"))
    ap.add_argument("--readers", default="gemini,oracle")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 2))
    ap.add_argument("--held-out", type=int, default=HELD_OUT, help="seed of the held-out set; 0 leaves it out")
    ap.add_argument("--only", help="comma-separated case ids, for a quick look (results are not written)")
    ap.add_argument("--out", default=str(RESULTS))
    ap.add_argument("--report", action="store_true", help="rewrite RESULTS.md from results.json and stop")
    args = ap.parse_args()
    logging.basicConfig(level=logging.ERROR)
    if args.report:
        REPORT.write_text(report(json.loads(RESULTS.read_text(encoding="utf-8"))), encoding="utf-8")
        return
    results = asyncio.run(run(args))
    if args.only:
        return
    out = Path(args.out)
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    (REPORT if out == RESULTS else out.with_suffix(".md")).write_text(report(results), encoding="utf-8")
    print()
    print(report(results))


if __name__ == "__main__":
    main()
