"""The owner's seat: alerts, products, the week's expected cash and stock, the manager's record, the day
close, and asking Musha. The manager's phone reads alerts, products, the week and the record and closes
the day, with the manager's key;
acting on an alert, declining Musha's own request, the owner's thread and asking Musha take the owner's.
Reading the alerts and closing the day also let Musha ask for the stock running low (insights.restock)."""
import logging
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import flows, insights, ledger
from ..deps import db, guarded, model, paypal, shop_or_404
from ..models import Alert, DayClose, JsonText, Message, Shop, SpendRequest
from ..money import to_cents
from ..views import iso, message_view, request_view
from ..words import qty
from .core import MAX_ID, allow, limit, request_for, shop_for

log = logging.getLogger("musha.routes")

router = APIRouter(prefix="/api")

ACTIONS = {"low_stock": ("fund", "ask"), "cash_gap": ("ask",), "stock_gap": ("ask",)}
MAX_COUNT = 100_000  # units of one item in a day's sales or shelf count
MAX_CENTS = 10_000_000  # US$100,000.00 in one day's till


class Act(BaseModel):
    action: str  # fund | ask


class CloseDay(BaseModel):
    units_sold: dict[str, int] = {}
    counted_cash: str
    banked: str = "0"
    cash_expenses: str = "0"
    stock_counts: dict[str, int] = {}
    note: str = ""


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=500)


AlertId = Annotated[int, Path(ge=1, le=MAX_ID)]


def alert_or_404(session: Session, alert_id: int) -> Alert:
    alert = session.get(Alert, alert_id)
    if not alert:
        raise HTTPException(404, f"Alert {alert_id} is missing.")
    return alert


def owners_alert(request: Request, alert_id: AlertId, session: Session = Depends(db)) -> Alert:
    """The alert in the path, for its shop's owner alone."""
    alert = alert_or_404(session, alert_id)
    allow(request, shop_or_404(session, alert.shop_id), "owner")
    return alert


async def restock(session: Session, pp, m, shop: Shop) -> None:
    """Musha asks for the stock running low, after the alerts are raised. A failure leaves the read or the
    close as it is, and the next read asks again."""
    try:
        await insights.restock(session, pp, m, shop)
    except Exception:
        session.rollback()
        log.exception("Musha's own request for shop %s", shop.id)


@router.get("/shops/{shop_id}/alerts")
async def alerts(shop: Shop = Depends(shop_for("manager")), session: Session = Depends(db), pp=Depends(paypal),
                 m=Depends(model)):
    insights.refresh_alerts(session, shop)
    await restock(session, pp, m, shop)
    return [insights.alert_view(a) for a in insights.listed_alerts(session, shop)]


@router.post("/alerts/{alert_id}/dismiss")
def dismiss(alert: Alert = Depends(owners_alert), session: Session = Depends(db)):
    insights.dismiss(session, alert)
    return insights.alert_view(alert)


@router.post("/alerts/{alert_id}/act")
async def act(body: Act, alert: Alert = Depends(owners_alert), session: Session = Depends(db),
              pp=Depends(paypal), m=Depends(model)):
    """Fund a low-stock alert's suggested request, or put any alert to the manager as a question."""
    from .. import agent

    shop = shop_or_404(session, alert.shop_id)
    allowed = ACTIONS.get(alert.kind)
    if not allowed:
        raise HTTPException(422, "Musha acts on low-stock, cash-gap and stock-gap alerts.")
    if body.action not in allowed:
        raise HTTPException(422, f"A {alert.kind} alert takes {' or '.join(allowed)}.")
    if alert.request_id:
        raise HTTPException(409, "This alert is funded already.")
    if alert.dismissed:
        raise HTTPException(409, "This alert is closed.")
    if body.action == "ask":
        try:
            message = insights.ask_manager(session, shop, alert)
        except flows.FlowError as e:
            raise HTTPException(409, str(e))
        return {"alert": insights.alert_view(alert), "message": message_view(message), "request": None}
    s = JsonText.load(alert.data, {})
    item = str(s.get("item") or "")
    if insights.key(item) in insights.open_items(session, shop):
        raise HTTPException(409, f"There is an open request for {item} already.")
    if not insights.claim_alert(session, alert):  # a second tap lands here
        raise HTTPException(409, "This alert is funded already.")
    try:
        req = flows.create_request(session, shop, kind=s["kind"], item=s["item"], quantity=s["quantity"],
                                   unit=s["unit"], amount_cents=s["amount_cents"], payee=s["payee"],
                                   origin="alert")
    except (KeyError, flows.FlowError) as e:
        session.rollback()
        alert.dismissed = False
        session.commit()
        raise HTTPException(422, f"The alert's suggested request is incomplete: {e}")
    alert.request_id = req.id
    session.commit()
    await guarded(agent.consider(session, pp, m, req))
    return {"alert": insights.alert_view(alert), "message": None, "request": request_view(req)}


@router.post("/requests/{request_id}/decline", dependencies=[Depends(limit("decline", 20, 60))])
def decline(req: SpendRequest = Depends(request_for("owner")), session: Session = Depends(db)):
    """The owner says no to a request Musha asked for itself, while it waits for her."""
    try:
        insights.decline(session, req)
    except flows.FlowError as e:
        raise HTTPException(409, str(e))
    return request_view(req)


@router.get("/shops/{shop_id}/products")
def products(shop: Shop = Depends(shop_for("manager")), session: Session = Depends(db)):
    rows = {insights.key(r["item"]): r for r in insights.low_stock(session, shop)}
    return [{"id": p.id, "name": p.name, "unit": p.unit, "sell_cents": p.sell_cents,
             "usual_payee": p.usual_payee, "pack": p.pack, **{
                 k: rows[insights.key(p.name)][k]
                 for k in ("on_hand", "avg_daily", "days_left", "low", "suggested")}}
            for p in insights.products(session, shop)]


@router.get("/shops/{shop_id}/week")
def week(shop: Shop = Depends(shop_for("manager")), session: Session = Depends(db)):
    books = insights.Books(session, shop)
    rows = insights.expected(session, shop, books=books)
    facts = insights.week_facts(session, shop, books=books, week=rows)
    return {**insights.with_answers(session, shop, rows), "facts": facts}


@router.get("/shops/{shop_id}/record")
def manager_record(shop: Shop = Depends(shop_for("manager")), session: Session = Depends(db)):
    """The manager's record: every buy PayPal released, since the first, with its receipt."""
    return insights.record(session, shop)


@router.get("/shops/{shop_id}/ledger/by")
def ledger_by(shop: Shop = Depends(shop_for("manager")), session: Session = Depends(db)):
    """Who each line in the books belongs to, by ledger id: manager, owner or agent."""
    return {str(k): v for k, v in insights.line_by(session, shop).items()}


@router.post("/shops/{shop_id}/close-day", status_code=201, dependencies=[Depends(limit("close_day", 10, 60))])
async def close_day(body: CloseDay, shop: Shop = Depends(shop_for("manager")), session: Session = Depends(db),
                    pp=Depends(paypal), m=Depends(model)):
    """The manager's end of day: sales by item, the cash count, banking, till spending and a shelf count.
    Then Musha raises the owner's alerts and asks for the stock the close left running low."""
    today = insights.today()
    if session.scalar(select(DayClose).where(DayClose.shop_id == shop.id, DayClose.day == today)):
        raise HTTPException(409, "Today is closed already.")
    goods = {insights.key(p.name): p for p in insights.products(session, shop)}
    sold, counts = by_item(goods, body.units_sold), by_item(goods, body.stock_counts)
    try:
        counted, banked, spent = (to_cents(body.counted_cash), to_cents(body.banked),
                                  to_cents(body.cash_expenses))
    except (ArithmeticError, ValueError):
        raise HTTPException(422, "Money is a decimal amount, such as 120.50.")
    if not all(0 <= c <= MAX_CENTS for c in (counted, banked, spent)):
        raise HTTPException(422, "Money amounts run from 0.00 to 100000.00.")

    at = flows.now()
    try:
        for k, n in sold.items():
            p = goods[k]
            if n:
                ledger.post(session, shop_id=shop.id, kind=ledger.SALE, ref=f"close-sale-{p.id}-{today}",
                            item=p.name, quantity=n, amount_cents=n * p.sell_cents, at=at, source="manager",
                            memo=f"{qty(n, p.unit)} sold")
        if banked:
            ledger.post(session, shop_id=shop.id, kind=ledger.BANKED, ref=f"close-banked-{shop.id}-{today}",
                        amount_cents=banked, at=at, source="manager", memo="Banked")
        close = DayClose(shop_id=shop.id, day=today, counted_cash_cents=counted, banked_cents=banked,
                         cash_expenses_cents=spent, note=body.note.strip(), at=at,
                         stock_counts=JsonText.dump({goods[k].name: n for k, n in counts.items()}))
        session.add(close)
        session.commit()
    except IntegrityError:  # a second submit of the same close, a moment behind the first
        session.rollback()
        raise HTTPException(409, "Today is closed already.")
    raised = insights.refresh_alerts(session, shop)
    await restock(session, pp, m, shop)
    rows = insights.expected(session, shop)
    day = today.isoformat()
    return {
        "close": close_view(close),
        "cash": next(r for r in rows["cash"] if r["day"] == day),
        "stock": [r for r in rows["stock"] if r["day"] == day],
        "alerts": [insights.alert_view(a) for a in raised],
    }


def by_item(goods: dict, counts: dict[str, int]) -> dict[str, int]:
    """Counts by product: each a product of the shop, named once, a whole number in range."""
    out = {}
    for name, n in counts.items():
        k = insights.key(name)
        if k not in goods:
            raise HTTPException(422, f"The shop sells {', '.join(p.name for p in goods.values())}. "
                                     f"Count one of those in place of {name}.")
        if k in out:
            raise HTTPException(422, f"Name each item once. {goods[k].name.capitalize()} came twice.")
        if not 0 <= n <= MAX_COUNT:
            raise HTTPException(422, f"Counts are whole numbers from 0 to {MAX_COUNT}.")
        out[k] = n
    return out


def close_view(c: DayClose) -> dict:
    return {"id": c.id, "day": c.day.isoformat() if isinstance(c.day, date) else c.day,
            "counted_cash_cents": c.counted_cash_cents, "banked_cents": c.banked_cents,
            "cash_expenses_cents": c.cash_expenses_cents, "stock_counts": JsonText.load(c.stock_counts, {}),
            "note": c.note, "at": iso(c.at)}


@router.get("/shops/{shop_id}/owner/messages")
def owner_messages(shop: Shop = Depends(shop_for("owner")), session: Session = Depends(db)):
    rows = session.scalars(select(Message).where(Message.shop_id == shop.id, Message.thread == "owner")
                           .order_by(Message.id))
    return [message_view(x) for x in rows]


@router.post("/shops/{shop_id}/ask", dependencies=[Depends(limit("ask", 10, 60))])
async def ask(body: Question, shop: Shop = Depends(shop_for("owner")), session: Session = Depends(db),
              m=Depends(model)):
    question, answer = await insights.ask(session, m, shop, body.question)
    return {"question": message_view(question), "answer": message_view(answer)}
