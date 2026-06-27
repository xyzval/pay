"""
MacroDroid Webhook Server
Menerima notifikasi pembayaran dari HP via MacroDroid,
lalu auto-confirm transaksi yang cocok.

Endpoint:
  POST /callback/notification  - Terima notifikasi dari MacroDroid
  GET  /health                 - Health check

MacroDroid akan kirim HTTP POST ke endpoint ini setiap kali
ada notifikasi pembayaran masuk di app Mitra Bukalapak.
"""

import re
import logging
import asyncio
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

app = FastAPI(title="MacroDroid Webhook Receiver", version="1.0.0")

# Reference to payment_manager (will be set from bot.py)
payment_manager = None
on_payment_confirmed = None  # callback function


class NotificationPayload(BaseModel):
    """Payload yang dikirim oleh MacroDroid."""
    text: str = ""          # Teks notifikasi lengkap
    title: str = ""         # Judul notifikasi
    app: str = ""           # Nama app (Mitra Bukalapak)
    amount: int = 0         # Nominal (jika sudah di-parse di MacroDroid)


def extract_amount_from_text(text: str) -> Optional[int]:
    """
    Extract nominal pembayaran dari teks notifikasi Mitra Bukalapak.
    
    Contoh teks notifikasi:
    - "Pembayaran diterima Rp 50.347 dari JOHN"
    - "Kamu menerima pembayaran Rp50.347"
    - "Transaksi masuk Rp 50,347"
    - "Payment received Rp50347"
    """
    # Pattern untuk menangkap nominal Rupiah
    patterns = [
        r'[Rr]p\.?\s*([0-9][0-9.,]*)',          # Rp 50.347 atau Rp50.347
        r'(\d{1,3}(?:[.,]\d{3})*)\s*(?:rupiah)',  # 50.347 rupiah
        r'sebesar\s*[Rr]p\.?\s*([0-9][0-9.,]*)',  # sebesar Rp 50.347
        r'(\d{4,})',                               # angka 4+ digit langsung
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            amount_str = match.group(1)
            # Hapus titik dan koma sebagai pemisah ribuan
            amount_str = amount_str.replace(".", "").replace(",", "")
            try:
                amount = int(amount_str)
                if 1000 <= amount <= 100000000:  # Range yang masuk akal
                    return amount
            except ValueError:
                continue
    
    return None


@app.post("/callback/notification")
async def receive_notification(request: Request):
    """
    Terima notifikasi dari MacroDroid.
    
    MacroDroid mengirim HTTP POST setiap ada notifikasi pembayaran
    dari Mitra Bukalapak.
    
    Body bisa berupa:
    1. JSON: {"text": "Pembayaran diterima Rp 50.347", "app": "Mitra Bukalapak"}
    2. Plain text: "Pembayaran diterima Rp 50.347"
    3. Form data: text=Pembayaran+diterima+Rp+50.347
    """
    global payment_manager, on_payment_confirmed
    
    # Parse body (support multiple formats dari MacroDroid)
    content_type = request.headers.get("content-type", "")
    text = ""
    amount = 0
    
    try:
        if "application/json" in content_type:
            data = await request.json()
            text = data.get("text", "") or data.get("notification_text", "") or data.get("message", "")
            amount = data.get("amount", 0)
        elif "form" in content_type:
            form = await request.form()
            text = form.get("text", "") or form.get("notification_text", "") or form.get("message", "")
            try:
                amount = int(form.get("amount", 0))
            except (ValueError, TypeError):
                amount = 0
        else:
            # Plain text body
            body = await request.body()
            text = body.decode("utf-8", errors="ignore")
    except Exception as e:
        logger.error(f"Error parsing notification: {e}")
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "Invalid request body"}
        )
    
    if not text and not amount:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "No text or amount provided"}
        )
    
    # Extract amount from text if not provided directly
    if not amount and text:
        amount = extract_amount_from_text(text)
    
    if not amount:
        logger.warning(f"Could not extract amount from: {text[:100]}")
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "error": "Could not extract amount from notification",
                "text_received": text[:200]
            }
        )
    
    logger.info(f"Notification received: Rp {amount:,} | Text: {text[:80]}")
    
    # Try to match with pending transaction
    if not payment_manager:
        return JSONResponse(
            status_code=503,
            content={"success": False, "error": "Payment manager not ready"}
        )
    
    matched_tx = await payment_manager.check_mutation(
        amount=amount,
        sender="MacroDroid Auto",
        reference="macrodroid_notification"
    )
    
    if matched_tx:
        logger.info(f"AUTO-CONFIRMED: TX {matched_tx['tx_id']} = Rp {amount:,}")
        
        # Call the callback to notify user via Telegram
        if on_payment_confirmed:
            try:
                await on_payment_confirmed(matched_tx)
            except Exception as e:
                logger.error(f"Error in payment callback: {e}")
        
        return JSONResponse(content={
            "success": True,
            "message": "Payment confirmed!",
            "tx_id": matched_tx["tx_id"],
            "amount": amount,
        })
    else:
        logger.info(f"No matching transaction for Rp {amount:,}")
        return JSONResponse(content={
            "success": False,
            "message": "No matching pending transaction",
            "amount": amount,
        })


@app.get("/health")
async def health():
    """Health check."""
    return {
        "status": "ok",
        "service": "MacroDroid Webhook Receiver",
        "timestamp": datetime.now().isoformat()
    }


@app.get("/")
async def root():
    """Info endpoint."""
    return {
        "name": "QRIS Auto-Confirm via MacroDroid",
        "endpoint": "POST /callback/notification",
        "health": "GET /health",
        "description": "Kirim notifikasi pembayaran dari MacroDroid ke endpoint ini untuk auto-confirm"
    }
