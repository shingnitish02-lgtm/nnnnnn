from urllib.parse import urlparse, unquote
import os, re, hmac, hashlib, json, base64, secrets, time, html, threading, logging, urllib.request, urllib.error
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, send_from_directory, abort, make_response, Response
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)  # Render sits behind a proxy: real client IP for rate limits
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("xoptime")
env = os.environ.get

DB_URL = os.environ["DATABASE_URL"]
if "-pooler" in DB_URL:
    log.warning("DATABASE_URL is a Neon POOLED url (-pooler): shared-database schemas can leak between apps. Use the direct (non-pooled) url.")
ADMIN_KEY = env("ADMIN_KEY", "")
OTP_SECRET = (env("OTP_SECRET") or ADMIN_KEY).encode()


def _shared_key(url):
    """Both apps share one database, so a secret derived from its login (user, password, db name) is the same on both sides."""
    u = urlparse(url)
    return hashlib.sha256(("xoptime-sync|%s|%s|%s" % (unquote(u.username or ""), unquote(u.password or ""), u.path.strip("/"))).encode()).hexdigest()


SELLER_URL = env("SELLER_URL", "https://supplier-xoptime.onrender.com").rstrip("/")   # override with SELLER_URL if it changes
SELLER_SYNC_KEY = env("SELLER_SYNC_KEY", "") or _shared_key(DB_URL)                  # optional: set SELLER_SYNC_KEY to use your own secret
SELLER_DB_SCHEMA = env("SELLER_DB_SCHEMA", "seller")
BOT_URL = env("WHATSAPP_BOT_URL", "").rstrip("/")         # your WhatsApp bot (same one the seller site uses)
BOT_KEY = env("WHATSAPP_BOT_KEY", "")                     # optional, sent as X-Bot-Key
BOT_NOTIFY_PATH = env("WHATSAPP_NOTIFY_PATH", "/send-message")
ADMIN_WHATSAPP = re.sub(r"\D", "", env("ADMIN_WHATSAPP", ""))[-10:]  # owner number: gets a message on every new order
RZP_ID, RZP_SECRET, RZP_WEBHOOK_SECRET = env("RAZORPAY_KEY_ID", ""), env("RAZORPAY_KEY_SECRET", ""), env("RAZORPAY_WEBHOOK_SECRET", "")
BIZ = {k: env("BUSINESS_" + k.upper(), "") for k in ("name", "address", "phone", "email", "grievance")}
BIZ["name"] = BIZ["name"] or "Xoptime"
IST = ZoneInfo("Asia/Kolkata")
KEYS = {"xt_profile", "xt_addr", "xt_wish", "cart", "xt_viewed"}
PID_OFFSET = 1000          # seller 1 (Shiprocket): buyer product id = seller product id + 1000
SELLER_WIRE_OFFSET = 1000  # the seller apps themselves always expect "buyer pid = their id + 1000" on orders

# ---- Sellers. "sr" = Shiprocket seller (SELLER_*), "ss" = self-ship seller (SELLER2_*). ----
# Each seller gets its own buyer-side product id range so product ids from the two sites never collide.
SELLERS = [{"key": "sr", "name": "Shiprocket seller", "url": SELLER_URL, "sync": SELLER_SYNC_KEY, "schema": SELLER_DB_SCHEMA, "offset": PID_OFFSET}]
_url2, _schema2 = env("SELLER2_URL", "").rstrip("/"), env("SELLER2_DB_SCHEMA", "")
_dburl2 = (lambda v: (re.search(r"postgres(?:ql)?://\S+", re.sub(r"\s+|['\"]", "", v)) or [v])[0] if v else "")(env("SELLER2_DATABASE_URL", ""))      # self-ship seller's OWN Neon database (read-only use: products + shipping ids)
if _dburl2 and not _schema2:
    _schema2 = "seller"                        # selfship app's default DB_SCHEMA
if _url2 or _schema2:
    SELLERS.append({"key": "ss", "name": "Self-ship seller", "url": _url2, "schema": _schema2, "dburl": _dburl2, "offset": 1000000,
                    "sync": env("SELLER2_SYNC_KEY", "") or _shared_key(_dburl2 or DB_URL)})
SELLER_BY_KEY = {s["key"]: s for s in SELLERS}
DEFAULT_SELLER = "sr"
SELLER2_SHIPPING = int(env("SELLER2_SHIPPING_CHARGE", "0") or 0)   # self-ship seller: 0 = buyer sees exactly the seller's selling price

SCHEMA = env("SELLER_DB_SCHEMA", "seller")
BOT_URL = env("WHATSAPP_BOT_URL", "").rstrip("/")         # your WhatsApp bot (same one the seller site uses)
BOT_KEY = env("WHATSAPP_BOT_KEY", "")                     # optional, sent as X-Bot-Key
BOT_NOTIFY_PATH = env("WHATSAPP_NOTIFY_PATH", "/send-message")
ADMIN_WHATSAPP = re.sub(r"\D", "", env("ADMIN_WHATSAPP", ""))[-10:]  # owner number: gets a message on every new order
RZP_ID, RZP_SECRET, RZP_WEBHOOK_SECRET = env("RAZORPAY_KEY_ID", ""), env("RAZORPAY_KEY_SECRET", ""), env("RAZORPAY_WEBHOOK_SECRET", "")
BIZ = {k: env("BUSINESS_" + k.upper(), "") for k in ("name", "address", "phone", "email", "grievance")}
BIZ["name"] = BIZ["name"] or "Xoptime"
IST = ZoneInfo("Asia/Kolkata")
KEYS = {"xt_profile", "xt_addr", "xt_wish", "cart", "xt_viewed"}
PID_OFFSET = 1000          # seller 1 (Shiprocket): buyer product id = seller product id + 1000
SELLER_WIRE_OFFSET = 1000  # the seller apps themselves always expect "buyer pid = their id + 1000" on orders

# ---- Sellers. "sr" = Shiprocket seller (SELLER_*), "ss" = self-ship seller (SELLER2_*). ----
# Each seller gets its own buyer-side product id range so product ids from the two sites never collide.
SELLERS = [{"key": "sr", "name": "Shiprocket seller", "url": SELLER_URL, "sync": SELLER_SYNC_KEY, "schema": SELLER_DB_SCHEMA, "offset": PID_OFFSET}]
_url2, _schema2 = env("SELLER2_URL", "").rstrip("/"), env("SELLER2_DB_SCHEMA", "")
_dburl2 = (lambda v: (re.search(r"postgres(?:ql)?://\S+", re.sub(r"\s+|['\"]", "", v)) or [v])[0] if v else "")(env("SELLER2_DATABASE_URL", ""))      # self-ship seller's OWN Neon database (read-only use: products + shipping ids)
if _dburl2 and not _schema2:
    _schema2 = "seller"                        # selfship app's default DB_SCHEMA
if _url2 or _schema2:
    SELLERS.append({"key": "ss", "name": "Self-ship seller", "url": _url2, "schema": _schema2, "dburl": _dburl2, "offset": 1000000,
                    "sync": env("SELLER2_SYNC_KEY", "") or _shared_key(_dburl2 or DB_URL)})
SELLER_BY_KEY = {s["key"]: s for s in SELLERS}
DEFAULT_SELLER = "sr"


def seller_of_pid(pid):
    """Which seller owns a buyer product id (by id range)."""
    return max(SELLERS, key=lambda s: s["offset"] if pid >= s["offset"] else -1)
ONLINE_OFF = 5             # % discount for paying online
PHONE_RE = re.compile(r"^[6-9]\d{9}$")
URL_RE = re.compile(r"^https?://[^\s\"'<>]+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id SERIAL PRIMARY KEY, token TEXT UNIQUE NOT NULL, created_at TIMESTAMPTZ DEFAULT now());
ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS users_phone_uq ON users(phone) WHERE phone IS NOT NULL;
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INT REFERENCES users(id) ON DELETE CASCADE, expires_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS otps(phone TEXT PRIMARY KEY, code_hash TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL, attempts INT DEFAULT 0, sent_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS otp_log(id SERIAL PRIMARY KEY, phone TEXT, ip TEXT, at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS user_data(user_id INT REFERENCES users(id) ON DELETE CASCADE, key TEXT, value JSONB, updated_at TIMESTAMPTZ DEFAULT now(), PRIMARY KEY(user_id, key));
CREATE TABLE IF NOT EXISTS products(id INT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, price INT NOT NULL, emoji TEXT DEFAULT '📦', bg TEXT DEFAULT '#eeeeee', images JSONB DEFAULT '[]', size_label TEXT, highlights JSONB, active BOOLEAN DEFAULT TRUE);
ALTER TABLE products ADD COLUMN IF NOT EXISTS mrp INT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS stock INT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS seller_pid INT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS seller_key TEXT;
CREATE TABLE IF NOT EXISTS orders(id SERIAL PRIMARY KEY, user_id INT REFERENCES users(id), items JSONB NOT NULL, total INT NOT NULL, status TEXT DEFAULT 'placed', created_at TIMESTAMPTZ DEFAULT now());
ALTER TABLE orders ADD COLUMN IF NOT EXISTS pay_method TEXT DEFAULT 'cod';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS address JSONB;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS return_reason TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS returned_at TIMESTAMPTZ;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS pay_status TEXT DEFAULT 'pending';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS pay_total INT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS rzp_order_id TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS rzp_payment_id TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipped_at TIMESTAMPTZ;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS tracking TEXT;
CREATE TABLE IF NOT EXISTS visits(vid TEXT NOT NULL, day DATE NOT NULL DEFAULT CURRENT_DATE, hits INT DEFAULT 1, first_at TIMESTAMPTZ DEFAULT now(), last_at TIMESTAMPTZ DEFAULT now(), PRIMARY KEY(vid, day));
ALTER TABLE orders ADD COLUMN IF NOT EXISTS seller_sync TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS seller_error TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS seller_try_at TIMESTAMPTZ;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS seller_push JSONB;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS seller_states JSONB;
CREATE INDEX IF NOT EXISTS orders_rzp ON orders(rzp_order_id);
"""


def db():
    c = psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True)
    c.execute("SET search_path TO public")   # shared Neon (pooled) connections can carry a seller's search_path: always use our own tables
    return c


def init_db():
    with db() as c:
        c.execute("SELECT pg_advisory_lock(7)")
        c.execute(SCHEMA)
        c.execute("DELETE FROM user_data WHERE key='xt_bank'")  # plaintext bank details are no longer collected
        c.execute("DELETE FROM products WHERE seller_pid IS NULL")  # old demo products: only seller-uploaded products exist
        c.execute("UPDATE products SET seller_key=%s WHERE seller_key IS NULL AND seller_pid IS NOT NULL", (DEFAULT_SELLER,))  # products synced before 2 sellers existed
        c.execute("SELECT pg_advisory_unlock(7)")
    if not (SELLER_URL and SELLER_SYNC_KEY):
        log.info("SELLER_URL / SELLER_SYNC_KEY not set: products are read from the seller schema in this database; order hand-off to the seller needs both")
    log.info("sellers configured: %s", ", ".join("%s(%s%s)" % (s["key"], s["url"] or "no-url", (" + DB schema=" + s["schema"] + (" OWN-DB" if s.get("dburl") else " shared-DB")) if s["schema"] else " API-only") for s in SELLERS))
    for s in SELLERS:
        if not s["url"]:
            log.warning("SELLER %s: URL not set. Products sync from its database, but ORDERS cannot reach it (stay 'pending'). Set SELLER2_URL", s["key"])
    keys = [s["sync"] for s in SELLERS]
    if len(SELLERS) > 1 and len(set(keys)) < len(keys):
        log.warning("SELLER_SYNC_KEY and SELLER2_SYNC_KEY are the same: status updates from the two sellers cannot be told apart. "
                    "Set a different BUYER_SYNC_KEY on the self-ship seller and the same value as SELLER2_SYNC_KEY here")
    for k, v in (("BOT_URL", BOT_URL), ("OTP_SECRET/ADMIN_KEY", OTP_SECRET), ("RAZORPAY keys", RZP_ID and RZP_SECRET)):
        if not v:
            log.warning("%s not set", k)


init_db()


# ---------- helpers ----------
def err(code, msg):
    abort(make_response(jsonify(error=msg), code))


def http(url, payload=None, headers=None, method=None, timeout=10):
    data = json.dumps(payload).encode() if payload is not None else None
    # Cloudflare (in front of Render) challenges the default "Python-urllib" client: identify as a normal API client.
    h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "XoptimeBuyer/1.0 (+https://xoptime.com)"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        body = e.read()[:300]
        hint = " [CLOUDFLARE CHALLENGE]" if b"Just a moment" in body else (" [RENDER EDGE 429]" if e.code == 429 and body.strip() == b"Too Many Requests" else "")
        hdr = " srv=%s cf-ray=%s retry-after=%s" % (e.headers.get("Server", "-"), e.headers.get("CF-RAY", "-"), e.headers.get("Retry-After", "-"))
        raise RuntimeError("HTTP %s%s from %s:%s | %s" % (e.code, hint, url.split("?")[0].replace("https://", ""), hdr, body[:70].decode("utf-8", "replace").replace("\n", " ")))
    return json.loads(raw or b"{}")


def bot(path, payload):
    return http(BOT_URL + path, payload, {"X-Internal-Key": BOT_KEY, "X-Bot-Key": BOT_KEY} if BOT_KEY else None)


def notify(phone, text):
    """WhatsApp message to a customer/owner. Best effort: never blocks or breaks an order."""
    if not (BOT_URL and phone):
        return

    def run():
        try:
            bot(BOT_NOTIFY_PATH, {"phone": phone, "message": text})
        except Exception as e:
            log.warning("notify failed: %s", e)
    threading.Thread(target=run, daemon=True).start()


def norm_phone(p):
    p = re.sub(r"\D", "", str(p or ""))[-10:]
    return p if PHONE_RE.match(p) else None


def bearer():
    h = request.headers.get("Authorization", "")
    return h[7:] if h.startswith("Bearer ") else ""


def user_row():
    t = bearer()
    if not t:
        abort(401)
    with db() as c:
        r = c.execute("SELECT u.id, u.phone FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=%s AND s.expires_at>now()", (t,)).fetchone()
        if not r:  # anonymous guest token (browse + cart only)
            r = c.execute("SELECT id, phone FROM users WHERE token=%s AND phone IS NULL", (t,)).fetchone()
    if not r:
        abort(401)
    return r


def uid():
    return user_row()["id"]


def login_user():
    r = user_row()
    if not r["phone"]:
        err(403, "Please login with your mobile number to continue")
    return r


# ---------- products (pulled from the seller site) ----------
_sync = {"t": 0.0, "lock": threading.Lock(), "err": None, "n": None}


def clean_img(u, base=None):
    u = str(u or "").strip()
    if u.startswith("/"):
        u = (SELLER_URL if base is None else base) + u
    return u if URL_RE.match(u) else None


def items_from_db(schema, dburl=None):
    """Same Neon database: read the seller's approved, in-stock products straight from its schema."""
    sch = re.sub(r"[^a-zA-Z0-9_]", "", schema or "")
    if not sch:
        raise RuntimeError("no schema configured")
    with (psycopg.connect(dburl, row_factory=dict_row, autocommit=True) if dburl else db()) as c:
        rows = c.execute(f"SELECT * FROM {sch}.products WHERE approved=1 AND COALESCE(stock,0)>0 ORDER BY id").fetchall()
        imgs = {}
        for r in c.execute(f"SELECT product_id, url FROM {sch}.product_images ORDER BY product_id, sort_order, id").fetchall():
            imgs.setdefault(r["product_id"], []).append(r["url"])
    out = []
    for p in rows:
        images = imgs.get(p["id"]) or ([p["image_url"]] if p.get("image_url") else [])
        hl = [(k, p[col]) for k, col in (("Brand", "brand"), ("Material", "material"), ("Pattern", "pattern"),
                                         ("Occasion", "occasion"), ("Sizes", "size_options"), ("Colours", "color_options"),
                                         ("Country of origin", "country_of_origin")) if p.get(col)]
        out.append({"seller_product_id": p["id"], "name": p["title"], "category": p.get("category") or "General",
                    "price": int(round(p["price"] or 0)), "mrp": int(round(p.get("mrp") or 0)) or None,
                    "stock": p["stock"], "images": images, "highlights": hl, "description": p.get("description") or ""})
    return out


def fetch_items(sl):
    if sl["schema"]:
        try:
            return items_from_db(sl["schema"], sl.get("dburl"))
        except Exception as e:
            log.warning("%s: schema '%s' not readable (%s), trying API", sl["key"], sl["schema"], str(e)[:200])
    if not (sl["url"] and sl["sync"]):
        raise RuntimeError("%s: no readable schema and URL / sync key not set" % sl["name"])
    data = seller_get(sl, "/api/public/products", timeout=25)
    return data.get("products")


def sync_one_seller(sl):
    """Upsert one seller's feed. Hides only that seller's products that disappeared from its feed."""
    items = fetch_items(sl)
    if not isinstance(items, list):
        raise ValueError("bad feed")
    ids = []
    with db() as c:
        for it in items:
            try:
                pid, price = sl["offset"] + int(it["seller_product_id"]), int(it["price"])
            except (KeyError, ValueError, TypeError):
                continue
            if price <= 0:
                continue
            mrp = int(it.get("mrp") or 0) or None
            imgs = [u for u in (clean_img(x, sl["url"]) for x in (it.get("images") or [])) if u]
            hl = [[str(a)[:60], str(b)[:200]] for a, b in (it.get("highlights") or []) if a and b]
            c.execute("""INSERT INTO products(id,name,category,price,mrp,stock,images,highlights,description,seller_pid,seller_key,active,bg)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,'#f3f4f8')
                ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, category=EXCLUDED.category, price=EXCLUDED.price, mrp=EXCLUDED.mrp,
                stock=EXCLUDED.stock, images=EXCLUDED.images, highlights=EXCLUDED.highlights, description=EXCLUDED.description,
                seller_key=EXCLUDED.seller_key, active=TRUE""",
                      (pid, str(it.get("name") or "Product")[:200], str(it.get("category") or "General")[:60], price, mrp,
                       it.get("stock"), Jsonb(imgs), Jsonb(hl), (it.get("description") or "")[:3000], int(it["seller_product_id"]), sl["key"]))
            ids.append(pid)
        # anything not in this seller's approved + in-stock feed is hidden
        c.execute("UPDATE products SET active=FALSE WHERE seller_key=%s AND NOT (id = ANY(%s))", (sl["key"], ids))
    return len(ids)


def sync_products(force=False):
    if not force and time.time() - _sync["t"] < 60:
        return True
    if not _sync["lock"].acquire(blocking=False):
        return True
    try:
        total, errs = 0, []
        for sl in SELLERS:                      # one seller failing must not hide the other seller's products
            try:
                n = sync_one_seller(sl)
                total += n
                log.info("product sync ok: %s %d products", sl["key"], n)
            except Exception as e:
                errs.append("%s: %s" % (sl["key"], str(e)[:200]))
                log.warning("product sync failed for %s: %s", sl["key"], e)
        _sync["t"], _sync["err"], _sync["n"] = time.time(), ("; ".join(errs) or None), total
        log.info("product sync ok: %d products total", total)
        return not errs
    except Exception as e:
        _sync["err"] = str(e)[:300]
        log.warning("product sync failed: %s", e)
        return False
    finally:
        _sync["lock"].release()


threading.Thread(target=sync_products, args=(True,), daemon=True).start()


def prod_json(r):
    d = {"id": r["id"], "n": r["name"], "c": r["category"], "p": r["price"], "e": r["emoji"], "bg": r["bg"], "st": r["stock"]}
    if r["mrp"] and r["mrp"] > r["price"]:
        d["mrp"] = r["mrp"]
    if r["images"]:
        d["im"], d["ims"] = r["images"][0], r["images"]
    if r["size_label"]:
        d["sz"] = r["size_label"]
    if r["highlights"]:
        d["hl"] = r["highlights"]
    if r["description"]:
        d["d"] = r["description"]
    return d


@app.get("/api/products")
def products():
    with db() as c:
        n = c.execute("SELECT count(*) n FROM products WHERE active AND seller_pid IS NOT NULL").fetchone()["n"]
    if n == 0:
        sync_products(force=True)          # first load: wait for the seller feed
    elif time.time() - _sync["t"] > 60:
        threading.Thread(target=sync_products, daemon=True).start()   # refresh in background
    with db() as c:
        rows = c.execute("SELECT * FROM products WHERE active AND seller_pid IS NOT NULL ORDER BY id DESC").fetchall()
    resp = jsonify([prod_json(r) for r in rows])
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------- pages ----------
# ---------------- Site icon (favicon) ----------------
# Embedded in this file so only app.py has to be uploaded. Shown in browser tabs, bookmarks, search results and on phone home screens.
_ICON_B64 = {"favicon-48.png": "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", "icon-192.png": "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", "apple-touch-icon.png": "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", "favicon.ico": "AAABAAMAEBAAAAAAIADjAwAANgAAACAgAAAAACAAggwAABkEAAAwMAAAAAAgAPkYAACbEAAAiVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAADqklEQVR4nE3MW0ybdQCG8ff/faVFcOMwDmGcdgrioRwExowgGZ2LSJgQ0jGjyQ5hETSS6TyQ6MXiHAGX9GZmYGCwGXWkMwXMmLABwWbMAUqAKRiGsxQ6KHSDQoG2ab/Xm5l499z8HgEAAIUEQUUIQD+dlTZjLE913NOFbTjjfGFb4YxJmLMFx/ZOyc82zbW/OiwBUEgBCAoAQgCkkersUydrM+3myhLZHpiZ/QJCDuSDKVq4wiIx4wJ6BmfcHUPO+v7uqmpJCK9CChgBGe9TUxKwr7MOwbxbVMzHvwz63KTiI+knSVIh6SPJe6MzLCi52AlMaYzQy4Ako0x+0XAuYjfH2657NkhFeSL8JPnITnpcpMdF//k6hZ995Ln/eQ2zM6sNQgBif8j+DK3kuJt9pUV4g2KlbWqNyMwIQxQ2ILdexaiyDQF5+XD23sJa189YTUpiVuHLyvDILA9/eH4f9NjR2G5opYf0Ha34hoiqZ8UHXeTxYn6RX0lNTgfVhyaIA2Yi7zvmHmtm/7jNt25dYOEefaOU9Hxafs7xYkqrTulSnR61BRacrj8CU3Qx3rh8Fu/mbcI/dQ2dJXYMlnpwLCcBiWGBUlB0JLPTE/KlmJe0sUH0iNVLLcLS048Td37A5RQdSk0BsEwrGJ2YRE7iOgqmexA59juWNoIRvz1UrHspwkIDYiWxPQqPTCb4a2qw2NYGxzkDDrU1IcrbgbL3GrHsUvBpbgxEyFZcdGuR/FwkZCHgdm3CN2eFyvHAalMVHtn5d3go4kd+E+rNdcSb+/BjbjDuL4xhMzwe89yFd5aj4QtX4eDeKDwWgu47ZixOTNpUFsti38qavzziRr//168bZNvIGDwAvItuyK/okJydBuP3ZmhcTtQ2nQK2bIF/flWZPnNGXnsmo0+KO1recPNKhy9cSNC9VcoTH5+EKiUNysIsElKTUVy0F1q/BUmLoxh1qqHMO2gt0sG8EeJ7rbuxAbJaha++bDW0ftLAlVW3x00qQ6NWGhq7OPbHLBeWNmjsm2TLtQHOfmtU2qNiPGcj0tlsHDBIsgD00MskNRcudHc2nG7myM1hLi27fX5SWfOT1lkH/zFdV269fthXgadYpX2TV01DnSQ1er1eFiSFEIIk1T/dflj7542BSpfdHriy6IDNugDng7+gdtnwdOxOpJeVuouq3q5PTQyuFkJ4SQoBAP9NnnRW121ruWVoXLf80BKnEl7E7N4zl3owr1e7K6RJCDH8f/MvbaT69nWbezwAAAAASUVORK5CYIKJUE5HDQoaCgAAAA1JSERSAAAAIAAAACAIBgAAAHN6evQAAAxJSURBVHicjZdpdJRlloCf9/uqUpWkKjtJZElYEiEsDZFFBEQhI0YGdECDtKIgIyK4jYDToj2NNKOtoqKjjoK20B5FTGS0FZVRwSVpZDVhXwJZSQIJSSq1V33L7R+K3ePRmbnn3HPeX+/zvPe+P+5V/HIoKNcUcywdKP5cMpsrmZoQqJ+SGO8pNkM9+WYknqK7HLi8Hn+yN6UxO9FdPWZU/perr2enU6lOE6CsXKdijg3IL0B+LlZpsNrWAWt2TyG+XXf18Z8oKwycyh8YbmBAoJGc4HlcZhgTRUdSGi1ZA2jNKuBUYj8kvaAxf9SEijVrCjeMVarWAlatEm31amX/PwTKdI0Ky14vTl5+6+HBgQPLSn370oYHjtLf9MlQsHNA6akexciRiiFDofclQkqyRJy6NEVi2p4Gn/roZJxD8X6+wmtvf27bo72eVEoZZWWiV1Qo63+pepmuAfzqzwNcfZZ/c3/KlbLe4ZX1YHwGdocnWczZsyW68U8SqW8Uw7TEFBFTROI/pBExRLr9dvT0KWPb2hfkhsnLZcjcHd+UlcsAdbElv1R2HSD/5aJfpd98Zr0zX/5AgvEHsA9kZor/wWVyYe8haYuIBEXEEhHbtsU2DLHjcbFiMTFjcYlFDQmG49Lli0gwbNpd1fuM1TOWSNqo/ziT+3spUgCrVmk/gYvSAAaW503wzKp7x5Eti9CNl3BI852LpGn/STneJXIuKGLGDBHTELGs7wVsS2zrb2lalphxQ+IxQwKRuFwwRIxQ0HhjwTJJG/ZM3cD1kvc9Un5svyqnTKdUXGO9N1e95+wjs8DYNGy4NHzw3/Jdm8iR5pgEeyIichFq/+31liUXwxYRy7bEEhErHhOjsUmitbXSVd8o0tZivHHzfaL3X1VVKuKCMh1EUQa6BvTNuOu377gKZDa68dLMOXLwaJtUNYnUNQclHjfFtm2xrB/Apvkj2LRFoo1NEm0+K0Y0JpZli9njF+OW2yTgTZNOkAsgdemZ0v36K8ZD0+4VMh79raYBlOtKQKm8/xzwu86NB3tCNUnhWUvUklefV1mpir4OG6VrgAVoWDZoYoOuI0Dsix04d2xHhhbhnDAeBgwCLYHIHbcQ3bSF1gfuxeXxYohFVHOQveQ+cZ+slZJZz4YPjlg0Uqpm1DsUyJjIl0v7hk579o0rsx7f8Jx25vhZ1pZ/R2qffjgSU4jpXmaMSeD68WlY6KjGRsynniGx+QyV1y+hq2AS7pCLhOMmgY5zHD3rxfPgC+TdOg+HbtPdHcDjssl1JqnUqybav5n5ieeWt99equtqhWL8roz7D95T43bF+07a+RlXDc9VKhpj/j2v8/7WBhgyATyDcHncbH2yL9PPfYOx6F4Sigbx2rQlLN3qxFTZkJqDIz0VOx7DtiNoF05D5wnstgbGjO7FFSNzWHn/DNw5vcTe+SXXzn787IERa0Y5PK1vTU2LdPXtt+JhJhf3VrY/TprHRcWmxcwIPsH2v3yFfsl5HFYKwX9/DbatJzp2LC9OupUVj1eBKwcuCaAcIcxQAlxoA18zttkJgQ6GjcziwUVXc67DzyV9etHaGVaZo0dJaVFm3wO710115LQfL+nTu7caf8ccSyK2nuRSiKahggZvPr+QybN+R5u/mm15fiZ98j7tg4fjfHMjudWtvP50DkkpHl5+81v+croTTRk8umQEBUUjiEQN7LjBjHH9ePqlr5lz/QgAEsTCSE+3J465VFf7Py9xpEd9xYWTJ5HaL0NJIEpCupvgkSOEDx8h+9c3U/7OKvxzb2HiJ7vpKhzE0uG/59IvPDyxcAIAtmWy6oWd0NnKkJG9eHhQjKR9FeBwgdPmUN+bqKvvYeLlhcTjFg5dI2qjBgzJI5VoscOFkZ8zdjiaiOZOc9O9bx+B9evps24dcX+A3EceYnD1bhoH9Gf1+HvZ+m0UGtooyI2xcHofNpXvovZ4PUSCzCkZj3H+HKd3HcBhC+7iwax9tYYpEwvRNA3DNACFYaGlZCSTnWDna7i1FG+/Xjh1RaihgeCvbyVzUAExI87+G25g99at1BQW8tnilWzc8jlOYw9aTw33/K6Sqv0tfPhRJSrYRXKvBKYOySD8/oekp3hJigfxjZzGrlMGc2+4FABNKWxbsAFnLEqyU1K0mNuNLTZODc49+wxZZ2pJaG3l4Bub+Pyrr6jLySV38xYWrLiTOdcVYNRV4gzVEO9uYeHybWSne5FYD+OG9ab4fD307kc4apAy9wae3eNmSFaUvnk5xA0LAQwTlIJwQyMaOlpcJfhD5ztRAh0H9tPkcKC2fUhxWiqlTz7F9IoK8sZchkPXeP295xl79XBiDTuwg8epPdLA3iYYPfMqZs4pJebrRuJRvH08HIlm8mb5ERbcUggoLNPGtISYrZMQM2itrERlZvkdKjO7sflYbe5AhZ045nKtYfceYg2NFKxZw+UTJ0FXF0Z1NdI/H29ODu+vvJWPx+ajWTo6CUQutDCusBcjkjuwrryC7pJ/ZK/Vl2Urd9HbXc2MGfOwLEFQxCJxJCkRx/6v7NO7q7WUqbc1OtTgwurDh49dPjUYl/6PraYpFsH37hZqmppIbtpMCpAMGIChIC07nQVZGXS7krmQ3ouWxCwqdwd4eX+Uc5zjTGM358+F6J+j8VbFIhITPUSiJoYphGJCYjL41j4hJ203yUWF1Y6S+bft2PfQvYt9Vd9olPwDhS++wrm776Ox8lsOHTpKW3Mbre0++uRmkDdsKG98dhBnTi7ejAw6ScKZ4CXDm4o3M42v39uJ4ffx1GM3smxxKeAgblhYogj5I6gsL9rbf+TM9i+0pmEzZPpNpTuUiGSUTltRc3/iub4jNm6i5ehJFQ0H8ftCRH09hH0+/D0B+k++glffrmRuSoi6rjh7Enqzed08PIk6zkQ3T7+4neaqGqYPSeTZI25WPnQdV4/NIxy1iASi9CR5yT28h2Mzp8mBbjhw97+d/eMrK0Y5EnTV9cyHp8o3L7xr+SvffGqpKaW658QhHBdacZ89hv/wIcx4kA/cGtnhZm7/djOB/IFM815L1BDSM5KIhCK8s72Kze6TFG34mL1X3s0HX+dRMr4/fl+QQEoKuYcPcHTujXR0h+x9g+foNy26sdyhqS4AJSID71z2VuCJ3NFWsLHVPtEl0h2ISyhqSDQcETMal2Mn6mXcxDvkudvuk/nX3i7/NG+1hMMxCQQiYpqmLHlwnZSV3C2vLFgqxePukI8+/U66wyJHfSLtm/9L9vbKkj+j2w9QZD2ydmtARAYCylEu5ZpSqi4o8tSco01rLr1pgTnl462O83E3uW4LEl34w3FsRxpXX3MN7+4+hEYS868t4cjpTsS2MEwYN34Cr53oZuPZEKOvuIzeo4ppb+uh93OrOfzyOk443HSSZXXMW+h4fsXsp5RSdVJeriMiqqy8XBcRV01joOqKEcvlo6mzje7WTjnaIdLeHhZ/IC6dvqj4gqZ0+eLSEzClyxcVX09E/IGoXOgMSUurX1ouxKQ+KNIZEQm+tUW6hg6R9ShZmdRb1pBjlE7+F6lr66gSEVf590z1w3z4/UFE8r6rD9SNHrdaXhp5nRHYt19O+UXq2k3x+Q0JhgwJRwyJROISjZoSjVsSNUViIhIVETsQFNm6RUIlV8m7JMosR3+5x1sk9zPAuGbqI1Lb3F4n8v1QepH542QqIppSyhaRovouc9vih7YNTN+2yXz8N1N198wy1eZMJyXRQYbHgccFDg1Mwybc6Sdw7BDRnTtp/+RTvjzSztdaGsqbzBjfeam1U6yUB/7V8ezjs+syk50zlFLHL7L+h8APErpSyhKRAcCf1lS0XLnlsVe5RjWYl43N19szs1WLJNET0WkPQltHgO6G01gtZ9DDPTiTnFyqRxnoPy89tmU1Dp3umLd2Of88vaASmK+Uqr/I+HEk/+l68qPE/vVORt/18Inz5rJX3z2atrd8O9mtpyRbfLbXDquwP6TikYBSWLgwxR0LSQ9R8ZOtdY+ZokqW3sb8Gy/zXZKiP7dhw4YnFy9ebPwU/rMCf9+OH86FwF0NHdGyj6vq82sPt9FYc5TOlgbkQhNihNC8HnLzCxgweSLFYwdTOrGgMc1NBbBBKVX70zv/T4G/+yTaRWMRyQSmAlOCcbM4GLXye3qCKZquSE3z+FOTEhpdUA18CexUSnVerChgK6V+dj3/K5fD8Y//FkoKAAAAAElFTkSuQmCCiVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAYAAABXAvmHAAAYwElEQVR4nK2ad5RV1dn/P/uc22fu9GEKM5ShdykqiFIiijpiH8SSGCMaKWLUJJYEu8bYoiaxkWJ5YwGCRimRWDCJIohUAXGAGWD63Lkzt5972vP7YyDB5M0bXev33HXWXveevZ77/exnt/Pso/jGJorpG3Q+nGkDhLzwo2elJuZlVFuGE6MZZ1xPIj0wY1glpmGGfBoU5/rShXn+SFVBTsOYIewor+DTOTXsLgipg7FMr9e7PvjAc/eMGY5SSr6JGvWN6tYt11gx19GARQ/ES//U0HNexrXOMFOdk+1Ec/+ceAt58Sb8yTb8ZgLNsXC8XpL+fLqCRWR8xXj9RfQJ51MxZNihE04Y/ckpI7x/mTeFt5RSnQB1dcv15cvr3K8L8jUBlutQ5yqU1F72RcnqrugSsp3zcnsODKmObmdKdDvDjCa3WhJSphwKdaU8HqUQQSkwNY14KChdBcXSkFfGLq1A/TUa1HqoJmfgSYyedGL9uVOHvra4lqeUUhEQVVe3QluxYq7z/wFguQ5znbvqlvvuOxK62jXab6uMfT7gzPYPmJU+YJdraZRSWtjj0YpQFGYzBAENoLAYVVKCFOTjFBbhhoKokB+zqIBEYb67p6vbXbX9CO/vF097xUxOPvP8xgunD3toUe3c3yu1wqyrE33FCvV/QvwXgOW6Yq6TO+nlYYl08hFfon7O9V1/YbZR72SVUhGlaWFcRtgZBgG5ACOGwclTcKdMgdGjYdAgKCv7h0cXsLt6cNpa8WWzeO0srYcOuC+vfFueXdukZ6cuZtb5l749Ldz8o/nfrtr3dSD+g030akBo6C/Opur25hnhM+VdlWu+qoecH3sL5OfeXHkXpAXELS4S+d5VYr61WhJNTZIQkayI2CLiiIhj22JnTbEMU8ysKabtiuGKJExXotGkJNpiIl1xaf7z285t46eaVaN+KCfceLD50jtazgaYeN0W7zcUf51XAfS/93K95LrUnZ5hshm/ebOWL9/3FMiLKNkKEhk6RLL33i2JLw5IS8qWFkMkbojYpiVuNiuuaYprW+I6jjiO01tadi9I2pBMypB0MiPdXXFpaopIa9QQqz0iHy66zhxfNEPCcz5Ijbnm8OW97fm1IZbrOkDlXfPyCq5KL1fl7hoVsuZ4SuRWgrIGpL5moMQffkQiB1ulPiGyr8WUzs6M2FlTxHFEXFdc1xHXdf/9chyxbUcs2xHLtCVrmJJJZyWRSEtXe7c0NEclmhJp/tWT1rl5I1zfrLfTYxf1zAOgbrn+X8VrABVLZ+fnfSf9rip1X1A5zjRvmdwL8p7XJy0/vEXav2iUzyMin9UnpbEhJlnDEhER13XFcY6JPR7A+WrpHAVxHLGPRiSbyUo6ZUiqJyGtzR3SbopE//Cyc1necNc3c1V6wo3ts3sh5D9B1OkKYMjjNTn51xz5i1Yhz2the7xeJg+AfDJlsrS991fZ1eHIx18kZeeeLolEMiLiiogcFf6vrX7cd7u3C7nOP+87riOObYtzdLxYImKKiCEi3UZWOkWk509v2RfmjRemvnpkzIJDNf8aiaOzkKg65mpHqib7NmUOrXgx9k5tnttk3S553oulgwuXLCb/1vtoMUP4EzFKC/30KQ6QE9QR1wWlAPmqSwRcARHQdVDqn3ekt664LkrXkc934XywAbelFTsWw9E0CAZx/D6YcDJ2wz6r9u7V3i0jrl1zyQBf3QowWTHXPe5P63r7fZ+fzl8cOFV2aF5rqOorN/iKZOOvfyubu0TW70zKzvqYNEdMSWVscd2jA/N/6+e2La5jyz/MNMVpahJrf7040WhvHau325lffCHG5Cki/QaKCZIFMUCSIBmQ7SCH9+6TrQsXW0WhS8R72vL52nFRUIASIDz0D8XVbau2rUq+0/cHKozt96vbnn2KnAvmIK0p+hd7Ced4CXnB09vZcFxBISh13HLiOCiPBxHBamrCePNNMn/fiF1TQ2DySeSMHoWnqh+o3gCl5l1G+M0/si1cQHTECDziYiqFoRTpjEHBrNMZf8/9FHd2yCOzr2BpbFLz0Au+P/7L54d3HQ3AdI8CKF1w73P+kbJMC9t99SHy/JNvyXudIuu2x6Ux6khn3JLuWFaiPYZEu9MS6YyLY5riuvKPKdK1bXFFxGlslPQLL0j8W2eIXHieyN8+kK9ab3T2L3tS6kH2VxfLjjV/krSIpI+OgWNjQkQk3pOUNluk8+ln7Yn+WcKIR+7VALjLowSU6n9X2eierX9/Kf23mvlWSGbecrc27yfXUqq5vLJiE889t5lg3yqUL4w3t4jcgnxSKofxVSa/W1SJ0nVcFErXcNesgVVv4nz8Cb5pJ/PnuTezrzsHHQfN48Pn9xLwaWQzFg2bNtHZfJhQaTFDJ01E0ilcsXFxaWuJ4vdaDO5fSN15pxDJ2BTbWff12ivVVXvKDoYmTTs1/eHV7R4dBKOtdq60DdpkGU545hXawp9+F5+Y5Pp0bvz2RD7922beWPUuDBgOnjII94d8Hzu+8FDiO8K9iwbhMTJo9/4c9YdXMCNRAt+5lBenX8H3bvgbrn8wFJZAbgnk+QkV+DDiJm5mKpijYH8LrNkCTjfoGejpYsyIIqZOrCQU0PAFfHh7UkSLC9W5l5/pjvrhqkG7O/rXKvitNn7ic96wnTrzRLOdDeFh7g/v+YEqyvXgU+BVEPR5eenpa5hxWhG0H8Svt6NlvsQb24k328SjLxxk3fKP8C5ZhPz8Mcx4gsDi+ayq/R4Lb3sX3Z+DvyCLLj2obCd0d5A+0AqRFrzReryRfXizh/H6Y/j9KfRUhPJAiud+djFdXWnmnDUB2xE8Pi9WylRF557rzu0j0Np85rPXPef1fNZ5uGqmE5liGAmqL1+oTzttMPFIloIcLz4vOI5DTiiHV3+3mFnnLGV3w048fWtw0t24NLB4QpJTn16Ps3ELyXAe2Svn8erI07n2kidwPH3APAzdESgpg0ACpXlAKdx4DDcRBTsJZhycFFhpNCfBspU3UX8oTiplMHxINcm0ie7z4kkZJCr76adPGcgDbzZMWbJ3TJWHVOfooVZ3dU+41D1j/iXKMoSAV8Or926JlQJXKcrLynl52RLOuuAOOg5F8RRXc9eUAHd2bIJtu8h4PViXnYtv6Z00L3ubRdeejD8/F9tSOLbJq+u20WkPQfP5cFJpTh9lMH54OUkjhKIETVPYmQwnTxrM2adPYPyMB1l6y+mI9OpwAR0hqXvV4Knj3WGrdlfvaj00Giquue9ONUTWnHu1vT8t0tRpSjztiGH2rpIiIult26Rnw4ciIrJu3YdSNfo8eeTCa0XGjBBbecUE2Xr+JfL0q5+LaZjyr7Zz9wEpGPpj0YbdL9qohyQw8Cb58lDrv9U7Zi+t2CZVY5dKJpMR13XFMCyJJwzp6IhLY9IVWb/enusdK5RecZ/H42bGFopB5RnTlK6DplwUOro4aLqXztdfJ7NvH5ULF+EYBmedNY3Vn29l+AN34/bEySIcPu9cHh24gFfuNegxmrn9qirShotC8Pm8vPj6J/S09xCs0jFaIsw6dzgVB/cQvfh88iorEaVwm5vo9ujkrVzOshe2ctEZwwkEApimDQoEhVIK27SholL1K1CQjY3VfJl4TW6On8JxoxETfLrCg4Pm89K8bBmx116j7MYb8ZQUowIBWn75S/r95MeQSGAq4cgFc3ig5kpeeeMQer7izre9rHyvg1DQh9/nI9qTZN27O1E+F9dMILFmvn3ZaSQQPncsjnRHOdwdZbuu0zqoHxt3xvjoo2auvOyko9uOf24YBIXruBDOpTJXA82s8bhWuiRUHianslQlHNAQ9KCPgy+9SOjBhyi56078+fm4wOGHHyZ2663kajo9rkvradN4rupM/ufld9FKBoF5EKctxeJfeijNc5l+UhV/3biXPZ834ivOxexspXpYCSdXBKnY9BmFixfj+gPo7W1UohG8+QZOn/cOJ40qYvSYvriu9I4NRzi6fUKJAkTlYEFAlXhc3Qn6C3Px54WImSbBYj9H3n8f/bafUqQUmcoKXNvmy4ce4os770RCQfR0huC0GRQ8/ivWX3QrpC1URnAEdLub9m7Flbcf5v3fzuK119+HbBpd+TF72jl/8ffwNR3gr9cvYHAwiMrJIdLdTd7wGpqHnMb7q+t5+MHhBH1BsqaNpjREXFwXXAfE44G0gUol8Qa8QY8EgiRVBqXA71EYjkXXffczobUJu7QMz7Zt7HNd1ixdisej46YzFE06mbNe/h/K+/Xl+adu5JJLf4Ib19GsNI47Bk8wh6Z9URbcsZ6Th1Xg1WzcZAJvboizTxtJRb8w9ttr0T0anu4e+gSDFJ4zg8WLtpHjS1B7xmBEQCG9HwFXBNsBLaBBZzvp7gTBQUE0X35eJhqNo5IJAnleenbsILlrF12ahjcRR9/0KYVtbYy6624ShcV4x4zj3BXLKa+uwEpnmHP+GTz22BLsniY0I4onsRXJJvAXKt57bzeRZJAFC2vJdhxhwvQJjO2op3HMOAquv47ADYuJX3EF+v138OnWKOtX7mfqtFxGjuiP4zhomo4r4LjgisKyXPwBSG/fQsQyyc3Py3jCxX0iTXs+K/d0x0TVoDJdETJGhkZNgZGhz7bP8Ew+iVmTJjD4/vsJnzadkgH9EMAbCmI7DosXXUlDQzOPP7YMCEJ7G07+CaD7eP6xP7Po9tMZ/61pTJ05ibKzp7HrV89gZw1cw0Dl+sgOG8zPnm3E7PmSyy+eCXhwXQulNBxHcBzBtlxszUuuadKzdq10EFJFffIinvLBAw82bnZGZxsakfHjCY4Yia+wCLu5iZ0BH8MbG6l8+GG02bMZ0rcap62NdP9+qOHDUeXlEArhBoPcc+8Swn1LOdjYiTcQQFQIjyeIk86gJWM8esP5DMzRUU3NnDBlEq5P4RT1oc1TyoMvNLPmjdVUDIhSe/ZJiAiapuE44Nq9Y8s0bAJFubib3iP68WbacgYycED1Qc/4SaN2bn0j/7yurZskNOdCivv2o/WOO0guXEDQMGnQNaLd3ZS+8gp54VwCJX0IauB4fLihIFpREYTzyMnN5e7SYvACGQuyaTAykEliZVJkj7xFoqSUvXl9OJxfzRdSyvb2HD7amaVhezOkt/DzX11BSUkRlmUDGpbjYjmCZbpkHagMQuMzvyaSSElbdR/OOWH4Ts+YytLN7/cfxJ51G9Spt1mSTjhq7ILr2VNcTPfPHsSzYwcdIrQCeiJJbiJJEAjoEABSDthADmACaSAb8OIOqCFeVUNnST+ayio56CnnSzOfpkNeMtE40UMN2O2tiJVg1NAgdy29mrq6M7FtG6U0bFuwbcFywUiZBMrDqHfW0vH2ajlCrspWV3HSoLLNSkQGnnLZox+Of+3h6sc2vOF2nHSKVmSa5Ob7cXDp/OxTYh99TGzX53QdaaK7K0osmSJlWkQyDoPGj6F46BDe+Xg3KV8uQyePpyHjZVtjkva4SzppE7J0PJpOTn6IkiLFjr9/TLKrmSu+XcuPb7qQQcOGkBMK4TgOIgrHBdNyyZoumYxFytUYEs6yY9YsUp995r7CAK19wZIjq35903QP0DT1tEkb160ZWN39/DOOOmWqdqSxm/j+JKmMSY+dS2vNTNrzJ9HZ2U13tIdUMktXd4aqfA9zb5nLHXevYrOrM7Sygj/8sZ5Rlblcd8UU8vNyqOiTR2lJmKLCEHkFedzwk7Xs6z+WybVz+HO7EH6rhaduG45lOyh6ZxzbcbFswbRcUhmXPjU5GAtv4sstnzHQE3C2VY7Uvjdj4kagyaOUsjo6utev/1Pt3JdWPqXduGCLdAwcqUJGBp/ukpuM0ddIoXuzaH0sKPRgmQ6phOKkS2v58ROvc+DTT9h23UmURZpZO3o0C1YeYtqMUzhhRJ+vJG4efeIt1q7fytpFIxnXXc/OMdXMenkH/Ur93L7wW6QNCxFF1hKyWZtELEvuoAIKn3yc1c8+R7nPL++YhVrBtFO5rm7aeqWUhYgoESl/8MVN+wdq49228853WtMirY1dEtv6uXS9tVq6nviFJG+8QWLn10rLmJGyKzco7wc0qX/xJek76fuy7LYHRcqLJQPiXHSWjJl9u1z3o5fEtGxJJg3JmpY4ji0jJy+WBQseEJk8VuIocaeOk2uuf0wmXviEiIjEEqZ0xUxpbk3J3vqYNJgixrLnZLPmkWf1oGzx9nEGFZztrnxny34RKRcR5dmwAX3mTNVmpbKvrF53/dI7X7vFfW75C+y86LsU+EP4+tfgHTcRNxXDE4+Rn4gT6IlTLg6+aTPIfXwNX2YH0D7xBLZ/9An5406kZ2Uj/c6ciNejg+ui6xqapqjIVxyJpmieMIm9u/ZSMmkyB+vbqCiuACBtuDimTcLR8VbkUfjEYzTccSvvi5c5/hxuzVTIlKsu0i4+c+IrSqk2EfEci4ASkZKPtzYd8Vdd7a4qqnDNT7fJllZXmpsSEk2JpJx/ZpzluPLJp16S3KLZ8tO7fyNPP/S0zDhniQwZfZkcbu6STMaUZNKQeCLT+yyx9q8SLDxbrr/pGXnu58/Ild9dKuGKC2XDxnpJJG052BCTvRGRA4d7pH3+96Ue5FE9IBu8efJLbYRbOvQqt+FA8xERKTmmWx3NlOlKKUdE5v/61d3Llly+0N5zYsxT8vp6GrwFlItBIBzAr4Ouges4xJImbVGDzu4UD93/DFs+3YMvGMTKJPjBbQv51vQJ6MrF59NxHJdUysSwhN/9diXr13xMMD+Mmejgivnf4ap5s/GHg4Qq8ght3ITv9iUktm7mLW+IEtGo9lRyerbMXv3O457aMyZdq5T6zTHNxwAUvU9uPmDFtUvfq337/tutzTMD3tALKzioF1PmZAjlBfF5elOEpm0Ti2fp6EpimA7RSISskSW/sIhAwE9uyENe2I+mFI4jZLIWXdE0liPEe7rJJNMUFBRQVFpMbnUhpY5J4JeP4T7zBJ2pFC958wiKh1pPAbONEusHD9/svedHl67ZuHFj3ZQpU0zgq+doIr1ZXxGpyWayR866+nWpZKp9aMY0ie1rlM3tIvUHe6S9y5Bo0pZUxhHDErEckazpiu32HmbYtojdm2EX2+7NVtuOK5btStZ0JZU0JRm3JJ4V6RaRuOWK8dqrEh83Rg6DrNQ0Od0/QG709ZOPggOlDyfa1yx8XETkSE9PT83xWv/NjoOYHY8l0jOvWeGG1XnOxrETxPrgXdkWE9l+ICWNhxPSHMlKR7cpsaQlqbQlqYwlRtaUrGmLaTpiWo5kTUeMrC3ptCXxmCHdMUMiWenNOke7xfzjm2JNO1VaQNaAfDc4QCpzTpSf+QfKusBg8TPTufz6x13byqYtS2b/n+L/F4h5lmGmL7ntfZe8xdaTfUaL9ejPpCWala0Rkf1HDGnpNKW505DWSFY6olnp6jElGjOlO957ReOmRJOOxCyRuIgkRMTc94XILx4VmTpZmkFeJiQX54yQcMEsmRyeKuv8VfKoNkrQ51k33rPCPZptnPe1xB8H4T1aXi6mmbrv958Lo542zwqfLXtqayX9zjr5eG9M3t0Tk91HDGnvNiWZFsnYIoYrYroiWVckZbrSHUlJ05bd8sWvn5e9F9TJ9qpq+SM+WaQNkgHFs8RfepFUFpwhv8oZIu94y+VUThW9+mbz1T/tEBFJZbPZy4/X9K/2H08pRcSrlLKSSevsnBz1m027U5VzH9xkHf7gQ/1m2azNPaWcjsFj+EQr5bDKQXl8lPg95OgatgmxmElHczs99fUkGr4kGY8QE53mvCIIhsl1skww2rjYPsxwK85Kq4TfBye6J14wy3njF/O8ZWU5LdF4fH5xfv66Y1q+EcCxkCmlnM7O2LCSkrxHLEvmPLHiS+5/Yacjn3+qvhNu1k6vEqyAhz2Gxp4uxa5Oh0MxIWMKaE7vvOZXFOoOA90UY8woo6x2BtndxCzhXSnlFW2YO2r6NFl6S61+ae1IgLebm+M/qqrK33dMw3/S+F8Puo85uOuu5b677667GrittTU24KW/NPHQa9tsY28DwyWinZKX1ibmJqnyGngzKcxEHCuZQkwLrCxkM2RMiwZb42Ny2Ue5u69wqDtu5sncOf9bnnNmDiEQ0Bsty3nogTdX/f6euXPN/yb+awEcg+DovPvBluaSGRMrlwDzsN0hf97Uwu/X7mLjp01ubH+LZKIRbCOigtmoCpMAMvTgYuGXgK9Acir6UTp2lDpn+lhtYd1kBvbLAagHXtvwWctTMyf1jRxbl/6b+K8NcBTiK05b4lJaEeY84AxgsthO/70HOthzsJ2mjjgtbRGSsW40JRQWhKksL2VAVRljh1fTtyIP4BDwieM4fznYkX5raGVe57821tfR9U3eVjkeRFdK2cd+O9TWU1NWnD/K7+FEYBwwECgBQkerpIEI0ADsiKezn/ZEjN39+xccPM6vB/jGr9v8P3S8PgU+47fMAAAAAElFTkSuQmCC"}
_ICON_MIME = {"favicon.ico": "image/x-icon", "favicon-48.png": "image/png", "icon-192.png": "image/png", "apple-touch-icon.png": "image/png"}
_ICON_LINKS = ('<link rel="icon" href="/favicon.ico" sizes="any">'
               '<link rel="icon" type="image/png" sizes="48x48" href="/favicon-48.png">'
               '<link rel="icon" type="image/png" sizes="192x192" href="/icon-192.png">'
               '<link rel="apple-touch-icon" href="/apple-touch-icon.png">'
               '<meta name="theme-color" content="#0a1230">')


def _icon(name):
    r = Response(base64.b64decode(_ICON_B64[name]), mimetype=_ICON_MIME[name])
    r.headers["Cache-Control"] = "public, max-age=86400"
    return r


@app.get("/favicon.ico")
def icon_ico():
    return _icon("favicon.ico")


@app.get("/favicon-48.png")
def icon_48():
    return _icon("favicon-48.png")


@app.get("/icon-192.png")
def icon_192():
    return _icon("icon-192.png")


@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
def icon_apple():
    return _icon("apple-touch-icon.png")


# ---------------- One-time LAUNCH switch ----------------
# Until the owner presses "Launch site" in /admin, visitors see a "launching today" page. The flag lives in the
# DATABASE (table site_flags), so new deploys never bring the button back: it is pressed once and gone.
_launched = False


def site_launched():
    global _launched
    if _launched:
        return True
    try:
        with db() as c:
            c.execute("CREATE TABLE IF NOT EXISTS site_flags(k TEXT PRIMARY KEY, v TEXT, at TIMESTAMPTZ DEFAULT now())")
            row = c.execute("SELECT 1 AS x FROM site_flags WHERE k='launched'").fetchone()
        if row:
            _launched = True
        return bool(row)
    except Exception as e:
        log.warning("launch flag check failed (%s): showing the shop", e)
        return True          # never hide the shop because of a database hiccup


def _preview_token():
    return hmac.new((ADMIN_KEY or "x").encode(), b"xt-preview", hashlib.sha256).hexdigest()[:32]


def _is_preview():
    return bool(ADMIN_KEY) and hmac.compare_digest(request.cookies.get("xt_pre", ""), _preview_token())


def _coming_soon():
    name = html.escape(BIZ["name"])
    page = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">" +
            _ICON_LINKS + "<title>" + name + " is launching today</title>"
            "<style>html,body{height:100%;margin:0}body{display:flex;align-items:center;justify-content:center;text-align:center;"
            "background:#0a1230;color:#eef0ff;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:24px}"
            ".b{max-width:420px}.r{font-size:56px}h1{font-size:30px;margin:12px 0 8px}p{color:#a4abd1;font-size:17px;line-height:1.5;margin:0}"
            ".t{display:inline-block;margin-top:22px;border:2px solid #e11d2e;color:#fff;border-radius:99px;padding:8px 18px;font-weight:700}</style></head>"
            "<body><div class=\"b\"><div class=\"r\">\U0001F680</div><h1>" + name + " is launching today!</h1>"
            "<p>Our store opens very soon. Please check back in a little while.</p><div class=\"t\">Coming soon</div></div></body></html>")
    r = Response(page, mimetype="text/html")
    r.headers["Cache-Control"] = "no-store"
    return r


@app.post("/admin/api/launch")
def admin_launch():
    admin_auth()
    global _launched
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS site_flags(k TEXT PRIMARY KEY, v TEXT, at TIMESTAMPTZ DEFAULT now())")
        c.execute("INSERT INTO site_flags(k, v) VALUES ('launched', '1') ON CONFLICT (k) DO NOTHING")
    _launched = True
    log.info("SITE LAUNCHED from admin panel")
    return jsonify(ok=True)


@app.post("/admin/api/preview")
def admin_preview():
    admin_auth()
    r = jsonify(ok=True)
    r.set_cookie("xt_pre", _preview_token(), max_age=43200, httponly=True, samesite="Lax", secure=request.is_secure)
    return r


_LAUNCH_JS = """<script>
document.getElementById("pv").onclick=function(){api("/admin/api/preview","POST",{}).then(function(){location.href="/"}).catch(function(e){alert("Failed: "+e.message)})};
document.getElementById("lc").onclick=function(){
  if(!confirm("Launch the site now? Customers will see the shop immediately. This can be done only once."))return;
  api("/admin/api/launch","POST",{}).then(function(){var a=document.getElementById("lc"),b=document.getElementById("pv");if(a)a.remove();if(b)b.remove();alert("Site is LIVE!")}).catch(function(e){alert("Launch failed: "+e.message)})
};
</script>"""


# Product-card look: square image shown whole (not cropped), name limited to 2 lines, no Add-to-cart button on cards (it is on the product page). Injected here so only app.py needs uploading.
_CARD_CSS = ("<style id=\"card-fix\">"
             "main{gap:6px !important;padding:4px 6px 40px !important}"
             ".p{border-radius:10px;border:0}"
             ".p .img{position:relative;overflow:hidden;height:auto;aspect-ratio:10/11;background:#fff !important;font-size:56px}"
             ".p .img .pi{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;padding:0}"
             ".p .heart{width:28px;height:28px;font-size:14px;top:6px;right:6px;background:rgba(255,255,255,.92) !important;color:#e11d2e !important}"
             ".p .b{padding:8px 10px 10px;gap:3px}"
             ".p .b small{display:none}"
             ".p .b strong{font-size:13px;font-weight:500;color:var(--mute);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
             ".p .b .pr{font-size:18px;padding-top:0;margin-top:0}"
             ".p .add{display:none !important}"
             "nav.bar button span svg{width:24px;height:24px;display:block;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}"
             "</style>")


def _ico(body):
    return '<svg viewBox="0 0 24 24" aria-hidden="true">' + body + '</svg>'


_NAV_ICONS = [
    ("<span>\U0001F3E0</span>Home</button>", "<span>" + _ico('<path d="M5 12l-2 0l9 -9l9 9l-2 0"/><path d="M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2 -2v-7"/><path d="M9 21v-6a2 2 0 0 1 2 -2h2a2 2 0 0 1 2 2v6"/>') + "</span>Home</button>"),
    ("<span>\U0001F5C2\uFE0F</span>Categories</button>", "<span>" + _ico('<rect x="4" y="4" width="6" height="6" rx="1"/><rect x="14" y="4" width="6" height="6" rx="1"/><rect x="4" y="14" width="6" height="6" rx="1"/><rect x="14" y="14" width="6" height="6" rx="1"/>') + "</span>Categories</button>"),
    ("<span>\U0001F4E6</span>My orders</button>", "<span>" + _ico('<path d="M12 3l8 4.5v9l-8 4.5l-8 -4.5v-9l8 -4.5"/><path d="M12 12l8 -4.5"/><path d="M12 12v9"/><path d="M12 12l-8 -4.5"/><path d="M16 5.25l-8 4.5"/>') + "</span>My orders</button>"),
]


@app.get("/")
def index():
    if not site_launched() and not _is_preview():
        return _coming_soon()
    try:
        with open(os.path.join(app.root_path, "static", "index.html"), encoding="utf-8") as f:
            page = f.read().replace("</head>", _ICON_LINKS + _CARD_CSS + "</head>", 1)
        for _old, _new in _NAV_ICONS:
            page = page.replace(_old, _new, 1)
        r = Response(page, mimetype="text/html")
    except Exception:
        r = send_from_directory("static", "index.html")
    r.headers["Cache-Control"] = "no-cache"
    return r


# Every page of the app has its own URL (browser back/forward, refresh and shared links all work);
# the single-page app in index.html reads the path and opens the right screen.
@app.get("/categories")
@app.get("/orders")
@app.get("/orders/<int:_oid>")
@app.get("/account")
@app.get("/account/<_k>")
@app.get("/login")
@app.get("/product/<int:_pid>")
@app.get("/checkout")
@app.get("/checkout/payment")
def spa_page(**_kw):
    return index()


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/api/config")
def config():
    return jsonify(name=BIZ["name"], phone=BIZ["phone"], email=BIZ["email"], online=bool(RZP_ID and RZP_SECRET), off=ONLINE_OFF)


@app.after_request
def headers(r):
    r.headers.setdefault("X-Content-Type-Options", "nosniff")
    r.headers.setdefault("X-Frame-Options", "DENY")
    r.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return r


@app.errorhandler(HTTPException)
def http_err(e):
    if request.path.startswith(("/api/", "/admin/api/")):
        return jsonify(error={401: "Please login again", 403: "Not allowed", 404: "Not found", 413: "Too large"}.get(e.code, e.name)), e.code
    return e


# ---------- guest + phone login (OTP via your WhatsApp bot) ----------
_guest_hits = {}


@app.post("/api/guest")
def guest():
    ip, now = request.remote_addr or "", time.time()
    hits = [t for t in _guest_hits.get(ip, []) if now - t < 3600]
    if len(hits) >= 30:
        err(429, "Too many requests")
    _guest_hits[ip] = hits + [now]
    t = secrets.token_urlsafe(32)
    with db() as c:
        c.execute("INSERT INTO users(token) VALUES (%s)", (t,))
    return jsonify(token=t)


def otp_hash(phone, code):
    return hmac.new(OTP_SECRET, ("%s:%s" % (phone, code)).encode(), hashlib.sha256).hexdigest()


@app.post("/api/auth/send-otp")
def send_otp():
    if not (OTP_SECRET and BOT_URL):
        err(503, "Login is temporarily unavailable")
    phone = norm_phone((request.get_json(silent=True) or {}).get("phone"))
    if not phone:
        err(400, "Enter a valid 10-digit mobile number")
    ip = request.remote_addr or ""
    with db() as c:
        row = c.execute("SELECT extract(epoch from now()-sent_at) AS age FROM otps WHERE phone=%s", (phone,)).fetchone()
        if row and row["age"] < 30:
            err(429, "Please wait 30 seconds before asking for a new OTP")
        n = c.execute("SELECT count(*) FILTER (WHERE phone=%s) p, count(*) FILTER (WHERE ip=%s) i FROM otp_log WHERE at > now() - interval '1 hour'", (phone, ip)).fetchone()
        if n["p"] >= 5 or n["i"] >= 15:
            err(429, "Too many OTP requests. Try again after some time")
        code = "%06d" % secrets.randbelow(10 ** 6)
        c.execute("""INSERT INTO otps(phone,code_hash,expires_at,attempts,sent_at) VALUES (%s,%s,now()+interval '5 minutes',0,now())
            ON CONFLICT (phone) DO UPDATE SET code_hash=EXCLUDED.code_hash, expires_at=EXCLUDED.expires_at, attempts=0, sent_at=now()""",
                  (phone, otp_hash(phone, code)))
        c.execute("INSERT INTO otp_log(phone, ip) VALUES (%s,%s)", (phone, ip))
        c.execute("DELETE FROM otp_log WHERE at < now() - interval '2 days'")
    try:
        r = bot("/send-otp", {"phone": phone, "otp": code})
        ok = bool(r.get("success") or r.get("ok"))
    except Exception as e:
        log.warning("bot send-otp failed: %s", e)
        ok = False
    if not ok:
        err(502, "Could not send the OTP on WhatsApp. Please try again in a minute")
    return jsonify(ok=True)


@app.post("/api/auth/verify")
def verify_otp():
    b = request.get_json(silent=True) or {}
    phone, code = norm_phone(b.get("phone")), str(b.get("otp") or "").strip()
    if not phone or not re.fullmatch(r"\d{6}", code):
        err(400, "Enter the 6-digit OTP")
    with db() as c:
        row = c.execute("SELECT code_hash, attempts, expires_at > now() AS live FROM otps WHERE phone=%s", (phone,)).fetchone()
        if not row or not row["live"]:
            err(400, "OTP expired. Please request a new one")
        if row["attempts"] >= 5:
            err(429, "Too many wrong attempts. Request a new OTP")
        if not hmac.compare_digest(row["code_hash"], otp_hash(phone, code)):
            c.execute("UPDATE otps SET attempts=attempts+1 WHERE phone=%s", (phone,))
            err(400, "Wrong OTP")
        c.execute("DELETE FROM otps WHERE phone=%s", (phone,))
        g = c.execute("SELECT id FROM users WHERE token=%s AND phone IS NULL", (bearer(),)).fetchone() if bearer() else None
        ex = c.execute("SELECT id FROM users WHERE phone=%s", (phone,)).fetchone()
        if ex:
            u = ex["id"]
            if g and g["id"] != u:  # bring the guest's cart / address over to the real account
                c.execute("""INSERT INTO user_data(user_id,key,value) SELECT %s,key,value FROM user_data
                    WHERE user_id=%s AND key IN ('cart','xt_wish','xt_addr','xt_profile') ON CONFLICT DO NOTHING""", (u, g["id"]))
        elif g:
            u = g["id"]
            c.execute("UPDATE users SET phone=%s WHERE id=%s", (phone, u))
        else:
            u = c.execute("INSERT INTO users(token, phone) VALUES (%s,%s) RETURNING id", (secrets.token_urlsafe(32), phone)).fetchone()["id"]
        tok = secrets.token_urlsafe(32)
        c.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES (%s,%s,now()+interval '90 days')", (tok, u))
        c.execute("DELETE FROM sessions WHERE expires_at < now()")
    return jsonify(token=tok)


@app.post("/api/auth/logout")
def logout():
    with db() as c:
        c.execute("DELETE FROM sessions WHERE token=%s", (bearer(),))
    return jsonify(ok=True)


# ---------- state ----------
def ts(d):
    return int(d.timestamp() * 1000) if d else None


def order_json(o):
    return {"id": 1000 + o["id"], "i": o["items"], "t": o["total"], "pm": o.get("pay_method") or "cod", "ps": o.get("pay_status"),
            "s": o["status"], "ts": ts(o["created_at"]), "dts": ts(o.get("delivered_at")), "trk": o.get("tracking"), "ad": o.get("address"),
            "rt": {"r": o["return_reason"], "ts": ts(o["returned_at"])} if o.get("returned_at") else None}


@app.get("/api/state")
def state():
    r = user_row()
    with db() as c:
        rows = c.execute("SELECT key, value FROM user_data WHERE user_id=%s", (r["id"],)).fetchall()
        orders = c.execute("SELECT * FROM orders WHERE user_id=%s AND status<>'awaiting_payment' ORDER BY id DESC LIMIT 100", (r["id"],)).fetchall()
    return jsonify(state={x["key"]: x["value"] for x in rows}, orders=[order_json(o) for o in orders], me={"phone": r["phone"]} if r["phone"] else None)


@app.put("/api/data/<key>")
def put_data(key):
    u = uid()
    if key not in KEYS:
        abort(404)
    if request.content_length and request.content_length > 600_000:
        abort(413)
    v = (request.get_json(silent=True) or {}).get("v")
    with db() as c:
        c.execute("INSERT INTO user_data(user_id,key,value) VALUES (%s,%s,%s) ON CONFLICT (user_id,key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()", (u, key, Jsonb(v)))
    return jsonify(ok=True)


# ---------- payments (Razorpay) ----------
def rzp(path, payload=None, method=None):
    auth = base64.b64encode(("%s:%s" % (RZP_ID, RZP_SECRET)).encode()).decode()
    return http("https://api.razorpay.com/v1" + path, payload, {"Authorization": "Basic " + auth}, method, timeout=20)


def rzp_start(c, o, amount):
    r = rzp("/orders", {"amount": amount * 100, "currency": "INR", "receipt": "xt%d" % o["id"], "notes": {"order": str(1000 + o["id"])}})
    c.execute("UPDATE orders SET rzp_order_id=%s, pay_total=%s WHERE id=%s", (r["id"], amount, o["id"]))
    return {"key": RZP_ID, "order_id": r["id"], "amount": amount * 100}


def mark_paid(c, o, pay_id):
    if o["pay_status"] in ("paid", "refunded", "refund_pending"):
        return o
    cancelled = o["status"] == "cancelled"
    return c.execute("UPDATE orders SET pay_status=%s, pay_method='online', total=COALESCE(pay_total,total), rzp_payment_id=%s, status=CASE WHEN status='awaiting_payment' THEN 'placed' ELSE status END WHERE id=%s RETURNING *",
                     ("refund_pending" if cancelled else "paid", pay_id, o["id"])).fetchone()


def try_refund(c, o):
    if o["pay_status"] not in ("paid", "refund_pending") or not o.get("rzp_payment_id"):
        return o
    try:
        rzp("/payments/%s/refund" % o["rzp_payment_id"], {"amount": o["total"] * 100, "notes": {"order": str(1000 + o["id"])}})
        ps = "refunded"
    except Exception as e:
        log.warning("refund failed for order %s: %s", o["id"], e)
        ps = "refund_pending"
    return c.execute("UPDATE orders SET pay_status=%s WHERE id=%s RETURNING *", (ps, o["id"])).fetchone()


@app.post("/api/orders/<int:oid>/verify")
def verify_payment(oid):
    u = uid()
    b = request.get_json(silent=True) or {}
    with db() as c:
        o, _ = own_order(c, u, oid)
        pid, sig = str(b.get("razorpay_payment_id") or ""), str(b.get("razorpay_signature") or "")
        want = hmac.new(RZP_SECRET.encode(), ("%s|%s" % (o["rzp_order_id"], pid)).encode(), hashlib.sha256).hexdigest()
        if not o["rzp_order_id"] or not hmac.compare_digest(want, sig):
            err(400, "Payment could not be verified")
        was = o["status"]
        o = mark_paid(c, o, pid)
        if was == "awaiting_payment":
            c.execute("DELETE FROM user_data WHERE user_id=%s AND key='cart'", (u,))
            announce_new(o)
    return jsonify(order_json(o))


@app.post("/api/razorpay/webhook")
def rzp_webhook():
    raw = request.get_data()
    if not RZP_WEBHOOK_SECRET or not hmac.compare_digest(hmac.new(RZP_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest(), request.headers.get("X-Razorpay-Signature", "")):
        abort(403)
    ev = json.loads(raw or b"{}")
    if ev.get("event") in ("payment.captured", "order.paid"):
        pay = ev.get("payload", {}).get("payment", {}).get("entity", {})
        with db() as c:
            o = c.execute("SELECT * FROM orders WHERE rzp_order_id=%s", (pay.get("order_id"),)).fetchone()
            if o and o["pay_status"] != "paid":
                was = o["status"]
                o = mark_paid(c, o, pay.get("id"))
                if was == "awaiting_payment":
                    c.execute("DELETE FROM user_data WHERE user_id=%s AND key='cart'", (o["user_id"],))
                    announce_new(o)
    return jsonify(ok=True)


# ---------- visitor counter (shown on the buyer banner, seller dashboard and admin panel) ----------
VID_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
_vis_hits = {}


@app.post("/api/visit")
def visit():
    """Called by the shop page on load (new visit) and every 2 min (heartbeat, keeps 'online now' accurate)."""
    b = request.get_json(silent=True) or {}
    vid = str(b.get("vid") or "")
    if not VID_RE.match(vid):
        err(400, "bad id")
    ip, now = request.remote_addr or "", time.time()
    hits = [t for t in _vis_hits.get(ip, []) if now - t < 60]
    if len(hits) >= 30:
        err(429, "Too many requests")
    _vis_hits[ip] = hits + [now]
    with db() as c:
        c.execute("""INSERT INTO visits(vid, day, hits) VALUES (%s, (now() AT TIME ZONE 'Asia/Kolkata')::date, %s)
            ON CONFLICT (vid, day) DO UPDATE SET last_at=now(), hits=visits.hits+EXCLUDED.hits""", (vid, 0 if b.get("hb") else 1))
    return jsonify(ok=True)


@app.get("/api/visitors")
def visitors():
    """Public, anonymous numbers only: online now (last 5 min), today, all-time, last 7 days."""
    with db() as c:
        r = c.execute("""SELECT
            (SELECT count(DISTINCT vid) FROM visits WHERE last_at > now() - interval '5 minutes') AS live,
            (SELECT count(*) FROM visits WHERE day=(now() AT TIME ZONE 'Asia/Kolkata')::date) AS today,
            (SELECT count(DISTINCT vid) FROM visits) AS total,
            (SELECT coalesce(sum(hits),0) FROM visits) AS views""").fetchone()
        wk = c.execute("""SELECT d::date AS day, (SELECT count(*) FROM visits v WHERE v.day=d::date) AS n
            FROM generate_series((now() AT TIME ZONE 'Asia/Kolkata')::date - 6, (now() AT TIME ZONE 'Asia/Kolkata')::date, interval '1 day') d ORDER BY d""").fetchall()
    resp = jsonify(live=r["live"], today=r["today"], total=r["total"], views=int(r["views"]), week=[{"day": x["day"].strftime("%d %b"), "n": x["n"]} for x in wk])
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------- seller bridge: buyer order -> seller's Pending Orders ----------
def seller_call(sl, path, payload):
    return http(sl["url"] + path, payload, {"X-Sync-Key": sl["sync"]}, timeout=25)


def seller_get(sl, path, timeout=25):
    """GET from a seller app; retries a few times on 429/5xx (Render rate-limits and cold starts)."""
    last = None
    for i in range(3):
        try:
            return http(sl["url"] + path, headers={"X-Sync-Key": sl["sync"]}, method="GET", timeout=timeout)
        except Exception as e:
            last = e
            if re.match(r"HTTP (429|502|503|504)\b", str(e)) and i < 2:
                time.sleep(1.5 * (i + 1))
                continue
            raise
    raise last


def order_sellers(o):
    """Seller keys involved in an order (old orders have no 'sk' on items: they all belong to seller 1)."""
    ks = []
    for i in o.get("items") or []:
        k = i.get("sk") or DEFAULT_SELLER
        if k in SELLER_BY_KEY and k not in ks:
            ks.append(k)
    return ks or [DEFAULT_SELLER]


def push_order_to_seller(oid):
    """Send a placed order to its seller app(s): each seller only gets its own items (shows up under that seller's Pending Orders).
    Idempotent on the seller side, so retrying is always safe."""
    with db() as c:
        claimed = c.execute("""UPDATE orders SET seller_sync='sending', seller_try_at=now() WHERE id=%s AND status NOT IN ('awaiting_payment','cancelled')
            AND (seller_sync='pending' OR (seller_sync='sending' AND seller_try_at < now() - interval '2 minutes')) RETURNING id""", (oid,)).fetchone()
        if not claimed:
            return
        o = c.execute("SELECT o.*, u.phone AS uphone FROM orders o JOIN users u ON u.id=o.user_id WHERE o.id=%s", (oid,)).fetchone()
    a = o.get("address") or {}
    push = dict(o.get("seller_push") or {})
    sellers = order_sellers(o)
    multi = len(sellers) > 1
    failed_msg = None
    for k in sellers:
        sl = SELLER_BY_KEY[k]
        if push.get(k) == "ok":
            continue
        if not (sl["url"] and sl["sync"]):
            push[k] = "pending"
            continue
        its = [i for i in o["items"] if (i.get("sk") or DEFAULT_SELLER) == k]
        part_total = o["total"] if not multi else sum(i["p"] * i["q"] for i in its)
        if multi and o.get("pay_method") == "online":
            part_total -= round(part_total * ONLINE_OFF / 100)
        payload = {"ref": str(1000 + o["id"]), "name": a.get("n") or "Customer",
                   "phone": norm_phone(a.get("p")) or o["uphone"], "address": ((a.get("l") or a.get("d") or "") + (", Near " + a["k"] if a.get("k") else ""))[:300],
                   "city": a.get("c") or "", "state": a.get("s") or "", "pincode": a.get("z") or "", "pay_method": o.get("pay_method") or "cod",
                   "paid": o.get("pay_status") == "paid", "total": part_total,
                   # the seller apps always expect buyer pid = their product id + 1000
                   "items": [{"pid": int(i["pid"]) - sl["offset"] + SELLER_WIRE_OFFSET, "q": i["q"], "p": i["p"], "n": i["n"]} for i in its]}
        try:
            seller_call(sl, "/api/public/orders", payload)
            push[k] = "ok"
        except Exception as e:
            msg = str(e)[:300]
            # 4xx = seller refused (e.g. out of stock): retrying won't help
            push[k] = "failed" if re.match(r"HTTP (400|409)\b", msg) else "pending"
            failed_msg = failed_msg or ("%s: %s" % (k, msg))
            log.warning("push order %s to seller %s: %s (%s)", oid, k, msg, push[k])
    vals = [push.get(k) for k in sellers]
    state = "ok" if all(v == "ok" for v in vals) else ("failed" if "failed" in vals else "pending")
    with db() as c:
        c.execute("UPDATE orders SET seller_sync=%s, seller_error=%s, seller_push=%s WHERE id=%s", (state, failed_msg, Jsonb(push), oid))
    if state == "failed":
        notify(ADMIN_WHATSAPP, "⚠️ Order #%d could not be sent to a seller: %s" % (1000 + oid, failed_msg))


def retry_unsent_orders():
    while True:
        time.sleep(90)
        try:
            with db() as c:
                ids = [r["id"] for r in c.execute("""SELECT id FROM orders WHERE status NOT IN ('awaiting_payment','cancelled') AND created_at > now() - interval '3 days'
                    AND (seller_sync='pending' OR (seller_sync='sending' AND seller_try_at < now() - interval '2 minutes')) ORDER BY id LIMIT 20""").fetchall()]
            for i in ids:
                push_order_to_seller(i)
        except Exception as e:
            log.warning("retry loop: %s", e)


threading.Thread(target=retry_unsent_orders, daemon=True).start()

RANK = {"placed": 0, "confirmed": 1, "shipped": 2, "out_for_delivery": 3, "delivered": 4}


def seller_from_key(k):
    """Which seller is calling, by the sync key it sent. None if unknown; a list if several sellers share one key."""
    m = [sl for sl in SELLERS if sl["sync"] and hmac.compare_digest(k or "", sl["sync"])]
    return m


@app.post("/api/seller/order-update")
def seller_order_update():
    """Seller app -> here: accepted / AWB assigned / shipped / out for delivery / delivered / rejected."""
    callers = seller_from_key(request.headers.get("X-Sync-Key", ""))
    if not callers:
        abort(403)
    b = request.get_json(silent=True) or {}
    try:
        oid = int(b.get("ref")) - 1000
    except (TypeError, ValueError):
        err(400, "bad ref")
    st = b.get("status")
    if st not in STATUS_MSG:
        err(400, "bad status")
    awb, courier = str(b.get("awb") or "").strip(), str(b.get("courier") or "").strip()
    trk = ("%s %s" % (courier, awb)).strip()[:80] if awb else ""
    with db() as c:
        o = c.execute("SELECT status, items, seller_states, tracking FROM orders WHERE id=%s", (oid,)).fetchone()
        if not o or o["status"] == "awaiting_payment":
            abort(404)
        cur = o["status"]
        sellers = order_sellers(o)
        if len(sellers) == 1:                         # one seller on the order: exactly the old behaviour
            if cur in ("cancelled", "delivered", "returned", "refunded", "return_requested") or (st in RANK and cur in RANK and RANK[st] < RANK[cur]):
                return jsonify(ok=True, ignored=True)
            if st == "cancelled" and cur not in ("placed", "confirmed"):
                return jsonify(ok=True, ignored=True)
            if st == cur:                             # same status again (e.g. AWB arrives after accept): just keep the tracking
                if trk:
                    c.execute("UPDATE orders SET tracking=%s WHERE id=%s", (trk, oid))
                return jsonify(ok=True)
            apply_to, new_trk = st, trk
        else:                                         # order split across sellers: track each seller, then combine
            who = [x["key"] for x in callers if x["key"] in sellers]
            if len(who) != 1:
                log.warning("order %s: cannot tell which seller sent '%s' (use a different sync key per seller)", oid, st)
                return jsonify(ok=True, ignored=True, note="ambiguous seller")
            k = who[0]
            states = dict(o.get("seller_states") or {})
            prev = states.get(k) or {}
            if prev.get("s") in ("cancelled", "delivered") or (st in RANK and prev.get("s") in RANK and RANK[st] < RANK[prev["s"]]):
                return jsonify(ok=True, ignored=True)
            states[k] = {"s": st, "t": trk or prev.get("t") or ""}
            c.execute("UPDATE orders SET seller_states=%s WHERE id=%s", (Jsonb(states), oid))
            if cur in ("cancelled", "delivered", "returned", "refunded", "return_requested"):
                return jsonify(ok=True, ignored=True)
            if st == "cancelled":                     # one part cancelled: refund of a part is a manual job, tell the owner
                notify(ADMIN_WHATSAPP, "⚠️ Order #%d: seller %s cancelled its part. Handle the partial refund / customer message manually." % (1000 + oid, k))
                return jsonify(ok=True)
            live = [states.get(x, {}).get("s") or "placed" for x in sellers if (states.get(x, {}).get("s")) != "cancelled"]
            ranks = [RANK.get(x, 0) for x in live] or [0]
            apply_to = next((n for n, r in sorted(RANK.items(), key=lambda kv: kv[1]) if r == min(ranks)), "placed")
            new_trk = " | ".join("%s: %s" % (x, states[x]["t"]) for x in sellers if states.get(x, {}).get("t"))[:200]
            if apply_to == cur or apply_to == "placed":
                if new_trk:
                    c.execute("UPDATE orders SET tracking=%s WHERE id=%s", (new_trk, oid))
                return jsonify(ok=True)
            st = apply_to
    apply_status(oid, apply_to, new_trk)
    return jsonify(ok=True)


# ---------- orders ----------
def announce_new(o):
    with db() as c:
        c.execute("UPDATE orders SET seller_sync='pending' WHERE id=%s AND seller_sync IS NULL", (o["id"],))
    threading.Thread(target=push_order_to_seller, args=(o["id"],), daemon=True).start()
    with db() as c:
        u = c.execute("SELECT phone FROM users WHERE id=%s", (o["user_id"],)).fetchone()
    n = sum(i["q"] for i in o["items"])
    notify(u["phone"], "✅ %s: order #%d placed (%d item%s, ₹%d, %s). We will message you here when it ships." % (
        BIZ["name"], 1000 + o["id"], n, "" if n == 1 else "s", o["total"], "paid online" if o["pay_method"] == "online" else "cash on delivery"))
    notify(ADMIN_WHATSAPP, "🛒 New order #%d: ₹%d (%s), %d item(s)." % (1000 + o["id"], o["total"], o["pay_method"], n))


_ship_cache = {}
_ship_fail = {}
try:
    SHIP_FALLBACK = int(float(env("SHIP_FALLBACK", "85") or 0))   # flat charge used ONLY when a seller's live quote is unreachable (0 = block checkout instead)
except ValueError:
    SHIP_FALLBACK = 85


def _ship_fallback(sl):
    """{buyer_product_id: SHIP_FALLBACK} for one seller, so checkout keeps working while its quote service is down/asleep."""
    if SHIP_FALLBACK <= 0:
        return None
    try:
        with db() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM products WHERE active AND seller_key=%s", (sl["key"],)).fetchall()]
        return {i: SHIP_FALLBACK for i in ids}
    except Exception as e:
        log.warning("shipping fallback failed for %s: %s", sl["key"], e)
        return None


def ship_charges(pin, only=None):
    """{buyer_product_id: rupees or None} for one delivery pincode, from every seller (Shiprocket rate / flat self-ship charge). Cached 30 min per seller."""
    out = {}
    for sl in SELLERS:
        if only is not None and sl["key"] not in only:   # at checkout ask only the sellers whose products are in the cart
            continue
        ck = (sl["key"], pin)
        hit = _ship_cache.get(ck)
        if hit and time.time() - hit[0] < 1800:
            out.update(hit[1])
            continue
        ch = None
        if sl["key"] == "ss" and sl["schema"]:      # self-ship seller = flat charge: no need to call its API, read product ids from its schema
            try:
                sch = re.sub(r"[^a-zA-Z0-9_]", "", sl["schema"])
                with (psycopg.connect(sl["dburl"], row_factory=dict_row, autocommit=True) if sl.get("dburl") else db()) as c:
                    ids = [r["id"] for r in c.execute(f"SELECT id FROM {sch}.products WHERE approved=1 AND COALESCE(stock,0)>0").fetchall()]
                ch = {sl["offset"] + int(i): SELLER2_SHIPPING for i in ids}
            except Exception as e:
                log.info("ss: flat shipping from schema failed (%s), trying API", e)
        if ch is None:
            bad = _ship_fail.get(ck)
            if bad and time.time() - bad < 20:           # seller just refused/failed: don't hammer it again
                if hit:
                    out.update(hit[1]); continue
                fb = _ship_fallback(sl)
                if fb is not None:
                    out.update(fb); continue
                raise RuntimeError("%s: shipping quote temporarily unavailable" % sl["name"])
            try:
                try:
                    data = seller_get(sl, "/api/public/shipping-quote?pin=" + pin, timeout=10)
                except Exception as e1:                  # the seller service may just be waking up: one quick retry
                    log.info("shipping quote %s from %s failed once (%s), retrying", pin, sl["key"], str(e1)[:120])
                    data = seller_get(sl, "/api/public/shipping-quote?pin=" + pin, timeout=10)
            except Exception as e2:
                _ship_fail[ck] = time.time()
                if hit:                                  # serve the last good (stale) quote instead of failing the page
                    out.update(hit[1]); continue
                fb = _ship_fallback(sl)
                if fb is not None:                       # not cached, so the next request tries the live quote again
                    log.warning("shipping quote %s from %s unavailable (%s): using flat Rs%d", pin, sl["key"], str(e2)[:160], SHIP_FALLBACK)
                    out.update(fb); continue
                raise
            ch = {sl["offset"] + int(k): (int(v) if v is not None else None) for k, v in (data.get("charges") or {}).items()}
            if sl["key"] == "ss":                   # self-ship: never add the seller's own flat charge, use SELLER2_SHIPPING (default 0)
                ch = {k: (SELLER2_SHIPPING if v is not None else None) for k, v in ch.items()}
        if len(_ship_cache) > 500:
            _ship_cache.clear()
        _ship_cache[ck] = (time.time(), ch)
        out.update(ch)
    return out


@app.get("/api/shipping")
def api_shipping():
    pin = re.sub(r"\D", "", request.args.get("pin", ""))
    if not re.fullmatch(r"[1-9]\d{5}", pin):
        err(400, "Enter a valid 6-digit pincode")
    try:
        return jsonify(charges={str(k): v for k, v in ship_charges(pin).items()})
    except Exception as e:
        log.warning("shipping quote %s: %s", pin, e)
        err(503, "Could not check delivery charges right now. Please try again")


@app.post("/api/orders")
def create_order():
    r = login_user()
    u = r["id"]
    body = request.get_json(silent=True) or {}
    pm = "online" if body.get("pm") == "online" else "cod"
    if pm == "online" and not (RZP_ID and RZP_SECRET):
        err(400, "Online payment is not available right now. Please choose Cash on Delivery")
    qty = {}
    for k, v in (body.get("cart") or {}).items():
        try:
            qty[int(k)] = max(1, min(int(v), 99))
        except (ValueError, TypeError):
            continue
    if not qty:
        err(400, "Your cart is empty")
    with db() as c:
        rows = c.execute("SELECT id, name, price, stock, images, size_label, seller_key FROM products WHERE active AND seller_pid IS NOT NULL AND id = ANY(%s)", (list(qty),)).fetchall()
        if len(rows) != len(qty):
            err(409, "Some items in your cart are no longer available. Please refresh")
        for x in rows:
            if x["stock"] is not None and qty[x["id"]] > x["stock"]:
                err(409, "Only %d left of %s" % (x["stock"], x["name"]))
        row = c.execute("SELECT value FROM user_data WHERE user_id=%s AND key='xt_addr'", (u,)).fetchone()
        addr = next((a for a in (row["value"] if row else []) if isinstance(a, dict) and a.get("d")), None)
        if not addr:
            err(400, "Please add a delivery address")
        # Delivery charge (Shiprocket rate for the address pincode) is added into each product's price
        pin = re.sub(r"\D", "", str(addr.get("z") or ""))
        if not re.fullmatch(r"[1-9]\d{5}", pin):
            err(400, "Please add a valid 6-digit pincode to your delivery address")
        if str(body.get("pin") or "") != pin:
            err(409, "Delivery pincode changed. Refreshing prices, please try again")
        try:
            ch = ship_charges(pin, only={(x["seller_key"] or DEFAULT_SELLER) for x in rows})
        except Exception as e:
            log.warning("shipping quote at order %s: %s", pin, e)
            err(503, "Could not calculate the delivery charge right now. Please try again")
        items = []
        for x in rows:
            sc = ch.get(x["id"])
            if sc is None:
                err(400, "Sorry, %s cannot be delivered to pincode %s" % (x["name"], pin))
            items.append({"pid": x["id"], "n": x["name"], "p": x["price"] + sc, "q": qty[x["id"]], "im": (x["images"] or [None])[0], "sz": x["size_label"], "sk": x["seller_key"] or DEFAULT_SELLER})
        total = sum(i["p"] * i["q"] for i in items)
        if pm == "online":
            total -= round(total * ONLINE_OFF / 100)
        o = c.execute("INSERT INTO orders(user_id, items, total, pay_method, address, status, pay_status) VALUES (%s,%s,%s,%s,%s,%s,'pending') RETURNING *",
                      (u, Jsonb(items), total, pm, Jsonb(addr), "awaiting_payment" if pm == "online" else "placed")).fetchone()
        out = order_json(o)
        if pm == "online":
            try:
                out["rzp"] = rzp_start(c, o, total)
            except Exception as e:
                log.warning("razorpay order failed: %s", e)
                c.execute("DELETE FROM orders WHERE id=%s", (o["id"],))
                err(502, "Could not start the payment. Please try again")
        else:
            c.execute("DELETE FROM user_data WHERE user_id=%s AND key='cart'", (u,))
            announce_new(o)
    return jsonify(out)


def own_order(c, u, oid):
    o = c.execute("SELECT * FROM orders WHERE id=%s AND user_id=%s", (oid - 1000, u)).fetchone()
    if not o:
        abort(404)
    return o, (time.time() - o["created_at"].timestamp())


@app.post("/api/orders/<int:oid>/cancel")
def cancel_order(oid):
    u = uid()
    with db() as c:
        o, _ = own_order(c, u, oid)
        if o["status"] not in ("placed", "confirmed"):
            err(409, "This order can no longer be cancelled")
        if o["seller_sync"] in ("ok", "sending", "pending", "failed"):   # a seller may already have it: they must release stock / the courier pickup
            pushed = o.get("seller_push") or {}
            for k in order_sellers(o):
                if o["seller_sync"] in ("pending", "failed") and pushed.get(k) != "ok":
                    continue                                              # that seller never received it
                try:
                    seller_call(SELLER_BY_KEY[k], "/api/public/orders/%d/cancel" % (1000 + o["id"]), {})
                except Exception as e:
                    m = re.match(r"HTTP 409 from [^:]*: b?['\"]?(.*)", str(e))
                    log.warning("seller cancel failed for %s (%s): %s", o["id"], k, e)
                    err(409 if m else 502, "This order has already been handed to the courier and cannot be cancelled" if m else "Could not reach the seller. Please try again")
        o = c.execute("UPDATE orders SET status='cancelled', seller_sync=CASE WHEN seller_sync='ok' THEN 'ok' ELSE 'cancelled' END WHERE id=%s RETURNING *", (o["id"],)).fetchone()
        o = try_refund(c, o)
    return jsonify(order_json(o))


@app.post("/api/orders/<int:oid>/pay-start")
def pay_start(oid):
    """Switch a Cash-on-Delivery order to online payment (5% off)."""
    err(409, "Online payment can only be chosen while placing the order")   # disabled: the seller already has this order as COD and would collect cash again
    u = uid()
    if not (RZP_ID and RZP_SECRET):
        err(400, "Online payment is not available right now")
    with db() as c:
        o, _ = own_order(c, u, oid)
        if o["status"] not in ("placed", "confirmed") or o["pay_method"] == "online":
            err(409, "This order cannot be paid online")
        try:
            rz = rzp_start(c, o, o["total"] - round(o["total"] * ONLINE_OFF / 100))
        except Exception as e:
            log.warning("razorpay order failed: %s", e)
            err(502, "Could not start the payment. Please try again")
    return jsonify(rzp=rz)


RETURN_REASONS = {"Product damaged", "Wrong product received", "Quality not as expected", "Size or quantity issue", "Changed my mind"}


@app.post("/api/orders/<int:oid>/return")
def return_order(oid):
    u = uid()
    reason = ((request.get_json(silent=True) or {}).get("reason") or "").strip()
    if reason not in RETURN_REASONS:
        err(400, "Select a reason")
    with db() as c:
        o, _ = own_order(c, u, oid)
        if o["status"] != "delivered" or not o["delivered_at"] or time.time() - o["delivered_at"].timestamp() > 7 * 86400:
            err(409, "This order is not eligible for return")
        o = c.execute("UPDATE orders SET status='return_requested', return_reason=%s, returned_at=now() WHERE id=%s RETURNING *", (reason, o["id"])).fetchone()
    notify(ADMIN_WHATSAPP, "↩️ Return requested for order #%d: %s" % (1000 + o["id"], reason))
    return jsonify(order_json(o))


# ---------- admin (header X-Admin-Key; page at /admin) ----------
def admin_auth():
    if not ADMIN_KEY or not hmac.compare_digest(request.headers.get("X-Admin-Key", ""), ADMIN_KEY):
        abort(403)


_DIAG_JS = """<script>
document.getElementById("dg").onclick=function(){
  var btn=this;btn.disabled=true;btn.textContent="Testing...";var lines=[];
  api("/admin/api/diag","POST",{}).then(function(r){
    var i=0;(function next(){
      if(i>=r.sellers.length){btn.disabled=false;btn.textContent="Test sellers";alert(lines.join("\\n\\n"));return}
      var sl=r.sellers[i++];
      api("/admin/api/diag","POST",{k:sl.key}).then(function(d){lines.push(sl.key+" ("+sl.url+")\\nKey/products: "+d.key_test+"\\nShipping quote: "+d.ship_test)}).catch(function(e){lines.push(sl.key+": test failed "+e.message)}).then(next)
    })()
  }).catch(function(e){btn.disabled=false;btn.textContent="Test sellers";alert("Test failed: "+e.message)})
};
</script>"""


@app.post("/admin/api/diag")
def admin_diag():
    """Admin button 'Test sellers': checks the secret key and the shipping quote of each seller and says what is wrong."""
    admin_auth()
    k = (request.get_json(silent=True) or {}).get("k")
    if not k:
        return jsonify(sellers=[{"key": x["key"], "url": x["url"]} for x in SELLERS])
    sl = next((x for x in SELLERS if x["key"] == k), None)
    if not sl:
        err(404, "Unknown seller")
    out = {}
    if not sl["url"]:
        return jsonify(key_test="FAIL: seller URL is not set (SELLER_URL / SELLER2_URL)", ship_test="not tested")
    t = time.time()
    try:
        d = http(sl["url"] + "/api/public/products", headers={"X-Sync-Key": sl["sync"]}, method="GET", timeout=12)
        n = len(d.get("products") or d.get("items") or []) if isinstance(d, dict) else len(d)
        out["key_test"] = "OK (%d products, %.1fs)" % (n, time.time() - t)
    except Exception as e:
        m = str(e)
        out["key_test"] = "FAIL: " + m[:220] + (" -> secret keys do not match (buyer key vs seller BUYER_SYNC_KEY)" if "HTTP 403" in m else (" -> BLOCKED BEFORE REACHING THE SELLER (Render/Cloudflare edge), not a key/app problem" if "HTTP 429" in m else (" -> seller asleep (timeout)" if "timed out" in m else "")))
    t = time.time()
    try:
        d = http(sl["url"] + "/api/public/shipping-quote?pin=110009", headers={"X-Sync-Key": sl["sync"]}, method="GET", timeout=14)
        ch = d.get("charges") or {}
        vals = [v for v in ch.values() if v is not None]
        out["ship_test"] = "OK (%d products priced, e.g. Rs%s, %.1fs)" % (len(vals), vals[0] if vals else "-", time.time() - t)
    except Exception as e:
        m = str(e)
        out["ship_test"] = "FAIL: " + m[:220] + (" -> Shiprocket rejected the request (check SHIPROCKET_EMAIL/PASSWORD and wallet)" if "HTTP 502" in m else (" -> BLOCKED BEFORE REACHING THE SELLER (Render/Cloudflare edge)" if "HTTP 429" in m else (" -> seller asleep (timeout)" if "timed out" in m else "")))
    return jsonify(out)


@app.get("/admin")
def admin_page():
    try:
        with open(os.path.join(app.root_path, "static", "admin.html"), encoding="utf-8") as f:
            page = f.read()
        page = page.replace('<button class="g" id="sy">Sync products</button>', '<button class="g" id="sy">Sync products</button><button class="g" id="dg">Test sellers</button>', 1)
        page = page.replace("</head>", _ICON_LINKS + "</head>", 1)
        page = page.replace("</body>", _DIAG_JS + "</body>", 1)
        if not site_launched():
            page = page.replace('id="dg">Test sellers</button>', 'id="dg">Test sellers</button>'
                                '<button class="g" id="pv">Preview shop</button>'
                                '<button class="g" id="lc" style="background:#16a34a;color:#fff;font-weight:700">Launch site</button>', 1)
            page = page.replace("</body>", _LAUNCH_JS + "</body>", 1)
        r = Response(page, mimetype="text/html")
    except Exception:
        r = send_from_directory("static", "admin.html")
    r.headers["Cache-Control"] = "no-store"
    return r


@app.get("/admin/api/orders")
def admin_orders():
    admin_auth()
    with db() as c:
        rows = c.execute("""SELECT o.*, u.phone, (SELECT value FROM user_data WHERE user_id=o.user_id AND key='xt_profile') AS profile
            FROM orders o JOIN users u ON u.id=o.user_id WHERE o.status<>'awaiting_payment' ORDER BY o.id DESC LIMIT 300""").fetchall()
    for r in rows:
        r["id"] = 1000 + r["id"]
        for k in ("created_at", "shipped_at", "delivered_at", "returned_at"):
            r[k] = r[k].astimezone(IST).strftime("%d %b %Y, %I:%M %p") if r.get(k) else None
        r["profile"] = {"n": (r["profile"] or {}).get("n")}
    return jsonify(rows)


STATUS_MSG = {
    "confirmed": "✅ Order #{id} is confirmed and being packed.",
    "shipped": "🚚 Order #{id} has been shipped.{trk}",
    "out_for_delivery": "📦 Order #{id} is out for delivery today. Please keep ₹{t} ready if you chose cash on delivery.",
    "delivered": "🎉 Order #{id} is delivered. Not happy? You can return it within 7 days from My orders.",
    "cancelled": "Order #{id} has been cancelled.{ref}",
    "returned": "We received the return for order #{id}. Your refund is being processed.",
    "refunded": "💳 Refund of ₹{t} for order #{id} has been initiated.",
}


def apply_status(oid, st, trk=""):
    """Move an order to a new status, handle refunds, and WhatsApp the customer. Used by the admin panel and the seller callback."""
    with db() as c:
        o = c.execute("SELECT o.*, u.phone FROM orders o JOIN users u ON u.id=o.user_id WHERE o.id=%s", (oid,)).fetchone()
        if not o or o["status"] == "awaiting_payment":
            abort(404)
        o = c.execute("""UPDATE orders SET status=%s, tracking=COALESCE(NULLIF(%s,''), tracking),
            shipped_at=CASE WHEN %s='shipped' THEN now() ELSE shipped_at END,
            delivered_at=CASE WHEN %s='delivered' THEN now() ELSE delivered_at END,
            pay_status=CASE WHEN %s='delivered' AND pay_method='cod' THEN 'paid' ELSE pay_status END
            WHERE id=%s RETURNING *""", (st, trk, st, st, st, o["id"])).fetchone()
        if st in ("cancelled", "refunded"):
            o = try_refund(c, o)
            if st == "refunded" and o["pay_method"] == "cod":
                o = c.execute("UPDATE orders SET pay_status='refunded' WHERE id=%s RETURNING *", (o["id"],)).fetchone()
        phone = c.execute("SELECT phone FROM users WHERE id=%s", (o["user_id"],)).fetchone()["phone"]
    ref = " Your refund will reach your original payment method in 5-7 working days." if o["pay_method"] == "online" and o["pay_status"] in ("refunded", "refund_pending") else ""
    notify(phone, STATUS_MSG[st].format(id=1000 + o["id"], t=o["total"], ref=ref, trk=(" Tracking: %s" % o["tracking"]) if o["tracking"] else ""))
    return o


@app.post("/admin/api/orders/<int:oid>/status")
def admin_status(oid):
    admin_auth()
    b = request.get_json(silent=True) or {}
    st, trk = b.get("status"), (b.get("tracking") or "").strip()[:80]
    if st not in STATUS_MSG:
        err(400, "Bad status")
    o = apply_status(oid - 1000, st, trk)
    return jsonify(ok=True, pay_status=o["pay_status"])


@app.post("/admin/api/sync")
def admin_sync():
    admin_auth()
    ok = sync_products(force=True)
    return jsonify(ok=ok, products_synced=_sync["n"], error=_sync["err"], seller_url=SELLER_URL, sellers=[{"key": x["key"], "url": x["url"], "schema": x["schema"]} for x in SELLERS])


# ---------- legal pages (templates: have them reviewed before you rely on them) ----------
def legal_pages():
    n, e, p, a = html.escape(BIZ["name"]), html.escape(BIZ["email"] or "(add BUSINESS_EMAIL)"), html.escape(BIZ["phone"] or "(add BUSINESS_PHONE)"), html.escape(BIZ["address"] or "(add BUSINESS_ADDRESS)")
    g = html.escape(BIZ["grievance"] or "(add BUSINESS_GRIEVANCE)")
    return {
        "terms": ("Terms of Use", f"""<p>These terms apply to your use of the {n} website and the orders you place on it. By using the site you agree to them.</p>
<h2>Orders and prices</h2><p>All prices are in Indian Rupees and include applicable taxes. An order is confirmed once it shows as placed in My Orders. We may cancel an order if an item is out of stock, mispriced or the delivery address cannot be served; any amount paid will be refunded in full.</p>
<h2>Account</h2><p>You log in with your mobile number and a one-time password sent on WhatsApp. You are responsible for activity on your account. Provide correct name, phone and address so we can deliver.</p>
<h2>Payments</h2><p>You can pay by Cash on Delivery or online. Online payments are processed by Razorpay; we never see or store your card, UPI PIN or bank login details. Paying online currently gives a {ONLINE_OFF}% discount.</p>
<h2>Cancellation, returns and refunds</h2><p>See the Return &amp; Refund Policy.</p>
<h2>Liability</h2><p>Product descriptions and images are provided by the sellers on our platform. Our liability for any order is limited to the amount you paid for it.</p>
<h2>Governing law</h2><p>These terms are governed by the laws of India.</p>
<h2>Contact</h2><p>{n}, {a}. Email {e}. Phone {p}.</p>"""),
        "privacy": ("Privacy Policy", f"""<p>{n} respects your privacy. This page explains what we collect and why.</p>
<h2>What we collect</h2><p>Your mobile number, name, delivery addresses, optional profile photo, cart, wishlist and order history.</p>
<h2>How we use it</h2><p>To log you in (OTP on WhatsApp), deliver and support your orders, send order updates on WhatsApp, process payments and refunds, and prevent fraud. We do not sell your data.</p>
<h2>Who we share it with</h2><p>The seller and courier partner get only what is needed to pack and deliver your order (name, address, phone). Razorpay processes online payments. Our hosting providers store the data on our behalf.</p>
<h2>Your choices</h2><p>You can edit or delete your addresses and profile in the app at any time. To delete your account and data, email {e} from your registered contact or message us on {p}. We keep order and invoice records for as long as the law requires.</p>
<h2>Security</h2><p>Data is transmitted over HTTPS. OTPs are stored only as hashes and expire in 5 minutes.</p>
<h2>Contact</h2><p>{n}, {a}. {e}</p>"""),
        "returns": ("Return & Refund Policy", f"""<h2>Cancellation</h2><p>You can cancel an order from My Orders until it is shipped. Prepaid orders are refunded to the original payment method within 5-7 working days.</p>
<h2>Returns</h2><p>You can request a return within 7 days of delivery from My Orders, for these reasons: product damaged, wrong product received, quality not as expected, size or quantity issue, or change of mind. The item must be unused and in its original packaging. We will arrange a pickup.</p>
<h2>Refunds</h2><p>After we receive and check the returned item, the refund is sent within 5-7 working days: to the original payment method for prepaid orders, and for Cash on Delivery orders to the UPI ID or bank account you share with our support team on WhatsApp.</p>
<h2>Contact</h2><p>{e} · {p}</p>"""),
        "shipping": ("Shipping Policy", f"""<p>We ship across India through our courier partners. Orders are usually delivered in 5-7 days after they are placed; remote locations may take longer. You get a WhatsApp message when your order is shipped and when it is out for delivery. The delivery charge for your pincode is already included in the price you see. If a delivery attempt fails, the courier will contact you; after repeated failed attempts the order may be returned to us and refunded (prepaid) or cancelled (COD).</p><p>Questions: {e} · {p}</p>"""),
        "contact": ("Contact & Grievance", f"""<p><b>{n}</b><br>{a}<br>Email: {e}<br>Phone / WhatsApp: {p}</p>
<h2>Grievance Officer</h2><p>{g}<br>Email: {e}<br>We acknowledge complaints within 48 hours and resolve them within 30 days.</p>"""),
    }


@app.get("/legal/<slug>")
def legal(slug):
    pg = legal_pages().get(slug)
    if not pg:
        abort(404)
    links = " · ".join('<a href="/legal/%s">%s</a>' % (k, html.escape(v[0])) for k, v in legal_pages().items())
    page = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>%s - %s</title>
<style>body{font:16px/1.6 system-ui,sans-serif;max-width:720px;margin:auto;padding:20px;color:#0b2a6b;background:#fff}h1{font-size:26px}h2{font-size:18px;margin:22px 0 4px}a{color:#e11d2e}nav{font-size:14px;margin-top:30px;border-top:1px solid #dfe2ee;padding-top:12px}p{color:#222}</style></head>
<body><a href="/">&larr; %s</a><h1>%s</h1>%s<nav>%s</nav></body></html>""" % (html.escape(pg[0]), html.escape(BIZ["name"]), html.escape(BIZ["name"]), html.escape(pg[0]), pg[1], links)
    return Response(page, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
