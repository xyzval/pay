"""
Merchant Manager
Handles merchant registration, API key management, balance, and fee system.
"""

import secrets
import hashlib
import aiosqlite
import logging
from datetime import datetime
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)


class MerchantManager:
    """
    Manages merchants who use this payment gateway.
    
    Each merchant gets:
    - Unique API Key for authentication
    - Webhook URL for payment notifications
    - Balance tracking (fees collected)
    - Transaction history
    """

    def __init__(self, db_path: str = "payments.db"):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self, db: aiosqlite.Connection = None):
        """Initialize merchant tables."""
        if db:
            self._db = db
        else:
            self._db = await aiosqlite.connect(self.db_path)
            await self._db.execute("PRAGMA journal_mode=WAL")

        # Merchants table
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS merchants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                merchant_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                api_key TEXT UNIQUE NOT NULL,
                api_key_hash TEXT NOT NULL,
                webhook_url TEXT DEFAULT '',
                webhook_secret TEXT DEFAULT '',
                fee_percent REAL DEFAULT 1.0,
                fee_fixed INTEGER DEFAULT 0,
                balance INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                telegram_user_id INTEGER DEFAULT 0,
                notes TEXT DEFAULT ''
            )
        """)

        # Merchant transactions (linked to main transactions)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS merchant_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                merchant_id TEXT NOT NULL,
                tx_id TEXT NOT NULL,
                amount INTEGER NOT NULL,
                fee_amount INTEGER DEFAULT 0,
                net_amount INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                webhook_sent INTEGER DEFAULT 0,
                webhook_response TEXT DEFAULT '',
                callback_url TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                paid_at TIMESTAMP,
                FOREIGN KEY (merchant_id) REFERENCES merchants(merchant_id)
            )
        """)

        # API request logs
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS api_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                merchant_id TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                method TEXT DEFAULT 'GET',
                ip_address TEXT DEFAULT '',
                status_code INTEGER DEFAULT 200,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Indexes
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_merchant_api_key 
            ON merchants(api_key_hash)
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_merchant_tx 
            ON merchant_transactions(merchant_id, status)
        """)

        await self._db.commit()
        logger.info("Merchant database tables initialized")

    async def close(self):
        """Close database connection."""
        if self._db:
            await self._db.close()

    # ========================
    # API KEY MANAGEMENT
    # ========================

    def _generate_api_key(self) -> str:
        """Generate a unique API key."""
        # Format: PAY-XXXXXXXXXXXXXXXXXXXXXXXXXXXX (32 chars)
        raw = secrets.token_hex(16)
        return f"PAY-{raw}"

    def _generate_merchant_id(self) -> str:
        """Generate a unique merchant ID."""
        # Format: MCH-XXXXXXXX
        raw = secrets.token_hex(4).upper()
        return f"MCH-{raw}"

    def _hash_api_key(self, api_key: str) -> str:
        """Hash API key for secure storage."""
        return hashlib.sha256(api_key.encode()).hexdigest()

    def _generate_webhook_secret(self) -> str:
        """Generate webhook signing secret."""
        return secrets.token_hex(20)

    async def create_merchant(
        self,
        name: str,
        webhook_url: str = "",
        fee_percent: float = 1.0,
        fee_fixed: int = 0,
        telegram_user_id: int = 0,
        notes: str = ""
    ) -> Dict:
        """
        Register a new merchant and generate API key.
        
        Args:
            name: Merchant business name
            webhook_url: URL to receive payment notifications
            fee_percent: Percentage fee per transaction (default 1%)
            fee_fixed: Fixed fee per transaction in Rupiah (default 0)
            telegram_user_id: Optional Telegram user ID of merchant
            notes: Admin notes
            
        Returns:
            Dict with merchant_id, api_key (only shown once!), and other details
        """
        merchant_id = self._generate_merchant_id()
        api_key = self._generate_api_key()
        api_key_hash = self._hash_api_key(api_key)
        webhook_secret = self._generate_webhook_secret()

        await self._db.execute(
            """INSERT INTO merchants 
               (merchant_id, name, api_key, api_key_hash, webhook_url, 
                webhook_secret, fee_percent, fee_fixed, telegram_user_id, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (merchant_id, name, api_key, api_key_hash, webhook_url,
             webhook_secret, fee_percent, fee_fixed, telegram_user_id, notes)
        )
        await self._db.commit()

        logger.info(f"Created merchant: {merchant_id} ({name})")

        return {
            "merchant_id": merchant_id,
            "name": name,
            "api_key": api_key,  # Only shown ONCE at creation!
            "webhook_url": webhook_url,
            "webhook_secret": webhook_secret,
            "fee_percent": fee_percent,
            "fee_fixed": fee_fixed,
            "is_active": True,
            "created_at": datetime.now().isoformat()
        }

    async def validate_api_key(self, api_key: str) -> Optional[Dict]:
        """
        Validate an API key and return merchant info if valid.
        
        Args:
            api_key: The API key to validate
            
        Returns:
            Merchant dict if valid, None if invalid/inactive
        """
        api_key_hash = self._hash_api_key(api_key)

        async with self._db.execute(
            """SELECT * FROM merchants 
               WHERE api_key_hash = ? AND is_active = 1""",
            (api_key_hash,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                columns = [desc[0] for desc in cursor.description]
                merchant = dict(zip(columns, row))
                # Don't return the raw API key
                merchant.pop("api_key", None)
                return merchant

        return None

    async def revoke_api_key(self, merchant_id: str) -> Optional[str]:
        """
        Revoke old API key and generate a new one.
        
        Returns:
            New API key if successful, None if merchant not found
        """
        merchant = await self.get_merchant(merchant_id)
        if not merchant:
            return None

        new_api_key = self._generate_api_key()
        new_hash = self._hash_api_key(new_api_key)

        await self._db.execute(
            """UPDATE merchants 
               SET api_key = ?, api_key_hash = ?, updated_at = ?
               WHERE merchant_id = ?""",
            (new_api_key, new_hash, datetime.now().isoformat(), merchant_id)
        )
        await self._db.commit()

        logger.info(f"Revoked and regenerated API key for {merchant_id}")
        return new_api_key

    async def deactivate_merchant(self, merchant_id: str) -> bool:
        """Deactivate a merchant (API key becomes invalid)."""
        await self._db.execute(
            """UPDATE merchants SET is_active = 0, updated_at = ?
               WHERE merchant_id = ?""",
            (datetime.now().isoformat(), merchant_id)
        )
        await self._db.commit()
        return True

    async def activate_merchant(self, merchant_id: str) -> bool:
        """Reactivate a merchant."""
        await self._db.execute(
            """UPDATE merchants SET is_active = 1, updated_at = ?
               WHERE merchant_id = ?""",
            (datetime.now().isoformat(), merchant_id)
        )
        await self._db.commit()
        return True

    # ========================
    # MERCHANT CRUD
    # ========================

    async def get_merchant(self, merchant_id: str) -> Optional[Dict]:
        """Get merchant by ID."""
        async with self._db.execute(
            "SELECT * FROM merchants WHERE merchant_id = ?", (merchant_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                columns = [desc[0] for desc in cursor.description]
                return dict(zip(columns, row))
        return None

    async def get_merchant_by_telegram(self, telegram_user_id: int) -> Optional[Dict]:
        """Get merchant by Telegram user ID."""
        async with self._db.execute(
            "SELECT * FROM merchants WHERE telegram_user_id = ? AND is_active = 1",
            (telegram_user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                columns = [desc[0] for desc in cursor.description]
                return dict(zip(columns, row))
        return None

    async def list_merchants(self, active_only: bool = True) -> List[Dict]:
        """List all merchants."""
        query = "SELECT * FROM merchants"
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY created_at DESC"

        async with self._db.execute(query) as cursor:
            rows = await cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    async def update_webhook_url(self, merchant_id: str, webhook_url: str) -> bool:
        """Update merchant's webhook URL."""
        await self._db.execute(
            """UPDATE merchants SET webhook_url = ?, updated_at = ?
               WHERE merchant_id = ?""",
            (webhook_url, datetime.now().isoformat(), merchant_id)
        )
        await self._db.commit()
        return True

    async def update_fee(self, merchant_id: str, fee_percent: float = None, fee_fixed: int = None) -> bool:
        """Update merchant's fee settings."""
        updates = []
        params = []

        if fee_percent is not None:
            updates.append("fee_percent = ?")
            params.append(fee_percent)
        if fee_fixed is not None:
            updates.append("fee_fixed = ?")
            params.append(fee_fixed)

        if not updates:
            return False

        updates.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(merchant_id)

        await self._db.execute(
            f"UPDATE merchants SET {', '.join(updates)} WHERE merchant_id = ?",
            params
        )
        await self._db.commit()
        return True

    # ========================
    # FEE CALCULATION
    # ========================

    def calculate_fee(self, amount: int, fee_percent: float, fee_fixed: int) -> int:
        """
        Calculate fee for a transaction.
        
        Args:
            amount: Transaction amount
            fee_percent: Percentage fee (e.g., 1.0 = 1%)
            fee_fixed: Fixed fee in Rupiah
            
        Returns:
            Total fee amount
        """
        percent_fee = int(amount * fee_percent / 100)
        return percent_fee + fee_fixed

    # ========================
    # MERCHANT TRANSACTIONS
    # ========================

    async def create_merchant_transaction(
        self,
        merchant_id: str,
        tx_id: str,
        amount: int,
        callback_url: str = "",
        metadata: str = "{}"
    ) -> Dict:
        """
        Create a transaction linked to a merchant.
        
        Args:
            merchant_id: The merchant who initiated the payment
            tx_id: Transaction ID from payment_manager
            amount: Transaction amount
            callback_url: Optional per-transaction callback URL
            metadata: JSON string of extra data
            
        Returns:
            Merchant transaction dict
        """
        merchant = await self.get_merchant(merchant_id)
        if not merchant:
            raise ValueError(f"Merchant {merchant_id} not found")

        fee_amount = self.calculate_fee(
            amount, merchant["fee_percent"], merchant["fee_fixed"]
        )
        net_amount = amount - fee_amount

        # Use merchant webhook_url if no per-transaction callback
        final_callback = callback_url or merchant["webhook_url"]

        await self._db.execute(
            """INSERT INTO merchant_transactions 
               (merchant_id, tx_id, amount, fee_amount, net_amount, 
                status, callback_url, metadata)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (merchant_id, tx_id, amount, fee_amount, net_amount,
             final_callback, metadata)
        )
        await self._db.commit()

        return {
            "merchant_id": merchant_id,
            "tx_id": tx_id,
            "amount": amount,
            "fee_amount": fee_amount,
            "net_amount": net_amount,
            "status": "pending",
            "callback_url": final_callback,
        }

    async def mark_merchant_transaction_paid(self, tx_id: str) -> Optional[Dict]:
        """Mark a merchant transaction as paid."""
        await self._db.execute(
            """UPDATE merchant_transactions 
               SET status = 'paid', paid_at = ?
               WHERE tx_id = ? AND status = 'pending'""",
            (datetime.now().isoformat(), tx_id)
        )
        await self._db.commit()

        return await self.get_merchant_transaction(tx_id)

    async def get_merchant_transaction(self, tx_id: str) -> Optional[Dict]:
        """Get merchant transaction by tx_id."""
        async with self._db.execute(
            "SELECT * FROM merchant_transactions WHERE tx_id = ?", (tx_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                columns = [desc[0] for desc in cursor.description]
                return dict(zip(columns, row))
        return None

    async def get_merchant_transactions(
        self, merchant_id: str, status: str = None, limit: int = 50
    ) -> List[Dict]:
        """Get transactions for a merchant."""
        if status:
            query = """SELECT * FROM merchant_transactions 
                      WHERE merchant_id = ? AND status = ?
                      ORDER BY created_at DESC LIMIT ?"""
            params = (merchant_id, status, limit)
        else:
            query = """SELECT * FROM merchant_transactions 
                      WHERE merchant_id = ?
                      ORDER BY created_at DESC LIMIT ?"""
            params = (merchant_id, limit)

        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    async def mark_webhook_sent(self, tx_id: str, response: str = "") -> None:
        """Mark webhook as sent for a transaction."""
        await self._db.execute(
            """UPDATE merchant_transactions 
               SET webhook_sent = 1, webhook_response = ?
               WHERE tx_id = ?""",
            (response, tx_id)
        )
        await self._db.commit()

    # ========================
    # STATISTICS
    # ========================

    async def get_merchant_stats(self, merchant_id: str) -> Dict:
        """Get statistics for a specific merchant."""
        stats = {}

        async with self._db.execute(
            "SELECT COUNT(*) FROM merchant_transactions WHERE merchant_id = ? AND status = 'pending'",
            (merchant_id,)
        ) as cursor:
            stats["pending"] = (await cursor.fetchone())[0]

        async with self._db.execute(
            "SELECT COUNT(*) FROM merchant_transactions WHERE merchant_id = ? AND status = 'paid'",
            (merchant_id,)
        ) as cursor:
            stats["paid"] = (await cursor.fetchone())[0]

        async with self._db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM merchant_transactions WHERE merchant_id = ? AND status = 'paid'",
            (merchant_id,)
        ) as cursor:
            stats["total_volume"] = (await cursor.fetchone())[0]

        async with self._db.execute(
            "SELECT COALESCE(SUM(fee_amount), 0) FROM merchant_transactions WHERE merchant_id = ? AND status = 'paid'",
            (merchant_id,)
        ) as cursor:
            stats["total_fees"] = (await cursor.fetchone())[0]

        async with self._db.execute(
            "SELECT COALESCE(SUM(net_amount), 0) FROM merchant_transactions WHERE merchant_id = ? AND status = 'paid'",
            (merchant_id,)
        ) as cursor:
            stats["total_net"] = (await cursor.fetchone())[0]

        return stats

    async def get_gateway_stats(self) -> Dict:
        """Get overall gateway statistics."""
        stats = {}

        async with self._db.execute(
            "SELECT COUNT(*) FROM merchants WHERE is_active = 1"
        ) as cursor:
            stats["active_merchants"] = (await cursor.fetchone())[0]

        async with self._db.execute(
            "SELECT COUNT(*) FROM merchants"
        ) as cursor:
            stats["total_merchants"] = (await cursor.fetchone())[0]

        async with self._db.execute(
            "SELECT COUNT(*) FROM merchant_transactions WHERE status = 'paid'"
        ) as cursor:
            stats["total_paid_transactions"] = (await cursor.fetchone())[0]

        async with self._db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM merchant_transactions WHERE status = 'paid'"
        ) as cursor:
            stats["total_volume"] = (await cursor.fetchone())[0]

        async with self._db.execute(
            "SELECT COALESCE(SUM(fee_amount), 0) FROM merchant_transactions WHERE status = 'paid'"
        ) as cursor:
            stats["total_fees_earned"] = (await cursor.fetchone())[0]

        return stats

    # ========================
    # API LOGGING
    # ========================

    async def log_api_request(
        self, merchant_id: str, endpoint: str, method: str = "GET",
        ip_address: str = "", status_code: int = 200
    ):
        """Log an API request."""
        await self._db.execute(
            """INSERT INTO api_logs (merchant_id, endpoint, method, ip_address, status_code)
               VALUES (?, ?, ?, ?, ?)""",
            (merchant_id, endpoint, method, ip_address, status_code)
        )
        await self._db.commit()
