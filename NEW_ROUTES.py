# ============================================================
# SELLORA — New Routes to add to your app.py / main Flask file
# Add these routes alongside your existing routes
# ============================================================

# ── HOMEPAGE ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
    conn = get_db()
    products = conn.execute(
        "SELECT p.*, u.name as seller_name, COALESCE(AVG(r.rating),0) avg_rating, COUNT(r.id) review_count "
        "FROM products p JOIN users u ON p.seller_id=u.id LEFT JOIN reviews r ON r.product_id=p.id "
        "WHERE p.approved=1 AND p.stock>0 GROUP BY p.id ORDER BY p.created_at DESC LIMIT 40"
    ).fetchall()
    featured = conn.execute(
        "SELECT p.*, COALESCE(AVG(r.rating),0) avg_rating, COUNT(r.id) review_count "
        "FROM products p LEFT JOIN reviews r ON r.product_id=p.id "
        "WHERE p.approved=1 AND p.stock>0 GROUP BY p.id ORDER BY avg_rating DESC, review_count DESC LIMIT 8"
    ).fetchall()
    categories = [r[0] for r in conn.execute("SELECT DISTINCT category FROM products WHERE approved=1 ORDER BY category").fetchall()]
    stats = {
        "products": conn.execute("SELECT COUNT(*) FROM products WHERE approved=1").fetchone()[0],
        "sellers": conn.execute("SELECT COUNT(*) FROM users WHERE role='seller'").fetchone()[0],
        "orders": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
    }
    return render_template("index.html", products=products, featured=featured, categories=categories, stats=stats)


# ── SEARCH ────────────────────────────────────────────────────────────────
@app.route("/search")
def search():
    q        = request.args.get("q","").strip()
    category = request.args.get("category","")
    sort     = request.args.get("sort","")
    min_p    = request.args.get("min","")
    max_p    = request.args.get("max","")
    rating   = request.args.get("rating","")
    page     = max(1, int(request.args.get("page",1)))
    per_page = 24

    sql = ("SELECT p.*, u.name as seller_name, COALESCE(AVG(rv.rating),0) avg_rating, COUNT(rv.id) review_count "
           "FROM products p JOIN users u ON p.seller_id=u.id LEFT JOIN reviews rv ON rv.product_id=p.id "
           "WHERE p.approved=1 AND p.stock>0 ")
    params = []
    if q:
        sql += "AND (p.title LIKE ? OR p.description LIKE ? OR p.category LIKE ? OR p.brand LIKE ?) "
        params += [f"%{q}%"]*4
    if category:
        sql += "AND p.category=? "
        params.append(category)
    if min_p:
        sql += "AND p.price>=? "; params.append(float(min_p))
    if max_p:
        sql += "AND p.price<=? "; params.append(float(max_p))
    sql += "GROUP BY p.id "
    if rating:
        sql += f"HAVING avg_rating>={rating} "
    order_map = {"price_asc":"p.price ASC","price_desc":"p.price DESC","newest":"p.created_at DESC","rating":"avg_rating DESC"}
    sql += f"ORDER BY {order_map.get(sort,'p.created_at DESC')} "

    conn = get_db()
    all_rows = conn.execute(sql, params).fetchall()
    total = len(all_rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    products = all_rows[(page-1)*per_page : page*per_page]
    categories = [r[0] for r in conn.execute("SELECT DISTINCT category FROM products WHERE approved=1 ORDER BY category").fetchall()]
    query_string = "&".join(f"{k}={v}" for k,v in request.args.items() if k != "page")
    return render_template("search.html", products=products, categories=categories, total=total,
                           page=page, total_pages=total_pages, query_string=query_string)


# ── PRODUCT PAGE (updated with similar products) ──────────────────────────
# Add `similar` to your existing /p/<int:pid> route:
#   similar = conn.execute(
#       "SELECT * FROM products WHERE category=? AND id!=? AND approved=1 AND stock>0 ORDER BY RANDOM() LIMIT 4",
#       (p["category"], pid)
#   ).fetchall()
#   return render_template("product.html", ..., similar=similar)


# ── ORDERS (buyer) ────────────────────────────────────────────────────────
@app.route("/orders")
@login_required
def orders():
    if session["role"] != "buyer": return redirect("/")
    conn = get_db()
    orders_raw = conn.execute(
        "SELECT * FROM orders WHERE buyer_id=? ORDER BY created_at DESC", (session["user_id"],)
    ).fetchall()
    orders = []
    for o in orders_raw:
        items = conn.execute("SELECT * FROM order_items WHERE order_id=?", (o["id"],)).fetchall()
        orders.append(dict(o, items=items))
    return render_template("orders.html", orders=orders)


@app.route("/orders/cancel/<int:oid>", methods=["POST"])
@login_required
def cancel_order(oid):
    conn = get_db()
    o = conn.execute("SELECT * FROM orders WHERE id=? AND buyer_id=?", (oid, session["user_id"])).fetchone()
    if not o: abort(404)
    if o["status"] in ("shipped","out_for_delivery","delivered","cancelled"):
        flash("Cannot cancel this order.", "err"); return redirect("/orders")
    conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (oid,))
    conn.commit()
    flash("Order cancelled.", "ok"); return redirect("/orders")


# ── RETURN REQUEST ────────────────────────────────────────────────────────
@app.route("/return/<int:item_id>", methods=["GET","POST"])
@login_required
def return_request(item_id):
    conn = get_db()
    item = conn.execute(
        "SELECT oi.*, o.public_id, o.status, o.buyer_id FROM order_items oi "
        "JOIN orders o ON oi.order_id=o.id WHERE oi.id=?", (item_id,)
    ).fetchone()
    if not item or item["buyer_id"] != session["user_id"]: abort(404)
    if request.method == "POST":
        reason = request.form.get("reason","")
        details = request.form.get("details","")
        full_reason = reason + (f" — {details}" if details else "")
        conn.execute("INSERT INTO return_requests (order_item_id, buyer_id, reason, status, created_at, updated_at) VALUES (?,?,?,'pending',datetime('now'),datetime('now'))",
                     (item_id, session["user_id"], full_reason))
        conn.commit()
        flash("Return request submitted.", "ok"); return redirect("/orders")
    return render_template("return_request.html", item=item)


# ── WISHLIST ──────────────────────────────────────────────────────────────
@app.route("/wishlist")
@login_required
def wishlist():
    conn = get_db()
    items = conn.execute(
        "SELECT p.* FROM wishlist_items w JOIN products p ON w.product_id=p.id "
        "WHERE w.user_id=? ORDER BY w.created_at DESC", (session["user_id"],)
    ).fetchall()
    return render_template("wishlist.html", items=items)


# ── SELLER ANALYTICS ──────────────────────────────────────────────────────
@app.route("/seller/analytics")
@login_required
def seller_analytics():
    if session["role"] != "seller": return redirect("/")
    conn = get_db()
    sid = session["user_id"]
    period = request.args.get("period","daily")

    # KPI stats
    stats = {}
    stats["total_revenue"] = conn.execute(
        "SELECT COALESCE(SUM(oi.line_total),0) FROM order_items oi JOIN orders o ON oi.order_id=o.id "
        "WHERE oi.seller_id=? AND o.status!='cancelled'", (sid,)).fetchone()[0]
    stats["month_revenue"] = conn.execute(
        "SELECT COALESCE(SUM(oi.line_total),0) FROM order_items oi JOIN orders o ON oi.order_id=o.id "
        "WHERE oi.seller_id=? AND o.status!='cancelled' AND strftime('%Y-%m',o.created_at)=strftime('%Y-%m','now')", (sid,)).fetchone()[0]
    stats["month_orders"] = conn.execute(
        "SELECT COUNT(DISTINCT oi.order_id) FROM order_items oi JOIN orders o ON oi.order_id=o.id "
        "WHERE oi.seller_id=? AND strftime('%Y-%m',o.created_at)=strftime('%Y-%m','now')", (sid,)).fetchone()[0]
    stats["total_returns"] = conn.execute(
        "SELECT COUNT(*) FROM return_requests rr JOIN order_items oi ON rr.order_item_id=oi.id WHERE oi.seller_id=?", (sid,)).fetchone()[0]
    total_items = conn.execute(
        "SELECT COALESCE(SUM(oi.qty),0) FROM order_items oi JOIN orders o ON oi.order_id=o.id WHERE oi.seller_id=? AND o.status!='cancelled'", (sid,)).fetchone()[0]
    stats["return_rate"] = (stats["total_returns"] / max(1, total_items)) * 100
    stats["avg_rating"] = conn.execute(
        "SELECT COALESCE(AVG(r.rating),0) FROM reviews r JOIN products p ON r.product_id=p.id WHERE p.seller_id=?", (sid,)).fetchone()[0]
    stats["total_reviews"] = conn.execute(
        "SELECT COUNT(*) FROM reviews r JOIN products p ON r.product_id=p.id WHERE p.seller_id=?", (sid,)).fetchone()[0]

    # Chart data — last 30 days
    chart_rows = conn.execute(
        "SELECT strftime('%d %b', o.created_at) as label, SUM(oi.line_total) rev, COUNT(DISTINCT oi.order_id) cnt "
        "FROM order_items oi JOIN orders o ON oi.order_id=o.id "
        "WHERE oi.seller_id=? AND o.status!='cancelled' AND o.created_at>=date('now','-30 days') "
        "GROUP BY date(o.created_at) ORDER BY o.created_at", (sid,)
    ).fetchall()
    chart_data = {
        "labels": [r["label"] for r in chart_rows],
        "revenue": [round(r["rev"],2) for r in chart_rows],
        "orders": [r["cnt"] for r in chart_rows]
    }

    # Top products
    top_raw = conn.execute(
        "SELECT oi.title, SUM(oi.qty) total_qty, SUM(oi.line_total) total_rev "
        "FROM order_items oi JOIN orders o ON oi.order_id=o.id "
        "WHERE oi.seller_id=? AND o.status!='cancelled' GROUP BY oi.product_id ORDER BY total_rev DESC LIMIT 5", (sid,)
    ).fetchall()
    max_rev = max((r["total_rev"] for r in top_raw), default=1)
    top_products = [dict(r, pct=int(r["total_rev"]/max_rev*100)) for r in top_raw]

    # Status distribution
    status_rows = conn.execute(
        "SELECT o.status, COUNT(DISTINCT o.id) cnt FROM order_items oi JOIN orders o ON oi.order_id=o.id "
        "WHERE oi.seller_id=? GROUP BY o.status", (sid,)
    ).fetchall()
    status_data = {"labels": [r["status"] for r in status_rows], "counts": [r["cnt"] for r in status_rows]}

    transactions = conn.execute(
        "SELECT * FROM seller_transactions WHERE seller_id=? ORDER BY created_at DESC LIMIT 20", (sid,)
    ).fetchall()

    return render_template("seller_analytics.html", stats=stats, chart_data=chart_data,
                           top_products=top_products, status_data=status_data,
                           transactions=transactions, chart_period=period)


# ── SELLER RETURNS ────────────────────────────────────────────────────────
@app.route("/seller/returns")
@login_required
def seller_returns():
    if session["role"] != "seller": return redirect("/")
    conn = get_db()
    status = request.args.get("status","")
    sql = ("SELECT rr.*, oi.title, oi.qty, oi.line_total, o.public_id, o.buyer_name, o.phone "
           "FROM return_requests rr JOIN order_items oi ON rr.order_item_id=oi.id "
           "JOIN orders o ON oi.order_id=o.id WHERE oi.seller_id=? ")
    params = [session["user_id"]]
    if status: sql += "AND rr.status=? "; params.append(status)
    sql += "ORDER BY rr.created_at DESC"
    returns = conn.execute(sql, params).fetchall()
    return render_template("seller_returns.html", returns=returns, status=status)


@app.route("/seller/returns/<int:rid>/approve", methods=["POST"])
@login_required
def seller_return_approve(rid):
    conn = get_db()
    conn.execute("UPDATE return_requests SET status='approved', updated_at=datetime('now') WHERE id=?", (rid,))
    conn.commit()
    flash("Return approved.", "ok"); return redirect("/seller/returns")


@app.route("/seller/returns/<int:rid>/reject", methods=["POST"])
@login_required
def seller_return_reject(rid):
    conn = get_db()
    conn.execute("UPDATE return_requests SET status='rejected', updated_at=datetime('now') WHERE id=?", (rid,))
    conn.commit()
    flash("Return rejected.", "ok"); return redirect("/seller/returns")


# ── SELLER BULK UPLOAD ────────────────────────────────────────────────────
@app.route("/seller/bulk-upload", methods=["GET","POST"])
@login_required
def seller_bulk_upload():
    if session["role"] != "seller": return redirect("/")
    results = None
    if request.method == "POST":
        f = request.files.get("csv_file")
        if f:
            import csv, io
            stream = io.StringIO(f.stream.read().decode("utf-8"), newline=None)
            reader = csv.DictReader(stream)
            conn = get_db()
            success, errors, total = 0, [], 0
            for i, row in enumerate(reader, 2):
                total += 1
                try:
                    title = row.get("title","").strip()
                    if not title: raise ValueError("title is required")
                    category = row.get("category","General").strip()
                    price = float(row.get("price",0))
                    stock = int(row.get("stock",0))
                    conn.execute(
                        "INSERT INTO products (seller_id, title, category, description, price, mrp, stock, gst_percent, hsn, size_options, color_options, approved, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,datetime('now'))",
                        (session["user_id"], title, category, row.get("description",""),
                         price, row.get("mrp") or None, stock,
                         float(row.get("gst_percent",18)), row.get("hsn",""),
                         row.get("size_options",""), row.get("color_options",""))
                    )
                    success += 1
                except Exception as e:
                    errors.append({"row": i, "message": str(e)})
            conn.commit()
            results = {"success": success, "errors": errors, "total": total}
    return render_template("seller_bulk_upload.html", results=results)


@app.route("/seller/bulk-upload/template")
@login_required
def seller_bulk_template():
    import io
    output = io.StringIO()
    output.write("title,category,description,price,mrp,stock,gst_percent,hsn,brand,size_options,color_options,image_url\n")
    output.write("Sample T-Shirt,Fashion,A comfortable cotton t-shirt,299,599,50,5,61091000,BrandName,\"S,M,L,XL\",\"Red,Blue,Black\",https://example.com/image.jpg\n")
    from flask import Response
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=xoptime_bulk_template.csv"})


# ── ADMIN RETURNS ─────────────────────────────────────────────────────────
@app.route("/admin/returns")
@login_required
def admin_returns():
    if session["role"] != "admin": return redirect("/")
    conn = get_db()
    status = request.args.get("status","")
    sql = ("SELECT rr.*, oi.title, oi.qty, oi.line_total, o.public_id, o.buyer_name, o.phone, u.name as seller_name "
           "FROM return_requests rr JOIN order_items oi ON rr.order_item_id=oi.id "
           "JOIN orders o ON oi.order_id=o.id JOIN users u ON oi.seller_id=u.id WHERE 1=1 ")
    params = []
    if status: sql += "AND rr.status=? "; params.append(status)
    sql += "ORDER BY rr.created_at DESC"
    returns = conn.execute(sql, params).fetchall()
    return render_template("admin_returns.html", returns=returns, status=status)


@app.route("/admin/returns/<int:rid>/approve", methods=["POST"])
@login_required
def admin_return_approve(rid):
    if session["role"] != "admin": abort(403)
    conn = get_db()
    conn.execute("UPDATE return_requests SET status='approved', updated_at=datetime('now') WHERE id=?", (rid,))
    conn.commit()
    flash("Return approved.", "ok"); return redirect("/admin/returns")


@app.route("/admin/returns/<int:rid>/reject", methods=["POST"])
@login_required
def admin_return_reject(rid):
    if session["role"] != "admin": abort(403)
    conn = get_db()
    conn.execute("UPDATE return_requests SET status='rejected', updated_at=datetime('now') WHERE id=?", (rid,))
    conn.commit()
    flash("Return rejected.", "ok"); return redirect("/admin/returns")


# ── ADMIN SELLERS ─────────────────────────────────────────────────────────
@app.route("/admin/sellers")
@login_required
def admin_sellers():
    if session["role"] != "admin": return redirect("/")
    conn = get_db()
    status = request.args.get("status","")
    sql = ("SELECT u.*, "
           "COUNT(DISTINCT p.id) product_count, "
           "COALESCE(SUM(oi.line_total),0) total_revenue, "
           "COUNT(DISTINCT oi.order_id) order_count, "
           "COALESCE(AVG(r.rating),0) avg_rating, "
           "COALESCE(u.seller_status,'active') seller_status "
           "FROM users u "
           "LEFT JOIN products p ON p.seller_id=u.id "
           "LEFT JOIN order_items oi ON oi.seller_id=u.id "
           "LEFT JOIN reviews r ON r.product_id=p.id "
           "WHERE u.role='seller' ")
    params = []
    if status: sql += "AND COALESCE(u.seller_status,'active')=? "; params.append(status)
    sql += "GROUP BY u.id ORDER BY total_revenue DESC"
    sellers = conn.execute(sql, params).fetchall()
    return render_template("admin_sellers.html", sellers=sellers, status=status)


@app.route("/admin/sellers/<int:uid>/approve", methods=["POST"])
@login_required
def admin_seller_approve(uid):
    if session["role"] != "admin": abort(403)
    conn = get_db()
    conn.execute("UPDATE users SET seller_status='active' WHERE id=?", (uid,))
    conn.commit()
    flash("Seller approved.", "ok"); return redirect("/admin/sellers")


@app.route("/admin/sellers/<int:uid>/suspend", methods=["POST"])
@login_required
def admin_seller_suspend(uid):
    if session["role"] != "admin": abort(403)
    conn = get_db()
    conn.execute("UPDATE users SET seller_status='suspended' WHERE id=?", (uid,))
    conn.commit()
    flash("Seller suspended.", "ok"); return redirect("/admin/sellers")


@app.route("/admin/sellers/<int:uid>/unsuspend", methods=["POST"])
@login_required
def admin_seller_unsuspend(uid):
    if session["role"] != "admin": abort(403)
    conn = get_db()
    conn.execute("UPDATE users SET seller_status='active' WHERE id=?", (uid,))
    conn.commit()
    flash("Seller unsuspended.", "ok"); return redirect("/admin/sellers")


# ── REFERRAL PAGE ─────────────────────────────────────────────────────────
@app.route("/referral")
@login_required
def referral():
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    referred_users = conn.execute(
        "SELECT name, created_at FROM users WHERE referred_by=?", (user["referral_code"],)
    ).fetchall() if user["referral_code"] else []
    stats = {
        "total_referrals": len(referred_users),
        "successful": len(referred_users),
        "earned": len(referred_users) * 50
    }
    return render_template("referral.html", referral_code=user["referral_code"] or "N/A",
                           referred_users=referred_users, stats=stats)


# ── ADMIN DASHBOARD (updated) ─────────────────────────────────────────────
# Update your existing /admin route to pass these extra variables:
#
# top_sellers = conn.execute(
#     "SELECT u.name, SUM(oi.line_total) revenue, COUNT(DISTINCT oi.order_id) orders "
#     "FROM order_items oi JOIN users u ON oi.seller_id=u.id "
#     "JOIN orders o ON oi.order_id=o.id WHERE o.status!='cancelled' "
#     "GROUP BY oi.seller_id ORDER BY revenue DESC LIMIT 5"
# ).fetchall()
# recent_orders = conn.execute(
#     "SELECT * FROM orders ORDER BY created_at DESC LIMIT 10"
# ).fetchall()
# chart data (last 30 days by day):
# chart_rows = conn.execute(
#     "SELECT strftime('%d %b',created_at) label, SUM(total_amount) rev, COUNT(*) cnt "
#     "FROM orders WHERE status!='cancelled' AND created_at>=date('now','-30 days') "
#     "GROUP BY date(created_at) ORDER BY created_at"
# ).fetchall()
# chart_data = {"labels":[r["label"] for r in chart_rows], "revenue":[r["rev"] for r in chart_rows], "orders":[r["cnt"] for r in chart_rows]}
# status_rows = conn.execute("SELECT status, COUNT(*) cnt FROM orders GROUP BY status").fetchall()
# status_data = {"labels":[r["status"] for r in status_rows], "counts":[r["cnt"] for r in status_rows]}
# k["sellers"] = conn.execute("SELECT COUNT(*) FROM users WHERE role='seller'").fetchone()[0]
