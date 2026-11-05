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
# The WhatsApp Business Cloud API (whatsapp.py): its Graph API version, and the approved template a line
# goes out as past the 24-hour window. WHATSAPP_GRAPH_VERSION and WHATSAPP_TEMPLATE name others.
GRAPH_VERSION = "v27.0"
TEMPLATE = "musha_update"


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
    # WhatsApp Business Cloud API (whatsapp.py). With all four set, Musha's lines to the manager also go to
    # the manager's WhatsApp, and the manager's WhatsApp messages come into the same thread
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_app_secret: str = ""  # signs Meta's webhooks
    whatsapp_verify_token: str = ""  # Meta's webhook handshake
    whatsapp_graph_version: str = GRAPH_VERSION
    # the approved template a line goes out as past the 24-hour window, while PayPal holds money for the manager
    whatsapp_template: str = TEMPLATE

    @property
    def whatsapp_ready(self) -> bool:
        return all((self.whatsapp_token, self.whatsapp_phone_number_id, self.whatsapp_app_secret,
                    self.whatsapp_verify_token))

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
        raise RuntimeError("Musha runs on the PayPal sandbox. Set PAYPAL_ENV=sandbox.")
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
        ag_grid_key=_secret("AG_GRID_KEY"),
        whatsapp_token=_secret("WHATSAPP_TOKEN"),
        whatsapp_phone_number_id=_secret("WHATSAPP_PHONE_NUMBER_ID"),
        whatsapp_app_secret=_secret("WHATSAPP_APP_SECRET"),
        whatsapp_verify_token=_secret("WHATSAPP_VERIFY_TOKEN"),
        whatsapp_graph_version=os.getenv("WHATSAPP_GRAPH_VERSION", "").strip() or GRAPH_VERSION,
        whatsapp_template=os.getenv("WHATSAPP_TEMPLATE", "").strip() or TEMPLATE,
    )


def _secret(name: str) -> str:
    """A secret from the environment. A Windows pipe into Secret Manager can lead it with a BOM."""
    return os.getenv(name, "").replace("\ufeff", "").strip()
