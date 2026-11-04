import asyncio
import logging
import os
import time

import httpx
from sqlalchemy import create_engine, event, inspect, literal, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

log = logging.getLogger("musha")


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    if url.startswith("postgres://"):  # the short scheme some hosts hand out
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if not url.startswith("sqlite"):
        return create_engine(url)
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})  # a short writer waits its turn

    @event.listens_for(engine, "connect")
    def _fast_writes(conn, _):  # a write-ahead log syncs once per checkpoint, so local writes run about twice as fast
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    return engine


def make_sessionmaker(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def schema_drift(engine) -> list[str]:
    """The tables and columns the models have and the database lacks, as "table" or
    "table.column". create_all adds a missing table and never a missing column, so a
    database made by older code shows its age here."""
    seen = inspect(engine)
    tables = set(seen.get_table_names())
    missing = []
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            missing.append(table.name)
            continue
        have = {c["name"] for c in seen.get_columns(table.name)}
        missing += [f"{table.name}.{c.name}" for c in table.columns if c.name not in have]
    return missing


class SchemaBehind(RuntimeError):
    """The database lacks columns the code reads, and Musha could not add them."""


def _add_column(conn, table, column) -> None:
    dialect = conn.dialect
    prep = dialect.identifier_preparer
    sql = f"ALTER TABLE {prep.quote(table.name)} ADD COLUMN {prep.quote(column.name)} " \
          f"{column.type.compile(dialect=dialect)}"
    default = column.default.arg if column.default is not None and column.default.is_scalar else None
    if default is not None:
        value = literal(default, type_=column.type).compile(dialect=dialect, compile_kwargs={"literal_binds": True})
        sql += f" DEFAULT {value}"
        if not column.nullable:
            sql += " NOT NULL"
    # a column with no plain default arrives nullable: old rows have no value to give it
    conn.execute(text(sql))


def prepare(engine) -> list[str]:
    """Bring the database up to the models: create missing tables, then add missing columns,
    each with its default, so a database made by older code keeps its shops. Returns what it
    added. Raises SchemaBehind when a column is still missing after that."""
    Base.metadata.create_all(engine)
    missing = [m for m in schema_drift(engine) if "." in m]
    if missing:
        with engine.begin() as conn:
            for m in missing:
                name, col = m.split(".", 1)
                table = Base.metadata.tables[name]
                _add_column(conn, table, table.columns[col])
        log.warning("database upgraded: added %s", ", ".join(missing))
    left = schema_drift(engine)
    if left:
        raise SchemaBehind("The database lacks " + ", ".join(left[:8]) + ". Run python -m musha.seed to rebuild it.")
    return missing


# Durable writes: under Litestream, a write answers once the bucket holds it

WRITES = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SYNC_SECONDS = 3.0  # the longest a reply waits on the bucket; Litestream's own sync carries the write after that
WARN_EVERY = 60.0  # seconds between warnings while Litestream stays out of reach


class LitestreamSync:
    """Asks Litestream to ship the database's changes now, and waits until Cloud Storage holds them.
    It speaks to the control socket deploy/litestream.yml opens (POST /sync with wait), the same
    call as `litestream sync -wait`. Each wait lasts SYNC_SECONDS at most. A socket that is slow,
    absent or refuses gives False, and the caller replies all the same."""

    def __init__(self, socket: str, path: str, *, seconds: float = SYNC_SECONDS,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.socket, self.path, self.seconds = socket, path, seconds
        self.transport = transport
        self.client: httpx.AsyncClient | None = None
        self._warned = 0.0

    async def __call__(self) -> bool:
        if self.client is None:
            self.client = httpx.AsyncClient(transport=self.transport or httpx.AsyncHTTPTransport(uds=self.socket),
                                            base_url="http://litestream", timeout=self.seconds + 1)
        try:
            r = await asyncio.wait_for(self.client.post("/sync", json={
                "path": self.path, "wait": True, "timeout": max(1, int(self.seconds))}), self.seconds)
            r.raise_for_status()
            status = r.json().get("status", "")
            if status in ("synced", "no_change"):
                return True
            self._warn(f"Litestream answered {status or 'without a status'}")
        except Exception as e:
            self._warn(repr(e) if not isinstance(e, asyncio.TimeoutError) else f"over {self.seconds:g} s")
        return False

    def _warn(self, why: str) -> None:
        at = time.monotonic()
        if at - self._warned >= WARN_EVERY:
            self._warned = at
            log.warning("a write replied before the bucket held it (%s); Litestream's 1 s sync carries it", why)


def litestream_sync(database_url: str) -> LitestreamSync | None:
    """The sync for this process: on when LITESTREAM_REPLICA_URL is set and the database is SQLite,
    as deploy/entrypoint.sh runs it on Cloud Run. MUSHA_DURABLE_WRITES=0 turns it off."""
    if not os.getenv("LITESTREAM_REPLICA_URL") or not database_url.startswith("sqlite"):
        return None
    if os.getenv("MUSHA_DURABLE_WRITES", "1").strip() == "0":
        return None
    path = os.getenv("MUSHA_DB_PATH") or make_url(database_url).database or ""
    socket = os.getenv("LITESTREAM_SOCKET") or "/var/run/litestream.sock"
    return LitestreamSync(socket, path) if path else None


class DurableWrites:
    """ASGI middleware: a request that can write (POST, PUT, PATCH, DELETE) gets its reply once
    Litestream has shipped the change to the bucket, so a reply the visitor saw outlives the
    instance. Litestream also syncs every second on its own, which carries the writes made outside
    a request: the feed's stills, a call's tools, translations a poll finishes later. Without a
    replica the middleware passes every request straight through."""

    def __init__(self, app, *, database_url: str = "", sync=None):
        self.app = app
        self.sync = sync if sync is not None else litestream_sync(database_url)
        if self.sync is not None:
            log.info("writes reply once Litestream has synced them")

    async def __call__(self, scope, receive, send):
        if self.sync is None or scope["type"] != "http" or scope.get("method") not in WRITES:
            return await self.app(scope, receive, send)

        async def synced(message):
            if message["type"] == "http.response.start":
                await self.sync()  # the route has committed; its reply waits for the bucket
            await send(message)

        await self.app(scope, receive, synced)
