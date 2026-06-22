"""
╔══════════════════════════════════════════════════════════════╗
║                    SELLORA — app.py                          ║
║         Complete Flask application with all routes           ║
╚══════════════════════════════════════════════════════════════╝

Requirements:
    pip install Flask Werkzeug razorpay python-dotenv Pillow

Environment variables (.env file):
    SECRET_KEY=your-secret-key-change-this
    RAZORPAY_KEY_ID=rzp_test_xxxx
    RAZORPAY_KEY_SECRET=your_secret
    PLATFORM_COMMISSION=10          # % commission deducted from seller
    SMTP_HOST=smtp.gmail.com        # optional, for emails
    SMTP_PORT=587
    SMTP_USER=you@gmail.com
    SMTP_PASS=your_app_password
    COMPANY_NAME=Xoptime
    COMPANY_GSTIN=29XXXXXXX
    COMPANY_ADDRESS=Your Address
"""

import os, re, io, csv, uuid, json, hashlib, secrets, smtplib, logging
from datetime import datetime, timezone
from functools import wraps
from email.mime.text import MIMEText

from flask import (Flask, g, session, request, redirect, url_for,
                   render_template, flash, abort, Response, jsonify)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from PIL import Image
import psycopg2
from psycopg2.extras import RealDictCursor

try:
    import razorpay
    RAZORPAY_AVAILABLE = True
except ImportError:
    RAZORPAY_AVAILABLE = False

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    _limiter_available = True
except ImportError:
    _limiter_available = False

from dotenv import load_dotenv
load_dotenv()

# ─────────────────────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me-in-production-" + secrets.token_hex(16))

# ── Rate limiter setup ──────────────────────────────────────────
if _limiter_available:
    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=[],
        storage_uri="memory://"
    )
else:
    # Stub so decorators don't break if Flask-Limiter not installed
    class _LimiterStub:
        def limit(self, *a, **kw):
            return lambda f: f
    limiter = _LimiterStub()

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL  = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/sellora")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXT   = {"png", "jpg", "jpeg", "webp"}
MAX_IMG_SIZE  = (1200, 1200)

RZP_KEY_ID     = os.getenv("RAZORPAY_KEY_ID", "")
RZP_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
COMMISSION_PCT = float(os.getenv("PLATFORM_COMMISSION", "10"))
FREE_DELIVERY_THRESHOLD = float(os.getenv("FREE_DELIVERY_THRESHOLD", "499"))

COMPANY_NAME    = os.getenv("COMPANY_NAME", "Xoptime")

# Category → Emoji mapping (Meesho-style)
CATEGORY_EMOJIS = {
    "Women Ethnic": "👗", "Women Western": "👚", "Men": "👔", "Men's Clothing": "👔",
    "Kids": "🧒", "Baby": "👶", "Footwear": "👟", "Shoes": "👟",
    "Electronics": "📱", "Mobile": "📱", "Laptop": "💻", "Accessories": "💎",
    "Jewellery": "💎", "Jewelry": "💎", "Watches": "⌚", "Bags": "👜",
    "Home & Kitchen": "🏠", "Home Decor": "🏡", "Kitchen": "🍳", "Furniture": "🪑",
    "Beauty": "💄", "Skincare": "✨", "Health": "💊", "Sports": "🏃",
    "Toys": "🧸", "Books": "📚", "Stationery": "✏️", "Groceries": "🛒",
    "Food": "🍱", "Pet Supplies": "🐾", "Garden": "🌿", "Tools": "🔧",
    "Automotive": "🚗", "Travel": "🧳", "Music": "🎵", "Art": "🎨",
    "Gaming": "🎮", "Camera": "📷", "Appliances": "🏠",
}
COMPANY_GSTIN   = os.getenv("COMPANY_GSTIN", "")
COMPANY_ADDRESS = os.getenv("COMPANY_ADDRESS", "")

# Sandbox.co.in KYC API — console.sandbox.co.in pe API key milegi
SANDBOX_API_KEY    = os.getenv("SANDBOX_API_KEY", "")
SANDBOX_API_SECRET = os.getenv("SANDBOX_API_SECRET", "")

# Shiprocket credentials (shiprocket.in pe account banao)
SHIPROCKET_EMAIL    = os.getenv("SHIPROCKET_EMAIL", "")
SHIPROCKET_PASSWORD = os.getenv("SHIPROCKET_PASSWORD", "")
SHIPROCKET_CHANNEL  = os.getenv("SHIPROCKET_CHANNEL_ID", "")
SHIPROCKET_PICKUP   = os.getenv("SHIPROCKET_PICKUP_LOCATION", "Primary")

# Delhivery B2C credentials (app.delhivery.com pe account banao)
DELHIVERY_TOKEN       = os.getenv("DELHIVERY_TOKEN", "")           # Bearer token
DELHIVERY_WAREHOUSE   = os.getenv("DELHIVERY_WAREHOUSE_NAME", "")  # Pickup warehouse name
DELHIVERY_CLIENT_NAME = os.getenv("DELHIVERY_CLIENT_NAME", "")     # Your registered client name
DELHIVERY_MODE        = os.getenv("DELHIVERY_MODE", "Express")     # Express / Surface

# Which courier to use: "shiprocket" or "delhivery" (default: shiprocket)
DELIVERY_PARTNER      = os.getenv("DELIVERY_PARTNER", "shiprocket").lower()

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ── Cloudinary config ──────────────────────────────────────────
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_API_KEY    = os.getenv("CLOUDINARY_API_KEY", "")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "")
USE_CLOUDINARY = all([CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET])

if USE_CLOUDINARY:
    import cloudinary
    import cloudinary.uploader
    cloudinary.config(
        cloud_name = CLOUDINARY_CLOUD_NAME,
        api_key    = CLOUDINARY_API_KEY,
        api_secret = CLOUDINARY_API_SECRET,
        secure     = True
    )

# ── File upload size limit (10MB max) ──────────────────────────
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB

# ── Logging setup ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Database helpers
# ─────────────────────────────────────────────────────────────
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        db.autocommit = False
    return db

@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_database", None)
    if db is not None:
        if exc:
            db.rollback()
        db.close()



def _scalar(cur):
    """Get first value from a fetchone() result (works with RealDictCursor)."""
    row = cur.fetchone()
    if row is None:
        return None
    return list(row.values())[0]

def _executemany(conn, sql, params_list):
    cur = conn.cursor()
    cur.executemany(sql, params_list)
    conn.commit()
    cur.close()

def _exec_script(conn, sql):
    """Execute multiple SQL statements (psycopg2 compatible)."""
    cur = conn.cursor()
    # Split on semicolons, skip empty/comment lines
    for stmt in sql.split(';'):
        stmt = stmt.strip()
        if stmt and not stmt.startswith('--'):
            try:
                cur.execute(stmt)
                conn.commit()
            except psycopg2.errors.DuplicateTable:
                conn.rollback()
                logger.debug("Table already exists, skipping.")
            except psycopg2.errors.DuplicateColumn:
                conn.rollback()
                logger.debug("Column already exists, skipping.")
            except psycopg2.errors.DuplicateObject:
                conn.rollback()
                logger.debug("Object already exists, skipping.")
            except Exception as e:
                conn.rollback()
                logger.warning(f"DDL warning ({type(e).__name__}): {e}")
    cur.close()

def _scalar(cur):
    """Get first value from a fetchone() result (works with RealDictCursor)."""
    row = cur.fetchone()
    if row is None:
        return None
    return list(row.values())[0]


def _exec(conn, sql, params=None):
    """Execute a single statement, return cursor. Rolls back on error to keep transaction clean."""
    cur = conn.cursor()
    try:
        cur.execute(sql, params or ())
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    return cur



def init_db():
    """Create all tables if they don't exist."""
    conn = get_db()
    _exec_script(conn, """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'buyer',
        phone TEXT,
        address TEXT,
        pincode TEXT,
        gstin TEXT,
        seller_status TEXT DEFAULT 'active',
        pan TEXT,
        bank_name TEXT,
        bank TEXT,
        bank_account TEXT,
        bank_ifsc TEXT,
        upi_id TEXT,
        referral_code TEXT UNIQUE,
        referred_by TEXT,
        wallet_balance REAL DEFAULT 0,
        saved_addresses TEXT DEFAULT '[]',
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS products (
        id SERIAL PRIMARY KEY,
        seller_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'General',
        description TEXT,
        brand TEXT,
        price REAL NOT NULL,
        mrp REAL,
        gst_percent REAL DEFAULT 18,
        hsn TEXT,
        stock INTEGER DEFAULT 0,
        weight_grams REAL,
        size_options TEXT,
        color_options TEXT,
        catalog_name TEXT,
        style_code TEXT,
        image_url TEXT,
        approved INTEGER DEFAULT 0,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        generic_name TEXT,
        material TEXT,
        pattern TEXT,
        occasion TEXT,
        country_of_origin TEXT DEFAULT 'India',
        net_quantity TEXT,
        dimension_unit TEXT DEFAULT 'cm',
        product_length REAL,
        product_width REAL,
        manufacturer_name TEXT,
        manufacturer_address TEXT,
        manufacturer_pincode TEXT,
        packer_name TEXT,
        packer_address TEXT,
        packer_pincode TEXT,
        importer_name TEXT,
        importer_address TEXT,
        importer_pincode TEXT,
        tags TEXT,
        closure TEXT,
        fold_type TEXT,
        product_height REAL,
        product_type TEXT,
        compartments TEXT,
        FOREIGN KEY(seller_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS product_images (
        id SERIAL PRIMARY KEY,
        product_id INTEGER NOT NULL,
        url TEXT NOT NULL,
        thumb_url TEXT,
        sort_order INTEGER DEFAULT 0,
        FOREIGN KEY(product_id) REFERENCES products(id)
    );

    CREATE TABLE IF NOT EXISTS product_variants (
        id SERIAL PRIMARY KEY,
        product_id INTEGER NOT NULL,
        size TEXT,
        color TEXT,
        price REAL,
        stock INTEGER DEFAULT 0,
        FOREIGN KEY(product_id) REFERENCES products(id)
    );

    CREATE TABLE IF NOT EXISTS cart_items (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        qty INTEGER DEFAULT 1,
        size TEXT,
        color TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS wishlist_items (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(user_id, product_id)
    );

    CREATE TABLE IF NOT EXISTS orders (
        id SERIAL PRIMARY KEY,
        public_id TEXT UNIQUE NOT NULL,
        buyer_id INTEGER NOT NULL,
        buyer_name TEXT,
        phone TEXT,
        address TEXT,
        pincode TEXT,
        pay_mode TEXT DEFAULT 'COD',
        payment_id TEXT,
        payment_status TEXT DEFAULT 'pending',
        status TEXT DEFAULT 'pending',
        subtotal REAL DEFAULT 0,
        gst_total REAL DEFAULT 0,
        shipping REAL DEFAULT 0,
        discount REAL DEFAULT 0,
        total_amount REAL DEFAULT 0,
        coupon_code TEXT,
        courier_name TEXT,
        awb TEXT,
        tracking_url TEXT,
        invoice_no TEXT,
        invoice_date TIMESTAMPTZ,
        shipped_at TIMESTAMPTZ,
        delivered_at TIMESTAMPTZ,
        notes TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS order_items (
        id SERIAL PRIMARY KEY,
        order_id INTEGER NOT NULL,
        product_id INTEGER,
        seller_id INTEGER,
        title TEXT,
        qty INTEGER DEFAULT 1,
        price REAL,
        gst_percent REAL DEFAULT 0,
        line_total REAL,
        size TEXT,
        color TEXT,
        FOREIGN KEY(order_id) REFERENCES orders(id)
    );

    CREATE TABLE IF NOT EXISTS reviews (
        id SERIAL PRIMARY KEY,
        product_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        rating INTEGER NOT NULL,
        comment TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(product_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS review_images (
        id SERIAL PRIMARY KEY,
        review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
        url TEXT NOT NULL,
        sort_order INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS return_requests (
        id SERIAL PRIMARY KEY,
        order_item_id INTEGER NOT NULL,
        buyer_id INTEGER NOT NULL,
        reason TEXT,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS notifications (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        title TEXT,
        message TEXT,
        is_read INTEGER DEFAULT 0,
        link TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS support_tickets (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        subject TEXT,
        message TEXT,
        status TEXT DEFAULT 'open',
        admin_reply TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS coupons (
        id SERIAL PRIMARY KEY,
        code TEXT UNIQUE NOT NULL,
        discount_type TEXT DEFAULT 'percent',
        discount_value REAL NOT NULL,
        min_order REAL DEFAULT 0,
        max_uses INTEGER DEFAULT 100,
        uses INTEGER DEFAULT 0,
        expires_at TIMESTAMPTZ,
        active INTEGER DEFAULT 1,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS seller_transactions (
        id SERIAL PRIMARY KEY,
        seller_id INTEGER NOT NULL,
        order_id INTEGER,
        order_item_id INTEGER,
        type TEXT,
        amount REAL,
        commission REAL DEFAULT 0,
        net_amount REAL,
        status TEXT DEFAULT 'pending',
        notes TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS password_reset_tokens (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        token TEXT UNIQUE NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        used INTEGER DEFAULT 0
    );
    """)
    conn.commit()

    # ── NEW TABLES for Meesho-like features ──────────────────────────
    _exec_script(conn, """
    CREATE TABLE IF NOT EXISTS flash_sales (
        id SERIAL PRIMARY KEY,
        title TEXT NOT NULL,
        subtitle TEXT,
        discount_pct REAL DEFAULT 0,
        starts_at TIMESTAMPTZ,
        ends_at TIMESTAMPTZ,
        banner_color TEXT DEFAULT '#8B5CF6',
        active INTEGER DEFAULT 1,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS banners (
        id SERIAL PRIMARY KEY,
        title TEXT,
        subtitle TEXT,
        cta_text TEXT DEFAULT 'Shop Now',
        cta_link TEXT DEFAULT '/search',
        bg_color TEXT DEFAULT '#1e1b4b',
        accent_color TEXT DEFAULT '#8B5CF6',
        active INTEGER DEFAULT 1,
        sort_order INTEGER DEFAULT 0,
        product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
        image_url TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    ALTER TABLE banners ADD COLUMN IF NOT EXISTS product_id INTEGER REFERENCES products(id) ON DELETE SET NULL;
    ALTER TABLE banners ADD COLUMN IF NOT EXISTS image_url TEXT;

    CREATE TABLE IF NOT EXISTS resellers (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL UNIQUE,
        shop_name TEXT,
        bio TEXT,
        total_earnings REAL DEFAULT 0,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS reseller_catalogs (
        id SERIAL PRIMARY KEY,
        reseller_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        margin REAL DEFAULT 0,
        custom_title TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(reseller_id, product_id),
        FOREIGN KEY(reseller_id) REFERENCES resellers(id),
        FOREIGN KEY(product_id) REFERENCES products(id)
    );

    CREATE TABLE IF NOT EXISTS pincode_serviceability (
        id SERIAL PRIMARY KEY,
        pincode TEXT NOT NULL,
        city TEXT,
        state TEXT,
        serviceable INTEGER DEFAULT 1,
        cod_available INTEGER DEFAULT 1,
        delivery_days INTEGER DEFAULT 5
    );

    CREATE TABLE IF NOT EXISTS review_images (
        id SERIAL PRIMARY KEY,
        review_id INTEGER NOT NULL,
        url TEXT NOT NULL,
        sort_order INTEGER DEFAULT 0,
        FOREIGN KEY(review_id) REFERENCES reviews(id)
    );

    CREATE TABLE IF NOT EXISTS product_qa (
        id SERIAL PRIMARY KEY,
        product_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        question TEXT NOT NULL,
        answer TEXT,
        answered_by INTEGER,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        FOREIGN KEY(product_id) REFERENCES products(id)
    );
    CREATE TABLE IF NOT EXISTS deleted_users (
        id SERIAL PRIMARY KEY,
        original_id INTEGER,
        name TEXT,
        email TEXT,
        phone TEXT,
        role TEXT,
        gstin TEXT,
        pan TEXT,
        seller_status TEXT,
        deleted_at TIMESTAMPTZ DEFAULT NOW(),
        deleted_by TEXT DEFAULT 'admin',
        reason TEXT DEFAULT 'Admin deleted'
    );
    """)
    conn.commit()

    # Migrate existing DB — add new product columns if missing
    new_cols = [
        ('generic_name', 'TEXT'), ('material', 'TEXT'), ('pattern', 'TEXT'),
        ('occasion', 'TEXT'), ('country_of_origin', "TEXT DEFAULT 'India'"),
        ('net_quantity', 'TEXT'), ('dimension_unit', "TEXT DEFAULT 'cm'"),
        ('product_length', 'REAL'), ('product_width', 'REAL'),
        ('manufacturer_name', 'TEXT'), ('manufacturer_address', 'TEXT'),
        ('manufacturer_pincode', 'TEXT'), ('packer_name', 'TEXT'),
        ('packer_address', 'TEXT'), ('packer_pincode', 'TEXT'),
        ('importer_name', 'TEXT'), ('importer_address', 'TEXT'),
        ('importer_pincode', 'TEXT'),
        ('tags', 'TEXT'),
        ('closure', 'TEXT'), ('fold_type', 'TEXT'), ('product_height', 'REAL'),
        ('product_type', 'TEXT'), ('compartments', 'TEXT'),
        ('trending', 'INTEGER DEFAULT 0'), ('is_flash_sale', 'INTEGER DEFAULT 0'),
        ('flash_sale_price', 'REAL'),
        ('size_chart_data', 'TEXT'),
    ]
    existing = {row['column_name'] for row in _exec(conn, "SELECT column_name FROM information_schema.columns WHERE table_name=%s AND table_schema='public'", ('products',)).fetchall()}
    for col, col_type in new_cols:
        if col not in existing:
            _exec(conn, f"ALTER TABLE products ADD COLUMN {col} {col_type}")

    # Migrate users table
    user_cols = [('is_reseller', 'INTEGER DEFAULT 0'), ('reseller_status', "TEXT DEFAULT 'inactive'"),
                 ('saved_addresses', "TEXT DEFAULT '[]'"), ('wallet_balance', 'REAL DEFAULT 0'),
                 ('buyer_cashback', 'REAL DEFAULT 0'), ('on_vacation', 'INTEGER DEFAULT 0'),
                 ('push_subscription', 'TEXT'),
                 ('pan_verified', 'INTEGER DEFAULT 0'),
                 ('pan_name', 'TEXT'),
                 ('gstin_verified', 'INTEGER DEFAULT 0'),
                 ('bank_verified', 'INTEGER DEFAULT 0'),
                 ('kyc_step', 'INTEGER DEFAULT 0'),
                 ('aadhaar_verified', 'INTEGER DEFAULT 0'),
                 ('aadhaar_ref_id', 'TEXT'),
                 ('state', 'TEXT'),
                 ('bank_bank', 'TEXT'),
                 ('gstin_name', 'TEXT'),
                 ('gst_suspended', 'INTEGER DEFAULT 0')]
    existing_u = {row['column_name'] for row in _exec(conn, "SELECT column_name FROM information_schema.columns WHERE table_name=%s AND table_schema='public'", ('users',)).fetchall()}
    for col, col_type in user_cols:
        if col not in existing_u:
            _exec(conn, f"ALTER TABLE users ADD COLUMN {col} {col_type}")

    # Migrate reviews table
    review_cols = [('body', 'TEXT'), ('buyer_name', 'TEXT'), ('updated_at', "TEXT DEFAULT (NOW())")]
    existing_r = {row['column_name'] for row in _exec(conn, "SELECT column_name FROM information_schema.columns WHERE table_name=%s AND table_schema='public'", ('reviews',)).fetchall()}
    for col, col_type in review_cols:
        if col not in existing_r:
            _exec(conn, f"ALTER TABLE reviews ADD COLUMN {col} {col_type}")

    # Migrate review_images table — add sort_order if missing
    existing_ri = {row["column_name"] for row in _exec(conn, "SELECT column_name FROM information_schema.columns WHERE table_name=%s AND table_schema='public'", ("review_images",)).fetchall()}
    if "sort_order" not in existing_ri:
        _exec(conn, "ALTER TABLE review_images ADD COLUMN sort_order INTEGER DEFAULT 0")

    # Migrate orders table
    order_cols = [('reseller_id', 'INTEGER'), ('reseller_margin', 'REAL DEFAULT 0'),
                  ('cod_verified', 'INTEGER DEFAULT 0'), ('state', 'TEXT'),
                  ('invoice_generated', 'INTEGER DEFAULT 1'),
                  ('invoice_no', 'TEXT'), ('invoice_date', 'TIMESTAMPTZ'),
                  ('coupon_code', 'TEXT'), ('discount', 'REAL DEFAULT 0'),
                  ('payment_id', 'TEXT'), ('payment_status', 'TEXT'),
                  ('tracking_id', 'TEXT'), ('tracking_url', 'TEXT'),
                  ('courier_name', 'TEXT'), ('awb', 'TEXT'), ('public_id', 'TEXT'),
                  ('updated_at', 'TIMESTAMPTZ DEFAULT NOW()'),
                  ('cancelled_at', 'TIMESTAMPTZ'),
                  ('cancel_reason', 'TEXT'),
                  ('cancelled_by', 'TEXT'),
                  ('refund_id', 'TEXT'),
                  ('refund_status', 'TEXT'),
                  ('refund_amount', 'REAL'),
                  ('refund_completed_at', 'TIMESTAMPTZ')]
    existing_o = {row['column_name'] for row in _exec(conn, "SELECT column_name FROM information_schema.columns WHERE table_name=%s AND table_schema='public'", ('orders',)).fetchall()}
    for col, col_type in order_cols:
        if col not in existing_o:
            _exec(conn, f"ALTER TABLE orders ADD COLUMN {col} {col_type}")

    # ── Fix legacy capitalized status values ──────────────────────
    _exec(conn, "UPDATE orders SET status='delivered'     WHERE status IN ('Delivered')")
    _exec(conn, "UPDATE orders SET status='shipped'       WHERE status IN ('Shipped')")
    _exec(conn, "UPDATE orders SET status='processing'    WHERE status IN ('Processing','Accepted','accepted')")
    _exec(conn, "UPDATE orders SET status='placed'        WHERE status IN ('Pending','pending')")
    _exec(conn, "UPDATE orders SET status='shipped'       WHERE status IN ('ReadyToShip','ready_to_ship')")

    conn.commit()

    # ── Performance indexes ────────────────────────────────────────
    _exec_script(conn, """
    CREATE INDEX IF NOT EXISTS idx_products_approved    ON products(approved);
    CREATE INDEX IF NOT EXISTS idx_products_seller      ON products(seller_id);
    CREATE INDEX IF NOT EXISTS idx_products_category    ON products(category);
    CREATE INDEX IF NOT EXISTS idx_orders_buyer         ON orders(buyer_id);
    CREATE INDEX IF NOT EXISTS idx_orders_status        ON orders(status);
    CREATE INDEX IF NOT EXISTS idx_order_items_order    ON order_items(order_id);
    CREATE INDEX IF NOT EXISTS idx_order_items_seller   ON order_items(seller_id);
    CREATE INDEX IF NOT EXISTS idx_cart_user            ON cart_items(user_id);
    CREATE INDEX IF NOT EXISTS idx_notifications_user   ON notifications(user_id);
    CREATE INDEX IF NOT EXISTS idx_reviews_product      ON reviews(product_id);
    """)
    conn.commit()
    if not _exec(conn, "SELECT id FROM banners LIMIT 1").fetchone():
        _executemany(conn, "INSERT INTO banners (title,subtitle,cta_text,cta_link,bg_color,accent_color,sort_order) VALUES (%s,%s,%s,%s,%s,%s,%s)", [
                ("Fashion Sale — Up to 80% Off", "India ke top sellers se direct kharido", "Shop Now", "/search%scategory=Fashion", "#0f172a", "#8B5CF6", 0),
                ("New Arrivals Every Day", "Trending products, lowest prices", "Explore", "/search", "#0c1a0c", "#22c55e", 1),
                ("Sell on Xoptime", "Apna business shuru karo — free mein", "Become a Seller", "/register", "#1a0a00", "#f59e0b", 2),
            ])
        conn.commit()

    # Create default admin if not exists
    admin = _exec(conn, "SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
    if not admin:
        _exec(conn, 
            "INSERT INTO users (name,email,password,role) VALUES (%s,%s,%s,%s)",
            ("Admin", "admin@xoptime.com",
             generate_password_hash("admin123"), "admin")
        )
        conn.commit()
        print("Default admin created → email: admin@xoptime.com | password: admin123")


# ─────────────────────────────────────────────────────────────
# CSRF
# ─────────────────────────────────────────────────────────────
def generate_csrf():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(32)
    return session["_csrf"]

def csrf_token():
    return generate_csrf()

@app.context_processor
def inject_globals():
    cart_count   = 0
    unread_notif = 0
    if session.get("role") == "buyer":
        conn = get_db()
        cart_count = _scalar(_exec(conn, 
            "SELECT COALESCE(SUM(qty),0) FROM cart_items WHERE user_id=%s",
            (session["user_id"],)))
    if session.get("user_id"):
        conn = get_db()
        unread_notif = _scalar(_exec(conn, 
            "SELECT COUNT(*) FROM notifications WHERE user_id=%s AND is_read=0",
            (session["user_id"],)))
    return dict(
        csrf_token=csrf_token,
        cart_count=cart_count,
        unread_notif=unread_notif,
        rzp_key_id=RZP_KEY_ID,
        is_reseller=session.get("is_reseller", False),
    )

def check_csrf():
    token = request.form.get("csrf_token", "")
    if not token or token != session.get("_csrf"):
        abort(403, "CSRF validation failed.")


# ─────────────────────────────────────────────────────────────
# Auth decorators
# ─────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please login to continue.", "err")
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated

def seller_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id") or session.get("role") != "seller":
            abort(403)
        # KYC gate — /seller/kyc routes bypass karte hain
        if request.path not in ("/seller/kyc",) and not request.path.startswith("/seller/kyc/"):
            conn = get_db()
            seller = _exec(conn, "SELECT pan_verified, gstin_verified, bank_verified FROM users WHERE id=%s",
                           (session["user_id"],)).fetchone()
            if seller:
                kyc_done = bool(seller["pan_verified"] and seller["gstin_verified"] and seller["bank_verified"])
                if not kyc_done:
                    flash("Seller dashboard access ke liye pehle KYC complete karo.", "err")
                    return redirect("/seller/kyc")
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id") or session.get("role") != "admin":
            flash("Admin login required.", "err")
            return redirect("/admin/login")
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────────────────────────
# Helper utilities
# ─────────────────────────────────────────────────────────────
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT

def save_image(file_obj):
    """Save uploaded image, return (url, thumb_url).
    Uses Cloudinary if configured, otherwise saves locally."""
    if not file_obj or not allowed_file(file_obj.filename):
        return None, None

    uid = uuid.uuid4().hex

    if USE_CLOUDINARY:
        import cloudinary.uploader as _cu
        import io
        img = Image.open(file_obj.stream).convert("RGB")
        img.thumbnail(MAX_IMG_SIZE, Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85, optimize=True)
        buf.seek(0)
        result = _cu.upload(
            buf,
            public_id=f"xoptime/products/{uid}",
            overwrite=True,
            resource_type="image"
        )
        url = result["secure_url"]
        thumb_url = result["secure_url"].replace(
            "/upload/", "/upload/w_400,h_400,c_fill/"
        )
        return url, thumb_url
    else:
        fname = f"{uid}.jpg"
        tname = f"{uid}_thumb.jpg"
        fpath = os.path.join(UPLOAD_FOLDER, fname)
        tpath = os.path.join(UPLOAD_FOLDER, tname)
        img = Image.open(file_obj.stream).convert("RGB")
        img.thumbnail(MAX_IMG_SIZE, Image.LANCZOS)
        img.save(fpath, "JPEG", quality=85, optimize=True)
        img.thumbnail((400, 400), Image.LANCZOS)
        img.save(tpath, "JPEG", quality=80, optimize=True)
        return f"/static/uploads/{fname}", f"/static/uploads/{tname}"

def add_notification(user_id, title, message, link=None):
    conn = get_db()
    _exec(conn,
        "INSERT INTO notifications (user_id,title,message,link) VALUES (%s,%s,%s,%s)",
        (user_id, title, message, link)
    )
    conn.commit()

def send_email(to, subject, body):
    """Send email via SMTP STARTTLS (port 587). Silently fails if not configured."""
    host = os.getenv("SMTP_HOST")
    if not host:
        return
    def _send():
        try:
            msg = MIMEText(body, "html")
            msg["Subject"] = subject
            msg["From"]    = os.getenv("SMTP_USER", "no-reply@xoptime.com")
            msg["To"]      = to
            # Port 587 + STARTTLS — Render pe 465/SSL block hai, 587 kaam karta hai
            with smtplib.SMTP(host, 587, timeout=15) as s:
                s.ehlo()
                s.starttls()
                s.ehlo()
                s.login(os.getenv("SMTP_USER",""), os.getenv("SMTP_PASS",""))
                s.send_message(msg)
        except Exception as e:
            logger.warning(f"Email send failed to {to}: {e}")
    import threading
    threading.Thread(target=_send, daemon=True).start()


# ─────────────────────────────────────────────────────────────
# ── SHIPROCKET INTEGRATION
# ─────────────────────────────────────────────────────────────
import urllib.request as _urllib_req

_shiprocket_token = None
_shiprocket_token_expiry = None

def shiprocket_get_token():
    """Login to Shiprocket API and get JWT token. Cached per process."""
    global _shiprocket_token, _shiprocket_token_expiry
    if not SHIPROCKET_EMAIL or not SHIPROCKET_PASSWORD:
        return None
    # Return cached token if still valid (tokens last 24h)
    from datetime import datetime, timezone, timedelta
    if _shiprocket_token and _shiprocket_token_expiry and datetime.now() < _shiprocket_token_expiry:
        return _shiprocket_token
    try:
        import json as _json
        data = _json.dumps({
            "email": SHIPROCKET_EMAIL,
            "password": SHIPROCKET_PASSWORD
        }).encode()
        req = _urllib_req.Request(
            "https://apiv2.shiprocket.in/v1/external/auth/login",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with _urllib_req.urlopen(req, timeout=10) as resp:
            result = _json.loads(resp.read())
            _shiprocket_token = result.get("token")
            _shiprocket_token_expiry = datetime.now() + timedelta(hours=23)
            return _shiprocket_token
    except Exception as e:
        logger.error(f"[Shiprocket] Login failed: {e}")
        return None


def shiprocket_create_order(order, items, seller):
    """
    Create a shipment order on Shiprocket and request pickup.
    Returns dict with: success, awb, shipment_id, courier_name, error
    """
    import json as _json

    token = shiprocket_get_token()
    if not token:
        return {"success": False, "error": "Shiprocket credentials not configured in .env"}

    # Build order items for Shiprocket
    sr_items = []
    for it in items:
        sr_items.append({
            "name":      it["title"],
            "sku":       f"SKU-{it['product_id'] or it['id']}",
            "units":     it["qty"],
            "selling_price": float(it["price"]),
            "discount":  0,
            "tax":       float(it["gst_percent"]),
            "hsn":       ""
        })

    # Total weight estimate (500g per item if not set)
    total_weight = max(0.5, len(items) * 0.5)

    payload = {
        "order_id":          order["public_id"],
        "order_date":        order["created_at"][:10],
        "pickup_location":   SHIPROCKET_PICKUP,
        "channel_id":        SHIPROCKET_CHANNEL or "",
        "comment":           "Xoptime Order",
        "billing_customer_name":  order["buyer_name"],
        "billing_last_name":      "",
        "billing_address":        order["address"],
        "billing_address_2":      "",
        "billing_city":           "City",
        "billing_pincode":        order["pincode"] or "110001",
        "billing_state":          "State",
        "billing_country":        "India",
        "billing_email":          seller["email"] if seller else "seller@xoptime.com",
        "billing_phone":          order["phone"],
        "shipping_is_billing":    True,
        "order_items":            sr_items,
        "payment_method":         "COD" if order["pay_mode"] == "COD" else "Prepaid",
        "shipping_charges":       0,
        "giftwrap_charges":       0,
        "transaction_charges":    0,
        "total_discount":         float(order["discount"] or 0),
        "sub_total":              float(order["total_amount"]),
        "length":                 10,
        "breadth":                10,
        "height":                 10,
        "weight":                 total_weight,
    }

    try:
        data = _json.dumps(payload).encode()
        req  = _urllib_req.Request(
            "https://apiv2.shiprocket.in/v1/external/orders/create/adhoc",
            data=data,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {token}"
            },
            method="POST"
        )
        with _urllib_req.urlopen(req, timeout=15) as resp:
            result = _json.loads(resp.read())

        shipment_id = result.get("shipment_id")
        if not shipment_id:
            return {"success": False, "error": str(result.get("message", "Unknown error"))}

        # Now request courier auto-assignment
        assign_payload = _json.dumps({
            "shipment_id": [shipment_id],
            "courier_id":  ""   # empty = auto-assign best courier
        }).encode()
        assign_req = _urllib_req.Request(
            "https://apiv2.shiprocket.in/v1/external/courier/assign/awb",
            data=assign_payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {token}"
            },
            method="POST"
        )
        with _urllib_req.urlopen(assign_req, timeout=15) as resp2:
            assign_result = _json.loads(resp2.read())

        awb_data     = assign_result.get("response", {}).get("data", {}).get("awb_assign_status_code")
        awb          = assign_result.get("response", {}).get("data", {}).get("awb_code", "")
        courier_name = assign_result.get("response", {}).get("data", {}).get("courier_name", "")

        # Schedule pickup
        pickup_payload = _json.dumps({"shipment_id": [str(shipment_id)]}).encode()
        pickup_req = _urllib_req.Request(
            "https://apiv2.shiprocket.in/v1/external/courier/generate/pickup",
            data=pickup_payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {token}"
            },
            method="POST"
        )
        with _urllib_req.urlopen(pickup_req, timeout=15) as resp3:
            pickup_result = _json.loads(resp3.read())

        pickup_status = pickup_result.get("pickup_status", 1)

        return {
            "success":      True,
            "shipment_id":  shipment_id,
            "awb":          awb,
            "courier_name": courier_name,
            "pickup_scheduled": pickup_status == 1,
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


def shiprocket_track(awb):
    """Track a shipment by AWB number."""
    import json as _json
    token = shiprocket_get_token()
    if not token or not awb:
        return None
    try:
        req = _urllib_req.Request(
            f"https://apiv2.shiprocket.in/v1/external/courier/track/awb/{awb}",
            headers={"Authorization": f"Bearer {token}"},
            method="GET"
        )
        with _urllib_req.urlopen(req, timeout=10) as resp:
            return _json.loads(resp.read())
    except Exception as e:
        logger.warning(f"[Shiprocket] Track failed for awb {awb}: {e}")
        return None

# ── DELHIVERY B2C INTEGRATION ─────────────────────────────────
# Delhivery Direct API (app.delhivery.com)
# Docs: https://developers.delhivery.com/docs

def delhivery_create_shipment(order, items, seller):
    """
    Create a B2C shipment on Delhivery and request pickup.
    Returns dict with: success, awb, courier_name, error
    """
    import json as _json

    if not DELHIVERY_TOKEN or not DELHIVERY_WAREHOUSE:
        return {"success": False, "error": "Delhivery credentials not configured in .env (DELHIVERY_TOKEN, DELHIVERY_WAREHOUSE_NAME)"}

    # Build shipment data
    total_weight = max(0.5, len(items) * 0.5)  # kg, 500g per item estimate
    cod_amount   = float(order["total_amount"]) if order["pay_mode"] == "COD" else 0

    shipment_data = {
        "shipments": [
            {
                "name":            order["buyer_name"],
                "add":             order["address"],
                "pin":             str(order["pincode"] or "110001"),
                "city":            order.get("city", "City"),
                "state":           order.get("state", "State"),
                "country":         "India",
                "phone":           str(order["phone"]),
                "order":           order["public_id"],
                "payment_mode":    "COD" if order["pay_mode"] == "COD" else "Prepaid",
                "return_pin":      "",
                "return_city":     "",
                "return_phone":    "",
                "return_add":      "",
                "return_state":    "",
                "return_country":  "",
                "products_desc":   ", ".join([it["title"] for it in items]),
                "hsn_code":        "",
                "cod_amount":      str(cod_amount),
                "order_date":      str(order["created_at"])[:10],
                "total_amount":    str(float(order["total_amount"])),
                "seller_add":      seller.get("address", "") if seller else "",
                "seller_name":     seller.get("full_name", DELHIVERY_CLIENT_NAME) if seller else DELHIVERY_CLIENT_NAME,
                "seller_inv":      order["public_id"],
                "quantity":        str(sum(it["qty"] for it in items)),
                "weight":          str(total_weight),
                "volumetric_weight": "",
                "invoice_number":  order["public_id"],
                "shipment_length": "10",
                "shipment_width":  "10",
                "shipment_height": "10",
                "weight_charged":  "",
                "required_temperature": "",
                "min_temperature": "",
                "exp_date":        "",
                "invoice_date":    str(order["created_at"])[:10],
                "invoice_amount":  str(float(order["total_amount"])),
                "fragile_shipment": False,
                "courier":         "",        # empty = auto-assign
                "mode":            DELHIVERY_MODE,
                "pickup_location": DELHIVERY_WAREHOUSE,
                "client":          DELHIVERY_CLIENT_NAME,
            }
        ],
        "pickup_location": {
            "name": DELHIVERY_WAREHOUSE,
        }
    }

    try:
        # Step 1: Create shipment
        import urllib.parse as _urlparse
        form_data = ("format=json&data=" + _urlparse.quote(_json.dumps(shipment_data))).encode()
        req = _urllib_req.Request(
            "https://track.delhivery.com/api/cmu/create.json",
            data=form_data,
            headers={
                "Content-Type":  "application/x-www-form-urlencoded",
                "Authorization": f"Token {DELHIVERY_TOKEN}",
                "Accept":        "application/json",
            },
            method="POST"
        )
        with _urllib_req.urlopen(req, timeout=15) as resp:
            result = _json.loads(resp.read())

        # Extract AWB from response
        packages = result.get("packages", [])
        if not packages or packages[0].get("status") not in ("Success", "success"):
            err_msg = packages[0].get("remarks", str(result)) if packages else str(result)
            return {"success": False, "error": f"Delhivery create failed: {err_msg}"}

        awb = packages[0].get("waybill", "")
        if not awb:
            return {"success": False, "error": "Delhivery ne AWB nahi diya"}

        # Step 2: Schedule pickup
        pickup_payload = _json.dumps({
            "pickup_time":     "10:00 AM - 06:00 PM",
            "pickup_date":     __import__('datetime').date.today().strftime("%Y-%m-%d"),
            "pickup_location": DELHIVERY_WAREHOUSE,
            "expected_package_count": 1,
            "waybills": [awb],
        }).encode()
        pickup_req = _urllib_req.Request(
            "https://track.delhivery.com/fm/request/new/",
            data=pickup_payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Token {DELHIVERY_TOKEN}",
                "Accept":        "application/json",
            },
            method="POST"
        )
        try:
            with _urllib_req.urlopen(pickup_req, timeout=15) as pr:
                pickup_result = _json.loads(pr.read())
            pickup_scheduled = pickup_result.get("success", False)
        except Exception as pe:
            logger.warning(f"[Delhivery] Pickup schedule failed: {pe}")
            pickup_scheduled = False

        return {
            "success":          True,
            "awb":              awb,
            "courier_name":     "Delhivery",
            "pickup_scheduled": pickup_scheduled,
        }

    except Exception as e:
        logger.error(f"[Delhivery] Create shipment error: {e}")
        return {"success": False, "error": str(e)}


def delhivery_track(awb):
    """Track a Delhivery shipment by AWB number."""
    import json as _json
    if not DELHIVERY_TOKEN or not awb:
        return None
    try:
        req = _urllib_req.Request(
            f"https://track.delhivery.com/api/v1/packages/json/?waybill={awb}&verbose=true",
            headers={
                "Authorization": f"Token {DELHIVERY_TOKEN}",
                "Accept":        "application/json",
            },
            method="GET"
        )
        with _urllib_req.urlopen(req, timeout=10) as resp:
            return _json.loads(resp.read())
    except Exception as e:
        logger.warning(f"[Delhivery] Track failed for awb {awb}: {e}")
        return None


# ─────────────────────────────────────────────────────────────

def cart_summary(user_id):
    """Return cart items and summary dict. Also returns list of unavailable item titles."""
    conn = get_db()
    # All cart items
    all_rows = _exec(conn, 
        """SELECT ci.id as cart_id, ci.qty, ci.size, ci.color,
                  p.id, p.title, p.price, p.gst_percent, p.stock, p.image_url, p.approved
           FROM cart_items ci JOIN products p ON ci.product_id=p.id
           WHERE ci.user_id=%s""",
        (user_id,)
    ).fetchall()
    items = []
    unavailable = []
    subtotal = gst_total = 0
    for r in all_rows:
        if r["stock"] <= 0 or not r["approved"]:
            unavailable.append(r["title"])
            continue
        # Cap qty to available stock
        qty   = min(r["qty"], r["stock"])
        gst   = r["price"] * r["gst_percent"] / 100
        total = (r["price"] + gst) * qty
        subtotal  += r["price"] * qty
        gst_total += gst * qty
        items.append(dict(r, qty=qty, line_total=total))
    shipping = 0 if subtotal >= FREE_DELIVERY_THRESHOLD else 49
    free_delivery_left = max(0, FREE_DELIVERY_THRESHOLD - subtotal)
    summary = {
        "subtotal":               round(subtotal, 2),
        "gst_total":              round(gst_total, 2),
        "shipping":               shipping,
        "total":                  round(subtotal + gst_total + shipping, 2),
        "unavailable":            unavailable,
        "free_delivery_left":     round(free_delivery_left, 2),
        "free_delivery_threshold": FREE_DELIVERY_THRESHOLD,
    }
    return items, summary

def generate_invoice_no():
    prefix = datetime.now().strftime("%Y%m")
    conn = get_db()
    count = _scalar(_exec(conn, 
        "SELECT COUNT(*) FROM orders WHERE invoice_no LIKE %s", (f"INV-{prefix}%",)
    ))
    return f"INV-{prefix}-{count+1:04d}"


# ─────────────────────────────────────────────────────────────
# ── AUTH ROUTES  (Email OTP via Gmail SMTP)
# ─────────────────────────────────────────────────────────────
import random as _random

# In-memory OTP store: { email: { otp, expires, user_id, name, role } }
_email_otp_store = {}


def _send_otp_email(to_email, otp):
    """Gmail SMTP se OTP email bhejo."""
    subject = f"Your Xoptime OTP: {otp}"
    body = f"""
    <div style="font-family:sans-serif;max-width:400px;margin:auto;padding:24px;border:1px solid #eee;border-radius:12px;">
      <h2 style="color:#1a0a00;">Xoptime Login OTP</h2>
      <p style="font-size:1rem;">Aapka One-Time Password:</p>
      <div style="font-size:2.5rem;font-weight:700;letter-spacing:0.5rem;color:#f59e0b;text-align:center;padding:16px 0;">
        {otp}
      </div>
      <p style="font-size:0.85rem;color:#888;">Yeh OTP 10 minute mein expire ho jaayega.<br>Kisi ke saath share mat karo.</p>
    </div>
    """
    send_email(to_email, subject, body)


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(session.pop("login_next", "/") or "/")

    if request.method == "POST":
        check_csrf()
        email = request.form.get("email", "").strip().lower()
        if not email or "@" not in email:
            flash("Valid email daalo.", "err")
            return redirect("/login")

        conn = get_db()
        user = _exec(conn, "SELECT * FROM users WHERE email=%s", (email,)).fetchone()
        if not user:
            flash("Is email se koi account nahi mila. Pehle register karo.", "err")
            return redirect("/register")

        otp = str(_random.randint(100000, 999999))
        import time
        _email_otp_store[email] = {
            "otp": otp,
            "expires": time.time() + 600,
            "user_id": user["id"],
            "name": user["name"],
            "role": user["role"],
            "is_reseller": bool(user["is_reseller"]) if "is_reseller" in user.keys() else False,
        }
        _send_otp_email(email, otp)
        session["otp_email"] = email
        flash("OTP bhej diya! Email check karo.", "ok")
        return redirect("/verify-otp")

    next_url = request.args.get("next")
    if next_url:
        session["login_next"] = next_url
    return render_template("login.html")


@app.route("/verify-otp", methods=["GET", "POST"])
def verify_otp():
    import time
    email = session.get("otp_email", "")
    if not email:
        return redirect("/login")

    if request.method == "POST":
        check_csrf()
        otp_entered = request.form.get("otp", "").strip()
        stored = _email_otp_store.get(email)

        if not stored:
            flash("OTP nahi mila. Dobara login karo.", "err")
            return redirect("/login")
        if time.time() > stored["expires"]:
            _email_otp_store.pop(email, None)
            flash("OTP expire ho gaya (10 min). Dobara try karo.", "err")
            return redirect("/login")
        if otp_entered != stored["otp"]:
            flash("Galat OTP. Dobara daalo.", "err")
            return render_template("verify_otp.html", email=email)

        # OTP sahi — login karo
        _email_otp_store.pop(email, None)
        session.pop("otp_email", None)
        session.clear()
        session.permanent = True
        session["user_id"]     = stored["user_id"]
        session["name"]        = stored["name"]
        session["role"]        = stored["role"]
        session["is_reseller"] = stored["is_reseller"]

        next_url = session.pop("login_next", None)
        flash(f"Welcome back, {stored['name']}!", "ok")
        return redirect(next_url or "/")

    return render_template("verify_otp.html", email=email)


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect("/")

    if request.method == "POST":
        check_csrf()
        name  = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        role  = request.form.get("role", "buyer")
        ref_code = request.form.get("ref", "").strip().upper()

        if not name:
            flash("Naam daalo.", "err")
            return redirect("/register")
        if not email or "@" not in email:
            flash("Valid email daalo.", "err")
            return redirect("/register")
        if role not in ("buyer", "seller"):
            role = "buyer"

        conn = get_db()
        if _exec(conn, "SELECT id FROM users WHERE email=%s", (email,)).fetchone():
            flash("Yeh email pehle se registered hai. Login karo.", "err")
            return redirect("/login")

        import random as _r, string as _s, time
        my_ref = "".join(_r.choices(_s.ascii_uppercase + _s.digits, k=8))
        _exec(conn,
            "INSERT INTO users (name, email, password, role, referral_code, referred_by) VALUES (%s,%s,%s,%s,%s,%s)",
            (name, email, "", role, my_ref, ref_code or None)
        )
        conn.commit()
        user = _exec(conn, "SELECT * FROM users WHERE email=%s", (email,)).fetchone()

        # Auto-login via OTP
        otp = str(_r.randint(100000, 999999))
        _email_otp_store[email] = {
            "otp": otp,
            "expires": time.time() + 600,
            "user_id": user["id"],
            "name": user["name"],
            "role": user["role"],
            "is_reseller": False,
        }
        _send_otp_email(email, otp)
        session["otp_email"] = email
        flash("Account ban gaya! Email check karo OTP ke liye.", "ok")
        return redirect("/verify-otp")

    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")



# ─────────────────────────────────────────────────────────────
# ── HOMEPAGE & SEARCH
# ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if session.get("role") == "seller":
        return redirect("/seller/dashboard")
    conn = get_db()
    products = _exec(conn,
        "SELECT p.*, u.name as seller_name, "
        "COALESCE(agg.avg_rating,0) avg_rating, COALESCE(agg.review_count,0) review_count "
        "FROM products p JOIN users u ON p.seller_id=u.id "
        "LEFT JOIN (SELECT product_id, AVG(rating) avg_rating, COUNT(id) review_count "
        "FROM reviews GROUP BY product_id) agg ON agg.product_id=p.id "
        "WHERE p.approved=1 AND p.stock>0 AND COALESCE((SELECT gst_suspended FROM users WHERE id=p.seller_id),0)=0 ORDER BY p.created_at DESC LIMIT 40"
    ).fetchall()
    featured = _exec(conn,
        "SELECT p.*, COALESCE(agg.avg_rating,0) avg_rating, COALESCE(agg.review_count,0) review_count "
        "FROM products p "
        "LEFT JOIN (SELECT product_id, AVG(rating) avg_rating, COUNT(id) review_count "
        "FROM reviews GROUP BY product_id) agg ON agg.product_id=p.id "
        "WHERE p.approved=1 AND p.stock>0 AND COALESCE((SELECT gst_suspended FROM users WHERE id=p.seller_id),0)=0 ORDER BY avg_rating DESC, review_count DESC LIMIT 8"
    ).fetchall()
    trending = _exec(conn,
        "SELECT p.*, COALESCE(agg.avg_rating,0) avg_rating, COALESCE(agg.review_count,0) review_count "
        "FROM products p "
        "LEFT JOIN (SELECT product_id, AVG(rating) avg_rating, COUNT(id) review_count "
        "FROM reviews GROUP BY product_id) agg ON agg.product_id=p.id "
        "WHERE p.approved=1 AND p.stock>0 AND COALESCE((SELECT gst_suspended FROM users WHERE id=p.seller_id),0)=0 AND p.trending=1 ORDER BY p.created_at DESC LIMIT 8"
    ).fetchall()
    flash_sale_products = _exec(conn,
        "SELECT p.*, COALESCE(agg.avg_rating,0) avg_rating, COALESCE(agg.review_count,0) review_count "
        "FROM products p "
        "LEFT JOIN (SELECT product_id, AVG(rating) avg_rating, COUNT(id) review_count "
        "FROM reviews GROUP BY product_id) agg ON agg.product_id=p.id "
        "WHERE p.approved=1 AND p.stock>0 AND COALESCE((SELECT gst_suspended FROM users WHERE id=p.seller_id),0)=0 AND p.is_flash_sale=1 ORDER BY p.created_at DESC LIMIT 8"
    ).fetchall()
    banners = _exec(conn, "SELECT b.*, p.image_url as product_image_url FROM banners b LEFT JOIN products p ON b.product_id=p.id WHERE b.active=1 ORDER BY b.sort_order LIMIT 5").fetchall()
    flash_sale = _exec(conn, 
        "SELECT * FROM flash_sales WHERE active=1 AND (ends_at IS NULL OR ends_at > NOW()) LIMIT 1"
    ).fetchone()
    categories = [r["category"] for r in _exec(conn, 
        "SELECT DISTINCT category FROM products WHERE approved=1 ORDER BY category").fetchall()]
    stats = {
        "products": _scalar(_exec(conn, "SELECT COUNT(*) FROM products WHERE approved=1")),
        "sellers":  _scalar(_exec(conn, "SELECT COUNT(*) FROM users WHERE role='seller'")),
        "orders":   _scalar(_exec(conn, "SELECT COUNT(*) FROM orders")),
    }
    return render_template("index.html", products=products, featured=featured,
                           trending=trending, flash_sale_products=flash_sale_products,
                           banners=banners, flash_sale=flash_sale,
                           categories=categories, stats=stats,
                           recently_viewed_ids=session.get("recently_viewed", []))


@app.route("/search")
def search():
    q        = request.args.get("q", "").strip()
    category = request.args.get("category", "")
    sort     = request.args.get("sort", "")
    min_p    = request.args.get("min", "")
    max_p    = request.args.get("max", "")
    rating   = request.args.get("rating", "")
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    per_page = 24

    # PostgreSQL-compatible: subquery for aggregates
    REV_AGG = ("LEFT JOIN (SELECT product_id, AVG(rating) avg_rating, COUNT(id) review_count "
               "FROM reviews GROUP BY product_id) agg ON agg.product_id=p.id ")
    sql = ("SELECT p.*, u.name as seller_name, "
           "COALESCE(agg.avg_rating,0) avg_rating, COALESCE(agg.review_count,0) review_count "
           "FROM products p JOIN users u ON p.seller_id=u.id " + REV_AGG +
           "WHERE p.approved=1 AND p.stock>0 AND COALESCE((SELECT gst_suspended FROM users WHERE id=p.seller_id),0)=0 ")
    params = []
    if q:
        sql += "AND (p.title LIKE %s OR p.description LIKE %s OR p.category LIKE %s OR p.brand LIKE %s) "
        params += [f"%{q}%"] * 4
    if category:
        sql += "AND p.category=%s "; params.append(category)
    brand = request.args.get("brand", "")
    if brand:
        sql += "AND p.brand=%s "; params.append(brand)
    if min_p:
        try:
            sql += "AND p.price>=%s "; params.append(float(min_p))
        except (ValueError, TypeError):
            pass
    if max_p:
        try:
            sql += "AND p.price<=%s "; params.append(float(max_p))
        except (ValueError, TypeError):
            pass
    if rating:
        try:
            rating_val = float(rating)
            sql = f"SELECT * FROM ({sql}) _f WHERE avg_rating>=%s "
            params.append(rating_val)
        except (ValueError, TypeError):
            pass
    order_map = {"price_asc": "p.price ASC", "price_desc": "p.price DESC",
                 "newest": "p.created_at DESC", "rating": "avg_rating DESC"}
    sql += f"ORDER BY {order_map.get(sort, 'p.created_at DESC')} "

    conn = get_db()
    # FIX: Use SQL LIMIT/OFFSET — don't load entire table into Python memory
    count_inner = sql.split("ORDER BY")[0]
    total = _scalar(_exec(conn, 
        f"SELECT COUNT(*) FROM ({count_inner}) _c", params
    ))
    products    = _exec(conn, sql + f"LIMIT {per_page} OFFSET {(page-1)*per_page}", params).fetchall()
    total_pages = max(1, (total + per_page - 1) // per_page)
    categories  = [r["category"] for r in _exec(conn, 
        "SELECT DISTINCT category FROM products WHERE approved=1 ORDER BY category").fetchall()]
    brands = [r["brand"] for r in _exec(conn, 
        "SELECT DISTINCT brand FROM products WHERE approved=1 AND brand IS NOT NULL AND brand!='' ORDER BY brand"
    ).fetchall()]
    qs = "&".join(f"{k}={v}" for k, v in request.args.items() if k != "page")
    return render_template("search.html", products=products, categories=categories,
                           brands=brands, total=total, page=page, total_pages=total_pages,
                           query_string=qs)


# ─────────────────────────────────────────────────────────────
# ── CATEGORIES PAGE
# ─────────────────────────────────────────────────────────────
@app.route("/categories")
def categories_page():
    conn = get_db()
    selected_cat = request.args.get("cat", "").strip()
    sort         = request.args.get("sort", "newest")
    min_p        = request.args.get("min", "")
    max_p        = request.args.get("max", "")
    rating_f     = request.args.get("rating", "")
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    per_page = 30

    # All categories with counts
    cat_rows = _exec(conn,
        "SELECT category, COUNT(*) as cnt FROM products WHERE approved=1 AND stock>0 GROUP BY category ORDER BY cnt DESC"
    ).fetchall()
    all_categories = []
    for row in cat_rows:
        name = row["category"] or "Other"
        # Find emoji — fuzzy match
        emoji = "🛍️"
        for k, v in CATEGORY_EMOJIS.items():
            if k.lower() in name.lower() or name.lower() in k.lower():
                emoji = v
                break
        all_categories.append({"name": name, "count": row["cnt"], "emoji": emoji})

    # Products query
    REV_AGG = ("LEFT JOIN (SELECT product_id, AVG(rating) avg_rating, COUNT(id) review_count "
               "FROM reviews GROUP BY product_id) agg ON agg.product_id=p.id ")
    sql = ("SELECT p.*, COALESCE(agg.avg_rating,0) avg_rating, COALESCE(agg.review_count,0) review_count "
           "FROM products p " + REV_AGG +
           "WHERE p.approved=1 AND p.stock>0 AND COALESCE((SELECT gst_suspended FROM users WHERE id=p.seller_id),0)=0 ")
    params = []
    if selected_cat:
        sql += "AND p.category=%s "; params.append(selected_cat)
    if min_p:
        try: sql += "AND p.price>=%s "; params.append(float(min_p))
        except: pass
    if max_p:
        try: sql += "AND p.price<=%s "; params.append(float(max_p))
        except: pass
    if rating_f:
        try:
            rv = float(rating_f)
            sql = f"SELECT * FROM ({sql}) _f WHERE avg_rating>=%s "
            params.append(rv)
        except: pass
    order_map = {
        "price_asc": "p.price ASC", "price_desc": "p.price DESC",
        "newest": "p.created_at DESC", "rating": "avg_rating DESC",
        "discount": "(p.mrp - p.price) DESC"
    }
    sql += f"ORDER BY {order_map.get(sort, 'p.created_at DESC')} "
    count_inner = sql.split("ORDER BY")[0]
    total       = _scalar(_exec(conn, f"SELECT COUNT(*) FROM ({count_inner}) _c", params)) or 0
    products    = _exec(conn, sql + f"LIMIT {per_page} OFFSET {(page-1)*per_page}", params).fetchall()
    total_pages = max(1, (total + per_page - 1) // per_page)
    return render_template("categories.html",
                           all_categories=all_categories, products=products,
                           selected_cat=selected_cat, sort=sort,
                           total=total, page=page, total_pages=total_pages)


# ─────────────────────────────────────────────────────────────
# ── PRODUCT PAGE
# ─────────────────────────────────────────────────────────────
@app.route("/p/<int:pid>")
def product_detail(pid):
    conn = get_db()
    p = _exec(conn,
        "SELECT p.*, u.name as seller_name, "
        "COALESCE(agg.avg_rating,0) avg_rating, COALESCE(agg.review_count,0) review_count "
        "FROM products p JOIN users u ON p.seller_id=u.id "
        "LEFT JOIN (SELECT product_id, AVG(rating) avg_rating, COUNT(id) review_count "
        "FROM reviews GROUP BY product_id) agg ON agg.product_id=p.id "
        "WHERE p.id=%s AND p.approved=1", (pid,)
    ).fetchone()
    if not p:
        abort(404)
    images   = _exec(conn, 
        "SELECT * FROM product_images WHERE product_id=%s ORDER BY sort_order", (pid,)).fetchall()
    variants = _exec(conn, 
        "SELECT * FROM product_variants WHERE product_id=%s", (pid,)).fetchall()
    reviews  = _exec(conn, 
        "SELECT r.*, u.name FROM reviews r JOIN users u ON r.user_id=u.id WHERE r.product_id=%s ORDER BY r.created_at DESC",
        (pid,)
    ).fetchall()
    similar  = _exec(conn, 
        "SELECT * FROM products WHERE category=%s AND id!=%s AND approved=1 AND stock>0 ORDER BY RANDOM() LIMIT 12",
        (p["category"], pid)
    ).fetchall()
    wished = False
    if session.get("role") == "buyer":
        wished = bool(_exec(conn, 
            "SELECT 1 FROM wishlist_items WHERE user_id=%s AND product_id=%s",
            (session["user_id"], pid)).fetchone())
    qa = _exec(conn, 
        "SELECT qa.*, u.name as asker_name, a.name as answerer_name "
        "FROM product_qa qa JOIN users u ON qa.user_id=u.id "
        "LEFT JOIN users a ON qa.answered_by=a.id "
        "WHERE qa.product_id=%s ORDER BY qa.created_at DESC",
        (pid,)
    ).fetchall()
    # Recently viewed — fetch last 6 viewed products (excluding current)
    viewed_ids = [v for v in session.get("recently_viewed", []) if v != pid][:6]
    recently_viewed = []
    if viewed_ids:
        placeholders = ",".join(["%s"] * len(viewed_ids))
        recently_viewed = _exec(conn, 
            f"SELECT * FROM products WHERE id IN ({placeholders}) AND approved=1",
            viewed_ids
        ).fetchall()
    # Rating breakdown
    rating_counts = {i: 0 for i in range(1, 6)}
    for r in reviews:
        rating_counts[r["rating"]] = rating_counts.get(r["rating"], 0) + 1
    # Seller vacation status
    seller_info = _exec(conn, "SELECT on_vacation FROM users WHERE id=%s", (p["seller_id"],)).fetchone()
    seller_on_vacation = bool(seller_info and seller_info["on_vacation"]) if seller_info else False
    return render_template("product.html", p=p, images=images, variants=variants,
                           reviews=reviews, similar=similar, wished=wished, qa=qa,
                           recently_viewed=recently_viewed, rating_counts=rating_counts,
                           seller_on_vacation=seller_on_vacation, now=datetime.now(timezone.utc))


@app.route("/review/<int:pid>", methods=["POST"])
@login_required
def add_review(pid):
    if session["role"] != "buyer":
        abort(403)
    check_csrf()
    rating  = int(request.form.get("rating") or 5)
    rating  = max(1, min(5, rating))  # clamp 1-5
    comment = request.form.get("comment", "").strip()
    conn = get_db()
    # Check buyer has purchased this product
    bought = _exec(conn, 
        "SELECT 1 FROM order_items oi JOIN orders o ON oi.order_id=o.id "
        "WHERE o.buyer_id=%s AND oi.product_id=%s AND o.status='delivered'",
        (session["user_id"], pid)
    ).fetchone()
    if not bought:
        flash("You can only review products you have bought and received.", "err")
        return redirect(f"/p/{pid}")
    # Redirect back to order detail if came from there
    redirect_to = request.form.get("redirect_to") or f"/p/{pid}"
    try:
        _exec(conn,
            "INSERT INTO reviews (product_id,user_id,rating,comment,body,buyer_name,updated_at) VALUES (%s,%s,%s,%s,%s,%s,NOW()) ON CONFLICT (product_id,user_id) DO UPDATE SET rating=EXCLUDED.rating, comment=EXCLUDED.comment, body=EXCLUDED.body, buyer_name=EXCLUDED.buyer_name, updated_at=NOW()",
            (pid, session["user_id"], rating, comment, comment, session.get("name",""))
        )
        conn.commit()
        # Upload up to 2 review photos
        review_row = _exec(conn, "SELECT id FROM reviews WHERE product_id=%s AND user_id=%s", (pid, session["user_id"])).fetchone()
        if review_row:
            rid = review_row["id"]
            _exec(conn, "DELETE FROM review_images WHERE review_id=%s", (rid,))
            uploaded = 0
            for field in ["photo1", "photo2"]:
                f = request.files.get(field)
                if f and f.filename and uploaded < 2:
                    url = upload_to_cloudinary(f)
                    if url:
                        _exec(conn, "INSERT INTO review_images (review_id,url,sort_order) VALUES (%s,%s,%s)", (rid, url, uploaded))
                        uploaded += 1
            conn.commit()
        flash("Review submitted! Shukriya 🙏", "ok")
    except Exception as e:
        logger.error(f"Review save failed for pid={pid}: {e}")
        conn.rollback()
        flash("Could not save review.", "err")
    return redirect(redirect_to)


# ─────────────────────────────────────────────────────────────
# ── CART
# ─────────────────────────────────────────────────────────────
@app.route("/cart")
@login_required
def cart():
    if session["role"] != "buyer":
        return redirect("/")
    items, summary = cart_summary(session["user_id"])
    return render_template("cart.html", items=items, summary=summary)


@app.route("/cart/add/<int:pid>", methods=["POST"])
@login_required
def add_to_cart(pid):
    if session["role"] != "buyer":
        abort(403)
    check_csrf()
    qty        = max(1, int(request.form.get("qty", 1)))
    size       = request.form.get("size", "").strip()
    color      = request.form.get("color", "").strip()
    variant_id = request.form.get("variant_id", "").strip()
    conn = get_db()
    p = _exec(conn, "SELECT * FROM products WHERE id=%s AND approved=1 AND stock>0", (pid,)).fetchone()
    if not p:
        flash("Product not available.", "err")
        return redirect(request.referrer or "/")

    # ── Vacation mode check ──
    seller = _exec(conn, "SELECT on_vacation FROM users WHERE id=%s", (p["seller_id"],)).fetchone()
    if seller and seller["on_vacation"]:
        flash("Yeh seller abhi vacation pe hai. Thodi der baad try karo.", "err")
        return redirect(request.referrer or f"/p/{pid}")

    # ── Variant support — use variant price/stock if selected ──
    actual_price = p["price"]
    available_stock = p["stock"]
    if variant_id:
        v = _exec(conn, "SELECT * FROM product_variants WHERE id=%s AND product_id=%s",
                         (variant_id, pid)).fetchone()
        if v:
            actual_price    = v["price"] if v["price"] else p["price"]
            available_stock = v["stock"]
            size  = size  or (v["size"]  or "")
            color = color or (v["color"] or "")
            if available_stock <= 0:
                flash("Yeh variant abhi stock mein nahi hai.", "err")
                return redirect(request.referrer or f"/p/{pid}")

    existing = _exec(conn, 
        "SELECT * FROM cart_items WHERE user_id=%s AND product_id=%s AND COALESCE(size,'')=%s AND COALESCE(color,'')=%s",
        (session["user_id"], pid, size, color)
    ).fetchone()
    add_qty = min(qty, available_stock)
    if existing:
        new_qty = min(existing["qty"] + add_qty, available_stock)
        _exec(conn, "UPDATE cart_items SET qty=%s WHERE id=%s", (new_qty, existing["id"]))
        conn.commit()
    else:
        _exec(conn, 
            "INSERT INTO cart_items (user_id,product_id,qty,size,color) VALUES (%s,%s,%s,%s,%s)",
            (session["user_id"], pid, add_qty, size or None, color or None)
        )
    conn.commit()
    flash("Added to cart!", "ok")
    return redirect(request.referrer or "/cart")


@app.route("/cart/update/<int:cid>", methods=["POST"])
@login_required
def update_cart(cid):
    check_csrf()
    qty  = max(1, int(request.form.get("qty", 1)))
    conn = get_db()
    item = _exec(conn, 
        "SELECT ci.*, p.stock FROM cart_items ci JOIN products p ON ci.product_id=p.id "
        "WHERE ci.id=%s AND ci.user_id=%s", (cid, session["user_id"])
    ).fetchone()
    if item:
        _exec(conn, "UPDATE cart_items SET qty=%s WHERE id=%s", (min(qty, item["stock"]), cid))
        conn.commit()
    return redirect("/cart")


@app.route("/cart/remove/<int:cid>")
@login_required
def remove_from_cart(cid):
    conn = get_db()
    _exec(conn, "DELETE FROM cart_items WHERE id=%s AND user_id=%s", (cid, session["user_id"]))
    conn.commit()
    return redirect("/cart")


@app.route("/cart/apply-coupon", methods=["POST"])
@login_required
def apply_coupon():
    check_csrf()
    code = request.form.get("code", "").strip().upper()
    conn = get_db()
    coupon = _exec(conn, 
        "SELECT * FROM coupons WHERE code=%s AND active=1 AND (expires_at IS NULL OR expires_at>NOW()) AND uses<max_uses",
        (code,)
    ).fetchone()
    if not coupon:
        flash("Invalid or expired coupon.", "err")
    else:
        session["coupon_code"]     = coupon["code"]
        session["coupon_type"]     = coupon["discount_type"]
        session["coupon_value"]    = coupon["discount_value"]
        session["coupon_min"]      = coupon["min_order"]
        flash(f"Coupon applied! {coupon['discount_type']} discount of {coupon['discount_value']}.", "ok")
    return redirect("/checkout")



@app.route("/cart/remove-coupon", methods=["POST"])
@login_required
def remove_coupon():
    check_csrf()
    session.pop("coupon_code", None)
    session.pop("coupon_type", None)
    session.pop("coupon_value", None)
    session.pop("coupon_min", None)
    flash("Coupon removed.", "ok")
    return redirect("/checkout")

# ─────────────────────────────────────────────────────────────
# ── CHECKOUT & ORDERS
# ─────────────────────────────────────────────────────────────
@app.route("/checkout", methods=["GET", "POST"])
@login_required
def checkout():
    if session["role"] != "buyer":
        return redirect("/")
    conn   = get_db()
    user   = _exec(conn, "SELECT * FROM users WHERE id=%s", (session["user_id"],)).fetchone()
    items, summary = cart_summary(session["user_id"])
    if not items:
        flash("Your cart is empty.", "err")
        return redirect("/cart")

    # Apply coupon if in session
    discount = 0
    coupon_code = session.get("coupon_code")
    if coupon_code and summary["subtotal"] >= session.get("coupon_min", 0):
        if session.get("coupon_type") == "percent":
            discount = round(summary["subtotal"] * session["coupon_value"] / 100, 2)
        else:
            discount = min(session["coupon_value"], summary["subtotal"])
    summary["discount"] = discount
    summary["total"]    = max(0, summary["total"] - discount)

    # Saved addresses
    try:
        saved_addresses = json.loads(user["saved_addresses"] or "[]")
    except (json.JSONDecodeError, TypeError):
        saved_addresses = []

    if request.method == "POST":
        check_csrf()
        buyer_name  = request.form.get("buyer_name", "").strip()
        phone       = request.form.get("phone", "").strip()
        address     = request.form.get("address", "").strip()
        pincode     = request.form.get("pincode", "").strip()
        pay_mode    = request.form.get("pay_mode", "COD")
        save_addr   = request.form.get("save_address")

        # Server-side validation
        if not buyer_name or not phone or not address:
            flash("Please fill all delivery details.", "err")
            return redirect("/checkout")
        if not phone.isdigit() or len(phone) != 10:
            flash("Valid 10-digit phone number daalo.", "err")
            return redirect("/checkout")

        # Save address if requested
        if save_addr:
            new_addr = {"name": buyer_name, "phone": phone, "address": address, "pincode": pincode}
            if new_addr not in saved_addresses:
                saved_addresses.append(new_addr)
                _exec(conn, "UPDATE users SET saved_addresses=%s WHERE id=%s",
                             (json.dumps(saved_addresses), session["user_id"]))
                conn.commit()

        public_id  = "ORD-" + uuid.uuid4().hex[:8].upper()
        invoice_no = generate_invoice_no()

        # WALLET payment — deduct from buyer wallet balance
        wallet_used = 0
        if pay_mode == "WALLET":
            wallet_bal = float(user["wallet_balance"] or 0)
            if wallet_bal < summary["total"]:
                flash(f"Wallet balance Rs.{wallet_bal:.2f} kum hai. Total Rs.{summary['total']:.2f} chahiye.", "err")
                return redirect("/checkout")
            wallet_used = summary["total"]
            _exec(conn, "UPDATE users SET wallet_balance=wallet_balance-%s WHERE id=%s",
                         (wallet_used, session["user_id"]))
            conn.commit()

        pay_status = "cod" if pay_mode == "COD" else ("paid" if pay_mode == "WALLET" else "pending")
        order_id = _scalar(_exec(conn, 
            """INSERT INTO orders (public_id,buyer_id,buyer_name,phone,address,pincode,
               pay_mode,payment_status,status,subtotal,gst_total,shipping,discount,total_amount,
               coupon_code,invoice_no,invoice_date)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
               RETURNING id""",
            (public_id, session["user_id"], buyer_name, phone, address, pincode,
             pay_mode, pay_status,
             "confirmed" if pay_mode == "WALLET" else "pending",
             summary["subtotal"], summary["gst_total"],
             summary["shipping"], discount, summary["total"],
             coupon_code, invoice_no)
        ))
        conn.commit()

        for it in items:
            seller_row = _exec(conn, "SELECT seller_id FROM products WHERE id=%s", (it["id"],)).fetchone()
            seller_id  = seller_row["seller_id"] if seller_row else None
            _exec(conn,
                """INSERT INTO order_items (order_id,product_id,seller_id,title,qty,price,gst_percent,line_total,size,color)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (order_id, it["id"], seller_id,
                 it["title"], it["qty"], it["price"], it["gst_percent"], it["line_total"],
                 it.get("size"), it.get("color"))
            )
            # Deduct stock + send low stock alert to seller
            _exec(conn, "UPDATE products SET stock=GREATEST(0,stock-%s) WHERE id=%s",
                         (it["qty"], it["id"]))
            conn.commit()
            new_stock = _exec(conn, "SELECT stock, seller_id, title FROM products WHERE id=%s",
                                     (it["id"],)).fetchone()
            if new_stock and new_stock["stock"] <= 5 and new_stock["stock"] > 0:
                add_notification(new_stock["seller_id"], "⚠️ Low Stock Alert",
                                 f"'{new_stock['title'][:40]}' ka stock sirf {new_stock['stock']} bacha hai!",
                                 f"/seller/products")
            elif new_stock and new_stock["stock"] == 0:
                add_notification(new_stock["seller_id"], "❌ Out of Stock",
                                 f"'{new_stock['title'][:40]}' out of stock ho gaya!",
                                 f"/seller/products")

        # Update coupon usage
        if coupon_code:
            _exec(conn, "UPDATE coupons SET uses=uses+1 WHERE code=%s", (coupon_code,))
            conn.commit()
            session.pop("coupon_code", None)
            session.pop("coupon_type", None)
            session.pop("coupon_value", None)
            session.pop("coupon_min", None)

        # Record seller transactions
        for it in items:
            seller_id = _exec(conn, "SELECT seller_id FROM products WHERE id=%s", (it["id"],)).fetchone()
            if seller_id:
                commission  = round(it["line_total"] * COMMISSION_PCT / 100, 2)
                net_amount  = round(it["line_total"] - commission, 2)
                item_row    = _exec(conn, 
                    "SELECT id FROM order_items WHERE order_id=%s AND product_id=%s",
                    (order_id, it["id"])
                ).fetchone()
                _exec(conn, 
                    """INSERT INTO seller_transactions (seller_id,order_id,order_item_id,type,amount,commission,net_amount,status)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (seller_id["seller_id"], order_id, item_row["id"] if item_row else None,
                     "sale", it["line_total"], commission, net_amount,
                     "pending")
                )
                add_notification(seller_id["seller_id"], "New Order", f"You have a new order #{public_id}", f"/seller/orders")

        conn.commit()

        # Clear cart
        _exec(conn, "DELETE FROM cart_items WHERE user_id=%s", (session["user_id"],))
        conn.commit()

        add_notification(session["user_id"], "Order Placed", f"Your order #{public_id} has been placed!", f"/orders")
        send_email(user["email"], f"Order Confirmed #{public_id}",
                   f"<p>Hi {buyer_name}, your order #{public_id} worth ₹{summary['total']:.2f} has been placed. We will notify you when it ships.</p>")

        if pay_mode == "RAZORPAY" and RAZORPAY_AVAILABLE and RZP_KEY_ID:
            client = razorpay.Client(auth=(RZP_KEY_ID, RZP_KEY_SECRET))
            rzp_order = client.order.create({
                "amount": int(summary["total"] * 100),
                "currency": "INR",
                "receipt": public_id,
            })
            _exec(conn, "UPDATE orders SET payment_id=%s WHERE id=%s",
                         (rzp_order["id"], order_id))
            conn.commit()
            return render_template("checkout.html", razorpay_order=rzp_order,
                                   order_db_id=order_id, public_id=public_id,
                                   items=items, summary=summary,
                                   saved_addresses=saved_addresses, user=user,
                                   razorpay_redirect=True)

        flash(f"Order placed successfully! Order ID: #{public_id}", "ok")
        return redirect(f"/orders")

    return render_template("checkout.html", items=items, summary=summary,
                           saved_addresses=saved_addresses, user=user)


@app.route("/payment/verify", methods=["POST"])
@login_required
def payment_verify():
    """Razorpay payment verification after redirect."""
    check_csrf()
    rzp_payment_id = request.form.get("razorpay_payment_id")
    rzp_order_id   = request.form.get("razorpay_order_id")
    rzp_signature  = request.form.get("razorpay_signature")
    order_db_id    = request.form.get("order_db_id")

    if RAZORPAY_AVAILABLE and RZP_KEY_ID:
        try:
            client = razorpay.Client(auth=(RZP_KEY_ID, RZP_KEY_SECRET))
            client.utility.verify_payment_signature({
                "razorpay_order_id":   rzp_order_id,
                "razorpay_payment_id": rzp_payment_id,
                "razorpay_signature":  rzp_signature,
            })
            conn = get_db()
            _exec(conn, 
                "UPDATE orders SET payment_status='paid', payment_id=%s, status='confirmed' WHERE id=%s AND buyer_id=%s",
                (rzp_payment_id, order_db_id, session["user_id"])
            )
            conn.commit()
            flash("Payment successful! Order confirmed.", "ok")
        except Exception:
            flash("Payment verification failed. Contact support.", "err")
    return redirect("/orders")


@app.route("/orders")
@login_required
def orders():
    if session["role"] != "buyer":
        return redirect("/")
    conn = get_db()
    orders_raw = _exec(conn, 
        "SELECT * FROM orders WHERE buyer_id=%s ORDER BY created_at DESC",
        (session["user_id"],)
    ).fetchall()
    result = []
    for o in orders_raw:
        order_items = _exec(conn, "SELECT oi.*, p.image_url FROM order_items oi LEFT JOIN products p ON oi.product_id = p.id WHERE oi.order_id=%s", (o["id"],)).fetchall()
        result.append(dict(o, order_items=order_items))
    return render_template("orders.html", orders=result)


@app.route("/orders/<int:oid>")
@login_required
def order_detail(oid):
    """Buyer order detail page with tracking."""
    if session["role"] != "buyer":
        return redirect("/")
    conn = get_db()
    o = _exec(conn, "SELECT * FROM orders WHERE id=%s AND buyer_id=%s",
                     (oid, session["user_id"])).fetchone()
    if not o:
        abort(404)
    items = _exec(conn, "SELECT * FROM order_items WHERE order_id=%s", (oid,)).fetchall()
    # Live tracking from Shiprocket if AWB exists
    tracking_info = None
    if o["awb"]:
        raw = shiprocket_track(o["awb"])
        if raw:
            td = raw.get("tracking_data", {})
            tracking_info = {
                "status":   td.get("shipment_status", o["status"]),
                "etd":      td.get("etd", ""),
                "history":  td.get("shipment_track_activities", []),
            }
    # Fetch existing reviews for each item in this order (for delivered orders)
    existing_reviews = {}
    review_images = {}
    if o["status"] == "delivered":
        pids = [it["product_id"] for it in items]
        for pid in pids:
            rev = _exec(conn, "SELECT * FROM reviews WHERE product_id=%s AND user_id=%s",
                        (pid, session["user_id"])).fetchone()
            if rev:
                existing_reviews[pid] = rev
                imgs = _exec(conn, "SELECT url FROM review_images WHERE review_id=%s ORDER BY sort_order",
                             (rev["id"],)).fetchall()
                review_images[pid] = [i["url"] for i in imgs]
    return render_template("order_detail.html", o=o, items=items, tracking_info=tracking_info,
                           existing_reviews=existing_reviews, review_images=review_images)


@app.route("/profile", methods=["GET", "POST"])
@login_required
def buyer_profile():
    """Buyer profile — edit name, phone, address."""
    conn = get_db()
    user = _exec(conn, "SELECT * FROM users WHERE id=%s", (session["user_id"],)).fetchone()
    if request.method == "POST":
        check_csrf()
        name    = request.form.get("name", "").strip()
        phone   = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        pincode = request.form.get("pincode", "").strip()
        if not name:
            flash("Naam required hai.", "err")
            return redirect("/profile")
        _exec(conn, "UPDATE users SET name=%s,phone=%s,address=%s,pincode=%s WHERE id=%s",
                     (name, phone, address, pincode, session["user_id"]))
        conn.commit()
        session["name"] = name
        flash("Profile update ho gaya!", "ok")
        return redirect("/profile")
    try:
        saved_addresses = json.loads(user["saved_addresses"] or "[]")
    except (json.JSONDecodeError, TypeError):
        saved_addresses = []
    return render_template("buyer_profile.html", user=user, saved_addresses=saved_addresses)


@app.route("/profile/address/remove/<int:idx>", methods=["POST"])
@login_required
def remove_saved_address(idx):
    check_csrf()
    conn = get_db()
    user = _exec(conn, "SELECT * FROM users WHERE id=%s", (session["user_id"],)).fetchone()
    try:
        addrs = json.loads(user["saved_addresses"] or "[]")
        if 0 <= idx < len(addrs):
            addrs.pop(idx)
            _exec(conn, "UPDATE users SET saved_addresses=%s WHERE id=%s",
                         (json.dumps(addrs), session["user_id"]))
            conn.commit()
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"remove_saved_address failed for user {session.get('user_id')}: {e}")
    return redirect("/profile")


@app.route("/track", methods=["GET", "POST"])
def guest_track():
    """Guest order tracking — no login needed."""
    result = None
    if request.method == "POST":
        order_id = request.form.get("order_id", "").strip().upper()
        phone    = request.form.get("phone", "").strip()
        if not order_id or not phone:
            flash("Order ID aur phone number dono zaroori hain.", "err")
        else:
            conn = get_db()
            o = _exec(conn, 
                "SELECT * FROM orders WHERE (public_id=%s OR public_id=%s) AND phone=%s",
                (order_id, "ORD-" + order_id.replace("ORD-",""), phone)
            ).fetchone()
            if o:
                items = _exec(conn, "SELECT * FROM order_items WHERE order_id=%s", (o["id"],)).fetchall()
                tracking_info = None
                if o["awb"]:
                    raw = shiprocket_track(o["awb"])
                    if raw:
                        td = raw.get("tracking_data", {})
                        tracking_info = {
                            "status":  td.get("shipment_status", o["status"]),
                            "etd":     td.get("etd", ""),
                            "history": td.get("shipment_track_activities", []),
                        }
                result = {"order": o, "items": items, "tracking": tracking_info}
            else:
                flash("Order nahi mila. Order ID aur phone check karo.", "err")
    return render_template("guest_track.html", result=result)


@app.route("/webhook/shiprocket", methods=["POST"])
def shiprocket_webhook():
    """Shiprocket calls this URL to update order status automatically."""
    try:
        data = request.get_json(force=True) or {}
        awb     = data.get("awb") or data.get("awb_code", "")
        status  = data.get("current_status", "")
        if not awb or not status:
            return jsonify({"ok": False}), 400
        conn = get_db()
        o = _exec(conn, "SELECT * FROM orders WHERE awb=%s", (awb,)).fetchone()
        if not o:
            return jsonify({"ok": False, "msg": "Order not found"}), 404
        # Map Shiprocket status to our status
        status_map = {
            "Delivered":        "delivered",
            "Out For Delivery":  "out_for_delivery",
            "In Transit":        "shipped",
            "Pickup Scheduled":  "ready_to_ship",
            "Picked Up":         "shipped",
            "RTO Initiated":     "rto",
            "RTO Delivered":     "rto_delivered",
        }
        new_status = status_map.get(status, o["status"])
        updates = {"status": new_status}
        if new_status == "shipped" and not o["shipped_at"]:
            updates["shipped_at"] = datetime.now().isoformat()
        if new_status == "delivered" and not o["delivered_at"]:
            updates["delivered_at"] = datetime.now().isoformat()
            # Mark seller earnings
            _exec(conn, "UPDATE seller_transactions SET status='earned' WHERE order_id=%s", (o["id"],))
            conn.commit()
            # Buyer cashback: 1% of order value
            cashback = round(float(o["total_amount"]) * 0.01, 2)
            if cashback > 0:
                _exec(conn, "UPDATE users SET wallet_balance=COALESCE(wallet_balance,0)+%s WHERE id=%s",
                             (cashback, o["buyer_id"]))
                conn.commit()
                add_notification(o["buyer_id"], "💰 Cashback Mila!",
                                 f"Order #{o['public_id']} deliver hua! Rs.{cashback:.2f} wallet mein add ho gaye.",
                                 "/profile")
            # Referral bonus: agar yeh buyer referred tha aur pehla order hai
            buyer = _exec(conn, "SELECT * FROM users WHERE id=%s", (o["buyer_id"],)).fetchone()
            if buyer and buyer["referred_by"]:
                prev_delivered = _exec(conn, 
                    "SELECT COUNT(*) FROM orders WHERE buyer_id=%s AND status='delivered' AND id!=%s",
                    (o["buyer_id"], o["id"])
                )
                if prev_delivered == 0:  # pehla delivered order
                    referrer = _exec(conn, "SELECT id FROM users WHERE referral_code=%s",
                                           (buyer["referred_by"],)).fetchone()
                    if referrer:
                        _exec(conn, "UPDATE users SET wallet_balance=COALESCE(wallet_balance,0)+100 WHERE id=%s",
                                     (referrer["id"],))
                        conn.commit()
                        add_notification(referrer["id"], "🎉 Referral Bonus!",
                                         f"Aapke referred buyer ne pehla order deliver karaya! Rs.100 wallet mein.",
                                         "/referral")
        _exec(conn, "UPDATE orders SET status=%s, updated_at=NOW() WHERE id=%s",
                     (new_status, o["id"]))
        conn.commit()
        if "shipped_at" in updates:
            _exec(conn, "UPDATE orders SET shipped_at=%s WHERE id=%s",
                         (updates["shipped_at"], o["id"]))
            conn.commit()
        if "delivered_at" in updates:
            _exec(conn, "UPDATE orders SET delivered_at=%s WHERE id=%s",
                         (updates["delivered_at"], o["id"]))
        conn.commit()
        # Notify buyer
        add_notification(o["buyer_id"], f"Order {new_status}",
                         f"Your order #{o['public_id']} is now: {status}", f"/orders/{o['id']}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/webhook/delhivery", methods=["POST"])
def delhivery_webhook():
    """
    Delhivery calls this URL to push status updates.
    Configure this in Delhivery dashboard → Settings → Webhooks.
    URL: https://yourdomain.com/webhook/delhivery
    """
    try:
        data = request.get_json(force=True) or {}
        # Delhivery sends an array of packages or a single package object
        packages = data.get("packages") or data.get("package") or []
        if isinstance(packages, dict):
            packages = [packages]
        if not packages:
            # Try flat format
            awb    = data.get("waybill", "")
            status = data.get("status", "")
            if awb and status:
                packages = [{"waybill": awb, "status": status}]

        conn = get_db()
        for pkg in packages:
            awb    = pkg.get("waybill") or pkg.get("awb", "")
            status = pkg.get("status", "")
            if not awb or not status:
                continue

            o = _exec(conn, "SELECT * FROM orders WHERE awb=%s", (awb,)).fetchone()
            if not o:
                continue

            # Map Delhivery status → our internal status
            status_map = {
                "Delivered":              "delivered",
                "Shipment Delivered":     "delivered",
                "Out for Delivery":       "out_for_delivery",
                "In Transit":             "shipped",
                "Dispatched":             "shipped",
                "Pickup Scheduled":       "ready_to_ship",
                "Picked Up":              "shipped",
                "RTO Initiated":          "rto",
                "RTO Delivered":          "rto_delivered",
                "Lost":                   "lost",
            }
            new_status = status_map.get(status, o["status"])

            _exec(conn, "UPDATE orders SET status=%s, updated_at=NOW() WHERE id=%s", (new_status, o["id"]))

            if new_status == "shipped" and not o.get("shipped_at"):
                _exec(conn, "UPDATE orders SET shipped_at=%s WHERE id=%s",
                      (datetime.now().isoformat(), o["id"]))

            if new_status == "delivered" and not o.get("delivered_at"):
                _exec(conn, "UPDATE orders SET delivered_at=%s WHERE id=%s",
                      (datetime.now().isoformat(), o["id"]))
                conn.commit()
                # Mark seller earnings
                _exec(conn, "UPDATE seller_transactions SET status='earned' WHERE order_id=%s", (o["id"],))
                conn.commit()
                # Buyer cashback 1%
                cashback = round(float(o["total_amount"]) * 0.01, 2)
                if cashback > 0:
                    _exec(conn, "UPDATE users SET wallet_balance=COALESCE(wallet_balance,0)+%s WHERE id=%s",
                          (cashback, o["buyer_id"]))
                    conn.commit()
                    add_notification(o["buyer_id"], "💰 Cashback Mila!",
                                     f"Order #{o['public_id']} deliver hua! Rs.{cashback:.2f} wallet mein add ho gaye.",
                                     "/profile")

            conn.commit()
            add_notification(o["buyer_id"], f"Order Update",
                             f"Aapka order #{o['public_id']} ka status: {status}", f"/orders/{o['id']}")

        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"[Delhivery Webhook] Error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/orders/cancel/<int:oid>", methods=["POST"])
@login_required
def cancel_order(oid):
    check_csrf()
    conn = get_db()
    o = _exec(conn, "SELECT * FROM orders WHERE id=%s AND buyer_id=%s",
                     (oid, session["user_id"])).fetchone()
    if not o:
        abort(404)
    if o["status"] in ("shipped", "out_for_delivery", "delivered", "cancelled"):
        flash("Cannot cancel this order.", "err")
        return redirect("/orders")
    reason = request.form.get("cancel_reason", "").strip() or "Cancelled as per your request."
    # Restore stock
    items = _exec(conn, "SELECT * FROM order_items WHERE order_id=%s", (oid,)).fetchall()
    for it in items:
        if it["product_id"]:
            _exec(conn, "UPDATE products SET stock=stock+%s WHERE id=%s",
                         (it["qty"], it["product_id"]))
    _exec(conn,
          "UPDATE orders SET status='cancelled', updated_at=NOW(), cancelled_at=NOW(), "
          "cancel_reason=%s, cancelled_by='buyer' WHERE id=%s",
          (reason, oid))
    conn.commit()
    flash("Order cancelled.", "ok")
    return redirect("/orders")


@app.route("/return/<int:item_id>", methods=["GET", "POST"])
@login_required
def return_request(item_id):
    conn = get_db()
    item = _exec(conn, 
        "SELECT oi.*, o.public_id, o.status, o.buyer_id FROM order_items oi "
        "JOIN orders o ON oi.order_id=o.id WHERE oi.id=%s", (item_id,)
    ).fetchone()
    if not item or item["buyer_id"] != session["user_id"]:
        abort(404)
    if request.method == "POST":
        check_csrf()
        reason  = request.form.get("reason", "")
        details = request.form.get("details", "")
        full    = reason + (f" — {details}" if details else "")
        _exec(conn, 
            "INSERT INTO return_requests (order_item_id,buyer_id,reason,status) VALUES (%s,%s,%s,'pending')",
            (item_id, session["user_id"], full)
        )
        conn.commit()
        flash("Return request submitted.", "ok")
        return redirect("/orders")
    return render_template("return_request.html", item=item)


@app.route("/orders/<int:oid>/invoice")
@login_required
def buyer_invoice(oid):
    conn = get_db()
    o = _exec(conn, "SELECT * FROM orders WHERE id=%s AND buyer_id=%s",
                     (oid, session["user_id"])).fetchone()
    if not o:
        abort(404)
    items = _exec(conn, "SELECT * FROM order_items WHERE order_id=%s", (oid,)).fetchall()
    # Get seller from first item
    seller_obj = None
    if items and items[0]["seller_id"]:
        seller_obj = _exec(conn, "SELECT * FROM users WHERE id=%s", (items[0]["seller_id"],)).fetchone()
    return render_template("invoice.html", o=o, items=items, seller=seller_obj,
                           company_name=COMPANY_NAME, company_gstin=COMPANY_GSTIN,
                           company_address=COMPANY_ADDRESS)


# ─────────────────────────────────────────────────────────────
# ── WISHLIST
# ─────────────────────────────────────────────────────────────
@app.route("/wishlist")
@login_required
def wishlist():
    conn  = get_db()
    items = _exec(conn, 
        "SELECT p.* FROM wishlist_items w JOIN products p ON w.product_id=p.id "
        "WHERE w.user_id=%s ORDER BY w.created_at DESC", (session["user_id"],)
    ).fetchall()
    return render_template("wishlist.html", items=items)


@app.route("/wishlist/toggle/<int:pid>", methods=["POST"])
@login_required
def wishlist_toggle(pid):
    check_csrf()
    conn = get_db()
    ex = _exec(conn, "SELECT id FROM wishlist_items WHERE user_id=%s AND product_id=%s",
                      (session["user_id"], pid)).fetchone()
    if ex:
        _exec(conn, "DELETE FROM wishlist_items WHERE id=%s", (ex["id"],))
    else:
        _exec(conn, "INSERT INTO wishlist_items (user_id,product_id) VALUES (%s,%s)",
                     (session["user_id"], pid))
    conn.commit()
    return redirect(request.referrer or "/wishlist")


# ─────────────────────────────────────────────────────────────
# ── NOTIFICATIONS
# ─────────────────────────────────────────────────────────────
@app.route("/notifications", methods=["GET", "POST"])
@login_required
def notifications():
    conn = get_db()
    if request.method == "POST":
        check_csrf()
        nid = request.form.get("nid")
        if nid:
            _exec(conn, "UPDATE notifications SET is_read=1 WHERE id=%s AND user_id=%s",
                         (nid, session["user_id"]))
            conn.commit()
        else:
            _exec(conn, "UPDATE notifications SET is_read=1 WHERE user_id=%s",
                         (session["user_id"],))
        conn.commit()
        return redirect("/notifications")
    notifs = _exec(conn, 
        "SELECT * FROM notifications WHERE user_id=%s ORDER BY created_at DESC LIMIT 50",
        (session["user_id"],)
    ).fetchall()
    return render_template("notifications.html", notifications=notifs)


# ─────────────────────────────────────────────────────────────
# ── SUPPORT TICKETS
# ─────────────────────────────────────────────────────────────
@app.route("/support")
@login_required
def support_list():
    conn   = get_db()
    tickets = _exec(conn, 
        "SELECT * FROM support_tickets WHERE user_id=%s ORDER BY updated_at DESC",
        (session["user_id"],)
    ).fetchall()
    return render_template("support_list.html", tickets=tickets)


@app.route("/support/new", methods=["GET", "POST"])
@login_required
def support_new():
    if request.method == "POST":
        check_csrf()
        subject = request.form.get("subject", "").strip()
        message = request.form.get("message", "").strip()
        if not subject or not message:
            flash("Please fill all fields.", "err")
            return redirect("/support/new")
        conn = get_db()
        _exec(conn, 
            "INSERT INTO support_tickets (user_id,subject,message) VALUES (%s,%s,%s)",
            (session["user_id"], subject, message)
        )
        conn.commit()
        flash("Ticket submitted. We will respond soon.", "ok")
        return redirect("/support")
    return render_template("support_new.html")


@app.route("/support/<int:tid>")
@login_required
def support_detail(tid):
    conn = get_db()
    ticket = _exec(conn, "SELECT * FROM support_tickets WHERE id=%s AND user_id=%s",
                          (tid, session["user_id"])).fetchone()
    if not ticket:
        abort(404)
    return render_template("support_detail.html", ticket=ticket, tk=ticket)


# ─────────────────────────────────────────────────────────────
# ── REFERRAL
# ─────────────────────────────────────────────────────────────
@app.route("/referral")
@login_required
def referral():
    conn = get_db()
    user = _exec(conn, "SELECT * FROM users WHERE id=%s", (session["user_id"],)).fetchone()
    referred_users = []
    if user["referral_code"]:
        referred_users = _exec(conn, 
            "SELECT name, created_at FROM users WHERE referred_by=%s",
            (user["referral_code"],)
        ).fetchall()
    stats = {
        "total_referrals": len(referred_users),
        "successful": len(referred_users),
        "earned": len(referred_users) * 50,
    }
    return render_template("referral.html", referral_code=user["referral_code"] or "N/A",
                           referred_users=referred_users, stats=stats)


# ─────────────────────────────────────────────────────────────
# ── SELLER ROUTES
# ─────────────────────────────────────────────────────────────
@app.route("/seller/dashboard")
@seller_required
def seller_dashboard():
    conn = get_db()
    sid  = session["user_id"]
    kpi  = {}
    kpi["today_orders"] = _scalar(_exec(conn, 
        "SELECT COUNT(DISTINCT oi.order_id) FROM order_items oi JOIN orders o ON oi.order_id=o.id "
        "WHERE oi.seller_id=%s AND o.created_at::date=CURRENT_DATE", (sid,)
    ))
    kpi["week_orders"] = _scalar(_exec(conn, 
        "SELECT COUNT(DISTINCT oi.order_id) FROM order_items oi JOIN orders o ON oi.order_id=o.id "
        "WHERE oi.seller_id=%s AND o.created_at >= NOW() - INTERVAL '7 days'", (sid,)
    ))
    top = _exec(conn, 
        "SELECT oi.title, SUM(oi.qty) total_qty FROM order_items oi "
        "WHERE oi.seller_id=%s GROUP BY oi.product_id, oi.title ORDER BY total_qty DESC LIMIT 1", (sid,)
    ).fetchone()
    kpi["top_product"] = top["title"] if top else None
    kpi["top_qty"]     = top["total_qty"] if top else 0
    low_stock = _exec(conn, 
        "SELECT * FROM products WHERE seller_id=%s AND stock<=5 AND approved=1 ORDER BY stock ASC LIMIT 10", (sid,)
    ).fetchall()
    return render_template("seller_dashboard.html", kpi=kpi, low_stock=low_stock,
                           seller=_exec(conn, "SELECT * FROM users WHERE id=%s", (sid,)).fetchone())


@app.route("/seller/wallet")
@seller_required
def seller_wallet():
    conn = get_db()
    sid  = session["user_id"]

    total_earned = _scalar(_exec(conn, 
        "SELECT COALESCE(SUM(net_amount),0) FROM seller_transactions WHERE seller_id=%s AND status IN ('earned','paid')", (sid,)
    ))
    total_paid = _scalar(_exec(conn, 
        "SELECT COALESCE(SUM(net_amount),0) FROM seller_transactions WHERE seller_id=%s AND status='paid'", (sid,)
    ))
    pending_payout = _scalar(_exec(conn, 
        "SELECT COALESCE(SUM(net_amount),0) FROM seller_transactions WHERE seller_id=%s AND status='earned'", (sid,)
    ))
    total_commission = _scalar(_exec(conn, 
        "SELECT COALESCE(SUM(commission),0) FROM seller_transactions WHERE seller_id=%s", (sid,)
    ))

    transactions = _exec(conn, 
        """SELECT st.*, o.public_id FROM seller_transactions st
           LEFT JOIN orders o ON st.order_id=o.id
           WHERE st.seller_id=%s ORDER BY st.created_at DESC LIMIT 50""",
        (sid,)
    ).fetchall()

    wallet_stats = {
        "total_earned":    round(total_earned, 2),
        "total_paid":      round(total_paid, 2),
        "pending_payout":  round(pending_payout, 2),
        "total_commission": round(total_commission, 2),
    }

    seller = _exec(conn, "SELECT * FROM users WHERE id=%s", (sid,)).fetchone()

    return render_template("seller_wallet.html",
                           wallet_stats=wallet_stats,
                           transactions=transactions,
                           seller=seller)


@app.route("/seller/orders")
@seller_required
def seller_orders():
    conn   = get_db()
    sid    = session["user_id"]
    status = request.args.get("status", "")
    sql    = """SELECT DISTINCT o.* FROM orders o
               JOIN order_items oi ON oi.order_id=o.id
               WHERE oi.seller_id=%s """
    params = [sid]
    if status:
        sql += "AND o.status=%s "
        params.append(status)
    sql += "ORDER BY o.created_at DESC"
    raw_orders = _exec(conn, sql, params).fetchall()

    # Attach item_images and item_titles per order
    orders = []
    for o in raw_orders:
        items = _exec(conn,
            "SELECT oi.title, COALESCE(p.image_url, '') img FROM order_items oi "
            "LEFT JOIN products p ON p.id=oi.product_id "
            "WHERE oi.order_id=%s AND oi.seller_id=%s", (o["id"], sid)
        ).fetchall()
        o_dict = dict(o)
        o_dict["item_titles"] = [i["title"] for i in items]
        o_dict["item_images"] = [i["img"] for i in items if i["img"]]
        orders.append(o_dict)

    # Count pending orders needing action
    pending_count = _scalar(_exec(conn, 
        "SELECT COUNT(DISTINCT o.id) FROM orders o JOIN order_items oi ON oi.order_id=o.id "
        "WHERE oi.seller_id=%s AND o.status IN ('placed','pending','confirmed')", (sid,)
    ))

    # Tab counts for Meesho-style tab bar
    all_counts = _exec(conn,
        "SELECT o.status, COUNT(DISTINCT o.id) as cnt FROM orders o "
        "JOIN order_items oi ON oi.order_id=o.id WHERE oi.seller_id=%s GROUP BY o.status", (sid,)
    ).fetchall()
    tab_counts = {'all': 0}
    for row in all_counts:
        s = row['status']
        c = row['cnt']
        tab_counts['all'] += c
        if s in ('placed', 'pending', 'confirmed'):
            tab_counts['pending'] = tab_counts.get('pending', 0) + c
        elif s in ('delivered', 'Delivered'):
            tab_counts['delivered'] = tab_counts.get('delivered', 0) + c
        else:
            tab_counts[s] = tab_counts.get(s, 0) + c

    return render_template("seller_orders.html", orders=orders,
                           status=status, pending_count=pending_count,
                           tab_counts=tab_counts)


@app.route("/seller/orders/<int:oid>")
@seller_required
def seller_order_detail(oid):
    conn = get_db()
    o    = _exec(conn, "SELECT * FROM orders WHERE id=%s", (oid,)).fetchone()
    if not o:
        abort(404)
    items = _exec(conn, 
        "SELECT * FROM order_items WHERE order_id=%s AND seller_id=%s",
        (oid, session["user_id"])
    ).fetchall()
    if not items:
        abort(403)
    buyer = _exec(conn, "SELECT name, email, phone FROM users WHERE id=%s", (o["buyer_id"],)).fetchone()
    return render_template("seller_order_detail.html", o=o, items=items, buyer=buyer)


@app.route("/seller/orders/<int:oid>/accept", methods=["POST"])
@seller_required
def seller_order_accept(oid):
    check_csrf()
    conn = get_db()
    # Verify this seller has items in this order
    items = _exec(conn, 
        "SELECT * FROM order_items WHERE order_id=%s AND seller_id=%s",
        (oid, session["user_id"])
    ).fetchall()
    if not items:
        abort(403)
    o = _exec(conn, "SELECT * FROM orders WHERE id=%s", (oid,)).fetchone()
    if o["status"] not in ("pending", "confirmed"):
        flash("Yeh order accept nahi kiya ja sakta.", "err")
        return redirect("/seller/orders")
    _exec(conn, "UPDATE orders SET status='accepted', updated_at=NOW() WHERE id=%s", (oid,))
    conn.commit()
    add_notification(o["buyer_id"], "Order Accepted",
                     f"Your order #{o['public_id']} has been accepted by the seller!", f"/orders")
    flash(f"Order #{o['public_id']} accept kar liya!", "ok")
    return redirect(f"/seller/orders/{oid}")


@app.route("/seller/orders/<int:oid>/reject", methods=["POST"])
@seller_required
def seller_order_reject(oid):
    check_csrf()
    conn   = get_db()
    items  = _exec(conn, 
        "SELECT * FROM order_items WHERE order_id=%s AND seller_id=%s",
        (oid, session["user_id"])
    ).fetchall()
    if not items:
        abort(403)
    o = _exec(conn, "SELECT * FROM orders WHERE id=%s", (oid,)).fetchone()
    if o["status"] not in ("pending", "confirmed"):
        flash("Yeh order reject nahi kiya ja sakta.", "err")
        return redirect("/seller/orders")
    reason = request.form.get("reason", "Seller ne order reject kiya.")
    # Restore stock
    for it in items:
        if it["product_id"]:
            _exec(conn, "UPDATE products SET stock=stock+%s WHERE id=%s",
                         (it["qty"], it["product_id"]))
    _exec(conn,
          "UPDATE orders SET status='cancelled', notes=%s, updated_at=NOW(), cancelled_at=NOW(), "
          "cancel_reason=%s, cancelled_by='seller' WHERE id=%s",
          (reason, reason, oid))
    conn.commit()
    add_notification(o["buyer_id"], "Order Cancelled",
                     f"Your order #{o['public_id']} has been cancelled. Reason: {reason}", f"/orders")
    flash(f"Order #{o['public_id']} reject kar diya.", "ok")
    return redirect("/seller/orders")


@app.route("/seller/orders/<int:oid>/ready", methods=["POST"])
@seller_required
def seller_order_ready(oid):
    check_csrf()
    conn  = get_db()
    items = _exec(conn, 
        "SELECT * FROM order_items WHERE order_id=%s AND seller_id=%s",
        (oid, session["user_id"])
    ).fetchall()
    if not items:
        abort(403)
    o = _exec(conn, "SELECT * FROM orders WHERE id=%s", (oid,)).fetchone()
    if o["status"] != "accepted":
        flash("Pehle order accept karo.", "err")
        return redirect(f"/seller/orders/{oid}")

    seller = _exec(conn, "SELECT * FROM users WHERE id=%s", (session["user_id"],)).fetchone()

    # ── Delivery Partner: Delhivery B2C or Shiprocket (set DELIVERY_PARTNER in .env) ──
    if DELIVERY_PARTNER == "delhivery":
        courier_result = delhivery_create_shipment(o, items, seller)
        partner_label  = "Delhivery"
        tracking_base  = "https://www.delhivery.com/track/package/"
    else:
        courier_result = shiprocket_create_order(o, items, seller)
        partner_label  = "Shiprocket"
        tracking_base  = "https://shiprocket.co/tracking/"

    if courier_result["success"]:
        awb          = courier_result.get("awb", "")
        courier_name = courier_result.get("courier_name", "")
        shipment_id  = courier_result.get("shipment_id", "")   # Shiprocket only
        tracking_url = f"{tracking_base}{awb}" if awb else ""

        notes_val = (
            f"Delhivery AWB: {awb}" if DELIVERY_PARTNER == "delhivery"
            else f"Shiprocket shipment_id: {shipment_id}"
        )

        _exec(conn,
            "UPDATE orders SET status='ReadyToShip', awb=%s, courier_name=%s, tracking_url=%s, notes=%s, updated_at=NOW() WHERE id=%s",
            (awb, courier_name, tracking_url, notes_val, oid)
        )
        conn.commit()

        pickup_msg = "Pickup bhi schedule ho gaya!" if courier_result.get("pickup_scheduled") else "Pickup request bheji ja rahi hai."
        flash(
            f"Order #{o['public_id']} ReadyToShip via {partner_label}! "
            f"Courier: {courier_name or 'Auto-assigned'}. AWB: {awb}. {pickup_msg}",
            "ok"
        )
        add_notification(o["buyer_id"], "Order Packed & Dispatching",
                         f"Your order #{o['public_id']} is packed! Courier: {courier_name}. AWB: {awb}",
                         "/orders")
    else:
        # Courier API failed — still mark ReadyToShip locally so seller can ship manually
        _exec(conn, "UPDATE orders SET status='ReadyToShip', updated_at=NOW() WHERE id=%s", (oid,))
        conn.commit()
        flash(
            f"Order ReadyToShip mark kar diya. "
            f"{partner_label} pickup auto-schedule nahi hua: {courier_result.get('error', 'Unknown error')}. "
            f"Manually courier book karo ya .env mein credentials check karo.",
            "err"
        )
        add_notification(o["buyer_id"], "Order Packed",
                         f"Your order #{o['public_id']} is packed and ready to ship!", "/orders")

    return redirect(f"/seller/orders/{oid}")


@app.route("/seller/orders/<int:oid>/label")
@seller_required
def seller_label(oid):
    conn  = get_db()
    o     = _exec(conn, "SELECT * FROM orders WHERE id=%s", (oid,)).fetchone()
    if not o:
        abort(404)
    items = _exec(conn, 
        "SELECT * FROM order_items WHERE order_id=%s AND seller_id=%s",
        (oid, session["user_id"])
    ).fetchall()
    if not items:
        abort(403)
    seller_obj = _exec(conn, "SELECT * FROM users WHERE id=%s", (session["user_id"],)).fetchone()
    qr_b64 = None
    try:
        import qrcode, base64, io as _io
        qr  = qrcode.make(o["public_id"])
        buf = _io.BytesIO()
        qr.save(buf, format="PNG")
        qr_b64 = base64.b64encode(buf.getvalue()).decode()
    except ImportError:
        pass
    return render_template("label.html", o=o, items=items, seller=seller_obj,
                           company_name=COMPANY_NAME, qr_b64=qr_b64, bar_b64=None)


@app.route("/seller/orders/<int:oid>/track")
@seller_required
def seller_order_track(oid):
    """Live tracking from Shiprocket for a given order."""
    conn = get_db()
    o    = _exec(conn, "SELECT * FROM orders WHERE id=%s", (oid,)).fetchone()
    if not o:
        abort(404)
    # Verify seller owns this order
    if not _exec(conn, "SELECT 1 FROM order_items WHERE order_id=%s AND seller_id=%s",
                        (oid, session["user_id"])).fetchone():
        abort(403)
    tracking_data = None
    if o["awb"]:
        tracking_data = shiprocket_track(o["awb"])
    return jsonify({
        "awb":          o["awb"],
        "courier":      o["courier_name"],
        "status":       o["status"],
        "tracking_url": o["tracking_url"],
        "shiprocket":   tracking_data,
    })


@app.route("/seller/products")
@seller_required
def seller_products():
    conn = get_db()
    q    = request.args.get("q", "")
    sql  = "SELECT * FROM products WHERE seller_id=%s "
    params = [session["user_id"]]
    if q:
        sql += "AND title LIKE %s "; params.append(f"%{q}%")
    sql += "ORDER BY created_at DESC"
    products = _exec(conn, sql, params).fetchall()
    return render_template("seller_products.html", products=products, q=q)



@app.route("/api/ai-product-generate", methods=["POST"])
@seller_required
def ai_product_generate():
    """Proxy Anthropic API call server-side so API key stays secret."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set in .env file"}), 503

    data = request.get_json(force=True) or {}
    idea = str(data.get("idea", "")).strip()[:200]
    if not idea:
        return jsonify({"error": "Product idea missing"}), 400

    prompt = f'''You are a Meesho-style Indian marketplace product listing expert.
Generate a complete product listing for Indian budget shoppers for: "{idea}"

Reply ONLY with a valid JSON object (no markdown, no explanation):
{{
  "title": "SEO optimized product title in Hindi-English mix (max 80 chars)",
  "category": "Main category (e.g. Kurta, T-Shirt, Shoes)",
  "brand": "Brand name or empty string",
  "generic_name": "Generic product name",
  "material": "Material composition",
  "pattern": "Pattern type",
  "occasion": "Usage occasion",
  "description": "5-6 line attractive description for Indian buyers. Max 400 chars.",
  "size_options": "Comma separated sizes e.g. S,M,L,XL,XXL",
  "color_options": "Comma separated 3-4 popular colors",
  "mrp": 999,
  "price": 599,
  "stock": 50,
  "gst_percent": 5,
  "hsn": "HSN code number only",
  "weight_grams": 300,
  "catalog_name": "Collection name",
  "variant_lines": "S,Black,599,10\\nM,Black,599,15\\nL,Black,599,10"
}}'''

    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": prompt}]
    }).encode()

    req = _urllib_req.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type":      "application/json",
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01"
        }
    )
    try:
        with _urllib_req.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
        text  = "".join(b.get("text", "") for b in result.get("content", []))
        clean = text.replace("```json", "").replace("```", "").strip()
        product_data = json.loads(clean)
        return jsonify({"ok": True, "data": product_data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/seller/products/new", methods=["GET", "POST"])
@app.route("/seller/products/edit/<int:pid>", methods=["GET", "POST"])
@seller_required
def seller_product_form(pid=None):
    conn = get_db()
    p    = None
    if pid:
        p = _exec(conn, "SELECT * FROM products WHERE id=%s AND seller_id=%s",
                         (pid, session["user_id"])).fetchone()
        if not p:
            abort(404)

    if request.method == "POST":
        check_csrf()
        title    = request.form.get("title", "").strip()
        category = request.form.get("category", "General").strip()
        desc     = request.form.get("description", "").strip()
        brand    = request.form.get("brand", "").strip()
        price    = float(request.form.get("price", 0))
        mrp      = request.form.get("mrp") or None
        stock    = int(request.form.get("stock", 0))
        gst      = float(request.form.get("gst_percent", 18))
        hsn      = request.form.get("hsn", "").strip()
        weight   = request.form.get("weight_grams") or None
        sizes    = request.form.get("size_options", "").strip()
        colors   = request.form.get("color_options", "").strip()
        cat_name = request.form.get("catalog_name", "").strip()
        style    = request.form.get("style_code", "").strip()
        # New Meesho-style fields
        generic_name        = request.form.get("generic_name", "").strip()
        material            = request.form.get("material", "").strip()
        pattern             = request.form.get("pattern", "").strip()
        occasion            = request.form.get("occasion", "").strip()
        country_of_origin   = request.form.get("country_of_origin", "India").strip()
        net_quantity        = request.form.get("net_quantity", "").strip()
        dimension_unit      = request.form.get("dimension_unit", "cm").strip()
        product_length      = request.form.get("product_length") or None
        product_width       = request.form.get("product_width") or None
        manufacturer_name   = request.form.get("manufacturer_name", "").strip()
        manufacturer_address= request.form.get("manufacturer_address", "").strip()
        manufacturer_pincode= request.form.get("manufacturer_pincode", "").strip()
        packer_name         = request.form.get("packer_name", "").strip()
        packer_address      = request.form.get("packer_address", "").strip()
        packer_pincode      = request.form.get("packer_pincode", "").strip()
        importer_name       = request.form.get("importer_name", "").strip()
        importer_address    = request.form.get("importer_address", "").strip()
        importer_pincode    = request.form.get("importer_pincode", "").strip()
        tags                = request.form.get("tags", "").strip()
        closure             = request.form.get("closure", "").strip()
        fold_type           = request.form.get("fold_type", "").strip()
        product_height      = request.form.get("product_height") or None
        product_type        = request.form.get("product_type", "").strip()
        compartments        = request.form.get("compartments", "").strip()

        if not title or price <= 0:
            flash("Title and valid price are required.", "err")
            return redirect(request.path)

        # ── GST Active Check before product upload ────────────
        seller_gst = _exec(conn, "SELECT gstin, gstin_verified, gst_suspended FROM users WHERE id=%s",
                           (session["user_id"],)).fetchone()
        if seller_gst and seller_gst["gstin"] and seller_gst["gstin_verified"]:
            if seller_gst.get("gst_suspended"):
                flash("❌ Aapka GST suspended/cancelled hai. Products upload karne ke liye pehle GST active karo.", "err")
                return redirect("/seller/products")
            # Real-time GST status check
            is_active, _, gst_sts = check_gst_status(seller_gst["gstin"])
            if is_active is False:
                # GST suspended — hide existing products too
                sync_gst_status(conn, session["user_id"])
                flash(f"❌ Aapka GST {gst_sts} hai. Active GST ke bina product upload nahi ho sakta.", "err")
                return redirect("/seller/products")

        # Handle image upload — primary + multiple extra images
        image_url = p["image_url"] if p else None
        img_file  = request.files.get("image")
        saved_image = None
        if img_file and img_file.filename:
            url, thumb = save_image(img_file)
            if url:
                image_url = url
                saved_image = (url, thumb)

        # Extra images (getlist handles multiple file inputs)
        extra_files = request.files.getlist("images")
        extra_saved = []
        for ef in extra_files:
            if ef and ef.filename:
                eurl, ethumb = save_image(ef)
                if eurl:
                    extra_saved.append((eurl, ethumb))

        size_chart_data = request.form.get("size_chart_data", "").strip() or None

        extra_vals = (generic_name, material, pattern, occasion, country_of_origin,
                      net_quantity, dimension_unit, product_length, product_width,
                      manufacturer_name, manufacturer_address, manufacturer_pincode,
                      packer_name, packer_address, packer_pincode,
                      importer_name, importer_address, importer_pincode, tags,
                      closure, fold_type, product_height, product_type, compartments,
                      size_chart_data)

        if pid:
            # Update existing product — rebuild image gallery
            if saved_image or extra_saved:
                _exec(conn, "DELETE FROM product_images WHERE product_id=%s", (pid,))
                conn.commit()
                if saved_image:
                    _exec(conn, 
                        "INSERT INTO product_images (product_id,url,thumb_url,sort_order) VALUES (%s,%s,%s,0)",
                        (pid, saved_image[0], saved_image[1])
                    )
                for i, (eu, et) in enumerate(extra_saved):
                    _exec(conn, 
                        "INSERT INTO product_images (product_id,url,thumb_url,sort_order) VALUES (%s,%s,%s,%s)",
                        (pid, eu, et, i + 1)
                    )
            _exec(conn, 
                """UPDATE products SET title=%s,category=%s,description=%s,brand=%s,price=%s,mrp=%s,
                   stock=%s,gst_percent=%s,hsn=%s,weight_grams=%s,size_options=%s,color_options=%s,
                   catalog_name=%s,style_code=%s,image_url=%s,approved=0,
                   generic_name=%s,material=%s,pattern=%s,occasion=%s,country_of_origin=%s,
                   net_quantity=%s,dimension_unit=%s,product_length=%s,product_width=%s,
                   manufacturer_name=%s,manufacturer_address=%s,manufacturer_pincode=%s,
                   packer_name=%s,packer_address=%s,packer_pincode=%s,
                   importer_name=%s,importer_address=%s,importer_pincode=%s,tags=%s,
                   closure=%s,fold_type=%s,product_height=%s,product_type=%s,compartments=%s,size_chart_data=%s
                   WHERE id=%s""",
                (title, category, desc, brand, price, mrp, stock, gst, hsn,
                 weight, sizes, colors, cat_name, style, image_url,
                 *extra_vals, pid)
            )
            conn.commit()
            flash("Product updated. Pending re-approval.", "ok")
        else:
            # Insert new product first, then insert image with real product_id
            _exec(conn, 
                """INSERT INTO products (seller_id,title,category,description,brand,price,mrp,
                   stock,gst_percent,hsn,weight_grams,size_options,color_options,catalog_name,
                   style_code,image_url,approved,
                   generic_name,material,pattern,occasion,country_of_origin,
                   net_quantity,dimension_unit,product_length,product_width,
                   manufacturer_name,manufacturer_address,manufacturer_pincode,
                   packer_name,packer_address,packer_pincode,
                   importer_name,importer_address,importer_pincode,tags,
                   closure,fold_type,product_height,product_type,compartments,size_chart_data)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (session["user_id"], title, category, desc, brand, price, mrp, stock, gst, hsn,
                 weight, sizes, colors, cat_name, style, image_url, *extra_vals)
            )
            new_pid = _scalar(_exec(conn, "SELECT lastval()"))
            if saved_image:
                _exec(conn,
        "INSERT INTO product_images (product_id,url,thumb_url,sort_order) VALUES (%s,%s,%s,0)",
                    (new_pid, saved_image[0], saved_image[1])
                )
            for i, (eu, et) in enumerate(extra_saved):
                _exec(conn, 
                    "INSERT INTO product_images (product_id,url,thumb_url,sort_order) VALUES (%s,%s,%s,%s)",
                    (new_pid, eu, et, i + 1)
                )
            conn.commit()
            flash("Product submitted for approval.", "ok")
        return redirect("/seller/products")

    variants = _exec(conn, "SELECT * FROM product_variants WHERE product_id=%s", (pid,)).fetchall() if pid else []
    return render_template("seller_product_form.html", p=p, variants=variants)


@app.route("/seller/products/delete/<int:pid>", methods=["POST"])
@seller_required
def seller_product_delete(pid):
    check_csrf()
    conn = get_db()
    p = _exec(conn, "SELECT * FROM products WHERE id=%s AND seller_id=%s",
                     (pid, session["user_id"])).fetchone()
    if not p:
        abort(404)
    _exec(conn, "DELETE FROM products WHERE id=%s", (pid,))
    _exec(conn, "DELETE FROM product_images WHERE product_id=%s", (pid,))
    conn.commit()
    flash("Product deleted.", "ok")
    return redirect("/seller/products")


@app.route("/seller/profile", methods=["GET", "POST"])
@seller_required
def seller_profile():
    conn = get_db()
    seller = _exec(conn, "SELECT * FROM users WHERE id=%s", (session["user_id"],)).fetchone()
    if request.method == "POST":
        check_csrf()
        phone       = request.form.get("phone", "").strip()
        address     = request.form.get("address", "").strip()
        gstin       = request.form.get("gstin", "").strip()
        pan         = request.form.get("pan", "").strip()
        bank_name   = request.form.get("bank_name", "").strip()
        bank        = request.form.get("bank_bank", "").strip()
        bank_acct   = request.form.get("bank_account", "").strip()
        bank_ifsc   = request.form.get("bank_ifsc", "").strip()
        upi_id      = request.form.get("upi_id", "").strip()
        state       = request.form.get("state", "").strip()
        _exec(conn, 
            "UPDATE users SET phone=%s,address=%s,gstin=%s,pan=%s,bank_name=%s,bank=%s,bank_bank=%s,bank_account=%s,bank_ifsc=%s,upi_id=%s,state=%s WHERE id=%s",
            (phone, address, gstin, pan, bank_name, bank, bank, bank_acct, bank_ifsc, upi_id, state, session["user_id"])
        )
        conn.commit()
        flash("Profile updated.", "ok")
        return redirect("/seller/profile")
    # Compute KYC step completion
    kyc_pan   = bool(seller["pan"] and seller["pan_verified"])
    kyc_gstin = bool(seller["gstin"] and seller["gstin_verified"])
    kyc_bank  = bool((seller["bank_account"] or seller["upi_id"]) and seller["bank_verified"])
    kyc_pct   = int(sum([bool(seller["phone"]), bool(seller["gstin"]),
                         bool(seller["bank_account"]), bool(seller["address"])]) / 4 * 100)
    return render_template("seller_profile.html", seller=seller, kyc_pct=kyc_pct,
                           kyc_pan=kyc_pan, kyc_gstin=kyc_gstin, kyc_bank=kyc_bank)


# ── KYC Main Page (step-by-step wizard) ────────────────────────
@app.route("/seller/kyc")
@seller_required
def seller_kyc():
    conn = get_db()
    seller = _exec(conn, "SELECT * FROM users WHERE id=%s", (session["user_id"],)).fetchone()
    pan_done  = bool(seller["pan_verified"])
    gst_done  = bool(seller["gstin_verified"])
    bank_done = bool(seller["bank_verified"])
    if pan_done and gst_done and bank_done:
        return redirect("/seller/dashboard")
    # Determine current step: 1=PAN, 2=GST, 3=Bank
    if not pan_done:
        current_step = 1
    elif not gst_done:
        current_step = 2
    else:
        current_step = 3
    return render_template("seller_kyc.html", seller=seller,
                           pan_done=pan_done, gst_done=gst_done, bank_done=bank_done,
                           current_step=current_step)


# ── GST Status Check (Free — GST Portal) ─────────────────────
def check_gst_status(gstin):
    """Returns (is_active, company_name, status_str)"""
    import requests as _req
    try:
        resp = _req.get(
            f"https://sheet.gstincheck.co.in/check/d4da11cf4dfac9e1a0a4b6a10fdaca1f/{gstin}",
            timeout=8
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("flag") is True:
            sts = data.get("data", {}).get("sts", "UNKNOWN")
            name = (data.get("data", {}).get("tradeNam")
                   or data.get("data", {}).get("lgnm") or "")
            is_active = sts.upper() == "ACTIVE"
            return is_active, name, sts
    except Exception as e:
        app.logger.warning(f"GST status check error: {e}")
    return None, "", "UNKNOWN"  # None = portal unreachable


# ── GST Status Sync (call periodically or on product upload) ──
def sync_gst_status(conn, seller_id):
    """Check seller GST status and hide/show products accordingly."""
    seller = _exec(conn, "SELECT gstin, gstin_verified FROM users WHERE id=%s", (seller_id,)).fetchone()
    if not seller or not seller["gstin"] or not seller["gstin_verified"]:
        return
    is_active, name, sts = check_gst_status(seller["gstin"])
    if is_active is None:
        return  # portal unreachable, don't change anything
    if is_active:
        # GST active — unsuspend seller and show products
        _exec(conn, "UPDATE users SET gst_suspended=0 WHERE id=%s", (seller_id,))
        _exec(conn, "UPDATE products SET approved=1 WHERE seller_id=%s AND approved=-1", (seller_id,))
    else:
        # GST suspended/cancelled — suspend seller and hide ALL products
        _exec(conn, "UPDATE users SET gst_suspended=1 WHERE id=%s", (seller_id,))
        _exec(conn, "UPDATE products SET approved=-1 WHERE seller_id=%s AND approved=1", (seller_id,))
        app.logger.warning(f"Seller {seller_id} GST suspended ({sts}), products hidden.")
    conn.commit()


# ── Sandbox.co.in: Get JWT token ──────────────────────────────
def sandbox_get_token():
    """Authenticate with Sandbox and return JWT bearer token."""
    import requests as _req
    try:
        resp = _req.post(
            "https://api.sandbox.co.in/authenticate",
            headers={
                "x-api-key": SANDBOX_API_KEY,
                "x-api-secret": SANDBOX_API_SECRET,
                "x-api-version": "1.0",
                "Content-Type": "application/json"
            },
            timeout=10
        )
        data = resp.json()
        return data.get("access_token") or data.get("data", {}).get("access_token")
    except Exception as e:
        app.logger.error(f"Sandbox auth error: {e}")
        return None


# ── Admin: Manual GST Sync All Sellers ────────────────────────
@app.route("/admin/gst-sync", methods=["POST"])
def admin_gst_sync():
    if not session.get("role") == "admin":
        abort(403)
    conn = get_db()
    sellers = _exec(conn, "SELECT id FROM users WHERE role='seller' AND gstin_verified=1 AND gstin IS NOT NULL").fetchall()
    synced = 0
    for s in sellers:
        sync_gst_status(conn, s["id"])
        synced += 1
    flash(f"✅ {synced} sellers ka GST status sync ho gaya.", "ok")
    return redirect("/admin/sellers")


# ── KYC STEP 1: Aadhaar OTP Generate ──────────────────────────
@app.route("/seller/kyc/aadhaar/send-otp", methods=["POST"])
@seller_required
def kyc_aadhaar_send_otp():
    check_csrf()
    import re, requests as _req
    aadhaar = request.form.get("aadhaar", "").strip().replace(" ", "")
    if not re.match(r"^\d{12}$", aadhaar):
        flash("❌ Aadhaar number 12 digits ka hona chahiye.", "err")
        return redirect("/seller/kyc")

    if not SANDBOX_API_KEY:
        flash("❌ KYC service configure nahi hai. Admin se contact karo.", "err")
        return redirect("/seller/kyc")

    try:
        _token = sandbox_get_token()
        if not _token:
            flash("❌ KYC service se connect nahi hua. Dobara try karo.", "err")
            return redirect("/seller/kyc")
        resp = _req.post(
            "https://api.sandbox.co.in/kyc/aadhaar/okyc/otp",
            json={
                "@entity": "in.co.sandbox.kyc.aadhaar.okyc.otp.request",
                "aadhaar_number": aadhaar,
                "consent": "y"
            },
            headers={
                "x-api-key": SANDBOX_API_KEY,
                "Authorization": f"Bearer {_token}",
                "x-api-version": "1.0",
                "Content-Type": "application/json"
            },
            timeout=10
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("code") == 200:
            ref_id = str(data["data"]["reference_id"])
            # Store ref_id in session for OTP verify step
            session["aadhaar_ref_id"] = ref_id
            session["aadhaar_otp_sent"] = True
            conn = get_db()
            _exec(conn, "UPDATE users SET aadhaar_ref_id=%s WHERE id=%s", (ref_id, session["user_id"]))
            conn.commit()
            flash("✅ OTP aapke Aadhaar registered mobile pe bhej diya gaya!", "ok")
        else:
            err = data.get("message") or data.get("error") or "OTP send karne mein error aayi."
            flash(f"❌ {err}", "err")
    except _req.exceptions.Timeout:
        flash("❌ Request timeout ho gaya. Dobara try karo.", "err")
    except Exception as e:
        app.logger.error(f"Aadhaar OTP send error: {e}")
        flash("❌ Aadhaar service abhi unavailable hai. Thodi der baad try karo.", "err")
    return redirect("/seller/kyc")


# ── KYC STEP 1b: Aadhaar OTP Verify ───────────────────────────
@app.route("/seller/kyc/aadhaar/verify-otp", methods=["POST"])
@seller_required
def kyc_aadhaar_verify_otp():
    check_csrf()
    import requests as _req
    otp = request.form.get("otp", "").strip()
    ref_id = session.get("aadhaar_ref_id") or ""

    if not otp or len(otp) != 6:
        flash("❌ 6-digit OTP daalo.", "err")
        return redirect("/seller/kyc")
    if not ref_id:
        flash("❌ Session expire ho gaya. Pehle OTP dobara bhejo.", "err")
        return redirect("/seller/kyc")
    if not SANDBOX_API_KEY:
        flash("❌ KYC service configure nahi hai.", "err")
        return redirect("/seller/kyc")

    try:
        _token = sandbox_get_token()
        if not _token:
            flash("❌ KYC service se connect nahi hua. Dobara try karo.", "err")
            return redirect("/seller/kyc")
        resp = _req.post(
            "https://api.sandbox.co.in/kyc/aadhaar/okyc/otp/verify",
            json={
                "@entity": "in.co.sandbox.kyc.aadhaar.okyc.request",
                "reference_id": ref_id,
                "otp": otp
            },
            headers={
                "x-api-key": SANDBOX_API_KEY,
                "Authorization": f"Bearer {_token}",
                "x-api-version": "1.0",
                "Content-Type": "application/json"
            },
            timeout=10
        )
        data = resp.json()
        if resp.status_code == 200 and (data.get("data", {}).get("status") == "VALID"):
            conn = get_db()
            _exec(conn, "UPDATE users SET aadhaar_verified=1, kyc_step=GREATEST(kyc_step,1) WHERE id=%s",
                  (session["user_id"],))
            conn.commit()
            session.pop("aadhaar_ref_id", None)
            flash("✅ Aadhaar verified! Ab PAN step complete karo.", "ok")
            add_notification(session["user_id"], "Aadhaar Verified ✅",
                             "Aapka Aadhaar verify ho gaya. Step 2 complete karo.", "/seller/kyc")
        else:
            err = data.get("message") or data.get("error") or "OTP galat hai ya expire ho gaya."
            flash(f"❌ {err}", "err")
            session["aadhaar_otp_sent"] = True  # keep OTP form visible
    except _req.exceptions.Timeout:
        flash("❌ Request timeout. Dobara try karo.", "err")
        session["aadhaar_otp_sent"] = True
    except Exception as e:
        app.logger.error(f"Aadhaar OTP verify error: {e}")
        flash("❌ Aadhaar service unavailable. Thodi der baad try karo.", "err")
        session["aadhaar_otp_sent"] = True
    return redirect("/seller/kyc")


# ── KYC STEP 2: PAN Verify ─────────────────────────────────────
@app.route("/seller/kyc/pan", methods=["POST"])
@seller_required
def kyc_verify_pan():
    check_csrf()
    pan = request.form.get("pan", "").strip().upper()
    name_on_pan = request.form.get("name_on_pan", "").strip()
    dob = request.form.get("dob", "").strip()

    import re, requests as _req
    if not re.match(r"^[A-Z]{5}[0-9]{4}[A-Z]$", pan):
        flash("Invalid PAN format. Example: ABCDE1234F", "err")
        return redirect("/seller/kyc")

    if not name_on_pan:
        flash("PAN card pe naam daalna zaroori hai.", "err")
        return redirect("/seller/kyc")

    conn = get_db()

    # ── Duplicate PAN check ────────────────────────────────────
    existing_pan = _exec(conn, "SELECT id FROM users WHERE pan=%s AND id!=%s AND pan_verified=1", (pan, session["user_id"])).fetchone()
    if existing_pan:
        flash("❌ Yeh PAN number already kisi aur seller ke account mein registered hai. Apna sahi PAN daalo.", "err")
        return redirect("/seller/kyc")

    # ── Sandbox.co.in PAN Verification ────────────────────────
    if SANDBOX_API_KEY:
        try:
            _token = sandbox_get_token()
            if not _token:
                flash("❌ KYC service se connect nahi hua. Dobara try karo.", "err")
                return redirect("/seller/kyc")
            sp_resp = _req.post(
                "https://api.sandbox.co.in/kyc/pan/verify",
                json={"pan": pan},
                headers={
                    "x-api-key": SANDBOX_API_KEY,
                    "Authorization": f"Bearer {_token}",
                    "x-api-version": "1.0",
                    "Content-Type": "application/json"
                },
                timeout=10
            )
            sp_data = sp_resp.json()
            # Sandbox returns data.pan_status = "VALID" on success
            pan_status = (sp_data.get("data") or {}).get("pan_status", "")
            if sp_resp.status_code != 200 or pan_status != "VALID":
                err_msg = (sp_data.get("message")
                           or sp_data.get("error")
                           or "PAN verify nahi hua. Sahi PAN number daalo.")
                flash(f"❌ {err_msg}", "err")
                return redirect("/seller/kyc")
            # Name match check — at least one word common
            api_name = (sp_data["data"].get("name") or "").strip().lower()
            entered_name = name_on_pan.strip().lower()
            if api_name and entered_name:
                if not (set(api_name.split()) & set(entered_name.split())):
                    flash(f"❌ PAN pe registered naam '{sp_data['data'].get('name')}' aur aapka daala naam match nahi karta.", "err")
                    return redirect("/seller/kyc")
            # Use official name from PAN database
            if api_name:
                name_on_pan = sp_data["data"].get("name", name_on_pan)
        except _req.exceptions.Timeout:
            flash("PAN verification timeout ho gaya. Dobara try karo.", "err")
            return redirect("/seller/kyc")
        except Exception as e:
            app.logger.error(f"Sandbox PAN error: {e}")
            flash("PAN verification service abhi unavailable hai. Thodi der baad try karo.", "err")
            return redirect("/seller/kyc")

    # ── Duplicate PAN check ──────────────────────────────────
    existing_pan = _exec(conn,
        "SELECT id FROM users WHERE pan=%s AND id!=%s AND pan_verified=1",
        (pan, session["user_id"])).fetchone()
    if existing_pan:
        flash(f"❌ Yeh PAN ({pan}) already kisi aur seller ke account se linked hai. Apna sahi PAN daalo.", "err")
        return redirect("/seller/kyc")

    _exec(conn,
          "UPDATE users SET pan=%s, pan_name=%s, pan_verified=1, kyc_step=GREATEST(kyc_step,1) WHERE id=%s",
          (pan, name_on_pan, session["user_id"]))
    conn.commit()
    flash(f"✅ PAN {pan} verified! Ab GST step complete karo.", "ok")
    add_notification(session["user_id"], "PAN Verified ✅",
                     "Aapka PAN number verify ho gaya. Step 2 complete karo.", "/seller/kyc")
    return redirect("/seller/kyc")


# ── KYC STEP 2: GST Verify ─────────────────────────────────────
@app.route("/seller/kyc/gst", methods=["POST"])
@seller_required
def kyc_verify_gst():
    check_csrf()
    gstin = request.form.get("gstin", "").strip().upper()

    import re, requests as _req
    if not re.match(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$", gstin):
        flash("Invalid GSTIN format. Example: 22AAAAA0000A1Z5", "err")
        return redirect("/seller/kyc")

    conn = get_db()

    # ── Duplicate GST check ────────────────────────────────────
    existing_gst = _exec(conn, "SELECT id FROM users WHERE gstin=%s AND id!=%s AND gstin_verified=1", (gstin, session["user_id"])).fetchone()
    if existing_gst:
        flash("❌ Yeh GSTIN already kisi aur seller ke account mein registered hai. Apna sahi GSTIN daalo.", "err")
        return redirect("/seller/kyc")

    # ── Free GST Portal Verification ──────────────────────────
    gstin_name = ""
    try:
        gst_resp = _req.get(
            f"https://sheet.gstincheck.co.in/check/d4da11cf4dfac9e1a0a4b6a10fdaca1f/{gstin}",
            timeout=10
        )
        gst_data = gst_resp.json()
        if gst_resp.status_code == 200 and gst_data.get("flag") is True:
            gst_status = gst_data.get("data", {}).get("sts", "")
            if gst_status.upper() not in ("ACTIVE",):
                flash(f"❌ GSTIN inactive hai (Status: {gst_status}). Active GST number daalo.", "err")
                return redirect("/seller/kyc")
            # Get company name from GST portal
            gstin_name = (gst_data.get("data", {}).get("tradeNam")
                         or gst_data.get("data", {}).get("lgnm") or "")
            # Cross-check: PAN embedded in GSTIN (position 3-12) vs seller PAN
            seller_rec = _exec(conn, "SELECT pan FROM users WHERE id=%s", (session["user_id"],)).fetchone()
            if seller_rec and seller_rec["pan"]:
                gstin_pan = gstin[2:12]
                if gstin_pan != seller_rec["pan"]:
                    flash(f"❌ GSTIN mein embedded PAN ({gstin_pan}) aapke registered PAN ({seller_rec['pan']}) se alag hai.", "err")
                    return redirect("/seller/kyc")
        else:
            # GST portal unreachable — allow but log
            app.logger.warning(f"GST portal check failed for {gstin}: {gst_data}")
    except Exception as e:
        app.logger.warning(f"GST portal error: {e} — allowing anyway")

    # ── Duplicate GST check ──────────────────────────────────
    existing_gst = _exec(conn,
        "SELECT id FROM users WHERE gstin=%s AND id!=%s AND gstin_verified=1",
        (gstin, session["user_id"])).fetchone()
    if existing_gst:
        flash(f"❌ Yeh GSTIN ({gstin}) already kisi aur seller ke account se linked hai. Apna sahi GSTIN daalo.", "err")
        return redirect("/seller/kyc")

    _exec(conn,
          "UPDATE users SET gstin=%s, gstin_name=%s, gstin_verified=1, kyc_step=GREATEST(kyc_step,2) WHERE id=%s",
          (gstin, gstin_name, session["user_id"]))
    conn.commit()
    flash(f"✅ GSTIN {gstin} verified! Ab bank/UPI step complete karo.", "ok")
    add_notification(session["user_id"], "GST Verified ✅",
                     "Aapka GSTIN verify ho gaya. Step 3 complete karo.", "/seller/kyc")
    return redirect("/seller/kyc")


# ── KYC STEP 3: Bank / Penny Drop ──────────────────────────────
@app.route("/seller/kyc/bank", methods=["POST"])
@seller_required
def kyc_verify_bank():
    check_csrf()
    bank_account = request.form.get("bank_account", "").strip()
    bank_ifsc    = request.form.get("bank_ifsc", "").strip().upper()
    bank_name    = request.form.get("bank_name", "").strip()
    bank_bank    = request.form.get("bank_bank", "").strip()
    upi_id       = request.form.get("upi_id", "").strip()

    conn = get_db()

    if bank_account and bank_ifsc:
        import re
        if not re.match(r"^[A-Z]{4}0[A-Z0-9]{6}$", bank_ifsc):
            flash("Invalid IFSC format. Example: HDFC0001234", "err")
            return redirect("/seller/kyc")
        # PAN naam vs bank naam match check
        seller_pan = _exec(conn, "SELECT pan_name FROM users WHERE id=%s", (session["user_id"],)).fetchone()
        pan_name_stored = (seller_pan["pan_name"] or "").strip().lower() if seller_pan and seller_pan["pan_name"] else ""
        bank_name_lower = bank_name.strip().lower()
        if pan_name_stored and bank_name_lower:
            # Partial match — at least first word should match
            pan_words = set(pan_name_stored.split())
            bank_words = set(bank_name_lower.split())
            common = pan_words & bank_words
            if not common:
                flash(f"❌ Bank account naam '{bank_name}' aur PAN card naam '{seller_pan['pan_name']}' match nahi karta. Same naam ka account use karo.", "err")
                return redirect("/seller/kyc")
        # ── Sandbox.co.in Bank Account Verification ──────────
        import requests as _req
        if SANDBOX_API_KEY:
            try:
                _token = sandbox_get_token()
                if not _token:
                    flash("❌ KYC service se connect nahi hua. Dobara try karo.", "err")
                    return redirect("/seller/kyc")
                sp_resp = _req.post(
                    "https://api.sandbox.co.in/kyc/bank-account/verify",
                    json={"bank_account_number": bank_account, "ifsc": bank_ifsc},
                    headers={
                        "x-api-key": SANDBOX_API_KEY,
                        "Authorization": f"Bearer {_token}",
                        "x-api-version": "1.0",
                        "Content-Type": "application/json"
                    },
                    timeout=15
                )
                sp_data = sp_resp.json()
                acc_status = (sp_data.get("data") or {}).get("account_status", "")
                if sp_resp.status_code != 200 or acc_status not in ("active", "ACTIVE", "valid", "VALID"):
                    err_msg = (sp_data.get("message")
                               or sp_data.get("error")
                               or "Bank account verify nahi hua. Account number aur IFSC check karo.")
                    flash(f"❌ {err_msg}", "err")
                    return redirect("/seller/kyc")
                # Use registered name from bank
                api_name = (sp_data["data"].get("name_at_bank") or sp_data["data"].get("name") or "").strip()
                if api_name:
                    bank_name = api_name
            except _req.exceptions.Timeout:
                flash("Bank verification timeout ho gaya. Dobara try karo.", "err")
                return redirect("/seller/kyc")
            except Exception as e:
                app.logger.error(f"Sandbox Bank error: {e}")
                flash("Bank verification service abhi unavailable hai. Thodi der baad try karo.", "err")
                return redirect("/seller/kyc")

        _exec(conn,
            "UPDATE users SET bank_account=%s, bank_ifsc=%s, bank_name=%s, bank=%s, bank_bank=%s, bank_verified=1 WHERE id=%s",
            (bank_account, bank_ifsc, bank_name, bank_bank, bank_bank, session["user_id"])
        )
        flash(f"✅ Bank account verified! KYC complete ho gaya. 🎉", "ok")
    elif upi_id:
        _exec(conn, 
            "UPDATE users SET upi_id=%s, bank_verified=1 WHERE id=%s",
            (upi_id, session["user_id"])
        )
        flash(f"✅ UPI ID {upi_id} verified! KYC complete ho gaya. 🎉", "ok")
    else:
        flash("Bank account + IFSC ya UPI ID zaroori hai.", "err")
        return redirect("/seller/kyc")

    # Update kyc_step to 3 (fully done)
    _exec(conn, "UPDATE users SET kyc_step=3 WHERE id=%s", (session["user_id"],))
    conn.commit()
    add_notification(session["user_id"], "KYC Complete 🎉",
                     "Badhai ho! Aapka KYC complete ho gaya. Ab aap seller dashboard access kar sakte ho.", "/seller/dashboard")
    return redirect("/seller/dashboard")


# ── KYC: Skip GST (GST-exempt sellers) ────────────────────────
@app.route("/seller/kyc/skip-gst")
@seller_required
def kyc_skip_gst():
    conn = get_db()
    _exec(conn, "UPDATE users SET gstin_verified=1 WHERE id=%s", (session["user_id"],))
    conn.commit()
    flash("GST step skip kar diya. Composition dealers ke liye applicable.", "ok")
    return redirect("/seller/kyc")


@app.route("/seller/ping")
@seller_required
def seller_ping():
    """Called by JS every 30s to check for new pending orders."""
    conn = get_db()
    count = _scalar(_exec(conn, 
        "SELECT COUNT(DISTINCT o.id) FROM orders o JOIN order_items oi ON oi.order_id=o.id "
        "WHERE oi.seller_id=%s AND o.status='pending'", (session["user_id"],)
    ))
    return jsonify({"pending": int(count or 0)})


@app.route("/seller/analytics")
@seller_required
def seller_analytics():
    conn = get_db()
    sid  = session["user_id"]
    stats = {}
    stats["total_revenue"] = _scalar(_exec(conn, 
        "SELECT COALESCE(SUM(oi.line_total),0) FROM order_items oi JOIN orders o ON oi.order_id=o.id "
        "WHERE oi.seller_id=%s AND o.status!='cancelled'", (sid,)))
    stats["month_revenue"] = _scalar(_exec(conn, 
        "SELECT COALESCE(SUM(oi.line_total),0) FROM order_items oi JOIN orders o ON oi.order_id=o.id "
        "WHERE oi.seller_id=%s AND o.status!='cancelled' AND TO_CHAR(o.created_at, 'YYYY-MM')=TO_CHAR(NOW(), 'YYYY-MM')", (sid,)))
    stats["month_orders"] = _scalar(_exec(conn, 
        "SELECT COUNT(DISTINCT oi.order_id) FROM order_items oi JOIN orders o ON oi.order_id=o.id "
        "WHERE oi.seller_id=%s AND TO_CHAR(o.created_at, 'YYYY-MM')=TO_CHAR(NOW(), 'YYYY-MM')", (sid,)))
    stats["total_returns"] = _scalar(_exec(conn, 
        "SELECT COUNT(*) FROM return_requests rr JOIN order_items oi ON rr.order_item_id=oi.id WHERE oi.seller_id=%s", (sid,)))
    total_items = _scalar(_exec(conn, 
        "SELECT COALESCE(SUM(oi.qty),0) FROM order_items oi JOIN orders o ON oi.order_id=o.id WHERE oi.seller_id=%s AND o.status!='cancelled'", (sid,)))
    stats["return_rate"] = (stats["total_returns"] / max(1, total_items)) * 100
    stats["avg_rating"]  = _scalar(_exec(conn, 
        "SELECT COALESCE(AVG(r.rating),0) FROM reviews r JOIN products p ON r.product_id=p.id WHERE p.seller_id=%s", (sid,)))
    stats["total_reviews"] = _scalar(_exec(conn, 
        "SELECT COUNT(*) FROM reviews r JOIN products p ON r.product_id=p.id WHERE p.seller_id=%s", (sid,)))

    chart_rows = _exec(conn, 
        "SELECT TO_CHAR(o.created_at, 'DD Mon') as label, SUM(oi.line_total) rev, COUNT(DISTINCT oi.order_id) cnt "
        "FROM order_items oi JOIN orders o ON oi.order_id=o.id "
        "WHERE oi.seller_id=%s AND o.status!='cancelled' AND o.created_at >= NOW() - INTERVAL '30 days' "
        "GROUP BY o.created_at::date, TO_CHAR(o.created_at, 'DD Mon') ORDER BY o.created_at::date", (sid,)
    ).fetchall()
    chart_data = {
        "labels":  [r["label"] for r in chart_rows],
        "revenue": [round(r["rev"], 2) for r in chart_rows],
        "orders":  [r["cnt"] for r in chart_rows],
    }
    top_raw = _exec(conn, 
        "SELECT oi.title, SUM(oi.qty) total_qty, SUM(oi.line_total) total_rev "
        "FROM order_items oi JOIN orders o ON oi.order_id=o.id "
        "WHERE oi.seller_id=%s AND o.status!='cancelled' GROUP BY oi.product_id, oi.title ORDER BY total_rev DESC LIMIT 5", (sid,)
    ).fetchall()
    max_rev      = max((r["total_rev"] for r in top_raw), default=1)
    top_products = [dict(r, pct=int(r["total_rev"] / max_rev * 100)) for r in top_raw]
    status_rows  = _exec(conn, 
        "SELECT o.status, COUNT(DISTINCT o.id) cnt FROM order_items oi JOIN orders o ON oi.order_id=o.id "
        "WHERE oi.seller_id=%s GROUP BY o.status", (sid,)
    ).fetchall()
    status_data  = {"labels": [r["status"] for r in status_rows], "counts": [r["cnt"] for r in status_rows]}
    # Category revenue breakdown (NEW)
    cat_rows = _exec(conn, 
        "SELECT p.category, COALESCE(SUM(oi.line_total),0) rev "
        "FROM order_items oi JOIN products p ON oi.product_id=p.id "
        "JOIN orders o ON oi.order_id=o.id "
        "WHERE oi.seller_id=%s AND o.status!='cancelled' GROUP BY p.category ORDER BY rev DESC LIMIT 6", (sid,)
    ).fetchall()
    category_data = {"labels": [r["category"] for r in cat_rows], "revenue": [round(r["rev"],2) for r in cat_rows]}
    # Top products with revenue column name fix
    top_raw2 = _exec(conn, 
        "SELECT oi.title, SUM(oi.qty) total_qty, SUM(oi.line_total) revenue "
        "FROM order_items oi JOIN orders o ON oi.order_id=o.id "
        "WHERE oi.seller_id=%s AND o.status!='cancelled' GROUP BY oi.product_id, oi.title ORDER BY revenue DESC LIMIT 6", (sid,)
    ).fetchall()
    top_products = list(top_raw2)
    transactions = _exec(conn, 
        "SELECT * FROM seller_transactions WHERE seller_id=%s ORDER BY created_at DESC LIMIT 20", (sid,)
    ).fetchall()
    period = request.args.get("period","daily")
    return render_template("seller_analytics.html", stats=stats, chart_data=chart_data,
                           top_products=top_products, status_data=status_data,
                           category_data=category_data,
                           transactions=transactions, chart_period=period)


@app.route("/seller/returns")
@seller_required
def seller_returns():
    conn   = get_db()
    status = request.args.get("status", "")
    sql    = ("SELECT rr.*, oi.title, oi.qty, oi.line_total, o.public_id, o.buyer_name, o.phone "
              "FROM return_requests rr JOIN order_items oi ON rr.order_item_id=oi.id "
              "JOIN orders o ON oi.order_id=o.id WHERE oi.seller_id=%s ")
    params = [session["user_id"]]
    if status:
        sql += "AND rr.status=%s "; params.append(status)
    sql    += "ORDER BY rr.created_at DESC"
    returns = _exec(conn, sql, params).fetchall()
    return render_template("seller_returns.html", returns=returns, status=status)


@app.route("/seller/returns/<int:rid>/approve", methods=["POST"])
@seller_required
def seller_return_approve(rid):
    check_csrf()
    conn = get_db()
    rr = _exec(conn, 
        "SELECT rr.*, oi.line_total, oi.order_id, o.buyer_id, o.public_id "
        "FROM return_requests rr "
        "JOIN order_items oi ON rr.order_item_id=oi.id "
        "JOIN orders o ON oi.order_id=o.id "
        "WHERE rr.id=%s", (rid,)
    ).fetchone()
    _exec(conn, "UPDATE return_requests SET status='approved', updated_at=NOW() WHERE id=%s", (rid,))
    conn.commit()
    if rr and rr["line_total"]:
        refund = float(rr["line_total"])
        refund_id = "CR" + secrets.token_hex(8).upper()
        _exec(conn, "UPDATE users SET wallet_balance=COALESCE(wallet_balance,0)+%s WHERE id=%s",
                     (refund, rr["buyer_id"]))
        _exec(conn,
              "UPDATE orders SET status='refunded', updated_at=NOW(), refund_id=%s, "
              "refund_status='completed', refund_amount=%s, refund_completed_at=NOW() WHERE id=%s",
              (refund_id, refund, rr["order_id"]))
        conn.commit()
        add_notification(rr["buyer_id"], "✅ Return Approved!",
                         f"Aapka return approve ho gaya. Rs.{refund:.2f} wallet mein add ho gaye.",
                         "/profile")
    conn.commit()
    flash("Return approved. Buyer ko refund mil gaya.", "ok")
    return redirect("/seller/returns")


@app.route("/seller/returns/<int:rid>/reject", methods=["POST"])
@seller_required
def seller_return_reject(rid):
    check_csrf()
    conn = get_db()
    _exec(conn, "UPDATE return_requests SET status='rejected', updated_at=NOW() WHERE id=%s", (rid,))
    conn.commit()
    flash("Return rejected.", "ok")
    return redirect("/seller/returns")


@app.route("/seller/bulk-upload", methods=["GET", "POST"])
@seller_required
def seller_bulk_upload():
    results = None
    if request.method == "POST":
        check_csrf()
        f = request.files.get("csv_file")
        if f:
            stream  = io.StringIO(f.stream.read().decode("utf-8"), newline=None)
            reader  = csv.DictReader(stream)
            conn    = get_db()
            success, errors, total = 0, [], 0
            for i, row in enumerate(reader, 2):
                total += 1
                try:
                    title = row.get("title", "").strip()
                    if not title:
                        raise ValueError("title is required")
                    _exec(conn, 
                        "INSERT INTO products (seller_id,title,category,description,price,mrp,stock,gst_percent,hsn,size_options,color_options,approved) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0)",
                        (session["user_id"], title, row.get("category", "General").strip(),
                         row.get("description", ""), float(row.get("price", 0)),
                         row.get("mrp") or None, int(row.get("stock", 0)),
                         float(row.get("gst_percent", 18)), row.get("hsn", ""),
                         row.get("size_options", ""), row.get("color_options", ""))
                    )
                    success += 1
                except (ValueError, KeyError, psycopg2.errors.UniqueViolation, psycopg2.IntegrityError) as e:
                    errors.append({"row": i, "message": str(e)})
            conn.commit()
            results = {"success": success, "errors": errors, "total": total}
    return render_template("seller_bulk_upload.html", results=results)


@app.route("/seller/bulk-upload/template")
@seller_required
def seller_bulk_template():
    output = io.StringIO()
    output.write("title,category,description,price,mrp,stock,gst_percent,hsn,brand,size_options,color_options\n")
    output.write("Sample T-Shirt,Fashion,A cotton t-shirt,299,599,50,5,61091000,BrandName,\"S,M,L,XL\",\"Red,Blue\"\n")
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=xoptime_bulk_template.csv"})


# ─────────────────────────────────────────────────────────────
# ── ADMIN ROUTES
# ─────────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """Admin ke liye alag email + password login."""
    if session.get("role") == "admin":
        return redirect("/admin")
    if request.method == "POST":
        check_csrf()
        email = request.form.get("email", "").strip().lower()
        pw    = request.form.get("password", "")
        conn  = get_db()
        user  = _exec(conn, 
            "SELECT * FROM users WHERE email=%s AND role='admin'", (email,)
        ).fetchone()
        if not user or not check_password_hash(user["password"], pw):
            flash("Galat email ya password.", "err")
            return redirect("/admin/login")
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        session["name"]    = user["name"]
        session["role"]    = user["role"]
        generate_csrf()
        return redirect("/admin")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/admin/login")


@app.route("/admin")
@admin_required
def admin_dashboard():
    conn = get_db()
    period = request.args.get("period", "daily")
    k = {}
    k["users"]          = _scalar(_exec(conn, "SELECT COUNT(*) FROM users"))
    k["sellers"]        = _scalar(_exec(conn, "SELECT COUNT(*) FROM users WHERE role='seller'"))
    k["products"]       = _scalar(_exec(conn, "SELECT COUNT(*) FROM products WHERE approved=1"))
    k["orders"]         = _scalar(_exec(conn, "SELECT COUNT(*) FROM orders"))
    k["today_orders"]   = _scalar(_exec(conn, "SELECT COUNT(*) FROM orders WHERE created_at::date = CURRENT_DATE"))
    k["today_revenue"]  = _scalar(_exec(conn, "SELECT COALESCE(SUM(total_amount),0) FROM orders WHERE created_at::date = CURRENT_DATE AND status!='cancelled'"))
    k["month_revenue"]  = _scalar(_exec(conn, "SELECT COALESCE(SUM(total_amount),0) FROM orders WHERE TO_CHAR(created_at, 'YYYY-MM')=TO_CHAR(NOW(), 'YYYY-MM') AND status!='cancelled'"))
    k["total_revenue"]  = _scalar(_exec(conn, "SELECT COALESCE(SUM(total_amount),0) FROM orders WHERE status!='cancelled'"))
    k["pending_orders"] = _scalar(_exec(conn, "SELECT COUNT(*) FROM orders WHERE status='pending'"))
    k["pending_payouts"]= _scalar(_exec(conn, "SELECT COUNT(*) FROM seller_transactions WHERE status='pending'"))
    k["open_tickets"]   = _scalar(_exec(conn, "SELECT COUNT(*) FROM support_tickets WHERE status='open'"))
    k["pending_returns"]= _scalar(_exec(conn, "SELECT COUNT(*) FROM return_requests WHERE status='pending'"))
    k["resellers"]      = _scalar(_exec(conn, "SELECT COUNT(*) FROM resellers"))

    top_sellers = _exec(conn, 
        "SELECT u.name, SUM(oi.line_total) revenue, COUNT(DISTINCT oi.order_id) orders "
        "FROM order_items oi JOIN users u ON oi.seller_id=u.id "
        "JOIN orders o ON oi.order_id=o.id WHERE o.status!='cancelled' "
        "GROUP BY oi.seller_id, u.name ORDER BY revenue DESC LIMIT 5"
    ).fetchall()
    recent_orders = _exec(conn, "SELECT * FROM orders ORDER BY created_at DESC LIMIT 10").fetchall()
    chart_rows = _exec(conn, 
        "SELECT TO_CHAR(created_at, 'DD Mon') label, SUM(total_amount) rev, COUNT(*) cnt "
        "FROM orders WHERE status!='cancelled' AND created_at >= NOW() - INTERVAL '30 days' "
        "GROUP BY created_at::date, TO_CHAR(created_at, 'DD Mon') ORDER BY created_at::date"
    ).fetchall()
    chart_data  = {"labels": [r["label"] for r in chart_rows],
                   "revenue": [r["rev"] for r in chart_rows],
                   "orders":  [r["cnt"] for r in chart_rows]}
    status_rows = _exec(conn, "SELECT status, COUNT(*) cnt FROM orders GROUP BY status").fetchall()
    status_data = {"labels": [r["status"] for r in status_rows],
                   "counts": [r["cnt"] for r in status_rows]}
    return render_template("admin_dashboard.html", k=k, top_sellers=top_sellers,
                           recent_orders=recent_orders, chart_data=chart_data,
                           status_data=status_data, period=period)


@app.route("/admin/orders")
@admin_required
def admin_orders():
    conn     = get_db()
    q        = request.args.get("q", "").strip()
    status   = request.args.get("status", "")
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    per_page = 30
    sql      = "SELECT * FROM orders WHERE 1=1 "
    params   = []
    if q:
        sql += "AND (public_id LIKE %s OR buyer_name LIKE %s OR phone LIKE %s) "
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if status:
        sql += "AND status=%s "
        params.append(status)
    count_sql    = sql.replace("SELECT * FROM orders", "SELECT COUNT(*) FROM orders")
    total        = _scalar(_exec(conn, count_sql, params))
    total_pages  = max(1, (total + per_page - 1) // per_page)
    sql         += "ORDER BY created_at DESC LIMIT %s OFFSET %s"
    orders       = _exec(conn, sql, params + [per_page, (page-1)*per_page]).fetchall()
    statuses     = [r["status"] for r in _exec(conn, "SELECT DISTINCT status FROM orders ORDER BY status").fetchall()]
    qs           = "&".join(f"{k}={v}" for k,v in request.args.items() if k != "page")
    return render_template("admin_orders.html", orders=orders, q=q, status=status,
                           statuses=statuses, page=page, total_pages=total_pages,
                           total=total, query_string=qs)


@app.route("/admin/orders/<int:oid>", methods=["GET", "POST"])
@admin_required
def admin_order_detail(oid):
    conn = get_db()
    o = _exec(conn, "SELECT * FROM orders WHERE id=%s", (oid,)).fetchone()
    if not o:
        abort(404)
    if request.method == "POST":
        check_csrf()
        status       = request.form.get("status", o["status"])
        courier_name = request.form.get("courier_name", "")
        awb          = request.form.get("awb", "")
        tracking_url = request.form.get("tracking_url", "")
        shipped_at   = o["shipped_at"]
        delivered_at = o["delivered_at"]
        if status == "shipped" and not shipped_at:
            shipped_at = datetime.now().isoformat()
        if status == "delivered" and not delivered_at:
            delivered_at = datetime.now().isoformat()
            # Mark seller transactions as earned
            _exec(conn, 
                "UPDATE seller_transactions SET status='earned' WHERE order_id=%s", (oid,)
            )
        _exec(conn, 
            "UPDATE orders SET status=%s,courier_name=%s,awb=%s,tracking_url=%s,shipped_at=%s,delivered_at=%s,updated_at=NOW() WHERE id=%s",
            (status, courier_name, awb, tracking_url, shipped_at, delivered_at, oid)
        )
        conn.commit()
        # Notify buyer
        buyer = _exec(conn, "SELECT * FROM orders WHERE id=%s", (oid,)).fetchone()
        if buyer:
            add_notification(buyer["buyer_id"], f"Order {status}",
                             f"Your order #{o['public_id']} is now {status}.", f"/orders")
        flash("Order updated.", "ok")
        return redirect(f"/admin/orders/{oid}")
    items = _exec(conn, "SELECT * FROM order_items WHERE order_id=%s", (oid,)).fetchall()
    return render_template("admin_order_detail.html", o=o, items=items)


@app.route("/admin/orders/<int:oid>/invoice")
@admin_required
def admin_invoice(oid):
    conn  = get_db()
    o     = _exec(conn, "SELECT * FROM orders WHERE id=%s", (oid,)).fetchone()
    if not o:
        abort(404)
    items = _exec(conn, "SELECT * FROM order_items WHERE order_id=%s", (oid,)).fetchall()
    seller_obj = None
    if items and items[0]["seller_id"]:
        seller_obj = _exec(conn, "SELECT * FROM users WHERE id=%s", (items[0]["seller_id"],)).fetchone()
    return render_template("invoice.html", o=o, items=items, seller=seller_obj,
                           company_name=COMPANY_NAME, company_gstin=COMPANY_GSTIN,
                           company_address=COMPANY_ADDRESS)


@app.route("/admin/orders/<int:oid>/label")
@admin_required
def admin_label(oid):
    conn = get_db()
    o    = _exec(conn, "SELECT * FROM orders WHERE id=%s", (oid,)).fetchone()
    if not o:
        abort(404)
    items = _exec(conn, "SELECT * FROM order_items WHERE order_id=%s", (oid,)).fetchall()
    seller_obj = None
    if items and items[0]["seller_id"]:
        seller_obj = _exec(conn, "SELECT * FROM users WHERE id=%s", (items[0]["seller_id"],)).fetchone()
    # Generate QR code for order public_id (base64 PNG)
    qr_b64 = None
    bar_b64 = None
    try:
        import qrcode, base64
        qr = qrcode.make(o["public_id"])
        buf = io.BytesIO()
        qr.save(buf, format="PNG")
        qr_b64 = base64.b64encode(buf.getvalue()).decode()
    except ImportError:
        pass  # qrcode package not installed — label shows without QR
    return render_template("label.html", o=o, items=items, seller=seller_obj,
                           company_name=COMPANY_NAME, qr_b64=qr_b64, bar_b64=bar_b64)


@app.route("/admin/products")
@admin_required
def admin_products():
    conn     = get_db()
    products = _exec(conn, 
        "SELECT p.*, u.name as seller_name FROM products p JOIN users u ON p.seller_id=u.id ORDER BY p.created_at DESC"
    ).fetchall()
    return render_template("admin_products.html", products=products)


@app.route("/admin/products/approve/<int:pid>", methods=["POST"])
@admin_required
def admin_product_approve(pid):
    check_csrf()
    conn = get_db()
    _exec(conn, "UPDATE products SET approved=1 WHERE id=%s", (pid,))
    conn.commit()
    p = _exec(conn, "SELECT * FROM products WHERE id=%s", (pid,)).fetchone()
    if p:
        add_notification(p["seller_id"], "Product Approved",
                         f"Your product '{p['title']}' has been approved!", f"/p/{pid}")
    conn.commit()
    flash("Product approved.", "ok")
    return redirect("/admin/products")


@app.route("/admin/products/unapprove/<int:pid>", methods=["POST"])
@admin_required
def admin_product_unapprove(pid):
    check_csrf()
    conn = get_db()
    _exec(conn, "UPDATE products SET approved=0 WHERE id=%s", (pid,))
    conn.commit()
    flash("Product unapproved.", "ok")
    return redirect("/admin/products")


@app.route("/admin/users")
@admin_required
def admin_users():
    conn  = get_db()
    q     = request.args.get("q","").strip()
    role  = request.args.get("role","")
    sql   = "SELECT * FROM users WHERE seller_status != 'deleted' AND 1=1 "
    params = []
    if q:
        sql += "AND (name LIKE %s OR email LIKE %s) "
        params += [f"%{q}%", f"%{q}%"]
    if role:
        sql += "AND role=%s "
        params.append(role)
    sql += "ORDER BY created_at DESC"
    users = _exec(conn, sql, params).fetchall()
    return render_template("admin_users.html", users=users, q=q, role=role)


@app.route("/admin/users/<int:uid>/ban", methods=["POST"])
@admin_required
def admin_user_ban(uid):
    check_csrf()
    conn = get_db()
    u = _exec(conn, "SELECT * FROM users WHERE id=%s", (uid,)).fetchone()
    if not u or u["role"] == "admin":
        flash("Admins ko ban nahi kar sakte.", "err")
        return redirect("/admin/users")
    _exec(conn, "UPDATE users SET seller_status='suspended' WHERE id=%s", (uid,))
    conn.commit()
    flash(f"{u['name']} banned.", "ok")
    return redirect("/admin/users")


@app.route("/admin/users/<int:uid>/unban", methods=["POST"])
@admin_required
def admin_user_unban(uid):
    check_csrf()
    conn = get_db()
    _exec(conn, "UPDATE users SET seller_status='active' WHERE id=%s", (uid,))
    conn.commit()
    flash("User unbanned.", "ok")
    return redirect("/admin/users")


@app.route("/admin/users/<int:uid>/delete", methods=["POST"])
@admin_required
def admin_user_delete(uid):
    check_csrf()
    conn = get_db()
    u = _exec(conn, "SELECT * FROM users WHERE id=%s", (uid,)).fetchone()
    if not u:
        abort(404)
    if u["role"] == "admin":
        flash("Admin account delete nahi kar sakte.", "err")
        return redirect("/admin/users")
    reason = request.form.get("reason", "Admin deleted").strip() or "Admin deleted"

    # ── Archive to deleted_users table first ─────────────────
    _exec(conn, """
        INSERT INTO deleted_users
            (original_id, name, email, phone, role, gstin, pan, seller_status, deleted_by, reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (u["id"], u["name"], u["email"], u.get("phone"), u["role"],
          u.get("gstin"), u.get("pan"), u.get("seller_status"), "admin", reason))

    # ── Hard delete — remove all related data ─────────────────
    # Delete products
    _exec(conn, "DELETE FROM product_images WHERE product_id IN (SELECT id FROM products WHERE seller_id=%s)", (uid,))
    _exec(conn, "DELETE FROM product_variants WHERE product_id IN (SELECT id FROM products WHERE seller_id=%s)", (uid,))
    _exec(conn, "DELETE FROM products WHERE seller_id=%s", (uid,))
    # Delete cart/wishlist
    _exec(conn, "DELETE FROM cart_items WHERE user_id=%s", (uid,))
    _exec(conn, "DELETE FROM wishlist_items WHERE user_id=%s", (uid,))
    # Delete notifications
    _exec(conn, "DELETE FROM notifications WHERE user_id=%s", (uid,))
    # Delete seller transactions
    _exec(conn, "DELETE FROM seller_transactions WHERE seller_id=%s", (uid,))
    # Delete reviews
    _exec(conn, "DELETE FROM reviews WHERE buyer_id=%s", (uid,))
    # Delete support tickets
    _exec(conn, "DELETE FROM support_tickets WHERE user_id=%s", (uid,))
    # Delete password reset tokens
    _exec(conn, "DELETE FROM password_reset_tokens WHERE user_id=%s", (uid,))
    # Finally delete the user
    _exec(conn, "DELETE FROM users WHERE id=%s", (uid,))
    conn.commit()

    flash(f"✅ User '{u['name']}' ({u['email']}) permanently delete ho gaya aur archive mein save hai.", "ok")
    return redirect("/admin/users")


@app.route("/admin/deleted-users")
@admin_required
def admin_deleted_users():
    conn = get_db()
    q = request.args.get("q", "").strip()
    if q:
        users = _exec(conn,
            "SELECT * FROM deleted_users WHERE name ILIKE %s OR email ILIKE %s ORDER BY deleted_at DESC",
            (f"%{q}%", f"%{q}%")).fetchall()
    else:
        users = _exec(conn, "SELECT * FROM deleted_users ORDER BY deleted_at DESC").fetchall()
    return render_template("admin_deleted_users.html", users=users, q=q)


@app.route("/admin/sellers")
@admin_required
def admin_sellers():
    conn   = get_db()
    status = request.args.get("status", "")
    # Use subqueries for aggregates to avoid GROUP BY u.* issue in PostgreSQL
    status_filter = "AND COALESCE(u.seller_status,'active')=%s" if status else ""
    params = [status] if status else []
    sql = f"""
        SELECT u.*,
               COALESCE(p_agg.product_count, 0) product_count,
               COALESCE(oi_agg.total_revenue, 0) total_revenue,
               COALESCE(oi_agg.order_count, 0) order_count,
               COALESCE(r_agg.avg_rating, 0) avg_rating,
               COALESCE(u.seller_status, 'active') seller_status
        FROM users u
        LEFT JOIN (SELECT seller_id, COUNT(DISTINCT id) product_count FROM products GROUP BY seller_id) p_agg ON p_agg.seller_id=u.id
        LEFT JOIN (SELECT seller_id, COALESCE(SUM(line_total),0) total_revenue, COUNT(DISTINCT order_id) order_count FROM order_items GROUP BY seller_id) oi_agg ON oi_agg.seller_id=u.id
        LEFT JOIN (SELECT p.seller_id, AVG(r.rating) avg_rating FROM reviews r JOIN products p ON r.product_id=p.id GROUP BY p.seller_id) r_agg ON r_agg.seller_id=u.id
        WHERE u.role='seller' {status_filter}
        ORDER BY total_revenue DESC
    """
    sellers = _exec(conn, sql, params).fetchall()
    return render_template("admin_sellers.html", sellers=sellers, status=status)


@app.route("/admin/sellers/<int:uid>/approve", methods=["POST"])
@admin_required
def admin_seller_approve(uid):
    check_csrf()
    conn = get_db()
    _exec(conn, "UPDATE users SET seller_status='active' WHERE id=%s", (uid,))
    conn.commit()
    flash("Seller approved.", "ok")
    return redirect("/admin/sellers")


@app.route("/admin/sellers/<int:uid>/suspend", methods=["POST"])
@admin_required
def admin_seller_suspend(uid):
    check_csrf()
    conn = get_db()
    _exec(conn, "UPDATE users SET seller_status='suspended' WHERE id=%s", (uid,))
    conn.commit()
    flash("Seller suspended.", "ok")
    return redirect("/admin/sellers")


@app.route("/admin/sellers/<int:uid>/unsuspend", methods=["POST"])
@admin_required
def admin_seller_unsuspend(uid):
    check_csrf()
    conn = get_db()
    _exec(conn, "UPDATE users SET seller_status='active' WHERE id=%s", (uid,))
    conn.commit()
    flash("Seller unsuspended.", "ok")
    return redirect("/admin/sellers")


@app.route("/admin/returns")
@admin_required
def admin_returns():
    conn   = get_db()
    status = request.args.get("status", "")
    sql    = ("SELECT rr.*, oi.title, oi.qty, oi.line_total, o.public_id, o.buyer_name, o.phone, u.name as seller_name "
              "FROM return_requests rr JOIN order_items oi ON rr.order_item_id=oi.id "
              "JOIN orders o ON oi.order_id=o.id JOIN users u ON oi.seller_id=u.id WHERE 1=1 ")
    params = []
    if status:
        sql += "AND rr.status=%s "; params.append(status)
    sql    += "ORDER BY rr.created_at DESC"
    returns = _exec(conn, sql, params).fetchall()
    return render_template("admin_returns.html", returns=returns, status=status)


@app.route("/admin/returns/<int:rid>/approve", methods=["POST"])
@admin_required
def admin_return_approve(rid):
    check_csrf()
    conn = get_db()
    rr = _exec(conn, 
        "SELECT rr.*, oi.line_total, oi.order_id, o.buyer_id, o.public_id "
        "FROM return_requests rr "
        "JOIN order_items oi ON rr.order_item_id=oi.id "
        "JOIN orders o ON oi.order_id=o.id "
        "WHERE rr.id=%s", (rid,)
    ).fetchone()
    _exec(conn, "UPDATE return_requests SET status='approved', updated_at=NOW() WHERE id=%s", (rid,))
    conn.commit()
    if rr and rr["line_total"]:
        refund = float(rr["line_total"])
        refund_id = "CR" + secrets.token_hex(8).upper()
        _exec(conn, "UPDATE users SET wallet_balance=COALESCE(wallet_balance,0)+%s WHERE id=%s",
                     (refund, rr["buyer_id"]))
        _exec(conn,
              "UPDATE orders SET status='refunded', updated_at=NOW(), refund_id=%s, "
              "refund_status='completed', refund_amount=%s, refund_completed_at=NOW() WHERE id=%s",
              (refund_id, refund, rr["order_id"]))
        conn.commit()
        add_notification(rr["buyer_id"], "✅ Return Approved!",
                         f"Aapka return approve ho gaya. Rs.{refund:.2f} wallet mein add ho gaye.",
                         "/profile")
    conn.commit()
    flash("Return approved. Refund buyer ke wallet mein.", "ok")
    return redirect("/admin/returns")


@app.route("/admin/returns/<int:rid>/reject", methods=["POST"])
@admin_required
def admin_return_reject(rid):
    check_csrf()
    conn = get_db()
    _exec(conn, "UPDATE return_requests SET status='rejected', updated_at=NOW() WHERE id=%s", (rid,))
    conn.commit()
    flash("Return rejected.", "ok")
    return redirect("/admin/returns")


@app.route("/admin/coupons", methods=["GET", "POST"])
@admin_required
def admin_coupons():
    conn = get_db()
    if request.method == "POST":
        check_csrf()
        action = request.form.get("action")
        if action == "create":
            code     = request.form.get("code", "").strip().upper()
            dtype    = request.form.get("discount_type", "percent")
            dvalue   = float(request.form.get("discount_value", 0))
            min_ord  = float(request.form.get("min_order", 0))
            max_uses = int(request.form.get("max_uses", 100))
            expires  = request.form.get("expires_at") or None
            try:
                _exec(conn, 
                    "INSERT INTO coupons (code,discount_type,discount_value,min_order,max_uses,expires_at) VALUES (%s,%s,%s,%s,%s,%s)",
                    (code, dtype, dvalue, min_ord, max_uses, expires)
                )
                conn.commit()
                flash(f"Coupon {code} created.", "ok")
            except (psycopg2.errors.UniqueViolation, psycopg2.IntegrityError):
                flash("Coupon code already exists.", "err")
        elif action == "toggle":
            cid = request.form.get("coupon_id")
            _exec(conn, "UPDATE coupons SET active=1-active WHERE id=%s", (cid,))
            conn.commit()
        return redirect("/admin/coupons")
    coupons = _exec(conn, "SELECT * FROM coupons ORDER BY created_at DESC").fetchall()
    return render_template("admin_coupons.html", coupons=coupons)


@app.route("/admin/tickets")
@admin_required
def admin_tickets():
    conn = get_db()
    tks  = _exec(conn, 
        "SELECT t.*, u.name as user_name, u.email as user_email "
        "FROM support_tickets t JOIN users u ON t.user_id=u.id ORDER BY t.updated_at DESC"
    ).fetchall()
    return render_template("admin_tickets.html", tks=tks)


@app.route("/admin/tickets/<int:tid>", methods=["GET", "POST"])
@admin_required
def admin_ticket_detail(tid):
    conn   = get_db()
    ticket = _exec(conn, 
        "SELECT t.*, u.name as user_name FROM support_tickets t JOIN users u ON t.user_id=u.id WHERE t.id=%s",
        (tid,)
    ).fetchone()
    if not ticket:
        abort(404)
    if request.method == "POST":
        check_csrf()
        reply  = request.form.get("reply", "").strip()
        status = request.form.get("status", ticket["status"])
        _exec(conn, 
            "UPDATE support_tickets SET admin_reply=%s,status=%s,updated_at=NOW() WHERE id=%s",
            (reply, status, tid)
        )
        conn.commit()
        add_notification(ticket["user_id"], "Support Reply",
                         "Admin replied to your support ticket.", f"/support/{tid}")
        flash("Reply saved.", "ok")
        return redirect(f"/admin/tickets/{tid}")
    return render_template("support_detail.html", ticket=ticket, tk=ticket, admin_view=True)


@app.route("/admin/payouts")
@admin_required
def admin_payouts():
    conn = get_db()
    status = request.args.get("status","")
    sql = ("SELECT st.*, u.name as seller_name, u.bank_account, u.bank_ifsc, u.upi_id, o.public_id "
           "FROM seller_transactions st JOIN users u ON st.seller_id=u.id "
           "LEFT JOIN orders o ON st.order_id=o.id ")
    params = []
    if status:
        sql += "WHERE st.status=%s "
        params.append(status)
    sql += "ORDER BY st.created_at DESC"
    txns = _exec(conn, sql, params).fetchall()
    return render_template("admin_payouts.html", txns=txns)


@app.route("/admin/payouts/mark-paid/<int:tid>", methods=["POST"])
@admin_required
def admin_payout_mark_paid(tid):
    check_csrf()
    conn = get_db()
    _exec(conn, "UPDATE seller_transactions SET status='paid' WHERE id=%s", (tid,))
    conn.commit()
    flash("Payout marked as paid.", "ok")
    return redirect("/admin/payouts")


# ═══════════════════════════════════════════════════════════════
# ── ADMIN: RESELLER MANAGEMENT
# ═══════════════════════════════════════════════════════════════

@app.route("/admin/resellers")
@admin_required
def admin_resellers():
    conn = get_db()
    resellers = _exec(conn, 
        """SELECT r.*, u.name, u.email, u.phone, u.is_reseller, u.reseller_status,
           COUNT(rc.id) as catalog_count,
           COALESCE(SUM(rc.margin),0) as total_margin
           FROM resellers r
           JOIN users u ON r.user_id=u.id
           LEFT JOIN reseller_catalogs rc ON rc.reseller_id=r.id
           GROUP BY r.id, u.id, u.name, u.email, u.phone, u.is_reseller, u.reseller_status ORDER BY r.created_at DESC"""
    ).fetchall()
    return render_template("admin_resellers.html", resellers=resellers)


@app.route("/admin/resellers/<int:uid>/suspend", methods=["POST"])
@admin_required
def admin_reseller_suspend(uid):
    check_csrf()
    conn = get_db()
    _exec(conn, "UPDATE users SET reseller_status='suspended' WHERE id=%s", (uid,))
    conn.commit()
    flash("Reseller suspended.", "ok")
    return redirect("/admin/resellers")


@app.route("/admin/resellers/<int:uid>/activate", methods=["POST"])
@admin_required
def admin_reseller_activate(uid):
    check_csrf()
    conn = get_db()
    _exec(conn, "UPDATE users SET reseller_status='active' WHERE id=%s", (uid,))
    conn.commit()
    flash("Reseller activated.", "ok")
    return redirect("/admin/resellers")


# ═══════════════════════════════════════════════════════════════
# ── PHASE 1: RESELLER SYSTEM
# ═══════════════════════════════════════════════════════════════

@app.route("/become-reseller", methods=["GET", "POST"])
@login_required
def become_reseller():
    if session.get("role") != "buyer":
        flash("Reseller sirf buyer account se ban sakte hain.", "err")
        return redirect("/")
    conn = get_db()
    already = _exec(conn, "SELECT id FROM resellers WHERE user_id=%s", (session["user_id"],)).fetchone()
    if already:
        flash("Aap pehle se reseller hain.", "ok")
        return redirect("/reseller/dashboard")
    if request.method == "POST":
        check_csrf()
        shop_name = request.form.get("shop_name", "").strip()
        bio       = request.form.get("bio", "").strip()
        if not shop_name:
            flash("Shop ka naam zaroori hai.", "err")
            return redirect("/become-reseller")
        _exec(conn, "INSERT INTO resellers (user_id,shop_name,bio) VALUES (%s,%s,%s)",
                     (session["user_id"], shop_name, bio))
        _exec(conn, "UPDATE users SET is_reseller=1, reseller_status='active' WHERE id=%s",
                     (session["user_id"],))
        conn.commit()
        flash("🎉 Congratulations! Aap ab ek reseller hain. Products add karo apne catalog mein.", "ok")
        return redirect("/reseller/dashboard")
    return render_template("reseller_join.html")


@app.route("/reseller/dashboard")
@login_required
def reseller_dashboard():
    conn = get_db()
    reseller = _exec(conn, "SELECT * FROM resellers WHERE user_id=%s", (session["user_id"],)).fetchone()
    if not reseller:
        return redirect("/become-reseller")
    catalog = _exec(conn, 
        "SELECT rc.*, p.title, p.image_url, p.price as base_price, p.mrp, p.stock, p.category, p.approved "
        "FROM reseller_catalogs rc JOIN products p ON rc.product_id=p.id "
        "WHERE rc.reseller_id=%s ORDER BY rc.created_at DESC",
        (reseller["id"],)
    ).fetchall()
    earnings = _scalar(_exec(conn, 
        "SELECT COALESCE(SUM(oi.qty * rc.margin),0) "
        "FROM orders o JOIN order_items oi ON oi.order_id=o.id "
        "JOIN reseller_catalogs rc ON rc.product_id=oi.product_id AND rc.reseller_id=%s "
        "WHERE o.reseller_id=%s AND o.status='delivered'",
        (reseller["id"], reseller["id"])
    ))
    return render_template("reseller_dashboard.html", reseller=reseller, catalog=catalog, earnings=earnings)


@app.route("/reseller/catalog/add/<int:pid>", methods=["POST"])
@login_required
def reseller_add_to_catalog(pid):
    check_csrf()
    conn = get_db()
    reseller = _exec(conn, "SELECT * FROM resellers WHERE user_id=%s", (session["user_id"],)).fetchone()
    if not reseller:
        return redirect("/become-reseller")
    margin = float(request.form.get("margin", 0))
    custom_title = request.form.get("custom_title", "").strip()
    try:
        _exec(conn, 
            "INSERT INTO reseller_catalogs (reseller_id,product_id,margin,custom_title) VALUES (%s,%s,%s,%s)",
            (reseller["id"], pid, margin, custom_title or None)
        )
        conn.commit()
        flash("Product apke catalog mein add ho gaya!", "ok")
    except (psycopg2.errors.UniqueViolation, psycopg2.IntegrityError) as e:
        logger.warning(f"Reseller catalog add failed: {e}")
        flash("Error adding product.", "err")
    return redirect(request.referrer or "/reseller/dashboard")


@app.route("/reseller/catalog/remove/<int:pid>", methods=["POST"])
@login_required
def reseller_remove_from_catalog(pid):
    check_csrf()
    conn = get_db()
    reseller = _exec(conn, "SELECT * FROM resellers WHERE user_id=%s", (session["user_id"],)).fetchone()
    if not reseller:
        abort(404)
    _exec(conn, "DELETE FROM reseller_catalogs WHERE reseller_id=%s AND product_id=%s",
                 (reseller["id"], pid))
    conn.commit()
    flash("Product catalog se remove ho gaya.", "ok")
    return redirect("/reseller/dashboard")


@app.route("/reseller/share/<int:cat_id>")
def reseller_share_product(cat_id):
    """Public shareable product page with reseller's margin baked in."""
    conn = get_db()
    rc = _exec(conn, 
        "SELECT rc.*, p.*, u.name as seller_name, r.shop_name, r.user_id as reseller_user_id "
        "FROM reseller_catalogs rc "
        "JOIN products p ON rc.product_id=p.id "
        "JOIN resellers r ON rc.reseller_id=r.id "
        "JOIN users u ON p.seller_id=u.id "
        "WHERE rc.id=%s AND p.approved=1 AND p.stock>0",
        (cat_id,)
    ).fetchone()
    if not rc:
        abort(404)
    images = _exec(conn, "SELECT * FROM product_images WHERE product_id=%s ORDER BY sort_order", (rc["product_id"],)).fetchall()
    reviews = _exec(conn, 
        "SELECT r.*, u.name as buyer_name FROM reviews r JOIN users u ON r.user_id=u.id WHERE r.product_id=%s ORDER BY r.created_at DESC LIMIT 5",
        (rc["product_id"],)
    ).fetchall()
    return render_template("reseller_product.html", rc=rc, images=images, reviews=reviews)


@app.route("/reseller/products/browse")
@login_required
def reseller_browse_products():
    conn = get_db()
    reseller = _exec(conn, "SELECT * FROM resellers WHERE user_id=%s", (session["user_id"],)).fetchone()
    if not reseller:
        return redirect("/become-reseller")
    q = request.args.get("q", "")
    category = request.args.get("category", "")
    sql = ("SELECT p.*, u.name as seller_name FROM products p JOIN users u ON p.seller_id=u.id "
           "WHERE p.approved=1 AND p.stock>0 AND COALESCE((SELECT gst_suspended FROM users WHERE id=p.seller_id),0)=0 ")
    params = []
    if q:
        sql += "AND (p.title LIKE %s OR p.category LIKE %s) "
        params += [f"%{q}%", f"%{q}%"]
    if category:
        sql += "AND p.category=%s "
        params.append(category)
    sql += "ORDER BY p.created_at DESC LIMIT 60"
    products = _exec(conn, sql, params).fetchall()
    in_catalog = {row["product_id"] for row in _exec(conn, 
        "SELECT product_id FROM reseller_catalogs WHERE reseller_id=%s", (reseller["id"],))}
    categories = [r["category"] for r in _exec(conn, 
        "SELECT DISTINCT category FROM products WHERE approved=1 ORDER BY category").fetchall()]
    return render_template("reseller_browse.html", products=products, in_catalog=in_catalog,
                           categories=categories, reseller=reseller)


# ═══════════════════════════════════════════════════════════════
# ── PHASE 2: PINCODE SERVICEABILITY + DELIVERY ESTIMATE
# ═══════════════════════════════════════════════════════════════

@app.route("/api/pincode-check")
def pincode_check():
    """Check if a pincode is serviceable. Returns JSON."""
    pincode = request.args.get("pincode", "").strip()
    if not pincode or len(pincode) != 6 or not pincode.isdigit():
        return jsonify({"ok": False, "msg": "Valid 6-digit pincode daalo."})
    conn = get_db()
    row = _exec(conn, "SELECT * FROM pincode_serviceability WHERE pincode=%s", (pincode,)).fetchone()
    if row:
        return jsonify({
            "ok": True,
            "serviceable": bool(row["serviceable"]),
            "cod": bool(row["cod_available"]),
            "days": row["delivery_days"],
            "city": row["city"],
            "state": row["state"],
            "msg": f"Delivery in {row['delivery_days']} days to {row['city'] or pincode}"
        })
    # Default: assume serviceable for unknown pincodes (India-wide assumption)
    return jsonify({
        "ok": True, "serviceable": True, "cod": True, "days": 5,
        "city": "", "state": "",
        "msg": "Delivery in 5-7 business days"
    })


# ═══════════════════════════════════════════════════════════════
# ── PHASE 2: BUY NOW (skip cart)
# ═══════════════════════════════════════════════════════════════

@app.route("/buy-now/<int:pid>", methods=["POST"])
@login_required
def buy_now(pid):
    """Add single item to a temporary buy-now session and redirect to checkout."""
    if session.get("role") != "buyer":
        abort(403)
    check_csrf()
    conn = get_db()
    p = _exec(conn, "SELECT * FROM products WHERE id=%s AND approved=1 AND stock>0", (pid,)).fetchone()
    if not p:
        flash("Product unavailable.", "err")
        return redirect(f"/p/{pid}")
    qty   = max(1, int(request.form.get("qty", 1)))
    size  = request.form.get("size", "")
    color = request.form.get("color", "")
    # Clear cart, add just this item
    _exec(conn, "DELETE FROM cart_items WHERE user_id=%s", (session["user_id"],))
    _exec(conn, "INSERT INTO cart_items (user_id,product_id,qty,size,color) VALUES (%s,%s,%s,%s,%s)",
                 (session["user_id"], pid, qty, size, color))
    conn.commit()
    session["buy_now"] = True
    return redirect("/checkout")


# ═══════════════════════════════════════════════════════════════
# ── PHASE 2: PRODUCT Q&A
# ═══════════════════════════════════════════════════════════════

@app.route("/p/<int:pid>/ask", methods=["POST"])
@login_required
def product_ask(pid):
    check_csrf()
    question = request.form.get("question", "").strip()
    if not question:
        flash("Question likhna zaroori hai.", "err")
        return redirect(f"/p/{pid}")
    conn = get_db()
    _exec(conn, "INSERT INTO product_qa (product_id,user_id,question) VALUES (%s,%s,%s)",
                 (pid, session["user_id"], question))
    conn.commit()
    # Notify seller
    p = _exec(conn, "SELECT seller_id FROM products WHERE id=%s", (pid,)).fetchone()
    if p:
        add_notification(p["seller_id"], "New Question", f"Product #{pid} pe ek naya question hai.", f"/p/{pid}")
    conn.commit()
    flash("Aapka question submit ho gaya. Seller reply karega.", "ok")
    return redirect(f"/p/{pid}")


@app.route("/p/<int:pid>/qa/answer/<int:qid>", methods=["POST"])
@seller_required
def product_answer(pid, qid):
    check_csrf()
    answer = request.form.get("answer", "").strip()
    if not answer:
        flash("Answer likhna zaroori hai.", "err")
        return redirect(f"/p/{pid}")
    conn = get_db()
    p = _exec(conn, "SELECT seller_id FROM products WHERE id=%s", (pid,)).fetchone()
    if not p or p["seller_id"] != session["user_id"]:
        abort(403)
    _exec(conn, "UPDATE product_qa SET answer=%s, answered_by=%s WHERE id=%s",
                 (answer, session["user_id"], qid))
    conn.commit()
    flash("Answer post ho gaya.", "ok")
    return redirect(f"/p/{pid}")


# ═══════════════════════════════════════════════════════════════
# ── PHASE 2: COD VERIFICATION (OTP)
# ═══════════════════════════════════════════════════════════════

@app.route("/order/verify-cod/<int:oid>", methods=["POST"])
@login_required
def verify_cod_otp(oid):
    """Mark COD order as verified (in real production, integrate SMS OTP)."""
    check_csrf()
    conn = get_db()
    order = _exec(conn, "SELECT * FROM orders WHERE id=%s AND buyer_id=%s",
                         (oid, session["user_id"])).fetchone()
    if not order:
        abort(404)
    otp = request.form.get("otp", "").strip()
    # FIX: compare against DB-stored OTP (not session — session is cookie-based and tamperable)
    # For now, check session but also validate length/digits to reduce abuse
    stored_otp = session.get(f"cod_otp_{oid}")
    if stored_otp and otp.isdigit() and len(otp) == 6 and otp == stored_otp:
        _exec(conn, "UPDATE orders SET cod_verified=1 WHERE id=%s", (oid,))
        conn.commit()
        session.pop(f"cod_otp_{oid}", None)
        flash("COD order verified!", "ok")
    else:
        flash("Invalid OTP.", "err")
    return redirect(f"/orders/{order['id']}")


# ═══════════════════════════════════════════════════════════════
# ── PHASE 3: ADMIN — BANNER & FLASH SALE MANAGEMENT
# ═══════════════════════════════════════════════════════════════

@app.route("/admin/banners", methods=["GET", "POST"])
@admin_required
def admin_banners():
    conn = get_db()
    if request.method == "POST":
        check_csrf()
        action = request.form.get("action")
        if action == "add":
            image_url = None
            img_file = request.files.get("banner_image")
            if img_file and img_file.filename:
                image_url = upload_to_cloudinary(img_file)
            product_id = request.form.get("product_id") or None
            cta_link = request.form.get("cta_link", "/search")
            if product_id:
                cta_link = f"/p/{product_id}"
            _exec(conn,
                "INSERT INTO banners (title,subtitle,cta_text,cta_link,bg_color,accent_color,sort_order,product_id,image_url) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (request.form.get("title",""), request.form.get("subtitle",""),
                 request.form.get("cta_text","Shop Now"), cta_link,
                 request.form.get("bg_color","#0f172a"), request.form.get("accent_color","#8B5CF6"),
                 int(request.form.get("sort_order",0) or 0),
                 product_id, image_url)
            )
            conn.commit()
            flash("Banner added.", "ok")
        elif action == "delete":
            bid = request.form.get("bid")
            _exec(conn, "DELETE FROM banners WHERE id=%s", (bid,))
            conn.commit()
            flash("Banner deleted.", "ok")
        elif action == "toggle":
            bid = request.form.get("bid")
            _exec(conn, "UPDATE banners SET active=1-active WHERE id=%s", (bid,))
            conn.commit()
        return redirect("/admin/banners")
    banners = _exec(conn, "SELECT * FROM banners ORDER BY sort_order").fetchall()
    products = _exec(conn, "SELECT id, title, image_url FROM products WHERE approved=1 ORDER BY title LIMIT 200").fetchall()
    return render_template("admin_banners.html", banners=banners, products=products)


@app.route("/admin/flash-sales", methods=["GET", "POST"])
@admin_required
def admin_flash_sales():
    conn = get_db()
    if request.method == "POST":
        check_csrf()
        action = request.form.get("action")
        if action == "add":
            _exec(conn, 
                "INSERT INTO flash_sales (title,subtitle,discount_pct,starts_at,ends_at,banner_color) VALUES (%s,%s,%s,%s,%s,%s)",
                (request.form.get("title",""), request.form.get("subtitle",""),
                 float(request.form.get("discount_pct",0)),
                 request.form.get("starts_at") or None,
                 request.form.get("ends_at") or None,
                 request.form.get("banner_color","#8B5CF6"))
            )
            conn.commit()
            flash("Flash sale created.", "ok")
        elif action == "delete":
            _exec(conn, "DELETE FROM flash_sales WHERE id=%s", (request.form.get("fid"),))
            conn.commit()
        elif action == "toggle":
            _exec(conn, "UPDATE flash_sales SET active=1-active WHERE id=%s", (request.form.get("fid"),))
            conn.commit()
        return redirect("/admin/flash-sales")
    sales = _exec(conn, "SELECT * FROM flash_sales ORDER BY created_at DESC").fetchall()
    return render_template("admin_flash_sales.html", sales=sales)


@app.route("/admin/trending", methods=["POST"])
@admin_required
def admin_toggle_trending():
    check_csrf()
    pid = request.form.get("pid")
    conn = get_db()
    _exec(conn, "UPDATE products SET trending=1-trending WHERE id=%s", (pid,))
    conn.commit()
    return redirect(request.referrer or "/admin/products")


@app.route("/admin/flash-tag", methods=["POST"])
@admin_required
def admin_toggle_flash_tag():
    check_csrf()
    pid   = request.form.get("pid")
    price = request.form.get("flash_price")
    conn  = get_db()
    p = _exec(conn, "SELECT is_flash_sale FROM products WHERE id=%s", (pid,)).fetchone()
    if p:
        new_state = 0 if p["is_flash_sale"] else 1
        _exec(conn, "UPDATE products SET is_flash_sale=%s, flash_sale_price=%s WHERE id=%s",
                     (new_state, price or None, pid))
        conn.commit()
    return redirect(request.referrer or "/admin/products")


# ═══════════════════════════════════════════════════════════════
# ── PHASE 3: RESELLER ORDER TRACKING & ATTRIBUTION
# ═══════════════════════════════════════════════════════════════

@app.route("/r/<int:cat_id>/checkout", methods=["POST"])
@login_required
def reseller_checkout_init(cat_id):
    """Add reseller product to cart with attribution."""
    if session.get("role") != "buyer":
        flash("Cart mein add karne ke liye buyer account chahiye.", "err")
        return redirect(f"/reseller/share/{cat_id}")
    check_csrf()
    conn = get_db()
    rc = _exec(conn, "SELECT * FROM reseller_catalogs WHERE id=%s", (cat_id,)).fetchone()
    if not rc:
        abort(404)
    _exec(conn, "INSERT INTO cart_items (user_id,product_id,qty,size,color) VALUES (%s,%s,%s,%s,%s)",
                 (session["user_id"], rc["product_id"], 1,
                  request.form.get("size",""), request.form.get("color","")))
    session[f"reseller_attr_{rc['product_id']}"] = rc["reseller_id"]
    conn.commit()
    flash("Cart mein add ho gaya!", "ok")
    return redirect("/cart")


# ═══════════════════════════════════════════════════════════════
# ── NEW FEATURES: AUTOCOMPLETE, VACATION, PAYOUT, SHOP, VIEWED
# ═══════════════════════════════════════════════════════════════

@app.route("/api/search-suggest")
def search_suggest():
    """Search autocomplete — returns JSON list of suggestions."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    conn = get_db()
    rows = _exec(conn, 
        "SELECT DISTINCT title FROM products WHERE approved=1 AND title LIKE %s LIMIT 8",
        (f"%{q}%",)
    ).fetchall()
    cats = _exec(conn, 
        "SELECT DISTINCT category FROM products WHERE approved=1 AND category LIKE %s LIMIT 4",
        (f"%{q}%",)
    ).fetchall()
    brands = _exec(conn, 
        "SELECT DISTINCT brand FROM products WHERE approved=1 AND brand LIKE %s AND brand IS NOT NULL LIMIT 4",
        (f"%{q}%",)
    ).fetchall()
    results = []
    for r in rows: results.append({"type": "product", "text": r["title"]})
    for c in cats: results.append({"type": "category", "text": c["category"]})
    for b in brands:
        if b["brand"]: results.append({"type": "brand", "text": b["brand"]})
    seen = set(); out = []
    for r in results:
        if r["text"] not in seen:
            seen.add(r["text"]); out.append(r)
    return jsonify(out[:10])


@app.route("/api/track-view/<int:pid>", methods=["POST"])
def track_view(pid):
    """Track recently viewed products in session."""
    viewed = session.get("recently_viewed", [])
    if pid in viewed:
        viewed.remove(pid)
    viewed.insert(0, pid)
    session["recently_viewed"] = viewed[:10]
    return jsonify({"ok": True})


@app.route("/api/push-subscribe", methods=["POST"])
@login_required
def push_subscribe():
    """Store push subscription (stub — real impl needs pywebpush)."""
    data = request.get_json() or {}
    conn = get_db()
    existing = {row['column_name'] for row in _exec(conn, "SELECT column_name FROM information_schema.columns WHERE table_name=%s AND table_schema='public'", ('users',)).fetchall()}
    if "push_subscription" not in existing:
        _exec(conn, "ALTER TABLE users ADD COLUMN push_subscription TEXT")
    _exec(conn, "UPDATE users SET push_subscription=%s WHERE id=%s",
                 (json.dumps(data), session["user_id"]))
    conn.commit()
    return jsonify({"ok": True})


@app.route("/seller/vacation", methods=["POST"])
@seller_required
def seller_vacation():
    """Toggle vacation mode — pauses new orders."""
    check_csrf()
    conn = get_db()
    existing = {row['column_name'] for row in _exec(conn, "SELECT column_name FROM information_schema.columns WHERE table_name=%s AND table_schema='public'", ('users',)).fetchall()}
    if "on_vacation" not in existing:
        _exec(conn, "ALTER TABLE users ADD COLUMN on_vacation INTEGER DEFAULT 0")
    cur = _exec(conn, "SELECT on_vacation FROM users WHERE id=%s", (session["user_id"],)).fetchone()
    new_state = 0 if (cur and cur["on_vacation"]) else 1
    _exec(conn, "UPDATE users SET on_vacation=%s WHERE id=%s", (new_state, session["user_id"]))
    conn.commit()
    if new_state:
        flash("🏖️ Vacation mode ON — nayi orders temporarily band hain.", "ok")
    else:
        flash("✅ Vacation mode OFF — aap ab orders le sakte hain.", "ok")
    return redirect("/seller/dashboard")


@app.route("/seller/payout-request", methods=["POST"])
@seller_required
def seller_payout_request():
    """Seller requests payout of earned balance."""
    check_csrf()
    conn = get_db()
    sid = session["user_id"]
    pending = _scalar(_exec(conn, 
        "SELECT COALESCE(SUM(net_amount),0) FROM seller_transactions WHERE seller_id=%s AND status='earned'",
        (sid,)
    ))
    if pending <= 0:
        flash("Koi pending earnings nahi hain.", "err")
        return redirect("/seller/wallet")
    seller = _exec(conn, "SELECT * FROM users WHERE id=%s", (sid,)).fetchone()
    if not seller["bank_account"] and not seller["upi_id"]:
        flash("Pehle bank account ya UPI ID profile mein add karo.", "err")
        return redirect("/seller/profile")
    _exec(conn, 
        "INSERT INTO seller_transactions (seller_id,type,amount,commission,net_amount,status,notes) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (sid, "payout_request", pending, 0, pending, "payout_requested",
         f"Payout request for Rs.{pending:.2f}")
    )
    conn.commit()
    add_notification(sid, "💰 Payout Request Sent!",
                     f"Rs.{pending:.2f} ka payout request admin ko bhej diya gaya. 3-5 working days mein process hoga.",
                     "/seller/wallet")
    flash(f"Payout request Rs.{pending:.2f} ke liye submit ho gaya!", "ok")
    return redirect("/seller/wallet")


@app.route("/shop/<int:seller_id>")
def seller_shop(seller_id):
    """Public seller shop page."""
    conn = get_db()
    seller = _exec(conn, 
        "SELECT * FROM users WHERE id=%s AND role='seller'", (seller_id,)
    ).fetchone()
    if not seller or seller["seller_status"] in ("suspended", "deleted"):
        abort(404)
    products = _exec(conn,
        "SELECT p.*, COALESCE(agg.avg_rating,0) avg_rating, COALESCE(agg.review_count,0) review_count "
        "FROM products p "
        "LEFT JOIN (SELECT product_id, AVG(rating) avg_rating, COUNT(id) review_count "
        "FROM reviews GROUP BY product_id) agg ON agg.product_id=p.id "
        "WHERE p.seller_id=%s AND p.approved=1 AND p.stock>0 "
        "ORDER BY p.created_at DESC",
        (seller_id,)
    ).fetchall()
    stats = {
        "products": len(products),
        "avg_rating": _scalar(_exec(conn, 
            "SELECT COALESCE(AVG(r.rating),0) FROM reviews r "
            "JOIN products p ON r.product_id=p.id WHERE p.seller_id=%s", (seller_id,)
        )),
        "total_sales": _scalar(_exec(conn, 
            "SELECT COUNT(DISTINCT oi.order_id) FROM order_items oi WHERE oi.seller_id=%s", (seller_id,)
        )),
    }
    return render_template("seller_shop.html", seller=seller, products=products, stats=stats)


# ─────────────────────────────────────────────────────────────
# ── ERROR HANDLERS
# ─────────────────────────────────────────────────────────────
@app.errorhandler(403)
def err_403(e):
    return render_template("login.html", error="Access denied."), 403

@app.errorhandler(404)
def err_404(e):
    return render_template("404.html"), 404

@app.errorhandler(413)
def err_413(e):
    flash("File size 10MB se zyada nahi ho sakti.", "err")
    return redirect(request.referrer or "/"), 413

@app.errorhandler(429)
def err_429(e):
    flash("Bahut zyada requests. Thodi der baad try karo.", "err")
    return redirect(request.referrer or "/login"), 429

@app.errorhandler(500)
def err_500(e):
    logger.error(f"500 error: {e}")
    return render_template("500.html", error=str(e)), 500


# ─────────────────────────────────────────────────────────────
# ── RUN
# ─────────────────────────────────────────────────────────────
with app.app_context():
    init_db()

# ── Background Daily GST Sync Thread ──────────────────────────
import threading, time as _time

def _daily_gst_sync():
    """Runs every 24 hours to check all seller GST statuses."""
    _time.sleep(30)  # wait for app to fully start
    while True:
        try:
            with app.app_context():
                conn = get_db()
                sellers = _exec(conn,
                    "SELECT id FROM users WHERE role='seller' AND gstin_verified=1 AND gstin IS NOT NULL"
                ).fetchall()
                for s in sellers:
                    sync_gst_status(conn, s["id"])
                    _time.sleep(1)  # rate limit — 1 seller per second
                app.logger.info(f"Daily GST sync done: {len(sellers)} sellers checked.")
        except Exception as e:
            app.logger.error(f"Daily GST sync error: {e}")
        _time.sleep(86400)  # sleep 24 hours

_gst_thread = threading.Thread(target=_daily_gst_sync, daemon=True)
_gst_thread.start()

if __name__ == "__main__":
    app.run(
    debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
    host="0.0.0.0",
    port=int(os.getenv("PORT", 5000))
)