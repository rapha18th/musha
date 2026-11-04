"""The demo family: Chipo in Leicester owns a tuck shop in Mbare that her brother Farai runs.

`seed(session)` adds one shop with its family, products, price history, trading
history, past receipts and camera stills. The web app calls it once per visitor,
so every visitor plays a shop of their own.

Run `python -m musha.seed` to reset the local database to one fresh shop, or
`python -m musha.seed --if-empty` to bring an older database up to the models and seed it
only when it is empty.
Sandbox emails come from the environment so each judge can use their own sandbox accounts.
"""
import os
import sys
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Base
from .flows import now
from .models import Message, Person, PricePoint, Product, Shop

# name, unit, shelf price, usual supplier, reorder pack, past cost per unit (cents, oldest first)
PRODUCTS = [
    ("eggs", "trays", 550, "Mbare wholesale", 10, [445, 450, 440, 455, 450, 460]),
    ("bread", "loaves", 110, "Mbare bakery", 20, [85, 85, 86, 85, 87, 86]),
    ("cooking oil", "2 L bottles", 450, "Mbare wholesale", 6, [360, 365, 360, 370, 368, 365]),
    ("sugar", "2 kg packs", 230, "Mbare wholesale", 10, [180, 182, 181, 185, 183, 184]),
]


def seed(session: Session) -> Shop:
    chipo = Person(role="owner", name="Chipo", city="Leicester", language="en",
                   email=os.getenv("MUSHA_OWNER_EMAIL") or "chipo-owner@example.com")
    farai = Person(role="manager", name="Farai", city="Harare", language="sn",
                   phone="+263 00 000 0001",
                   email=os.getenv("MUSHA_MANAGER_EMAIL") or "farai-manager@example.com")
    session.add_all([chipo, farai])
    session.flush()
    shop = Shop(name="Chipo's Tuck Shop", place="Mbare, Harare", currency="USD",
                owner_id=chipo.id, manager_id=farai.id, demo=True)
    session.add(shop)
    session.flush()

    start = now() - timedelta(days=42)
    for name, unit, sell, payee, pack, costs in PRODUCTS:
        session.add(Product(shop_id=shop.id, name=name, unit=unit, sell_cents=sell,
                            usual_payee=payee, pack=pack))
        for i, cents in enumerate(costs):
            session.add(PricePoint(shop_id=shop.id, item=name, supplier=payee, unit_cents=cents,
                                   bought_at=start + timedelta(days=7 * i)))
    session.flush()

    from . import camera, insights, proof

    insights.seed_trading(session, shop)
    proof.seed_history(session, shop)
    camera.seed_frames(session, shop)

    # Written in English; the language bridge puts it in Farai's language when the thread is read.
    session.add(Message(shop_id=shop.id, sender="musha", thread="manager", language="en",
                        text="Hello Farai. Send me a message or a voice note when the shop needs something.",
                        text_en="Hello Farai. Send me a message or a voice note when the shop needs something."))
    session.add(Message(shop_id=shop.id, sender="musha", thread="owner", language="en",
                        text=f"Welcome, Chipo. Ask me anything about {shop.name}."))
    session.commit()
    return shop


def reset(engine, session_factory) -> Shop:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with session_factory() as session:
        return seed(session)


def main(argv: list[str]) -> Shop | None:
    """`--if-empty` brings an older database up to the models and seeds it only when it holds no
    shop. Without it, the database is reset to one fresh shop. Visitors get shops of their own,
    with keys, from the web app."""
    from .config import get_settings
    from .db import make_engine, make_sessionmaker, prepare

    engine = make_engine(get_settings().database_url)
    sessions = make_sessionmaker(engine)
    if "--if-empty" in argv:
        added = prepare(engine)
        if added:
            print(f"Database upgraded: added {', '.join(added)}")
        with sessions() as s:
            if s.scalar(select(Shop.id).limit(1)):
                print("Database already seeded")
                return None
            shop = seed(s)
    else:
        shop = reset(engine, sessions)
    print(f"Seeded {shop.name}, {shop.place} (shop {shop.id})")
    return shop


if __name__ == "__main__":
    main(sys.argv[1:])
