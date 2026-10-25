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
_dburl2 = env("SELLER2_DATABASE_URL", "")      # self-ship seller's OWN Neon database (read-only use: products + shipping ids)
if _dburl2 and not _schema2:
    _schema2 = "seller"                        # selfship app's default DB_SCHEMA
if _url2 or _schema2:
    SELLERS.append({"key": "ss", "name": "Self-ship seller", "url": _url2, "schema": _schema2, "dburl": _dburl2, "offset": 1000000,
                    "sync": env("SELLER2_SYNC_KEY", "") or _shared_key(DB_URL)})
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
_dburl2 = env("SELLER2_DATABASE_URL", "")      # self-ship seller's OWN Neon database (read-only use: products + shipping ids)
if _dburl2 and not _schema2:
    _schema2 = "seller"                        # selfship app's default DB_SCHEMA
if _url2 or _schema2:
    SELLERS.append({"key": "ss", "name": "Self-ship seller", "url": _url2, "schema": _schema2, "dburl": _dburl2, "offset": 1000000,
                    "sync": env("SELLER2_SYNC_KEY", "") or _shared_key(DB_URL)})
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
    return psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True)


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
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s from %s: %s" % (e.code, url.split("?")[0], e.read()[:200]))
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
            log.info("%s: schema '%s' not readable (%s), trying API", sl["key"], sl["schema"], e)
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
@app.get("/")
def index():
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
            if re.match(r"HTTP (502|503|504)\b", str(e)) and i < 2:
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


def ship_charges(pin):
    """{buyer_product_id: rupees or None} for one delivery pincode, from every seller (Shiprocket rate / flat self-ship charge). Cached 30 min per seller."""
    out = {}
    for sl in SELLERS:
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
                raise RuntimeError("%s: shipping quote temporarily unavailable" % sl["name"])
            try:
                data = seller_get(sl, "/api/public/shipping-quote?pin=" + pin, timeout=25)
            except Exception:
                _ship_fail[ck] = time.time()
                if hit:                                  # serve the last good (stale) quote instead of failing the page
                    out.update(hit[1]); continue
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
            ch = ship_charges(pin)
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


@app.get("/admin")
def admin_page():
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
