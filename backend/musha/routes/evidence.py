"""Evidence over HTTP: receipts and photos in, the proof engine's verdicts, the owner's decision,
attachments out, and demo receipts and goods photos for the manager's phone. The manager key opens
evidence and answers; the owner key opens decisions and a second scoring."""
import unicodedata
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from .. import evidence, flows, proof, receipts
from ..deps import db, guarded, model, paypal, shop_or_404
from ..models import Attachment, JsonText, SpendRequest
from ..money import to_cents, to_value
from ..views import attachment_view, message_view, proof_view, request_view
from .camera import capped_form
from .core import MAX_ID, Amount, allow, cents_of, limit, request_for

router = APIRouter(prefix="/api")

CAPTIONS = {"receipt": "Receipt", "goods": "Photo of the goods", "quote": "Quote", "voice": "Voice note",
            "photo": "Photo"}


class Answer(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class Decide(BaseModel):
    action: str = Field(max_length=12)  # release | return
    amount: Amount | None = None  # decimal string; release defaults to the receipt total


class Sample(BaseModel):
    variant: str = Field(default="genuine", max_length=24)


BODY_LIMIT = evidence.MAX_BYTES + 64_000  # the file and the form around it
TOO_LARGE = "Files up to 8 MB, please."
FIELDS = ("kind", "caption", "total", "receipt_no", "date", "payee")
PHOTO_KINDS = ("goods", "photo")


def _verdict(req, p, **extra) -> dict:
    return {"proof": p and proof_view(p), "request": request_view(req), **extra}


@router.post("/requests/{request_id}/evidence", status_code=201,
             dependencies=[Depends(limit("evidence", 20, 60))])
async def add_evidence(request: Request, background: BackgroundTasks,
                       req: SpendRequest = Depends(request_for("manager")), session: Session = Depends(db),
                       pp=Depends(paypal), m=Depends(model)):
    """The manager sends a receipt, a photo, a quote or a voice note against a request, as multipart
    form data: `file`, `kind` and, for a receipt typed by hand, `total`, `receipt_no`, `date` and
    `payee`. The body stops at 8 MB as it arrives. A receipt on a held or asking request runs the
    proof engine at once. A flagged request waits for the owner: its new receipts join the evidence
    the owner sees. A goods photo is read as it arrives, so the receipt's scoring finds it read."""
    form = await capped_form(request, BODY_LIMIT, TOO_LARGE, max_fields=len(FIELDS) + 1)
    try:
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise HTTPException(422, "Send the evidence as a file named file.")
        fields = {name: str(form.get(name) or "")[:200] for name in FIELDS}
        data = await file.read(evidence.MAX_BYTES + 1)
        content_type = file.content_type or ""
    finally:
        await form.close()
    kind, caption, total = fields["kind"] or "receipt", fields["caption"], fields["total"]
    receipt_no, date, payee = fields["receipt_no"], fields["date"], fields["payee"]
    try:
        a = evidence.store_attachment(session, req.shop, kind=kind, data=data,
                                      content_type=content_type, request_id=req.id)
    except evidence.EvidenceError as e:
        raise HTTPException(422, str(e))
    if kind == "receipt":
        if total and not proof._cents(total):
            raise HTTPException(422, "Type the total as a number, for example 171.00.")
        typed = proof.typed_reading(total=total, receipt_no=receipt_no, day=date, payee=payee)
        if typed:
            a.reading = JsonText.dump(typed)
    manager = req.shop.manager
    text = caption.strip() or CAPTIONS[kind]
    msg = flows.say(session, req.shop_id, text, sender="manager", request_id=req.id, attachment_id=a.id,
                    language=manager.language if caption.strip() else "en",
                    text_en=None if not caption.strip() or manager.language == "en" else "")
    session.commit()
    p = None
    if kind == "receipt" and req.status in ("held", "asking"):
        p = await guarded(proof.prove(session, pp, m, req))
    elif kind == "receipt" and req.status == "flagged":
        owner = req.shop.owner.name
        flows.say(session, req.shop_id, f"{owner} is checking the {req.item} receipt. "
                  f"I added this one for {owner} to see.", request_id=req.id)
        session.commit()
    elif kind in PHOTO_KINDS and req.status == "asking" and _asked_for_photo(req):
        p = await guarded(proof.answer(session, pp, m, req, text))
    elif kind in PHOTO_KINDS and req.status not in ("captured", "part_captured", "voided"):
        background.add_task(proof.read_photo_soon, request.app.state.sessions, m, a.id)
    return _verdict(req, p, attachment=attachment_view(a), message=message_view(msg))


def _asked_for_photo(req) -> bool:
    return proof.asked_about(req.proofs[-1] if req.proofs else None) == "photo"


@router.post("/requests/{request_id}/prove", dependencies=[Depends(limit("prove", 10, 60))])
async def prove_again(req: SpendRequest = Depends(request_for("owner")), session: Session = Depends(db),
                      pp=Depends(paypal), m=Depends(model)):
    """Run the engine over the latest receipt again, for a held request. On a request the owner
    holds, the score releases or keeps it for her; it never sends the manager a question."""
    p = await guarded(proof.prove(session, pp, m, req))
    if p is None:
        raise HTTPException(409, "Proving needs a receipt on an open hold.")
    return _verdict(req, p)


@router.post("/requests/{request_id}/answer", dependencies=[Depends(limit("answer", 10, 60))])
async def answer(body: Answer, req: SpendRequest = Depends(request_for("manager")), session: Session = Depends(db),
                 pp=Depends(paypal), m=Depends(model)):
    """The manager answers Musha's one question; the engine scores again and decides."""
    if req.status != "asking":
        raise HTTPException(409, f"Request {req.id} is {req.status}. Answers go to a request with an open question.")
    manager = req.shop.manager
    msg = flows.say(session, req.shop_id, body.text, sender="manager", request_id=req.id,
                    language=manager.language, text_en=None if manager.language == "en" else "")
    session.commit()
    p = await guarded(proof.answer(session, pp, m, req, body.text))
    return _verdict(req, p, message=message_view(msg))


@router.post("/requests/{request_id}/decide", dependencies=[Depends(limit("decide", 20, 60))])
async def decide(body: Decide, req: SpendRequest = Depends(request_for("owner")), session: Session = Depends(db),
                 pp=Depends(paypal)):
    """The owner decides a held request: release an amount, or return it all."""
    if body.action not in ("release", "return"):
        raise HTTPException(422, "The action is release or return.")
    cents = cents_of(body.amount) if body.action == "release" and body.amount else None
    p = await guarded(proof.decide(session, pp, req, action=body.action, amount_cents=cents))
    return _verdict(req, p)


@router.get("/attachments/{attachment_id}")
def attachment(request: Request, attachment_id: Annotated[int, Path(ge=1, le=MAX_ID)],
               session: Session = Depends(db)):
    """One piece of evidence, for a holder of the manager's or the owner's key to its shop."""
    a = session.get(Attachment, attachment_id)
    if not a:
        raise HTTPException(404, f"Attachment {attachment_id} is missing.")
    allow(request, shop_or_404(session, a.shop_id), "manager")
    return Response(a.data, media_type=a.content_type, headers={
        "Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'inline; filename="{a.kind}-{a.id}{_ext(a.content_type)}"'})


@router.post("/requests/{request_id}/samples", dependencies=[Depends(limit("samples", 20, 60))])
def sample(body: Sample, req: SpendRequest = Depends(request_for("manager")), session: Session = Depends(db)):
    """A demo receipt photo for this request, to send from the manager's phone like any photo.
    `reused` is last week's receipt, byte for byte. Headers carry what is printed, for typing
    it in when Musha runs without the model."""
    if body.variant not in receipts.VARIANTS:
        raise HTTPException(422, f"A sample is one of {', '.join(receipts.VARIANTS)}.")
    if body.variant == "reused":
        old = _earlier_receipt(session, req)
        if old is None:
            raise HTTPException(404, "A reused sample needs an earlier receipt in this shop.")
        printed = JsonText.load(old.reading, {}) or {}
        return _image(old.data, old.content_type, "reused", total=printed.get("total"),
                      number=printed.get("receipt_no", ""), day=printed.get("date", ""),
                      payee=printed.get("payee", ""))
    spec = receipts.sample_spec(session, req, body.variant)
    data = receipts.render_receipt(spec, seed=f"sample:{req.id}:{body.variant}")
    return _image(data, "image/jpeg", body.variant, total=to_value(spec.total), number=spec.receipt_no,
                  day=spec.at.date().isoformat(), payee=spec.payee)


@router.get("/samples/goods/{item}")
def goods(item: str):
    """A photo of the goods for the demo, one per product."""
    data = receipts.goods_photo(item)
    if data is None:
        raise HTTPException(404, "Goods photos cover the demo shop's own products.")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


def _earlier_receipt(session: Session, req) -> Attachment | None:
    """Last week's receipt from the shop's history, for the same item when there is one;
    otherwise the most recent receipt on another request."""
    rows = list(session.scalars(select(Attachment).where(
        Attachment.shop_id == req.shop_id, Attachment.kind == "receipt", Attachment.request_id.is_not(None),
        Attachment.request_id != req.id).order_by(Attachment.id.desc())))
    same = [a for a in rows if a.request and proof.names_item(req.item, a.request.item)]
    history = [a for a in same or rows if (JsonText.load(a.reading, {}) or {}).get("source") == "seed"]
    return (history or same or rows or [None])[0]


def _image(data: bytes, mime: str, variant: str, *, total, number: str, day: str, payee: str) -> Response:
    total_text = to_value(to_cents(f"{total}")) if total not in (None, "") else ""
    return Response(data, media_type=mime, headers={
        "Content-Disposition": f'inline; filename="receipt-{variant}{_ext(mime)}"',
        "X-Receipt-Total": total_text, "X-Receipt-No": number, "X-Receipt-Date": day,
        "X-Receipt-Payee": _ascii(payee), "X-Sample": variant,
        "Access-Control-Expose-Headers": "X-Receipt-Total, X-Receipt-No, X-Receipt-Date, X-Receipt-Payee, X-Sample"})


def _ascii(s: str) -> str:
    """Headers travel as Latin-1; keep the payee readable in plain letters."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _ext(mime: str) -> str:
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
            "application/pdf": ".pdf", "audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a",
            "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/aac": ".aac", "audio/amr": ".amr"}.get(mime, "")
