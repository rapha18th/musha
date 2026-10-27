"""Chitoro lite: the counter phone, the pixel gate, the delivery check, and Look in.

STUB. Owned by the camera builder. Contract:
    await delivery_check(session, model, shop, *, item, since, until) -> dict | None
None means no camera watched the window, so the Camera check does not apply.
Otherwise {"seen": bool, "frame_ids": [int], "at": iso | None, "reason": str}.
"""
from sqlalchemy.orm import Session


async def delivery_check(session: Session, model, shop, *, item: str, since, until) -> dict | None:
    return None


def seed_frames(session: Session, shop) -> None:
    return None
