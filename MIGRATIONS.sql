-- Run these SQL migrations on your database:

-- Add seller_status to users table
ALTER TABLE users ADD COLUMN seller_status TEXT DEFAULT 'active';

-- Add PAN and bank details to users table
ALTER TABLE users ADD COLUMN pan TEXT;
ALTER TABLE users ADD COLUMN bank_name TEXT;
ALTER TABLE users ADD COLUMN bank_bank TEXT;
ALTER TABLE users ADD COLUMN bank_account TEXT;
ALTER TABLE users ADD COLUMN bank_ifsc TEXT;
ALTER TABLE users ADD COLUMN upi_id TEXT;

-- Add cart_count to be computed in context processor:
-- In your @app.context_processor def inject_globals():
--     cart_count = 0
--     if session.get("role") == "buyer":
--         conn = get_db()
--         cart_count = conn.execute("SELECT COALESCE(SUM(qty),0) FROM cart_items WHERE user_id=?", (session["user_id"],)).fetchone()[0]
--     return dict(cart_count=cart_count, ...)
