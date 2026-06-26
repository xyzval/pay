"""
Webhook Sender
Sends payment notifications to merchant callback URLs.
Includes retry logic, signature verification, and logging.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Optional, Dict

import aiohttp

logger = logging.getLogger(__name__)


class WebhookSender:
    """
    Sends webhook notifications to merchants when payments are confirmed.
    
    Features:
    - HMAC-SHA256 signature for payload verification
    - Retry with exponential backoff (3 attempts)
    - Timeout handling
    - Response logging
    """

    def __init__(self, max_retries: int = 3, timeout: int = 10):
        """
        Args:
            max_retries: Maximum retry attempts
            timeout: Request timeout in seconds
        """
        self.max_retries = max_retries
        self.timeout = timeout

    def _generate_signature(self, payload: str, secret: str) -> str:
        """
        Generate HMAC-SHA256 signature for webhook payload.
        Merchant can verify this to ensure payload authenticity.
        
        Args:
            payload: JSON string of the webhook body
            secret: Merchant's webhook secret
            
        Returns:
            Hex-encoded HMAC-SHA256 signature
        """
        return hmac.new(
            secret.encode('utf-8'),
            payload.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

    def build_payload(self, transaction: Dict, event: str = "payment.success") -> Dict:
        """
        Build webhook payload.
        
        Args:
            transaction: Transaction data dict
            event: Event type (payment.success, payment.expired, payment.cancelled)
            
        Returns:
            Webhook payload dict
        """
        return {
            "event": event,
            "data": {
                "tx_id": transaction.get("tx_id", ""),
                "merchant_id": transaction.get("merchant_id", ""),
                "amount": transaction.get("amount", 0),
                "fee_amount": transaction.get("fee_amount", 0),
                "net_amount": transaction.get("net_amount", 0),
                "status": transaction.get("status", ""),
                "paid_at": transaction.get("paid_at", ""),
                "metadata": transaction.get("metadata", "{}"),
            },
            "timestamp": int(time.time()),
        }

    async def send(
        self,
        url: str,
        transaction: Dict,
        webhook_secret: str = "",
        event: str = "payment.success"
    ) -> Dict:
        """
        Send webhook notification to merchant.
        
        Args:
            url: Merchant's webhook/callback URL
            transaction: Transaction data
            webhook_secret: Secret for signing the payload
            event: Event type
            
        Returns:
            Dict with success status, response code, and body
        """
        if not url:
            logger.warning(f"No webhook URL for tx {transaction.get('tx_id')}")
            return {"success": False, "error": "No webhook URL configured"}

        # Build payload
        payload = self.build_payload(transaction, event)
        payload_json = json.dumps(payload, separators=(',', ':'))

        # Generate signature
        signature = ""
        if webhook_secret:
            signature = hmac.new(
                webhook_secret.encode('utf-8'),
                payload_json.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()

        # Headers
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "QRISPayGateway/1.0",
            "X-Webhook-Event": event,
            "X-Webhook-Timestamp": str(payload["timestamp"]),
        }
        if signature:
            headers["X-Webhook-Signature"] = signature

        # Retry loop
        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url,
                        data=payload_json,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=self.timeout)
                    ) as response:
                        response_body = await response.text()
                        
                        if 200 <= response.status < 300:
                            logger.info(
                                f"Webhook sent successfully to {url} "
                                f"(tx: {transaction.get('tx_id')}, "
                                f"status: {response.status})"
                            )
                            return {
                                "success": True,
                                "status_code": response.status,
                                "response": response_body[:500],
                                "attempts": attempt,
                            }
                        else:
                            last_error = f"HTTP {response.status}: {response_body[:200]}"
                            logger.warning(
                                f"Webhook attempt {attempt}/{self.max_retries} "
                                f"failed: {last_error}"
                            )

            except asyncio.TimeoutError:
                last_error = f"Timeout after {self.timeout}s"
                logger.warning(
                    f"Webhook attempt {attempt}/{self.max_retries} "
                    f"timeout for {url}"
                )
            except aiohttp.ClientError as e:
                last_error = f"Connection error: {str(e)}"
                logger.warning(
                    f"Webhook attempt {attempt}/{self.max_retries} "
                    f"error: {last_error}"
                )
            except Exception as e:
                last_error = f"Unexpected error: {str(e)}"
                logger.error(f"Webhook unexpected error: {e}")

            # Exponential backoff before retry
            if attempt < self.max_retries:
                wait_time = 2 ** attempt  # 2s, 4s, 8s
                await asyncio.sleep(wait_time)

        # All retries failed
        logger.error(
            f"Webhook FAILED after {self.max_retries} attempts "
            f"for tx {transaction.get('tx_id')}: {last_error}"
        )
        return {
            "success": False,
            "error": last_error,
            "attempts": self.max_retries,
        }

    async def send_payment_success(self, url: str, transaction: Dict, webhook_secret: str = "") -> Dict:
        """Convenience method for payment success webhook."""
        return await self.send(url, transaction, webhook_secret, "payment.success")

    async def send_payment_expired(self, url: str, transaction: Dict, webhook_secret: str = "") -> Dict:
        """Convenience method for payment expired webhook."""
        return await self.send(url, transaction, webhook_secret, "payment.expired")

    async def send_payment_cancelled(self, url: str, transaction: Dict, webhook_secret: str = "") -> Dict:
        """Convenience method for payment cancelled webhook."""
        return await self.send(url, transaction, webhook_secret, "payment.cancelled")
