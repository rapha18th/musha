"""The owner's agent over HTTP: the mandate, the saved PayPal, a request's assessment, and the
weekly reconciliation. The saved PayPal's vault id stays on the server."""
import uuid
from decimal import InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import agent, flows, reconcile
from ..deps import db, guarded, model, paypal
from ..models import Shop, SpendRequest
from ..paypal import PayPalError
from ..views import iso
from .core import cents_of, limit, request_for, shop_for

router = APIRouter(prefix="/api")


class MandateIn(BaseModel):
    """Any field left out keeps its value. Amounts are decimal strings, e.g. "200.00"."""
    active: bool | None = None
    limit: str | float | None = None
    weekly_cap: str | float | None = None
    kinds: list[str] | None = Field(default=None, max_length=8)
    known_payees_only: bool | None = None
    price_tolerance_pct: int | None = Field(default=None, ge=0, le=100)


class SavedPayPal(BaseModel):
    vault_setup_token: str = Field(min_length=1, max_length=64)


async def mandate_view(session: Session, pp, shop: Shop) -> dict:
    m = agent.mandate_for(session, shop.id) or agent.blank(shop.id)
    spent = agent.week_total(session, shop.id)
    return {
        "shop_id": shop.id,
        "active": m.active,
        "saved": bool(m.vault_id),
        "ready": bool(m.active and m.vault_id),
        "payer_email": await agent.payer_email(pp, m),
        "limit_cents": m.limit_cents,
        "weekly_cap_cents": m.weekly_cap_cents,
        "kinds": agent.kinds_of(m),
        "known_payees_only": m.known_payees_only,
        "price_tolerance_pct": m.price_tolerance_pct,
        "week": {"since": iso(agent.week_start()), "agent_cents": spent,
                 "left_cents": max(m.weekly_cap_cents - spent, 0)},
        "decisions": [{**d, "at": iso(d["at"])} for d in agent.decisions(session, shop.id)],
        "updated_at": iso(m.updated_at),
    }


@router.get("/shops/{shop_id}/mandate")
async def get_mandate(shop: Shop = Depends(shop_for("owner")), session: Session = Depends(db), pp=Depends(paypal)):
    return await mandate_view(session, pp, shop)


@router.put("/shops/{shop_id}/mandate", dependencies=[Depends(limit("mandate", 20, 60))])
async def put_mandate(body: MandateIn, shop: Shop = Depends(shop_for("owner")), session: Session = Depends(db),
                      pp=Depends(paypal)):
    """Tighter terms apply at once. Terms that let the agent do more (turning it on, a higher
    limit or cap, a new kind, any payee, a wider price band) take the owner's approval at
    PayPal, whose window names the limits: a saved PayPal is removed, and the agent waits
    for Save PayPal. The owner key opens this route; only the owner can approve at PayPal."""
    shop_id = shop.id
    m = agent.mandate_or_new(session, shop_id)
    before = agent.terms(m)
    try:
        most = _cents(body.limit, m.limit_cents)
        cap = _cents(body.weekly_cap, m.weekly_cap_cents)
    except (InvalidOperation, ValueError):
        raise HTTPException(422, "Amounts are decimal strings, such as 200.00, up to 100000.00.")
    if most <= 0 or cap <= 0:
        raise HTTPException(422, "The limit and the weekly cap need an amount.")
    if cap < most:
        raise HTTPException(422, "The weekly cap needs to be at least the limit for one request.")
    if body.kinds is not None:
        kinds = [k.strip() for k in body.kinds if k.strip()]
        if set(kinds) - set(flows.KINDS):
            raise HTTPException(422, f"Kinds are {', '.join(flows.KINDS)}.")
        m.kinds = ",".join(k for k in flows.KINDS if k in kinds)
    m.limit_cents, m.weekly_cap_cents = most, cap
    if body.active is not None:
        m.active = body.active
    if body.known_payees_only is not None:
        m.known_payees_only = body.known_payees_only
    if body.price_tolerance_pct is not None:
        m.price_tolerance_pct = body.price_tolerance_pct
    m.updated_at = flows.now()
    old = ""
    if agent.widens(before, agent.terms(m)):
        m.vault_setup_token = ""  # an approval still open at PayPal names the old terms
        if m.vault_id:
            old = m.vault_id
            m.vault_id = m.vault_customer_id = ""
            m.active = False
            flows.say(session, shop_id, f"New terms for Musha's agent: "
                      f"{agent.listed(agent.kinds_of(m)) or 'requests'} up to {flows.money(m.limit_cents)} a "
                      f"request and {flows.money(m.weekly_cap_cents)} a week. Save your PayPal again to "
                      "approve them. The agent is off until then.", thread="owner")
    session.commit()
    if old:
        await _forget(pp, old)
    return await mandate_view(session, pp, shop)


@router.post("/shops/{shop_id}/mandate/setup-token", dependencies=[Depends(limit("setup_token", 10, 60))])
async def setup_token(request: Request, shop: Shop = Depends(shop_for("owner")), session: Session = Depends(db),
                      pp=Depends(paypal)):
    """Step one of saving PayPal for the agent: the browser's PayPalSavePaymentButton
    takes this id as its vaultSetupToken."""
    shop_id = shop.id
    m = agent.mandate_or_new(session, shop_id)
    back = request.headers.get("referer") or str(request.base_url)
    token = await guarded(pp.create_setup_token(
        return_url=back, cancel_url=back, description=agent.describe_mandate(shop, m),
        request_id=f"musha-setup-{shop_id}-{uuid.uuid4().hex[:12]}"))
    m.vault_setup_token = token["id"]
    m.updated_at = flows.now()
    session.commit()
    return {"id": token["id"]}


@router.post("/shops/{shop_id}/mandate/payment-token", dependencies=[Depends(limit("payment_token", 10, 60))])
async def payment_token(body: SavedPayPal, shop: Shop = Depends(shop_for("owner")),
                        session: Session = Depends(db), pp=Depends(paypal)):
    """Step two, after the owner approves at PayPal: swap the setup token for a saved PayPal.
    Saving PayPal for the agent turns the agent on."""
    shop_id = shop.id
    m = agent.mandate_for(session, shop_id)
    if not m or not m.vault_setup_token or body.vault_setup_token != m.vault_setup_token:
        raise HTTPException(409, "That approval belongs to another setup. Start again from Save PayPal.")
    token = await guarded(pp.create_payment_token(body.vault_setup_token,
                                                  request_id=f"musha-vault-{body.vault_setup_token}"))
    old = m.vault_id
    m.vault_id = token["id"]
    m.vault_customer_id = (token.get("customer") or {}).get("id", "")
    m.vault_setup_token = ""
    m.active = True
    m.updated_at = flows.now()
    agent.remember_payer(m.vault_id, token)
    flows.say(session, shop_id, f"Your PayPal is saved for Musha's agent. It approves "
              f"{agent.listed(agent.kinds_of(m)) or 'requests'} up to {flows.money(m.limit_cents)} a request "
              f"and {flows.money(m.weekly_cap_cents)} a week, and asks you about the rest.", thread="owner")
    session.commit()
    if old and old != m.vault_id:
        await _forget(pp, old)
    return {"saved": True}


@router.delete("/shops/{shop_id}/mandate/vault")
async def delete_vault(shop: Shop = Depends(shop_for("owner")), session: Session = Depends(db), pp=Depends(paypal)):
    """Remove the saved PayPal at PayPal and here. The agent turns off with it."""
    shop_id = shop.id
    m = agent.mandate_for(session, shop_id)
    if m and m.vault_id:
        await _forget(pp, m.vault_id, strict=True)
        m.vault_id = ""
        m.vault_customer_id = ""
        m.active = False
        m.updated_at = flows.now()
        flows.say(session, shop_id, "Your saved PayPal is removed. Musha's agent is off, and every "
                  "request comes to you.", thread="owner")
        session.commit()
    return await mandate_view(session, pp, shop)


@router.get("/requests/{request_id}/assessment")
def assessment(req: SpendRequest = Depends(request_for("owner")), session: Session = Depends(db)):
    return {**agent.assess(session, req), "recommendation": agent.recommendation(session, req)}


@router.post("/shops/{shop_id}/reconcile", dependencies=[Depends(limit("reconcile", 12, 600))])
async def reconcile_week(shop: Shop = Depends(shop_for("owner")), session: Session = Depends(db),
                         pp=Depends(paypal), m=Depends(model), agent_summary: bool = Query(True, alias="agent")):
    """PayPal's week against the books. `?agent=false` answers with the exact match at once; a
    second call with the agent on brings its summary."""
    return await guarded(reconcile.weekly(session, pp, m, shop, agent=agent_summary))


async def _forget(pp, vault_id: str, *, strict: bool = False):
    """Delete a saved PayPal at PayPal. One PayPal already removed counts as removed."""
    try:
        await pp.delete_payment_token(vault_id)
    except PayPalError as e:
        if strict and e.status != 404:
            raise HTTPException(502, {"paypal_status": e.status, "detail": e.body})


def _cents(value, current: int) -> int:
    if value is None:
        return current
    return cents_of(value, zero=True)


