# Xoptime (Flask + Neon Postgres, deploy on Render)

1. **Neon**: create a project, copy the connection string (`postgresql://...?sslmode=require`).
2. **GitHub**: push this folder to a new repo.
3. **Render**: New > Blueprint (uses `render.yaml`) or New > Web Service.
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn app:app --workers 2 --bind 0.0.0.0:$PORT`
   - Env vars: `DATABASE_URL` = Neon string, `ADMIN_KEY` = any long secret.
4. Open your Render URL. Tables and demo products are created automatically on first start.

Local run: `pip install -r requirements.txt && DATABASE_URL=... python app.py`

Seller orders: `https://YOUR-APP.onrender.com/admin/orders?key=ADMIN_KEY` (shows items, total, customer profile and addresses).

Products live in the Neon `products` table (edit price/name/active there). Put new photos in `static/img/` and add their paths to `images`.

Notes: users are anonymous guests (a random token saved in the browser); add real login (phone/OTP) before taking real payments. Bank details are stored as plain JSON, so encrypt them or drop that feature before going live.

## Seller site link (products)
Products uploaded + approved on the seller site show up here automatically.
Set on THIS (buyer) service:
- `SELLER_API_URL` = seller site URL, e.g. `https://xoptime-seller.onrender.com`
- `SELLER_SYNC_KEY` = long random secret (same value as `BUYER_SYNC_KEY` on seller)
Seller products get id `10000 + seller_product_id`. Feed is cached 60s; if seller is asleep/down the last copy is served.
