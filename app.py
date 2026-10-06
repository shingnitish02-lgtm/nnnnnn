import os, secrets
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, send_from_directory, abort
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

app = Flask(__name__, static_folder="static", static_url_path="/static")
DB_URL = os.environ["DATABASE_URL"]
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
IST = ZoneInfo("Asia/Kolkata")
KEYS = {"xt_profile", "xt_addr", "xt_bank", "xt_wish", "xt_lang", "cart", "xt_viewed"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id SERIAL PRIMARY KEY, token TEXT UNIQUE NOT NULL, created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS user_data(user_id INT REFERENCES users(id) ON DELETE CASCADE, key TEXT, value JSONB, updated_at TIMESTAMPTZ DEFAULT now(), PRIMARY KEY(user_id, key));
CREATE TABLE IF NOT EXISTS products(id INT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, price INT NOT NULL, emoji TEXT DEFAULT '📦', bg TEXT DEFAULT '#eeeeee', images JSONB DEFAULT '[]', size_label TEXT, highlights JSONB, active BOOLEAN DEFAULT TRUE);
CREATE TABLE IF NOT EXISTS orders(id SERIAL PRIMARY KEY, user_id INT REFERENCES users(id), items JSONB NOT NULL, total INT NOT NULL, status TEXT DEFAULT 'placed', created_at TIMESTAMPTZ DEFAULT now());
ALTER TABLE orders ADD COLUMN IF NOT EXISTS pay_method TEXT DEFAULT 'cod';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS address JSONB;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS return_reason TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS returned_at TIMESTAMPTZ;
"""

# Demo products (edit/delete in the Neon table later). Format: id, name, category, price, emoji, bg
SEED = [
    (0, "Cotton kurta", "Fashion", 799, "👕", "#fbe3b0"), (1, "Denim jacket", "Fashion", 1999, "🧥", "#c9d8f5"),
    (2, "Running shoes", "Fashion", 2499, "👟", "#f8cfd0"), (3, "Leather wallet", "Fashion", 599, "👛", "#e3d4c2"),
    (4, "Wireless earbuds", "Electronics", 1499, "🎧", "#d3e4ff"), (5, "Smart watch", "Electronics", 3299, "⌚", "#dcd3f7"),
    (6, "Power bank 10000mAh", "Electronics", 999, "🔋", "#cfeedd"), (7, "Bluetooth speaker", "Electronics", 1799, "🔊", "#ffd9c2"),
    (8, "Basmati rice 5kg", "Grocery", 649, "🍚", "#f4ecc8"), (9, "Masala tea 500g", "Grocery", 299, "🍵", "#d6ecd0"),
    (10, "Dry fruits mix", "Grocery", 899, "🥜", "#f0dcc0"), (11, "Fresh mangoes 1kg", "Grocery", 199, "🥭", "#ffe6a3"),
    (12, "Cotton bedsheet", "Home", 1199, "🛏️", "#d8e6f2"), (13, "Steel water bottle", "Home", 449, "🧴", "#cdeaf0"),
    (14, "Table lamp", "Home", 899, "💡", "#fff0b8"), (15, "Face wash", "Beauty", 249, "🧼", "#d9f0e8"),
    (16, "Perfume 100ml", "Beauty", 1299, "🌸", "#f7d6e6"), (17, "Hair oil", "Beauty", 199, "🫙", "#e6dcc3"),
]
POLY = [(18, "Poly courier bags 6x8 inch, Pack of 50", 249, 50), (19, "Poly courier bags 6x8 inch, Pack of 75", 349, 75)]


def db():
    return psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True)


def init_db():
    with db() as c:
        c.execute("SELECT pg_advisory_lock(7)")
        c.execute(SCHEMA)
        for r in SEED:
            c.execute("INSERT INTO products(id,name,category,price,emoji,bg) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING", r)
        for i, n, p, k in POLY:
            imgs = ["/static/img/p%d_%d.jpg" % (i, x) for x in range(4)]
            hl = [["Size", "6x8 inch"], ["Material", "Opaque (non-transparent) poly"], ["Net Quantity (N)", "Pack Of %d" % k],
                  ["Closure", "Strong adhesive seal"], ["Extras", "Printed QR code"], ["Use", "Courier / shipping"]]
            c.execute("INSERT INTO products(id,name,category,price,emoji,bg,images,size_label,highlights) VALUES (%s,%s,'Packaging',%s,'📦','#f3e6f3',%s,'6x8 inch',%s) ON CONFLICT (id) DO NOTHING",
                      (i, n, p, Jsonb(imgs), Jsonb(hl)))
        c.execute("SELECT pg_advisory_unlock(7)")


init_db()


def uid():
    h = request.headers.get("Authorization", "")
    t = h[7:] if h.startswith("Bearer ") else ""
    if not t:
        abort(401)
    with db() as c:
        r = c.execute("SELECT id FROM users WHERE token=%s", (t,)).fetchone()
    if not r:
        abort(401)
    return r["id"]


def prod_json(r):
    d = {"id": r["id"], "n": r["name"], "c": r["category"], "p": r["price"], "e": r["emoji"], "bg": r["bg"]}
    if r["images"]:
        d["im"], d["ims"] = r["images"][0], r["images"]
    if r["size_label"]:
        d["sz"] = r["size_label"]
    if r["highlights"]:
        d["hl"] = r["highlights"]
    return d


def order_json(o):
    return {"id": 1000 + o["id"], "d": o["created_at"].astimezone(IST).strftime("%d %b, %I:%M %p"), "i": o["items"], "t": o["total"], "pm": o.get("pay_method") or "cod",
            "ts": int(o["created_at"].timestamp() * 1000), "st": {"cancelled": "cancelled", "return_requested": "returned"}.get(o["status"], ""), "ad": o.get("address"),
            "rt": {"r": o["return_reason"], "ts": int(o["returned_at"].timestamp() * 1000)} if o.get("returned_at") else None}


@app.get("/")
def index():
    r = send_from_directory("static", "index.html")
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/healthz")
def healthz():
    return "ok"


@app.post("/api/guest")
def guest():
    t = secrets.token_urlsafe(32)
    with db() as c:
        c.execute("INSERT INTO users(token) VALUES (%s)", (t,))
    return jsonify(token=t)


@app.get("/api/products")
def products():
    with db() as c:
        rows = c.execute("SELECT * FROM products WHERE active ORDER BY id").fetchall()
    return jsonify([prod_json(r) for r in rows])


@app.get("/api/state")
def state():
    u = uid()
    with db() as c:
        rows = c.execute("SELECT key, value FROM user_data WHERE user_id=%s", (u,)).fetchall()
        orders = c.execute("SELECT * FROM orders WHERE user_id=%s ORDER BY id DESC LIMIT 100", (u,)).fetchall()
    return jsonify(state={r["key"]: r["value"] for r in rows}, orders=[order_json(o) for o in orders])


@app.put("/api/data/<key>")
def put_data(key):
    u = uid()
    if key not in KEYS:
        abort(404)
    if request.content_length and request.content_length > 600_000:
        abort(413)
    v = (request.get_json(silent=True) or {}).get("v")
    with db() as c:
        c.execute("INSERT INTO user_data(user_id,key,value) VALUES (%s,%s,%s) ON CONFLICT (user_id,key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()",
                  (u, key, Jsonb(v)))
    return jsonify(ok=True)


@app.post("/api/orders")
def create_order():
    u = uid()
    body = request.get_json(silent=True) or {}
    cart = body.get("cart") or {}
    pm = "online" if body.get("pm") == "online" else "cod"
    qty = {}
    for k, v in cart.items():
        try:
            qty[int(k)] = max(1, min(int(v), 99))
        except (ValueError, TypeError):
            continue
    if not qty:
        abort(400)
    with db() as c:
        rows = c.execute("SELECT id, name, emoji, price FROM products WHERE active AND id = ANY(%s)", (list(qty),)).fetchall()
        if not rows:
            abort(400)
        items = [{"pid": r["id"], "n": r["name"], "e": r["emoji"], "p": r["price"], "q": qty[r["id"]]} for r in rows]
        total = sum(i["p"] * i["q"] for i in items)
        if pm == "online":
            total -= round(total * 5 / 100)  # 5% online-payment discount
        row = c.execute("SELECT value FROM user_data WHERE user_id=%s AND key='xt_addr'", (u,)).fetchone()
        addr = next((a for a in (row["value"] if row else []) if isinstance(a, dict) and a.get("d")), None)
        o = c.execute("INSERT INTO orders(user_id, items, total, pay_method, address) VALUES (%s,%s,%s,%s,%s) RETURNING *",
                      (u, Jsonb(items), total, pm, Jsonb(addr) if addr else None)).fetchone()
        c.execute("DELETE FROM user_data WHERE user_id=%s AND key='cart'", (u,))
    return jsonify(order_json(o))


def own_order(c, u, oid):
    o = c.execute("SELECT * FROM orders WHERE id=%s AND user_id=%s", (oid - 1000, u)).fetchone()
    if not o:
        abort(404)
    return o, (datetime.now(timezone.utc) - o["created_at"]).total_seconds()


@app.post("/api/orders/<int:oid>/cancel")
def cancel_order(oid):
    u = uid()
    with db() as c:
        o, age = own_order(c, u, oid)
        if o["status"] != "placed" or age >= 86400:  # cancellable until shipping (1 day)
            abort(409)
        o = c.execute("UPDATE orders SET status='cancelled' WHERE id=%s RETURNING *", (o["id"],)).fetchone()
    return jsonify(order_json(o))


@app.post("/api/orders/<int:oid>/pay")
def pay_order(oid):
    u = uid()
    with db() as c:
        o, age = own_order(c, u, oid)
        if o["status"] != "placed" or o["pay_method"] == "online" or age >= 5.5 * 86400:
            abort(409)
        total = o["total"] - round(o["total"] * 5 / 100)
        o = c.execute("UPDATE orders SET pay_method='online', total=%s WHERE id=%s RETURNING *", (total, o["id"])).fetchone()
    return jsonify(order_json(o))


RETURN_REASONS = {"Product damaged", "Wrong product received", "Quality not as expected", "Size or quantity issue", "Changed my mind"}


@app.post("/api/orders/<int:oid>/return")
def return_order(oid):
    u = uid()
    reason = ((request.get_json(silent=True) or {}).get("reason") or "").strip()
    if reason not in RETURN_REASONS:
        abort(400)
    with db() as c:
        o, age = own_order(c, u, oid)
        # delivered after 5 days; return window is 7 days after delivery
        if o["status"] != "placed" or age < 5.5 * 86400 or age > 12 * 86400:
            abort(409)
        o = c.execute("UPDATE orders SET status='return_requested', return_reason=%s, returned_at=now() WHERE id=%s RETURNING *",
                      (reason, o["id"])).fetchone()
    return jsonify(order_json(o))


@app.get("/admin/orders")
def admin_orders():
    if not ADMIN_KEY or not secrets.compare_digest(request.args.get("key", ""), ADMIN_KEY):
        abort(403)
    with db() as c:
        rows = c.execute("""SELECT o.id, o.created_at, o.items, o.total, o.status, o.pay_method, o.return_reason,
            (SELECT value FROM user_data WHERE user_id=o.user_id AND key='xt_profile') AS profile,
            (SELECT value FROM user_data WHERE user_id=o.user_id AND key='xt_addr') AS addresses
            FROM orders o ORDER BY o.id DESC LIMIT 200""").fetchall()
    for r in rows:
        r["id"] = 1000 + r["id"]
        r["created_at"] = r["created_at"].astimezone(IST).isoformat()
        if r["profile"]:
            r["profile"].pop("ph", None)
    return jsonify(rows)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
