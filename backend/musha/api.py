"""HTTP API for the owner console, the manager's phone and the counter camera."""
import asyncio
import contextlib
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import flows
from .ai import make_model
from .config import Settings, get_settings
from .db import Base, make_engine, make_sessionmaker
from .paypal import PayPal
from .routes import agent, camera, core, evidence, insights, phone

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
log = logging.getLogger("musha")


def make_paypal(settings: Settings):
    if settings.paypal_mode == "standin":
        from .standin import StandInPayPal

        return StandInPayPal()
    return PayPal(settings)


def create_app(settings: Settings | None = None, paypal=None, model=None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    sessions = make_sessionmaker(engine)
    pp = paypal or make_paypal(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if settings.jobs_every_minutes > 0:
            task = asyncio.create_task(_keep_holds(sessions, pp, settings.jobs_every_minutes * 60))
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Musha", version="0.2.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.sessions = sessions
    app.state.settings = settings
    app.state.paypal = pp
    app.state.model = model or make_model(settings)

    for area in (core, evidence, camera, insights, phone, agent):
        app.include_router(area.router)

    if FRONTEND_DIST.exists():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="web")
    return app


async def _keep_holds(sessions, pp, every_seconds: int):
    """Reauthorize holds past the honor period and close them before they expire."""
    while True:
        await asyncio.sleep(every_seconds)
        try:
            with sessions() as session:
                done = await flows.run_due(session, pp)
            if done:
                log.info("hold job: %s", done)
        except Exception:
            log.exception("hold job failed")
