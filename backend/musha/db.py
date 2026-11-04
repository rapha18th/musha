import logging

from sqlalchemy import create_engine, event, inspect, literal, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

log = logging.getLogger("musha")


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    if url.startswith("postgres://"):  # Render hands out the short scheme
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
