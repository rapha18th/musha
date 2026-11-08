"""HTTP API for the owner's console, the manager's dashboard and the counter camera."""
import asyncio
import contextlib
import logging
import re
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from . import accounts, flows
from . import seed as seeding
from .ai import Gemini, make_model
from .config import Settings, get_settings
from .db import DurableWrites, make_engine, make_sessionmaker, prepare
from .deps import access
from .paypal import PayPal
from .routes import agent, auth, camera, core, evidence, insights, money, phone
from .routes import call as shop_call
from .routes import whatsapp

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
# The web app's own pages. None is a file in the build, so each opens index.html and the app draws it.
PAGES = re.compile(r"(sign-in|sign-up|owner|manager|counter|invite/[A-Za-z0-9_-]{1,128})")
log = logging.getLogger("musha")


class Web(StaticFiles):
    """The built web app: its files as they are, and index.html for each of its own pages, such as
    /owner or the manager's /invite/<token>."""

    async def get_response(self, path: str, scope):
        if PAGES.fullmatch(path.replace("\\", "/")):
            path = "index.html"
        return await super().get_response(path, scope)


def make_paypal(settings: Settings):
    if settings.paypal_mode == "standin":
        from .standin import StandInPayPal

        return StandInPayPal()
    return PayPal(settings)


def create_app(settings: Settings | None = None, paypal=None, model=None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    prepare(engine)  # a database older than the code gains its new columns, or the app refuses to start
    accounts.prepare(engine)  # and the unique index on login emails
    sessions = make_sessionmaker(engine)
    pp = paypal or make_paypal(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        with sessions() as session:  # the sandbox shop and its sign-ins, made once; the hosted service needs their passwords
            seeding.sandbox(session)
            try:
                await seeding.refresh(session, pp)  # a day old and left alone, it starts fresh
            except Exception:
                session.rollback()
                log.exception("seeding the sandbox shop again")
        outbox = getattr(app.state, "whatsapp", None)
        if outbox is not None:
            outbox.start()  # lines committed on a worker thread reach the manager's WhatsApp from this loop
        tasks = []
        if settings.jobs_every_minutes > 0:
            tasks.append(asyncio.create_task(_keep_holds(sessions, pp, app.state.model,
                                                         settings.jobs_every_minutes * 60)))
        if isinstance(app.state.model, Gemini):
            tasks.append(asyncio.create_task(_warm(app.state.model)))
        from . import feed  # the sample video feed: a still at the counter phone's pace while a page watches

        tasks.append(asyncio.create_task(feed.run(sessions, app.state.model)))
        yield
        for task in tasks:
            task.cancel()

    app = FastAPI(title="Musha", version="0.2.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.sessions = sessions
    app.state.settings = settings
    app.state.paypal = pp
    app.state.model = model or make_model(settings)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(core.BodyLimit, paths={whatsapp.WEBHOOK: whatsapp.MAX_BODY})  # a body past its size stops before anything reads it
    app.add_middleware(DurableWrites, database_url=settings.database_url)  # under Litestream, a write replies once the bucket holds it
    app.add_exception_handler(RequestValidationError, core.validation_errors)  # errors without the input

    for area in (core, auth, evidence, camera, insights, money, phone, agent):  # every id in a path answers to its shop
        app.include_router(area.router, dependencies=[Depends(access)])
    app.include_router(shop_call.router, dependencies=[Depends(access)])
    app.include_router(whatsapp.linking, dependencies=[Depends(access)])  # the manager's link code
    app.include_router(shop_call.live_router)  # the call's socket checks the owner's session itself
    app.include_router(whatsapp.router)  # Meta's webhook checks its own signature
    whatsapp.install(app)  # with WhatsApp on, Musha's lines to each manager also go to the manager's WhatsApp

    if FRONTEND_DIST.exists():
        app.mount("/", Web(directory=FRONTEND_DIST, html=True), name="web")
    return app


async def _warm(model: Gemini):
    """The first call in a process pays for sign-in and a connection; pay it before a person waits."""
    await model.text("localize", ["Reply with the word ready."])


async def _keep_holds(sessions, pp, model, every_seconds: int):
    """Reauthorize holds past the honor period and close them before they expire, then let Musha ask
    for the stock running low in the shops someone used lately: the same turn POST /api/jobs/holds runs."""
    from . import insights  # the module; the name above is its routes

    while True:
        await asyncio.sleep(every_seconds)
        try:
            with sessions() as session:
                done = await flows.run_due(session, pp)
                done += await insights.restock_due(session, pp, model)
            if done:
                log.info("hold job: %s", done)
        except Exception:
            log.exception("hold job failed")
