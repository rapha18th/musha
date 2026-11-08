"""The WhatsApp simulator (whatsapp-sim/sim.py) as Meta, against the app, in one process. The phone's
webhooks reach the app through its ASGI door, and the app's Graph API calls reach the simulator through
its own: the same bytes a local run sends over HTTP."""
import importlib.util
import io
import json
import re
import sys
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from musha import flows, proof
from musha import whatsapp as wa
from musha.config import Settings
from musha.models import Attachment, Message, Shop, WhatsAppMessage
from musha.routes import evidence as upload

from .conftest import ORDER_ID

SIM_FILE = Path(__file__).resolve().parents[2] / "whatsapp-sim" / "sim.py"
PAGE_JS = SIM_FILE.parent / "page" / "chat.js"
_spec = importlib.util.spec_from_file_location("whatsapp_sim", SIM_FILE)
meta = importlib.util.module_from_spec(_spec)
sys.modules["whatsapp_sim"] = meta
_spec.loader.exec_module(meta)

NUMBER_ID = "115550168742001"
BASE = "http://127.0.0.1:8090"  # the simulator's Graph base, as run.py sets it
FARAI = "+263771234567"
WORDS = "40 trays of eggs from Mbare wholesale, US$180.00"


@pytest.fixture
def settings(tmp_path):
    return Settings(paypal_client_id="cid", paypal_client_secret="secret", paypal_webhook_id="WH-1",
                    database_url=f"sqlite:///{tmp_path / 'musha.db'}", currency="USD", jobs_key="jobs",
                    jobs_every_minutes=0, whatsapp_token="sim-token", whatsapp_phone_number_id=NUMBER_ID,
                    whatsapp_app_secret="sim-secret", whatsapp_verify_token="sim-verify", whatsapp_graph_base=BASE)


def config(**changes) -> "meta.Config":
    values = dict(token="sim-token", number_id=NUMBER_ID, app_secret="sim-secret", verify_token="sim-verify",
                  phone=FARAI, webhook="http://127.0.0.1:8000/api/whatsapp/webhook", pace=0)
    return meta.Config(**{**values, **changes})


def phone_for(app, **changes) -> "meta.Sim":
    """A simulator whose webhooks reach this app."""
    return meta.Sim(config(**changes), http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app)))


@pytest.fixture
def line(app):
    """The app with its loop running, Farai's number proved, and the simulator on both sides of the line."""
    with app.state.sessions() as s:
        farai = s.get(Shop, 1).manager
        farai.phone = FARAI
        wa.prove(s, farai)
        s.commit()
    with TestClient(app) as client:
        sim = phone_for(app)
        app.state.whatsapp.cloud.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=sim.app))
        yield client, sim


def settle(client, sim):
    """Until Musha has sent every line and the simulator has posted every status."""
    async def quiet():
        outbox = client.app.state.whatsapp
        while True:
            await sim.idle()
            await outbox.idle()
            if not sim._tasks and not outbox._running:
                return

    client.portal.call(quiet)


def lines(app, shop_id=1, **where):
    with app.state.sessions() as s:
        q = select(Message).where(Message.shop_id == shop_id, Message.thread == "manager")
        for name, value in where.items():
            q = q.where(getattr(Message, name) == value)
        return list(s.scalars(q.order_by(Message.id)))


def rows(app, **where):
    with app.state.sessions() as s:
        q = select(WhatsAppMessage)
        for name, value in where.items():
            q = q.where(getattr(WhatsAppMessage, name) == value)
        return list(s.scalars(q.order_by(WhatsAppMessage.id)))


def musha_on_the_phone(sim):
    return [x for x in sim.lines if x.side == "them"]


def webhooks(sim):
    return [w for w in sim.wire if w["way"] == "webhook" and w["method"] == "POST"]


def fund(client, sim):
    """A request Chipo approves: PayPal holds the money, and it waits on Farai's proof."""
    r = client.post("/api/shops/1/requests", json={"item": "eggs", "quantity": 40, "unit": "trays",
                                                  "amount": "180.00", "payee": "Mbare wholesale"})
    rid = r.json()["id"]
    assert client.post(f"/api/requests/{rid}/order").json() == {"order_id": ORDER_ID}
    assert client.post(f"/api/requests/{rid}/authorize").json()["status"] == "held"
    settle(client, sim)
    return rid


def jpeg(color=(200, 120, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, "JPEG")
    return buf.getvalue()


def test_meta_checks_the_callback_url_with_the_verify_token(app, line):
    client, sim = line
    assert client.portal.call(sim.verify) == (True, 200)
    assert client.portal.call(phone_for(app, verify_token="a-guess").verify) == (False, 403)


def test_farais_text_reaches_her_thread_and_musha_answers_on_the_phone(app, line, model):
    client, sim = line
    sent = client.portal.call(sim.send_text, WORDS)
    settle(client, sim)
    assert sent.state == "read"  # Musha took it and read it: blue ticks
    farai = lines(app, sender="manager")[-1]
    assert (farai.text, farai.request_id is not None) == (WORDS, True)

    answers = [m for m in lines(app) if m.id > farai.id]
    assert answers and [x.text for x in musha_on_the_phone(sim)] == [m.text for m in answers]
    assert any(x.text.startswith("Sent to Chipo") for x in musha_on_the_phone(sim))
    assert {r.state for r in rows(app, direction="out")} == {"delivered"}  # Meta's statuses came back

    assert client.portal.call(sim.seen) == len(answers)
    settle(client, sim)
    assert {r.state for r in rows(app, direction="out")} == {"read"}
    assert {x.state for x in musha_on_the_phone(sim)} == {"read"}

    hook = json.loads(webhooks(sim)[0]["request"])  # the webhook as Meta sends it
    change = hook["entry"][0]["changes"][0]
    assert (hook["object"], change["field"]) == ("whatsapp_business_account", "messages")
    assert change["value"]["metadata"] == {"display_phone_number": "15550168742", "phone_number_id": NUMBER_ID}
    assert change["value"]["messages"][0] | {"timestamp": "0"} == {
        "from": FARAI[1:], "id": sent.id, "timestamp": "0", "type": "text", "text": {"body": WORDS}}
    assert sent.id.startswith("wamid.")


def test_a_photo_from_the_phone_downloads_and_takes_the_proof_path(app, line, monkeypatch):
    client, sim = line
    rid = fund(client, sim)
    approval = musha_on_the_phone(sim)[-1]  # the window is shut, so the approval came as the template
    assert approval.kind == "template"
    assert approval.text.startswith("Musha has an update on your shop: ")
    assert approval.text.endswith(" Reply here to answer Musha.")

    took, proved = [], []
    receive = upload.receive

    async def spy(session, pp, m, req, **kw):
        took.append((req.id, kw["kind"], kw["content_type"], kw["data"]))
        return await receive(session, pp, m, req, **kw)

    async def prove(session, pp, m, req):
        proved.append(req.id)

    monkeypatch.setattr(upload, "receive", spy)
    monkeypatch.setattr(proof, "prove", prove)
    photo = jpeg()
    sent = client.portal.call(lambda: sim.send_media(photo, "image/jpeg", reply_to=approval.id))
    settle(client, sim)

    assert sent.state == "read"  # Musha read the photo: blue ticks
    assert took == [(rid, "receipt", "image/jpeg", photo)]
    with app.state.sessions() as s:
        kept = s.scalar(select(Attachment).where(Attachment.request_id == rid, Attachment.kind == "receipt"))
        assert kept.data == photo
    graph = [(w["method"], w["path"].split("?")[0], w["status"]) for w in sim.wire if w["way"] == "graph"]
    assert ("GET", f"/v27.0/{sent.media}", 200) in graph  # the media URL, then the bytes, both with the token
    assert ("GET", "/whatsapp_business/attachments/", 200) in graph


def test_a_voice_note_from_the_phone_is_heard_like_one_from_the_dashboard(app, line, model):
    client, sim = line
    note = b"OggS\x00\x02 a voice note"
    model.answers["transcribe"] = {"text": WORDS, "language": "en", "english": WORDS}
    client.portal.call(lambda: sim.send_media(note, "audio/ogg; codecs=opus", voice=True, seconds=4))
    settle(client, sim)
    farai = lines(app, sender="manager")[-1]
    assert farai.text == WORDS and farai.request_id is not None
    with app.state.sessions() as s:
        voice = s.get(Attachment, farai.attachment_id)
        assert (voice.kind, voice.content_type, voice.data) == ("voice", "audio/ogg", note)
    audio = json.loads(webhooks(sim)[0]["request"])["entry"][0]["changes"][0]["value"]["messages"][0]["audio"]
    assert (audio["mime_type"], audio["voice"]) == ("audio/ogg; codecs=opus", True)


def test_a_webhook_signed_with_another_secret_is_refused(app, line):
    client, sim = line
    stranger = phone_for(app, app_secret="another-secret")
    before = len(lines(app))
    sent = client.portal.call(stranger.send_text, WORDS)
    settle(client, sim)
    assert (sent.state, sent.note) == ("failed", "Musha's webhook answered 401.")
    assert len(lines(app)) == before and rows(app) == [] and musha_on_the_phone(stranger) == []


def test_a_text_past_the_window_fails_as_meta_fails_it_and_goes_again_as_the_template(app, line):
    client, sim = line
    fund(client, sim)
    with app.state.sessions() as s:  # Musha's record says Farai wrote an hour ago; the phone has no such message
        s.add(WhatsAppMessage(shop_id=1, direction="in", wamid="wamid.earlier", phone=FARAI, kind="text",
                              state="done", at=flows.now() - timedelta(minutes=60)))
        s.commit()
    shown = len(musha_on_the_phone(sim))
    line_ = client.post("/api/shops/1/owner/to-manager", json={"text": "Send the receipt."}).json()
    settle(client, sim)
    failed = [w for w in webhooks(sim) if w["what"] == "status failed 131047"]
    assert len(failed) == 1
    assert [(r.kind, r.state) for r in rows(app, line_id=line_["id"])] == [("template", "delivered")]
    assert [x.text for x in musha_on_the_phone(sim)[shown:]] == [
        "Musha has an update on your shop: Chipo: Send the receipt. Reply here to answer Musha."]


def test_the_graph_api_holds_to_metas_rules(app, line):
    client, sim = line

    async def calls():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=sim.app), base_url=BASE) as http:
            good = {"Authorization": "Bearer sim-token"}
            text = {"messaging_product": "whatsapp", "to": FARAI, "type": "text", "text": {"body": "Hello"}}
            return [
                await http.post(f"/v27.0/{NUMBER_ID}/messages", json=text, headers={"Authorization": "Bearer other"}),
                await http.post("/v27.0/999/messages", json=text, headers=good),
                await http.post(f"/v27.0/{NUMBER_ID}/messages", json={**text, "to": "+15550001111"}, headers=good),
                await http.post(f"/v27.0/{NUMBER_ID}/messages", json={**text, "type": "template", "template": {
                    "name": "another", "language": {"code": "en"}}}, headers=good),
                await http.get(f"/v27.0/{NUMBER_ID}", params={"fields": "display_phone_number"}, headers=good),
            ]

    token, number, stranger, template, shown = client.portal.call(calls)
    assert [r.json()["error"]["code"] for r in (token, number, stranger, template)] == [190, 100, 131030, 132001]
    assert shown.json() == {"display_phone_number": "+1 555-016-8742", "id": NUMBER_ID}
    assert sim.lines == []


def sample_captions() -> dict[str, str]:
    """The caption the phone's page gives each sample photo (page/chat.js, SAMPLES)."""
    found = re.search(r"const SAMPLES = (\{.*?\});", PAGE_JS.read_text(encoding="utf-8"))
    return {what: caption for what, (_, caption) in json.loads(re.sub(r"(\w+):", r'"\1":', found[1])).items()}


def test_the_sample_goods_photo_is_filed_as_the_goods_before_the_receipt(app, line, monkeypatch):
    client, sim = line
    rid = fund(client, sim)
    took = []
    receive = upload.receive

    async def spy(session, pp, m, req, **kw):
        took.append(kw["kind"])
        return await receive(session, pp, m, req, **kw)

    async def prove(session, pp, m, req):
        pass

    monkeypatch.setattr(upload, "receive", spy)
    monkeypatch.setattr(proof, "prove", prove)
    captions = sample_captions()
    for what in ("goods", "receipt"):  # the goods first, before any receipt is in
        client.portal.call(lambda: sim.send_media(sim.sample(what), "image/jpeg", caption=captions[what]))
        settle(client, sim)
    assert took == ["goods", "receipt"]
    with app.state.sessions() as s:
        kinds = s.scalars(select(Attachment.kind).where(Attachment.request_id == rid).order_by(Attachment.id))
        assert list(kinds) == ["goods", "receipt"]


def test_musha_types_for_25_seconds_at_most_when_no_reply_comes(app, line):
    client, sim = line
    fund(client, sim)
    shown = len(musha_on_the_phone(sim))
    # a goods photo on a held request: Musha reads it and keeps its reading, and says nothing yet
    sent = client.portal.call(lambda: sim.send_media(sim.sample("goods"), "image/jpeg", caption="Goods"))
    settle(client, sim)
    assert sent.state == "read" and len(musha_on_the_phone(sim)) == shown
    assert sim.typing and sim.view()["typing"]
    sim.typing_until -= meta.TYPING  # 25 seconds pass: the indicator goes off, as Meta's does
    assert not sim.typing and not sim.view()["typing"]
    # the page's poll hears it go off at once, before its own wait runs out
    sim.typing_until = time.time() + 0.2
    began = time.monotonic()
    client.portal.call(sim.changed, sim.version)
    assert time.monotonic() - began < meta.WAIT / 4 and not sim.view()["typing"]


def test_the_phone_takes_calls_from_its_own_page_alone(app, line):
    client, sim = line
    words = json.dumps({"text": WORDS})
    away = {"Origin": "https://elsewhere.example", "Sec-Fetch-Site": "cross-site"}
    plain = {"Content-Type": "text/plain;charset=UTF-8"}  # a type another site's page sends with no preflight
    own = {"Origin": BASE, "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"}

    async def calls():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=sim.app), base_url=BASE) as http:
            return [
                await http.post("/sim/text", content=words, headers={**plain, **away}),
                await http.post("/sim/text", content=words, headers={**own, "Origin": away["Origin"]}),
                await http.post("/sim/text", content=words, headers={**own, "Sec-Fetch-Site": "cross-site"}),
                await http.post("/sim/text", content=words, headers=plain),
                await http.post("/sim/media", content=jpeg(), headers={"Content-Type": "image/jpeg", **away}),
                await http.post("/sim/seen", content="{}", headers={**plain, **away}),
                await http.post("/sim/clear", headers=away),
                await http.post("/sim/verify", headers=away),
                await http.post("/sim/verify"),  # no page at all, as run.py makes the check
                await http.post("/sim/text", content=words, headers=own),
            ]

    *refused, check, sent = client.portal.call(calls)
    settle(client, sim)
    assert [r.status_code for r in refused] == [403, 403, 403, 415, 403, 403, 403, 403]
    assert check.json() == {"ok": True, "status": 200}
    assert sent.status_code == 200 and [x.text for x in sim.lines if x.side == "me"] == [WORDS]
    assert [json.loads(w["request"])["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"]
            for w in webhooks(sim) if w["what"].endswith("from the phone")] == [WORDS]
