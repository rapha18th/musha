"""The owner's agent: approves spend inside the owner's mandate, and asks the owner outside it.

STUB. Owned by the mandate builder. Contract:
    await consider(session, pp, model, req) -> dict | None
Called right after a request is created. Returns None when no mandate is active.
"""
from sqlalchemy.orm import Session


async def consider(session: Session, pp, model, req) -> dict | None:
    return None
