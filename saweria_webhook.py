"""
Saweria Webhook Receiver
Menerima webhook dari Saweria saat ada donasi/pembayaran masuk.
Auto-match nominal dengan transaksi pending → auto confirm.

Endpoint:
  POST /callback/saweria  - Webhook dari Saweria
  GET  /health            - Health check
"""

import re
import logging
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

app = FastAPI(title="Saweria Webhook Receiver", version="1.0.0")

# Set from bot.py
payment_manager = None
on_payment_confirmed = None


def extract_amount(data: dict) -> Optional[int]:
    """Extract amount dari webhook payload Saweria."""
    # Saweria webhook biasanya kirim field 'amount' atau 'amount_raw'
    amount = data.get("amount") or data.get("amount_raw") or data.get("total") or 0

    if isinstance(amount, str):
        # Hapus karakter non-angka
        amount = re.sub(r'[^\d]', '', amount)
        try:
            amount = int(amount)
        except ValueError:
            amount = 0

    if isinstance(amount, float):
        amount = int(amount)

    return amount if amount > 0 else None


@app.post("/callback/saweria")
async def saweria_callback(request: Request):
    """
    Terima webhook dari Saweria.

    Saweria mengirim POST request saat ada donasi masuk.
    Payload biasanya berisi: amount, donator_name, message, dll.
    """
    global payment_manager, on_payment_confirmed

    try:
        data = await request.json()
    except Exception:
        try:
            body = await request.body()
            data = {"raw": body.decode("utf-8", errors="ignore")}
        except:
            return JSONResponse(status_code=400, content={"error": "Invalid body"})

    logger.info(f"Saweria webhook received: {data}")

    # Extract amount
    amount = extract_amount(data)

    if not amount:
        # Coba cari dari field lain
        for key in ["amount", "total", "amount_raw", "value", "nominal"]:
            if key in data:
                try:
                    amount = int(re.sub(r'[^\d]', '', str(data[key])))
                    if amount > 0:
                        break
                except:
                    continue

    if not amount:
        logger.warning(f"No amount found in webhook: {data}")
        return JSONResponse(content={"success": False, "error": "No amount found"})

    if not payment_manager:
        return JSONResponse(status_code=503, content={"error": "Not ready"})

    # Match dengan pending transaction
    sender = data.get("donator_name") or data.get("name") or data.get("sender") or "Saweria"
    matched_tx = await payment_manager.check_mutation(
        amount=amount,
        sender=sender,
        reference="saweria_webhook"
    )

    if matched_tx:
        logger.info(f"SAWERIA AUTO-CONFIRM: {matched_tx['tx_id']} = Rp {amount:,}")

        if on_payment_confirmed:
            try:
                await on_payment_confirmed(matched_tx)
            except Exception as e:
                logger.error(f"Callback error: {e}")

        return JSONResponse(content={
            "success": True,
            "tx_id": matched_tx["tx_id"],
            "amount": amount
        })
    else:
        logger.info(f"Saweria: Rp {amount:,} - no matching transaction")
        return JSONResponse(content={
            "success": False,
            "message": "No matching transaction",
            "amount": amount
        })


@app.get("/health")
async def health():
    return {"status": "ok", "service": "Saweria Webhook", "timestamp": datetime.now().isoformat()}


@app.get("/")
async def root():
    return {
        "name": "Saweria Webhook Receiver",
        "endpoint": "POST /callback/saweria",
        "status": "active"
    }
