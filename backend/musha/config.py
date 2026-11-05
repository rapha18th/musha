"""Settings from the environment. Musha runs on the PayPal sandbox only."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BACKEND_DIR.parent / ".env")

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
DEFAULT_MODEL = "gemini-3.8-flash"
# The shop call runs on Gemini Live. With an API key, the Gemini API serves gemini-3.8-live;
# on Vertex AI (a project and application default credentials) the Live model is gemini-live-2.5-flash.
LIVE_MODEL_API = "gemini-3.8-live"
LIVE_MODEL_VERTEX = "gemini-live-2.5-flash"


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
    # MUSHA_LIVE_MODEL names the shop call's Live model; empty picks one for the backend (live_model)
    live_model_override: str = ""
    # AG Grid and AG Charts Enterprise licence for the Money view, served at runtime by /api/config;
    # empty runs the view with the trial watermark
    ag_grid_key: str = ""

    @property
    def live_model(self) -> str:
        """The shop call's model: the override, else the Live model of the backend Gemini runs on."""
        return self.live_model_override or (LIVE_MODEL_API if self.gemini_api_key else LIVE_MODEL_VERTEX)

    @property
    def live_ready(self) -> bool:
        return bool(self.gemini_api_key or self.gemini_project)


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
        live_model_override=os.getenv("MUSHA_LIVE_MODEL", "").strip(),
        ag_grid_key=os.getenv("AG_GRID_KEY", "").replace("\ufeff", "").strip(),  # a Windows pipe can lead with a BOM
    )
