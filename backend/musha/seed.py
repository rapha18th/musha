"""The demo family: Chipo in Leicester owns a tuck shop in Mbare that her brother Farai runs.

Run `python -m musha.seed` to reset the local database to this state.
Sandbox emails come from the environment so each judge can use their own sandbox accounts.
"""
import os
from datetime import timedelta

from sqlalchemy.orm import Session

from . import ledger
from .db import Base
from .flows import now
from .models import Message, Person, PricePoint, Shop

EGG_PRICES = [445, 450, 440, 455, 450, 460]  # cents per tray, oldest first


def seed(session: Session) -> Shop:
    chipo = Person(role="owner", name="Chipo", city="Leicester", language="en",
                   email=os.getenv("MUSHA_OWNER_EMAIL", "chipo-owner@example.com"))
    farai = Person(role="manager", name="Farai", city="Harare", language="sn",
                   phone="+263 00 000 0001",
                   email=os.getenv("MUSHA_MANAGER_EMAIL", "farai-manager@example.com"))
    session.add_all([chipo, farai])
    session.flush()
    shop = Shop(name="Chipo's Tuck Shop", place="Mbare, Harare", currency="USD",
                owner_id=chipo.id, manager_id=farai.id)
    session.add(shop)
    session.flush()

    start = now() - timedelta(days=42)
    for i, cents in enumerate(EGG_PRICES):
        bought = start + timedelta(days=7 * i)
        session.add(PricePoint(shop_id=shop.id, item="eggs", supplier="Mbare wholesale",
                               unit_cents=cents, bought_at=bought))
        ledger.post(session, shop_id=shop.id, kind=ledger.STOCK_IN, ref=f"seed-stock-{i}",
                    amount_cents=cents * 30, item="eggs", quantity=30,
                    memo="30 trays from Mbare wholesale", source="seed", at=bought)
        ledger.post(session, shop_id=shop.id, kind=ledger.SALE, ref=f"seed-sale-{i}",
                    amount_cents=550 * 28, item="eggs", quantity=28,
                    memo="Egg sales for the week", source="seed", at=bought + timedelta(days=6))

    session.add(Message(shop_id=shop.id, sender="musha",
                        text="Mhoro Farai. Send me a voice note or a message when the shop needs stock."))
    session.commit()
    return shop


def reset(engine, session_factory) -> Shop:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with session_factory() as session:
        return seed(session)


if __name__ == "__main__":
    from .config import get_settings
    from .db import make_engine, make_sessionmaker

    engine = make_engine(get_settings().database_url)
    shop = reset(engine, make_sessionmaker(engine))
    print(f"Seeded {shop.name}, {shop.place}")
