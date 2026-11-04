import asyncio
import io
import json
import random
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

from musha import camera, evidence, flows, ledger, proof, receipts
from musha.api import create_app
from musha.models import Alert, Attachment, JsonText, LedgerEntry, Message, Shop, SpendRequest
from musha.proof import Reading
from musha.receipts import Line, ReceiptSpec, reading_of, render_receipt
from musha.seed import reset
from musha.standin import StandInPayPal

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def no_camera(monkeypatch):
    """No counter camera unless a test puts one on the counter."""
    seen = {"answer": None, "calls": []}

    async def delivery_check(session, model, shop, *, item, since, until, **_):
        seen["calls"].append({"item": item, "since": since, "until": until})
        return seen["answer"]

    monkeypatch.setattr(camera, "delivery_check", delivery_check)
    return seen


@pytest.fixture
def pp():
    return StandInPayPal()


@pytest.fixture
def world(settings, model, pp):
    app = create_app(settings, pp, model=model)
    reset(app.state.engine, app.state.sessions)
    with app.state.sessions() as s:
        yield SimpleNamespace(app=app, s=s, shop=s.get(Shop, 1), pp=pp, model=model,
                              client=TestClient(app))


async def held(w, *, item="eggs", quantity=40, unit="trays", amount=18000, payee="Mbare wholesale",
               kind="stock") -> SpendRequest:
    req = flows.create_request(w.s, w.shop, item=item, quantity=quantity, unit=unit, amount_cents=amount,
                               payee=payee, kind=kind)
    await flows.open_order(w.s, w.pp, req)
    await flows.authorize(w.s, w.pp, req)
    assert req.status == "held"
    return req


def today() -> datetime:
    return flows.now().astimezone(proof.SHOP_TZ).replace(tzinfo=None, second=0, microsecond=0)


def spec_for(req, *, unit=430, quantity=None, payee=None, at=None, no="MW-100001", lines=None, total=None):
    lines = lines or (Line(f"{req.item} ({req.unit})", quantity or req.quantity, unit),)
    return ReceiptSpec(payee=payee or req.payee, address="Stand 9, Mbare, Harare", receipt_no=no,
                       at=at or today(), lines=tuple(lines), total_cents=total)


async def send(w, req, spec=None, *, data=None, seed=1, read=True):
    """The manager sends a receipt photo; the model reads exactly what is printed."""
    if read:
        w.model.answers[proof.READ_TASK] = reading_of(spec)
    data = data if data is not None else render_receipt(spec, seed=seed)
    evidence.store_attachment(w.s, w.shop, kind="receipt", data=data, content_type="image/jpeg",
                              request_id=req.id)
    w.s.commit()
    return await proof.prove(w.s, w.pp, w.model, req)


def checks(p) -> dict:
    return {c["key"]: c for c in JsonText.load(p.checks)}


def seeded_receipt(s) -> Attachment:
    """Last week's eggs receipt from the shop's history: the newest seeded receipt, the one the
    reused sample sends again."""
    return s.scalar(select(Attachment).join(SpendRequest).where(
        Attachment.kind == "receipt", SpendRequest.status == "captured").order_by(Attachment.id.desc()))


def jpeg(im: Image.Image, **kw) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "JPEG", **kw)
    return buf.getvalue()


def snapshot(seed: int) -> bytes:
    """A photo of its own: noise from the seed, so no two seeds look alike."""
    rng = random.Random(seed)
    return jpeg(Image.frombytes("RGB", (64, 48), bytes(rng.randrange(256) for _ in range(64 * 48 * 3))))


GOODS_OK = {"seen": True, "count": 40, "quantity_ok": True, "reason": "Forty trays of eggs."}


# Evidence

def test_photos_are_turned_upright_and_hashed(world):
    exif = Image.Exif()
    exif[0x0112] = 6  # the phone was held sideways
    sideways = jpeg(Image.new("RGB", (40, 20), (200, 30, 30)), exif=exif.tobytes())
    a = evidence.store_attachment(world.s, world.shop, kind="goods", data=sideways, content_type="image/png")
    assert a.content_type == "image/jpeg"  # the bytes decide, the client's label does not
    assert Image.open(io.BytesIO(a.data)).size == (20, 40)
    assert len(a.phash) == 16 and int(a.phash, 16) >= 0
    import hashlib
    assert a.sha256 == hashlib.sha256(a.data).hexdigest()
    plain = jpeg(Image.new("RGB", (30, 30), (10, 120, 10)))
    b = evidence.store_attachment(world.s, world.shop, kind="photo", data=plain, content_type="image/jpeg")
    assert b.data == plain  # an upright photo is kept byte for byte


def test_evidence_refuses_what_it_cannot_trust(world):
    with pytest.raises(evidence.EvidenceError):
        evidence.store_attachment(world.s, world.shop, kind="receipt", data=b"<html>hello</html>",
                                  content_type="image/jpeg")
    with pytest.raises(evidence.EvidenceError):
        evidence.store_attachment(world.s, world.shop, kind="receipt", data=b"\xff\xd8\xff" + b"0" * 64,
                                  content_type="image/jpeg")  # looks like a JPEG, opens as nothing
    with pytest.raises(evidence.EvidenceError):
        evidence.store_attachment(world.s, world.shop, kind="goods", data=b"%PDF-1.7 quote",
                                  content_type="application/pdf")
    with pytest.raises(evidence.EvidenceError):
        evidence.store_attachment(world.s, world.shop, kind="receipt", data=b"\xff" * (8 * 1024 * 1024 + 1),
                                  content_type="image/jpeg")
    with pytest.raises(evidence.EvidenceError):
        evidence.store_attachment(world.s, world.shop, kind="loan", data=b"x", content_type="text/plain")
    with pytest.raises(evidence.EvidenceError):
        evidence.store_attachment(world.s, world.shop, kind="receipt", data=b"x", request_id=999999)
    voice = evidence.store_attachment(world.s, world.shop, kind="voice", data=b"\x1a\x45\xdf\xa3" + b"\0" * 40,
                                      content_type="video/webm")
    assert (voice.content_type, voice.phash) == ("audio/webm", "")
    quote = evidence.store_attachment(world.s, world.shop, kind="quote", data=b"%PDF-1.7 quote",
                                      content_type="image/jpeg")
    assert quote.content_type == "application/pdf"


def test_hamming_counts_differing_bits():
    assert evidence.hamming("0000000000000000", "000000000000000f") == 4
    assert evidence.hamming("ffffffffffffffff", "ffffffffffffffff") == 0
    assert evidence.hamming("", "ffffffffffffffff") == 64


# The receipt renderer

def test_receipts_render_the_same_paper_from_any_angle():
    spec = ReceiptSpec(payee="Mbare wholesale", address="Stand 9, Mbare, Harare", receipt_no="MW-000777",
                       at=datetime(2026, 10, 27, 10, 42), lines=(Line("eggs (trays)", 40, 430),))
    one, again, other = render_receipt(spec, seed=1), render_receipt(spec, seed=1), render_receipt(spec, seed=2)
    assert one == again and one != other
    assert Image.open(io.BytesIO(one)).size == receipts.PHOTO
    assert spec.total == 17200 and spec.cash == 18000
    assert reading_of(spec)["items"][0] == {"name": "eggs (trays)", "quantity": 40, "unit_price": 4.3,
                                            "line_total": 172.0}
    # a forwarded copy of one photo stays within a few bits; the same receipt photographed again does not
    forwarded = jpeg(Image.open(io.BytesIO(one)).resize((720, 960)), quality=60)
    assert evidence.hamming(evidence.perceptual_hash(one), evidence.perceptual_hash(forwarded)) <= 4
    assert evidence.hamming(evidence.perceptual_hash(one), evidence.perceptual_hash(other)) > evidence.PHASH_NEAR


# The engine: release

async def test_a_genuine_receipt_releases_the_total_and_returns_the_rest(world):
    req = await held(world)
    before = ledger.summary(world.s, world.shop.id)
    p = await send(world, req, spec_for(req, unit=430))
    c = checks(p)
    assert (p.decision, p.score, p.round, p.spent_cents) == ("release", 88, 1, 17200)
    assert [x["key"] for x in JsonText.load(p.checks)] == ["read", "match", "price", "duplicate", "freshness",
                                                           "photo", "camera"]
    assert c["read"]["passed"] and c["match"]["points"] == 25 and c["price"]["points"] == 15
    assert c["duplicate"]["points"] == 20 and c["freshness"]["points"] == 10
    assert (c["photo"]["points"], c["photo"]["applies"]) == (0, True)
    assert c["camera"]["applies"] is False
    assert set(c["match"]) == {"key", "label", "points", "max", "applies", "passed", "reason", "data"}
    # PayPal captured the receipt total to the cent and returned the rest
    assert req.status == "part_captured" and req.hold.captured_cents == 17200
    assert [x["amount"]["value"] for x in world.pp.captures.values()] == ["172.00"]
    after = ledger.summary(world.s, world.shop.id)
    assert after["funded_cents"] - before["funded_cents"] == 17200
    assert after["returned_cents"] - before["returned_cents"] == 800
    assert p.summary.startswith("Score 88. Mbare wholesale, US$172.00") and "US$8.00 returned" in p.summary
    assert p.elapsed_ms >= 0 and "\u2014" not in p.summary


async def test_photo_and_camera_complete_the_score(world, no_camera):
    req = await held(world)
    world.model.answers[proof.GOODS_TASK] = {"seen": True, "count": 40, "quantity_ok": True,
                                             "reason": "Forty trays of eggs on the counter."}
    no_camera["answer"] = {"seen": True, "frame_ids": [3], "at": "2026-10-27T08:42:00+00:00",
                           "reason": "The counter camera saw crates arrive at 10:42."}
    evidence.store_attachment(world.s, world.shop, kind="goods", data=jpeg(Image.new("RGB", (64, 48), "white")),
                              content_type="image/jpeg", request_id=req.id)
    p = await send(world, req, spec_for(req, unit=430))
    c = checks(p)
    assert (p.score, p.decision) == (100, "release")
    assert c["photo"]["points"] == 10 and c["camera"]["points"] == 20
    assert c["camera"]["data"]["frame_ids"] == [3]
    call = no_camera["calls"][-1]
    assert call["item"] == "eggs" and call["since"] == flows.utc(req.hold.authorized_at)
    assert call["until"] >= call["since"]


async def test_an_expense_needs_no_photo_or_price_history(world):
    req = await held(world, item="fridge door seal", quantity=1, unit="units", amount=2500,
                     payee="Mbare fridge repairs", kind="expense")
    p = await send(world, req, spec_for(req, unit=2400, lines=(Line("Fridge door seal", 1, 2400),)))
    c = checks(p)
    assert c["price"]["applies"] is False and c["photo"]["applies"] is False
    assert (p.score, p.decision, req.hold.captured_cents) == (100, "release", 2400)


# The checks, one by one

async def test_match_names_what_does_not_fit(world):
    req = await held(world)
    r = lambda **kw: Reading(**{"source": "model", "payee": "Mbare wholesale", "receipt_no": "1",
                                "day": today().date(), "currency": "USD", "legible": True,
                                "lines": [proof.Line("EGGS TRAY", 40, 430, 17200)], "total_cents": 17200, **kw})
    assert proof.match_check(req, r()).points == 25
    wrong = proof.match_check(req, r(payee="Mbare bakery"))
    assert wrong.points == 18 and "Mbare bakery" in wrong.reason and wrong.data["failed"] == ["payee"]
    few = proof.match_check(req, r(lines=[proof.Line("EGGS TRAY", 30, 430, 12900)], total_cents=12900))
    assert few.data["failed"] == ["quantity"] and "30 trays" in few.reason
    close = proof.match_check(req, r(lines=[proof.Line("EGGS TRAY", 38, 430, 16340)], total_cents=16340))
    assert close.points == 25  # within ten percent of the quantity
    over = proof.match_check(req, r(lines=[proof.Line("EGGS TRAY", 40, 470, 18800)], total_cents=18800))
    assert over.data["failed"] == ["total"]
    within = proof.match_check(req, r(lines=[proof.Line("EGGS TRAY", 40, 455, 18200)], total_cents=18200))
    assert within.points == 25  # a small tolerance over the hold
    off = proof.match_check(req, r(total_cents=16000))
    assert off.data["failed"] == ["sum"]
    other = proof.match_check(req, r(lines=[proof.Line("BREAD LOAF", 40, 430, 17200)]))
    assert set(other.data["failed"]) == {"item", "quantity"}
    typed = proof.match_check(req, r(source="typed", payee="", lines=[]))
    assert typed.points == 25  # the total is all a typed reading can show
    # other goods on the bill are a contradiction; a carrier bag rides along and stays unpaid
    cigarettes = r(lines=[proof.Line("EGGS TRAY", 40, 400, 16000), proof.Line("CIGARETTES 20S", 1, 2000, 2000)],
                   total_cents=18000)
    extra = proof.match_check(req, cigarettes)
    assert extra.data["failed"] == ["extra"] and extra.hard and "CIGARETTES 20S for US$20.00" in extra.reason
    assert proof.spent_of(req, cigarettes) == 16000
    bags = r(lines=[proof.Line("EGGS TRAY", 40, 430, 17200), proof.Line("CARRIER BAG", 2, 15, 30)], total_cents=17230)
    assert proof.match_check(req, bags).points == 25 and proof.spent_of(req, bags) == 17200
    assert "Other lines come to US$0.30 and are left out." in proof.match_check(req, bags).reason
    # the hold is in US dollars; a receipt in ZWG says nothing about what that is
    zwg = proof.match_check(req, r(currency="ZWG"))
    assert zwg.data["failed"] == ["currency"] and zwg.hard and proof.spent_of(req, r(currency="ZWG")) == 0
    assert proof.match_check(req, r(currency="US$")).points == 25
    # a total the manager typed that the printed one contradicts
    typo = proof.match_check(req, r(typed_cents=12700))
    assert typo.data["failed"] == ["typed"] and typo.hard and "typed a total of US$127.00" in typo.reason
    assert proof.match_check(req, r(typed_cents=17200)).points == 25


def test_names_match_tolerantly():
    assert proof.same_payee("Mbare wholesale", "MBARE WHOLESALERS (PVT) LTD")
    assert proof.same_payee("mbare wholesale", "Mbare Wholesale.")
    assert not proof.same_payee("Mbare wholesale", "Mbare bakery")
    assert not proof.same_payee("Mbare wholesale", "")
    assert proof.names_item("eggs", "EGGS 30S TRAY")
    assert proof.names_item("cooking oil", "COOKING OIL (2 L BOTTLES)")
    assert not proof.names_item("eggs", "BREAD (LOAVES)")
    assert proof.unit_word("2 kg packs") == "2 kg pack" and proof.unit_word("loaves") == "loaf"
    # names that share trade words belong to different shops; a misprint or a run-together name does not
    assert not proof.same_payee("Magaba general dealers", "MBARE GENERAL DEALERS")
    assert not proof.same_payee("Gulf cash and carry", "REMEMBRANCE CASH & CARRY (PVT) LTD")
    assert proof.same_payee("Mbare wholesale", "MBAREWHOLESALE")
    assert proof.same_payee("Mbare wholesale", "MBAR3 WHOLESALE")
    # tills shorten words to their consonants
    assert proof.names_item("cooking oil", "CKNG OIL 2LT") and proof.names_item("salt", "SLT 1KG")
    assert not proof.names_item("bread", "BED SHEET")
    # packaging and charges name no goods
    assert proof.names_item("eggs", "eggs (trays)")
    assert not proof.names_item("eggs", "egg trays (empty)") and not proof.names_item("eggs", "EGG CRATE DEPOSIT")
    assert [proof.currency_code(c) for c in ("US$", "usd", "ZiG", "ZWL", "")] == ["USD", "USD", "ZWG", "ZWG", ""]


def test_receipt_numbers_match_without_a_dropped_prefix():
    assert proof.same_number("No. 8850", "8850") and proof.same_number("MW-041203", "MW 41203")
    assert proof.same_number("INV1003404", "1003404")
    assert not proof.same_number("MW-041203", "GD-041203")  # another till's series
    assert not proof.same_number("MW-41203", "1203") and not proof.same_number("MW-001", "001")


async def test_price_scores_against_the_usual_price(world):
    req = await held(world)
    usual, count = proof.usual_price(world.s, world.shop.id, "eggs", "Mbare wholesale", flows.now())
    assert count >= 6

    def priced(unit):
        r = Reading(source="model", payee="Mbare wholesale", receipt_no="1", day=today().date(),
                    lines=[proof.Line("EGGS", 40, unit, unit * 40)], total_cents=unit * 40, currency="USD",
                    legible=True)
        return proof.price_check(world.s, req, r)

    assert priced(usual).points == 15
    assert priced(round(usual * 1.10)).points == 15
    half = priced(round(usual * 1.15))
    assert half.points == 7.5 and "15% above" in half.reason
    assert priced(round(usual * 1.25)).points == 0
    assert priced(round(usual * 0.8)).points == 15  # paying less is fine


async def test_freshness_is_inside_the_window_after_approval(world):
    req = await held(world)
    p = await send(world, req, spec_for(req, at=today() - timedelta(days=9)))
    c = checks(p)
    assert c["freshness"]["points"] == 0 and "before Chipo approved" in c["freshness"]["reason"]
    assert (p.score, p.decision, req.status) == (75, "ask", "asking")

    late = await held(world)
    late.approved_at = late.hold.authorized_at = flows.now() - timedelta(days=8)
    world.s.commit()
    p = await send(world, late, spec_for(late, no="MW-100002"), seed=2)
    assert "more than 7 days" in checks(p)["freshness"]["reason"]


# Duplicates

async def test_reused_receipt_photo_is_held_for_the_owner(world):
    """Last week's receipt, sent again byte for byte, with the model off: its reading is reused."""
    req = await held(world)
    old = seeded_receipt(world.s)
    p = await send(world, req, data=old.data, read=False)
    c = checks(p)
    assert c["read"]["passed"] and c["duplicate"]["data"]["how"] == "photo"
    assert c["duplicate"]["points"] == 0 and f"request {old.request_id}" in c["duplicate"]["reason"]
    assert c["freshness"]["points"] == 0
    assert p.decision == "hold" and req.status == "flagged" and req.hold.status == "held"
    alert = world.s.scalar(select(Alert).where(Alert.key == f"proof:{req.id}:1"))
    assert alert.kind == "proof" and alert.request_id == req.id and "stays held" in alert.body
    assert {"duplicate", "freshness"} <= set(json.loads(alert.data)["failed"])
    said = world.s.scalars(select(Message.text).where(Message.request_id == req.id)).all()
    assert said[-1].startswith("I sent the receipt for eggs to Chipo to check.")


async def test_duplicate_caught_by_receipt_number(world):
    req = await held(world)
    old = seeded_receipt(world.s)
    printed = JsonText.load(old.reading)
    again = spec_for(req, unit=430, no=printed["receipt_no"].replace("-", " "))  # read with a space
    p = await send(world, req, again, seed=99)
    c = checks(p)
    assert c["duplicate"]["data"]["how"] == "number"
    assert (c["duplicate"]["data"]["attachment_id"], c["duplicate"]["data"]["request_id"]) == (old.id, old.request_id)
    assert c["duplicate"]["points"] == 0


async def test_duplicate_caught_by_perceptual_hash(world):
    req = await held(world)
    old = seeded_receipt(world.s)
    forwarded = jpeg(Image.open(io.BytesIO(old.data)).resize((800, 1066)), quality=55)
    assert evidence.hamming(old.phash, evidence.perceptual_hash(forwarded)) <= evidence.PHASH_NEAR
    p = await send(world, req, spec_for(req, unit=430, no=""), data=forwarded)
    c = checks(p)
    assert c["duplicate"]["data"]["how"] == "copy" and c["duplicate"]["points"] == 0


async def test_a_new_receipt_is_no_duplicate_of_another(world):
    req = await held(world)
    p = await send(world, req, spec_for(req, no="MW-100777"), seed=5)
    assert checks(p)["duplicate"]["points"] == 20


# Bands, the question and the answer

def test_decision_bands():
    assert [proof.band(s) for s in (100, 85, 84, 60, 59, 0)] == ["release", "release", "ask", "ask", "hold", "hold"]


async def test_the_question_comes_from_the_weakest_check(world):
    req = await held(world)
    world.model.answers[proof.QUESTION_TASK] = lambda parts: (
        "Eggs cost more than usual \u2014 why?" if "Price" in parts[0] else None)
    p = await send(world, req, spec_for(req, unit=565, no="MW-200001"))  # 25% over the usual price
    assert p.decision == "ask" and req.status == "asking"
    assert p.question == "Eggs cost more than usual, why?"
    said = world.s.scalars(select(Message.text).where(Message.request_id == req.id)).all()
    assert said[-1] == p.question


async def test_without_the_model_the_question_is_a_template(world):
    req = await held(world)
    p = await send(world, req, spec_for(req, payee="Mbare bakery", no="MB-1"))
    assert p.decision == "ask"
    assert p.question == ("The receipt is from Mbare bakery, and the request named Mbare wholesale. "
                          "What took you to Mbare bakery for the eggs?")


async def test_an_answer_that_explains_the_price_releases(world):
    req = await held(world, amount=23000)
    world.model.answers[proof.GOODS_TASK] = {"seen": True, "count": 40, "quantity_ok": True, "reason": "Forty trays."}
    evidence.store_attachment(world.s, world.shop, kind="goods", data=jpeg(Image.new("RGB", (64, 48), "white")),
                              content_type="image/jpeg", request_id=req.id)
    first = await send(world, req, spec_for(req, unit=565, no="MW-300001"))
    assert (first.score, first.decision) == (81, "ask")
    world.model.answers[proof.ANSWER_TASK] = {"resolves": ["price", "duplicate"],
                                              "reason": "The wholesaler raised egg prices this week."}
    second = await proof.answer(world.s, world.pp, world.model, req, "Mazai akakwira mutengo svondo rino.")
    c = checks(second)
    assert c["price"]["points"] == 7.5 and c["price"]["data"]["eased"]
    assert c["duplicate"]["points"] == 20  # untouched: it was never in doubt
    assert (second.round, second.score, second.decision) == (2, 91, "release")
    assert first.answer == second.answer == "Mazai akakwira mutengo svondo rino."
    assert req.hold.captured_cents == 22600 and req.status == "part_captured"


async def test_an_answer_that_explains_nothing_goes_to_the_owner(world):
    req = await held(world)
    p = await send(world, req, spec_for(req, at=today() - timedelta(days=9), no="MW-400001"))
    assert p.decision == "ask"
    world.model.answers[proof.ANSWER_TASK] = {"resolves": [], "reason": "No reason given."}
    second = await proof.answer(world.s, world.pp, world.model, req, "Ndizvo")
    assert (second.round, second.decision, req.status) == (2, "hold", "flagged")
    assert world.s.scalar(select(Alert).where(Alert.key == f"proof:{req.id}:2"))
    with pytest.raises(flows.FlowError):
        await proof.answer(world.s, world.pp, world.model, req, "again")


async def test_words_never_clear_a_duplicate(world, no_camera):
    req = await held(world)
    old = seeded_receipt(world.s)
    printed = JsonText.load(old.reading)
    world.model.answers[proof.GOODS_TASK] = {"seen": True, "count": 40, "quantity_ok": True, "reason": "Trays."}
    evidence.store_attachment(world.s, world.shop, kind="goods", data=jpeg(Image.new("RGB", (64, 48), "white")),
                              content_type="image/jpeg", request_id=req.id)
    no_camera["answer"] = {"seen": True, "frame_ids": [], "at": None, "reason": "Crates arrived."}
    p = await send(world, req, spec_for(req, unit=430, no=printed["receipt_no"]), seed=7)
    assert p.decision == "ask" and checks(p)["duplicate"]["points"] == 0
    world.model.answers[proof.ANSWER_TASK] = {"resolves": ["duplicate", "price", "freshness", "photo"],
                                              "reason": "Farai says it is a new receipt."}
    second = await proof.answer(world.s, world.pp, world.model, req, "It is a new receipt, I promise.")
    assert checks(second)["duplicate"]["points"] == 0
    assert second.decision == "hold" and req.hold.status == "held"


# The read gate

async def test_an_unreadable_receipt_asks_for_a_sharper_photo(world):
    req = await held(world)
    world.model.answers[proof.READ_TASK] = {"payee": "", "receipt_no": "", "date": "", "items": [], "total": 0,
                                            "currency": "", "legible": False}
    spec = spec_for(req, no="MW-500001")
    p = await send(world, req, spec, read=False)
    assert (p.score, p.decision, req.status) == (0, "ask", "asking")
    assert p.question.startswith("This receipt is hard to read.")
    again = await send(world, req, spec, seed=8, read=False)
    assert (again.decision, req.status) == ("hold", "flagged")


async def test_typed_values_wait_for_the_owner(world):
    """With the model off, the manager's typed values stand in for the reading. They score, and
    the owner confirms them with one tap: Musha releases nothing on the manager's word alone."""
    world.model.enabled = False
    req = await held(world)
    data = render_receipt(spec_for(req, no="MW-600001"), seed=4)
    r = world.client.post(f"/api/requests/{req.id}/evidence", files={"file": ("r.jpg", data, "image/jpeg")},
                          data={"kind": "receipt", "total": "172.00", "receipt_no": "MW-600001",
                                "date": today().date().isoformat()})
    assert r.status_code == 201
    body = r.json()
    assert body["attachment"]["reading"]["source"] == "typed"
    assert (body["proof"]["decision"], body["proof"]["score"], body["proof"]["spent_cents"]) == ("hold", 88, 17200)
    assert (body["request"]["status"], body["request"]["hold"]["captured_cents"]) == ("flagged", 0)
    assert body["message"]["attachment_id"] == body["attachment"]["id"]
    assert body["message"]["sender"] == "manager"
    alert = world.s.scalar(select(Alert).where(Alert.key == f"proof:{req.id}:1"))
    assert "Farai typed the receipt values while the receipt reader was off." in alert.body
    assert json.loads(alert.data)["typed"] is True
    tap = world.client.post(f"/api/requests/{req.id}/decide", json={"action": "release"}).json()
    assert tap["request"]["hold"]["captured_cents"] == 17200


async def test_a_busy_reader_asks_for_the_photo_again(world):
    """The model is on and gives no answer: the typed values wait, and so does the money."""
    req = await held(world)
    spec = spec_for(req, no="MW-610001")
    data = render_receipt(spec, seed=4)
    typed = {"kind": "receipt", "total": "172.00", "receipt_no": "MW-610001", "date": today().date().isoformat()}
    first = world.client.post(f"/api/requests/{req.id}/evidence", files={"file": ("r.jpg", data, "image/jpeg")},
                              data=typed).json()
    assert (first["proof"]["decision"], first["request"]["status"]) == ("ask", "asking")
    assert first["proof"]["question"] == "Musha's receipt reader is busy. Send the same receipt photo again in a minute."
    assert first["proof"]["summary"].endswith("Musha asked Farai to send it again.")
    assert first["request"]["hold"]["captured_cents"] == 0
    # the same photo again, and the model reads it: the typed total agrees, and the money goes.
    # The note that the reader was busy never counted as the request's one question.
    world.model.answers[proof.READ_TASK] = reading_of(spec)
    again = world.client.post(f"/api/requests/{req.id}/evidence", files={"file": ("r.jpg", data, "image/jpeg")},
                              data=typed).json()
    assert (again["proof"]["round"], again["proof"]["decision"]) == (1, "release")
    assert again["request"]["hold"]["captured_cents"] == 17200


async def test_a_typed_total_the_receipt_contradicts_is_asked_about(world):
    req = await held(world)
    spec = spec_for(req, no="MW-620001")
    world.model.answers[proof.READ_TASK] = reading_of(spec)
    r = world.client.post(f"/api/requests/{req.id}/evidence",
                          files={"file": ("r.jpg", render_receipt(spec, seed=6), "image/jpeg")},
                          data={"kind": "receipt", "total": "127.00"}).json()
    match = {c["key"]: c for c in r["proof"]["checks"]}["match"]
    assert r["proof"]["decision"] == "ask" and match["data"]["failed"] == ["typed"] and match["data"]["hard"]
    assert r["proof"]["question"] == "You typed US$127.00, and the receipt reads US$172.00. Which total is right?"


# The owner decides

async def flagged(w) -> SpendRequest:
    req = await held(w)
    old = seeded_receipt(w.s)
    p = await send(w, req, data=old.data, read=False)
    assert req.status == "flagged" and p.decision == "hold"
    return req


async def test_owner_releases_an_amount(world):
    req = await flagged(world)
    r = world.client.post(f"/api/requests/{req.id}/decide", json={"action": "release", "amount": "150.25"})
    assert r.status_code == 200
    body = r.json()
    assert body["proof"]["decision"] == "release"
    assert body["proof"]["summary"] == "Chipo released US$150.25 for eggs. US$29.75 went back to Chipo."
    assert (body["request"]["hold"]["captured_cents"], body["request"]["status"]) == (15025, "part_captured")
    world.s.expire_all()
    assert all(a.dismissed for a in world.s.scalars(select(Alert).where(Alert.request_id == req.id)))
    assert world.client.post(f"/api/requests/{req.id}/decide", json={"action": "return"}).status_code == 409


async def test_owner_returns_everything(world):
    req = await flagged(world)
    r = world.client.post(f"/api/requests/{req.id}/decide", json={"action": "return"})
    body = r.json()
    assert body["proof"]["decision"] == "return" and body["request"]["status"] == "voided"
    assert body["request"]["hold"]["captured_cents"] == 0
    assert world.client.post(f"/api/requests/{req.id}/decide", json={"action": "keep"}).status_code == 422


# Routes

async def test_samples_follow_the_request(world):
    req = await held(world)
    c = world.client
    genuine = c.post(f"/api/requests/{req.id}/samples", json={"variant": "genuine"})
    assert genuine.headers["content-type"] == "image/jpeg"
    total = int(genuine.headers["x-receipt-total"].replace(".", ""))
    assert 0 < total < req.hold.amount_cents and genuine.headers["x-receipt-payee"] == req.payee
    reused = c.post(f"/api/requests/{req.id}/samples", json={"variant": "reused"})
    assert reused.content == seeded_receipt(world.s).data
    inflated = c.post(f"/api/requests/{req.id}/samples", json={"variant": "inflated"})
    assert int(inflated.headers["x-receipt-total"].replace(".", "")) > req.hold.amount_cents
    wrong = c.post(f"/api/requests/{req.id}/samples", json={"variant": "wrong_payee"})
    assert wrong.headers["x-receipt-payee"] and not proof.same_payee(req.payee, wrong.headers["x-receipt-payee"])
    stale = c.post(f"/api/requests/{req.id}/samples", json={"variant": "stale"})
    assert stale.headers["x-receipt-date"] < proof.shop_day(req.approved_at).isoformat()
    assert c.post(f"/api/requests/{req.id}/samples", json={"variant": "forged"}).status_code == 422


async def test_a_sample_sent_back_runs_the_engine(world):
    req = await held(world)
    c = world.client
    img = c.post(f"/api/requests/{req.id}/samples", json={"variant": "genuine"})
    h = img.headers
    world.model.answers[proof.READ_TASK] = reading_of(receipts.sample_spec(world.s, req, "genuine"))
    r = c.post(f"/api/requests/{req.id}/evidence", files={"file": ("receipt.jpg", img.content, "image/jpeg")},
               data={"kind": "receipt", "total": h["x-receipt-total"], "receipt_no": h["x-receipt-no"],
                     "date": h["x-receipt-date"], "payee": h["x-receipt-payee"]})
    assert r.json()["proof"]["decision"] == "release"
    att = c.get(r.json()["attachment"]["url"])
    assert att.headers["content-type"] == "image/jpeg" and att.content == img.content
    assert c.get("/api/attachments/999999").status_code == 404
    # after a fresh receipt, `reused` is still last week's, byte for byte
    later = await held(world)
    assert c.post(f"/api/requests/{later.id}/samples", json={"variant": "reused"}).content \
        == seeded_receipt(world.s).data


async def test_a_goods_photo_answers_a_question_about_the_photo(world):
    req = await held(world, amount=21000)
    world.model.answers[proof.GOODS_TASK] = {"seen": False, "count": 0, "quantity_ok": False,
                                             "reason": "The photo shows an empty counter."}
    evidence.store_attachment(world.s, world.shop, kind="goods", data=jpeg(Image.new("RGB", (64, 48), "grey")),
                              content_type="image/jpeg", request_id=req.id)
    p = await send(world, req, spec_for(req, unit=518, no="MW-700001"))  # 15% over the usual price
    c = checks(p)
    assert (p.decision, c["price"]["points"], c["photo"]["points"]) == ("ask", 7.5, 0)
    assert proof.asked_about(p) == "photo" and c["photo"]["data"]["asked"]
    assert p.question == "Please send a photo of the 40 trays of eggs."
    world.model.answers[proof.GOODS_TASK] = {"seen": True, "count": 40, "quantity_ok": True, "reason": "Trays."}
    r = world.client.post(f"/api/requests/{req.id}/evidence", data={"kind": "goods"},
                          files={"file": ("eggs.jpg", jpeg(Image.new("RGB", (64, 48), "white")), "image/jpeg")})
    body = r.json()
    assert (body["proof"]["round"], body["proof"]["decision"], body["proof"]["score"]) == (2, "release", 91)
    assert body["request"]["hold"]["captured_cents"] == 20720


async def test_a_contradiction_never_releases_on_its_score(world, no_camera):
    """Photo and camera vouch for the goods, so the score reaches 85; the receipt is from another payee."""
    req = await held(world)
    world.model.answers[proof.GOODS_TASK] = {"seen": True, "count": 40, "quantity_ok": True, "reason": "Trays."}
    evidence.store_attachment(world.s, world.shop, kind="goods", data=jpeg(Image.new("RGB", (64, 48), "white")),
                              content_type="image/jpeg", request_id=req.id)
    no_camera["answer"] = {"seen": True, "frame_ids": [5], "at": None, "reason": "Crates arrived."}
    p = await send(world, req, spec_for(req, payee="Mbare bakery", no="MB-3"))
    c = checks(p)
    assert p.score >= proof.RELEASE_AT and p.decision == "ask" and req.hold.status == "held"
    assert c["match"]["data"]["hard"] and proof.asked_about(p) == "match"
    # an answer cannot settle a different payee: the owner decides
    world.model.answers[proof.ANSWER_TASK] = {"resolves": [], "reason": "The usual supplier was closed."}
    second = await proof.answer(world.s, world.pp, world.model, req, "Mbare wholesale yakanga yakavhara.")
    assert second.score >= proof.RELEASE_AT and second.decision == "hold" and req.status == "flagged"


async def test_a_stale_receipt_asks_even_with_a_high_score(world, no_camera):
    req = await held(world, item="fridge door seal", quantity=1, unit="units", amount=2500,
                     payee="Mbare fridge repairs", kind="expense")
    no_camera["answer"] = {"seen": True, "frame_ids": [6], "at": None, "reason": "The repairman came in."}
    p = await send(world, req, spec_for(req, unit=2400, at=today() - timedelta(days=12),
                                        lines=(Line("Fridge door seal", 1, 2400),)))
    c = checks(p)
    assert (p.score, p.decision) == (87, "ask") and c["freshness"]["data"]["hard"]
    assert proof.asked_about(p) == "freshness"
    # a purchase date that fits eases it, and the money goes
    world.model.answers[proof.ANSWER_TASK] = {"resolves": ["freshness"], "reason": "Farai paid a deposit then."}
    second = await proof.answer(world.s, world.pp, world.model, req, "I paid a deposit before you approved.")
    assert checks(second)["freshness"]["data"]["eased"] and second.decision == "release"
    assert req.hold.captured_cents == 2400


def test_a_contradiction_outranks_a_bigger_softer_loss():
    soft = proof.Check("price", points=7.5)
    hard = proof.Check("match", points=18, hard=True, data={"failed": ["payee"]})
    later = proof.Check("photo", points=0, absent=True)
    assert proof.weakest([proof.Check("read", passed=True), soft, hard, later]) is hard
    assert proof.contradicted([soft, hard, later]) == [hard]


async def test_a_missing_photo_never_outranks_evidence_against(world, no_camera):
    req = await held(world)
    no_camera["answer"] = {"seen": False, "frame_ids": [4], "at": None, "reason": "No crates seen."}
    p = await send(world, req, spec_for(req, payee="Mbare bakery", no="MB-2"))
    assert proof.asked_about(p) == "match"


def test_answer_route_needs_a_question(world):
    assert world.client.post("/api/requests/1/answer", json={"text": "hello"}).status_code == 409


def test_goods_photos_are_served_for_the_shop_products(world):
    for product in ("eggs", "bread", "cooking oil", "sugar", "fridge door seal"):
        r = world.client.get(f"/api/samples/goods/{product}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        assert len(r.content) < 150_000
        assert Image.open(io.BytesIO(r.content)).width == 768
    assert world.client.get("/api/samples/goods/elephants").status_code == 404


# The seeded history

def test_the_seed_keeps_last_weeks_receipt(world):
    s = world.s
    receipt = seeded_receipt(s)
    req = receipt.request
    assert req and req.status == "captured" and req.item == "eggs"
    assert req.hold.status == "captured" and req.hold.captured_cents == req.amount_cents
    printed = JsonText.load(receipt.reading)
    assert printed["source"] == "seed" and printed["payee"] == req.payee
    assert proof._cents(printed["total"]) == req.amount_cents
    assert len(receipt.phash) == 16 and receipt.request_id == req.id
    refs = s.scalars(select(LedgerEntry.ref).where(LedgerEntry.request_id == req.id)).all()
    assert refs and all(r.startswith("seed-") for r in refs)
    kinds = set(s.scalars(select(LedgerEntry.kind).where(LedgerEntry.request_id == req.id)))
    assert {ledger.FUNDS_HELD, ledger.FUNDS_CAPTURED, ledger.STOCK_IN} <= kinds
    assert req.proofs and req.proofs[0].decision == "release"
    assert proof.shop_day(flows.now()) - date_of(printed) >= timedelta(days=6)


def date_of(printed):
    return datetime.fromisoformat(printed["date"]).date()


# Money over the wire

def test_capture_carries_the_eggs_lines_to_the_cent(app, client, sandbox, model, no_camera):
    """The receipt adds an empty tray for 20 cents: a small charge that stays out of the capture."""
    with app.state.sessions() as s:
        shop = s.get(Shop, 1)
        req = flows.create_request(s, shop, item="eggs", quantity=40, unit="trays", amount_cents=18000,
                                   payee="Mbare wholesale")
        rid = req.id
    client.post(f"/api/requests/{rid}/order")
    assert client.post(f"/api/requests/{rid}/authorize").json()["status"] == "held"
    spec = ReceiptSpec(payee="Mbare wholesale", address="Stand 9, Mbare, Harare", receipt_no="MW-800001",
                       at=today(), lines=(Line("eggs (trays)", 40, 427), Line("egg trays (empty)", 1, 20)))
    model.answers[proof.READ_TASK] = reading_of(spec)
    r = client.post(f"/api/requests/{rid}/evidence",
                    files={"file": ("r.jpg", render_receipt(spec, seed=11), "image/jpeg")}, data={"kind": "receipt"})
    assert r.json()["proof"]["decision"] == "release" and r.json()["proof"]["spent_cents"] == 17080
    match = {c["key"]: c for c in r.json()["proof"]["checks"]}["match"]
    assert "Other lines come to US$0.20 and are left out." in match["reason"]
    capture = [c for c in sandbox.calls if c.request.url.path.endswith("/capture")][-1]
    assert json.loads(capture.request.content)["amount"] == {"currency_code": "USD", "value": "170.80"}
    assert r.json()["request"]["hold"]["captured_cents"] == 17080


# Guards: what the hold pays for, and who decides

async def test_other_goods_on_the_bill_are_asked_about_and_never_paid(world):
    """US$180 held for eggs. The receipt adds cigarettes to make US$180: the hold pays for the eggs."""
    req = await held(world)
    spec = spec_for(req, no="MW-110001", lines=(Line("eggs (trays)", 40, 400), Line("CIGARETTES 20S", 1, 2000)))
    assert spec.total == 18000
    p = await send(world, req, spec)
    c = checks(p)
    assert (p.decision, p.spent_cents, req.hold.status) == ("ask", 16000, "held")
    assert c["match"]["data"]["failed"] == ["extra"] and c["match"]["data"]["hard"]
    assert proof.asked_about(p) == "match"
    assert p.question == "The receipt also shows CIGARETTES 20S for US$20.00. What were they for?"
    # no answer makes the cigarettes the shop's: the owner decides, and one tap releases the eggs
    second = await proof.answer(world.s, world.pp, world.model, req, "Ndezve mutengi.")
    assert (second.round, second.decision, req.status, second.spent_cents) == (2, "hold", "flagged", 16000)
    tap = world.client.post(f"/api/requests/{req.id}/decide", json={"action": "release"}).json()
    assert tap["request"]["hold"]["captured_cents"] == 16000


async def test_a_camera_yet_to_see_the_goods_never_blocks_a_release(world, no_camera):
    """The usual order: pay at the market, send the receipt, carry the goods to the shop."""
    req = await held(world)
    world.model.answers[proof.GOODS_TASK] = GOODS_OK
    evidence.store_attachment(world.s, world.shop, kind="goods", data=snapshot(1), content_type="image/jpeg",
                              request_id=req.id)
    no_camera["answer"] = {"seen": False, "frame_ids": [7], "at": None,
                           "reason": "The counter camera watched from 08:00 to 08:30 UTC and saw no eggs arrive."}
    p = await send(world, req, spec_for(req, no="MW-120001"))
    c = checks(p)["camera"]
    assert (p.decision, p.score, req.hold.captured_cents) == ("release", 100, 17200)
    assert c["applies"] is False and c["data"]["waiting"] and c["reason"].endswith("sees the delivery.")


async def test_the_answer_round_sees_goods_that_arrive_after_the_receipt(world, monkeypatch):
    req = await held(world)
    world.model.answers[proof.GOODS_TASK] = GOODS_OK
    evidence.store_attachment(world.s, world.shop, kind="goods", data=snapshot(2), content_type="image/jpeg",
                              request_id=req.id)
    arrived, asked_until = {}, []

    async def delivery_check(session, model, shop, *, item, since, until, **_):
        asked_until.append(until)
        if arrived and until >= arrived["at"]:
            return {"seen": True, "frame_ids": [9], "at": arrived["at"].isoformat(),
                    "reason": "The counter camera saw egg trays arrive."}
        return {"seen": False, "frame_ids": [8], "at": None, "reason": "The counter camera saw no eggs arrive."}

    monkeypatch.setattr(camera, "delivery_check", delivery_check)
    first = await send(world, req, spec_for(req, at=today() - timedelta(days=9), no="MW-130001"))
    assert first.decision == "ask" and proof.asked_about(first) == "freshness"
    arrived["at"] = flows.now()  # the trays reach the counter after the receipt went in
    world.model.answers[proof.ANSWER_TASK] = {"resolves": ["freshness"], "reason": "Farai paid a deposit then."}
    second = await proof.answer(world.s, world.pp, world.model, req, "I paid a deposit before you approved.")
    c = checks(second)
    assert c["camera"]["points"] == 20 and asked_until[-1] >= arrived["at"]
    assert (second.round, second.score, second.decision) == (2, 95, "release")


def test_the_question_skips_the_camera_while_anything_else_is_open():
    read = proof.Check("read", passed=True)
    photo = proof.Check("photo", points=0, absent=True)
    lens = proof.Check("camera", points=0, absent=True)
    assert proof.weakest([read, photo, lens]) is photo
    assert proof.weakest([read, proof.Check("photo", points=10), lens]) is lens


async def test_a_flagged_request_waits_for_the_owner(world):
    req = await flagged(world)
    alert = world.s.scalar(select(Alert).where(Alert.key == f"proof:{req.id}:1"))
    spec = spec_for(req, no="MW-140001")
    world.model.answers[proof.READ_TASK] = reading_of(spec)
    body = world.client.post(f"/api/requests/{req.id}/evidence", data={"kind": "receipt"},
                             files={"file": ("r.jpg", render_receipt(spec, seed=14), "image/jpeg")}).json()
    assert body["proof"] is None and body["request"]["status"] == "flagged"
    assert body["request"]["hold"]["captured_cents"] == 0
    said = world.s.scalars(select(Message.text).where(Message.request_id == req.id).order_by(Message.id)).all()
    assert said[-1] == "Chipo is checking the eggs receipt. I added this one for Chipo to see."
    # the owner runs the engine again from the console; the release closes the alert
    again = world.client.post(f"/api/requests/{req.id}/prove").json()
    assert again["proof"]["decision"] == "release" and again["request"]["hold"]["captured_cents"] == 17200
    world.s.expire_all()
    assert world.s.get(Alert, alert.id).dismissed


async def test_a_receipt_in_another_currency_never_releases(world):
    req = await held(world)
    p = await send(world, req, replace(spec_for(req, no="MW-150001"), currency="ZWG"))  # TOTAL ZWG 172.00
    c = checks(p)
    assert p.decision == "ask" and c["match"]["data"]["failed"] == ["currency"] and c["match"]["data"]["hard"]
    assert p.spent_cents == 0 and "The receipt is in ZWG; the hold is in USD." in c["match"]["reason"]
    assert c["price"]["points"] == 0 and "ZWG" in c["price"]["reason"]
    assert p.question == "The receipt is in ZWG. What did the eggs cost in USD?"
    second = await proof.answer(world.s, world.pp, world.model, req, "Ndakabhadhara neZiG.")
    assert (second.decision, req.status, req.hold.captured_cents) == ("hold", "flagged", 0)


async def test_one_question_per_request(world):
    req = await held(world, amount=23000)
    first = await send(world, req, spec_for(req, unit=565, no="MW-160001"))  # far over the usual price
    assert (first.round, first.decision) == (1, "ask")
    again = await send(world, req, spec_for(req, unit=560, no="MW-160002"), seed=2)  # a new receipt, still dear
    assert (again.round, again.decision, req.status) == (2, "hold", "flagged")
    assert [p.decision for p in req.proofs] == ["ask", "hold"]
    assert world.s.scalar(select(Alert).where(Alert.key == f"proof:{req.id}:2"))
    # a sharper receipt after the question releases in the same second round
    other = await held(world)
    world.model.answers[proof.READ_TASK] = {"payee": "", "receipt_no": "", "date": "", "items": [], "total": 0,
                                            "currency": "", "legible": False}
    blurred = await send(world, other, spec_for(other, no="MW-160003"), seed=3, read=False)
    assert (blurred.round, blurred.decision) == (1, "ask")
    sharp = await send(world, other, spec_for(other, no="MW-160003"), seed=4)
    assert (sharp.round, sharp.decision, other.hold.captured_cents) == (2, "release", 17200)


async def test_a_later_copy_never_makes_the_original_a_duplicate(world):
    first_req = await held(world, amount=23000)
    world.model.answers[proof.GOODS_TASK] = GOODS_OK
    evidence.store_attachment(world.s, world.shop, kind="goods", data=snapshot(3), content_type="image/jpeg",
                              request_id=first_req.id)
    first = await send(world, first_req, spec_for(first_req, unit=565, no="MW-170001"))
    assert (first.score, first.decision) == (81, "ask")
    copier = await held(world, amount=23000)
    original = world.s.get(Attachment, first.receipt_id)
    copy = await send(world, copier, data=original.data, read=False)
    assert checks(copy)["duplicate"]["data"]["request_id"] == first_req.id and copy.decision == "hold"
    world.model.answers[proof.ANSWER_TASK] = {"resolves": ["price"], "reason": "Egg prices rose this week."}
    second = await proof.answer(world.s, world.pp, world.model, first_req, "Mazai akakwira mutengo.")
    assert checks(second)["duplicate"]["points"] == 20
    assert (second.score, second.decision) == (91, "release")


def seeded_goods(s) -> Attachment:
    return s.scalar(select(Attachment).join(SpendRequest).where(
        Attachment.kind == "goods", SpendRequest.status == "captured").order_by(Attachment.id))


async def test_a_goods_photo_sent_before_scores_nothing(world):
    """Last week's goods photo, forwarded again: the model would pass it; the reuse check stops it first."""
    req = await held(world)
    old = seeded_goods(world.s)
    forwarded = jpeg(Image.open(io.BytesIO(old.data)).resize((700, 525)), quality=60)
    world.model.answers[proof.GOODS_TASK] = GOODS_OK
    evidence.store_attachment(world.s, world.shop, kind="goods", data=forwarded, content_type="image/jpeg",
                              request_id=req.id)
    p = await send(world, req, spec_for(req, no="MW-180001"))
    c = checks(p)["photo"]
    assert (c["points"], c["data"]["how"], c["data"]["request_id"]) == (0, "copy", old.request_id)
    assert f"was sent for request {old.request_id}" in c["reason"] and c["data"]["hard"]
    assert p.decision == "ask" and proof.asked_about(p) == "photo"
    assert p.question == (f"That photo was sent before, for request {old.request_id}. "
                          "Please send a new photo of the 40 trays of eggs.")
    # words never clear a reused photo
    world.model.answers[proof.ANSWER_TASK] = {"resolves": ["photo"], "reason": "Farai says it is today's photo."}
    second = await proof.answer(world.s, world.pp, world.model, req, "Ifoto yanhasi.")
    assert checks(second)["photo"]["points"] == 0 and second.decision == "hold"


async def test_the_demo_goods_photo_is_new_to_the_shop(world):
    req = await held(world)
    world.model.answers[proof.GOODS_TASK] = GOODS_OK
    served = world.client.get("/api/samples/goods/eggs").content
    assert not evidence.same_photo(SimpleNamespace(sha256="", phash=evidence.perceptual_hash(served), data=served),
                                   seeded_goods(world.s))
    world.client.post(f"/api/requests/{req.id}/evidence", data={"kind": "goods"},
                      files={"file": ("eggs.jpg", served, "image/jpeg")})
    world.s.refresh(req)
    p = await send(world, req, spec_for(req, no="MW-190001"))
    assert checks(p)["photo"]["points"] == 10 and p.decision == "release"


async def test_the_owner_cannot_decide_a_capture_paypal_is_settling(world):
    req = await held(world)
    req.status = "capturing"  # PayPal answered PENDING; the hold is still held
    world.s.commit()
    r = world.client.post(f"/api/requests/{req.id}/decide", json={"action": "release", "amount": "100.00"})
    assert r.status_code == 409 and world.pp.captures == {}
    with pytest.raises(flows.FlowError):
        await proof.decide(world.s, world.pp, req, action="return")


# One question, the owner's hold, and the database while the model thinks

async def test_a_busy_reader_leaves_the_one_question_for_the_evidence(world):
    """The reader is busy on the first read. The resend shows a price far above the usual, and the
    manager hears the price question: the busy note was never the request's one question."""
    req = await held(world)
    spec = spec_for(req, unit=580, no="MW-620001")  # 29% above the usual US$4.50
    data = render_receipt(spec, seed=6)
    first = world.client.post(f"/api/requests/{req.id}/evidence", files={"file": ("r.jpg", data, "image/jpeg")},
                              data={"kind": "receipt"}).json()
    assert first["proof"]["decision"] == "ask"
    world.s.expire_all()
    assert proof.busy_note(world.s.get(SpendRequest, req.id).proofs[-1])
    world.model.answers[proof.READ_TASK] = reading_of(spec)
    again = world.client.post(f"/api/requests/{req.id}/evidence", files={"file": ("r.jpg", data, "image/jpeg")},
                              data={"kind": "receipt"}).json()
    assert (again["proof"]["round"], again["proof"]["decision"], again["request"]["status"]) == (1, "ask", "asking")
    assert again["proof"]["question"].startswith("Eggs cost US$5.80 a tray, above the usual US$4.50.")


async def test_a_reader_busy_twice_more_goes_to_the_owner(world):
    req = await held(world)
    data = render_receipt(spec_for(req, no="MW-630001"), seed=7)
    decisions = [world.client.post(f"/api/requests/{req.id}/evidence", data={"kind": "receipt"},
                                   files={"file": ("r.jpg", data, "image/jpeg")}).json()["proof"]["decision"]
                 for _ in range(3)]
    assert decisions == ["ask", "ask", "hold"]


async def test_rerunning_a_flagged_request_never_asks_the_manager(world):
    """Chipo holds a flagged request. Scored again, it is released or kept for her; the manager hears nothing new."""
    req = await flagged(world)
    spec = spec_for(req, payee="Magaba traders", no="MT-300001")  # a second receipt, from another seller
    world.model.answers[proof.READ_TASK] = reading_of(spec)
    world.client.post(f"/api/requests/{req.id}/evidence", data={"kind": "receipt"},
                      files={"file": ("r.jpg", render_receipt(spec, seed=8), "image/jpeg")})
    told = world.s.scalar(select(func.count(Message.id)).where(Message.request_id == req.id))
    body = world.client.post(f"/api/requests/{req.id}/prove").json()
    assert (body["proof"]["decision"], body["request"]["status"]) == ("hold", "flagged")
    assert body["proof"]["question"] == ""
    world.s.expire_all()
    assert world.s.scalar(select(func.count(Message.id)).where(Message.request_id == req.id)) == told
    alert = world.s.scalar(select(Alert).where(Alert.key == f"proof:{req.id}:1"))
    assert "Magaba traders" in alert.body  # the owner's alert shows the newest score


async def test_the_model_never_waits_on_a_write_lock(world, settings):
    """Each model call finds the database free: another connection takes the write lock at once.
    SQLite locks the whole file, so a scoring holding it would stall every shop on the server."""
    path = world.app.state.engine.url.database
    free = {}

    def lock_is_free(task, answer):
        def call(parts):
            con = sqlite3.connect(path, timeout=0)
            try:
                con.execute("BEGIN IMMEDIATE")
                con.rollback()
                free.setdefault(task, []).append(True)
            except sqlite3.OperationalError:
                free.setdefault(task, []).append(False)
            finally:
                con.close()
            return answer
        return call

    req = await held(world, amount=23000)
    spec = spec_for(req, unit=565, no="MW-640001")  # dear enough for a question
    world.model.answers[proof.READ_TASK] = lock_is_free("read", reading_of(spec))
    world.model.answers[proof.GOODS_TASK] = lock_is_free("goods", GOODS_OK)
    world.model.answers[proof.QUESTION_TASK] = lock_is_free("question", None)
    world.model.answers[proof.ANSWER_TASK] = lock_is_free("answer", {"resolves": ["price"], "reason": "Dearer eggs."})
    evidence.store_attachment(world.s, world.shop, kind="goods", data=snapshot(64), content_type="image/jpeg",
                              request_id=req.id)
    first = await send(world, req, spec, read=False)
    assert first.decision == "ask"
    second = await proof.answer(world.s, world.pp, world.model, req, "Mazai akakwira.")
    assert second.decision == "release"
    assert free == {"read": [True], "goods": [True], "question": [True], "answer": [True]}


async def test_a_reader_past_its_deadline_counts_as_busy(world, monkeypatch):
    class Slow:
        enabled, model, calls = True, "slow", []

        async def json(self, task, parts, schema, *, system=""):
            await asyncio.sleep(5)

        async def text(self, task, parts, *, system=""):
            await asyncio.sleep(5)

    monkeypatch.setattr(proof, "RECEIPT_SECONDS", 0.05)
    monkeypatch.setattr(proof, "PHOTO_SECONDS", 0.05)
    req = await held(world)
    world.model = Slow()
    started = asyncio.get_running_loop().time()
    p = await send(world, req, spec_for(req, no="MW-650001"), read=False)
    assert asyncio.get_running_loop().time() - started < 2
    assert p.decision == "ask" and p.question.startswith("Musha's receipt reader is busy.")


async def test_a_slow_question_goes_out_plain(world, monkeypatch):
    async def slow(task, parts, *, system=""):
        await asyncio.sleep(5)
        return "A polished question?"

    monkeypatch.setattr(proof, "QUESTION_SECONDS", 0.05)
    monkeypatch.setattr(world.model, "text", slow)
    req = await held(world)
    p = await send(world, req, spec_for(req, unit=565, no="MW-660001"))
    assert p.decision == "ask" and p.question.startswith("Eggs cost US$5.65 a tray, above the usual")


async def test_a_release_paypal_stops_goes_to_the_owner(world, monkeypatch):
    async def declined(*a, **kw):
        return {"id": "CAP-D", "status": "DECLINED", "amount": {"currency_code": "USD", "value": "172.00"}}

    req = await held(world)
    monkeypatch.setattr(world.pp, "capture_authorization", declined)
    p = await send(world, req, spec_for(req, no="MW-670001"))
    assert (p.decision, req.status, req.hold.status, req.hold.capture_attempts) == ("hold", "flagged", "held", 1)
    assert p.summary.startswith("Score ") and "PayPal stopped the release of US$172.00." in p.summary
    owner = world.s.scalars(select(Message.text).where(Message.thread == "owner").order_by(Message.id)).all()
    assert owner[-1] == "PayPal declined the US$172.00 capture for eggs. US$180.00 stays held. Release it again or return it."
    assert world.s.scalar(select(Alert).where(Alert.key == f"proof:{req.id}:1"))


async def test_the_total_over_the_hold_reads_plainly(world):
    req = await held(world)
    p = await send(world, req, spec_for(req, unit=460, no="MW-680001"))  # 40 at US$4.60: US$184.00
    assert "The receipt shows US$184.00 for eggs, above the US$180.00 held." in checks(p)["match"]["reason"]


async def test_a_goods_photo_is_read_as_it_arrives(world):
    req = await held(world)
    world.model.answers[proof.GOODS_TASK] = GOODS_OK
    world.client.post(f"/api/requests/{req.id}/evidence", data={"kind": "goods"},
                      files={"file": ("eggs.jpg", snapshot(65), "image/jpeg")})
    goods = [c for c in world.model.calls if c[0] == proof.GOODS_TASK]
    assert len(goods) == 1  # read on arrival
    world.s.expire_all()
    req = world.s.get(SpendRequest, req.id)
    p = await send(world, req, spec_for(req, no="MW-690001"))
    assert checks(p)["photo"]["points"] == 10 and p.decision == "release"
    assert len([c for c in world.model.calls if c[0] == proof.GOODS_TASK]) == 1  # the scoring found it read


async def test_a_scoring_waits_for_the_photo_read_in_flight(world):
    req = await held(world)
    photo = evidence.store_attachment(world.s, world.shop, kind="goods", data=snapshot(66), content_type="image/jpeg",
                                      request_id=req.id)
    world.s.commit()
    flight = asyncio.get_running_loop().create_future()
    proof._LOOKING[photo.id] = flight

    async def land():
        await asyncio.sleep(0.05)
        with world.app.state.sessions() as s:
            s.get(Attachment, photo.id).reading = JsonText.dump(
                {"source": "model", "for": proof._photo_key(req), "check": GOODS_OK})
            s.commit()
        flight.set_result(None)

    try:
        asyncio.ensure_future(land())
        c = await proof.photo_check(world.s, world.model, req)
    finally:
        proof._LOOKING.pop(photo.id, None)
    assert c.points == 10 and not [x for x in world.model.calls if x[0] == proof.GOODS_TASK]


async def test_the_owners_view_shows_a_receipt_being_read(world):
    req = await held(world)
    seen = []
    spec = spec_for(req, no="MW-700101")

    def read(parts):
        seen.append(req.id in proof.PROVING)
        return reading_of(spec)

    world.model.answers[proof.READ_TASK] = read
    await send(world, req, spec, read=False)
    assert seen == [True] and req.id not in proof.PROVING
    assert world.client.get(f"/api/requests/{req.id}").json()["proving"] is False


async def test_returning_a_flagged_hold_closes_its_alert(world):
    req = await flagged(world)
    alert = world.s.scalar(select(Alert).where(Alert.key == f"proof:{req.id}:1"))
    r = world.client.post(f"/api/requests/{req.id}/settle", json={"spent": "0"})
    assert r.json()["status"] == "voided"
    world.s.expire_all()
    assert world.s.get(Alert, alert.id).dismissed


async def test_an_evidence_upload_stops_at_8_mb_as_it_arrives(world):
    req = await held(world)
    big = b"\xff\xd8" + b"\x00" * (evidence.MAX_BYTES + 100_000)
    r = world.client.post(f"/api/requests/{req.id}/evidence", data={"kind": "receipt"},
                          files={"file": ("r.jpg", big, "image/jpeg")})
    assert (r.status_code, r.json()["detail"]) == (413, "Files up to 8 MB, please.")

    def chunks():  # no Content-Length: the body is counted as it arrives
        yield b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"r.jpg\"\r\n\r\n"
        for _ in range(9):
            yield b"\x00" * 1_000_000

    r = world.client.post(f"/api/requests/{req.id}/evidence", content=chunks(),
                          headers={"content-type": "multipart/form-data; boundary=b"})
    assert r.status_code == 413
    assert world.client.post(f"/api/requests/{req.id}/evidence", data={"kind": "receipt"}).status_code == 422


async def test_a_shop_keeps_a_bounded_amount_of_evidence(world, monkeypatch):
    req = await held(world)
    kept = world.s.scalar(select(func.sum(Attachment.size)).where(Attachment.shop_id == 1))
    monkeypatch.setattr(evidence, "SHOP_BYTES", kept + 1000)
    r = world.client.post(f"/api/requests/{req.id}/evidence", data={"kind": "goods"},
                          files={"file": ("g.jpg", snapshot(71), "image/jpeg")})
    assert (r.status_code, r.json()["detail"]) == (422, "This shop's evidence is full at 40 MB.")
