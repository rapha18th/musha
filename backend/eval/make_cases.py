"""The labelled receipt set for the proof engine (plan 8.2): 120 synthetic cases from one seed.

40 genuine, 20 inflated, 20 wrong payee, 20 duplicate and 20 stale. Every case is a stock request
the owner approved (item, quantity, unit, the amount held, the payee, the approval time), the
receipt the manager sends for it, the camera that photographs it (seed, tilt, light), and the
expected failure. Each flawed case carries one flaw; the rest of its receipt is honest.

All cases run in one shop over seven weeks. The shop (products, suppliers, each supplier's till
and eighteen weeks of price history) sits at the top of cases.json, so run_eval.py rebuilds it.

    backend/.venv/Scripts/python backend/eval/make_cases.py                # writes eval/cases.json
    backend/.venv/Scripts/python backend/eval/make_cases.py --images DIR   # and every photo, to look at

Another seed draws a fresh set from the same shop: run_eval.py holds one out (seed 7) to check
that changes made while reading the main set's results hold on cases nobody looked at.
"""
import argparse
import json
import random
import statistics
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from musha.receipts import Line, ReceiptSpec, render_receipt  # noqa: E402

SEED = 2026
SHOP_TZ = timezone(timedelta(hours=2))
START = date(2026, 9, 7)  # the cases run seven weeks from here
DAYS = 48
HISTORY_FROM = date(2026, 5, 4)  # eighteen weekly purchases before the cases begin
HISTORY_WEEKS = 18
COUNTS = {"genuine": 40, "inflated": 20, "wrong_payee": 20, "duplicate": 20, "stale": 20}

SHOP = {"name": "Chipo's Tuck Shop", "place": "Mbare, Harare", "owner": "Chipo", "manager": "Farai"}

# The businesses: as the shop records them, as their till prints them, and how the till numbers receipts
SUPPLIERS = {
    "Mbare wholesale": ("Mbare Wholesale (Pvt) Ltd", "Stand 14, Mbare Musika, Harare", "MW-{:06d}", 41200, "dejavu"),
    "Mbare bakery": ("Mbare Bakery", "Stand 3, Ardbennie Road, Mbare", "No. {}", 8800, "lucida"),
    "Gulf cash and carry": ("Gulf Cash & Carry", "Gulf Complex, Harare", "INV{:07d}", 1003400, "consolas"),
    "Siyaso traders": ("Siyaso Traders", "Siyaso Market, Mbare, Harare", "ST {:05d}", 5100, "courier"),
    "Magaba general dealers": ("Magaba General Dealers", "Magaba, Mbare, Harare", "RCPT#{:06d}", 27100, "narrow"),
    "Musika fresh produce": ("Musika Fresh Produce", "Mbare Musika, Harare", "FP-{:05d}", 310, "liberation"),
}
# Businesses the shop has never bought from
OTHERS = {
    "Rufaro supermarket": ("Rufaro Supermarket", "Rufaro, Mbare, Harare", "{:06d}", 120400, "consolas"),
    "Matapi traders": ("Matapi Traders", "Matapi Flats, Mbare", "MT-{:05d}", 2210, "courier"),
    "Mupedzanhamo traders": ("Mupedzanhamo Traders", "Mupedzanhamo Market, Mbare", "MP{:05d}", 9050, "liberation"),
    "Remembrance cash and carry": ("Remembrance Cash & Carry (Pvt) Ltd", "Remembrance Drive, Mbare",
                                   "RC{:06d}", 77100, "dejavu"),
    "Mbare general dealers": ("Mbare General Dealers", "Stand 41, Mbare, Harare", "GD {:05d}", 6400, "lucida"),
    "Siyaso hardware": ("Siyaso Hardware", "Siyaso Market, Mbare, Harare", "SH{:05d}", 1830, "narrow"),
    "Mbare fresh market": ("Mbare Fresh Market", "Mbare Musika, Harare", "MF {:04d}", 4410, "consolas"),
}
# A business whose name shares words with the payee the request named
NEAR = {
    "Mbare wholesale": ["Mbare general dealers"],
    "Mbare bakery": ["Mbare general dealers", "Mbare fresh market"],
    "Gulf cash and carry": ["Remembrance cash and carry"],
    "Siyaso traders": ["Siyaso hardware"],
    "Magaba general dealers": ["Mbare general dealers"],
    "Musika fresh produce": ["Mbare fresh market"],
}
FOOTERS = ["Thank you. Come again.", "Goods once sold are not returnable.", "Prices include VAT.",
           "Thank you for your support."]

# name, unit, shelf price, usual supplier, reorder pack, cost per unit, {other supplier: cost}, {supplier: till label}
PRODUCTS = [
    ("eggs", "trays", 550, "Mbare wholesale", 10, 450, {"Gulf cash and carry": 470},
     {"Mbare wholesale": "EGGS LARGE 30S TRAY", "Gulf cash and carry": "EGGS TRAY X30"}),
    ("bread", "loaves", 110, "Mbare bakery", 20, 86, {}, {"Mbare bakery": "WHITE BREAD 700G"}),
    ("cooking oil", "2 L bottles", 450, "Mbare wholesale", 6, 365, {"Gulf cash and carry": 375},
     {"Mbare wholesale": "COOKING OIL 2L", "Gulf cash and carry": "CKNG OIL 2LT"}),
    ("sugar", "2 kg packs", 230, "Mbare wholesale", 10, 183, {"Siyaso traders": 188},
     {"Mbare wholesale": "WHITE SUGAR 2KG", "Siyaso traders": "SUGAR 2KG PKT"}),
    ("mealie meal", "10 kg bags", 780, "Siyaso traders", 5, 640, {}, {"Siyaso traders": "ROLLER MEALIE MEAL 10KG"}),
    ("rice", "2 kg packs", 320, "Gulf cash and carry", 10, 255, {}, {"Gulf cash and carry": "RICE LONG GRAIN 2KG"}),
    ("salt", "1 kg packs", 70, "Gulf cash and carry", 12, 48, {}, {"Gulf cash and carry": "TABLE SALT 1KG"}),
    ("bath soap", "bars", 80, "Magaba general dealers", 24, 55, {}, {"Magaba general dealers": "BATH SOAP BAR 200G"}),
    ("candles", "packs", 150, "Magaba general dealers", 10, 115, {}, {"Magaba general dealers": "CANDLES 6S"}),
    ("tomatoes", "crates", 1500, "Musika fresh produce", 2, 1150, {}, {"Musika fresh produce": "TOMATOES CRATE"}),
]


def round5(cents: float) -> int:
    return max(5, int(round(cents / 5)) * 5)


def dollars_up(cents: float) -> int:
    return -(-int(round(cents)) // 100) * 100


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def at_shop(day: date, minutes: int) -> datetime:
    return datetime.combine(day, time(0, 0), SHOP_TZ) + timedelta(minutes=minutes)


class Builder:
    def __init__(self, seed: int = SEED):
        self.seed = seed
        self.rng = random.Random(seed)
        self.products = {p[0]: p for p in PRODUCTS}
        self.history = self._history()
        self.cases: list[dict] = []

    # The shop's past purchases: weekly, with a slow drift and a little noise
    def _history(self) -> list[dict]:
        out = []
        for name, _, _, usual, _, cost, others, _ in PRODUCTS:
            for supplier, base in [(usual, cost), *others.items()]:
                drift = self.rng.uniform(-0.02, 0.04)
                for w in range(HISTORY_WEEKS if supplier == usual else HISTORY_WEEKS // 2):
                    weeks = HISTORY_WEEKS - 1 - w if supplier == usual else HISTORY_WEEKS - 1 - 2 * w
                    day = HISTORY_FROM + timedelta(weeks=weeks)
                    unit = round5(base * (1 + drift * weeks / HISTORY_WEEKS + self.rng.gauss(0, 0.012)))
                    out.append({"item": name, "supplier": supplier, "unit_cents": unit,
                                "bought_at": utc_iso(at_shop(day, 9 * 60 + self.rng.randint(0, 300)))})
        return sorted(out, key=lambda p: p["bought_at"])

    def usual(self, item: str, payee: str) -> int:
        """The median of the last eight purchases before the cases, from this payee when there are any."""
        points = [p for p in self.history if p["item"] == item]
        theirs = [p for p in points if p["supplier"] == payee]
        use = sorted(theirs or points, key=lambda p: p["bought_at"], reverse=True)[:8]
        return int(statistics.median(p["unit_cents"] for p in use))

    # Pieces of a case

    def pick(self, i: int):
        """Products in turn, so every product appears in every class."""
        return PRODUCTS[(i + self.rng.randint(0, 2)) % len(PRODUCTS)]

    def quantity(self, product) -> int:
        pack = product[4]
        q = pack * self.rng.randint(1, 4)
        if pack > 2 and self.rng.random() < 0.2:
            q += self.rng.randint(1, pack - 1)  # an odd amount now and then
        return q

    def approval(self, lo: int = 0, hi: int = DAYS - 1) -> datetime:
        day = START + timedelta(days=self.rng.randint(lo, hi))
        return at_shop(day, self.rng.randint(7 * 60, 14 * 60 + 30))

    def printed(self, after: datetime, days: int = 0) -> datetime:
        """When the till printed the receipt: after approval, in shop hours."""
        if days == 0:
            start = after + timedelta(minutes=self.rng.randint(20, 240))
            if start.hour >= 18:
                start = at_shop(after.date() + timedelta(days=1), self.rng.randint(8 * 60, 11 * 60))
            return start
        return at_shop(after.date() + timedelta(days=days), self.rng.randint(8 * 60, 16 * 60))

    def uploaded(self, printed: datetime) -> datetime:
        r = self.rng.random()
        if r < 0.7:
            return printed + timedelta(minutes=self.rng.randint(3, 90))
        if r < 0.9:
            return printed + timedelta(minutes=self.rng.randint(90, 360))
        return at_shop(printed.date() + timedelta(days=1), self.rng.randint(7 * 60, 10 * 60))

    def camera(self) -> dict:
        r = self.rng.random()
        if r < 0.55:
            tilt = None  # the seed's own small angle
        elif r < 0.85:
            tilt = round(self.rng.choice((-1, 1)) * self.rng.uniform(4, 8), 1)
        else:
            tilt = round(self.rng.choice((-1, 1)) * self.rng.uniform(9, 14), 1)
        light = self.rng.choices(["", "dim", "warm", "bright", "shade"], [45, 15, 15, 10, 15])[0]
        return {"seed": self.rng.randrange(10 ** 9), "tilt": tilt, "light": light}

    def business(self, name: str) -> tuple:
        return SUPPLIERS.get(name) or OTHERS[name]

    def label(self, product, supplier: str) -> str:
        name, unit = product[0], product[1]
        return product[7].get(supplier) or f"{name} ({unit})"

    def receipt(self, product, supplier: str, qty: int, unit: int, at: datetime, extra=()) -> dict:
        printed, address, _, _, font = self.business(supplier)
        lines = [{"name": self.label(product, supplier), "quantity": qty, "unit_cents": unit}, *extra]
        return {"payee": printed, "address": address, "receipt_no": "", "at": at.replace(tzinfo=None).isoformat(),
                "lines": lines, "footer": self.rng.choice(FOOTERS),
                "till": self.rng.choice(["", "", "1", "2", "3"]), "font": font, "business": supplier,
                "total_cents": sum(x["quantity"] * x["unit_cents"] for x in lines)}

    def case(self, expected: str, variant: str, product, qty: int, hold: int, payee: str, approved: datetime,
             receipt: dict, uploaded: datetime, note: str, photo: dict | None = None) -> dict:
        n = sum(1 for c in self.cases if c["expected"] == expected) + 1
        c = {"id": f"{expected}-{n:02d}", "expected": expected, "variant": variant,
             "request": {"kind": "stock", "item": product[0], "quantity": qty, "unit": product[1],
                         "hold_cents": hold, "payee": payee, "approved_at": utc_iso(approved)},
             "uploaded_at": utc_iso(uploaded), "receipt": receipt, "photo": photo or self.camera(),
             "usual_cents": self.usual(product[0], payee), "note": note}
        self.cases.append(c)
        return c

    # The five classes

    def genuine(self):
        variants = ["usual"] * 22 + ["known_other"] * 6 + ["extra_line"] * 6 + ["split_lines"] * 2 + ["next_days"] * 4
        self.rng.shuffle(variants)
        seconds = [p for p in PRODUCTS if p[6]]
        for i, variant in enumerate(variants):
            product = self.rng.choice(seconds) if variant == "known_other" else self.pick(i)
            payee = self.rng.choice(list(product[6])) if variant == "known_other" else product[3]
            usual = self.usual(product[0], payee)
            qty = self.quantity(product)
            unit = round5(usual * self.rng.uniform(0.92, 1.04))
            approved = self.approval()
            at = self.printed(approved, self.rng.randint(1, 4) if variant == "next_days" else 0)
            extra = []
            if variant == "extra_line":
                extra = [{"name": "CARRIER BAG", "quantity": self.rng.randint(1, 3), "unit_cents": 15}]
            receipt = self.receipt(product, payee, qty, unit, at, extra)
            if variant == "split_lines":
                first = qty // 2
                receipt["lines"] = [{**receipt["lines"][0], "quantity": first},
                                    {**receipt["lines"][0], "quantity": qty - first}]
            hold = max(dollars_up(qty * usual * self.rng.uniform(1.03, 1.15)), dollars_up(receipt["total_cents"]))
            self.case("genuine", variant, product, qty, hold, payee, approved, receipt, self.uploaded(at),
                      f"{qty} {product[1]} at {unit} cents against a usual {usual}.")

    def inflated(self):
        variants = ["inflated_request"] * 15 + ["over_hold"] * 5
        self.rng.shuffle(variants)
        for i, variant in enumerate(variants):
            product = self.pick(i + 3)
            payee = product[3]
            usual = self.usual(product[0], payee)
            qty = self.quantity(product)
            factor = self.rng.uniform(1.25, 1.6) if variant == "inflated_request" else self.rng.uniform(1.25, 1.5)
            unit = round5(usual * factor)
            approved = self.approval()
            at = self.printed(approved)
            receipt = self.receipt(product, payee, qty, unit, at)
            if variant == "inflated_request":  # the request asked for the inflated amount, and was approved
                hold = dollars_up(receipt["total_cents"] * self.rng.uniform(1.0, 1.05))
            else:  # the request was at the usual price; the receipt comes in above the hold
                hold = dollars_up(qty * usual * self.rng.uniform(1.03, 1.1))
            self.case("inflated", variant, product, qty, hold, payee, approved, receipt, self.uploaded(at),
                      f"{unit} cents a unit, {round((unit / usual - 1) * 100)}% above the usual {usual}.")

    def wrong_payee(self):
        variants = ["known_supplier"] * 7 + ["unknown"] * 8 + ["near_miss"] * 5
        self.rng.shuffle(variants)
        unknown = [o for o in OTHERS if o not in {n for v in NEAR.values() for n in v}]
        for i, variant in enumerate(variants):
            product = self.pick(i + 5)
            payee = product[3]
            if variant == "known_supplier":
                seller = self.rng.choice([s for s in SUPPLIERS if s != payee])
            elif variant == "unknown":
                seller = self.rng.choice(unknown)
            else:
                seller = self.rng.choice(NEAR[payee])
            usual = self.usual(product[0], payee)
            theirs = self.usual(product[0], seller) if seller in product[6] else usual
            qty = self.quantity(product)
            unit = round5(theirs * self.rng.uniform(0.94, 1.03))
            approved = self.approval()
            at = self.printed(approved)
            receipt = self.receipt(product, seller, qty, unit, at)
            hold = max(dollars_up(qty * usual * self.rng.uniform(1.03, 1.15)), dollars_up(receipt["total_cents"]))
            self.case("wrong_payee", variant, product, qty, hold, payee, approved, receipt, self.uploaded(at),
                      f"The request named {payee}; the receipt is from {seller}.")

    def duplicate(self):
        """The same receipt for a second request the same day: the photo sent again, or photographed again."""
        sources = [c for c in self.cases if c["expected"] == "genuine"
                   and self._shop(c["uploaded_at"]).date() == datetime.fromisoformat(c["receipt"]["at"]).date()
                   and self._shop(c["uploaded_at"]).hour < 17]
        picked = self.rng.sample(sources, COUNTS["duplicate"])
        for i, src in enumerate(picked):
            variant = "same_photo" if i % 2 == 0 else "photographed_again"
            req = src["request"]
            product = self.products[req["item"]]
            sent = self._shop(src["uploaded_at"])
            approved = sent + timedelta(minutes=self.rng.randint(30, 150))
            uploaded = approved + timedelta(minutes=self.rng.randint(10, 90))
            photo = {"copy_of": src["id"]} if variant == "same_photo" else {**self.camera(), "receipt_of": src["id"]}
            self.case("duplicate", variant, product, req["quantity"], req["hold_cents"], req["payee"], approved,
                      src["receipt"], uploaded, f"The receipt already sent for {src['id']}.", photo)

    def stale(self):
        variants = ["before_approval"] * 12 + ["late_upload"] * 4 + ["late_receipt"] * 4
        self.rng.shuffle(variants)
        for i, variant in enumerate(variants):
            product = self.pick(i + 7)
            payee = product[3]
            usual = self.usual(product[0], payee)
            qty = self.quantity(product)
            unit = round5(usual * self.rng.uniform(0.93, 1.04))
            if variant == "before_approval":
                approved = self.approval(lo=3)
                bought = at_shop(approved.date() - timedelta(days=self.rng.randint(1, 20)),
                                 self.rng.randint(8 * 60, 16 * 60))
                receipt = self.receipt(product, payee, qty, unit, bought)
                uploaded = approved + timedelta(minutes=self.rng.randint(20, 240))
                note = f"Printed {(approved.date() - bought.date()).days} days before approval."
            elif variant == "late_upload":
                approved = self.approval(hi=DAYS - 14)
                bought = self.printed(approved, self.rng.randint(0, 2))
                receipt = self.receipt(product, payee, qty, unit, bought)
                uploaded = at_shop(approved.date() + timedelta(days=self.rng.randint(9, 13)),
                                   self.rng.randint(8 * 60, 17 * 60))
                note = f"Sent {(uploaded.date() - approved.date()).days} days after approval."
            else:
                approved = self.approval(hi=DAYS - 14)
                bought = self.printed(approved, self.rng.randint(8, 12))
                receipt = self.receipt(product, payee, qty, unit, bought)
                uploaded = self.uploaded(bought)
                note = f"Printed {(bought.date() - approved.date()).days} days after approval."
            hold = max(dollars_up(qty * usual * self.rng.uniform(1.03, 1.15)), dollars_up(receipt["total_cents"]))
            self.case("stale", variant, product, qty, hold, payee, approved, receipt, uploaded, note)

    @staticmethod
    def _shop(iso: str) -> datetime:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(SHOP_TZ)

    def number_receipts(self):
        """Each till numbers its receipts in the order it prints them."""
        own = [c for c in self.cases if "receipt_of" not in c["photo"] and "copy_of" not in c["photo"]]
        counters = {}
        for c in sorted(own, key=lambda c: c["receipt"]["at"]):
            r = c["receipt"]
            _, _, fmt, first, _ = self.business(r["business"])
            n = counters.get(r["business"], first) + self.rng.randint(3, 40)
            counters[r["business"]] = n
            r["receipt_no"] = fmt.format(n)
        by_id = {c["id"]: c for c in self.cases}
        for c in self.cases:
            src = c["photo"].get("receipt_of") or c["photo"].get("copy_of")
            if src:
                c["receipt"] = by_id[src]["receipt"]

    def build(self) -> dict:
        self.genuine()
        self.inflated()
        self.wrong_payee()
        self.duplicate()
        self.stale()
        self.number_receipts()
        assert {k: sum(c["expected"] == k for c in self.cases) for k in COUNTS} == COUNTS
        cases = sorted(self.cases, key=lambda c: (c["uploaded_at"], c["id"]))
        return {
            "seed": self.seed,
            "shop": {**SHOP,
                     "products": [{"name": p[0], "unit": p[1], "sell_cents": p[2], "usual_payee": p[3], "pack": p[4]}
                                  for p in PRODUCTS],
                     "price_history": self.history},
            "counts": COUNTS,
            "cases": cases,
        }


def spec_of(receipt: dict) -> ReceiptSpec:
    return ReceiptSpec(payee=receipt["payee"], address=receipt["address"], receipt_no=receipt["receipt_no"],
                       at=datetime.fromisoformat(receipt["at"]),
                       lines=tuple(Line(x["name"], x["quantity"], x["unit_cents"]) for x in receipt["lines"]),
                       footer=receipt["footer"], till=receipt["till"], font=receipt["font"])


def photo_of(case: dict, by_id: dict) -> bytes:
    """The photo the manager sends. A same-photo duplicate is the earlier photo, byte for byte."""
    photo = case["photo"]
    if photo.get("copy_of"):
        return photo_of(by_id[photo["copy_of"]], by_id)
    return render_receipt(spec_of(case["receipt"]), seed=photo["seed"], tilt=photo["tilt"], light=photo["light"])


def build(seed: int = SEED) -> dict:
    return Builder(seed).build()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--out", help="defaults to eval/cases.json, or eval/cases-<seed>.json for another seed")
    ap.add_argument("--images", help="also write every photo to this folder")
    args = ap.parse_args()
    args.out = args.out or str(HERE / ("cases.json" if args.seed == SEED else f"cases-{args.seed}.json"))
    data = build(args.seed)
    Path(args.out).write_text(json.dumps(data, indent=1), encoding="utf-8")
    counts = {}
    for c in data["cases"]:
        counts[f"{c['expected']}/{c['variant']}"] = counts.get(f"{c['expected']}/{c['variant']}", 0) + 1
    print(f"{len(data['cases'])} cases to {args.out}")
    for k, v in sorted(counts.items()):
        print(f"  {k:<32} {v}")
    if args.images:
        out = Path(args.images)
        out.mkdir(parents=True, exist_ok=True)
        by_id = {c["id"]: c for c in data["cases"]}
        for c in data["cases"]:
            (out / f"{c['id']}.jpg").write_bytes(photo_of(c, by_id))
        print(f"Photos in {out}")


if __name__ == "__main__":
    main()
