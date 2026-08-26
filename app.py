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

import os, re, io, csv, uuid, json, hashlib, hmac, secrets, smtplib, logging
import threading, time as _time
from datetime import datetime, timezone
from functools import wraps
from email.mime.text import MIMEText

from flask import (Flask, g, session, request, redirect, url_for,
                   render_template, flash, abort, Response, jsonify)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from PIL import Image

try:
    from xhtml2pdf import pisa
    PISA_AVAILABLE = True
except ImportError:
    PISA_AVAILABLE = False
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
_env_secret = os.getenv("SECRET_KEY")
if not _env_secret:
    # WARNING: Yeh fallback har worker process mein ALAG random key banata hai —
    # isse users ka session baar-baar invalid ho jaata hai (random auto-logout).
    # Render dashboard > Environment mein SECRET_KEY zaroor set karo (fixed value).
    logging.getLogger(__name__).warning(
        "SECRET_KEY env var NOT SET! Falling back to a random per-process key — "
        "yeh production mein random auto-logout cause karega. Render dashboard mein SECRET_KEY set karo."
    )
    _env_secret = "change-me-in-production-" + secrets.token_hex(16)
app.secret_key = _env_secret

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
# Razorpay KYC abhi verify ho raha hai — jab tak Anjali/Nitish manually .env mein
# AUTO_PAYOUT_ENABLED=true na karein, koi bhi seller payout Razorpay se auto-transfer
# nahi hoga. Admin panel se hamesha manual "Mark as Paid" hi use hoga.
AUTO_PAYOUT_ENABLED = os.getenv("AUTO_PAYOUT_ENABLED", "false").lower() == "true"
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

# ── Session cookie hardening ────────────────────────────────────
# Flask's defaults (no Secure flag, SameSite=None-ish behavior) are not safe
# for production: a cookie without Secure can leak over plain HTTP, and
# without SameSite it's more exposed to CSRF. FLASK_DEBUG=true (local dev,
# usually plain http://localhost) skips the Secure flag so cookies still work
# without HTTPS locally.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("FLASK_DEBUG", "false").lower() != "true"

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

# ── Cross-app links (buyer <-> seller are separate deployments, same DB) ──
SELLER_APP_URL = os.getenv("SELLER_APP_URL", "https://xoptime-seller.onrender.com").rstrip("/")
BUYER_APP_URL  = os.getenv("BUYER_APP_URL",  "https://xoptime-buyer.onrender.com").rstrip("/")


# ─────────────────────────────────────────────────────────────
# Core helpers (shared with seller app — kept in sync manually)
# ─────────────────────────────────────────────────────────────

# ── Module-level constants that live between functions in the original
# monolith — the AST-based split only pulled out function bodies, so these
# got dropped. Restoring them here (order doesn't matter functionally,
# Python resolves module globals at call time, not def time).
SETTLEMENT_DAYS = int(os.getenv("SETTLEMENT_DAYS", "7"))   # default 7 din baad payout eligible

import urllib.request as _urllib_req
import urllib.error as _urllib_error
_shiprocket_token = None
_shiprocket_token_expiry = None

SHIPPING_SAME_STATE  = 75   # buyer & seller same state
SHIPPING_OTHER_STATE = 95   # buyer & seller different state
SHIPPING_DEFAULT     = 85   # buyer ka pincode nahi pata

_login_attempts = {}

WHATSAPP_BOT_URL          = os.getenv("WHATSAPP_BOT_URL", "http://localhost:4000")
WHATSAPP_BOT_INTERNAL_KEY = os.getenv("WHATSAPP_BOT_INTERNAL_KEY", "")

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

    CREATE TABLE IF NOT EXISTS phone_otp_store (
        phone TEXT PRIMARY KEY,
        otp TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL
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

    CREATE TABLE IF NOT EXISTS shiprocket_rate_cache (
        id SERIAL PRIMARY KEY,
        pickup_pincode TEXT NOT NULL,
        delivery_pincode TEXT NOT NULL,
        weight_key TEXT NOT NULL,
        rate REAL NOT NULL,
        fetched_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(pickup_pincode, delivery_pincode, weight_key)
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
        ('shipping_charge', 'REAL DEFAULT 0'),
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
                 ('gst_suspended', 'INTEGER DEFAULT 0'),
                 ('shiprocket_pickup_name', 'TEXT'),
                 ('city', 'TEXT')]
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

    # ── Migrate seller_transactions table — settlement automation columns
    st_cols = [('settlement_due_at', 'TIMESTAMPTZ'),
               ('settled_at', 'TIMESTAMPTZ'),
               ('payout_id', 'TEXT'),
               ('utr', 'TEXT'),
               ('payout_mode', 'TEXT'),
               ('payout_failure_reason', 'TEXT')]
    existing_st = {row['column_name'] for row in _exec(conn, "SELECT column_name FROM information_schema.columns WHERE table_name=%s AND table_schema='public'", ('seller_transactions',)).fetchall()}
    for col, col_type in st_cols:
        if col not in existing_st:
            _exec(conn, f"ALTER TABLE seller_transactions ADD COLUMN {col} {col_type}")

    # ── Migrate users table — Razorpay contact/fund account ids for auto payout
    user_extra_cols = [('rzp_contact_id', 'TEXT'), ('rzp_fund_account_id', 'TEXT'), ('landmark', 'TEXT')]
    existing_u2 = {row['column_name'] for row in _exec(conn, "SELECT column_name FROM information_schema.columns WHERE table_name=%s AND table_schema='public'", ('users',)).fetchall()}
    for col, col_type in user_extra_cols:
        if col not in existing_u2:
            _exec(conn, f"ALTER TABLE users ADD COLUMN {col} {col_type}")

    # Migrate orders table
    order_cols = [('reseller_id', 'INTEGER'), ('reseller_margin', 'REAL DEFAULT 0'),
                  ('cod_verified', 'INTEGER DEFAULT 0'), ('state', 'TEXT'), ('city', 'TEXT'),
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
                  ('refund_completed_at', 'TIMESTAMPTZ'),
                  ('shiprocket_shipment_id', 'TEXT'),
                  ('shiprocket_order_id', 'TEXT'),
                  ('shiprocket_cancel_synced', 'INTEGER DEFAULT 0'),
                  ('shiprocket_label_synced', 'INTEGER DEFAULT 0'),
                  ('shiprocket_awb_error', 'TEXT'),
                  ('landmark', 'TEXT'), ('address_type', 'TEXT'), ('alt_phone', 'TEXT')]
    existing_o = {row['column_name'] for row in _exec(conn, "SELECT column_name FROM information_schema.columns WHERE table_name=%s AND table_schema='public'", ('orders',)).fetchall()}
    for col, col_type in order_cols:
        if col not in existing_o:
            _exec(conn, f"ALTER TABLE orders ADD COLUMN {col} {col_type}")

    # ── Fix legacy capitalized status values (one-time historical cleanup) ──
    _exec(conn, "UPDATE orders SET status='delivered'     WHERE status IN ('Delivered')")
    _exec(conn, "UPDATE orders SET status='shipped'       WHERE status IN ('Shipped')")
    _exec(conn, "UPDATE orders SET status='processing'    WHERE status IN ('Processing','Accepted','accepted')")
    _exec(conn, "UPDATE orders SET status='placed'        WHERE status IN ('Pending','pending')")
    # NOTE: 'ReadyToShip' -> 'shipped' auto-rewrite REMOVED (2026-07-02).
    # This used to run on every app startup/restart and was silently flipping
    # live ReadyToShip orders to 'shipped', which then failed to match the
    # capitalized 'Shipped' tab filter (case-sensitive) — orders vanished
    # from every tab in the seller UI. ReadyToShip is a valid, distinct status
    # and must not be auto-rewritten.
    # One-time cleanup (2026-07-03): the Shiprocket webhook's "Pickup Scheduled"
    # mapping was writing lowercase 'ready_to_ship' instead of 'ReadyToShip',
    # which caused the exact same disappearing-order bug via a different path.
    # Repair any orders already stuck in that state.
    _exec(conn, "UPDATE orders SET status='ReadyToShip' WHERE status='ready_to_ship'")
    # One-time correction (2026-07-03): order ORD-46BEA78A's courier was
    # reassigned on Shiprocket's dashboard (Shadowfax -> Rocket Express) but
    # Xoptime never learned about it due to the webhook/awb-lookup bug above.
    # Patch it directly from the confirmed Shiprocket manifest.
    _exec(conn, "UPDATE orders SET awb='1319460358444', courier_name='Rocket Express', updated_at=NOW() "
                "WHERE public_id='ORD-46BEA78A' AND awb='SF3395828631KR'")

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
                ("Fashion Sale — Up to 80% Off", "India ke top sellers se direct kharido", "Shop Now", "/search?category=Fashion", "#0f172a", "#8B5CF6", 0),
                ("New Arrivals Every Day", "Trending products, lowest prices", "Explore", "/search", "#0c1a0c", "#22c55e", 1),
                ("Sell on Xoptime", "Apna business shuru karo — free mein", "Become a Seller", "/register", "#1a0a00", "#f59e0b", 2),
            ])
        conn.commit()

    # Create default admin if not exists.
    # NOTE: this used to hardcode the password as "admin123" for every single
    # deploy of this app — since the source is not secret, that's effectively
    # a public admin password on any fresh install. We now generate a random
    # one-time password (or let the operator pin it via ADMIN_DEFAULT_PASSWORD)
    # and only ever print it once, at creation time.
    admin = _exec(conn, "SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
    if not admin:
        default_password = os.getenv("ADMIN_DEFAULT_PASSWORD") or secrets.token_urlsafe(12)
        _exec(conn, 
            "INSERT INTO users (name,email,password,role) VALUES (%s,%s,%s,%s)",
            ("Admin", "admin@xoptime.com",
             generate_password_hash(default_password), "admin")
        )
        conn.commit()
        print(f"Default admin created → email: admin@xoptime.com | password: {default_password}")
        print("⚠️  Save this password now — log in and change it immediately. It will not be shown again.")

def _rzp_ensure_contact_and_fund(conn, seller):
    """Razorpay Contact + Fund Account create/fetch karo seller ke liye (idempotent)."""
    import requests as _req
    rzp_auth = (RZP_KEY_ID, RZP_KEY_SECRET)

    contact_id = seller.get("rzp_contact_id")
    if not contact_id:
        # Create contact
        resp = _req.post(
            "https://api.razorpay.com/v1/contacts",
            auth=rzp_auth,
            json={
                "name":    seller["name"],
                "email":   seller.get("email") or f"seller{seller['id']}@xoptime.com",
                "contact": seller.get("phone") or "9999999999",
                "type":    "vendor",
                "reference_id": f"seller_{seller['id']}",
            }
        )
        data = resp.json()
        contact_id = data.get("id")
        if contact_id:
            _exec(conn, "UPDATE users SET rzp_contact_id=%s WHERE id=%s", (contact_id, seller["id"]))
            conn.commit()
        else:
            raise Exception(f"Razorpay contact create failed: {data}")

    fund_account_id = seller.get("rzp_fund_account_id")
    if not fund_account_id:
        # Create fund account — bank account preferred, fallback UPI
        if seller.get("bank_account") and seller.get("bank_ifsc"):
            fa_payload = {
                "contact_id":    contact_id,
                "account_type":  "bank_account",
                "bank_account":  {
                    "name":           seller["bank_name"] or seller["name"],
                    "ifsc":           seller["bank_ifsc"],
                    "account_number": seller["bank_account"],
                },
            }
        elif seller.get("upi_id"):
            fa_payload = {
                "contact_id":   contact_id,
                "account_type": "vpa",
                "vpa":          {"address": seller["upi_id"]},
            }
        else:
            raise Exception("Seller ke paas bank/UPI details nahi hain")

        resp = _req.post(
            "https://api.razorpay.com/v1/fund_accounts",
            auth=rzp_auth,
            json=fa_payload
        )
        data = resp.json()
        fund_account_id = data.get("id")
        if fund_account_id:
            _exec(conn, "UPDATE users SET rzp_fund_account_id=%s WHERE id=%s", (fund_account_id, seller["id"]))
            conn.commit()
        else:
            raise Exception(f"Razorpay fund account create failed: {data}")

    return contact_id, fund_account_id

def _rzp_payout(conn, txn, seller):
    """Single seller_transaction ke liye Razorpay Payout initiate karo."""
    import requests as _req

    contact_id, fund_account_id = _rzp_ensure_contact_and_fund(conn, seller)

    amount_paise = int(round(txn["net_amount"] * 100))  # Razorpay paise mein leta hai
    payout_mode  = "NEFT" if seller.get("bank_account") else "UPI"

    rzp_auth = (RZP_KEY_ID, RZP_KEY_SECRET)
    resp = _req.post(
        "https://api.razorpay.com/v1/payouts",
        auth=rzp_auth,
        json={
            "account_number":  os.getenv("RAZORPAY_ACCOUNT_NUMBER", ""),  # Razorpay dashboard ka account no
            "fund_account_id": fund_account_id,
            "amount":          amount_paise,
            "currency":        "INR",
            "mode":            payout_mode,
            "purpose":         "payout",
            "queue_if_low_balance": True,
            "narration":       f"Xoptime settlement txn#{txn['id']}",
            "reference_id":    f"xoptime_txn_{txn['id']}",
        }
    )
    data = resp.json()
    payout_id = data.get("id")
    utr        = data.get("utr") or ""
    status     = data.get("status", "")

    if payout_id:
        _exec(conn,
            """UPDATE seller_transactions
               SET status='paid', settled_at=NOW(), payout_id=%s, utr=%s, payout_mode=%s
               WHERE id=%s""",
            (payout_id, utr, payout_mode, txn["id"])
        )
        conn.commit()
        app.logger.info(f"[PAYOUT] txn#{txn['id']} seller#{seller['id']} ₹{txn['net_amount']} → {payout_id} ({status})")
        return True, payout_id
    else:
        err = data.get("error", {}).get("description", str(data))
        _exec(conn,
            "UPDATE seller_transactions SET payout_failure_reason=%s WHERE id=%s",
            (err, txn["id"])
        )
        conn.commit()
        app.logger.error(f"[PAYOUT FAIL] txn#{txn['id']} seller#{seller['id']}: {err}")
        return False, err

def run_settlement_cycle():
    """
    Meesho-style auto settlement:
    1. Har delivered order ke liye settlement_due_at = delivered_at + 7 din set karo
    2. Due ho gaye transactions ko Razorpay se auto-pay karo (agar Razorpay configured hai)
       Ya fallback mein sirf status='settlement_due' kar do admin ke liye
    """
    with app.app_context():
        conn = get_db()
        try:
            # ── Step 1: Set settlement_due_at for newly earned transactions ──
            _exec(conn, """
                UPDATE seller_transactions st
                SET settlement_due_at = (
                    SELECT o.delivered_at + INTERVAL '""" + str(SETTLEMENT_DAYS) + """ days'
                    FROM orders o WHERE o.id = st.order_id
                )
                WHERE st.status = 'earned'
                  AND st.settlement_due_at IS NULL
                  AND st.order_id IS NOT NULL
            """)
            conn.commit()

            # ── Step 2: Find transactions whose due date has passed ──
            due_txns = _exec(conn, """
                SELECT st.*,
                       u.name AS seller_name, u.bank_account, u.bank_ifsc,
                       u.bank_name, u.upi_id, u.email, u.phone,
                       u.rzp_contact_id, u.rzp_fund_account_id
                FROM seller_transactions st
                JOIN users u ON st.seller_id = u.id
                WHERE st.status = 'earned'
                  AND st.settlement_due_at IS NOT NULL
                  AND st.settlement_due_at <= NOW()
            """).fetchall()

            if not due_txns:
                return

            app.logger.info(f"[SETTLEMENT] {len(due_txns)} transactions due for payout")

            use_razorpay = (
                AUTO_PAYOUT_ENABLED
                and RAZORPAY_AVAILABLE
                and RZP_KEY_ID
                and os.getenv("RAZORPAY_ACCOUNT_NUMBER", "")
                and "live" in RZP_KEY_ID  # Sirf live mode mein auto-pay
            )

            for txn in due_txns:
                try:
                    if use_razorpay and (txn["bank_account"] or txn["upi_id"]):
                        # Auto Razorpay payout
                        _rzp_payout(conn, txn, txn)
                    else:
                        # Fallback: mark as settlement_due — admin manually pay karega
                        _exec(conn,
                            "UPDATE seller_transactions SET status='settlement_due' WHERE id=%s",
                            (txn["id"],)
                        )
                        conn.commit()
                        # Admin ko notification
                        _exec(conn,
                            """INSERT INTO notifications (user_id, message, type)
                               SELECT id, %s, 'system' FROM users WHERE role='admin' LIMIT 1""",
                            (f"Settlement due: {txn['seller_name']} ko ₹{txn['net_amount']:.2f} bhejni hai (txn#{txn['id']})",)
                        )
                        conn.commit()
                except Exception as e:
                    app.logger.error(f"[SETTLEMENT ERROR] txn#{txn['id']}: {e}")

        except Exception as e:
            app.logger.error(f"[SETTLEMENT CYCLE ERROR]: {e}")

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

def upload_to_cloudinary(file_obj):
    """Thin wrapper around save_image() that returns just the URL.
    (Bug fix: this was being called at 2 places but was never defined —
    would crash with NameError on product-review image upload and on
    admin banner image upload.)"""
    url, _thumb = save_image(file_obj)
    return url

def add_notification(user_id, title, message, link=None):
    conn = get_db()
    _exec(conn,
        "INSERT INTO notifications (user_id,title,message,link) VALUES (%s,%s,%s,%s)",
        (user_id, title, message, link)
    )
    conn.commit()

def _send_email_worker(to, subject, body):
    """Internal: actually sends email. Run in background thread only."""
    host = os.getenv("SMTP_HOST")
    if not host:
        return
    try:
        msg = MIMEText(body, "html")
        msg["Subject"] = subject
        msg["From"]    = os.getenv("SMTP_USER", "no-reply@xoptime.com")
        msg["To"]      = to
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", 587))) as s:
            s.starttls()
            s.login(os.getenv("SMTP_USER",""), os.getenv("SMTP_PASS",""))
            s.send_message(msg)
    except Exception as e:
        logger.warning(f"Email send failed to {to}: {e}")

def send_email(to, subject, body):
    """Send email in background thread so it never blocks the request."""
    import threading
    threading.Thread(target=_send_email_worker, args=(to, subject, body), daemon=True).start()

def shiprocket_register_pickup_address(seller):
    """
    Seller ka address Shiprocket pe pickup location ke roop mein register karo.
    Returns pickup_name (string) ya None on failure.
    """
    import json as _json
    token = shiprocket_get_token()
    if not token:
        return None

    seller_id   = seller["id"]
    pickup_name = f"SELLER_{seller_id}"
    name        = seller.get("name") or seller.get("full_name") or f"Seller {seller_id}"
    address     = seller.get("address") or ""
    landmark    = seller.get("landmark") or ""
    pincode     = str(seller.get("pincode") or "110001").strip()
    city        = seller.get("city") or "Delhi"
    state       = seller.get("state") or "Delhi"
    phone       = str(seller.get("phone") or "9999999999").strip()
    email       = seller.get("email") or f"seller{seller_id}@xoptime.com"

    payload = _json.dumps({
        "pickup_location": pickup_name,
        "name":            name,
        "email":           email,
        "phone":           phone,
        "address":         address,
        "address_2":       landmark,
        "city":            city,
        "state":           state,
        "country":         "India",
        "pin_code":        pincode,
    }).encode()

    try:
        req = _urllib_req.Request(
            "https://apiv2.shiprocket.in/v1/external/settings/company/addpickup",
            data=payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST"
        )
        with _urllib_req.urlopen(req, timeout=15) as resp:
            result = _json.loads(resp.read())

        # Success ya already exists dono cases mein pickup_name return karo
        if result.get("success") or result.get("data") or "already" in str(result).lower():
            logger.info(f"[Shiprocket] Pickup registered for seller {seller_id}: {pickup_name}")
            return pickup_name
        else:
            logger.warning(f"[Shiprocket] Pickup register failed for seller {seller_id}: {result}")
            return None
    except _urllib_error.HTTPError as e:
        try:
            body = e.read().decode()
        except Exception:
            body = "<no body>"
        logger.error(f"[Shiprocket] Pickup register error for seller {seller_id}: {e} | body={body}")
        return None
    except Exception as e:
        logger.error(f"[Shiprocket] Pickup register error for seller {seller_id}: {e}")
        return None

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

# ── Live Shiprocket delivery-rate lookup ────────────────────────
# ₹10 is quietly folded into whatever Shiprocket returns — never shown to the
# buyer as a separate line item, just baked into the final delivery charge.
SHIPROCKET_RATE_MARKUP = 10
SHIPROCKET_RATE_CACHE_HOURS = 24
import threading as _threading

def _shiprocket_weight_key(weight_kg, cod):
    return f"{weight_kg:.1f}_{1 if cod else 0}"

def _shiprocket_rate_cache_get(conn, pickup, delivery, weight_key):
    """DB se cached rate uthao agar 24hr ke andar fetch hui thi."""
    try:
        row = _exec(conn,
            """SELECT rate, fetched_at FROM shiprocket_rate_cache
               WHERE pickup_pincode=%s AND delivery_pincode=%s AND weight_key=%s""",
            (pickup, delivery, weight_key)).fetchone()
        if not row:
            return None
        fetched_at = row["fetched_at"]
        now = datetime.now(fetched_at.tzinfo) if fetched_at.tzinfo else datetime.now()
        if (now - fetched_at).total_seconds() > SHIPROCKET_RATE_CACHE_HOURS * 3600:
            return None
        return row["rate"]
    except Exception as e:
        logger.error(f"[Shiprocket rate] cache read failed: {e}")
        return None

def _shiprocket_rate_cache_set(pickup, delivery, weight_key, rate):
    """Apni khud ki connection se save karo — background thread se bhi safely
    call ho sake, request ka `conn` object thread-safe nahi hota."""
    try:
        _conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        try:
            _exec(_conn,
                """INSERT INTO shiprocket_rate_cache
                       (pickup_pincode, delivery_pincode, weight_key, rate, fetched_at)
                   VALUES (%s,%s,%s,%s,NOW())
                   ON CONFLICT (pickup_pincode, delivery_pincode, weight_key)
                   DO UPDATE SET rate=EXCLUDED.rate, fetched_at=NOW()""",
                (pickup, delivery, weight_key, rate))
            _conn.commit()
        finally:
            _conn.close()
    except Exception as e:
        logger.error(f"[Shiprocket rate] cache save failed: {e}")

def _shiprocket_fetch_live_rate(pickup, delivery, weight_kg, cod):
    """Actual Shiprocket serviceability API call — sabse sasta available
    courier ka rate uthao aur ₹10 quietly add karke return karo. Fail hone
    par None (koi exception bahar nahi jaane deta)."""
    token = shiprocket_get_token()
    if not token:
        return None
    try:
        import json as _json
        from urllib.parse import urlencode
        params = {
            "pickup_postcode": pickup,
            "delivery_postcode": delivery,
            "weight": weight_kg,
            "cod": 1 if cod else 0,
        }
        url = "https://apiv2.shiprocket.in/v1/external/courier/serviceability/?" + urlencode(params)
        req = _urllib_req.Request(url, headers={"Authorization": f"Bearer {token}"}, method="GET")
        with _urllib_req.urlopen(req, timeout=6) as resp:
            result = _json.loads(resp.read())
        couriers = (((result or {}).get("data") or {}).get("available_courier_companies") or [])
        rates = [float(c["rate"]) for c in couriers if c.get("rate") is not None]
        if not rates:
            return None
        return round(min(rates) + SHIPROCKET_RATE_MARKUP)
    except Exception as e:
        logger.error(f"[Shiprocket rate] live fetch failed ({pickup}->{delivery}): {e}")
        return None

def _shiprocket_warm_cache_async(pickup, delivery, weight_key, weight_kg, cod):
    """Cache-miss hua par turant rate nahi chahiye (jaise homepage listing) —
    background thread mein live rate fetch karke cache bhar do, taaki agli
    baar wahi combo instant serve ho. Page load isse kabhi slow nahi hota."""
    def _job():
        rate = _shiprocket_fetch_live_rate(pickup, delivery, weight_kg, cod)
        if rate is not None:
            _shiprocket_rate_cache_set(pickup, delivery, weight_key, rate)
    _threading.Thread(target=_job, daemon=True).start()

def shiprocket_get_rate(pickup_pincode, delivery_pincode, weight_kg=0.5, cod=False, live=False):
    """
    Delivery charge nikaalo Shiprocket ke live courier-serviceability rate se
    (cheapest available courier + ₹10 chhupa hua markup), 24hr DB-cached.

    live=False (homepage/listing/product page): cache-hit -> turant return,
        cache-miss -> background mein fetch shuru karke None return (caller
        turant flat state-wise rate pe fallback karega, page kabhi slow nahi
        hoga, agli baar cache garam mil jayega).
    live=True (cart/checkout — jahan accuracy sabse zyada matter karti hai):
        cache-hit -> turant return, cache-miss -> blocking live call (short
        timeout), result cache mein save karke return. Fail hua to None
        (caller flat rate pe fallback karega).

    Har jagah None ka matlab hai: "Shiprocket rate abhi available nahi,
    purana flat logic use karo" — site kabhi break nahi hoti.
    """
    if not pickup_pincode or not delivery_pincode:
        return None
    pickup_pincode   = str(pickup_pincode).strip()
    delivery_pincode = str(delivery_pincode).strip()
    if not (pickup_pincode.isdigit() and len(pickup_pincode) == 6):
        return None
    if not (delivery_pincode.isdigit() and len(delivery_pincode) == 6):
        return None

    # 100g granularity pe round karo taaki chhoti weight variation se bhi
    # cache reuse ho sake aur combos bahut zyada na ban jaayein.
    weight_kg = max(0.1, round(float(weight_kg or 0.5), 1))
    weight_key = _shiprocket_weight_key(weight_kg, cod)

    try:
        conn = get_db()
        cached = _shiprocket_rate_cache_get(conn, pickup_pincode, delivery_pincode, weight_key)
    except Exception:
        cached = None
    if cached is not None:
        return cached

    if not live:
        _shiprocket_warm_cache_async(pickup_pincode, delivery_pincode, weight_key, weight_kg, cod)
        return None

    rate = _shiprocket_fetch_live_rate(pickup_pincode, delivery_pincode, weight_kg, cod)
    if rate is not None:
        _shiprocket_rate_cache_set(pickup_pincode, delivery_pincode, weight_key, rate)
    return rate

def shiprocket_create_order(order, items, seller):
    """
    Create a shipment order on Shiprocket and request pickup.
    Returns dict with: success, awb, shipment_id, courier_name, error
    """
    import json as _json

    token = shiprocket_get_token()
    if not token:
        return {"success": False, "error": "Shiprocket credentials not configured in .env"}

    # Seller ka dynamic pickup location use karo
    # Agar registered nahi hai toh abhi register karo
    pickup_name = None
    if seller:
        pickup_name = seller.get("shiprocket_pickup_name")
        if not pickup_name:
            # Pehli baar dispatch — abhi register karo
            pickup_name = shiprocket_register_pickup_address(seller)
            if pickup_name:
                try:
                    conn = get_db()
                    _exec(conn, "UPDATE users SET shiprocket_pickup_name=%s WHERE id=%s",
                          (pickup_name, seller["id"]))
                    conn.commit()
                except Exception:
                    pass
    if not pickup_name:
        pickup_name = SHIPROCKET_PICKUP  # fallback to env var

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
        "order_date":        (order["created_at"].strftime("%Y-%m-%d")
                               if hasattr(order["created_at"], "strftime")
                               else str(order["created_at"])[:10]),
        "pickup_location":   pickup_name,
        "channel_id":        SHIPROCKET_CHANNEL or "",
        "comment":           "Xoptime Order",
        "billing_customer_name":  order["buyer_name"],
        "billing_last_name":      "",
        "billing_address":        order["address"],
        "billing_address_2":      "",
        "billing_city":           (order.get("city") or "").strip() or "Delhi",
        "billing_pincode":        order["pincode"] or "110001",
        "billing_state":          (order.get("state") or "").strip() or "Delhi",
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
        sr_order_id = result.get("order_id")
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

        # ── AWB assign fail hone ka asli reason nikalo ──
        # Order-create call succeed ho sakta hai, par uske baad wala
        # assign/awb call fail ho sakta hai (KYC pending, wallet balance zero,
        # koi courier serviceable nahi, etc). Pehle wapas success:True
        # bhej dete the yahan par empty awb ke saath, isliye seller ko sirf
        # generic ".env check karo" wala message dikhta tha, asli wajah kabhi
        # nahi. Ab Shiprocket ke raw response se asli error nikaal ke bhejte hain.
        assign_error = None
        if not awb:
            assign_error = (
                assign_result.get("message")
                or assign_result.get("response", {}).get("data", {}).get("awb_assign_error")
                or assign_result.get("response", {}).get("message")
                or _json.dumps(assign_result)[:500]
            )
            logger.error(f"[Shiprocket] AWB assign failed for shipment {shipment_id}: {assign_error}")

        # Schedule pickup (sirf tab jab AWB mil chuka ho — warna pickup call
        # bhi fail hoga aur bekaar mein ek aur API hit hogi)
        pickup_status = 0
        if awb:
            try:
                pickup_payload = _json.dumps({"shipment_id": [int(shipment_id)]}).encode()
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
            except Exception as e:
                logger.error(f"[Shiprocket] pickup schedule failed for shipment {shipment_id}: {e}")

        return {
            "success":      bool(awb),
            "shipment_id":  shipment_id,
            "order_id":     sr_order_id,
            "awb":          awb,
            "courier_name": courier_name,
            "pickup_scheduled": pickup_status == 1,
            "error":        assign_error,
        }

    except _urllib_error.HTTPError as e:
        try:
            body = e.read().decode()
        except Exception:
            body = "<no body>"
        logger.error(f"[Shiprocket] create_order HTTPError: {e} | body={body}")
        return {"success": False, "error": f"{e} | {body}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def shiprocket_cancel_order(sr_order_id):
    """
    Cancels an order on Shiprocket's side (POST /v1/external/orders/cancel).
    Only works before the shipment has actually been picked up/shipped by
    the courier — Shiprocket will return an error/skip-list in that case,
    which we surface back to the caller so they can handle it (e.g. tell
    the seller to intercept the shipment manually).
    """
    import json as _json
    token = shiprocket_get_token()
    if not token or not sr_order_id:
        return {"success": False, "error": "Missing token or shiprocket_order_id"}
    try:
        payload = _json.dumps({"ids": [int(sr_order_id)]}).encode()
        req = _urllib_req.Request(
            "https://apiv2.shiprocket.in/v1/external/orders/cancel",
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST"
        )
        with _urllib_req.urlopen(req, timeout=15) as resp:
            result = _json.loads(resp.read())
        # Shiprocket returns {"message": "...", "status_code": 200} on success.
        ok = result.get("status_code") in (200, 1) or "cancel" in str(result.get("message", "")).lower()
        return {"success": ok, "message": result.get("message", ""), "raw": result}
    except _urllib_error.HTTPError as e:
        try:
            body = e.read().decode()
        except Exception:
            body = "<no body>"
        logger.error(f"[Shiprocket] cancel_order HTTPError for order {sr_order_id}: {e} | body={body}")
        return {"success": False, "error": f"{e} | {body}"}
    except Exception as e:
        logger.warning(f"[Shiprocket] cancel_order failed for order {sr_order_id}: {e}")
        return {"success": False, "error": str(e)}

def shiprocket_generate_label(shipment_id):
    """
    Calls Shiprocket's own label-generation endpoint. We don't use the PDF
    this returns (Xoptime shows its own branded label), but making this call
    is what flips Shiprocket's internal 'Label Downloaded' flag on their
    dashboard, so both sides stay in sync.

    NOTE: Shiprocket's API expects shipment_id as an INTEGER inside the array
    (e.g. {"shipment_id": [121221]}). We were previously sending it as a
    STRING (["121221"]), which Shiprocket silently rejects — the call
    "succeeds" at the HTTP level but returns no label_url, so our success
    check always failed and shiprocket_label_synced never got set to 1.
    That's why the seller's dashboard never showed "Label Downloaded" on
    Shiprocket's side even though the seller had already downloaded it here.
    """
    import json as _json
    token = shiprocket_get_token()
    if not token or not shipment_id:
        return {"success": False, "error": "Missing token or shipment_id"}
    try:
        shipment_id_int = int(shipment_id)
    except (TypeError, ValueError):
        return {"success": False, "error": f"shipment_id is not numeric: {shipment_id!r}"}
    try:
        payload = _json.dumps({"shipment_id": [shipment_id_int]}).encode()
        req = _urllib_req.Request(
            "https://apiv2.shiprocket.in/v1/external/courier/generate/label",
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST"
        )
        with _urllib_req.urlopen(req, timeout=15) as resp:
            result = _json.loads(resp.read())
        ok = bool(result.get("label_url"))
        if not ok:
            logger.warning(f"[Shiprocket] generate_label returned no label_url for shipment {shipment_id_int}: {result}")
        return {"success": ok, "label_url": result.get("label_url", ""), "raw": result}
    except _urllib_error.HTTPError as e:
        try:
            body = e.read().decode()
        except Exception:
            body = "<no body>"
        logger.error(f"[Shiprocket] generate_label HTTPError for shipment {shipment_id_int}: {e} | body={body}")
        return {"success": False, "error": f"{e} | {body}"}
    except Exception as e:
        logger.warning(f"[Shiprocket] generate_label failed for shipment {shipment_id}: {e}")
        return {"success": False, "error": str(e)}

def shiprocket_get_shipment_status(shipment_id):
    """
    Fetch the CURRENT awb/courier for a shipment from Shiprocket.
    Used to re-sync Xoptime's copy after a seller reassigns the courier
    directly on the Shiprocket dashboard (which doesn't always reach us
    via webhook, since our webhook lookup keys off the OLD awb).
    """
    import json as _json
    token = shiprocket_get_token()
    if not token or not shipment_id:
        return {"success": False, "error": "Missing token or shipment_id"}
    try:
        req = _urllib_req.Request(
            f"https://apiv2.shiprocket.in/v1/external/courier/track/shipment/{shipment_id}",
            headers={"Authorization": f"Bearer {token}"},
            method="GET"
        )
        with _urllib_req.urlopen(req, timeout=15) as resp:
            result = _json.loads(resp.read())
        # Response keyed by shipment_id, containing shipment_track list
        bucket = result.get(str(shipment_id)) or next(iter(result.values()), {})
        track_list = bucket.get("shipment_track") or []
        if not track_list:
            return {"success": False, "error": "No tracking data yet"}
        latest = track_list[0]
        return {
            "success":      True,
            "awb":          latest.get("awb_code", ""),
            "courier_name": latest.get("courier_name", ""),
            "current_status": latest.get("current_status", ""),
        }
    except _urllib_error.HTTPError as e:
        try:
            body = e.read().decode()
        except Exception:
            body = "<no body>"
        logger.error(f"[Shiprocket] get_shipment_status HTTPError: {e} | body={body}")
        return {"success": False, "error": f"{e} | {body}"}
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

def get_buyer_state():
    """Session se logged-in buyer ki state laao."""
    uid = session.get("user_id")
    if not uid or session.get("role") != "buyer":
        return None
    try:
        conn = get_db()
        row = _exec(conn, "SELECT state FROM users WHERE id=%s", (uid,)).fetchone()
        return row["state"] if row else None
    except Exception:
        return None

def get_buyer_pincode():
    """Session se logged-in buyer ka pincode laao — live Shiprocket rate ke liye."""
    uid = session.get("user_id")
    if not uid or session.get("role") != "buyer":
        return None
    try:
        conn = get_db()
        row = _exec(conn, "SELECT pincode FROM users WHERE id=%s", (uid,)).fetchone()
        return row["pincode"] if row else None
    except Exception:
        return None

def _flat_shipping_rate(buyer_state, seller_state):
    """Purana state-wise flat logic — Shiprocket live rate na mile (cache-miss
    ya API down) tab silently fallback ke roop mein use hota hai."""
    if buyer_state and seller_state:
        if buyer_state.strip().lower() == seller_state.strip().lower():
            return SHIPPING_SAME_STATE
        return SHIPPING_OTHER_STATE
    return SHIPPING_DEFAULT

def _resolve_shipping_charge(seller_state, seller_pincode, buyer_state, buyer_pincode,
                              weight_grams=None, live=False):
    """Ek hi jagah se delivery charge nikaalo: pehle live Shiprocket rate
    (cached ya live) try karo, na mile to purana flat state-wise logic pe
    chup-chaap fallback ho jao. ₹10 markup Shiprocket rate ke andar hi hai —
    yahan se alag kabhi nahi dikhaya jaata."""
    weight_kg = max(0.1, (weight_grams or 500) / 1000.0)
    rate = None
    if seller_pincode and buyer_pincode:
        rate = shiprocket_get_rate(seller_pincode, buyer_pincode, weight_kg, cod=False, live=live)
    if rate is not None:
        return rate
    return _flat_shipping_rate(buyer_state, seller_state)

def bake_shipping_into_price(rows, buyer_state=None, buyer_pincode=None, live=False):
    """
    Delivery charge product price mein add karo — live Shiprocket rate
    (seller pickup pincode + buyer pincode + product weight ke hisaab se,
    ₹10 chhupa hua markup ke saath) use karta hai jab available ho, warna
    purana state-wise flat logic (₹75/₹95/₹85) silently fallback hota hai.
    Seller ka state/pincode product ke saath aana chahiye as 'seller_state'
    / 'seller_pincode'.
    """
    if rows is None:
        return rows
    single = isinstance(rows, dict)
    row_list = [rows] if single else rows
    for r in row_list:
        try:
            seller_state   = r.get("seller_state") or r.get("state")
            seller_pincode = r.get("seller_pincode")
            ship = _resolve_shipping_charge(
                seller_state, seller_pincode, buyer_state, buyer_pincode,
                weight_grams=r.get("weight_grams"), live=live,
            )
            if r.get("price") is not None:
                r["price"] = r["price"] + ship
            if r.get("flash_sale_price") is not None:
                r["flash_sale_price"] = r["flash_sale_price"] + ship
            r["shipping_charge"] = ship
        except AttributeError:
            continue
    return row_list[0] if single else row_list

def cart_summary(user_id):
    """Return cart items and summary dict. Also returns list of unavailable item titles.
    Each product's shipping_charge is baked into its price (all-inclusive price),
    same as on the homepage/product page — so GST is computed on the seller's
    base selling price only, while subtotal/total reflect the all-inclusive price."""
    conn = get_db()
    buyer_state   = get_buyer_state()
    buyer_pincode = get_buyer_pincode()
    # All cart items — seller ka state/pincode bhi laao taaki shipping live
    # Shiprocket rate (ya fallback state-wise flat rate) dynamically calculate
    # ho, stale products.shipping_charge column (jo hamesha 0 rehta hai) pe
    # depend karne ke bajaye.
    all_rows = _exec(conn, 
        """SELECT ci.id as cart_id, ci.qty, ci.size, ci.color,
                  p.id, p.title, p.price, p.shipping_charge, p.gst_percent, p.stock, p.image_url, p.approved,
                  p.weight_grams,
                  u.state as seller_state, u.pincode as seller_pincode
           FROM cart_items ci JOIN products p ON ci.product_id=p.id
                              JOIN users u ON p.seller_id = u.id
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
        base_price = r["price"]
        seller_state   = r.get("seller_state")
        seller_pincode = r.get("seller_pincode")
        # live=True: cart/checkout mein items kam hote hain aur yahi final
        # amount charge hota hai, isliye accuracy ke liye live Shiprocket
        # call (short timeout) karte hain, cache-hit ho to woh instant hi hai.
        ship = _resolve_shipping_charge(
            seller_state, seller_pincode, buyer_state, buyer_pincode,
            weight_grams=r.get("weight_grams"), live=True,
        )
        gst   = base_price * r["gst_percent"] / 100
        all_inclusive_price = base_price + ship
        total = (all_inclusive_price + gst) * qty
        subtotal  += all_inclusive_price * qty
        gst_total += gst * qty
        r["price"] = all_inclusive_price
        items.append(dict(r, qty=qty, line_total=total))
    # Shipping is now per-product and already baked into each item's price above,
    # so no extra flat shipping fee is added on top here.
    shipping = 0
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

def _otp_store_set(phone, otp, user_id, ttl_seconds=120):
    conn = get_db()
    _exec(conn, """
        INSERT INTO phone_otp_store (phone, otp, user_id, expires_at)
        VALUES (%s, %s, %s, NOW() + (%s || ' seconds')::interval)
        ON CONFLICT (phone) DO UPDATE
            SET otp = EXCLUDED.otp,
                user_id = EXCLUDED.user_id,
                expires_at = EXCLUDED.expires_at
    """, (phone, otp, user_id, ttl_seconds))
    conn.commit()

def _otp_store_get(phone):
    conn = get_db()
    row = _exec(conn, """
        SELECT phone, otp, user_id, expires_at,
               (expires_at < NOW()) AS is_expired
        FROM phone_otp_store WHERE phone=%s
    """, (phone,)).fetchone()
    return row

def _otp_store_pop(phone):
    conn = get_db()
    _exec(conn, "DELETE FROM phone_otp_store WHERE phone=%s", (phone,))
    conn.commit()

def send_whatsapp_otp(phone, otp):
    """WhatsApp bot microservice ko call karke OTP bhejo (admin panel se QR scan karke connect kiya gaya number)."""
    import requests as _req
    try:
        resp = _req.post(
            f"{WHATSAPP_BOT_URL}/send-otp",
            json={"phone": phone, "otp": otp},
            headers={"x-internal-key": WHATSAPP_BOT_INTERNAL_KEY},
            timeout=10
        )
        data = resp.json()
        if not data.get("ok"):
            app.logger.error(f"WhatsApp OTP send failed: {data.get('error')}")
        return bool(data.get("sent"))
    except Exception as e:
        app.logger.error(f"WhatsApp OTP send error: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# Auth routes (login/register/otp) — buyer app owns these;
# seller-role accounts get redirected to the seller app after OTP
# ─────────────────────────────────────────────────────────────
@app.route("/send-otp", methods=["POST"])
@limiter.limit("5 per minute")
def send_otp():
    """Send OTP via WhatsApp to phone number for login."""
    phone = request.form.get("phone", "").strip()
    if not re.match(r"^[6-9]\d{9}$", phone):
        flash("Valid 10-digit Indian mobile number daalo.", "err")
        return redirect("/login")

    conn = get_db()
    user = _exec(conn, "SELECT * FROM users WHERE phone=%s AND role='buyer'", (phone,)).fetchone()
    if not user:
        other = _exec(conn, "SELECT id FROM users WHERE phone=%s", (phone,)).fetchone()
        if other:
            flash(f"Yeh number seller account se registered hai. Seller Panel se login karo: {SELLER_APP_URL}/login", "err")
        else:
            flash("Is phone number se koi account nahi mila. Pehle register karo.", "err")
        return redirect("/login")

    if user["seller_status"] in ("suspended", "deleted"):
        flash("Aapka account suspend hai. Support se contact karo.", "err")
        return redirect("/login")

    import random
    otp = str(random.randint(100000, 999999))
    _otp_store_set(phone, otp, user["id"], ttl_seconds=120)  # 2 min expiry

    sent = send_whatsapp_otp(phone, otp)
    if sent:
        flash("OTP aapke WhatsApp pe bhej diya gaya! 📱", "ok")
    else:
        flash("WhatsApp OTP send nahi hua. Bot running hai? Dobara try karo.", "err")
        return redirect("/login")

    session["otp_phone"] = phone
    return redirect("/verify-otp")

@app.route("/send-otp-go")
def send_otp_go():
    """Internal redirect — sends OTP via WhatsApp for phone stored in session."""
    import random
    phone = session.get("otp_phone", "")
    if not phone:
        return redirect("/login")
    conn = get_db()
    user = _exec(conn, "SELECT * FROM users WHERE phone=%s AND role='buyer'", (phone,)).fetchone()
    if not user:
        other = _exec(conn, "SELECT id FROM users WHERE phone=%s", (phone,)).fetchone()
        if other:
            flash(f"Yeh number seller account se registered hai. Seller Panel se login karo: {SELLER_APP_URL}/login", "err")
        return redirect("/login")
    otp = str(random.randint(100000, 999999))
    _otp_store_set(phone, otp, user["id"], ttl_seconds=120)  # 2 min expiry
    sent = send_whatsapp_otp(phone, otp)
    if sent:
        flash("OTP aapke WhatsApp pe bhej diya gaya! 📱", "ok")
    else:
        flash("WhatsApp OTP send nahi hua. Bot running hai? Dobara try karo.", "err")
        return redirect("/login")
    return redirect("/verify-otp")

@app.route("/verify-otp", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def verify_otp():
    """Verify OTP and log user in."""
    phone = session.get("otp_phone", "")
    if not phone:
        return redirect("/login")

    if request.method == "POST":
        check_csrf()
        otp_entered = request.form.get("otp", "").strip()
        stored = _otp_store_get(phone)

        if not stored:
            flash("OTP expire ho gaya. Dobara try karo.", "err")
            return redirect("/login")

        if stored["is_expired"]:
            _otp_store_pop(phone)
            flash("OTP expire ho gaya (10 min). Dobara try karo.", "err")
            return redirect("/login")

        if otp_entered != stored["otp"]:
            flash("Galat OTP. Dobara daalo.", "err")
            return render_template("verify_otp.html", phone=phone)

        # OTP sahi hai — login karo
        _otp_store_pop(phone)
        session.pop("otp_phone", None)

        conn = get_db()
        user = _exec(conn, "SELECT * FROM users WHERE id=%s", (stored["user_id"],)).fetchone()

        session.clear()
        session.permanent = True
        session["user_id"]     = user["id"]
        session["name"]        = user["name"]
        session["role"]        = user["role"]
        session["is_reseller"] = bool(user["is_reseller"]) if "is_reseller" in user.keys() else False
        generate_csrf()

        # Buyer ka pincode nahi hai toh setup page pe bhejo
        if user["role"] == "buyer" and not user.get("pincode"):
            return redirect("/setup-pincode")

        if user["role"] == "seller":
            session.clear()
            flash(f"'{user['name']}', yeh seller account hai — Seller Panel pe login karo.", "ok")
            return redirect(f"{SELLER_APP_URL}/login")

        flash(f"Welcome back, {user['name']}! 🎉", "ok")
        return redirect("/")

    return render_template("verify_otp.html", phone=phone)

@app.route("/setup-pincode", methods=["GET", "POST"])
@login_required
def setup_pincode():
    """OTP ke baad buyer ka pincode setup karo — state-wise shipping ke liye."""
    if session.get("role") != "buyer":
        return redirect("/")

    if request.method == "POST":
        check_csrf()
        pincode = request.form.get("pincode", "").strip()

        # Skip kiya
        if not pincode:
            flash(f"Welcome, {session['name']}! 🎉", "ok")
            return redirect("/")

        # Pincode validate karo (6 digits)
        if not pincode.isdigit() or len(pincode) != 6:
            flash("Valid 6-digit pincode daalo.", "err")
            return render_template("setup_pincode.html")

        conn = get_db()
        # Pincode se state dhundho
        pin_row = _exec(conn, "SELECT * FROM pincode_serviceability WHERE pincode=%s", (pincode,)).fetchone()
        state = pin_row["state"] if pin_row else None

        # Save karo
        if state:
            _exec(conn, "UPDATE users SET pincode=%s, state=%s WHERE id=%s",
                  (pincode, state, session["user_id"]))
        else:
            _exec(conn, "UPDATE users SET pincode=%s WHERE id=%s",
                  (pincode, session["user_id"]))
        conn.commit()

        flash(f"Welcome, {session['name']}! 🎉", "ok")
        return redirect("/")

    return render_template("setup_pincode.html")

@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    """Login page — phone number se, OTP ke zariye."""
    if session.get("user_id"):
        return redirect("/")
    if request.method == "POST":
        check_csrf()
        phone = request.form.get("phone", "").strip()
        if not re.match(r"^[6-9]\d{9}$", phone):
            flash("Valid 10-digit Indian mobile number daalo.", "err")
            return redirect("/login")
        conn = get_db()
        user = _exec(conn, "SELECT * FROM users WHERE phone=%s AND role='buyer'", (phone,)).fetchone()
        if not user:
            other = _exec(conn, "SELECT id FROM users WHERE phone=%s", (phone,)).fetchone()
            if other:
                flash(f"Yeh number seller account se registered hai. Seller Panel se login karo: {SELLER_APP_URL}/login", "err")
                return redirect("/login")
            flash("Is number se koi account nahi mila. Pehle register karo.", "err")
            return redirect("/register")
        if user["seller_status"] in ("suspended", "deleted"):
            flash("Aapka account suspend hai. Support se contact karo.", "err")
            return redirect("/login")
        session["otp_phone"] = phone
        return redirect("/send-otp-go")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def register():
    """Register — sirf naam, phone aur account type. Email/password nahi."""
    if session.get("user_id"):
        return redirect("/")
    if request.method == "POST":
        check_csrf()
        name     = request.form.get("name", "").strip()
        phone    = request.form.get("phone", "").strip()
        role     = "buyer"  # buyer app — sirf buyer accounts yahan banenge
        ref_code = request.form.get("ref_code", "").strip()
        if not name:
            flash("Apna naam daalo.", "err")
            return redirect("/register")
        if not re.match(r"^[6-9]\d{9}$", phone):
            flash("Valid 10-digit Indian mobile number daalo.", "err")
            return redirect("/register")
        conn = get_db()
        if _exec(conn, "SELECT id FROM users WHERE phone=%s AND role='buyer'", (phone,)).fetchone():
            flash("Yeh phone number pehle se buyer account se registered hai. Login karo.", "err")
            return redirect("/login")
        my_ref = uuid.uuid4().hex[:8].upper()
        # email aur password blank — phone-only system
        # email blank rakho (NOT NULL ke liye empty string)
        # role-specific fake email — same phone buyer + seller dono ban sakta hai, email UNIQUE hai isliye role suffix zaroori
        fake_email = f"phone_{phone}_buyer@xoptime.local"
        _exec(conn, 
            "INSERT INTO users (name,email,phone,password,role,referral_code,referred_by) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (name, fake_email, phone, "", role, my_ref, ref_code or None)
        )
        new_user_id = _scalar(_exec(conn, "SELECT lastval()"))

        # ── Referral wallet credit ──
        if ref_code:
            referrer = _exec(conn, "SELECT * FROM users WHERE referral_code=%s", (ref_code,)).fetchone()
            if referrer:
                _exec(conn, "UPDATE users SET wallet_balance=COALESCE(wallet_balance,0)+50 WHERE id=%s",
                             (referrer["id"],))
                conn.commit()
                add_notification(referrer["id"], "Referral Reward!",
                                 f"'{name}' ne aapka referral code use kiya. ₹50 wallet mein add ho gaye!",
                                 "/referral")
        conn.commit()

        # Auto-login via OTP after register
        session["otp_phone"] = phone
        flash("Account ban gaya! OTP se login karo.", "ok")
        return redirect("/send-otp-go")
    return render_template("register.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def forgot_password():
    if request.method == "POST":
        check_csrf()
        email = request.form.get("email", "").strip().lower()
        conn  = get_db()
        user  = _exec(conn, "SELECT * FROM users WHERE email=%s", (email,)).fetchone()
        if user:
            token = secrets.token_urlsafe(32)
            _exec(conn, 
                "INSERT INTO password_reset_tokens (user_id,token,expires_at) VALUES (%s,%s,NOW() + INTERVAL '1 hour')",
                (user["id"], token)
            )
            conn.commit()
            reset_url = request.host_url.rstrip("/") + f"/reset-password/{token}"
            send_email(email, "Reset your Xoptime password",
                       f"<p>Click <a href='{reset_url}'>here</a> to reset your password. Link valid for 1 hour.</p>")
        flash("If that email exists, a reset link has been sent.", "ok")
        return redirect("/login")
    return render_template("forgot_password.html")

@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    conn = get_db()
    row  = _exec(conn, 
        "SELECT * FROM password_reset_tokens WHERE token=%s AND used=0 AND expires_at>NOW()",
        (token,)
    ).fetchone()
    if not row:
        flash("Invalid or expired reset link.", "err")
        return redirect("/login")
    if request.method == "POST":
        check_csrf()
        pw = request.form.get("password", "")
        if len(pw) < 6:
            flash("Password must be at least 6 characters.", "err")
            return redirect(request.path)
        _exec(conn, "UPDATE users SET password=%s WHERE id=%s",
                     (generate_password_hash(pw), row["user_id"]))
        _exec(conn, "UPDATE password_reset_tokens SET used=1 WHERE id=%s", (row["id"],))
        conn.commit()
        flash("Password updated! Please login.", "ok")
        return redirect("/login")
    return render_template("change_password.html", token=token, reset_mode=True)

@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        check_csrf()
        old = request.form.get("old_password", "")
        new = request.form.get("new_password", "")
        conn = get_db()
        user = _exec(conn, "SELECT * FROM users WHERE id=%s", (session["user_id"],)).fetchone()
        if not check_password_hash(user["password"], old):
            flash("Current password is wrong.", "err")
            return redirect("/change-password")
        if len(new) < 6:
            flash("New password must be 6+ chars.", "err")
            return redirect("/change-password")
        _exec(conn, "UPDATE users SET password=%s WHERE id=%s",
                     (generate_password_hash(new), session["user_id"]))
        conn.commit()
        flash("Password changed successfully.", "ok")
        return redirect("/")
    return render_template("change_password.html")


# ─────────────────────────────────────────────────────────────
# Buyer routes
# ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if session.get("role") == "seller":
        return redirect(f"{SELLER_APP_URL}/seller/dashboard")
    conn = get_db()
    buyer_state = get_buyer_state()
    products = _exec(conn,
        "SELECT p.*, u.name as seller_name, u.state as seller_state, u.pincode as seller_pincode, "
        "COALESCE(agg.avg_rating,0) avg_rating, COALESCE(agg.review_count,0) review_count "
        "FROM products p JOIN users u ON p.seller_id=u.id "
        "LEFT JOIN (SELECT product_id, AVG(rating) avg_rating, COUNT(id) review_count "
        "FROM reviews GROUP BY product_id) agg ON agg.product_id=p.id "
        "WHERE p.approved=1 AND p.stock>0 AND COALESCE((SELECT gst_suspended FROM users WHERE id=p.seller_id),0)=0 ORDER BY p.created_at DESC LIMIT 40"
    ).fetchall()
    featured = _exec(conn,
        "SELECT p.*, u.state as seller_state, u.pincode as seller_pincode, COALESCE(agg.avg_rating,0) avg_rating, COALESCE(agg.review_count,0) review_count "
        "FROM products p JOIN users u ON p.seller_id=u.id "
        "LEFT JOIN (SELECT product_id, AVG(rating) avg_rating, COUNT(id) review_count "
        "FROM reviews GROUP BY product_id) agg ON agg.product_id=p.id "
        "WHERE p.approved=1 AND p.stock>0 AND COALESCE((SELECT gst_suspended FROM users WHERE id=p.seller_id),0)=0 ORDER BY avg_rating DESC, review_count DESC LIMIT 8"
    ).fetchall()
    trending = _exec(conn,
        "SELECT p.*, u.state as seller_state, u.pincode as seller_pincode, COALESCE(agg.avg_rating,0) avg_rating, COALESCE(agg.review_count,0) review_count "
        "FROM products p JOIN users u ON p.seller_id=u.id "
        "LEFT JOIN (SELECT product_id, AVG(rating) avg_rating, COUNT(id) review_count "
        "FROM reviews GROUP BY product_id) agg ON agg.product_id=p.id "
        "WHERE p.approved=1 AND p.stock>0 AND COALESCE((SELECT gst_suspended FROM users WHERE id=p.seller_id),0)=0 AND p.trending=1 ORDER BY p.created_at DESC LIMIT 8"
    ).fetchall()
    flash_sale_products = _exec(conn,
        "SELECT p.*, u.state as seller_state, u.pincode as seller_pincode, COALESCE(agg.avg_rating,0) avg_rating, COALESCE(agg.review_count,0) review_count "
        "FROM products p JOIN users u ON p.seller_id=u.id "
        "LEFT JOIN (SELECT product_id, AVG(rating) avg_rating, COUNT(id) review_count "
        "FROM reviews GROUP BY product_id) agg ON agg.product_id=p.id "
        "WHERE p.approved=1 AND p.stock>0 AND COALESCE((SELECT gst_suspended FROM users WHERE id=p.seller_id),0)=0 AND p.is_flash_sale=1 ORDER BY p.created_at DESC LIMIT 8"
    ).fetchall()
    banners = _exec(conn, "SELECT b.*, p.image_url as product_image_url FROM banners b LEFT JOIN products p ON b.product_id=p.id WHERE b.active=1 ORDER BY b.sort_order LIMIT 5").fetchall()
    flash_sale = _exec(conn, 
        "SELECT * FROM flash_sales WHERE active=1 AND (ends_at IS NULL OR ends_at > NOW()) LIMIT 1"
    ).fetchone()
    # Homepage pe ek saath 60+ products dikhte hain — live=False rakha hai
    # taaki har ek ke liye blocking Shiprocket call na ho, page fast rahe.
    # Cache-hit hue to live rate turant milti hai, cache-miss pe background
    # mein warm ho jaata hai next load ke liye, aur abhi ke liye flat rate
    # se fallback silently ho jaata hai.
    _bp = get_buyer_pincode()
    bake_shipping_into_price(products, buyer_state, _bp)
    bake_shipping_into_price(featured, buyer_state, _bp)
    bake_shipping_into_price(trending, buyer_state, _bp)
    bake_shipping_into_price(flash_sale_products, buyer_state, _bp)
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
    sql = ("SELECT p.*, u.name as seller_name, u.state as seller_state, u.pincode as seller_pincode, "
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
    bake_shipping_into_price(products, get_buyer_state(), get_buyer_pincode())
    categories  = [r["category"] for r in _exec(conn, 
        "SELECT DISTINCT category FROM products WHERE approved=1 ORDER BY category").fetchall()]
    brands = [r["brand"] for r in _exec(conn, 
        "SELECT DISTINCT brand FROM products WHERE approved=1 AND brand IS NOT NULL AND brand!='' ORDER BY brand"
    ).fetchall()]
    qs = "&".join(f"{k}={v}" for k, v in request.args.items() if k != "page")
    return render_template("search.html", products=products, categories=categories,
                           brands=brands, total=total, page=page, total_pages=total_pages,
                           query_string=qs)

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
    bake_shipping_into_price(products, get_buyer_state(), get_buyer_pincode())
    return render_template("categories.html",
                           all_categories=all_categories, products=products,
                           selected_cat=selected_cat, sort=sort,
                           total=total, page=page, total_pages=total_pages)

@app.route("/p/<int:pid>")
def product_detail(pid):
    conn = get_db()
    buyer_state = get_buyer_state()
    p = _exec(conn,
        "SELECT p.*, u.name as seller_name, u.state as seller_state, u.pincode as seller_pincode, "
        "COALESCE(agg.avg_rating,0) avg_rating, COALESCE(agg.review_count,0) review_count "
        "FROM products p JOIN users u ON p.seller_id=u.id "
        "LEFT JOIN (SELECT product_id, AVG(rating) avg_rating, COUNT(id) review_count "
        "FROM reviews GROUP BY product_id) agg ON agg.product_id=p.id "
        "WHERE p.id=%s AND p.approved=1", (pid,)
    ).fetchone()
    if not p:
        abort(404)
    buyer_pincode = get_buyer_pincode()
    # Product page pe sirf ek hi "main" product hai — is wahan live=True
    # (blocking, short timeout) safe hai, cache-hit ho to instant hi hai.
    bake_shipping_into_price(p, buyer_state, buyer_pincode, live=True)
    images   = _exec(conn, 
        "SELECT * FROM product_images WHERE product_id=%s ORDER BY sort_order", (pid,)).fetchall()
    variants = _exec(conn, 
        "SELECT * FROM product_variants WHERE product_id=%s", (pid,)).fetchall()
    reviews  = _exec(conn, 
        "SELECT r.*, u.name FROM reviews r JOIN users u ON r.user_id=u.id WHERE r.product_id=%s ORDER BY r.created_at DESC",
        (pid,)
    ).fetchall()
    similar  = _exec(conn, 
        "SELECT * FROM products WHERE seller_id=%s AND id!=%s AND approved=1 AND stock>0 ORDER BY RANDOM() LIMIT 12",
        (p["seller_id"], pid)
    ).fetchall()
    bake_shipping_into_price(similar, buyer_state, buyer_pincode)
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
        buyer_name   = request.form.get("buyer_name", "").strip()
        phone        = request.form.get("phone", "").strip()
        alt_phone    = request.form.get("alt_phone", "").strip()
        address      = request.form.get("address", "").strip()
        landmark     = request.form.get("landmark", "").strip()
        pincode      = request.form.get("pincode", "").strip()
        address_type = request.form.get("address_type", "Home").strip() or "Home"
        pay_mode     = request.form.get("pay_mode", "COD")
        save_addr    = request.form.get("save_address")

        # Buyer city/state — form se lo, fallback to users table (pincode setup pe save hoti hai login pe)
        buyer_city  = request.form.get("city", "").strip()
        buyer_state = request.form.get("state", "").strip()
        if not buyer_state or not buyer_city:
            _u = _exec(conn, "SELECT state, city FROM users WHERE id=%s", (session["user_id"],)).fetchone()
            if _u:
                buyer_state = buyer_state or (_u["state"] or "")
                buyer_city  = buyer_city  or (_u["city"]  or "")
        # Pincode se state dhundho agar abhi bhi missing
        if not buyer_state and pincode:
            _pin = _exec(conn, "SELECT state, city FROM pincode_serviceability WHERE pincode=%s", (pincode,)).fetchone()
            if _pin:
                buyer_state = buyer_state or (_pin["state"] or "")
                buyer_city  = buyer_city  or (_pin["city"]  or "")

        # Server-side validation
        if not buyer_name or not phone or not address:
            flash("Please fill all delivery details.", "err")
            return redirect("/checkout")
        if not phone.isdigit() or len(phone) != 10:
            flash("Valid 10-digit phone number daalo.", "err")
            return redirect("/checkout")

        # Save address if requested
        if save_addr:
            new_addr = {"name": buyer_name, "phone": phone, "alt_phone": alt_phone,
                        "address": address, "landmark": landmark,
                        "pincode": pincode, "city": buyer_city, "state": buyer_state,
                        "address_type": address_type}
            if new_addr not in saved_addresses:
                saved_addresses.append(new_addr)
                _exec(conn, "UPDATE users SET saved_addresses=%s WHERE id=%s",
                             (json.dumps(saved_addresses), session["user_id"]))
                conn.commit()

        # Re-check stock with a row lock, inside this transaction, right before
        # confirming the order. Without this, two buyers checking out the last
        # unit at the same time could both get "confirmed" orders — the
        # GREATEST(0, stock-qty) update below only floors stock at 0, it
        # doesn't stop the second order from going through. FOR UPDATE locks
        # each product row until this transaction commits/rolls back, so a
        # concurrent checkout on the same product has to wait for us to finish.
        insufficient = []
        for it in items:
            prod = _exec(conn, "SELECT id, title, stock FROM products WHERE id=%s FOR UPDATE",
                                (it["id"],)).fetchone()
            if not prod or prod["stock"] < it["qty"]:
                insufficient.append(it["title"])
        if insufficient:
            conn.rollback()
            flash(f"Stock khatam ho gaya: {', '.join(insufficient)}. Cart update karo.", "err")
            return redirect("/cart")

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
            """INSERT INTO orders (public_id,buyer_id,buyer_name,phone,alt_phone,address,landmark,
               address_type,pincode,city,state,
               pay_mode,payment_status,status,subtotal,gst_total,shipping,discount,total_amount,
               coupon_code,invoice_no,invoice_date)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
               RETURNING id""",
            (public_id, session["user_id"], buyer_name, phone, alt_phone, address, landmark,
             address_type, pincode,
             buyer_city or "N/A", buyer_state or "N/A",
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

        if pay_mode == "QR_CODE" and RAZORPAY_AVAILABLE and RZP_KEY_ID:
            # Generate Razorpay UPI QR Code
            import urllib.request as _ur, base64 as _b64, time as _time
            client = razorpay.Client(auth=(RZP_KEY_ID, RZP_KEY_SECRET))
            rzp_order = client.order.create({
                "amount": int(summary["total"] * 100),
                "currency": "INR",
                "receipt": public_id,
            })
            _exec(conn, "UPDATE orders SET payment_id=%s WHERE id=%s",
                         (rzp_order["id"], order_id))
            conn.commit()

            qr_image_url = ""
            qr_id        = ""
            qr_error_msg = ""

            # Try Razorpay QR API
            try:
                qr_resp = client.qrcode.create({
                    "type": "upi_qr",
                    "name": "Xoptime Payment",
                    "usage": "single_use",
                    "fixed_amount": True,
                    "payment_amount": int(summary["total"] * 100),
                    "description": f"Order #{public_id}",
                    "close_by": int(_time.time()) + 900,  # 15 min expiry
                })
                qr_image_url = qr_resp.get("image_url", "")
                qr_id        = qr_resp.get("id", "")
                app.logger.info(f"QR created: {qr_id}, image_url={qr_image_url!r}")
            except Exception as qr_err:
                app.logger.warning(f"QR create failed: {qr_err}")
                qr_error_msg = str(qr_err)

            # Fallback: generate UPI deeplink + QR using free API if Razorpay QR fails
            if not qr_image_url:
                try:
                    # Get seller/platform UPI ID for fallback
                    platform_upi = os.getenv("PLATFORM_UPI_ID", "")
                    if platform_upi:
                        amt_str = f"{summary['total']:.2f}"
                        upi_uri = (f"upi://pay?pa={platform_upi}"
                                   f"&pn=Xoptime&am={amt_str}&cu=INR"
                                   f"&tn=Order+{public_id}")
                        # Use a free QR generator API (no auth needed)
                        import urllib.parse as _up
                        encoded_uri = _up.quote(upi_uri, safe="")
                        qr_image_url = f"https://api.qrserver.com/v1/create-qr-code/?size=220x220&data={encoded_uri}"
                        qr_id = f"fallback_{public_id}"
                        app.logger.info(f"Using fallback QR for order {public_id}")
                except Exception as fb_err:
                    app.logger.warning(f"Fallback QR also failed: {fb_err}")

            return render_template("checkout.html",
                                   qr_mode=True,
                                   qr_image_url=qr_image_url,
                                   qr_id=qr_id,
                                   qr_error_msg=qr_error_msg,
                                   rzp_order_id=rzp_order["id"],
                                   order_db_id=order_id, public_id=public_id,
                                   items=items, summary=summary,
                                   saved_addresses=saved_addresses, user=user,
                                   rzp_key_id=RZP_KEY_ID)

        if pay_mode in ("RAZORPAY", "UPI") and RAZORPAY_AVAILABLE and RZP_KEY_ID:
            client = razorpay.Client(auth=(RZP_KEY_ID, RZP_KEY_SECRET))
            rzp_order = client.order.create({
                "amount": int(summary["total"] * 100),
                "currency": "INR",
                "receipt": public_id,
            })
            _exec(conn, "UPDATE orders SET payment_id=%s WHERE id=%s",
                         (rzp_order["id"], order_id))
            conn.commit()
            upi_id = request.form.get("upi_id", "").strip() if pay_mode == "UPI" else ""
            return render_template("checkout.html", razorpay_order=rzp_order,
                                   order_db_id=order_id, public_id=public_id,
                                   items=items, summary=summary,
                                   saved_addresses=saved_addresses, user=user,
                                   rzp_key_id=RZP_KEY_ID,
                                   razorpay_redirect=True,
                                   prefill_upi=upi_id,
                                   pay_mode_selected=pay_mode)

        flash(f"Order placed successfully! Order ID: #{public_id}", "ok")
        return redirect(f"/orders")

    return render_template("checkout.html", items=items, summary=summary,
                           saved_addresses=saved_addresses, user=user,
                           rzp_key_id=RZP_KEY_ID)

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

@app.route("/payment/qr-status")
@login_required
def qr_payment_status():
    """Polling endpoint — frontend checks every 3s if QR payment is done."""
    order_db_id = request.args.get("order_id", "")
    if not order_db_id:
        return {"status": "error"}, 400
    conn  = get_db()
    order = _exec(conn,
        "SELECT status, payment_status, public_id FROM orders WHERE id=%s AND buyer_id=%s",
        (order_db_id, session["user_id"])
    ).fetchone()
    if not order:
        return {"status": "error"}, 404
    return {
        "status":         order["status"],
        "payment_status": order["payment_status"],
        "public_id":      order["public_id"],
    }

@app.route("/webhook/razorpay", methods=["POST"])
def razorpay_webhook():
    """Razorpay webhook — confirms payment even if user closes browser after paying."""
    import hmac as _hmac, hashlib as _hashlib
    webhook_secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
    payload_bytes  = request.get_data()
    rzp_signature  = request.headers.get("X-Razorpay-Signature", "")
    is_debug       = os.getenv("FLASK_DEBUG", "false").lower() == "true"

    if not webhook_secret:
        # Fail closed unless we're explicitly in local/dev mode — accepting
        # unverified webhooks in production would let anyone fake payments.
        if not is_debug:
            app.logger.error("RAZORPAY_WEBHOOK_SECRET not set — rejecting webhook.")
            return {"status": "webhook secret not configured"}, 500
    else:
        expected = _hmac.new(webhook_secret.encode(), payload_bytes, _hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(expected, rzp_signature):
            return {"status": "invalid signature"}, 400

    try:
        data  = request.get_json(force=True) or {}
        event = data.get("event", "")

        if event == "payment.captured":
            payment        = data.get("payload", {}).get("payment", {}).get("entity", {})
            rzp_order_id   = payment.get("order_id")
            rzp_payment_id = payment.get("id")
            if rzp_order_id and rzp_payment_id:
                conn  = get_db()
                order = _exec(conn,
                    "SELECT id, buyer_id, public_id, status FROM orders WHERE payment_id=%s",
                    (rzp_order_id,)
                ).fetchone()
                if order and order["status"] != "confirmed":
                    _exec(conn,
                        "UPDATE orders SET payment_status='paid', payment_id=%s, status='confirmed' WHERE id=%s",
                        (rzp_payment_id, order["id"])
                    )
                    conn.commit()
                    add_notification(order["buyer_id"], "Payment Confirmed",
                                     f"Payment received for order #{order['public_id']}!", "/orders")

        elif event == "payment.failed":
            payment      = data.get("payload", {}).get("payment", {}).get("entity", {})
            rzp_order_id = payment.get("order_id")
            if rzp_order_id:
                conn = get_db()
                _exec(conn,
                    "UPDATE orders SET payment_status='failed' WHERE payment_id=%s AND payment_status='pending'",
                    (rzp_order_id,)
                )
                conn.commit()

    except Exception as e:
        app.logger.error(f"Razorpay webhook error: {e}")
        return {"status": "error"}, 500

    return {"status": "ok"}, 200

@app.route("/webhook/shiprocket", methods=["POST"])
def shiprocket_webhook():
    """Shiprocket calls this URL to update order status automatically."""
    # Shiprocket lets you set a custom "API token" header on the webhook
    # config in its dashboard. Without checking it, anyone who knows an
    # order's AWB number (printed on the shipping label) could POST here
    # and fake a "delivered" status to trigger cashback/referral/settlement.
    expected_token = os.getenv("SHIPROCKET_WEBHOOK_TOKEN", "")
    is_debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    if not expected_token:
        if not is_debug:
            app.logger.error("SHIPROCKET_WEBHOOK_TOKEN not set — rejecting webhook.")
            return jsonify({"ok": False, "error": "webhook token not configured"}), 500
    else:
        received_token = request.headers.get("X-Api-Key") or request.headers.get("api-token", "")
        if not hmac.compare_digest(str(received_token), expected_token):
            return jsonify({"ok": False, "error": "invalid token"}), 401
    try:
        data = request.get_json(force=True) or {}
        awb          = data.get("awb") or data.get("awb_code", "")
        status       = data.get("current_status", "")
        shipment_id  = data.get("shipment_id", "")
        courier_name = data.get("courier_name", "")
        if not awb or not status:
            return jsonify({"ok": False}), 400
        conn = get_db()
        # Primary lookup: current awb. Fallback: shipment_id — this covers the
        # case where the seller reassigned the courier directly on Shiprocket's
        # dashboard, which changes the awb, so our stored (old) awb no longer
        # matches the awb this webhook is reporting.
        o = _exec(conn, "SELECT * FROM orders WHERE awb=%s", (awb,)).fetchone()
        if not o and shipment_id:
            o = _exec(conn, "SELECT * FROM orders WHERE shiprocket_shipment_id=%s", (str(shipment_id),)).fetchone()
        if not o:
            return jsonify({"ok": False, "msg": "Order not found"}), 404
        # Map Shiprocket status to our status
        status_map = {
            "Delivered":        "delivered",
            "Out For Delivery":  "out_for_delivery",
            "In Transit":        "shipped",
            "Pickup Scheduled":  "ReadyToShip",
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
            # Mark seller earnings + set settlement due date (7 din baad)
            _exec(conn, """UPDATE seller_transactions
                           SET status='earned',
                               settlement_due_at = NOW() + INTERVAL '""" + str(SETTLEMENT_DAYS) + """ days'
                           WHERE order_id=%s AND status='pending'""", (o["id"],))
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
                prev_delivered = _scalar(_exec(conn,
                    "SELECT COUNT(*) FROM orders WHERE buyer_id=%s AND status='delivered' AND id!=%s",
                    (o["buyer_id"], o["id"])
                ))
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
        _exec(conn, "UPDATE orders SET status=%s, awb=%s, courier_name=%s, updated_at=NOW() WHERE id=%s",
                     (new_status, awb, courier_name or o["courier_name"], o["id"]))
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

    # If a Shiprocket shipment was already created for this order (seller had
    # marked it ReadyToShip), cancel it there too so the courier doesn't still
    # try to pick it up / deliver it.
    if o["shiprocket_order_id"]:
        try:
            sr_result = shiprocket_cancel_order(o["shiprocket_order_id"])
            _exec(conn, "UPDATE orders SET shiprocket_cancel_synced=%s WHERE id=%s",
                         (1 if sr_result.get("success") else 0, oid))
            conn.commit()
            if not sr_result.get("success"):
                logger.warning(f"[Shiprocket] cancel not confirmed for order {oid} "
                                f"(shiprocket_order_id={o['shiprocket_order_id']}): {sr_result.get('error') or sr_result.get('message')}")
        except Exception as e:
            logger.warning(f"[Shiprocket] cancel_order call error for order {oid}: {e}")

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

@app.route("/wishlist")
@login_required
def wishlist():
    conn  = get_db()
    items = _exec(conn, 
        "SELECT p.* FROM wishlist_items w JOIN products p ON w.product_id=p.id "
        "WHERE w.user_id=%s ORDER BY w.created_at DESC", (session["user_id"],)
    ).fetchall()
    bake_shipping_into_price(items, get_buyer_state(), get_buyer_pincode())
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
# Error handlers
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


with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(
    debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
    host="0.0.0.0",
    port=int(os.getenv("PORT", 5000))
    )
