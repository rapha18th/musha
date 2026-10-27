"""The language bridge between a manager who writes Shona and an owner who reads English.

STUB. Owned by the bridge builder. Contract:
    await localize_pending(session, model, shop) -> None
Translates Musha's and the owner's English lines in the manager thread into the manager's language.
"""
from sqlalchemy.orm import Session


async def localize_pending(session: Session, model, shop) -> None:
    return None
