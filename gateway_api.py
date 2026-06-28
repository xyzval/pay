"""
Payment Gateway API - Mirip Violet
Merchant daftar → dapat API Key → create payment → auto confirm via Saweria webhook.

Endpoints:
  POST /api/create-payment     - Buat pembayaran baru (return link + nominal)
  GET  /api/check-status/:id   - Cek status pembayaran
  POST /api/cancel/:id         - Batalkan pembayaran
  GET  /api/balance            - Statistik merchant

Auth: Header → Authorization: Bearer API_KEY
"""

import re
import secrets
import hashlib
import logging
import aiosqlite
from datetime import datetime
from typing import Optional, Dict, List

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

app = FastAPI(title="QRIS Payment Gateway API", version="1.0.0", docs_url="/docs")

# Set from bot.py
payment_manager = None
on_payment_confirmed = None
SAWERIA_USERNAME = "nvatryn"
db = None


# ========================
# MODELS
# ========================

class CreatePaymentRequest(BaseModel):
    amount: int = Field(..., ge=1, le=10000000, description="Nominal (Rp)")
    product_name: str = Field(default="", max_length=100)
    callback_url: str = Field(default="", max_length=500)
    customer_name: str = Field(default="", max_length=100)
    order_id: str = Field(default="", max_length=100)


# ========================
# DATABASE (Merchants)
# ========================

async def init_merchant_db():
    """Create merchant table."""
    global db
    await db.execute("""
        CREATE TABLE IF NOT EXISTS merchants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merchant_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            api_key_hash TEXT NOT NULL,
            callback_url TEXT DEFAULT '',
            fee_percent REAL DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS merchant_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merchant_id TEXT NOT NULL,
            tx_id TEXT NOT NULL,
            amount INTEGER NOT NULL,
            product_name TEXT DEFAULT '',
            callback_url TEXT DEFAULT '',
            order_id TEXT DEFAULT '',
            customer_name TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            webhook_sent INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.commit()


def generate_api_key() -> str:
    return f"PAY-{secrets.token_hex(16)}"


def generate_merchant_id() -> str:
    return f"MCH-{secrets.token_hex(4).upper()}"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def create_merchant(name: str, callback_url: str = "", fee: float = 0) -> Dict:
    """Register new merchant."""
    global db
    mid = generate_merchant_id()
    api_key = generate_api_key()
    key_hash = hash_key(api_key)

    await db.execute(
        "INSERT INTO merchants (merchant_id, name, api_key_hash, callback_url, fee_percent) VALUES (?,?,?,?,?)",
        (mid, name, key_hash, callback_url, fee)
    )
    await db.commit()
    return {"merchant_id": mid, "api_key": api_key, "name": name, "callback_url": callback_url}


async def validate_key(api_key: str) -> Optional[Dict]:
    """Validate API key."""
    global db
    key_hash = hash_key(api_key)
    async with db.execute(
        "SELECT * FROM merchants WHERE api_key_hash = ? AND is_active = 1", (key_hash,)
    ) as cursor:
        row = await cursor.fetchone()
        if row:
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))
    return None


async def get_merchant_payments(merchant_id: str, limit: int = 50) -> List[Dict]:
    """Get merchant payment history."""
    global db
    async with db.execute(
        "SELECT * FROM merchant_payments WHERE merchant_id = ? ORDER BY created_at DESC LIMIT ?",
        (merchant_id, limit)
    ) as cursor:
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, r)) for r in rows]


async def get_merchant_stats(merchant_id: str) -> Dict:
    """Get merchant statistics."""
    global db
    stats = {}
    async with db.execute(
        "SELECT COUNT(*) FROM merchant_payments WHERE merchant_id = ? AND status = 'pending'",
        (merchant_id,)
    ) as c:
        stats["pending"] = (await c.fetchone())[0]
    async with db.execute(
        "SELECT COUNT(*) FROM merchant_payments WHERE merchant_id = ? AND status = 'paid'",
        (merchant_id,)
    ) as c:
        stats["paid"] = (await c.fetchone())[0]
    async with db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM merchant_payments WHERE merchant_id = ? AND status = 'paid'",
        (merchant_id,)
    ) as c:
        stats["total_volume"] = (await c.fetchone())[0]
    return stats


# ========================
# AUTH
# ========================

async def auth(authorization: str = Header(None)) -> Dict:
    if not authorization:
        raise HTTPException(status_code=401, detail={"success": False, "error": "Missing API Key"})
    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail={"success": False, "error": "Format: Bearer API_KEY"})
    merchant = await validate_key(parts[1])
    if not merchant:
        raise HTTPException(status_code=401, detail={"success": False, "error": "Invalid API Key"})
    return merchant


# ========================
# API ENDPOINTS
# ========================

@app.post("/api/create-payment")
async def api_create_payment(body: CreatePaymentRequest, request: Request, authorization: str = Header(None)):
    """Buat pembayaran baru. Return link Saweria + nominal unik."""
    merchant = await auth(authorization)
    global payment_manager, SAWERIA_USERNAME, db

    if not payment_manager:
        raise HTTPException(status_code=503, detail={"success": False, "error": "Not ready"})

    # Create transaction
    tx = await payment_manager.create_transaction(
        user_id=0, chat_id=0,
        base_amount=body.amount,
        product_name=body.product_name
    )

    # Save merchant payment
    callback = body.callback_url or merchant.get("callback_url", "")
    await db.execute(
        """INSERT INTO merchant_payments
           (merchant_id, tx_id, amount, product_name, callback_url, order_id, customer_name)
           VALUES (?,?,?,?,?,?,?)""",
        (merchant["merchant_id"], tx["tx_id"], tx["unique_amount"],
         body.product_name, callback, body.order_id, body.customer_name)
    )
    await db.commit()

    return {
        "success": True,
        "data": {
            "tx_id": tx["tx_id"],
            "amount": tx["unique_amount"],
            "base_amount": body.amount,
            "payment_link": f"https://saweria.co/{SAWERIA_USERNAME}",
            "payment_info": f"Bayar TEPAT Rp {tx['unique_amount']:,} di link tersebut",
            "product_name": body.product_name,
            "order_id": body.order_id,
            "status": "pending",
            "expires_at": tx["expires_at"],
        }
    }


@app.get("/api/check-status/{tx_id}")
async def api_check_status(tx_id: str, authorization: str = Header(None)):
    """Cek status pembayaran."""
    merchant = await auth(authorization)
    global payment_manager

    tx = await payment_manager.get_transaction(tx_id.upper())
    if not tx:
        raise HTTPException(status_code=404, detail={"success": False, "error": "Not found"})

    return {
        "success": True,
        "data": {
            "tx_id": tx["tx_id"],
            "amount": tx["total_amount"],
            "status": tx["status"],
            "product_name": tx["product_name"],
            "created_at": tx["created_at"],
            "paid_at": tx.get("paid_at"),
        }
    }


@app.post("/api/cancel/{tx_id}")
async def api_cancel(tx_id: str, authorization: str = Header(None)):
    """Batalkan pembayaran pending."""
    merchant = await auth(authorization)
    global payment_manager, db

    tx = await payment_manager.get_transaction(tx_id.upper())
    if not tx:
        raise HTTPException(status_code=404, detail={"success": False, "error": "Not found"})
    if tx["status"] != "pending":
        raise HTTPException(status_code=400, detail={"success": False, "error": f"Already {tx['status']}"})

    await payment_manager._db.execute(
        "UPDATE transactions SET status = 'cancelled' WHERE tx_id = ?", (tx_id.upper(),)
    )
    await payment_manager._db.commit()

    return {"success": True, "data": {"tx_id": tx_id.upper(), "status": "cancelled"}}


@app.get("/api/balance")
async def api_balance(authorization: str = Header(None)):
    """Statistik merchant."""
    merchant = await auth(authorization)
    stats = await get_merchant_stats(merchant["merchant_id"])
    return {
        "success": True,
        "data": {
            "merchant_id": merchant["merchant_id"],
            "name": merchant["name"],
            **stats
        }
    }


@app.get("/api/transactions")
async def api_transactions(authorization: str = Header(None), limit: int = 50):
    """List transaksi merchant."""
    merchant = await auth(authorization)
    payments = await get_merchant_payments(merchant["merchant_id"], limit)
    return {"success": True, "data": {"transactions": payments, "count": len(payments)}}


# ========================
# SAWERIA WEBHOOK
# ========================

@app.post("/callback/saweria")
async def saweria_callback(request: Request):
    """Terima webhook dari Saweria → auto confirm."""
    global payment_manager, on_payment_confirmed, db

    try:
        data = await request.json()
    except:
        try:
            body = await request.body()
            data = {"raw": body.decode()}
        except:
            return JSONResponse(status_code=400, content={"error": "Invalid"})

    logger.info(f"Saweria webhook: {data}")

    # Extract amount
    amount = 0
    for key in ["amount", "total", "amount_raw", "value", "nominal"]:
        if key in data:
            try:
                amount = int(re.sub(r'[^\d]', '', str(data[key])))
                if amount > 0:
                    break
            except:
                continue

    if not amount:
        return JSONResponse(content={"success": False, "error": "No amount"})

    if not payment_manager:
        return JSONResponse(status_code=503, content={"error": "Not ready"})

    # Match & confirm
    sender = data.get("donator_name") or data.get("name") or "Saweria"
    matched = await payment_manager.check_mutation(amount=amount, sender=sender, reference="saweria")

    if matched:
        logger.info(f"AUTO-CONFIRM: {matched['tx_id']} = Rp {amount:,}")

        # Notify via Telegram
        if on_payment_confirmed:
            try:
                await on_payment_confirmed(matched)
            except Exception as e:
                logger.error(f"Callback error: {e}")

        # Send webhook to merchant
        await _send_merchant_callback(matched["tx_id"], amount)

        return JSONResponse(content={"success": True, "tx_id": matched["tx_id"]})

    return JSONResponse(content={"success": False, "message": "No match", "amount": amount})


async def _send_merchant_callback(tx_id: str, amount: int):
    """Send callback to merchant's URL."""
    global db
    import aiohttp

    async with db.execute(
        "SELECT * FROM merchant_payments WHERE tx_id = ?", (tx_id,)
    ) as cursor:
        row = await cursor.fetchone()
        if not row:
            return
        cols = [d[0] for d in cursor.description]
        mp = dict(zip(cols, row))

    if not mp.get("callback_url"):
        return

    # Update status
    await db.execute(
        "UPDATE merchant_payments SET status = 'paid', webhook_sent = 1 WHERE tx_id = ?",
        (tx_id,)
    )
    await db.commit()

    # Send callback
    payload = {
        "event": "payment.success",
        "data": {
            "tx_id": tx_id,
            "amount": amount,
            "product_name": mp.get("product_name", ""),
            "order_id": mp.get("order_id", ""),
            "customer_name": mp.get("customer_name", ""),
            "status": "paid",
            "paid_at": datetime.now().isoformat(),
        }
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(mp["callback_url"], json=payload, timeout=10) as resp:
                logger.info(f"Merchant callback sent: {resp.status}")
    except Exception as e:
        logger.error(f"Merchant callback failed: {e}")


# ========================
# HEALTH & INFO
# ========================

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.get("/")
async def root():
    return {
        "name": "QRIS Payment Gateway",
        "version": "1.0.0",
        "docs": "/docs",
        "endpoints": {
            "create_payment": "POST /api/create-payment",
            "check_status": "GET /api/check-status/{tx_id}",
            "cancel": "POST /api/cancel/{tx_id}",
            "balance": "GET /api/balance",
            "transactions": "GET /api/transactions",
            "saweria_webhook": "POST /callback/saweria",
        }
    }
