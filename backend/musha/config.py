"""Settings from the environment. Musha runs on the PayPal sandbox only."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BACKEND_DIR.parent / ".env")

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"


@dataclass(frozen=True)
class Settings:
    paypal_client_id: str
    paypal_client_secret: str
    paypal_webhook_id: str
    database_url: str
    currency: str
    jobs_key: str
    paypal_base: str = SANDBOX_BASE


def get_settings() -> Settings:
    env = os.getenv("PAYPAL_ENV", "sandbox")
    if env != "sandbox":
        raise RuntimeError("Musha is a sandbox demo. Set PAYPAL_ENV=sandbox.")
    return Settings(
        paypal_client_id=os.getenv("PAYPAL_CLIENT_ID", ""),
        paypal_client_secret=os.getenv("PAYPAL_CLIENT_SECRET", ""),
        paypal_webhook_id=os.getenv("PAYPAL_WEBHOOK_ID", ""),
        database_url=os.getenv("DATABASE_URL") or f"sqlite:///{(BACKEND_DIR / 'musha.db').as_posix()}",
        currency=os.getenv("MUSHA_CURRENCY", "USD"),
        jobs_key=os.getenv("MUSHA_JOBS_KEY", ""),
    )
