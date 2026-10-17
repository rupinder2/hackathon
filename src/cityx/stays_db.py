from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stays (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          phone TEXT NOT NULL,
          city TEXT NOT NULL,
          address TEXT,
          listed_price TEXT NOT NULL,
          source TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS stays_phone_city
        ON stays (phone, city)
        """
    )
    conn.commit()
    return conn


def upsert_stay(
    conn: sqlite3.Connection,
    *,
    name: str,
    phone: str,
    city: str,
    address: str | None,
    listed_price: str,
    source: str = "google_hotels",
) -> None:
    conn.execute(
        """
        INSERT INTO stays (name, phone, city, address, listed_price, source)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(phone, city) DO UPDATE SET
          name = excluded.name,
          address = COALESCE(stays.address, excluded.address),
          listed_price = excluded.listed_price,
          source = excluded.source
        """,
        (name, phone, city, address, listed_price, source),
    )
    conn.commit()


def stays_for_city(conn: sqlite3.Connection, city: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT name, phone, city, address, listed_price, source
            FROM stays
            WHERE city = ?
            ORDER BY id
            """,
            (city,),
        )
    )
