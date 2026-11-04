"""The weekly reconciliation: PayPal's own record of the shop's money against Musha's books.

Plain code does the matching, so the lists are exact with or without a model. With the
model on, an agent reads PayPal through the PayPal Agent Toolkit's read-only tools and
writes the owner's summary: Gemini function calling with automatic calling off, an
allowlist, and at most four rounds inside one budget of AGENT_SECONDS. Musha runs every
call itself. When the budget runs out, the rule summary stands. `list_transactions` runs
on Musha's own client over the reconciliation window; invoices and disputes run through the
toolkit with Context(sandbox=True). The toolkit is optional (pip install --no-deps
paypal-agent-toolkit); without it the same tools are declared by hand.
"""
import asyncio
import functools
import json
import logging
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import flows, ledger
from .models import Hold, LedgerEntry, Payout, Shop, SpendRequest
from .money import to_cents
from .paypal import rfc3339, utc_seconds

log = logging.getLogger("musha.reconcile")

DAYS = 7
LAG = timedelta(hours=3)  # PayPal lists a transaction up to three hours after it happens
MAX_ROUNDS = 4
MAX_CALLS = 4  # tool calls Musha runs in one round
AGENT_SECONDS = 25.0  # the whole agent, every round: the owner waits on it
STEP_SECONDS = 15.0  # one model turn
MAX_PAGES = 10
READ_ONLY = {"transactions": {"list": True},
             "invoices": {"list": True, "get": True},
             "disputes": {"list": True, "get": True}}

BOOK_KINDS = {ledger.FUNDS_HELD: "held", ledger.FUNDS_CAPTURED: "captured",
              ledger.FUNDS_RELEASED: "returned"}
AUTHORIZATION, REAUTHORIZATION, VOID = "T1300", "T1301", "T1302"
PAYOUT_CODES = ("T0001", "T0000")
CODE_NAMES = {"T1300": "authorization", "T1301": "reauthorization", "T1302": "void",
              "T0006": "payment", "T0000": "payment", "T0001": "payout",
              "T1106": "reversal", "T1107": "refund"}


async def weekly(session: Session, pp, model, shop: Shop, *, at: datetime | None = None,
                 agent: bool = True) -> dict:
    """PayPal's last seven days, ending three hours ago, against the books. With `agent` off the
    exact match comes back at once, for a page that shows the tiles first and the summary after."""
    start, end = window(pp, at)
    rows = await paypal_rows(pp, start, end)
    report = compare(book_lines(session, shop.id), rows, start, end, owners(session), shop.id)
    report = {"window": {"start": rfc3339(start), "end": rfc3339(end)}, **report,
              "summary": template(report, start, end), "by": "rules", "agent": None}
    if model.enabled and agent:
        found = await _agent(pp, model, shop, report, rows)
        summary = house_style(found.pop("summary"), report)
        report["agent"] = found
        if summary:
            report["summary"], report["by"] = summary, "model"
    return report


def house_style(text: str | None, report: dict) -> str | None:
    """The agent's summary in the app's own voice: positive sentences, US$ amounts, each amount one
    the matching holds. A sentence that misses goes; with none left, the rule summary stands."""
    from . import insights

    if not isinstance(text, str) or not text.strip():
        return None
    text = insights.SIGNED.sub("", insights.DASH.sub(", ", text.strip()))
    text = re.sub(r"\bUSD ?(?=\d)", "US$", text)
    text = re.sub(r"(?<![A-Za-z])\$ ?(?=\d)", "US$", text)
    facts = {k: report.get(k) for k in ("matched", "missing_in_books", "missing_in_paypal", "mismatched")}
    kept = [x for x in insights.SENTENCE.split(text) if x.strip() and not insights.NEGATION.search(x)
            and not insights.outside(x, facts)]
    return " ".join(kept) or None


def window(pp, at: datetime | None = None) -> tuple[datetime, datetime]:
    """Seven days, ending when PayPal's list is complete, rounded up to the second."""
    end = utc_seconds((at or flows.now()) - getattr(pp, "search_lag", LAG) + timedelta(seconds=1))
    return end - timedelta(days=DAYS), end


# PayPal's side

async def paypal_rows(pp, start: datetime, end: datetime) -> list[dict]:
    rows, page = [], 1
    while True:
        answer = await pp.search_transactions(start, end, page)
        rows += [_row(d.get("transaction_info") or {}) for d in answer.get("transaction_details") or []]
        if page >= min(answer.get("total_pages") or 1, MAX_PAGES):
            return rows
        page += 1


def _row(info: dict) -> dict:
    value = (info.get("transaction_amount") or {}).get("value")
    return {"paypal_id": info.get("transaction_id", ""), "reference": info.get("paypal_reference_id", ""),
            "code": info.get("transaction_event_code", ""),
            "amount_cents": abs(to_cents(value)) if value else 0,
            "status": info.get("transaction_status", ""),
            "at": info.get("transaction_initiation_date", ""), "invoice_id": info.get("invoice_id", "")}


# The books' side

def book_lines(session: Session, shop_id: int) -> list[dict]:
    """Every line of the books PayPal should also show: money held, reauthorized, captured
    and returned, and every payout PayPal accepted. Seeded history never touched PayPal.
    `ids` are the PayPal ids a line answers to: money returned from a reauthorized hold is
    voided under either authorization."""
    holds = {h.request_id: h for h in session.scalars(
        select(Hold).join(SpendRequest, Hold.request_id == SpendRequest.id).where(SpendRequest.shop_id == shop_id))}
    lines = []
    for e in session.scalars(select(LedgerEntry).where(
            LedgerEntry.shop_id == shop_id, LedgerEntry.kind.in_(BOOK_KINDS), LedgerEntry.source != "seed")):
        pid, kind, hold = e.ref.partition(":")[2], BOOK_KINDS[e.kind], holds.get(e.request_id)
        ids = {pid, hold.authorization_id} if kind == "returned" and hold else {pid}
        lines.append({"kind": kind, "ref": e.ref, "paypal_id": pid, "ids": ids,
                      "amount_cents": e.amount_cents, "item": e.item, "request_id": e.request_id,
                      "at": flows.utc(e.at)})
    for hold in holds.values():
        if hold.reauthorized_at and hold.authorization_id != hold.first_authorization_id:
            lines.append({"kind": "reauthorized", "ref": f"reauthorized:{hold.authorization_id}",
                          "paypal_id": hold.authorization_id, "ids": {hold.authorization_id},
                          "amount_cents": hold.amount_cents, "item": hold.request.item,
                          "request_id": hold.request_id, "at": flows.utc(hold.reauthorized_at)})
    for p in session.scalars(select(Payout).where(Payout.shop_id == shop_id, Payout.status != "failed")):
        req = p.request_id and session.get(SpendRequest, p.request_id)
        lines.append({"kind": p.purpose, "ref": f"payout:{p.batch_id}", "paypal_id": p.paypal_batch_id,
                      "ids": {p.paypal_batch_id} - {""},
                      "amount_cents": p.amount_cents, "item": req.item if req else p.purpose,
                      "request_id": p.request_id, "at": flows.utc(p.created_at)})
    return lines


def owners(session: Session) -> dict[str, int]:
    """Every PayPal id Musha knows, and the shop it belongs to. One PayPal account serves
    every shop, so each shop's report leaves out the others' rows."""
    ids: dict[str, int] = {}
    for auth, first, order, shop_id in session.execute(
            select(Hold.authorization_id, Hold.first_authorization_id, SpendRequest.order_id,
                   SpendRequest.shop_id).join(SpendRequest, Hold.request_id == SpendRequest.id)):
        for x in (auth, first, order, f"musha-{first}"):
            if x:
                ids[x] = shop_id
    for ref, shop_id in session.execute(select(LedgerEntry.ref, LedgerEntry.shop_id)
                                        .where(LedgerEntry.ref.like("capture:%"))):
        ids[ref.partition(":")[2]] = shop_id
    for batch, shop_id in session.execute(select(Payout.paypal_batch_id, Payout.shop_id)):
        if batch:
            ids[batch] = shop_id
    return ids


# Matching, in plain code

def compare(lines: list[dict], rows: list[dict], start: datetime, end: datetime,
            known: dict[str, int], shop_id: int) -> dict:
    """PayPal's rows against the books. Every row no line claims is a gap, unless its ids
    belong to another shop on the same PayPal account: a second capture, a reversal or a
    refund on this shop's own money shows here."""
    used: set[int] = set()
    captured = {line["request_id"] for line in lines if line["kind"] == "captured"}
    matched, missing_in_paypal, mismatched = [], [], []
    for line in sorted((x for x in lines if start <= x["at"] <= end), key=lambda x: x["at"]):
        i, note = _match(line, rows, used, captured)
        if i is None:
            (matched if note else missing_in_paypal).append(_book(line, note=note))
            continue
        row = rows[i]
        reason = None if note else _differs(line, row)
        if reason:
            mismatched.append({**_book(line), "paypal_id": row["paypal_id"], "code": row["code"],
                               "book_cents": line["amount_cents"], "paypal_cents": row["amount_cents"],
                               "reason": reason})
        else:
            matched.append({**_book(line, note=note), "paypal_id": row["paypal_id"], "code": row["code"]})
    missing_in_books = [_paypal(r) for i, r in enumerate(rows)
                        if i not in used and _owner(r, known) in (None, shop_id)]
    return {"matched": matched, "missing_in_books": missing_in_books,
            "missing_in_paypal": missing_in_paypal, "mismatched": mismatched}


def _match(line: dict, rows: list[dict], used: set[int], captured: set) -> tuple[int | None, str]:
    """The PayPal row behind one line of the books, by PayPal's own ids first."""
    pid, kind, ids = line["paypal_id"], line["kind"], line.get("ids") or {line["paypal_id"]}

    def first(*tests) -> int | None:
        for test in tests:
            for i, r in enumerate(rows):
                if i not in used and test(r):
                    used.add(i)
                    return i
        return None

    if kind == "held":
        return first(lambda r: r["paypal_id"] == pid and r["code"] == AUTHORIZATION,
                     lambda r: r["paypal_id"] == pid), ""
    if kind == "reauthorized":
        return first(lambda r: r["paypal_id"] == pid and r["code"] == REAUTHORIZATION,
                     lambda r: r["paypal_id"] == pid), ""
    if kind == "captured":
        return first(lambda r: r["paypal_id"] == pid), ""
    if kind == "returned":
        i = first(lambda r: r["code"] == VOID and (r["reference"] in ids or r["paypal_id"] in ids))
        if i is not None:
            return i, ""
        if any(r["paypal_id"] in ids and r["status"] == "V" for r in rows):
            return None, "PayPal shows the authorization voided"
        if line["request_id"] in captured:
            return None, "closed by the final capture"
        return None, ""
    when = line["at"]
    return first(lambda r: pid and pid in (r["paypal_id"], r["reference"]),
                 lambda r: (r["code"] in PAYOUT_CODES and r["amount_cents"] == line["amount_cents"]
                            and _near(r["at"], when))), ""


def _differs(line: dict, row: dict) -> str | None:
    if row["status"] in ("D", "V") and line["kind"] in ("captured", "wages", "release"):
        return "PayPal shows it denied" if row["status"] == "D" else "PayPal shows it reversed"
    if row["amount_cents"] != line["amount_cents"]:
        return "the amounts differ"
    return None


def _owner(row: dict, known: dict[str, int]) -> int | None:
    for key in (row["paypal_id"], row["reference"], row["invoice_id"]):
        if key and key in known:
            return known[key]
    return None


def _near(at: str, when: datetime) -> bool:
    try:
        return abs(datetime.fromisoformat(at.replace("Z", "+00:00")) - when) <= timedelta(days=1)
    except ValueError:
        return False


def _book(line: dict, *, note: str = "") -> dict:
    out = {"kind": line["kind"], "ref": line["ref"], "paypal_id": line["paypal_id"],
           "amount_cents": line["amount_cents"], "item": line["item"],
           "request_id": line["request_id"], "at": rfc3339(line["at"])}
    if note:
        out["note"] = note
    return out


def _paypal(row: dict) -> dict:
    return {**row, "what": CODE_NAMES.get(row["code"], "transaction")}


def template(report: dict, start: datetime, end: datetime) -> str:
    """The owner's summary in plain words, when no model writes one."""
    span = _span(start, end)
    matched = len(report["matched"])
    gaps = [report[k] for k in ("missing_in_books", "missing_in_paypal", "mismatched")]
    if not any(gaps):
        if not matched:
            return f"{span}. PayPal and the books are both quiet."
        return f"{span}. PayPal and the books agree on all {_lines(matched)}."
    in_books, in_paypal, differ = gaps
    parts = [span + "."]
    if matched:
        parts.append(f"{_lines(matched).capitalize()} {'matches' if matched == 1 else 'match'}.")
    if in_books:
        total = sum(r["amount_cents"] for r in in_books)
        parts.append(f"PayPal shows {_lines(len(in_books))} the books lack, {flows.money(total)} in all.")
    if in_paypal:
        total = sum(r["amount_cents"] for r in in_paypal)
        parts.append(f"The books show {_lines(len(in_paypal))} PayPal has yet to list, "
                     f"{flows.money(total)} in all.")
    if differ:
        parts.append(f"{_lines(len(differ)).capitalize()} {'differs' if len(differ) == 1 else 'differ'} "
                     "between the two.")
    return " ".join(parts)


def _lines(n: int) -> str:
    return f"{n} line" if n == 1 else f"{n} lines"


def _span(start: datetime, end: datetime) -> str:
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day} to {end.day} {end:%B}"
    return f"{start.day} {start:%B} to {end.day} {end:%B}"


# The agent

@functools.cache
def _toolkit():
    """The PayPal Agent Toolkit's framework-free core, when it is installed."""
    try:
        from importlib.metadata import version

        from paypal_agent_toolkit.shared.api import PayPalAPI
        from paypal_agent_toolkit.shared.configuration import Configuration, Context, is_tool_allowed
        from paypal_agent_toolkit.shared.tools import tools
    except Exception:  # absent, or installed without what its core imports
        return None
    config = Configuration(actions=READ_ONLY, context=Context(sandbox=True))
    return SimpleNamespace(PayPalAPI=PayPalAPI, Context=Context,
                           name=f"paypal-agent-toolkit {version('paypal-agent-toolkit')}",
                           tools=[t for t in tools if is_tool_allowed(t, config)])


def declarations() -> dict:
    """The read-only tools the agent may call, from the toolkit when it is installed."""
    kit = _toolkit()
    if kit:
        decls = [{"name": t["method"], "description": t["description"].strip(),
                  "parameters": _plain(t["args_schema"].model_json_schema())} for t in kit.tools]
        source = kit.name
    else:
        decls, source = [dict(d) for d in HAND_WRITTEN], "musha"
    for d in decls:
        if d["name"] == "list_transactions":
            d["description"] += ("\n\nMusha runs this over the reconciliation window, filtered by "
                                 "transaction_id or transaction_status when you give one.")
    return {"source": source, "declarations": decls}


HAND_WRITTEN = [
    {"name": "list_transactions",
     "description": "List transactions from PayPal within a date range of up to 31 days, or one "
                    "transaction by its ID. transaction_status is D (denied), P (pending), "
                    "S (successful) or V (reversed).",
     "parameters": {"type": "object", "properties": {
         "transaction_id": {"type": "string", "description": "The ID of the transaction to retrieve."},
         "transaction_status": {"type": "string", "enum": ["D", "P", "S", "V"]},
         "start_date": {"type": "string", "description": "ISO 8601 start, seconds required."},
         "end_date": {"type": "string", "description": "ISO 8601 end, at most 31 days after the start."},
         "search_months": {"type": "integer"}, "page_size": {"type": "integer"},
         "page": {"type": "integer"}}}},
    {"name": "list_invoices",
     "description": "List invoices from PayPal, with optional pagination.",
     "parameters": {"type": "object", "properties": {
         "page": {"type": "integer", "minimum": 1, "maximum": 1000},
         "page_size": {"type": "integer", "minimum": 1, "maximum": 100},
         "total_required": {"type": "boolean"}}}},
    {"name": "get_invoice",
     "description": "Get one invoice from PayPal by its ID.",
     "parameters": {"type": "object", "properties": {"invoice_id": {"type": "string"}},
                    "required": ["invoice_id"]}},
    {"name": "list_disputes",
     "description": "List disputes from PayPal, filtered by transaction or state.",
     "parameters": {"type": "object", "properties": {
         "disputed_transaction_id": {"type": "string"},
         "dispute_state": {"type": "string", "enum": ["REQUIRED_ACTION", "REQUIRED_OTHER_PARTY_ACTION",
                                                      "UNDER_PAYPAL_REVIEW", "RESOLVED", "OPEN_INQUIRIES",
                                                      "APPEALABLE"]},
         "page_size": {"type": "integer"}}}},
    {"name": "get_dispute",
     "description": "Get one dispute from PayPal by its ID.",
     "parameters": {"type": "object", "properties": {"dispute_id": {"type": "string"}},
                    "required": ["dispute_id"]}},
]


def _plain(schema):
    """Pydantic's schema in the plain subset function calling reads: optional fields lose their
    null branch, and titles, defaults and patterns go."""
    if isinstance(schema, list):
        return [_plain(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    options = schema.get("anyOf")
    if options:
        rest = [o for o in options if o.get("type") != "null"]
        if len(rest) == 1:
            merged = {k: v for k, v in schema.items() if k != "anyOf"}
            return _plain({**merged, **rest[0]})
    out = {}
    for key, value in schema.items():
        if key in ("title", "default", "pattern"):
            continue
        out[key] = ({name: _plain(v) for name, v in value.items()} if key == "properties"
                    else _plain(value))
    return out


SYSTEM = ("You are Musha's reconciliation agent for {shop}, a shop {owner} funds from abroad. "
          "Every tool you have only reads. Read PayPal's rows with list_transactions before you "
          "write, and look at disputes when a payment looks wrong. Then write the summary for "
          "{owner}: two or three short, plain sentences. Say what matched, what either side is "
          "missing with amounts in US$, and the one thing to do next when there is one. "
          "Write plain text with short declarative sentences and commas, and skip dashes. "
          "Write positive sentences that say what PayPal and the books show; when everything matches, "
          "say so and stop. Write each amount as US$ with cents, exactly as the matching gives it.")


async def _agent(pp, model, shop: Shop, report: dict, rows: list[dict]) -> dict:
    tools = declarations()
    allowed = {d["name"] for d in tools["declarations"]}
    system = SYSTEM.format(shop=shop.name, owner=shop.owner.name)
    prompt = "Reconcile this week.\n" + json.dumps({
        "shop": shop.name, "owner": shop.owner.name, "window": report["window"],
        "musha_matching": {k: report[k] for k in ("matched", "missing_in_books", "missing_in_paypal",
                                                   "mismatched")},
    }, default=str)
    turns = (_GeminiTurns if getattr(model, "client", None) is not None else _JsonTurns)(
        model, system, prompt, tools["declarations"])
    calls, said, rounds = [], {"summary": None}, [0]

    async def loop():
        for n_round in range(1, MAX_ROUNDS + 1):
            rounds[0] = n_round
            asked, text = await turns.step(last=n_round == MAX_ROUNDS)
            if not asked:
                said["summary"] = text
                return
            results = []
            for n, (call_id, name, args) in enumerate(asked):
                result = (await _run(pp, name, args, allowed, rows, report["window"]) if n < MAX_CALLS
                          else {"error": f"Musha runs {MAX_CALLS} calls a round."})
                calls.append({"tool": name, "args": args, "ok": "error" not in result})
                results.append((call_id, name, result))
            turns.answer(results)

    try:
        await asyncio.wait_for(loop(), AGENT_SECONDS)
    except asyncio.TimeoutError:
        log.warning("reconcile agent for shop %s ran past %ss", shop.id, AGENT_SECONDS)
        said["summary"] = None
    except Exception as e:  # the model is a witness, never a single point of failure
        log.warning("reconcile agent for shop %s failed: %r", shop.id, e)
        said["summary"] = None
    return {"tools": tools["source"], "rounds": rounds[0], "calls": calls,
            "summary": (said["summary"] or "").strip() or None}


async def _run(pp, name: str, args: dict, allowed: set[str], rows: list[dict], span: dict) -> dict:
    """Run one call the model asked for, inside the read-only allowlist."""
    if name not in allowed:
        return {"error": f"{name} is outside Musha's read-only tools."}
    try:
        if name == "list_transactions":
            picked = rows
            if args.get("transaction_id"):
                picked = [r for r in picked if args["transaction_id"] in (r["paypal_id"], r["reference"])]
            if args.get("transaction_status"):
                picked = [r for r in picked if r["status"] == args["transaction_status"]]
            return {"window": span, "total_items": len(picked), "transactions": picked[:200]}
        if getattr(pp, "standin", False):
            return {"items": [], "total_items": 0}  # rehearsal PayPal keeps no invoices or disputes
        api = _toolkit_api(pp)
        if api is None:
            return {"error": "Invoices and disputes come through the PayPal Agent Toolkit. "
                             "Install it on this server to read them."}
        return _as_dict(await asyncio.wait_for(asyncio.to_thread(api.run, name, dict(args)), 15))
    except TimeoutError:
        return {"error": "PayPal took too long to answer."}
    except Exception as e:
        return {"error": str(e)[:300]}


def _toolkit_api(pp):
    kit = _toolkit()
    settings = getattr(pp, "settings", None)
    if not kit or not settings or not settings.paypal_client_id:
        return None
    return kit.PayPalAPI(client_id=settings.paypal_client_id, secret=settings.paypal_client_secret,
                         context=kit.Context(sandbox=True))


def _as_dict(out) -> dict:
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except ValueError:
            return {"result": out[:8000]}
    return out if isinstance(out, dict) else {"result": out}


class _GeminiTurns:
    """Gemini's own function calling, automatic calling off: Musha runs every call itself."""

    def __init__(self, model, system: str, prompt: str, decls: list[dict]):
        from google.genai import types

        self.types, self.model, self.system = types, model, system
        self.tool = types.Tool(function_declarations=[types.FunctionDeclaration(
            name=d["name"], description=d["description"], parameters_json_schema=d["parameters"])
            for d in decls])
        self.contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]

    async def step(self, *, last: bool) -> tuple[list[tuple], str | None]:
        t = self.types
        config = t.GenerateContentConfig(
            system_instruction=self.system, tools=[self.tool],
            thinking_config=t.ThinkingConfig(thinking_level="low"),
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
            tool_config=t.ToolConfig(function_calling_config=t.FunctionCallingConfig(
                mode="NONE" if last else "AUTO")))
        r = await asyncio.wait_for(self.model.client.aio.models.generate_content(
            model=self.model.model, contents=self.contents, config=config), STEP_SECONDS)
        asked = r.function_calls or []
        if not asked:
            return [], r.text
        self.contents.append(r.candidates[0].content)
        return [(c.id, c.name, dict(c.args or {})) for c in asked], None

    def answer(self, results: list[tuple]):
        t = self.types
        self.contents.append(t.Content(role="user", parts=[
            t.Part(function_response=t.FunctionResponse(id=call_id, name=name, response=result))
            for call_id, name, result in results]))


class _JsonTurns:
    """The same loop for a model that answers JSON, such as the scripted test model:
    each round names the calls to run, or gives the summary."""
    STEP = {"type": "object", "properties": {
        "calls": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "args": {"type": "object"}}, "required": ["name"]}},
        "summary": {"type": "string"}}}

    def __init__(self, model, system: str, prompt: str, decls: list[dict]):
        self.model, self.system = model, system
        self.parts = [prompt, "Tools you can call:\n" + json.dumps(decls)]

    async def step(self, *, last: bool) -> tuple[list[tuple], str | None]:
        parts = self.parts + (["Write the summary now."] if last else [])
        answer = await self.model.json("reconcile", parts, self.STEP, system=self.system) or {}
        asked = [] if last else [(None, c.get("name", ""), c.get("args") or {})
                                 for c in answer.get("calls") or [] if isinstance(c, dict)]
        return asked, None if asked else answer.get("summary")

    def answer(self, results: list[tuple]):
        self.parts.append("Tool results:\n" + json.dumps(
            [{"name": name, "result": result} for _, name, result in results], default=str))
