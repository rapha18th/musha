"""Settings from the environment. Musha runs on the PayPal sandbox only."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BACKEND_DIR.parent / ".env")

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
DEFAULT_MODEL = "gemini-3.8-flash"


@dataclass(frozen=True)
class Settings:
    paypal_client_id: str
    paypal_client_secret: str
    paypal_webhook_id: str
    database_url: str
    currency: str
    jobs_key: str
    paypal_base: str = SANDBOX_BASE
    # "sandbox" moves sandbox money; "standin" rehearses every flow in memory
    paypal_mode: str = "sandbox"
    # Gemini: an API key, or a Google Cloud project with application default credentials
    gemini_api_key: str = ""
    gemini_project: str = ""
    gemini_location: str = "global"
    gemini_model: str = DEFAULT_MODEL
    # Run the hold-keeping job inside the web process every N minutes; 0 turns it off
    jobs_every_minutes: int = 30


def get_settings() -> Settings:
    env = os.getenv("PAYPAL_ENV", "sandbox")
    if env != "sandbox":
        raise RuntimeError("Musha is a sandbox demo. Set PAYPAL_ENV=sandbox.")
    mode = os.getenv("MUSHA_PAYPAL", "sandbox")
    if mode not in ("sandbox", "standin"):
        raise RuntimeError("MUSHA_PAYPAL is sandbox or standin.")
    return Settings(
        paypal_client_id=os.getenv("PAYPAL_CLIENT_ID", ""),
        paypal_client_secret=os.getenv("PAYPAL_CLIENT_SECRET", ""),
        paypal_webhook_id=os.getenv("PAYPAL_WEBHOOK_ID", ""),
        database_url=os.getenv("DATABASE_URL") or f"sqlite:///{(BACKEND_DIR / 'musha.db').as_posix()}",
        currency=os.getenv("MUSHA_CURRENCY", "USD"),
        jobs_key=os.getenv("MUSHA_JOBS_KEY", ""),
        paypal_mode=mode,
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_project=os.getenv("GOOGLE_CLOUD_PROJECT", ""),
        gemini_location=os.getenv("GOOGLE_CLOUD_LOCATION", "global"),
        gemini_model=os.getenv("MUSHA_GEMINI_MODEL", DEFAULT_MODEL),
        jobs_every_minutes=int(os.getenv("MUSHA_JOBS_EVERY_MINUTES", "30")),
    )
