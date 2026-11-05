import hashlib
import hmac
import io
import json
import time
from datetime import timedelta

import anyio.to_thread
import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

from musha import accounts, bridge, flows, proof
from musha import seed as seeding
from musha import whatsapp as wa
from musha.api import create_app
from musha.config import GRAPH_VERSION, Settings
from musha.models import Attachment, Message, Shop, SpendRequest, WhatsAppMessage
from musha.paypal import PayPal
from musha.routes import evidence as upload
from musha.seed import reset

from .conftest import ORDER_ID, signing_up

SECRET = "app-secret"
NUMBER_ID = "106540352242922"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
FARAI = "+263771234567"
FROM = "263771234567"  # WhatsApp writes the number without its plus
HOOK = "/api/whatsapp/webhook"
MEDIA = "https://lookaside.fbsbx.com/whatsapp_business/attachments/1"
WORDS = "40 trays of eggs from Mbare wholesale, US$180.00"


@pytest.fixture(autouse=True)
def fresh_bridge():
    bridge._failed.clear()
    bridge._localizing.clear()
    yield
    bridge._failed.clear()
    bridge._localizing.clear()


@pytest.fixture
def settings(tmp_path):
    return Settings(paypal_client_id="cid", paypal_client_secret="secret", paypal_webhook_id="WH-1",
                    database_url=f"sqlite:///{tmp_path / 'musha.db'}", currency="USD", jobs_key="jobs",
                    jobs_every_minutes=0, whatsapp_token="wa-token", whatsapp_phone_number_id=NUMBER_ID,
                    whatsapp_app_secret=SECRET, whatsapp_verify_token="verify-me")


@pytest.fixture
def sent(sandbox):
    """Meta's Graph API: each send is kept and answers with a message id of its own."""
    out = []

    def send(request: httpx.Request):
        assert request.headers["Authorization"] == "Bearer wa-token"
        body = json.loads(request.content)
        out.append(body)
        return httpx.Response(200, json={"messaging_product": "whatsapp",
                                         "contacts": [{"input": body["to"], "wa_id": body["to"][1:]}],
                                         "messages": [{"id": f"wamid.out{len(out)}"}]})

    sandbox.post(f"{GRAPH}/{NUMBER_ID}/messages").mock(side_effect=send)
    return out


@pytest.fixture
def client(app, sent):
    """The app with its loop running, as on the server, so the outbox sends after each reply.
    Farai's WhatsApp number is FARAI, and she has proved it."""
    with app.state.sessions() as s:
        farai = s.get(Shop, 1).manager
        farai.phone = "+263 77 123 4567"
        wa.prove(s, farai)
        s.commit()
    with TestClient(app) as c:
        yield c


def settle(client):
    client.portal.call(client.app.state.whatsapp.idle)


def signature(raw: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def post(client, payload, *, secret: str = SECRET):
    raw = json.dumps(payload).encode()
    r = client.post(HOOK, content=raw, headers={"Content-Type": "application/json",
                                               "X-Hub-Signature-256": signature(raw, secret)})
    settle(client)
    return r


def hook(*messages, statuses=(), number_id=NUMBER_ID):
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA-1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550783881", "phone_number_id": number_id},
        "contacts": [{"profile": {"name": "Farai"}, "wa_id": FROM}],
        "messages": list(messages), "statuses": list(statuses)}}]}]}


def text(body, wamid="wamid.in1", sender=FROM):
    return {"from": sender, "id": wamid, "timestamp": str(int(time.time())), "type": "text", "text": {"body": body}}


def media(kind, wamid, caption="", mime="image/jpeg"):
    m = {"from": FROM, "id": wamid, "timestamp": str(int(time.time())), "type": kind,
         kind: {"id": "MEDIA-1", "mime_type": mime, "sha256": "abc"}}
    if caption:
        m[kind]["caption"] = caption
    return m


def reaction(wamid="wamid.react"):
    """Farai reacts to a line: Musha says nothing, and the window stays as it was."""
    return {"from": FROM, "id": wamid, "timestamp": str(int(time.time())), "type": "reaction",
            "reaction": {"message_id": "wamid.out1", "emoji": "\U0001f44d"}}


def wrote(app, minutes=60):
    """Farai wrote to the business number `minutes` ago: inside 24 hours, her window is open."""
    with app.state.sessions() as s:
        s.add(WhatsAppMessage(shop_id=1, direction="in", wamid=f"wamid.earlier{minutes}", phone=FARAI,
                              kind="text", state="done", at=flows.now() - timedelta(minutes=minutes)))
        s.commit()


def serve(sandbox, data: bytes, mime: str):
    sandbox.get(f"{GRAPH}/MEDIA-1").respond(200, json={
        "messaging_product": "whatsapp", "url": MEDIA, "mime_type": mime, "sha256": "abc",
        "file_size": len(data), "id": "MEDIA-1"})
    sandbox.get(MEDIA).respond(200, content=data, headers={"Content-Type": mime})


def jpeg(color=(200, 120, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, "JPEG")
    return buf.getvalue()


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


def fund(client):
    """A request Chipo approves: PayPal holds the money, and it waits on Farai's proof."""
    r = client.post("/api/shops/1/requests", json={"item": "eggs", "quantity": 40, "unit": "trays",
                                                  "amount": "180.00", "payee": "Mbare wholesale"})
    rid = r.json()["id"]
    assert client.post(f"/api/requests/{rid}/order").json() == {"order_id": ORDER_ID}
    assert client.post(f"/api/requests/{rid}/authorize").json()["status"] == "held"
    settle(client)
    return rid


def shona(parts):
    return [f"[sn] {line}" for line in json.loads(parts[-1])]


# The webhook

def test_the_handshake_returns_the_challenge_for_the_verify_token(client):
    ok = client.get(HOOK, params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                  "hub.challenge": "1158201444"})
    assert (ok.status_code, ok.text, ok.headers["content-type"].split(";")[0]) == (200, "1158201444", "text/plain")
    wrong = client.get(HOOK, params={"hub.mode": "subscribe", "hub.verify_token": "guess",
                                     "hub.challenge": "1158201444"})
    assert wrong.status_code == 403 and "1158201444" not in wrong.text
    assert client.get("/api/config").json()["whatsapp"] is True  # the line under Farai's chat says it is live


def test_a_webhook_signed_with_another_secret_is_refused_and_heard_nowhere(app, client, sent):
    before = len(lines(app))
    assert post(client, hook(text(WORDS)), secret="another-secret").status_code == 401
    raw = json.dumps(hook(text(WORDS))).encode()
    assert client.post(HOOK, content=raw, headers={"Content-Type": "application/json"}).status_code == 401
    assert client.post(HOOK, content=raw + b" ", headers={"Content-Type": "application/json",
                                                          "X-Hub-Signature-256": signature(raw)}).status_code == 401
    assert len(lines(app)) == before and rows(app) == [] and sent == []


def test_a_signed_webhook_is_answered_and_a_reaction_gets_no_reply(app, client, sent):
    before = len(lines(app))
    r = post(client, hook(reaction()))
    assert (r.status_code, r.json()) == (200, {"ok": True})
    assert len(lines(app)) == before and sent == []
    assert [(x.kind, x.state) for x in rows(app, direction="in")] == [("reaction", "done")]


def test_farais_text_lands_in_her_thread_and_musha_answers_on_her_whatsapp(app, client, model, sent):
    model.answers["localize"] = shona
    assert post(client, hook(text(WORDS))).status_code == 200
    farai = lines(app, sender="manager")[-1]
    assert (farai.text, farai.request_id is not None) == (WORDS, True)
    with app.state.sessions() as s:
        req = s.get(SpendRequest, farai.request_id)
        assert (req.item, req.quantity, req.amount_cents, req.origin) == ("eggs", 40, 18000, "manager")
    answers = [m for m in lines(app) if m.id > farai.id]
    assert answers and all(m.sender == "musha" and m.language == "sn" for m in answers)
    assert [(b["to"], b["type"], b["text"]["body"]) for b in sent] == [(FARAI, "text", m.text) for m in answers]
    assert any(b["text"]["body"].startswith("[sn] Sent to Chipo") for b in sent)
    assert [(r.direction, r.state) for r in rows(app, line_id=answers[0].id)] == [("out", "sent")]


def test_meta_sending_a_message_again_is_heard_once_and_answered_once(app, client, sent):
    def requests():
        with app.state.sessions() as s:
            return s.scalar(select(func.count(SpendRequest.id)))

    before = requests()
    post(client, hook(text(WORDS)))
    replies = len(sent)
    assert replies and requests() == before + 1
    assert post(client, hook(text(WORDS))).status_code == 200
    assert len(lines(app, sender="manager", text=WORDS)) == 1 and len(sent) == replies
    assert requests() == before + 1
    assert [r.state for r in rows(app, direction="in")] == ["done"]


def test_a_number_no_shop_knows_is_ignored(app, client, sent):
    before = {shop: len(lines(app, shop)) for shop in (1, 2)}
    assert post(client, hook(text(WORDS, sender="15550001111"))).status_code == 200
    assert {shop: len(lines(app, shop)) for shop in (1, 2)} == before
    assert rows(app) == [] and sent == []


def test_a_change_for_another_business_number_is_left_alone(app, client, sent):
    post(client, hook(text(WORDS), number_id="999"))
    assert rows(app) == [] and not lines(app, sender="manager", text=WORDS)


def test_a_photo_downloads_and_takes_the_dashboards_proof_path(app, client, sandbox, monkeypatch):
    rid = fund(client)
    took, proved = [], []
    receive = upload.receive

    async def spy(session, pp, m, req, **kw):
        took.append((req.id, kw["kind"], kw["content_type"], kw["data"]))
        return await receive(session, pp, m, req, **kw)

    async def prove(session, pp, m, req):
        proved.append(req.id)

    monkeypatch.setattr(upload, "receive", spy)
    monkeypatch.setattr(proof, "prove", prove)
    receipt, goods = jpeg(), jpeg((30, 160, 90))
    serve(sandbox, receipt, "image/jpeg")
    assert post(client, hook(media("image", "wamid.photo1"))).status_code == 200
    serve(sandbox, goods, "image/jpeg")
    post(client, hook(media("image", "wamid.photo2", caption="The trays came in")))
    assert [(t[0], t[1], t[2]) for t in took] == [(rid, "receipt", "image/jpeg"), (rid, "goods", "image/jpeg")]
    assert took[0][3] == receipt and proved == [rid]
    with app.state.sessions() as s:
        kept = list(s.scalars(select(Attachment).where(Attachment.request_id == rid).order_by(Attachment.id)))
        assert [(a.kind, a.data) for a in kept] == [("receipt", receipt), ("goods", goods)]
    shown = [(m.text, m.attachment_id) for m in lines(app, sender="manager")[-2:]]
    assert shown == [("Receipt", kept[0].id), ("The trays came in", kept[1].id)]
    calls = [c.request for c in sandbox.calls if c.request.url.host == "lookaside.fbsbx.com"]
    assert len(calls) == 2 and all(c.headers["Authorization"] == "Bearer wa-token" for c in calls)


def test_a_photo_with_nothing_waiting_for_proof_gets_a_plain_answer(app, client, sandbox, sent):
    post(client, hook(media("image", "wamid.photo1")))
    assert lines(app)[-1].text == "Proof goes with an approved request. Ask for money first."
    assert sent[-1]["text"]["body"] == "Proof goes with an approved request. Ask for money first."
    assert not [c for c in sandbox.calls if c.request.url.host == "lookaside.fbsbx.com"]


def test_a_voice_note_is_transcribed_like_one_from_the_dashboard(app, client, sandbox, model):
    note = b"OggS\x00\x02 a voice note"
    serve(sandbox, note, "audio/ogg; codecs=opus")
    model.answers["transcribe"] = {"text": WORDS, "language": "en", "english": WORDS}
    post(client, hook(media("audio", "wamid.voice1", mime="audio/ogg; codecs=opus")))
    farai = lines(app, sender="manager")[-1]
    assert farai.text == WORDS and farai.request_id is not None
    with app.state.sessions() as s:
        voice = s.get(Attachment, farai.attachment_id)
        assert (voice.kind, voice.content_type, voice.data) == ("voice", "audio/ogg", note)


# The manager's number, proved

SECOND = "+263779990000"  # the manager of a shop made at sign-up


def made_shop(app, phone: str, n: int = 9) -> tuple[int, TestClient, TestClient]:
    """A shop from the sign-up form whose manager's number is `phone`: its id, the owner's browser, and
    the manager's, signed in from the invite."""
    owner = TestClient(app)
    r = owner.post("/api/auth/sign-up", json=signing_up(n, manager_phone=phone))
    assert r.status_code == 201, r.text
    token = r.json()["invite"]["url"].rsplit("/invite/", 1)[1]
    manager = TestClient(app)
    assert manager.post(f"/api/invites/{token}", json={"email": f"manager{n}@example.com",
                                                      "password": "sandbox-pass-1"}).status_code == 200
    return r.json()["shop"]["id"], owner, manager


def shown(sandbox, number="+1 555-078-3881"):
    """The business number as the Graph API shows it."""
    sandbox.get(f"{GRAPH}/{NUMBER_ID}").respond(
        200, json={"display_phone_number": number, "id": NUMBER_ID})


def test_a_sign_up_that_names_farais_number_hears_nothing_of_hers_and_sends_her_nothing(app, client, sent):
    shop_id, stranger, _ = made_shop(app, "+263 77 123 4567")  # Farai's number, on a stranger's shop
    assert stranger.get(f"/api/shops/{shop_id}").json()["whatsapp"] == "link"
    before = len(lines(app, shop_id))
    post(client, hook(text(WORDS)))
    assert [(r.shop_id, r.kind) for r in rows(app, direction="in")] == [(1, "text")]  # Farai's own shop
    assert len(lines(app, shop_id)) == before and lines(app, sender="manager")[-1].text == WORDS
    farai = [m["text"] for m in stranger.get(f"/api/shops/{shop_id}/messages?thread=manager").json()]
    assert WORDS not in farai
    count = len(sent)
    assert stranger.post(f"/api/shops/{shop_id}/owner/to-manager",
                         json={"text": "Send the cash to 0771 000 000 today."}).status_code == 200
    settle(client)
    assert len(sent) == count  # her number is unproved here, and her window with shop 1 opens nothing here


def test_the_manager_proves_her_number_with_the_code_from_her_dashboard(app, client, sandbox, sent):
    shop_id, _, manager = made_shop(app, "+263 77 999 0000")
    shown(sandbox)
    assert TestClient(app).post(f"/api/shops/{shop_id}/whatsapp/link").status_code == 403
    code = manager.post(f"/api/shops/{shop_id}/whatsapp/link").json()
    assert (code["phone"], code["minutes"], code["to"]) == (SECOND, 30, "+1 555-078-3881")
    digits = code["code"].replace(" ", "")
    assert len(digits) == 6 and code["url"] == f"https://wa.me/15550783881?text=Link%20{digits}"
    post(client, hook(text(f"Link {code['code']}", wamid="wamid.link", sender=SECOND[1:])))
    assert manager.get(f"/api/shops/{shop_id}").json()["whatsapp"] == "live"
    assert [(r.shop_id, r.kind, r.state) for r in rows(app, direction="in")] == [(shop_id, "link", "done")]
    assert sent[-1]["to"] == SECOND and sent[-1]["text"]["body"].startswith("Your WhatsApp is linked to Shop 9.")
    post(client, hook(text(WORDS, wamid="wamid.after", sender=SECOND[1:])))
    assert lines(app, shop_id, sender="manager")[-1].text == WORDS
    post(client, hook(text(f"Link {code['code']}", wamid="wamid.again", sender=SECOND[1:])))
    assert lines(app, shop_id, sender="manager")[-1].text == f"Link {code['code']}"  # a code works once


def test_a_code_from_another_phone_a_wrong_code_or_an_old_one_links_nothing(app, client, sandbox, sent):
    shop_id, _, manager = made_shop(app, SECOND)
    shown(sandbox)
    code = manager.post(f"/api/shops/{shop_id}/whatsapp/link").json()["code"]
    wrong = f"{(int(code.replace(' ', '')) + 1) % 10 ** 6:06d}"
    post(client, hook(text(f"Link {code}", wamid="wamid.other", sender="15550001111")))
    post(client, hook(text(f"Link {wrong}", wamid="wamid.wrong", sender=SECOND[1:])))
    with app.state.sessions() as s:
        s.get(Shop, shop_id).manager.link_code_expires_at = flows.now() - timedelta(minutes=1)
        s.commit()
    post(client, hook(text(f"Link {code}", wamid="wamid.late", sender=SECOND[1:])))
    assert rows(app, direction="in") == [] and manager.get(f"/api/shops/{shop_id}").json()["whatsapp"] == "link"


def test_a_number_proved_in_a_new_shop_moves_there_from_the_old_one(app, client, sandbox, sent):
    shop_id, _, manager = made_shop(app, "+263 77 123 4567")  # Farai runs a second shop too
    shown(sandbox)
    code = manager.post(f"/api/shops/{shop_id}/whatsapp/link").json()["code"]
    post(client, hook(text(f"Link {code}", wamid="wamid.move")))
    assert manager.get(f"/api/shops/{shop_id}").json()["whatsapp"] == "live"
    assert client.get("/api/shops/1").json()["whatsapp"] == "off"  # a seeded shop: its number is the operator's
    post(client, hook(text(WORDS, wamid="wamid.next")))
    assert lines(app, shop_id, sender="manager")[-1].text == WORDS
    assert not lines(app, 1, sender="manager", text=WORDS)


def test_the_manager_corrects_her_number_and_proves_the_new_one(app, client, sandbox, sent):
    shop_id, _, manager = made_shop(app, "+263 77 000 0001")  # the owner wrote it wrong
    shown(sandbox)
    assert manager.post(f"/api/shops/{shop_id}/whatsapp/link", json={"phone": "0779990000"}).status_code == 422
    code = manager.post(f"/api/shops/{shop_id}/whatsapp/link", json={"phone": "+263 77 999 0000"}).json()
    assert code["phone"] == SECOND
    post(client, hook(text(code["code"], wamid="wamid.fixed", sender=SECOND[1:])))
    assert manager.get(f"/api/shops/{shop_id}").json()["whatsapp"] == "live"


def test_the_sandbox_shops_number_comes_from_the_operator_alone(app, client, sandbox):
    farai = TestClient(app)
    with app.state.sessions() as s:  # the start seeded the sandbox shop without MUSHA_MANAGER_PHONE
        shop = seeding.sandbox_shop(s)
        sandbox_id, token = shop.id, accounts.open_session(s, shop.manager)
        s.commit()
    farai.cookies.set(accounts.COOKIE, token)
    view = farai.get(f"/api/shops/{sandbox_id}").json()
    assert (view["sandbox"], view["whatsapp"]) == (True, "off")  # the line under the chat claims no WhatsApp
    assert farai.post(f"/api/shops/{sandbox_id}/whatsapp/link", json={"phone": SECOND}).status_code == 409
    assert client.get("/api/shops/1").json()["whatsapp"] == "live"


def test_a_window_opens_for_the_shop_the_manager_wrote_to_alone(app, client):
    shop_id, _, _ = made_shop(app, "+263 77 123 4567")
    wrote(app)  # Farai wrote to shop 1 an hour ago
    with app.state.sessions() as s:
        assert wa.window_open(s, s.get(Shop, 1), FARAI)
        assert not wa.window_open(s, s.get(Shop, shop_id), FARAI)


# Musha's lines out

def test_a_line_inside_the_window_goes_out_as_text_with_the_owners_name(app, client, sent):
    wrote(app)
    assert sent == []
    line = client.post("/api/shops/1/owner/to-manager", json={"text": "Well done this week."}).json()
    settle(client)
    assert [(b["to"], b["type"], b["text"]["body"]) for b in sent] == [(FARAI, "text", "Chipo: Well done this week.")]
    assert [(r.kind, r.state, r.wamid) for r in rows(app, line_id=line["id"])] == [("text", "sent", "wamid.out1")]


def test_past_the_window_with_nothing_held_a_line_waits_in_the_dashboard(app, client, sent):
    wrote(app, minutes=25 * 60)
    post(client, hook(reaction()))
    line = client.post("/api/shops/1/owner/to-manager", json={"text": "Well done this week."}).json()
    settle(client)
    assert sent == []
    assert [r.state for r in rows(app, line_id=line["id"])] == ["waiting"]


def test_past_the_window_a_line_goes_as_the_template_while_paypal_holds_money_for_farai(app, client, sent):
    fund(client)
    assert sent and all(b["type"] == "template" for b in sent)  # the approval, once PayPal holds the money
    line = client.post("/api/shops/1/owner/to-manager", json={"text": "Send the receipt\nwhen you can."}).json()
    settle(client)
    assert sent[-1]["to"] == FARAI and sent[-1]["template"] == {
        "name": "musha_update", "language": {"code": "en"},
        "components": [{"type": "body", "parameters": [{"type": "text", "text": "Chipo: Send the receipt when you can."}]}]}
    assert [(r.kind, r.state) for r in rows(app, line_id=line["id"])] == [("template", "sent")]


def test_a_text_meta_refuses_for_the_window_goes_once_more_as_the_template(app, client, sent):
    wrote(app)
    fund(client)
    line = client.post("/api/shops/1/owner/to-manager", json={"text": "Send the receipt."}).json()
    settle(client)
    first = rows(app, line_id=line["id"])[0].wamid
    refused = {"id": first, "status": "failed", "timestamp": str(int(time.time())), "recipient_id": FROM,
               "errors": [{"code": 131047, "title": "Re-engagement message"}]}
    count = len(sent)
    post(client, hook(statuses=[refused]))
    post(client, hook(statuses=[refused]))
    assert len(sent) == count + 1 and sent[-1]["type"] == "template"
    assert [(r.kind, r.state) for r in rows(app, line_id=line["id"])] == [("template", "sent")]


def test_statuses_move_a_line_forward_only(app, client, sent):
    wrote(app)
    line = client.post("/api/shops/1/owner/to-manager", json={"text": "Thank you."}).json()
    settle(client)
    wamid = rows(app, line_id=line["id"])[0].wamid
    for state in ("read", "delivered"):
        post(client, hook(statuses=[{"id": wamid, "status": state, "timestamp": "1", "recipient_id": FROM}]))
    assert rows(app, line_id=line["id"])[0].state == "read"


def test_a_line_goes_out_once_however_often_it_is_woken(app, client, sent):
    wrote(app)
    line = client.post("/api/shops/1/owner/to-manager", json={"text": "Thank you."}).json()
    settle(client)
    outbox = app.state.whatsapp
    client.portal.call(lambda: _wake(outbox, line["id"]))
    settle(client)
    assert len(sent) == 1

    async def twice():
        with app.state.sessions() as s:
            shop, m = s.get(Shop, 1), s.get(Message, line["id"])
            return await outbox.send_line(s, shop, FARAI, m)

    assert client.portal.call(twice) is None and len(sent) == 1


async def _wake(outbox, line_id):
    outbox.wake([(1, line_id), (1, line_id)])


def test_a_line_committed_on_a_worker_thread_goes_out_from_the_loop(app, client, sent):
    wrote(app)

    def write():  # a sync route runs on a worker thread
        with app.state.sessions() as s:
            flows.say(s, 1, "Count the bread tonight.")
            s.commit()

    async def on_a_thread():
        await anyio.to_thread.run_sync(write)

    client.portal.call(on_a_thread)
    settle(client)
    assert [b["text"]["body"] for b in sent] == ["Count the bread tonight."]


def test_a_line_rolled_back_never_goes_out(app, client, sent):
    wrote(app)

    async def write():
        with app.state.sessions() as s:
            flows.say(s, 1, "A line that never was.")
            s.rollback()
            flows.say(s, 1, "The line that stands.")
            flows.say(s, 1, "Farai's own words.", sender="manager")
            flows.say(s, 1, "A line for Chipo alone.", thread="owner")
            s.commit()

    client.portal.call(write)
    settle(client)
    assert [b["text"]["body"] for b in sent] == ["The line that stands."]


def test_a_send_that_may_have_reached_meta_is_never_sent_again(app, client, sandbox, sent):
    wrote(app)
    sandbox.post(f"{GRAPH}/{NUMBER_ID}/messages").mock(side_effect=httpx.ReadTimeout("slow"))
    line = client.post("/api/shops/1/owner/to-manager", json={"text": "Thank you."}).json()
    settle(client)
    posts = [c for c in sandbox.calls if c.request.url.path.endswith("/messages")]
    assert len(posts) == 1
    assert [r.state for r in rows(app, line_id=line["id"])] == ["failed"]


def test_meta_batches_past_the_usual_body_limit_are_read(app, client):
    padding = [{"id": f"wamid.other{i}", "status": "delivered", "timestamp": "1", "recipient_id": "1" * 12}
               for i in range(1500)]
    payload = hook(reaction(), statuses=padding)
    assert len(json.dumps(payload)) > 64_000
    assert post(client, payload).status_code == 200
    assert [r.kind for r in rows(app, direction="in")] == ["reaction"]


def test_nothing_goes_to_whatsapp_when_it_is_off(tmp_path, sandbox, model):
    off = Settings(paypal_client_id="cid", paypal_client_secret="secret", paypal_webhook_id="WH-1",
                   database_url=f"sqlite:///{tmp_path / 'off.db'}", currency="USD", jobs_key="jobs",
                   jobs_every_minutes=0)
    app = create_app(off, PayPal(off), model=model)
    reset(app.state.engine, app.state.sessions)
    with TestClient(app) as c:
        assert c.post("/api/shops/1/owner/to-manager", json={"text": "Thank you."}).status_code == 200
        assert c.get(HOOK, params={"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "1"}).status_code == 503
        assert c.post(HOOK, content=b"{}", headers={"X-Hub-Signature-256": signature(b"{}", "")}).status_code == 503
        assert c.get("/api/config").json()["whatsapp"] is False
        assert c.get("/api/shops/1").json()["whatsapp"] == "off"
        assert c.post("/api/shops/1/whatsapp/link").status_code == 503
    assert getattr(app.state, "whatsapp", None) is None
    assert not [x for x in sandbox.calls if x.request.url.host == "graph.facebook.com"]
    with app.state.sessions() as s:
        assert s.scalar(select(func.count(WhatsAppMessage.id))) == 0


# The pieces

def test_numbers_read_as_e164():
    assert wa.e164("+263 77 123 4567") == wa.e164("263771234567") == wa.e164("00263 (77) 123-4567") == FARAI
    assert wa.e164("0771234567") == "" and wa.e164("+12") == "" and wa.e164("") == ""


def test_the_signature_covers_the_raw_body():
    raw = b'{"object":"whatsapp_business_account"}'
    assert wa.signed(raw, signature(raw), SECRET)
    assert not wa.signed(raw, signature(raw, "other"), SECRET)
    assert not wa.signed(raw, signature(raw)[len("sha256="):], SECRET)
    assert not wa.signed(raw, signature(raw), "")


def test_a_template_parameter_is_one_line_inside_the_body():
    assert wa.parameter("One\n\ntwo\tthree     four") == "One two three four"
    long = wa.parameter("word " * 400)
    assert len(long) == wa.PARAMETER_LIMIT and long.endswith("…")
