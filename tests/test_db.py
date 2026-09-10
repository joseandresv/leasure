import sqlite3

from sqlalchemy import create_engine, inspect, text

import db as db_mod


def test_migration_adds_missing_columns_and_backs_up(tmp_path, monkeypatch):
    path = tmp_path / "leasure.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tracks (id INTEGER PRIMARY KEY, title VARCHAR(500), artist VARCHAR(500))")
    con.commit()
    con.close()

    monkeypatch.setattr(db_mod, "DB_PATH", path)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        db_mod._migrate_missing_columns(conn)
    cols = {c["name"] for c in inspect(engine).get_columns("tracks")}
    assert {"genre", "file_path", "status", "synced_at"} <= cols
    backups = list(tmp_path.glob("leasure.db.*.bak"))
    assert len(backups) == 1

    # Second run is a no-op: no new backup
    with engine.begin() as conn:
        db_mod._migrate_missing_columns(conn)
    assert len(list(tmp_path.glob("leasure.db.*.bak"))) == 1


def test_migration_fills_not_null_column_and_backup_is_readable(tmp_path, monkeypatch):
    path = tmp_path / "leasure.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tracks (id INTEGER PRIMARY KEY, title VARCHAR(500) NOT NULL, artist VARCHAR(500) NOT NULL)")
    con.execute("INSERT INTO tracks (title, artist) VALUES ('Song', 'Band')")
    con.commit()
    con.close()

    monkeypatch.setattr(db_mod, "DB_PATH", path)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        db_mod._migrate_missing_columns(conn)

    with engine.connect() as conn:
        row = conn.execute(text("SELECT status, created_at FROM tracks")).one()
    assert row.status == "pending"
    assert row.created_at is None  # func.now() is not a constant ADD COLUMN default

    backup = next(iter(tmp_path.glob("leasure.db.*.bak")))
    con = sqlite3.connect(backup)
    assert con.execute("SELECT count(*) FROM tracks").fetchone()[0] == 1
    con.close()
