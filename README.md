# Xoptime — Buyer App

Split off from the monolith `app.py` (v11, admin already removed). This app owns
everything a buyer does: browsing, cart, checkout, orders, wishlist, reseller
program, notifications, support, plus shared auth (login/register/OTP).

## Deploy checklist
1. Push this folder as its own GitHub repo, deploy on Render (`render.yaml` included).
2. Set `DATABASE_URL` to the **same** Postgres DB the seller app uses — this is the
   whole point of the split, both apps read/write the same tables.
3. Set `SELLER_APP_URL` to wherever the seller app ends up deployed
   (e.g. `https://xoptime-seller.onrender.com`).
4. Copy over the other env vars from your old single-app deployment
   (Razorpay, Shiprocket, Cloudinary, SMTP, COMPANY_*) — see `render.yaml` comments.

## How cross-domain login works
- `/login`, `/register`, `/verify-otp` all live here (and are duplicated in the
  seller app too — same phone+OTP flow).
- If someone verifies OTP here but their account role is `seller`, they're
  **not** logged in on this domain — they get a flash message and a redirect to
  `SELLER_APP_URL/login` instead, since a session cookie set here won't exist on
  the seller app's domain.
- `register()` here only ever creates `role='buyer'` accounts — the role
  dropdown was removed from the form handling (Seller signup happens on the
  seller app).

## What's NOT here
Seller dashboard/orders/products/KYC/wallet/analytics/payouts, and the
settlement + GST-sync cron endpoints — all live in the seller app.
