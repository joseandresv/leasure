import logging
import sqlite3
from contextlib import closing
from datetime import datetime

from sqlalchemy import Index, Integer, Numeric, String, event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import settings
from models import Base

logger = logging.getLogger(__name__)

DB_PATH = settings.data_dir / "leasure.db"
DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _record):
    """Per-connection SQLite settings: enforce FK cascades (off by default in
    SQLite, so ondelete=CASCADE was silently ignored), WAL for concurrent reads
    while the worker writes, and a busy timeout instead of immediate 'locked'."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def _backup_db_file() -> None:
    if not DB_PATH.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DB_PATH.with_name(f"leasure.db.{stamp}.bak")
    # Online backup API, not copyfile: under WAL the main db file on its own can
    # be missing committed pages that still live in the -wal.
    with closing(sqlite3.connect(DB_PATH)) as source, closing(sqlite3.connect(backup)) as dest:
        source.backup(dest)
    logger.info("Database backed up to %s before migration", backup.name)


def _sql_literal(value) -> str:
    if isinstance(value, str):
        quoted = value.replace("'", "''")
        return f"'{quoted}'"
    return str(value)


def _add_column_default(column) -> str | None:
    """Constant SQL default for ADD COLUMN, or None if the column has no usable
    one. SQL-expression server defaults (func.now()) are excluded: SQLite rejects
    non-constant defaults in ADD COLUMN."""
    if column.default is not None and getattr(column.default, "is_scalar", False):
        return _sql_literal(column.default.arg)
    arg = getattr(column.server_default, "arg", None)
    if isinstance(arg, str):
        return _sql_literal(arg)
    if not column.nullable:
        if isinstance(column.type, String):
            return "''"
        if isinstance(column.type, (Integer, Numeric)):
            return "0"
    return None


def _migrate_missing_columns(conn) -> None:
    """create_all only creates missing *tables*. Add any column the models declare
    that the existing table lacks, plus any index those tables are missing (additive
    only), after backing up."""
    inspector = inspect(conn)
    pending: list[tuple[str, str]] = []
    pending_indexes: list[Index] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in inspector.get_table_names():
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            coltype = column.type.compile(dialect=conn.dialect)
            default = _add_column_default(column)
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {coltype}'
            if default is None:
                if not column.nullable:
                    logger.warning(
                        "Adding %s.%s as nullable: NOT NULL needs a constant default",
                        table.name, column.name)
            else:
                if not column.nullable:
                    ddl += " NOT NULL"
                ddl += f" DEFAULT {default}"
            pending.append((column.name, ddl))
        known = {ix["name"] for ix in inspector.get_indexes(table.name)}
        pending_indexes += [ix for ix in table.indexes if ix.name not in known]
    if not pending and not pending_indexes:
        return
    _backup_db_file()
    for name, ddl in pending:
        logger.info("Migrating: adding column %s", name)
        conn.execute(text(ddl))
    for index in sorted(pending_indexes, key=lambda ix: ix.name or ""):
        logger.info("Migrating: creating index %s", index.name)
        index.create(conn)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_missing_columns)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
