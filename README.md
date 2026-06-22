# Xoptime — Setup Guide

## Quick Start (5 minutes)

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Setup environment
```bash
cp .env.example .env
# .env file mein apni values bharo (SECRET_KEY zaroori hai)
```

### 3. Run karo
```bash
python app.py
```

Browser mein kholo: **http://localhost:5000**

---

## Default Admin Login
- **Email:** admin@xoptime.com
- **Password:** admin123
- ⚠️ Production mein yeh zaroor change karo!

---

## .env mein kya fill karna hai

| Variable | Zaroori? | Description |
|---|---|---|
| `SECRET_KEY` | ✅ Haan | Koi bhi random string (30+ chars) |
| `RAZORPAY_KEY_ID` | Online payment ke liye | Razorpay dashboard se |
| `RAZORPAY_KEY_SECRET` | Online payment ke liye | Razorpay dashboard se |
| `PLATFORM_COMMISSION` | Optional | Default: 10% |
| `SMTP_*` | Email ke liye | Gmail app password use karo |

---

## Folder Structure
```
xoptime_fixed/
├── app.py              ← Main Flask application (yahi run karo)
├── database.db         ← SQLite database (auto-create hoga)
├── .env                ← Apni credentials (git mein mat daalo!)
├── .env.example        ← Template
├── requirements.txt    ← Python packages
├── templates/          ← HTML templates
├── static/
│   ├── style.css
│   ├── uploads/        ← Product images yahan save honge
│   └── ...
```

---

## Features Jo Add Ki Gayi Hain

### Authentication
- ✅ Login / Register / Logout
- ✅ Forgot Password (email se reset)
- ✅ Change Password
- ✅ CSRF protection on all forms
- ✅ Referral code system

### Buyer Features
- ✅ Homepage with products
- ✅ Search with filters
- ✅ Product detail page
- ✅ Cart (add/update/remove)
- ✅ Checkout with COD + Razorpay + UPI
- ✅ Razorpay payment verification
- ✅ Coupon code apply
- ✅ Order management (view/cancel)
- ✅ Return requests
- ✅ Wishlist
- ✅ Reviews (delivered orders only)
- ✅ Notifications
- ✅ Support tickets

### Seller Features
- ✅ Dashboard with KPIs
- ✅ Product add/edit/delete with image upload
- ✅ Bulk CSV upload
- ✅ Order management
- ✅ Analytics with charts
- ✅ Return requests management
- ✅ Profile + Bank/KYC details

### Admin Features
- ✅ Dashboard with full stats + charts
- ✅ Order management + tracking update
- ✅ Product approval/unapproval
- ✅ User management
- ✅ Seller management (approve/suspend)
- ✅ Return requests
- ✅ Coupon management
- ✅ Support ticket replies
- ✅ Seller payout tracking
- ✅ Invoice + Shipping label print

---

## Production Deployment Tips

1. `DEBUG=False` karo
2. `SECRET_KEY` aur Razorpay live keys use karo
3. Proper SMTP setup karo
4. SQLite ki jagah PostgreSQL use karo (migrate_sqlite_to_postgres.py available hai)
5. Nginx + Gunicorn ya uWSGI use karo
6. Static files ke liye CDN lagao

---

## Razorpay Setup

1. [razorpay.com](https://razorpay.com) pe account banao
2. Dashboard > Settings > API Keys se Key ID aur Secret lo
3. `.env` mein paste karo
4. Testing ke liye `rzp_test_` wali keys use karo

Test cards:
- Card: `4111 1111 1111 1111`
- Expiry: koi bhi future date
- CVV: koi bhi 3 digits
- OTP: `1234`
