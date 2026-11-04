"""Chipo's Money view: every PayPal movement behind the shop, and Musha's reading of a question about it.

- GET /shops/{id}/money: each request PayPal held money for, with its PayPal events (the authorization,
  a reauthorization, each capture, the void or the return, each payout) and its latest proof; the money
  river, where each week's holds went; and the clock on every open hold. Built from what the books
  already hold: ledger lines keyed by PayPal's own ids, holds, proofs and payouts.
- POST /shops/{id}/money/ask: a question about the money becomes a small view spec (filter, group,
  sort, chart) that the console's grid and chart apply through their own APIs. Gemini reads the
  question when it is on, and the rules read the common questions when it is off. Either way the spec
  is checked against the grid's columns and the shop's own values, and every amount in the answer
  against the rows the spec selects.
"""
import json
import re
import statistics
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import flows, insights, ledger
from ..ai import bounded
from ..deps import db, model
from ..models import Hold, JsonText, LedgerEntry, Payout, Proof, Shop, SpendRequest
from ..views import iso
from .core import limit, shop_for

router = APIRouter(prefix="/api")

DAY = timedelta(days=1)
OPEN_HOLD = ("held", "pending")
ASK_SECONDS = 20.0  # past this the rules read the question, so the owner never waits on the model

# The grid's vocabulary. A spec names only these.
MEASURES = ("held", "released", "returned", "open")
GROUPS = ("week", "supplier", "item", "kind", "by", "state")
SORTS = ("approved", *MEASURES, "score", "supplier", "item")
CHARTS = ("bar", "donut")
KINDS = ("stock", "expense", "asset", "wages")
STATES = ("review", "held", "flagged", "released", "part", "returned")
LISTS = {"items": "item", "suppliers": "supplier", "by": "by"}  # filters matched to the shop's own values


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=300)


@router.get("/shops/{shop_id}/money")
def money(shop: Shop = Depends(shop_for("owner")), session: Session = Depends(db)):
    return money_view(session, shop)


@router.post("/shops/{shop_id}/money/ask", dependencies=[Depends(limit("money_ask", 10, 60))])
async def ask(body: Question, shop: Shop = Depends(shop_for("owner")), session: Session = Depends(db),
              m=Depends(model)):
    view = money_view(session, shop)
    return await read(m, shop, view["rows"], body.question.strip(), insights.today())


# The rows

def money_view(session: Session, shop: Shop, at: datetime | None = None) -> dict:
    at = at or flows.now()
    reqs = list(session.scalars(
        select(SpendRequest).join(Hold, Hold.request_id == SpendRequest.id)
        .where(SpendRequest.shop_id == shop.id).order_by(SpendRequest.approved_at, SpendRequest.id)))
    lines: dict[int, list[LedgerEntry]] = {}
    for e in session.scalars(select(LedgerEntry).where(
            LedgerEntry.shop_id == shop.id, LedgerEntry.request_id.is_not(None),
            LedgerEntry.kind.in_((ledger.FUNDS_HELD, ledger.FUNDS_CAPTURED, ledger.FUNDS_RELEASED)))
            .order_by(LedgerEntry.at, LedgerEntry.id)):
        lines.setdefault(e.request_id, []).append(e)
    payouts: dict[int, list[Payout]] = {}
    for p in session.scalars(select(Payout).where(Payout.shop_id == shop.id, Payout.request_id.is_not(None))
                             .order_by(Payout.created_at, Payout.id)):
        payouts.setdefault(p.request_id, []).append(p)
    proofs: dict[int, Proof] = {}
    for p in session.scalars(select(Proof).join(SpendRequest, Proof.request_id == SpendRequest.id)
                             .where(SpendRequest.shop_id == shop.id).order_by(Proof.id)):
        proofs[p.request_id] = p  # the latest run stands
    rows = [row(shop, r, r.hold, lines.get(r.id, []), payouts.get(r.id, []), proofs.get(r.id), at) for r in reqs]
    return {"now": iso(at), "currency": shop.currency, "rows": rows, "river": river(rows, at),
            "holds": clock(rows, reqs, at), "totals": totals(rows)}


def row(shop: Shop, req: SpendRequest, hold: Hold, lines: list[LedgerEntry], payouts: list[Payout],
        proof: Proof | None, at: datetime) -> dict:
    released = sum(e.amount_cents for e in lines if e.kind == ledger.FUNDS_CAPTURED)
    returned = sum(e.amount_cents for e in lines if e.kind == ledger.FUNDS_RELEASED)
    approved = flows.utc(req.approved_at or hold.authorized_at)
    wages = flows.is_wages(req)
    return {
        "id": req.id,
        "approved_at": iso(approved),
        "approved_day": approved.date().isoformat(),
        "week": monday(approved.date()).isoformat(),
        "item": req.item,
        "title": f"Wages for {shop.manager.name}" if wages else flows.describe(req),
        "kind": "wages" if wages else req.kind,
        "supplier": req.payee or (shop.manager.name if wages else ""),
        "by": asker(shop, req),
        "approved_by": "Musha's agent" if req.approved_by == "agent" else shop.owner.name,
        "held_cents": hold.amount_cents,
        "released_cents": released,
        "returned_cents": returned,
        "open_cents": max(0, hold.amount_cents - released - returned) if hold.status in OPEN_HOLD else 0,
        "state": state(req, hold, returned),
        "status": req.status,
        "authorization_id": "" if seeded(lines) else hold.first_authorization_id,
        "seeded": seeded(lines),
        "score": proof.score if proof else None,
        "proof": proof and proof_view(proof),
        "events": events(hold, lines, payouts),
    }


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def asker(shop: Shop, req: SpendRequest) -> str:
    """Who asked for the money: the manager, Musha for stock it asked for itself, else the owner."""
    if req.origin == "manager":
        return shop.manager.name
    return "Musha" if req.origin == flows.MUSHA else shop.owner.name


def state(req: SpendRequest, hold: Hold, returned: int) -> str:
    if hold.status == "pending":
        return "review"
    if hold.status == "held":
        return "flagged" if req.status == "flagged" else "held"
    if hold.status == "voided":
        return "returned"
    return "part" if returned else "released"


def seeded(lines: list[LedgerEntry]) -> bool:
    """A buy from the shop's opening books: its lines never touched PayPal, so it carries no PayPal id."""
    return bool(lines) and all(e.source == "seed" for e in lines)


def events(hold: Hold, lines: list[LedgerEntry], payouts: list[Payout]) -> list[dict]:
    """The request's PayPal events, oldest first, each with the PayPal id it answers to."""
    out = []
    for e in lines:
        pid = "" if e.source == "seed" else e.ref.partition(":")[2]
        if e.kind == ledger.FUNDS_HELD:
            kind = "authorization"
        elif e.kind == ledger.FUNDS_CAPTURED:
            kind = "capture"
        else:  # a void returns a whole hold; a final capture returns the rest of one
            kind = "void" if hold.status == "voided" else "return"
        out.append({"at": iso(e.at), "kind": kind, "paypal_id": pid, "amount_cents": e.amount_cents,
                    "status": "", "source": e.source})
    if hold.reauthorized_at and hold.authorization_id != hold.first_authorization_id:
        out.append({"at": iso(hold.reauthorized_at), "kind": "reauthorization", "paypal_id": hold.authorization_id,
                    "amount_cents": hold.amount_cents, "status": "", "source": "job"})
    for p in payouts:
        out.append({"at": iso(p.created_at), "kind": "payout", "paypal_id": p.paypal_batch_id,
                    "amount_cents": p.amount_cents, "status": p.status, "source": p.purpose})
    return sorted(out, key=lambda x: x["at"] or "")


def proof_view(p: Proof) -> dict:
    checks = [{k: c.get(k) for k in ("key", "label", "points", "max", "applies", "passed", "reason")}
              for c in JsonText.load(p.checks, [])]
    return {"id": p.id, "round": p.round, "score": p.score, "decision": p.decision, "spent_cents": p.spent_cents,
            "summary": p.summary, "created_at": iso(p.created_at), "checks": checks}


def river(rows: list[dict], at: datetime) -> list[dict]:
    """Where each week's holds went: released to the shop, returned to the owner, or still held. Each
    week is the holds approved in it, so the three bands add up to what PayPal held that week."""
    if not rows:
        return []
    weeks: dict[str, dict] = {}
    week = date.fromisoformat(min(r["week"] for r in rows))
    while week <= at.date():
        weeks[week.isoformat()] = {"week": week.isoformat(), "held_cents": 0, "released_cents": 0,
                                   "returned_cents": 0, "open_cents": 0, "captures": [], "voids": [],
                                   "authorizations": []}
        week += timedelta(days=7)
    for r in rows:
        w = weeks.get(r["week"])
        if w is None:
            continue
        for k in ("held_cents", "released_cents", "returned_cents", "open_cents"):
            w[k] += r[k]
        for e in r["events"]:
            band = {"capture": "captures", "void": "voids", "return": "voids", "authorization": "authorizations"}
            if e["kind"] in band:
                w[band[e["kind"]]].append(e["paypal_id"] or "opening books")
    return list(weeks.values())


def clock(rows: list[dict], reqs: list[SpendRequest], at: datetime) -> dict:
    """Each open hold against PayPal's 29 days: when the honor period ends, when Musha renews it, when
    Musha voids it, and the days left. PayPal's closed holds give the median time a hold stayed open; the
    opening books never touched PayPal, so they stay out of it."""
    open_, hours = [], []
    for r, req in zip(rows, reqs):
        hold = req.hold
        start, expires = flows.utc(hold.authorized_at), flows.utc(hold.expires_at)
        if hold.status in OPEN_HOLD:
            honor_from = flows.utc(hold.reauthorized_at or hold.authorized_at)
            open_.append({
                "request_id": r["id"], "title": r["title"], "item": r["item"], "amount_cents": hold.amount_cents,
                "authorization_id": hold.authorization_id, "state": r["state"],
                "authorized_at": iso(start), "expires_at": iso(expires),
                "honor_until": iso(honor_from + flows.HONOR_PERIOD),
                "renew_at": None if hold.reauthorized_at else iso(start + flows.HONOR_PERIOD + timedelta(hours=1)),
                "void_at": iso(expires - flows.VOID_MARGIN), "reauthorized_at": iso(hold.reauthorized_at),
                "life_days": round((expires - start) / DAY, 2),
                "days_held": round(max(0.0, (at - start) / DAY), 2),
                "days_left": round(max(0.0, (expires - at) / DAY), 2)})
            continue
        closed = [e["at"] for e in r["events"] if e["kind"] in ("capture", "void", "return")]
        if closed and not r["seeded"]:
            hours.append((datetime.fromisoformat(min(closed)) - start) / timedelta(hours=1))
    open_.sort(key=lambda h: h["days_left"])
    return {"open": open_, "closed": len(hours),
            "median_hours": round(statistics.median(hours), 1) if hours else None}


def sums(rows: list[dict]) -> dict:
    return {f"{m}_cents": sum(r[f"{m}_cents"] for r in rows) for m in MEASURES}


def totals(rows: list[dict]) -> dict:
    """The sums of every row, and PayPal's events by their own ids. The opening books count apart."""
    def count(*kinds: str) -> int:
        return sum(1 for r in rows for e in r["events"] if e["kind"] in kinds and e["paypal_id"])

    return {**sums(rows), "requests": len(rows), "authorizations": count("authorization"),
            "captures": count("capture"), "voids": count("void", "return"), "payouts": count("payout"),
            "opening": sum(1 for r in rows if r["seeded"])}


# The spec

def valid_spec(raw, rows: list[dict], today: date) -> dict:
    """The spec cut to the grid's vocabulary. Names match the shop's own items, suppliers and people,
    whatever their case; anything else goes. A chart charts a grouping, so a chart alone groups by week."""
    raw = raw if isinstance(raw, dict) else {}
    f = raw.get("filter") if isinstance(raw.get("filter"), dict) else {}
    out_filter: dict = {"since": day_of(f.get("since"), today), "until": day_of(f.get("until"), today)}
    if out_filter["since"] and out_filter["until"] and out_filter["since"] > out_filter["until"]:
        out_filter["until"] = None
    for name, column in LISTS.items():
        known = {r[column].lower(): r[column] for r in rows if r[column]}
        out_filter[name] = unique(known.get(str(v).strip().lower()) for v in listed(f.get(name)))
    out_filter["kinds"] = unique(v for v in listed(f.get("kinds")) if v in KINDS)
    out_filter["states"] = unique(v for v in listed(f.get("states")) if v in STATES)
    out_filter["has"] = f.get("has") if f.get("has") in MEASURES else None

    group = raw.get("group") if raw.get("group") in GROUPS else None
    s = raw.get("sort") if isinstance(raw.get("sort"), dict) else {}
    sort = None
    if s.get("column") in SORTS:
        sort = {"column": s["column"], "direction": "asc" if s.get("direction") == "asc" else "desc"}
    c = raw.get("chart") if isinstance(raw.get("chart"), dict) else {}
    chart = None
    if c.get("type") in CHARTS:
        chart = {"type": c["type"], "measure": c.get("measure") if c.get("measure") in MEASURES else "released"}
        group = group or "week"
    title = raw.get("title") if isinstance(raw.get("title"), str) else ""
    title = insights.DASH.sub(", ", title.strip())[:80]
    return {"filter": out_filter, "group": group, "sort": sort, "chart": chart, "title": title}


def day_of(value, today: date) -> str | None:
    """An ISO date between 2020 and tomorrow, else nothing."""
    try:
        d = date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
    return d.isoformat() if date(2020, 1, 1) <= d <= today + DAY else None


def listed(value) -> list:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def unique(values) -> list:
    out = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return out


def chosen(spec: dict, rows: list[dict]) -> list[dict]:
    """The rows the spec's filter keeps, the way the grid's filters keep them."""
    f = spec["filter"]
    keep = []
    for r in rows:
        if f["since"] and r["approved_day"] < f["since"]:
            continue
        if f["until"] and r["approved_day"] > f["until"]:
            continue
        if any(f[name] and r[column] not in f[name] for name, column in LISTS.items()):
            continue
        if (f["kinds"] and r["kind"] not in f["kinds"]) or (f["states"] and r["state"] not in f["states"]):
            continue
        if f["has"] and not r[f"{f['has']}_cents"]:
            continue
        keep.append(r)
    return keep


def measure_of(spec: dict) -> str:
    if spec["chart"]:
        return spec["chart"]["measure"]
    if spec["filter"]["has"]:
        return spec["filter"]["has"]
    if spec["sort"] and spec["sort"]["column"] in MEASURES:
        return spec["sort"]["column"]
    return "released"


def facts_of(spec: dict, rows: list[dict]) -> dict:
    """The selected rows in sums: the totals, and each group's, largest first by the spec's measure."""
    picked = chosen(spec, rows)
    m = measure_of(spec)
    groups = []
    if spec["group"]:
        keyed: dict[str, list] = {}
        for r in picked:
            keyed.setdefault(str(r[spec["group"]] or "Other"), []).append(r)
        groups = sorted(({"name": k, "requests": len(v), **sums(v)} for k, v in keyed.items()),
                        key=lambda g: -g[f"{m}_cents"])
    return {"measure": m, "requests": len(picked), **sums(picked), "groups": groups}


# Reading the question

async def read(m, shop: Shop, rows: list[dict], question: str, today: date) -> dict:
    """The question as a spec and a short answer. The model's answer stands when every amount in it
    comes from the selected rows and its sentences are positive; else the books' own sentence does."""
    raw = None
    if m.enabled:
        raw = await bounded(m.json("money_view", [
            f"Today is {today.isoformat()}.", "Rows:\n" + json.dumps(model_rows(rows), ensure_ascii=False),
            f"Question: {question}"], SPEC_SCHEMA, system=SPEC_SYSTEM.format(
                owner=shop.owner.name, manager=shop.manager.name, shop=shop.name)), ASK_SECONDS)
    by = "model" if isinstance(raw, dict) else "rules"
    spec = valid_spec(raw if by == "model" else rules(question, rows, today, shop), rows, today)
    facts = facts_of(spec, rows)
    text = None
    if by == "model":
        text = insights.checked(raw.get("answer"), facts)
        text = insights.positive(text) if text else None
    spec["title"] = spec["title"] or title_of(spec, today)
    return {"question": question, "spec": spec, "answer": text or sentence(spec, facts, shop, today),
            "by": by, "facts": facts}


def model_rows(rows: list[dict]) -> list[dict]:
    """The rows as the model reads them, with the grid's column names and amounts in US$."""
    keep = ("id", "approved_day", "week", "item", "supplier", "kind", "by", "state", "score")
    return [{**{k: r[k] for k in keep}, **{m: flows.money(r[f"{m}_cents"]) for m in MEASURES}} for r in rows]


SPEC_SYSTEM = """You turn {owner}'s question about the money of {shop} into a view of her PayPal grid. \
Her sister {manager} runs the shop. Each row is one request PayPal held money for: the day {owner} \
approved it, its week (a Monday), the item, the supplier, the kind (stock, expense, asset or wages), \
who asked (by), its state (review, held, flagged, released, part, returned), the proof score, and four \
amounts: held (what PayPal held), released (what reached the shop), returned (what came back to \
{owner}) and open (what PayPal holds now).
Fill the filter with dates as YYYY-MM-DD and names copied from the rows; "has" keeps the rows with an \
amount in that column. Group by one column when the question asks "by" or "per" something. Sort by \
the column that answers the question. Pick a chart for a grouping: bar, or donut for a share. Use "" \
or [] for what the question leaves open. The title names the view in a few words.
Write the answer in one or two short sentences from the rows, with the totals that answer the question, \
written as US$12.34 and dates as 19 Oct. Say where the money went: released to the shop, returned to \
{owner}, or held by PayPal. A supplier is where a buy was made, so name one as "on buys from" the supplier. \
Plain, declarative, positive sentences. No em-dashes."""

_LIST = {"type": "array", "items": {"type": "string"}}
SPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "filter": {"type": "object", "properties": {
            "since": {"type": "string"}, "until": {"type": "string"}, "items": _LIST, "suppliers": _LIST,
            "kinds": _LIST, "states": _LIST, "by": _LIST, "has": {"type": "string", "enum": ["", *MEASURES]}}},
        "group": {"type": "string", "enum": ["", *GROUPS]},
        "sort": {"type": "object", "properties": {"column": {"type": "string", "enum": ["", *SORTS]},
                                                  "direction": {"type": "string", "enum": ["asc", "desc"]}}},
        "chart": {"type": "object", "properties": {"type": {"type": "string", "enum": ["", *CHARTS]},
                                                   "measure": {"type": "string", "enum": ["", *MEASURES]}}},
        "title": {"type": "string"},
        "answer": {"type": "string"},
    },
    "required": ["filter", "group", "sort", "chart", "title", "answer"],
}

# The rules: the common questions, read the same way every time.
WORDS = {
    "returned": r"\b(return\w*|came back|come back|back to me|change|refund\w*|unspent|void\w*)\b",
    "open": r"\b(still held|held now|holds? now|right now|open holds?|waiting|pending)\b",
    "held": r"\b(held|holds?|holding|authori[sz]\w*)\b",
    "released": r"\b(releas\w*|spent|spend|paid|pay|captur\w*|reached|bought|went to the shop)\b",
}
GROUP_WORDS = [
    ("supplier", r"\b(by|per|each|which|every) suppliers?\b|\bsuppliers\b|\bwho did i pay\b"),
    ("item", r"\b(by|per|each|which|every) (item|product|good)s?\b|\bitems\b|\bproducts\b"),
    ("week", r"\b(by|per|each|every) week\b|\bweekly\b|\bweek by week\b|\bover time\b|\btrend\b"),
    ("by", r"\b(by|per) (person|who)\b|\bwho asked\b"),
    ("kind", r"\b(by|per) (kind|type)\b"),
]


def rules(question: str, rows: list[dict], today: date, shop: Shop) -> dict:
    """A spec for the common questions: which money, when, from whom, grouped how."""
    q = question.lower()
    measure = next((m for m in ("returned", "open", "held", "released") if re.search(WORDS[m], q)), "released")
    f: dict = {"since": None, "until": None, "items": [], "suppliers": [], "kinds": [], "states": [], "by": []}
    f["since"], f["until"] = period(q, today)
    if re.search(r"\bwages?\b", q):
        f["kinds"] = ["wages"]
    if re.search(r"\b(flagged|held for me|waiting for me|for me to decide)\b", q):
        f["states"] = ["flagged"]
    elif measure == "open":
        f["states"] = ["review", "held", "flagged"]
    f["has"] = measure
    for name, column in (("items", "item"), ("suppliers", "supplier")):
        values = {r[column] for r in rows if r[column]}
        f[name] = sorted(v for v in values if re.search(rf"\b{re.escape(v.lower())}", q)
                         or (column == "item" and re.search(rf"\b{re.escape(stem(v.lower()))}", q)))
    asked = re.search(r"\b(?:asked by|requested by|did) (\w+)|\b(\w+)'s requests\b|\b(\w+) asked\b", q)
    name = asked and next(g for g in asked.groups() if g)
    if name and name in {r["by"].lower() for r in rows}:
        f["by"] = [name]
    group = next((g for g, pattern in GROUP_WORDS if re.search(pattern, q)), None)
    sort = {"column": "approved", "direction": "desc"}
    if re.search(r"\b(biggest|largest|most|top|highest)\b", q):
        sort = {"column": measure, "direction": "desc"}
    elif re.search(r"\b(smallest|least|lowest)\b", q):
        sort = {"column": "score" if "score" in q else measure, "direction": "asc"}
    elif re.search(r"\b(oldest|first)\b", q):
        sort = {"column": "approved", "direction": "asc"}
    elif re.search(r"\b(score|proof)\b", q):
        sort = {"column": "score", "direction": "asc"}
    chart = None
    if group:
        kind = "donut" if re.search(r"\b(share|split|breakdown|pie|donut|portion)\b", q) else "bar"
        chart = {"type": kind, "measure": measure}
        if group != "week":
            sort = {"column": measure, "direction": "desc"}
    return {"filter": f, "group": group, "sort": sort, "chart": chart, "title": ""}


def stem(word: str) -> str:
    """'egg' from 'eggs', 'loaf' from 'loaves': enough to find an item named either way."""
    return re.sub(r"(es|s)$", "", word) if len(word) > 3 else word


def period(q: str, today: date) -> tuple[str | None, str | None]:
    """The dates a question names: today, yesterday, this or last week or month, or the last n days or weeks."""
    first = today.replace(day=1)
    if re.search(r"\btoday\b", q):
        return today.isoformat(), None
    if re.search(r"\byesterday\b", q):
        return (today - DAY).isoformat(), (today - DAY).isoformat()
    if re.search(r"\bthis week\b", q):
        return monday(today).isoformat(), None
    if re.search(r"\blast week\b", q):
        start = monday(today) - timedelta(days=7)
        return start.isoformat(), (start + timedelta(days=6)).isoformat()
    if re.search(r"\bthis month\b", q):
        return first.isoformat(), None
    if re.search(r"\blast month\b", q):
        end = first - DAY
        return end.replace(day=1).isoformat(), end.isoformat()
    n = re.search(r"\b(?:last|past) (\d{1,3}) (day|week)s?\b", q)
    if n:
        days = int(n.group(1)) * (7 if n.group(2) == "week" else 1)
        return (today - timedelta(days=max(0, days - 1))).isoformat(), None
    return None, None


# The answer in the books' own words

VERB = {"held": "PayPal held {x} for {shop}", "released": "PayPal released {x} to {shop}",
        "returned": "PayPal returned {x} to {owner}", "open": "PayPal holds {x} for {shop} now"}
NOUN = {"held": "Money held", "released": "Money released", "returned": "Money returned", "open": "Money held now"}
GROUP_NOUN = {"week": "week", "supplier": "supplier", "item": "item", "kind": "kind", "by": "who asked",
              "state": "state"}
STATE_WORDS = {"review": "PayPal reviewing", "held": "held", "flagged": "held for {owner}", "released": "released",
               "part": "released with change", "returned": "returned"}


def usd(cents: int) -> str:
    return f"US${cents / 100:,.2f}"


def when_words(spec: dict, today: date) -> str:
    since, until = spec["filter"]["since"], spec["filter"]["until"]
    if not since and not until:
        return ""
    if since == today.replace(day=1).isoformat() and not until:
        return " this month"
    if since == monday(today).isoformat() and not until:
        return " this week"
    if since == today.isoformat() and not until:
        return " today"
    if since and since == until:
        return f" on {short(since)}"
    if since and until:
        return f" from {short(since)} to {short(until)}"
    return f" since {short(since)}" if since else f" up to {short(until)}"


def short(iso_day: str) -> str:
    d = date.fromisoformat(iso_day)
    return f"{d.day} {d:%b}"


def sentence(spec: dict, facts: dict, shop: Shop, today: date) -> str:
    m = facts["measure"]
    n = facts["requests"]
    lead = VERB[m].format(x=usd(facts[f"{m}_cents"]), shop=shop.name, owner=shop.owner.name)
    text = f"{lead}{when_words(spec, today)}"
    text += f", across {n} {'request' if n == 1 else 'requests'}." if n else "."
    parts = [g for g in facts["groups"] if g[f"{m}_cents"]][:4]
    if parts:
        label = GROUP_NOUN[spec["group"]].capitalize()
        names = [week_name(g["name"]) if spec["group"] == "week"
                 else STATE_WORDS.get(g["name"], g["name"]).format(owner=shop.owner.name) if spec["group"] == "state"
                 else g["name"] for g in parts]
        text += f" By {label.lower()}: " + ", ".join(f"{name} {usd(g[f'{m}_cents'])}" for name, g in zip(names, parts)) + "."
    return text


def week_name(iso_day: str) -> str:
    return f"week of {short(iso_day)}"


def title_of(spec: dict, today: date) -> str:
    m = spec["chart"]["measure"] if spec["chart"] else measure_of(spec)
    text = NOUN[m] + when_words(spec, today)
    if spec["group"]:
        text += f", by {GROUP_NOUN[spec['group']]}"
    return text
