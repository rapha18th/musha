"""A database made by older code: Musha adds what it lacks, or refuses to start."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from musha import seed as seeding
from musha.ai import NoModel
from musha.api import create_app
from musha.db import Base, SchemaBehind, make_engine, make_sessionmaker, prepare, schema_drift
from musha.standin import StandInPayPal

OLD = ("shops.demo", "shops.owner_key", "shops.counter_key", "shops.camera_consent_at", "spend_requests.inferred")


def older_database(url: str) -> None:
    """A database with one shop, from before the newest columns."""
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    with make_sessionmaker(engine)() as s:
        seeding.seed(s)
    with engine.begin() as conn:
        for m in OLD:
            table, column = m.split(".")
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    assert sorted(schema_drift(engine)) == sorted(OLD)
    engine.dispose()


def test_an_older_database_gains_its_new_columns_and_keeps_its_shops(settings):
    older_database(settings.database_url)
    app = create_app(settings, StandInPayPal(), model=NoModel())
    assert schema_drift(app.state.engine) == []
    client = TestClient(app)
    shop = client.get("/api/shops/1")
    assert (shop.status_code, shop.json()["name"]) == (200, "Chipo's Tuck Shop")
    assert client.get("/api/shops/1/requests").status_code == 200
    assert client.get("/api/shops/1/camera").status_code == 200
    with app.state.sessions() as s:
        assert s.execute(text("SELECT demo, owner_key, camera_consent_at FROM shops WHERE id = 1")).one() == (
            0, "", None)


def test_the_seed_upgrades_an_older_database_and_leaves_its_shops(settings, monkeypatch, capsys):
    older_database(settings.database_url)
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    assert seeding.main(["--if-empty"]) is None
    out = capsys.readouterr().out
    assert "Database upgraded: added" in out and "Database already seeded" in out
    assert schema_drift(make_engine(settings.database_url)) == []


def test_a_fresh_database_is_seeded_once(settings, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    shop = seeding.main(["--if-empty"])
    assert shop is not None and seeding.main(["--if-empty"]) is None
    with make_sessionmaker(make_engine(settings.database_url))() as s:
        assert s.scalar(text("SELECT count(*) FROM shops")) == 1


def test_a_column_musha_cannot_add_stops_the_start(settings, monkeypatch):
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    monkeypatch.setattr("musha.db._add_column", lambda conn, table, column: None)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE shops DROP COLUMN demo"))
    with pytest.raises(SchemaBehind, match="shops.demo"):
        prepare(engine)
