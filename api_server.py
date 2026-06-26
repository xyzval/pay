"""
REST API Server - Payment Gateway
Provides API endpoints for merchants to create payments, check status, and receive webhooks.

Endpoints:
  POST   /api/v1/payment/create     - Create a new payment (returns QRIS)
  GET    /api/v1/payment/status/:id  - Check payment status
  POST   /api/v1/payment/cancel/:id  - Cancel a pending payment
  GET    /api/v1/merchant/balance    - Get merchant balance & stats
  GET    /api/v1/merchant/transactions - List merchant transactions

Authentication:
  All endpoints require header: Authorization: Bearer <API_KEY>
"""

import os
import json
import base64
import logging
from datetime import datetime
from typing import Optional
from io import BytesIO

from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from merchant_manager import MerchantManager
from payment_manager import PaymentManager
from qris_converter import static_to_dynamic, generate_qr_image
from webhook_sender import WebhookSender

logger = logging.getLogger(__name__)

# ========================
# PYDANTIC MODELS
# ========================

class CreatePaymentRequest(BaseModel):
    amount: int = Field(..., ge=1000, le=10000000, description="Amount in Rupiah (min 1000, max 10.000.000)")
    product_name: str = Field(default="", max_length=100, description="Product/order description")
    callback_url: str = Field(default="", max_length=500, description="Per-transaction callback URL (optional, overrides default webhook)")
    metadata: str = Field(default="{}", max_length=1000, description="JSON string of extra data (order_id, customer_name, etc)")
    expiry_minutes: int = Field(default=30, ge=5, le=1440, description="Expiry time in minutes (5-1440)")


class CancelPaymentRequest(BaseModel):
    reason: str = Field(default="", max_length=200, description="Cancellation reason")


class CreatePaymentResponse(BaseModel):
    success: bool
    data: dict


class PaymentStatusResponse(BaseModel):
    success: bool
    data: dict


class ErrorResponse(BaseModel):
    success: bool = False
    error: str
    code: str


# ========================
# API APPLICATION
# ========================

app = FastAPI(
    title="QRIS Payment Gateway API",
    description="Payment Gateway API untuk menerima pembayaran via QRIS. Merchant mendaftar, mendapat API key, dan bisa create payment dari bot/website mereka.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global instances (initialized in lifespan)
payment_manager: PaymentManager = None
merchant_manager: MerchantManager = None
webhook_sender: WebhookSender = None
QRIS_STATIC: str = ""


# ========================
# LIFESPAN / STARTUP
# ========================

@app.on_event("startup")
async def startup():
    """Initialize managers on startup."""
    global payment_manager, merchant_manager, webhook_sender, QRIS_STATIC

    from dotenv import load_dotenv
    load_dotenv()

    QRIS_STATIC = os.getenv("QRIS_STATIC", "")
    expiry = int(os.getenv("EXPIRY_MINUTES", "30"))

    payment_manager = PaymentManager(db_path="payments.db", expiry_minutes=expiry)
    await payment_manager.initialize()

    merchant_manager = MerchantManager(db_path="payments.db")
    await merchant_manager.initialize(payment_manager._db)

    webhook_sender = WebhookSender(max_retries=3, timeout=10)

    logger.info("API Server started successfully")


@app.on_event("shutdown")
async def shutdown():
    """Cleanup on shutdown."""
    global payment_manager
    if payment_manager:
        await payment_manager.close()


# ========================
# AUTHENTICATION
# ========================

async def verify_api_key(authorization: str = Header(None)) -> dict:
    """
    Verify API key from Authorization header.
    Format: Authorization: Bearer PAY-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    """
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail={"success": False, "error": "Missing Authorization header", "code": "AUTH_MISSING"}
        )

    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail={"success": False, "error": "Invalid Authorization format. Use: Bearer <API_KEY>", "code": "AUTH_INVALID_FORMAT"}
        )

    api_key = parts[1]
    merchant = await merchant_manager.validate_api_key(api_key)

    if not merchant:
        raise HTTPException(
            status_code=401,
            detail={"success": False, "error": "Invalid or inactive API key", "code": "AUTH_INVALID_KEY"}
        )

    # Log API request
    return merchant


# ========================
# API ENDPOINTS
# ========================

@app.post("/api/v1/payment/create", response_model=CreatePaymentResponse)
async def create_payment(
    body: CreatePaymentRequest,
    request: Request,
    merchant: dict = Depends(verify_api_key)
):
    """
    Create a new QRIS payment.
    
    Returns QR code image (base64) and payment details.
    Merchant's webhook URL will be called when payment is confirmed.
    """
    global payment_manager, merchant_manager, QRIS_STATIC

    if not QRIS_STATIC:
        raise HTTPException(status_code=503, detail={
            "success": False, "error": "Payment gateway not configured", "code": "GATEWAY_NOT_READY"
        })

    try:
        # Create transaction in payment manager
        tx = await payment_manager.create_transaction(
            user_id=0,  # API-created, no Telegram user
            chat_id=0,
            base_amount=body.amount,
            product_name=body.product_name
        )

        # Create merchant transaction (with fee calculation)
        merchant_tx = await merchant_manager.create_merchant_transaction(
            merchant_id=merchant["merchant_id"],
            tx_id=tx["tx_id"],
            amount=tx["unique_amount"],
            callback_url=body.callback_url,
            metadata=body.metadata
        )

        # Convert QRIS static to dynamic
        dynamic_qris = static_to_dynamic(QRIS_STATIC, tx["unique_amount"])

        # Generate QR image as base64
        qr_buffer = generate_qr_image(dynamic_qris)
        qr_base64 = base64.b64encode(qr_buffer.read()).decode('utf-8')

        # Log API request
        client_ip = request.client.host if request.client else ""
        await merchant_manager.log_api_request(
            merchant["merchant_id"], "/api/v1/payment/create", "POST", client_ip, 200
        )

        return {
            "success": True,
            "data": {
                "tx_id": tx["tx_id"],
                "amount": tx["unique_amount"],
                "base_amount": body.amount,
                "fee_amount": merchant_tx["fee_amount"],
                "net_amount": merchant_tx["net_amount"],
                "product_name": body.product_name,
                "qris_string": dynamic_qris,
                "qr_image_base64": qr_base64,
                "status": "pending",
                "expires_at": tx["expires_at"],
                "merchant_id": merchant["merchant_id"],
            }
        }

    except Exception as e:
        logger.error(f"Error creating payment: {e}")
        raise HTTPException(status_code=500, detail={
            "success": False, "error": str(e), "code": "INTERNAL_ERROR"
        })


@app.get("/api/v1/payment/status/{tx_id}", response_model=PaymentStatusResponse)
async def check_payment_status(
    tx_id: str,
    request: Request,
    merchant: dict = Depends(verify_api_key)
):
    """
    Check payment status by transaction ID.
    
    Returns current status: pending, paid, expired, cancelled
    """
    global payment_manager, merchant_manager

    # Get transaction
    tx = await payment_manager.get_transaction(tx_id.upper())
    if not tx:
        raise HTTPException(status_code=404, detail={
            "success": False, "error": "Transaction not found", "code": "TX_NOT_FOUND"
        })

    # Verify merchant owns this transaction
    merchant_tx = await merchant_manager.get_merchant_transaction(tx_id.upper())
    if merchant_tx and merchant_tx["merchant_id"] != merchant["merchant_id"]:
        raise HTTPException(status_code=403, detail={
            "success": False, "error": "Transaction does not belong to this merchant", "code": "TX_FORBIDDEN"
        })

    # Check if expired
    if tx["status"] == "pending":
        expires_at = datetime.fromisoformat(tx["expires_at"])
        if datetime.now() > expires_at:
            await payment_manager.mark_expired_transactions()
            tx["status"] = "expired"

    # Log
    client_ip = request.client.host if request.client else ""
    await merchant_manager.log_api_request(
        merchant["merchant_id"], f"/api/v1/payment/status/{tx_id}", "GET", client_ip, 200
    )

    return {
        "success": True,
        "data": {
            "tx_id": tx["tx_id"],
            "amount": tx["total_amount"],
            "status": tx["status"],
            "product_name": tx["product_name"],
            "created_at": tx["created_at"],
            "expires_at": tx["expires_at"],
            "paid_at": tx.get("paid_at", None),
        }
    }


@app.post("/api/v1/payment/cancel/{tx_id}")
async def cancel_payment(
    tx_id: str,
    body: CancelPaymentRequest = None,
    request: Request = None,
    merchant: dict = Depends(verify_api_key)
):
    """
    Cancel a pending payment.
    
    Only works for pending transactions owned by this merchant.
    """
    global payment_manager, merchant_manager

    # Verify ownership
    merchant_tx = await merchant_manager.get_merchant_transaction(tx_id.upper())
    if not merchant_tx:
        raise HTTPException(status_code=404, detail={
            "success": False, "error": "Transaction not found", "code": "TX_NOT_FOUND"
        })
    if merchant_tx["merchant_id"] != merchant["merchant_id"]:
        raise HTTPException(status_code=403, detail={
            "success": False, "error": "Transaction does not belong to this merchant", "code": "TX_FORBIDDEN"
        })
    if merchant_tx["status"] != "pending":
        raise HTTPException(status_code=400, detail={
            "success": False, "error": f"Transaction already {merchant_tx['status']}", "code": "TX_NOT_PENDING"
        })

    # Cancel in payment manager (user_id=0 for API transactions)
    await payment_manager._db.execute(
        "UPDATE transactions SET status = 'cancelled' WHERE tx_id = ? AND status = 'pending'",
        (tx_id.upper(),)
    )
    await payment_manager._db.execute(
        "UPDATE merchant_transactions SET status = 'cancelled' WHERE tx_id = ? AND status = 'pending'",
        (tx_id.upper(),)
    )
    await payment_manager._db.commit()

    # Log
    client_ip = request.client.host if request.client else ""
    await merchant_manager.log_api_request(
        merchant["merchant_id"], f"/api/v1/payment/cancel/{tx_id}", "POST", client_ip, 200
    )

    return {
        "success": True,
        "data": {
            "tx_id": tx_id.upper(),
            "status": "cancelled",
            "message": "Payment cancelled successfully"
        }
    }


@app.get("/api/v1/merchant/balance")
async def get_merchant_balance(
    request: Request,
    merchant: dict = Depends(verify_api_key)
):
    """
    Get merchant balance and statistics.
    """
    global merchant_manager

    stats = await merchant_manager.get_merchant_stats(merchant["merchant_id"])

    # Log
    client_ip = request.client.host if request.client else ""
    await merchant_manager.log_api_request(
        merchant["merchant_id"], "/api/v1/merchant/balance", "GET", client_ip, 200
    )

    return {
        "success": True,
        "data": {
            "merchant_id": merchant["merchant_id"],
            "name": merchant["name"],
            "fee_percent": merchant["fee_percent"],
            "fee_fixed": merchant["fee_fixed"],
            "stats": stats,
        }
    }


@app.get("/api/v1/merchant/transactions")
async def list_merchant_transactions(
    status: Optional[str] = None,
    limit: int = 50,
    request: Request = None,
    merchant: dict = Depends(verify_api_key)
):
    """
    List merchant's transactions.
    
    Query params:
    - status: filter by status (pending, paid, expired, cancelled)
    - limit: max results (default 50)
    """
    global merchant_manager

    if limit > 100:
        limit = 100

    transactions = await merchant_manager.get_merchant_transactions(
        merchant["merchant_id"], status=status, limit=limit
    )

    # Log
    client_ip = request.client.host if request.client else ""
    await merchant_manager.log_api_request(
        merchant["merchant_id"], "/api/v1/merchant/transactions", "GET", client_ip, 200
    )

    return {
        "success": True,
        "data": {
            "transactions": transactions,
            "count": len(transactions),
        }
    }


# ========================
# HEALTH CHECK
# ========================

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "ok",
        "service": "QRIS Payment Gateway",
        "version": "1.0.0",
        "timestamp": datetime.now().isoformat()
    }


@app.get("/")
async def root():
    """Root endpoint - API info."""
    return {
        "name": "QRIS Payment Gateway API",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
        "description": "Payment Gateway QRIS untuk menerima pembayaran otomatis. Daftar sebagai merchant untuk mendapat API Key."
    }


# ========================
# RUN SERVER
# ========================

def run_api_server(host: str = "0.0.0.0", port: int = 8000):
    """Run the API server standalone."""
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_api_server()
