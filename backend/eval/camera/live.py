"""Shared setup for the live camera scripts: Gemini and a seeded demo shop in memory.

The model is Gemini on Vertex AI for the project in GOOGLE_CLOUD_PROJECT (application default
credentials), or the Gemini API with GEMINI_API_KEY. A run from the cache needs neither.
"""
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

from musha.ai import Gemini  # noqa: E402
from musha.config import Settings  # noqa: E402  (loads the repo's .env)
from musha.db import Base, make_engine, make_sessionmaker  # noqa: E402
from musha.seed import seed  # noqa: E402

MODEL = "gemini-3.8-flash"


def project() -> str:
    return os.getenv("GOOGLE_CLOUD_PROJECT", "")


def settings(database_url: str = "sqlite://") -> Settings:
    return Settings(paypal_client_id="", paypal_client_secret="", paypal_webhook_id="",
                    database_url=database_url, currency="USD", jobs_key="", paypal_mode="standin",
                    gemini_api_key=os.getenv("GEMINI_API_KEY", ""), gemini_project=project(),
                    gemini_location=os.getenv("GOOGLE_CLOUD_LOCATION", "global"), gemini_model=MODEL,
                    jobs_every_minutes=0)


def gemini() -> Gemini:
    if not (project() or os.getenv("GEMINI_API_KEY")):
        raise SystemExit("Set GOOGLE_CLOUD_PROJECT or GEMINI_API_KEY to read stills live.")
    return Gemini(settings())


def demo_shop(database_url: str = "sqlite://"):
    """A fresh session and the seeded demo shop."""
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session = make_sessionmaker(engine)()
    return session, seed(session)


async def patiently(call, tries: int = 4, wait: float = 8.0):
    """The model answers None when it fails, often on a busy quota. Try again after a pause."""
    import asyncio

    for i in range(tries):
        result = await call()
        if result is not None:
            return result
        await asyncio.sleep(wait * (i + 1))
    return None
