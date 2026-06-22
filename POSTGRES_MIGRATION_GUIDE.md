# SQLite → PostgreSQL Migration Guide (Sellora / Xoptime)

## Step 1: PostgreSQL setup karo

### Option A — Local (dev ke liye)
```bash
# Ubuntu/Debian
sudo apt install postgresql postgresql-contrib
sudo -u postgres psql -c "CREATE USER sellora WITH PASSWORD 'yourpassword';"
sudo -u postgres psql -c "CREATE DATABASE sellora OWNER sellora;"
```

### Option B — Cloud (production ke liye, FREE tier available)
- **Neon** → https://neon.tech  ← Recommended (generous free tier)
- **Render** → https://render.com/docs/databases
- **Supabase** → https://supabase.com

Inme se koi bhi use karo, DATABASE_URL milega dashboard se.

---

## Step 2: Files replace karo

| Old file | New file |
|---|---|
| `app.py` | `app_pg.py` (rename to `app.py`) |
| `requirements.txt` | `requirements_pg.txt` (rename to `requirements.txt`) |
| `.env.example` | `.env.example` (update karo) |

---

## Step 3: .env mein DATABASE_URL add karo

```env
DATABASE_URL=postgresql://sellora:yourpassword@localhost:5432/sellora
```

---

## Step 4: Dependencies install karo

```bash
pip install -r requirements.txt
```

---

## Step 5: Tables create karo (fresh start)

```bash
python app.py
# App start hote hi init_db() automatically sab tables bana dega
```

---

## Step 6: Purana SQLite data migrate karo (agar hai toh)

Agar `database.db` mein existing data hai:

```bash
# Windows PowerShell
$env:DATABASE_URL = "postgresql://sellora:yourpassword@localhost:5432/sellora"
python migrate_sqlite_to_postgres.py

# Linux/Mac
DATABASE_URL="postgresql://sellora:yourpassword@localhost:5432/sellora" python migrate_sqlite_to_postgres.py
```

---

## Step 7: Run karo!

```bash
python app.py
# ya production mein:
gunicorn wsgi:app
```

---

## Kya badla app.py mein?

| SQLite | PostgreSQL |
|---|---|
| `sqlite3` | `psycopg2` |
| `?` placeholders | `%s` placeholders |
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `SERIAL PRIMARY KEY` |
| `datetime('now')` | `NOW()` |
| `PRAGMA table_info(...)` | `information_schema.columns` |
| `conn.executescript(...)` | `_exec_script(conn, ...)` |
| `last_insert_rowid()` | `lastval()` |
| `strftime('%d %b', col)` | `TO_CHAR(col, 'DD Mon')` |
| `INSERT OR REPLACE` | `INSERT ... ON CONFLICT DO UPDATE` |
| `sqlite3.IntegrityError` | `psycopg2.IntegrityError` |
| `row[0]` (index access) | `row["column_name"]` (dict access) |

---

## Common Errors aur Fix

### `psycopg2.OperationalError: could not connect`
→ DATABASE_URL check karo. PostgreSQL server chal raha hai?

### `relation "users" does not exist`
→ App ek baar run karo — `init_db()` tables banayega automatically.

### `column "xyz" of relation does not exist`
→ `init_db()` mein woh column add hua hai ALTER TABLE se — app restart karo.

### `FATAL: password authentication failed`
→ `.env` mein password check karo.
