"""Entry point: `uvicorn main:app --reload` from the backend folder."""
from musha.api import create_app

app = create_app()
