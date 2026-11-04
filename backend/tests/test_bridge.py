import asyncio
import io
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

from musha import agent, bridge, camera, flows, ledger, proof
from musha.ai import Blob, NoModel
from musha import models
from musha.models import Attachment, LedgerEntry, Message, Payout, Proof, Shop, SpendRequest
from musha.paypal import PayPalError
from musha.receipts import Line, ReceiptSpec, reading_of, render_receipt
from musha.seed import reset
from musha.words import qty

WORDS = "Tinoda matray makumi mana emazai, US$180. Firiji inoda seal itsva, US$25."
ENGLISH = "We need forty trays of eggs, US$180. The fridge needs a new seal, US$25."
EGGS = {"kind": "stock", "item": "eggs", "quantity": 40, "unit": "trays", "amount": 180,
        "payee": "Mbare wholesale"}
SEAL = {"kind": "expense", "item": "fridge door seal", "quantity": 1, "unit": "units", "amount": 25,
        "payee": ""}
BREAD = {"kind": "stock", "item": "bread", "quantity": 20, "unit": "loaves", "amount": 17,
         "payee": "Mbare bakery"}
EXAMPLE = "40 trays of eggs from Mbare wholesale, US$180.00"
PHONE = "/api/shops/1/phone/messages"
VOICE = "/api/shops/1/phone/voice"


@pytest.fixture(autouse=True)
def fresh_bridge():
    """Translation failures and translations in flight are kept per process; each test starts clean."""
    bridge._failed.clear()
    bridge._localizing.clear()
    yield
    bridge._failed.clear()
    bridge._localizing.clear()


def shona(parts):
    return [f"[sn] {line}" for line in json.loads(parts[-1])]


def calls(model, task):
    return [c for c in model.calls if c[0] == task]


def read(english="", *, requests=(), answers=0, answer="", reply="", language="sn"):
    return {"language": language, "english": english, "answers_request_id": answers, "answer": answer,
            "requests": list(requests), "reply": reply}


def count(app, model=SpendRequest):
    with app.state.sessions() as s:
        return s.scalar(select(func.count(model.id)))


def request(client, item="eggs", quantity=40, unit="trays", amount="180.00", payee="Mbare wholesale"):
    return client.post("/api/shops/1/requests", json={"item": item, "quantity": quantity, "unit": unit,
                                                      "amount": amount, "payee": payee}).json()["id"]


def set_asking(app, *ids):
    with app.state.sessions() as s:
        for rid in ids:
            s.get(SpendRequest, rid).status = "asking"
        s.commit()


@pytest.fixture
def answers(monkeypatch):
    """proof.answer, recorded. Patched strictly, so a rename in proof fails here."""
    seen = []

    async def answer(session, pp, m, req, text):
        seen.append((req.id, text))

    monkeypatch.setattr(proof, "answer", answer)
    return seen


# Musha and the owner, in the manager's language

def test_lines_reach_the_manager_in_one_batched_translation(client, model):
    client.post("/api/shops/1/owner/to-manager", json={"text": "Thank you, Farai."})
    client.post("/api/shops/1/requests", json={"item": "bread", "quantity": 20, "unit": "loaves",
                                              "amount": "17.00", "payee": "Mbare bakery"})
    before = len(calls(model, "localize"))
    model.answers["localize"] = shona
    lines = client.get("/api/shops/1/messages").json()
    assert len(calls(model, "localize")) == before + 1
    ours = [m for m in lines if m["sender"] in ("musha", "owner")]
    assert len(ours) == 3
    assert all(m["language"] == "sn" and m["text"] == f"[sn] {m['text_en']}" for m in ours)
    assert any(m["text_en"] == "Thank you, Farai." for m in ours)
    # translated lines stay translated, so a second read asks the model nothing
    assert client.get("/api/shops/1/messages").json() == lines
    assert len(calls(model, "localize")) == before + 1


def test_a_list_of_the_wrong_length_is_tried_again_one_line_a_call(client, model):
    client.post("/api/shops/1/owner/to-manager", json={"text": "Well done."})
    request(client, "bread", 20, "loaves", "17.00", "Mbare bakery")
    model.answers["localize"] = lambda parts: shona(parts)[:1]
    before = len(calls(model, "localize"))
    lines = [m for m in client.get("/api/shops/1/messages").json() if m["sender"] != "manager"]
    assert all(m["language"] == "sn" and m["text"] == f"[sn] {m['text_en']}" for m in lines)
    # one batch of every pending line, then one call for each line
    assert len(calls(model, "localize")) == before + 1 + len(lines)


def test_a_line_that_keeps_failing_rests(client, model, monkeypatch):
    model.answers["localize"] = lambda parts: []
    client.post("/api/shops/1/owner/to-manager", json={"text": "Well done."})  # first try
    client.get("/api/shops/1/messages")
    client.get("/api/shops/1/messages")
    tried = len(calls(model, "localize"))
    lines = client.get("/api/shops/1/messages").json()  # after three failures the line rests
    assert len(calls(model, "localize")) == tried
    assert all(m["language"] == "en" for m in lines if m["sender"] != "manager")
    monkeypatch.setattr(bridge, "LOCALIZE_REST", 0)  # ten minutes later
    client.get("/api/shops/1/messages")
    assert len(calls(model, "localize")) > tried


def test_a_translation_that_changes_an_amount_keeps_the_english(client, model):
    model.answers["localize"] = lambda parts: [s.replace("US$180.00", "US$18.00") for s in shona(parts)]
    client.post("/api/shops/1/owner/to-manager", json={"text": "Thank you, Farai."})
    request(client)
    lines = client.get("/api/shops/1/messages").json()
    sent = next(m for m in lines if m["text_en"].startswith("Sent to Chipo"))
    assert (sent["language"], sent["text"]) == ("en", sent["text_en"])
    assert "US$180.00" in sent["text"]
    thanks = next(m for m in lines if m["text_en"] == "Thank you, Farai.")
    assert thanks["text"] == "[sn] Thank you, Farai."


def test_a_translation_keeps_numbers_names_and_a_sane_length():
    en = "Sent to Chipo: 40 trays of eggs, US$180.00. I will tell you when it is approved."
    names = ["Chipo", "Farai", "Mbare wholesale"]
    good = "Zvatumirwa kuna Chipo: matray 40 emazai, US$180.00. Ndichakuudza kana zvabvumirwa."
    assert bridge.faithful(en, good, names) == good
    assert bridge.faithful(en, good.replace("40", "4"), names) is None
    assert bridge.faithful(en, good.replace("US$", "$"), names) is None
    assert bridge.faithful(en, good.replace("Chipo", "amai"), names) is None
    assert bridge.faithful(en, "  ", names) is None
    assert bridge.faithful(en, good * 4, names) is None
    assert bridge.faithful("Ok.", "Zvakanaka.", names) == "Zvakanaka."


def test_lines_stay_english_with_the_model_off(app):
    with app.state.sessions() as s:
        shop = s.get(Shop, 1)
        asyncio.run(bridge.localize_pending(s, NoModel(), shop))
        lines = s.scalars(select(Message).where(Message.thread == "manager")).all()
        assert lines and all(m.language == "en" and m.text == m.text_en for m in lines)


class Slow:
    """A model that takes a moment to translate."""
    enabled = True

    def __init__(self):
        self.calls = 0

    async def json(self, task, parts, schema, *, system=""):
        self.calls += 1
        await asyncio.sleep(0.05)
        return shona(parts)


def test_the_phone_waits_for_a_translation_in_flight(app):
    async def run():
        model = Slow()
        with app.state.sessions() as a, app.state.sessions() as b:
            flows.say(a, 1, "First line.")
            a.commit()
            first = asyncio.create_task(bridge.localize_pending(a, model, a.get(Shop, 1)))
            await asyncio.sleep(0.01)  # the poll's translation is in flight
            mine = flows.say(b, 1, "Second line.")
            b.commit()
            await bridge.localize_pending(b, model, b.get(Shop, 1))  # another poll returns at once
            assert mine.language == "en"
            await bridge.localize_pending(b, model, b.get(Shop, 1), wait=True)  # the phone waits
            await first
            assert (mine.language, mine.text) == ("sn", "[sn] Second line.")
            assert model.calls == 2

    asyncio.run(run())


def test_owner_writes_english_and_the_manager_reads_shona(client, model):
    model.answers["localize"] = shona
    line = client.post("/api/shops/1/owner/to-manager", json={"text": "Well done this week."}).json()
    shown = next(m for m in client.get("/api/shops/1/messages").json() if m["id"] == line["id"])
    assert (shown["sender"], shown["thread"], shown["text"], shown["text_en"], shown["language"]) == (
        "owner", "manager", "[sn] Well done this week.", "Well done this week.", "sn")


# The manager, heard

def test_one_message_holds_two_requests(client, model):
    model.answers["read_message"] = read(ENGLISH, requests=[EGGS, SEAL])
    r = client.post(PHONE, json={"text": WORDS}).json()
    reqs = r["requests"]
    assert [(q["kind"], q["item"], q["quantity"], q["amount_cents"], q["payee"]) for q in reqs] == [
        ("stock", "eggs", 40, 18000, "Mbare wholesale"), ("expense", "fridge door seal", 1, 2500, "")]
    assert all((q["words"], q["words_en"], q["language"], q["origin"]) == (WORDS, ENGLISH, "sn", "manager")
               for q in reqs)
    line = r["messages"][0]
    assert (line["sender"], line["text"], line["text_en"], line["language"]) == ("manager", WORDS, ENGLISH, "sn")
    assert sum(m["text"].startswith("Sent to Chipo") for m in r["messages"]) == 2
    task, parts = calls(model, "read_message")[0]
    assert parts == [f"Message: {WORDS}"]


def test_goods_outside_the_catalogue_without_a_price_get_a_question_for_the_total(client, model):
    buns = {"kind": "stock", "item": "buns", "quantity": 30, "unit": "buns", "amount": 0, "payee": "Lobels"}
    model.answers["read_message"] = read("I need money for thirty buns from Lobels.", requests=[buns])
    r = client.post(PHONE, json={"text": "Ndoda mari yemabhanzi makumi matatu kubva kuLobels."}).json()
    assert r["requests"] == []
    ours = [m["text_en"] for m in r["messages"] if m["sender"] == "musha"]
    assert ours == ["What does 30 buns from Lobels cost? Send the total and I will ask Chipo."]
    model.answers["read_message"] = read("The fridge needs a new seal.", requests=[{**SEAL, "amount": 0}])
    r = client.post(PHONE, json={"text": "Firiji inoda seal itsva."}).json()
    assert [m["text_en"] for m in r["messages"] if m["sender"] == "musha"] == [
        "What does the fridge door seal cost? Send the total and I will ask Chipo."]


def test_missing_price_and_payee_come_from_the_shops_history(client, model):
    model.answers["read_message"] = read("Forty trays of eggs.", requests=[{**EGGS, "item": "Eggs", "amount": 0,
                                                                           "payee": ""}])
    req = client.post(PHONE, json={"text": "Matray makumi mana emazai"}).json()["requests"][0]
    assert (req["item"], req["unit"], req["payee"], req["amount_cents"]) == ("eggs", "trays", "Mbare wholesale", 18000)


def test_the_price_of_one_is_read_as_the_total(client, model):
    model.answers["read_message"] = read("Forty trays of eggs at US$4.50.", requests=[{**EGGS, "amount": 4.5}])
    r = client.post(PHONE, json={"text": "Matray 40 emazai, US$4.50 imwe neimwe"}).json()
    assert r["requests"][0]["amount_cents"] == 18000
    assert not any(m["text_en"].startswith("Check the price") for m in r["messages"])


def test_a_price_far_from_the_usual_is_kept_and_flagged(client, model):
    model.answers["read_message"] = read("Forty trays of eggs, US$1000.", requests=[{**EGGS, "amount": 1000}])
    r = client.post(PHONE, json={"text": "Matray 40 emazai, US$1000"}).json()
    req = r["requests"][0]
    assert req["amount_cents"] == 100000
    note = r["messages"][-1]
    assert (note["sender"], note["request_id"]) == ("musha", req["id"])
    assert note["text_en"] == ("Check the price for 40 trays of eggs. US$1000.00 is US$25.00 a tray, and recent "
                               "buys were US$4.50 a tray. If the total is wrong, send the right one.")


def test_one_message_carries_five_requests_at_most_each_once(app, client, model):
    many = [EGGS, EGGS, SEAL, BREAD, *({**SEAL, "item": f"repair {i}"} for i in range(4))]
    model.answers["read_message"] = read("A long list.", requests=many)
    before = count(app)
    r = client.post(PHONE, json={"text": "Rondedzero refu"}).json()
    assert [q["item"] for q in r["requests"]] == ["eggs", "fridge door seal", "bread", "repair 0", "repair 1"]
    assert count(app) == before + 5
    assert any(m["text_en"] == "I sent the first 5 requests. Send the rest in another message."
               for m in r["messages"])


def test_long_names_are_cut_to_fit_their_columns(client, model):
    long = {"kind": "expense", "item": "door " * 40, "quantity": 1, "unit": "u" * 40, "amount": 30,
            "payee": "p" * 300}
    model.answers["read_message"] = read("A repair.", requests=[long])
    req = client.post(PHONE, json={"text": "Kugadzirisa"}).json()["requests"][0]
    assert (len(req["item"]), len(req["unit"]), len(req["payee"])) == (79, 24, 120)


def test_a_message_without_a_request_gets_a_nudge_from_the_shops_goods(client, model):
    model.answers["read_message"] = read("Good morning.")
    r = client.post(PHONE, json={"text": "Mangwanani"}).json()
    assert r["requests"] == []
    nudge = r["messages"][-1]
    assert nudge["sender"] == "musha"
    assert nudge["text_en"].endswith(f"For example: {EXAMPLE}.")


def test_when_the_model_fails_the_manager_hears_how_to_type_it(client, model):
    r = client.post(PHONE, json={"text": "Tinoda matray makumi mana emazai"}).json()
    assert r["requests"] == []
    assert [m["sender"] for m in r["messages"]] == ["manager", "musha"]
    assert r["messages"][1]["text_en"] == f"{bridge.SAY_AGAIN} For example: {EXAMPLE}."


def test_a_quantity_or_amount_beyond_reason_is_refused(app, client, model, monkeypatch):
    before = count(app)
    check = "Check the quantity and the amount for {}, then send the request again."
    model.answers["read_message"] = read("Eggs.", requests=[{**EGGS, "quantity": 10 ** 19}])
    r = client.post(PHONE, json={"text": "Mazai"})
    assert (r.status_code, r.json()["requests"]) == (200, [])
    assert check.format("eggs") in [m["text_en"] for m in r.json()["messages"]]
    model.answers["read_message"] = read("A repair.", requests=[{**SEAL, "amount": 1e20}])
    r = client.post(PHONE, json={"text": "Kugadzirisa"})
    assert (r.status_code, r.json()["requests"]) == (200, [])
    assert check.format("fridge door seal") in [m["text_en"] for m in r.json()["messages"]]
    assert count(app) == before

    def refused(*a, **kw):  # a value the database cannot hold
        raise OverflowError("Python int too large to convert to SQLite INTEGER")

    monkeypatch.setattr(flows, "create_request", refused)
    model.answers["read_message"] = read("A repair.", requests=[SEAL])
    r = client.post(PHONE, json={"text": "Kugadzirisa zvakare"})
    assert (r.status_code, r.json()["requests"]) == (200, [])
    assert any(m["text_en"].startswith("The request for fridge door seal was too long to save.")
               for m in r.json()["messages"])


def test_without_a_model_the_example_still_reads_and_the_reply_comes_once(app):
    off = NoModel()
    with app.state.sessions() as s:
        shop = s.get(Shop, 1)
        before = s.scalar(select(func.count(SpendRequest.id)))
        assert asyncio.run(bridge.hear(s, None, off, shop, text="Tinoda mazai")) == []
        line, reply = s.scalars(select(Message).order_by(Message.id.desc()).limit(2)).all()[::-1]
        assert (line.sender, line.text, line.language) == ("manager", "Tinoda mazai", "sn")
        assert reply.text_en == f"{bridge.SAY_AGAIN} For example: {EXAMPLE}."
        asyncio.run(bridge.hear(s, None, off, shop, text="Mazai"))
        assert s.scalars(select(Message).order_by(Message.id.desc())).first().text == "Mazai"  # said once
        made = asyncio.run(bridge.hear(s, None, off, shop, text=f"{EXAMPLE}."))
        assert [(r.item, r.quantity, r.amount_cents, r.payee) for r in made] == [
            ("eggs", 40, 18000, "Mbare wholesale")]
        assert s.scalar(select(func.count(SpendRequest.id))) == before + 1


def test_the_example_shape_reads_by_rule(app):
    with app.state.sessions() as s:
        goods = bridge.products(s, s.get(Shop, 1))
        found = bridge.rule_read("20 loaves of bread US$17 and 6 2 L bottles of cooking oil from "
                                 "Mbare wholesale; 1 tray of eggs", goods)
    assert [(f["item"], f["quantity"], f["amount"], f["payee"]) for f in found] == [
        ("bread", 20, "17", ""), ("cooking oil", 6, "0", "Mbare wholesale"), ("eggs", 1, "0", "")]


def test_a_count_with_a_multiplication_sign_reads_by_rule(app):
    """Musha's own examples write '10 × 2 kg packs of sugar': typed back, they read."""
    with app.state.sessions() as s:
        goods = bridge.products(s, s.get(Shop, 1))
        found = bridge.rule_read(f"{qty(10, '2 kg packs')} of sugar from Mbare wholesale, US$19.00; "
                                 f"{qty(1, '2 L bottles')} of cooking oil; 6 x 2 L bottles of cooking oil", goods)
    assert [(f["item"], f["quantity"], f["amount"], f["payee"]) for f in found] == [
        ("sugar", 10, "19.00", "Mbare wholesale"), ("cooking oil", 1, "0", ""), ("cooking oil", 6, "0", "")]


def test_a_request_for_one_pack_reads_in_the_singular(app):
    with app.state.sessions() as s:
        req = flows.create_request(s, s.get(Shop, 1), item="sugar", quantity=1, unit="2 kg packs",
                                   amount_cents=190, payee="Mbare wholesale")
        assert flows.describe(req) == "1 × 2 kg pack of sugar"
        line = s.scalars(select(Message).order_by(Message.id.desc())).first()
        assert line.text_en == "Sent to Chipo: 1 × 2 kg pack of sugar, US$1.90. I will tell you when it is approved."


def test_voice_note_is_transcribed_then_read(app, client, model):
    audio = b"\x1aE\xdf\xa3 voice note"
    model.answers["transcribe"] = {"text": WORDS, "language": "sn", "english": ENGLISH}
    model.answers["read_message"] = read(ENGLISH, requests=[EGGS, SEAL])
    r = client.post(VOICE, files={"audio": ("note.webm", audio, "audio/webm;codecs=opus")})
    assert r.status_code == 200
    out = r.json()
    assert len(out["requests"]) == 2
    line = out["messages"][0]
    assert (line["sender"], line["text"], line["text_en"], line["language"]) == ("manager", WORDS, ENGLISH, "sn")
    task, parts = calls(model, "transcribe")[0]
    assert parts[0] == Blob(audio, "audio/webm")
    with app.state.sessions() as s:
        voice = s.get(Attachment, line["attachment_id"])
        assert (voice.kind, voice.content_type, voice.data, voice.size) == ("voice", "audio/webm", audio, len(audio))
        assert len(voice.sha256) == 64


def test_an_unclear_voice_note_asks_for_text(client, model):
    r = client.post(VOICE, files={"audio": ("note.ogg", b"OggS", "audio/ogg")}).json()
    assert r["requests"] == []
    assert [m["sender"] for m in r["messages"]] == ["manager", "musha"]
    assert r["messages"][0]["text"] == "Voice note" and r["messages"][0]["attachment_id"]
    assert r["messages"][1]["text_en"] == bridge.UNCLEAR_VOICE and "type the message" in bridge.UNCLEAR_VOICE
    # a transcription of the wrong shape is unclear too
    model.answers["transcribe"] = {"text": 42, "language": ["sn"], "english": None}
    r = client.post(VOICE, files={"audio": ("note.ogg", b"OggS2", "audio/ogg")})
    assert r.status_code == 200
    assert r.json()["messages"][-1]["text_en"] == bridge.UNCLEAR_VOICE


def test_without_a_model_a_voice_note_gets_the_example(app):
    with app.state.sessions() as s:
        shop = s.get(Shop, 1)
        note = Attachment(shop_id=1, kind="voice", content_type="audio/ogg", data=b"OggS", size=4, sha256="0" * 64)
        s.add(note)
        s.flush()
        assert asyncio.run(bridge.hear(s, None, NoModel(), shop, voice=note)) == []
        reply = s.scalars(select(Message).order_by(Message.id.desc())).first()
        assert reply.text_en == f"{bridge.VOICE_OFF} Type it like this: {EXAMPLE}."


def test_a_voice_note_in_reply_to_a_question_keeps_the_question_open(app, client, model, answers):
    rid = request(client)
    set_asking(app, rid)
    model.answers["transcribe"] = {"text": "", "language": "sn", "english": ""}
    r = client.post(VOICE, data={"request_id": str(rid), "client_id": "v-1"},
                    files={"audio": ("note.ogg", b"OggS", "audio/ogg")})
    assert r.status_code == 200
    out = r.json()
    assert answers == []
    assert [(m["sender"], m["request_id"]) for m in out["messages"]] == [("manager", rid), ("musha", rid)]
    assert out["messages"][-1]["text_en"] == bridge.TYPE_ANSWER
    with app.state.sessions() as s:
        assert s.get(SpendRequest, rid).status == "asking"


def test_a_voice_note_answers_the_question_it_replies_to(app, client, model, answers):
    rid = request(client)
    set_asking(app, rid)
    english = "The wholesaler dropped the price."
    model.answers["transcribe"] = {"text": "Mutengo wadzikira.", "language": "sn", "english": english}
    model.answers["read_message"] = read(english, answer=english)  # the model finds the answer, naming no request
    r = client.post(VOICE, data={"request_id": str(rid)}, files={"audio": ("note.ogg", b"OggS3", "audio/ogg")})
    assert r.status_code == 200
    assert answers == [(rid, english)]
    assert r.json()["messages"][0]["request_id"] == rid


def test_voice_notes_are_audio(client):
    r = client.post(VOICE, files={"audio": ("x.png", b"png", "image/png")})
    assert r.status_code == 415
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    r = client.post(VOICE, files={"audio": ("x.webm", png, "audio/webm")})
    assert r.status_code == 415
    assert client.post(VOICE, files={"audio": ("x.webm", b"", "audio/webm")}).status_code == 422
    assert client.post(VOICE, json={"audio": "x"}).status_code == 422


def test_voice_notes_over_8_mb_are_refused_before_they_are_read(app, client, model):
    before = count(app, Attachment)
    over = b"\x1aE\xdf\xa3" + b"0" * (8 * 1024 * 1024 - 3)  # 8 MB and one byte
    r = client.post(VOICE, files={"audio": ("n.webm", over, "audio/webm")})
    assert (r.status_code, r.json()["detail"]) == (413, "Keep voice notes under 8 MB.")
    head = (b'--x\r\nContent-Disposition: form-data; name="audio"; filename="n.webm"\r\n'
            b"Content-Type: audio/webm\r\n\r\n\x1aE\xdf\xa3")

    def chunks():  # no length up front: the cap applies as the body streams
        yield head
        for _ in range(9):
            yield b"0" * 1024 * 1024
        yield b"\r\n--x--\r\n"

    r = client.post(VOICE, content=chunks(), headers={"content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 413
    r = client.post(VOICE, content=b"0" * 16, headers={"content-type": "multipart/form-data; boundary=x",
                                                       "content-length": str(9 * 1024 * 1024)})
    assert r.status_code == 413
    assert count(app, Attachment) == before
    assert calls(model, "transcribe") == []


# Heard once

def test_the_same_words_twice_are_heard_once(app, client, model):
    model.answers["read_message"] = read(ENGLISH, requests=[EGGS, SEAL])
    before = count(app)
    first = client.post(PHONE, json={"text": WORDS}).json()
    again = client.post(PHONE, json={"text": WORDS}).json()
    assert again == first
    assert count(app) == before + 2
    assert len(calls(model, "read_message")) == 1


def test_the_phones_own_id_makes_a_retry_safe(app, client, model):
    model.answers["read_message"] = read(ENGLISH, requests=[EGGS])
    before = count(app)
    first = client.post(PHONE, json={"text": WORDS}, headers={"Idempotency-Key": "m-1"}).json()
    retry = client.post(PHONE, json={"text": WORDS + " "}, headers={"Idempotency-Key": "m-1"}).json()
    assert [q["id"] for q in retry["requests"]] == [q["id"] for q in first["requests"]]
    assert count(app) == before + 1
    model.answers["read_message"] = read("Twenty loaves of bread.", requests=[BREAD])
    client.post(PHONE, json={"text": WORDS, "client_id": "m-2"})  # a new id is a new message
    assert count(app) == before + 2
    assert len(calls(model, "read_message")) == 2


def test_the_same_words_after_two_minutes_are_a_new_message(app, client, model):
    model.answers["read_message"] = read(ENGLISH, requests=[EGGS])
    before = count(app)
    client.post(PHONE, json={"text": WORDS})
    with app.state.sessions() as s:
        for row in s.scalars(select(bridge.Heard)):
            row.at = flows.now() - timedelta(minutes=3)
        s.commit()
    again = client.post(PHONE, json={"text": WORDS}).json()
    assert len(calls(model, "read_message")) == 2  # heard afresh
    # the eggs are on their way already, so the chase makes no second request
    assert count(app) == before + 1
    assert again["messages"][-1]["text_en"].endswith("is open already. It waits for Chipo.")


def test_a_message_still_being_read_is_a_conflict(app, client, model):
    with app.state.sessions() as s:
        s.add(bridge.Heard(shop_id=1, key=bridge.message_key(client_id="m-9"), at=flows.now()))
        s.commit()
    r = client.post(PHONE, json={"text": WORDS}, headers={"Idempotency-Key": "m-9"})
    assert (r.status_code, r.json()["detail"]) == (409, "Musha is still reading that message.")
    assert calls(model, "read_message") == []


def test_a_retry_after_a_failure_asks_for_nothing_twice(app, model, monkeypatch):
    client = TestClient(app, raise_server_exceptions=False)
    model.answers["read_message"] = read(ENGLISH, requests=[EGGS, SEAL])
    seen = []

    async def consider(session, pp, m, req):
        seen.append(req.id)
        if len(seen) == 2:
            raise RuntimeError("PayPal timed out")

    monkeypatch.setattr(agent, "consider", consider)
    before = count(app)
    assert client.post(PHONE, json={"text": WORDS}).status_code == 500
    assert count(app) == before + 2
    r = client.post(PHONE, json={"text": WORDS})
    assert r.status_code == 200
    assert [q["item"] for q in r.json()["requests"]] == ["eggs", "fridge door seal"]
    assert count(app) == before + 2 and len(calls(model, "read_message")) == 1


def test_a_message_that_fails_before_anything_is_kept_can_be_sent_again(app, model, monkeypatch):
    client = TestClient(app, raise_server_exceptions=False)
    model.answers["read_message"] = read(ENGLISH, requests=[EGGS])

    def broken(*a, **kw):
        raise RuntimeError("catalogue unavailable")

    monkeypatch.setattr(bridge, "catalogue", broken)
    assert client.post(PHONE, json={"text": WORDS}).status_code == 500
    monkeypatch.undo()
    before = count(app)
    assert len(client.post(PHONE, json={"text": WORDS}).json()["requests"]) == 1
    assert count(app) == before + 1


def test_lines_from_elsewhere_do_not_mark_a_failed_message_heard(app, model):
    client = TestClient(app, raise_server_exceptions=False)

    def read_after_a_failure(parts):
        if len(calls(model, "read_message")) == 1:
            with app.state.sessions() as other:  # the webhook writes while Musha reads
                flows.say(other, 1, "Proof accepted. US$10.00 released for bread.")
                other.commit()
            raise RuntimeError("the model client failed")
        return read(ENGLISH, requests=[EGGS])

    model.answers["read_message"] = read_after_a_failure
    said = {"text": WORDS, "client_id": "m-5"}
    assert client.post(PHONE, json=said).status_code == 500
    r = client.post(PHONE, json=said)
    assert r.status_code == 200
    assert [q["item"] for q in r.json()["requests"]] == ["eggs"]
    assert [m["text"] for m in r.json()["messages"] if m["sender"] == "manager"] == [WORDS]


def test_heard_lives_with_the_other_tables_and_a_reset_clears_it(app):
    assert bridge.Heard is models.Heard and "heard" in models.Base.metadata.tables
    with app.state.sessions() as s:
        s.add(bridge.Heard(shop_id=1, key=bridge.message_key(client_id="m-8"), at=flows.now()))
        s.commit()
    reset(app.state.engine, app.state.sessions)
    with app.state.sessions() as s:
        assert s.scalar(select(func.count(bridge.Heard.id))) == 0
        s.add(bridge.Heard(shop_id=1, key=bridge.message_key(client_id="m-8"), at=flows.now()))
        s.commit()  # the table came back with the rest


def test_a_row_older_than_its_shop_is_a_new_message(app, client, model):
    """A row a reset left behind, from an earlier database, hears its key afresh."""
    model.answers["read_message"] = read(ENGLISH, requests=[EGGS])
    with app.state.sessions() as s:
        born = flows.utc(s.get(Shop, 1).created_at)
        s.add(bridge.Heard(shop_id=1, key=bridge.message_key(client_id="m-7"), since=900, until=950,
                           request_ids="[999]", at=born - timedelta(days=1)))
        s.commit()
    r = client.post(PHONE, json={"text": WORDS, "client_id": "m-7"}).json()
    assert [q["item"] for q in r["requests"]] == ["eggs"]
    assert len(calls(model, "read_message")) == 1


# Answers to the proof engine's questions

def test_a_reply_goes_to_the_request_that_is_asking(app, client, model, answers):
    rid = request(client)
    set_asking(app, rid)
    before = count(app)
    english = "The wholesaler dropped the price to US$4.27 a tray."
    model.answers["read_message"] = read(english, answers=rid, answer=english)
    r = client.post(PHONE, json={"text": "Mutengo wadzikira US$4.27 pa tray."}).json()
    assert answers == [(rid, english)]
    assert [q["id"] for q in r["requests"]] == [rid]
    assert r["messages"][0]["request_id"] == rid
    assert count(app) == before


def test_an_answer_and_a_new_request_in_one_message(app, client, model, answers):
    rid = request(client)
    set_asking(app, rid)
    model.answers["read_message"] = read("The price went up this week. We also need twenty loaves of bread.",
                                         answers=rid, answer="The price went up this week.",
                                         requests=[BREAD, {**EGGS, "amount": 190}])
    r = client.post(PHONE, json={"text": "Mutengo wakwira. Tinodawo zvingwa 20."}).json()
    assert answers == [(rid, "The price went up this week.")]
    # the eggs it answers about are no new request; the bread is
    assert [(q["id"] == rid, q["item"]) for q in r["requests"]] == [(True, "eggs"), (False, "bread")]
    assert r["requests"][1]["amount_cents"] == 1700


def test_with_two_questions_open_the_answer_goes_to_the_one_named(app, client, model, answers):
    eggs, bread = request(client), request(client, "bread", 20, "loaves", "17.00", "Mbare bakery")
    set_asking(app, eggs, bread)
    model.answers["read_message"] = read("The eggs were dearer this week.", answers=eggs,
                                         answer="The eggs were dearer this week.")
    client.post(PHONE, json={"text": "Mazai aidhura svondo rino."})
    assert answers == [(eggs, "The eggs were dearer this week.")]
    # the phone's reply to a question outranks the model's guess
    model.answers["read_message"] = read("The baker had no change.", answers=eggs, answer="The baker had no change.")
    client.post(PHONE, json={"text": "Mubiki akashaya chenji.", "request_id": bread})
    assert answers[-1] == (bread, "The baker had no change.")


def test_with_two_questions_open_and_none_named_musha_asks_which(app, client, model, answers):
    eggs, bread = request(client), request(client, "bread", 20, "loaves", "17.00", "Mbare bakery")
    set_asking(app, eggs, bread)
    model.answers["read_message"] = read("It was dearer.")
    r = client.post(PHONE, json={"text": "Zvaidhura."}).json()
    assert answers == [] and r["requests"] == []
    assert r["messages"][-1]["text_en"] == ("Which request is your answer for? I have questions open on bread "
                                            "and eggs. Start your answer with the item.")


def test_a_closed_question_gets_a_plain_reply(app, client, model):
    rid = request(client)
    set_asking(app, rid)  # asking, with no hold behind it: proof.answer refuses
    model.answers["read_message"] = read("It was dearer.", answers=rid, answer="It was dearer.")
    r = client.post(PHONE, json={"text": "Zvaidhura."}).json()
    assert r["messages"][-1]["text_en"] == bridge.CLOSED
    assert r["messages"][-1]["request_id"] == rid


def test_words_the_model_could_not_read_answer_no_question(app, client, model, answers):
    rid = request(client)
    set_asking(app, rid)
    before = count(app)
    r = client.post(PHONE, json={"text": "Tinoda mazai makumi mana"}).json()  # the model is away
    assert answers == [] and r["requests"] == [] and count(app) == before
    assert r["messages"][-1]["text_en"].startswith(bridge.SAY_AGAIN)
    # a reply the phone sends to the question still reaches it
    client.post(PHONE, json={"text": "Zvaidhura.", "request_id": rid})
    assert answers == [(rid, "Zvaidhura.")]


ANSWER = "The wholesaler raised eggs to US$6.00 a tray this week."
ANSWER_WORDS = "Mazai akwira kusvika US$6.00 pa tray svondo rino."


def asking_hold(app, client, model, monkeypatch) -> tuple[int, str]:
    """Thirty trays held, with a receipt dearer than usual: the proof engine asks why.
    The manager's answer, when it comes, eases the price check."""
    async def no_camera(session, m, shop, *, item, since, until, **more):
        return None

    monkeypatch.setattr(camera, "delivery_check", no_camera)
    with app.state.sessions() as s:  # the shop's PayPal, whatever the local .env says
        s.get(Shop, 1).manager.email = "farai-manager@example.com"
        s.commit()
    rid = request(client, quantity=30)
    client.post(f"/api/requests/{rid}/order")
    assert client.post(f"/api/requests/{rid}/authorize").json()["status"] == "held"
    model.answers[proof.GOODS_TASK] = {"seen": True, "count": 30, "quantity_ok": True, "reason": "Thirty trays."}
    photo = io.BytesIO()
    Image.new("RGB", (64, 48), "white").save(photo, "JPEG")
    client.post(f"/api/requests/{rid}/evidence", data={"kind": "goods"},
                files={"file": ("eggs.jpg", photo.getvalue(), "image/jpeg")})
    printed = flows.now().astimezone(proof.SHOP_TZ).replace(tzinfo=None, second=0, microsecond=0)
    spec = ReceiptSpec(payee="Mbare wholesale", address="Stand 9, Mbare, Harare", receipt_no="MW-900001",
                       at=printed, lines=(Line("eggs (trays)", 30, 600),))
    model.answers[proof.READ_TASK] = reading_of(spec)
    first = client.post(f"/api/requests/{rid}/evidence", data={"kind": "receipt"},
                        files={"file": ("r.jpg", render_receipt(spec, seed=21), "image/jpeg")}).json()
    assert (first["proof"]["decision"], first["request"]["status"]) == ("ask", "asking")
    model.answers["read_message"] = read(ANSWER, answers=rid, answer=ANSWER)
    model.answers[proof.ANSWER_TASK] = {"resolves": ["price"], "reason": "The wholesaler raised egg prices."}
    return rid, first["proof"]["question"]


@pytest.mark.parametrize("error", [flows.FlowError("PayPal returned capture status DECLINED."),
                                   PayPalError(503, {"name": "SERVICE_UNAVAILABLE"})],
                         ids=["declined", "paypal_down"])
def test_an_answer_that_fails_keeps_the_question_open_for_a_retry(app, client, model, monkeypatch, error):
    rid, _ = asking_hold(app, client, model, monkeypatch)
    real, failing = proof.answer, [error]

    async def answer(*a, **kw):  # the answer fails before it lands, the question still open
        if failing:
            raise failing.pop()
        return await real(*a, **kw)

    monkeypatch.setattr(proof, "answer", answer)
    said = {"text": ANSWER_WORDS, "client_id": "answer-1"}
    r = client.post(PHONE, json=said)
    assert (r.status_code, r.json()["detail"]) == (409, bridge.RETRY)
    with app.state.sessions() as s:
        req = s.get(SpendRequest, rid)
        assert (req.status, req.hold.status, req.proofs[-1].decision, req.proofs[-1].answer) == (
            "asking", "held", "ask", "")
        last = s.scalars(select(Message).where(Message.thread == "manager").order_by(Message.id.desc())).first()
        assert (last.text_en, last.request_id) == (bridge.RETRY, rid)
    # the phone sends the same message again, and the answer lands
    out = client.post(PHONE, json=said).json()
    assert [(q["id"], q["status"]) for q in out["requests"]] == [(rid, "captured")]
    assert len(calls(model, "read_message")) == 2
    assert bridge.CLOSED not in [m["text_en"] for m in client.get("/api/shops/1/messages").json()]


def test_an_answer_from_the_phone_releases_the_money(app, client, model, monkeypatch):
    rid, asked = asking_hold(app, client, model, monkeypatch)
    question = client.get("/api/shops/1/messages").json()[-1]
    assert (question["request_id"], question["text_en"]) == (rid, asked)
    out = client.post(PHONE, json={"text": ANSWER_WORDS}).json()
    assert [(q["id"], q["status"]) for q in out["requests"]] == [(rid, "captured")]
    with app.state.sessions() as s:
        captured = s.scalars(select(LedgerEntry).where(LedgerEntry.request_id == rid,
                                                       LedgerEntry.kind == ledger.FUNDS_CAPTURED)).all()
        assert [e.amount_cents for e in captured] == [18000]
        assert [(p.purpose, p.amount_cents) for p in s.scalars(select(Payout).where(Payout.request_id == rid))] == [
            ("release", 18000)]
        assert s.get(SpendRequest, rid).proofs[-1].answer == ANSWER


def test_a_reply_to_an_answered_question_gets_a_plain_reply(app, client, model):
    rid = request(client)
    with app.state.sessions() as s:
        s.add(Proof(request_id=rid, decision="ask", question="Why was the price higher?"))
        s.commit()
    model.answers["read_message"] = read("It was dearer.")
    r = client.post(PHONE, json={"text": "Zvaidhura.", "request_id": rid}).json()
    assert r["messages"][-1]["text_en"] == bridge.CLOSED
    # a reply to a line with no question gets the usual nudge
    other = request(client, "bread", 20, "loaves", "17.00", "Mbare bakery")
    r = client.post(PHONE, json={"text": "Ndatenda.", "request_id": other}).json()
    assert r["messages"][-1]["text_en"].startswith("Tell me what the shop needs")


# Which question a message answers

def test_a_reply_to_a_closed_question_never_answers_another_request(app, client, model, answers):
    eggs, bread = request(client), request(client, "bread", 20, "loaves", "17.00", "Mbare bakery")
    with app.state.sessions() as s:  # the eggs question has its answer; bread is asking
        s.add(Proof(request_id=eggs, decision="ask", question="Why was the price higher?", answer="A price rise."))
        s.commit()
    set_asking(app, bread)
    model.answers["read_message"] = read("It was dearer.")
    r = client.post(PHONE, json={"text": "Zvaidhura.", "request_id": eggs}).json()
    assert answers == []
    assert r["messages"][-1]["text_en"] == bridge.CLOSED
    with app.state.sessions() as s:
        assert s.get(SpendRequest, bread).status == "asking"


def test_a_new_request_sent_under_the_question_banner_is_no_answer(app, client, model, answers):
    rid = request(client)
    set_asking(app, rid)
    before = count(app)
    model.answers["read_message"] = read("I need 20 loaves of bread, $18 from Mbare bakery.",
                                         requests=[{**BREAD, "amount": 18}])
    r = client.post(PHONE, json={"text": "Ndoda bread 20 loaves, $18 kuMbare bakery", "request_id": rid}).json()
    assert answers == []  # the eggs question stays open for its own answer
    assert [q["item"] for q in r["requests"]] == ["bread"] and count(app) == before + 1
    assert r["messages"][0]["request_id"] == r["requests"][0]["id"]
    with app.state.sessions() as s:
        assert s.get(SpendRequest, rid).status == "asking"


def test_small_talk_under_the_question_banner_is_no_answer(app, client, model, answers):
    rid = request(client)
    set_asking(app, rid)
    model.answers["read_message"] = read("Good morning Musha, how are you?", reply="Good morning, Farai.")
    r = client.post(PHONE, json={"text": "Mangwanani Musha, makadii?", "request_id": rid}).json()
    assert answers == []
    assert r["messages"][-1]["text_en"] == "Good morning, Farai."
    # with the model away, a reply that asks for other goods is no answer either
    model.answers["read_message"] = None
    client.post(PHONE, json={"text": "20 loaves of bread from Mbare bakery, US$17", "request_id": rid})
    assert answers == []


# A chase for an open request

def test_a_chase_for_an_open_request_opens_no_second_one(app, client, model):
    model.answers["read_message"] = read("I need money for eggs, forty trays, $180 at Mbare wholesale.",
                                         requests=[EGGS])
    first = client.post(PHONE, json={"text": "Ndoda mari yemazai, trays makumi mana, $180 kuMbare wholesale"}).json()
    rid = first["requests"][0]["id"]
    before = count(app)
    model.answers["read_message"] = read("Musha, eggs 40 trays $180 please, Chipo has not replied yet.",
                                         requests=[EGGS])
    r = client.post(PHONE, json={"text": "Musha, mazai 40 trays $180 ndapota, Chipo haasati apindura"}).json()
    assert count(app) == before
    assert [q["id"] for q in r["requests"]] == [rid]
    last = r["messages"][-1]
    assert (last["text_en"], last["request_id"]) == (
        f"Request {rid} for 40 trays of eggs, US$180.00, is open already. It waits for Chipo.", rid)


def test_the_reader_is_told_the_open_requests_and_the_wages(app, client, model, monkeypatch):
    seen = {}
    real = model.json

    async def json_(task, parts, schema, *, system=""):
        seen[task] = system
        return await real(task, parts, schema, system=system)

    monkeypatch.setattr(model, "json", json_)
    rid = request(client)
    model.answers["read_message"] = read("Did Chipo send the money for eggs?", reply="Yes.")
    client.post(PHONE, json={"text": "Chipo atumira mari yemazai here?"})
    assert f"Request {rid}, 40 trays of eggs, US$180.00. It waits for Chipo." in seen["read_message"]
    assert "Chipo has paid Farai US$0.00 in wages through Musha so far." in seen["read_message"]


@pytest.mark.parametrize("reply", ["Let me check with Chipo and get back to you about the money for eggs.",
                                   "Your wages this month are US$250.00.",
                                   "Chipo has not sent the money yet."])
def test_a_reply_that_promises_or_invents_gives_way_to_the_books(app, client, model, reply):
    rid = request(client)
    model.answers["read_message"] = read("A question.", reply=reply)
    r = client.post(PHONE, json={"text": "Chipo atumira mari yemazai here?"}).json()
    assert r["messages"][-1]["text_en"] == f"Request {rid}, 40 trays of eggs, US$180.00. It waits for Chipo."


# Questions from the owner's alerts

def asked_alert(client, kind):
    alert = next(a for a in client.get("/api/shops/1/alerts").json() if a["kind"] == kind)
    client.post(f"/api/alerts/{alert['id']}/act", json={"action": "ask"})
    return alert


def test_farais_answer_to_a_cash_gap_reaches_chipo(app, client, model):
    gap = asked_alert(client, "cash_gap")
    english = "I took $23 from the till and bought paraffin and candles for the shop."
    model.answers["read_message"] = {**read(english), "answers_alert_id": gap["id"], "alert_answer": english}
    r = client.post(PHONE, json={"text": "Ndakatora $23 kubva mutill ndikatenga paraffin nemakenduru."}).json()
    assert r["messages"][-1]["text_en"] == "Thank you. Chipo has your answer."
    owner = client.get("/api/shops/1/owner/messages").json()[-1]
    day = gap["data"]["question"].split(" the till")[0].removeprefix("On ")
    assert owner["text"] == f"Farai, about the till on {day}: {english}"
    data = next(a for a in client.get("/api/shops/1/alerts").json() if a["id"] == gap["id"])["data"]
    assert (data["answer_en"], data["answer_message_id"]) == (english, r["messages"][0]["id"])
    with app.state.sessions() as s:  # answered, the question leaves the reader's list
        assert gap["id"] not in [a.id for a in bridge.asked_open(s, s.get(Shop, 1))]


def test_the_reader_sees_the_alert_question_farai_replies_to(app, client, model):
    low = asked_alert(client, "low_stock")
    with app.state.sessions() as s:
        listed = bridge.alert_questions(bridge.asked_open(s, s.get(Shop, 1)))
    assert (f"- alert {low['id']}, low stock: Eggs: 1.8 days left. Suggested order: 40 trays of eggs from "
            f"Mbare wholesale, US$180.00.") in listed
    model.answers["read_message"] = {**read("Yes, that is right, please send it.", requests=[EGGS]),
                                     "answers_alert_id": low["id"], "alert_answer": "Yes, please send it."}
    r = client.post(PHONE, json={"text": "Hongu ndizvo, ndapota tumira"}).json()
    req = r["requests"][0]
    assert (req["item"], req["quantity"], req["amount_cents"]) == ("eggs", 40, 18000)
    with app.state.sessions() as s:  # the yes funds the alert, as the owner's fund button does
        alert = s.get(models.Alert, low["id"])
        assert (alert.request_id, alert.dismissed) == (req["id"], True)


# What Musha fills in

def test_a_payee_the_message_never_names_is_marked_inferred(client, model):
    model.answers["read_message"] = read("I need eggs, 40 trays, $180.", requests=[EGGS])
    req = client.post(PHONE, json={"text": "Ndoda mazai 40 trays $180"}).json()["requests"][0]
    assert (req["payee"], req["inferred"]) == ("Mbare wholesale", ["payee"])
    model.answers["read_message"] = read("Bread, 20 loaves, $17 at Mbare bakery.", requests=[BREAD])
    req = client.post(PHONE, json={"text": "Bread 20 loaves $17 kuMbare bakery"}).json()["requests"][0]
    assert (req["payee"], req["inferred"]) == ("Mbare bakery", [])


def test_a_request_without_a_count_asks_for_a_week_of_stock(client, model):
    model.answers["read_message"] = read("I need money for bread.", requests=[{**BREAD, "quantity": 0, "amount": 0}])
    r = client.post(PHONE, json={"text": "Ndoda mari yechingwa."}).json()
    req = r["requests"][0]
    week = next(p for p in client.get("/api/shops/1/products").json() if p["name"] == "bread")["suggested"]
    assert (req["quantity"], req["amount_cents"]) == (week["quantity"], week["amount_cents"])
    assert req["quantity"] > 1 and set(req["inferred"]) == {"quantity", "amount", "payee"}
    assert any(m["text_en"] == f"I asked for {week['quantity']} loaves of bread, about a week of stock. "
               "Send the count if it differs." for m in r["messages"])


def test_a_count_comes_from_the_amount_when_the_message_gives_one(client, model):
    model.answers["read_message"] = read("Bread for $69.", requests=[{**BREAD, "quantity": 0, "amount": 69}])
    r = client.post(PHONE, json={"text": "Chingwa che $69"}).json()
    req = r["requests"][0]
    assert (req["quantity"], req["amount_cents"], set(req["inferred"])) == (80, 6900, {"quantity", "payee"})
    assert any(m["text_en"] == "US$69.00 buys about 80 loaves of bread at recent prices, so I asked for that. "
               "Send the count if it differs." for m in r["messages"])


def test_a_shared_total_is_kept_or_queried(client, model):
    sugar = {"kind": "stock", "item": "sugar", "quantity": 10, "unit": "2 kg packs", "amount": 0, "payee": ""}
    model.answers["read_message"] = {**read("We need 40 loaves of bread and 10 packs of sugar, $80 for all.",
                                            requests=[{**BREAD, "quantity": 40, "amount": 0}, sugar]),
                                     "stated_total": 80, "currency": "USD"}
    r = client.post(PHONE, json={"text": "Tinoda bread 40 loaves ne sugar 10 packs, zvese $80"}).json()
    bread, sugar_ = r["requests"]
    assert r["messages"][-1]["text_en"] == (
        f"You wrote US$80.00 for bread and sugar. Recent buys put them at "
        f"{flows.money(bread['amount_cents'])} for 40 loaves of bread and "
        f"{flows.money(sugar_['amount_cents'])} for 10 × 2 kg packs of sugar. Send each total if they differ.")


def test_an_amount_in_rand_is_named_back_in_us_dollars(client, model):
    model.answers["read_message"] = {**read("Eggs, 40 trays, R3300.", requests=[{**EGGS, "amount": 0}]),
                                     "stated_total": 3300, "currency": "ZAR"}
    r = client.post(PHONE, json={"text": "Mazai 40 trays R3300"}).json()
    assert r["messages"][-1]["text_en"] == (
        "You wrote R3,300.00. I asked Chipo in US dollars, at recent prices: US$180.00 for 40 trays of eggs. "
        "Send the total in US dollars if it differs.")


# The phone waits a few seconds at most

def test_the_answer_the_phone_waits_on_runs_quick():
    from musha.ai import QUICK
    assert {proof.ANSWER_TASK, proof.QUESTION_TASK, "read_message", "transcribe", "localize"} <= QUICK


class Hung:
    """A model call that keeps going after a cancel, the way a stuck SDK call can."""
    enabled = True

    def __init__(self, seconds):
        self.seconds = seconds

    async def json(self, task, parts, schema, *, system=""):
        try:
            await asyncio.sleep(self.seconds)
        except asyncio.CancelledError:
            await asyncio.sleep(self.seconds)
        return None


def test_a_bounded_call_returns_while_the_call_it_left_runs_on():
    from musha.ai import bounded

    async def run():
        loop = asyncio.get_running_loop()
        started = loop.time()
        out = await bounded(Hung(0.5).json("x", [], {}), 0.05, default="late")
        return out, loop.time() - started

    out, took = asyncio.run(run())
    assert out == "late" and took < 0.4


def test_reading_past_its_budget_falls_back_to_the_rule_reader(app, monkeypatch):
    async def late(aw, seconds, default=None):
        aw.close()  # never awaited: the budget ran out
        return default

    monkeypatch.setattr(bridge, "bounded", late)
    with app.state.sessions() as s:
        made = asyncio.run(bridge.hear(s, None, Hung(0), s.get(Shop, 1), text=f"{EXAMPLE}."))
        assert [(r.item, r.quantity, r.amount_cents) for r in made] == [("eggs", 40, 18000)]


def test_a_translation_marked_long_ago_counts_as_stopped(app):
    async def run():
        with app.state.sessions() as s:
            line = flows.say(s, 1, "A line for Farai.")
            s.commit()
            bridge._localizing[1] = (asyncio.Event(), bridge.time.monotonic() - bridge.LOCALIZE_STALE - 1)
            await bridge.localize_pending(s, Slow(), s.get(Shop, 1))
            return line

    line = asyncio.run(run())
    assert (line.language, line.text) == ("sn", "[sn] A line for Farai.")
    assert 1 not in bridge._localizing


def test_a_translation_past_its_budget_leaves_the_lines_english_and_frees_the_shop(app, monkeypatch):
    monkeypatch.setattr(bridge, "LOCALIZE_BUDGET", 0.05)

    async def run():
        with app.state.sessions() as s:
            line = flows.say(s, 1, "A line for Farai.")
            s.commit()
            await bridge.localize_pending(s, Hung(0.3), s.get(Shop, 1))
            return line

    line = asyncio.run(run())
    assert line.language == "en" and 1 not in bridge._localizing
    assert bridge._failed[(1, line.id)][0] == 1  # a failed try: a line that keeps failing rests


def test_the_phone_answers_before_a_slow_translation_finishes(app, client, monkeypatch):
    monkeypatch.setattr(bridge, "PHONE_WAIT", 0.05)

    class Slower(Slow):
        async def json(self, task, parts, schema, *, system=""):
            if task != "localize":
                return read("Good morning.", reply="Good morning, Farai.")
            await asyncio.sleep(2.5)
            return shona(parts)

    app.state.model = Slower()
    started = bridge.time.monotonic()
    r = client.post(PHONE, json={"text": "Mangwanani"}).json()
    assert bridge.time.monotonic() - started < 2.0  # the translation still runs
    last = r["messages"][-1]
    assert (last["text_en"], last["language"]) == ("Good morning, Farai.", "en")
