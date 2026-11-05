"""Measure the delivery check on the drawn set: 30 deliveries and 30 counter scenes without one.

Each still in stills/ is read with the engine's own reading code, camera.read: the app's system
prompt, schema, thinking level and clean-up. Readings are cached in readings.json by the still's
sha256 and the prompt, so a rerun reads only what changed. Then every still goes through the
app's path: a fresh sandbox shop, the still ingested as a counter sample, and the delivery check
asked about every product the shop sells.

    backend/.venv/Scripts/python backend/eval/camera/run_eval.py           # reads what the cache lacks
    backend/.venv/Scripts/python backend/eval/camera/run_eval.py --cached  # the cache only, no model
    backend/.venv/Scripts/python backend/eval/camera/run_eval.py --report  # RESULTS.md from results.json
    backend/.venv/Scripts/python backend/eval/camera/run_eval.py --sets main,probe  # a trial on some sets

A trial with --sets reads and scores only those sets and writes results-<sets>.json, so a prompt
can be tried on the sets it was tuned on while the test set stays unread.

A delivery is confirmed when the reading says delivery seen and its goods match the label.
A false alarm is a still without a delivery that the reading calls a delivery.
Each set is scored on its own: main (the prompt was tuned on it), probe, and fresh (held out).
Writes results.json and RESULTS.md beside this script and prints the summary.
"""
import asyncio
import hashlib
import json
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from live import MODEL, sandbox_shop

from musha import ai, camera
from musha.ai import Blob
from musha.flows import now
from musha.models import Product

HERE = Path(__file__).resolve().parent
STILLS = HERE / "stills"
LABELS = HERE / "labels.json"
CACHE = HERE / "readings.json"
RESULTS = HERE / "results.json"
REPORT = HERE / "RESULTS.md"
APP_LIMIT = ai.TIMEOUT["quick"]  # the app drops a reading that runs past this
TRIES = 4
AT_ONCE = 3


def version(system: str) -> str:
    return hashlib.sha256(f"{MODEL}\n{system}".encode()).hexdigest()[:12]


class Cached:
    """The model behind a cache. It answers camera.read from readings.json by the still's sha256
    and the prompt, and asks the live model only for what the cache lacks."""
    enabled = True
    model = MODEL

    def __init__(self, cache: dict, live=None, names: dict | None = None):
        self.cache, self.live, self.names = cache, live, names or {}
        self.misses = 0

    async def json(self, task: str, parts, schema: dict, *, system: str = ""):
        blob = next(p for p in parts if isinstance(p, Blob))
        sha = hashlib.sha256(blob.data).hexdigest()
        v = version(system)
        hit = self.cache["stills"].get(sha, {}).get("readings", {}).get(v)
        if hit:
            return hit["raw"]
        if not self.live:
            self.misses += 1
            return None
        self.cache["prompts"].setdefault(v, {"model": MODEL, "system": system,
                                             "first_used": datetime.now(timezone.utc).isoformat()})
        failed = []
        for attempt in range(1, TRIES + 1):
            started = time.monotonic()
            raw = await self.live.json(task, parts, schema, system=system)
            took = round(time.monotonic() - started, 2)
            if raw is not None:
                entry = self.cache["stills"].setdefault(sha, {"still": self.names.get(sha, ""), "readings": {}})
                entry["readings"][v] = {"raw": raw, "seconds": took, "attempts": attempt, "failed_seconds": failed}
                save(self.cache)
                return raw
            failed.append(took)
            await asyncio.sleep(10 * attempt)  # a busy quota, most often
        return None

    async def text(self, task: str, parts, *, system: str = ""):
        return None


def load_cache() -> dict:
    if CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    return {"prompts": {}, "stills": {}}


def save(cache: dict) -> None:
    CACHE.write_text(json.dumps(cache, indent=1) + "\n", encoding="utf-8")


def stills(sets: list[str] | None = None) -> list[tuple[str, dict, bytes]]:
    labels = json.loads(LABELS.read_text(encoding="utf-8"))
    return [(name, label, (STILLS / f"{name}.jpg").read_bytes()) for name, label in labels.items()
            if (STILLS / f"{name}.jpg").exists() and (not sets or label["set"] in sets)]


async def read_all(model: Cached, rows) -> None:
    """Read every still the cache lacks, a few at a time, against one sandbox shop."""
    session, shop = sandbox_shop()
    gate = asyncio.Semaphore(AT_ONCE)

    async def one(name, data):
        async with gate:
            r = await camera.read(session, model, shop, data, "image/jpeg")
            print(f"  read {name}: {r and r['line']}", flush=True)

    await asyncio.gather(*(one(name, data) for name, _, data in rows))
    session.close()


async def through_the_app(model: Cached, label: dict, data: bytes) -> dict:
    """The app's path for one still: ingest as a counter sample, then the delivery check per product."""
    session, shop = sandbox_shop()
    products = list(session.query(Product).filter_by(shop_id=shop.id).order_by(Product.id))
    since = now() - timedelta(seconds=1)
    frame = await camera.ingest_frame(session, model, shop, data=data, content_type="image/jpeg")
    checks = {}
    for p in products:
        c = await camera.delivery_check(session, model, shop, item=p.name, since=since, until=now())
        checks[p.name] = bool(c and c["seen"])
    out = {"change": frame.change, "gated": frame.change >= camera.GATE, "reading": camera.reading_of(frame),
           "checks": checks}
    session.close()
    return out


def score(name: str, label: dict, reading: dict | None, units: set[str]) -> dict:
    d = (reading or {}).get("delivery") or {"seen": False, "goods": "", "quantity_estimate": 0}
    right = bool(label["delivery"] and d["seen"] and camera.matches(label["goods"], d["goods"], units))
    return {"still": name, "set": label.get("set", "main"), "scene": label["scene"], "delivery": label["delivery"],
            "goods": label["goods"],
            "amount": label.get("amount"), "light": label["light"], "angle": label["angle"],
            "occlusion": label["occlusion"], "read": reading is not None, "seen": d["seen"],
            "seen_goods": d["goods"], "quantity_estimate": d["quantity_estimate"], "right_goods": right,
            "line": (reading or {}).get("line", "")}


def in_time(r: dict) -> bool:
    """Did the still's read finish within the app's limit? The app drops a slower read at ingest and
    reads the still again when the delivery check scores a request."""
    return r.get("read_seconds") is None or r["read_seconds"] <= APP_LIMIT


def summarize(rows: list[dict], seconds: list[float]) -> dict:
    deliveries = [r for r in rows if r["delivery"] and r["read"]]
    others = [r for r in rows if not r["delivery"] and r["read"]]
    seen = [r for r in deliveries if r["seen"]]
    right = [r for r in deliveries if r["right_goods"]]
    alarms = [r for r in others if r["seen"]]
    slow = [r for r in rows if r["read"] and not in_time(r)]
    scenes = {}
    for r in others:
        s = scenes.setdefault(r["scene"], {"stills": 0, "false_alarms": 0})
        s["stills"] += 1
        s["false_alarms"] += r["seen"]
    goods = {}
    for r in deliveries:
        g = goods.setdefault(r["goods"], {"stills": 0, "confirmed": 0})
        g["stills"] += 1
        g["confirmed"] += r["right_goods"]
    rate = lambda a, b: round(len(a) / len(b), 3) if b else None  # noqa: E731
    return {
        "deliveries": len(deliveries),
        "confirmed": len(right),
        "confirmation_rate": rate(right, deliveries),
        "delivery_seen": len(seen),
        "goods_accuracy": rate(right, seen),
        "missed": [r["still"] for r in deliveries if not r["seen"]],
        "wrong_goods": [f"{r['still']}: {r['seen_goods']}" for r in seen if not r["right_goods"]],
        "by_goods": goods,
        "stills_without_delivery": len(others),
        "false_alarms": len(alarms),
        "false_alarm_rate": rate(alarms, others),
        "false_alarm_stills": [f"{r['still']}: {r['seen_goods']}" for r in alarms],
        "by_scene": scenes,
        "unread": [r["still"] for r in rows if not r["read"]],
        "median_read_seconds": round(statistics.median(seconds), 2) if seconds else None,
        "p90_read_seconds": round(sorted(seconds)[int(0.9 * (len(seconds) - 1))], 2) if seconds else None,
        "reads_past_app_limit": sum(s > APP_LIMIT for s in seconds),
        "app_limit_seconds": APP_LIMIT,
        # the strict count: a read past the app's limit counts as unread
        "confirmed_in_time": sum(in_time(r) for r in right),
        "false_alarms_in_time": sum(in_time(r) for r in alarms),
        "slow_reads": [f"{r['still']}: {r['read_seconds']} s" for r in slow],
    }


def by_set(rows: list[dict]) -> dict:
    """A summary for each set of stills: main, probe and fresh."""
    out = {}
    for name in dict.fromkeys(r["set"] for r in rows):
        part = [r for r in rows if r["set"] == name]
        out[name] = summarize(part, [r["read_seconds"] for r in part if r.get("read_seconds") is not None])
    return out


def earlier(cache: dict, current: str, named: list, units: set[str]) -> list[dict]:
    """Summaries of the other prompts in the cache, from their cached readings alone."""
    out = []
    for v, p in cache["prompts"].items():
        if v == current:
            continue
        rows = []
        for name, label, data in named:
            hit = cache["stills"].get(hashlib.sha256(data).hexdigest(), {}).get("readings", {}).get(v)
            row = score(name, label, camera.clean(hit["raw"]) if hit else None, units)
            row["read_seconds"] = hit["seconds"] if hit else None
            rows.append(row)
        out.append({"prompt": v, "first_used": p["first_used"], "system": p["system"], "summary": by_set(rows)})
    return out


def app_path(rows: list[dict]) -> dict:
    """What the delivery check itself confirmed, product by product."""
    deliveries = [r for r in rows if r["delivery"]]
    negatives = [(r, item) for r in rows for item in r["checks"] if not r["delivery"] or item != r["goods"]]
    return {
        "confirmed_by_check": sum(r["checks"].get(r["goods"], False) for r in deliveries),
        "negative_checks": len(negatives),
        "false_confirmations": [f"{r['still']}: {item}" for r, item in negatives if r["checks"][item]],
        "gate": camera.GATE,
        "deliveries_through_gate": sum(r["gated"] for r in deliveries),
        "others_through_gate": sum(r["gated"] for r in rows if not r["delivery"]),
    }


ABOUT = {"main": "Main: tuned on", "probe": "Probe: tuned on", "fresh": "Fresh: tuned on",
         "test": "Test: held out"}
# What a look at the still shows, for the report. The scoring never reads these.
NOTES = {
    "n05-sale": "The customer faces into the shop with two egg trays. One still leaves her direction open.",
    "h04-sugar": "An elderly woman in a church uniform stands at the counter beside a bale of sugar. The second "
                 "prompt confirmed it.",
    "h07-sale": "The customer walks out with a crate of bread, her back to the camera.",
    "f11-sale": "A market trader walks out with a stack of egg trays, her back to the camera.",
    "f12-sale": "A man walks out with a bale of sugar on his head, his back to the camera.",
}
STILLS_NOTE = (
    "Stills: gemini-3.1-flash-image drew each one from a single picture of the quiet counter (gemini-3-pro-image "
    "when it failed), as a 512 px JPEG under 60 KB. Goods and amounts come from the seeded products. People, "
    "light, the phone's angle and occlusion vary. Each set was checked by eye before it was read. A still was "
    "redrawn only when its picture missed its label: a supplier drawn behind the counter, or a customer leaving "
    "drawn facing into the shop. That happened to fresh f03, f05, f07 and f11 and to test t02, t12, t15 and t18. "
    "Every still is read once per prompt. The held-out set is small, 10 deliveries and 10 other scenes, "
    "and every still shows the same counter."
)
HISTORY = [
    "The first prompt missed three main deliveries, each brought by a woman carrying a baby on her back. It "
    "called three manager restocks and one sale deliveries. The probe stills were drawn to test those failures.",
    "The second prompt says a delivery comes from outside the shop, through the front, from someone on the "
    "customer side, and that anyone can be a supplier. It names a customer carrying goods out and the manager "
    "filling shelves from a carton or the store room as a sale and a restock. The fresh stills were held out "
    "from it.",
    "The third prompt adds direction. Goods that arrive move towards the counter. A person walking from the "
    "counter towards the street with goods is a customer taking a purchase home. It was written after the "
    "fresh stills showed two such customers read as deliveries. The test stills were drawn after it was fixed "
    "and read once.",
]


def pct(a: int, b: int) -> str:
    return f"{a} of {b} ({100 * a / b:.1f}%)" if b else "none"


def money_risk() -> str:
    """What a false alarm can do to the money, from the proof engine's own weights: a receipt read in
    full with the quantity off, sent before the goods photo, scored without and with a camera hit."""
    from musha import proof

    w, parts = proof.WEIGHTS, proof.MATCH_PARTS
    full = ("payee", "currency", "item", "quantity", "sum", "extra", "total")
    match = w["match"] * (sum(parts[p] for p in full) - parts["quantity"]) / sum(parts[p] for p in full)
    checks = [proof.Check("read", passed=True), proof.Check("match", match), proof.Check("price", w["price"]),
              proof.Check("duplicate", w["duplicate"]), proof.Check("freshness", w["freshness"]),
              proof.Check("photo", 0, absent=True), proof.Check("camera", applies=False)]
    before = proof.score_of(checks)
    checks[-1] = proof.Check("camera", w["camera"])
    after = proof.score_of(checks)
    lend = (f"- A false alarm lends the Camera check's {w['camera']} points to a request for the same goods "
            "in the same window, and that can move money.")
    if proof.band(before) != "release" and proof.band(after) == "release":
        return (f"{lend} A receipt with the quantity off, sent before the goods photo, scores {before}, so Musha "
                f"asks the manager one question. With a false camera hit it scores {after}, and PayPal captures "
                "the money. A receipt that contradicts the request waits for an answer whatever its score.")
    return f"{lend} A receipt with the quantity off, sent before the goods photo, scores {before}, and {after} with it."


def report(result: dict) -> str:
    """RESULTS.md: the numbers in results.json, in a few plain lines."""
    s, app, rows = result["summary"], result["app"], result["rows"]
    sets = [k for k in ABOUT if k in s]
    short = {k: ABOUT[k].split(":")[0] for k in sets}
    lines = [
        "# Counter camera: the delivery check",
        "",
        f"Drawn counter stills, read by {result['model']} through `camera.read`: the app's own prompt, schema "
        "and clean-up. Each still then takes the app's path. It is ingested as a counter sample in a fresh "
        "sandbox shop, and the delivery check is asked about every product the shop sells.",
        "",
    ]
    for k, what in (("test", "Held out"), ("main", "The 60-still main set")):
        if k in s:
            x = s[k]
            lines.append(f"**{what}: {x['confirmed']} of {x['deliveries']} deliveries confirmed, "
                         f"{x['false_alarms']} of {x['stills_without_delivery']} false alarms.**")
            lines.append("")
    limit = next(iter(s.values()))["app_limit_seconds"]
    strict = [f"{short[k].lower()} {s[k]['confirmed_in_time']} of {s[k]['deliveries']}"
              for k in ("test", "main") if k in s]
    lines += [f"Counting a read past the app's {limit:g} s limit as unread, deliveries confirmed: "
              + "; ".join(strict) + ". The app drops such a read at ingest and reads the still again when "
              "the delivery check scores a request.", ""]
    lines += [
        f"| Set | Stills | Deliveries confirmed | Within {limit:g} s | Goods right when seen | False alarms | "
        "Median read |",
        "|---|---|---|---|---|---|---|",
    ]
    for k in sets:
        x = s[k]
        lines.append(f"| {ABOUT[k]} | {x['deliveries'] + x['stills_without_delivery']} | "
                     f"{pct(x['confirmed'], x['deliveries'])} | {x['confirmed_in_time']} of {x['deliveries']} | "
                     f"{x['confirmed']} of {x['delivery_seen']} | "
                     f"{pct(x['false_alarms'], x['stills_without_delivery'])} | {x['median_read_seconds']} s |")
    lines += [
        "",
        "- Confirmed: the reading says a delivery is seen, and its goods match the labelled product.",
        "- False alarm: a scene with nothing arriving that the reading calls a delivery.",
        "- Through the app, product by product: " + "; ".join(
            f"{short[k].lower()} {app[k]['confirmed_by_check']} of {s[k]['deliveries']} deliveries confirmed, "
            f"{len(app[k]['false_confirmations'])} of {app[k]['negative_checks']} other checks confirmed wrongly"
            for k in sets) + ".",
        money_risk(),
        "",
        STILLS_NOTE,
        "",
        "| Scene with nothing arriving | " + " | ".join(short.values()) + " |",
        "|---|" + "---|" * len(sets),
    ]
    for sc in dict.fromkeys(sc for k in sets for sc in s[k]["by_scene"]):
        cells = [(lambda b: f"{b['false_alarms']} false of {b['stills']}" if b else "")(s[k]["by_scene"].get(sc))
                 for k in sets]
        lines.append(f"| {sc} | " + " | ".join(cells) + " |")
    lines += ["", "| Delivered goods | " + " | ".join(short.values()) + " |", "|---|" + "---|" * len(sets)]
    for g in dict.fromkeys(g for k in sets for g in s[k]["by_goods"]):
        cells = [(lambda b: f"{b['confirmed']} of {b['stills']}" if b else "")(s[k]["by_goods"].get(g))
                 for k in sets]
        lines.append(f"| {g} | " + " | ".join(cells) + " |")

    wrong = [r for k in sets for r in rows if r["set"] == k and
             (not r["right_goods"] if r["delivery"] else r["seen"])]
    lines += ["", "## Misreads", ""]
    for r in wrong:
        what = f"read as {r['seen_goods'] or 'goods'} arriving" if r["seen"] else "read as nothing arriving"
        note = f" {NOTES[r['still']]}" if r["still"] in NOTES else ""
        lines.append(f"- `{r['still']}` ({short[r['set']].lower()}): {what}.{note}")
    if not wrong:
        lines.append("- None.")

    lines += ["", "## The prompt", ""]
    earlier = {e["prompt"]: e for e in result.get("earlier") or []}
    for i, (v, e) in enumerate(list(earlier.items()) + [(result["prompt"], None)]):
        summary = e["summary"] if e else s
        done = [k for k in sets if k in summary and not summary[k]["unread"]]
        parts = [f"{short[k].lower()} {summary[k]['confirmed']} of {summary[k]['deliveries']} confirmed and "
                 f"{summary[k]['false_alarms']} of {summary[k]['stills_without_delivery']} false" for k in done]
        text = HISTORY[i] + " " if i < len(HISTORY) else ""
        lines.append(f"{i + 1}. `{v}`{' (this run)' if not e else ''}. {text}Read: " + "; ".join(parts) + ".")
    lines.append("")
    lines.append("readings.json holds the full text of every prompt.")

    secs = sorted(r["read_seconds"] for r in rows if r.get("read_seconds") is not None)
    over = sum(x["reads_past_app_limit"] for x in s.values())
    slow = [x for x in secs if x > limit]
    lines += ["", "## Time and the gate", ""]
    if secs:
        took = f", taking {slow[0]:.0f} to {slow[-1]:.0f} s" if slow else ""
        lines.append(f"- Reads: median {statistics.median(secs):.1f} s, p90 {secs[int(0.9 * (len(secs) - 1))]:.1f} s "
                     f"over {len(secs)} stills. {over} ran past the app's {limit:g} s limit{took}: "
                     + ", ".join(f"`{x}`" for k in sets for x in s[k]["slow_reads"]) + ". The slow reads "
                     "gather at the start of a run, when three reads leave a fresh process together. The app "
                     "drops a read past its limit, and the delivery check reads the still again on demand.")
    lines.append(f"- The pixel gate at {camera.GATE} passed " + "; ".join(
        f"{app[k]['deliveries_through_gate']} of {s[k]['deliveries']} {short[k].lower()} deliveries"
        for k in sets) + ". The delivery check reads up to three unread stills in its window, so a delivery "
        "under the gate is checked too.")
    lines += ["", "## Run it", "", "```",
              "backend/.venv/Scripts/python backend/eval/camera/run_eval.py --cached   # from readings.json, offline",
              "backend/.venv/Scripts/python backend/eval/camera/run_eval.py            # reads what the cache lacks",
              "backend/.venv/Scripts/python backend/eval/camera/make_eval_set.py       # draws missing stills",
              "```", ""]
    return "\n".join(lines)


async def main(cached: bool, sets: list[str] | None = None):
    cache = load_cache()
    named = stills(sets)
    shas = {hashlib.sha256(data).hexdigest(): name for name, _, data in named}
    live = None
    if not cached:
        from live import gemini

        ai.TIMEOUT["quick"] = 180.0  # measure the whole read; the summary counts reads past the app's limit
        live = gemini()
    model = Cached(cache, live, shas)
    if live:
        await read_all(model, named)
    model.live = None  # the app's path below runs on the cache alone

    session, shop = sandbox_shop()
    current = version(camera.system_for(session, shop))
    units = {w for p in session.query(Product).filter_by(shop_id=shop.id) for w in camera.words(p.unit)}
    session.close()

    rows = []
    for name, label, data in named:
        a = await through_the_app(model, label, data)
        row = score(name, label, a["reading"], units)
        row.update(change=a["change"], gated=a["gated"], checks=a["checks"])
        rows.append(row)
        mark = "ok" if (row["right_goods"] if label["delivery"] else not row["seen"]) else "XX"
        print(f"{mark} {name:24} change {a['change']:.2f}  {row['line']}", flush=True)
    for name, _, data in named:
        hit = cache["stills"].get(hashlib.sha256(data).hexdigest(), {}).get("readings", {}).get(current)
        next(r for r in rows if r["still"] == name)["read_seconds"] = hit["seconds"] if hit else None

    summary = by_set(rows)
    app = {name: app_path([r for r in rows if r["set"] == name]) for name in summary}
    result = {"model": MODEL, "prompt": current, "summary": summary, "app": app,
              "earlier": earlier(cache, current, named, units),
              "rows": rows}
    if sets:  # a trial on some sets, kept out of the record
        HERE.joinpath(f"results-{'-'.join(sets)}.json").write_text(json.dumps(result, indent=1) + "\n",
                                                                   encoding="utf-8")
    else:
        RESULTS.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
        REPORT.write_text(report(result), encoding="utf-8")
    if model.misses:
        print(f"{model.misses} reads were missing from the cache")
    print(json.dumps({"summary": summary, "app": app}, indent=1))


if __name__ == "__main__":
    if "--report" in sys.argv[1:]:
        REPORT.write_text(report(json.loads(RESULTS.read_text(encoding="utf-8"))), encoding="utf-8")
    else:
        args = sys.argv[1:]
        only = args[args.index("--sets") + 1].split(",") if "--sets" in args else None
        asyncio.run(main("--cached" in args, only))
