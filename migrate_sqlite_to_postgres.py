"""
Xoptime migration: SQLite -> Postgres

Usage (Windows PowerShell):
  setx DATABASE_URL "postgresql://user:pass@localhost:5432/xoptime"
  python migrate_sqlite_to_postgres.py

Notes:
- This will COPY data from database.db into the Postgres DB in DATABASE_URL.
- It will INSERT explicit ids and then reset sequences.
"""

import os
import sqlite3
from sqlalchemy import create_engine, text as sqltext

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SQLITE_PATH = os.path.join(APP_DIR, "database.db")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise SystemExit("DATABASE_URL not set. Example: postgresql://user:pass@localhost:5432/xoptime")

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)


ORDER = [
    "users",
    "products",
    "product_variants",
    "product_images",
    "cart_items",
    "orders",
    "order_items",
    "reviews",
    "seller_wallet",
    "seller_transactions",
    "payouts",
    "wishlist_items",
    "coupons",
    "return_requests",
    "notifications",
    "support_tickets",
    "support_messages",
]


def list_tables_sqlite(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return [r[0] for r in rows]


def table_columns_sqlite(conn, table):
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    # (cid, name, type, notnull, dflt_value, pk)
    return [c[1] for c in cols], cols


def clear_table_pg(table):
    with engine.begin() as cx:
        cx.execute(sqltext(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE'))


def copy_table(table, sqlite_conn):
    cols, cols_meta = table_columns_sqlite(sqlite_conn, table)
    col_list = ", ".join([f'"{c}"' for c in cols])
    placeholders = ", ".join([f":p{i}" for i in range(len(cols))])
    ins = sqltext(f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})')

    rows = sqlite_conn.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()

    with engine.begin() as cx:
        for r in rows:
            cx.execute(ins, {f"p{i}": r[i] for i in range(len(cols))})

    # reset sequence if table has id PK
    pk_cols = [c for c in cols_meta if c[5] == 1]  # pk==1
    if pk_cols:
        pk_name = pk_cols[0][1]
        if pk_name == "id":
            with engine.begin() as cx:
                # typical sequence name for SERIAL: <table>_id_seq
                seq = f"{table}_id_seq"
                cx.execute(
                    sqltext(
                        f"SELECT setval(:seq, COALESCE((SELECT MAX(id) FROM \"{table}\"), 1), true)"
                    ),
                    {"seq": seq},
                )


def main():
    if not os.path.exists(SQLITE_PATH):
        raise SystemExit(f"SQLite DB not found: {SQLITE_PATH}")

    sconn = sqlite3.connect(SQLITE_PATH)
    sconn.row_factory = sqlite3.Row

    tables = list_tables_sqlite(sconn)
    # ordered tables first, then any remaining
    ordered = [t for t in ORDER if t in tables]
    for t in tables:
        if t not in ordered:
            ordered.append(t)

    print("Will migrate tables:", ordered)

    # clear in reverse order to respect FKs
    for t in reversed(ordered):
        try:
            clear_table_pg(t)
        except Exception:
            # table might not exist yet (if schema not created)
            pass

    # Copy
    for t in ordered:
        print("Copying:", t)
        copy_table(t, sconn)

    sconn.close()
    print("Done ✅")


if __name__ == "__main__":
    main()
