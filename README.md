# Xoptime buyer site (Flask + Neon Postgres, Render)

Products, prices, stock and **images come from the seller site** (`/api/public/products`, Cloudinary URLs). Nothing is stored in this repo.

## Go-live steps
1. **Seller site (Render)**: set `BUYER_SYNC_KEY` = a long random secret. Set the Cloudinary env vars there too (images are lost on Render without them). Approve products in the seller admin: only approved, in-stock products reach the buyer site.
2. **Neon**: create a project, copy the connection string.
3. **Push this folder to GitHub** (`.env` is git-ignored), then Render > New > Blueprint.
4. **Env vars on the buyer service** (see `render.yaml`):
   - `DATABASE_URL`, `SELLER_URL`, `SELLER_SYNC_KEY` (= seller's `BUYER_SYNC_KEY`)
   - `WHATSAPP_BOT_URL` (public https URL of your bot), optional `WHATSAPP_BOT_KEY`, `ADMIN_WHATSAPP` (your number)
   - `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` (leave empty = Cash on Delivery only)
   - `BUSINESS_NAME`, `BUSINESS_ADDRESS`, `BUSINESS_PHONE`, `BUSINESS_EMAIL`, `BUSINESS_GRIEVANCE`
5. **Razorpay**: use live keys, keep auto-capture on, add webhook `https://YOUR-SITE/api/razorpay/webhook` with events `payment.captured` and `order.paid`.
6. Open `https://YOUR-SITE/admin` (enter `ADMIN_KEY`) to manage orders. Open the site, log in with a real number, place a COD and an online test order.

## WhatsApp bot contract
- Login: `POST {WHATSAPP_BOT_URL}/send-otp` with `{"phone":"9876543210","otp":"123456"}`, bot replies `{"success": true}` (same as the seller site uses). The site generates and verifies the OTP.
- Order updates: `POST {WHATSAPP_BOT_URL}/send-message` with `{"phone":"9876543210","message":"..."}` (path configurable via `WHATSAPP_NOTIFY_PATH`). Best effort: if it fails, orders still work.

## Notes
- Legal pages (`/legal/terms`, `privacy`, `returns`, `shipping`, `contact`) are templates built from the `BUSINESS_*` vars. Have them reviewed and check they match how you actually operate.
- Every placed order is pushed to the seller app (`/api/public/orders`, retried every 90s until it lands) and appears in the seller's Pending Orders. The seller app reports accept / AWB / shipped / delivered / rejected back to `/api/seller/order-update`. Use a **separate** Neon DB from the seller app.
- Stock is read from the seller feed (refreshed every 60 s); the seller app reduces it when the order arrives.
