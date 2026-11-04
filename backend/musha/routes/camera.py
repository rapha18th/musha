"""The counter camera over HTTP: consent, stills in, heartbeat, Look in, the owner's view, and staged scenes.
The counter key opens the phone's routes; Look in is the owner's."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from .. import camera
from ..deps import db, model, shop_or_404
from ..models import CameraFrame, Shop
from .core import MAX_ID, allow, limit, shop_for

router = APIRouter(prefix="/api")

SENT = ("sample", "look_in")  # what the counter page sends; staged stills come from the staged route
BODY_LIMIT = camera.MAX_UPLOAD + 64_000  # the still and the form around it
TOO_LARGE = "A still is 8 MB at most."


class Staged(BaseModel):
    scene: str = Field(max_length=24)


class Consent(BaseModel):
    by: str = Field(default="", max_length=80)  # the name of the person who agreed; the manager's by default


async def capped_form(request: Request, cap: int, too_large: str, *, max_fields: int = 8):
    """The upload's form, read with a cap on the body. FastAPI would parse a File(...) parameter, and
    spool every byte of it to disk, before the route could look at its size; this stops at the limit."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap:
        raise HTTPException(413, too_large)
    receive, received = request.receive, 0

    async def counted():
        nonlocal received
        message = await receive()
        if message.get("type") == "http.request":
            received += len(message.get("body", b""))
            if received > cap:
                raise HTTPException(413, too_large)
        return message

    return await Request(request.scope, counted).form(max_files=1, max_fields=max_fields)


@router.post("/shops/{shop_id}/camera/consent")
def consent(body: Consent | None = None, shop: Shop = Depends(shop_for("counter")),
            session: Session = Depends(db)):
    """The manager agrees to the counter camera on the counter page, before the first still."""
    camera.agree(session, shop, body.by if body else "")
    return camera.status(session, shop)


@router.post("/shops/{shop_id}/camera/frames", status_code=201,
             dependencies=[Depends(limit("camera_frames", 20, 120))])
async def frame_in(request: Request, shop: Shop = Depends(shop_for("counter")), session: Session = Depends(db),
                   m=Depends(model)):
    """The counter page sends a still as multipart form data: `image` (JPEG, PNG or WebP, 8 MB at most)
    and `kind`, a sample or a look_in when the heartbeat asked for one. A still that repeats the newest
    one is answered with 200 and left unkept."""
    if not camera.consented(shop):
        raise HTTPException(409, f"The counter camera starts once {shop.manager.name} agrees on the counter page.")
    form = await capped_form(request, BODY_LIMIT, TOO_LARGE)
    try:
        image, kind = form.get("image"), form.get("kind") or "sample"
        if not isinstance(image, UploadFile):
            raise HTTPException(422, "Send the still as a file named image.")
        if kind not in SENT:
            raise HTTPException(422, "A still from the counter is a sample or a look_in.")
        data = await image.read(camera.MAX_UPLOAD + 1)
        content_type = image.content_type or ""
    finally:
        await form.close()
    try:
        frame = await camera.ingest_frame(session, m, shop, data=data, content_type=content_type, kind=kind)
    except camera.CameraError as e:
        raise HTTPException(422, str(e))
    view = camera.frame_view(frame)
    return view if frame.id is not None else JSONResponse(view, status_code=200)


@router.post("/shops/{shop_id}/camera/heartbeat", dependencies=[Depends(limit("heartbeat", 30, 60))])
def heartbeat(shop: Shop = Depends(shop_for("counter")), session: Session = Depends(db)):
    return camera.heartbeat(session, shop)


@router.get("/shops/{shop_id}/camera")
def status(shop: Shop = Depends(shop_for("counter")), session: Session = Depends(db)):
    return camera.status(session, shop)


@router.get("/shops/{shop_id}/camera/frames")
def frames(limit_: int = Query(24, ge=1, le=100, alias="limit"), shop: Shop = Depends(shop_for("counter")),
           session: Session = Depends(db)):
    """The camera record, newest first."""
    return [camera.frame_view(f) for f in camera.history(session, shop, limit_)]


@router.post("/shops/{shop_id}/look-in", dependencies=[Depends(limit("look_in", 10, 60))])
def look_in(shop: Shop = Depends(shop_for("owner")), session: Session = Depends(db)):
    """The owner asks for a fresh picture. The answer carries the newest still so far;
    the console polls GET camera until a newer one arrives."""
    camera.request_look_in(session, shop)
    return camera.status(session, shop)


@router.get("/camera/frames/{frame_id}")
def frame_image(request: Request, frame_id: Annotated[int, Path(ge=1, le=MAX_ID)], session: Session = Depends(db)):
    """One still's picture, for a holder of a key to its shop."""
    frame = session.get(CameraFrame, frame_id)
    if not frame:
        raise HTTPException(404, "That still is gone. Musha keeps delivery stills 30 days and the rest a few days.")
    allow(request, shop_or_404(session, frame.shop_id), "counter")
    return Response(frame.data, media_type=frame.content_type,
                    headers={"Cache-Control": "private, max-age=86400"})


@router.post("/shops/{shop_id}/camera/staged", status_code=201, dependencies=[Depends(limit("staged", 20, 60))])
async def staged(body: Staged, shop: Shop = Depends(shop_for("counter")), session: Session = Depends(db),
                 m=Depends(model)):
    """A staged still from the assets, for a demo without a phone on the counter. Demo shops alone."""
    try:
        frame = await camera.stage(session, m, shop, body.scene)
    except camera.NotDemo as e:
        raise HTTPException(403, str(e))
    except camera.CameraError as e:
        raise HTTPException(422, str(e))
    return camera.frame_view(frame)
