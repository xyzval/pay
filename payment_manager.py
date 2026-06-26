"""
Payment Manager
Handles transaction creation, unique amount generation, mutation checking,
and payment verification for QRIS payments via Mitra Bukalapak.
"""

import asyncio
import aiosqlite
import time
import random
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

logger = logging.getLogger(__name__)

# Transaction statuses
STATUS_PENDING = "pending"
STATUS_PAID = "paid"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"


class PaymentManager:
    """
    Manages QRIS payments with unique amount tracking and auto-verification.
    
    Flow:
    1. User requests payment → generate unique amount (base + unique suffix)
    2. Bot sends QRIS with that exact amount
    3. Background task checks mutations periodically
    4. When matching amount found → mark as paid → notify user
    """

    def __init__(self, db_path: str = "payments.db", expiry_minutes: int = 30):
        self.db_path = db_path
        self.expiry_minutes = expiry_minutes
        self._db: Optional[aiosqlite.Connection] = None
        self._checking = False
        self._callbacks: Dict[str, callable] = {}

    async def initialize(self):
        """Initialize database and create tables."""
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_id TEXT UNIQUE NOT NULL,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                base_amount INTEGER NOT NULL,
                unique_amount INTEGER NOT NULL,
                total_amount INTEGER NOT NULL,
                product_name TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                paid_at TIMESTAMP,
                notes TEXT DEFAULT ''
            )
        """)
        
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS mutations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount INTEGER NOT NULL,
                sender TEXT DEFAULT '',
                reference TEXT DEFAULT '',
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                matched_tx_id TEXT DEFAULT '',
                raw_data TEXT DEFAULT ''
            )
        """)
        
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tx_status ON transactions(status)
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tx_unique_amount ON transactions(unique_amount, status)
        """)
        
        await self._db.commit()
        logger.info("Payment database initialized successfully")

    async def close(self):
        """Close database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    def generate_unique_amount(self, base_amount: int) -> int:
        """
        Generate a unique amount by adding a random suffix (1-999) to the base amount.
        This makes each transaction identifiable by its exact amount.
        
        Example: base_amount = 50000 → could become 50123, 50456, etc.
        """
        suffix = random.randint(1, 999)
        return base_amount + suffix

    async def _is_amount_in_use(self, amount: int) -> bool:
        """Check if a unique amount is already being used by a pending transaction."""
        async with self._db.execute(
            "SELECT COUNT(*) FROM transactions WHERE unique_amount = ? AND status = ?",
            (amount, STATUS_PENDING)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] > 0

    async def create_transaction(
        self,
        user_id: int,
        chat_id: int,
        base_amount: int,
        product_name: str = ""
    ) -> Dict:
        """
        Create a new payment transaction with a unique amount.
        
        Args:
            user_id: Telegram user ID
            chat_id: Telegram chat ID
            base_amount: Base price in Rupiah
            product_name: Optional product/service name
            
        Returns:
            Transaction dict with tx_id, unique_amount, total_amount, expires_at
        """
        # Generate unique amount that's not already in use
        max_attempts = 50
        unique_amount = 0
        
        for _ in range(max_attempts):
            unique_amount = self.generate_unique_amount(base_amount)
            if not await self._is_amount_in_use(unique_amount):
                break
        else:
            # Fallback: use timestamp-based suffix
            unique_amount = base_amount + (int(time.time()) % 1000)

        # Generate transaction ID
        tx_id = f"TX{int(time.time())}{random.randint(100, 999)}"
        
        # Calculate expiry
        expires_at = datetime.now() + timedelta(minutes=self.expiry_minutes)
        
        await self._db.execute(
            """INSERT INTO transactions 
               (tx_id, user_id, chat_id, base_amount, unique_amount, total_amount, 
                product_name, status, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (tx_id, user_id, chat_id, base_amount, unique_amount, unique_amount,
             product_name, STATUS_PENDING, expires_at.isoformat())
        )
        await self._db.commit()
        
        logger.info(f"Created transaction {tx_id}: Rp {unique_amount:,} for user {user_id}")
        
        return {
            "tx_id": tx_id,
            "user_id": user_id,
            "chat_id": chat_id,
            "base_amount": base_amount,
            "unique_amount": unique_amount,
            "total_amount": unique_amount,
            "product_name": product_name,
            "status": STATUS_PENDING,
            "expires_at": expires_at.isoformat(),
        }

    async def get_transaction(self, tx_id: str) -> Optional[Dict]:
        """Get transaction details by tx_id."""
        async with self._db.execute(
            "SELECT * FROM transactions WHERE tx_id = ?", (tx_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                columns = [desc[0] for desc in cursor.description]
                return dict(zip(columns, row))
        return None

    async def get_pending_transactions(self) -> List[Dict]:
        """Get all pending (not expired) transactions."""
        now = datetime.now().isoformat()
        async with self._db.execute(
            """SELECT * FROM transactions 
               WHERE status = ? AND expires_at > ?
               ORDER BY created_at DESC""",
            (STATUS_PENDING, now)
        ) as cursor:
            rows = await cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    async def get_user_transactions(self, user_id: int, limit: int = 10) -> List[Dict]:
        """Get recent transactions for a user."""
        async with self._db.execute(
            """SELECT * FROM transactions 
               WHERE user_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (user_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    async def mark_as_paid(self, tx_id: str) -> bool:
        """Mark a transaction as paid."""
        await self._db.execute(
            """UPDATE transactions 
               SET status = ?, paid_at = ?
               WHERE tx_id = ? AND status = ?""",
            (STATUS_PAID, datetime.now().isoformat(), tx_id, STATUS_PENDING)
        )
        await self._db.commit()
        
        # Check if update was successful
        tx = await self.get_transaction(tx_id)
        if tx and tx["status"] == STATUS_PAID:
            logger.info(f"Transaction {tx_id} marked as PAID")
            return True
        return False

    async def mark_expired_transactions(self) -> List[str]:
        """Mark all expired pending transactions. Returns list of expired tx_ids."""
        now = datetime.now().isoformat()
        
        # Get expired transactions first
        async with self._db.execute(
            """SELECT tx_id, user_id, chat_id FROM transactions 
               WHERE status = ? AND expires_at <= ?""",
            (STATUS_PENDING, now)
        ) as cursor:
            expired = await cursor.fetchall()
        
        if expired:
            await self._db.execute(
                """UPDATE transactions 
                   SET status = ?
                   WHERE status = ? AND expires_at <= ?""",
                (STATUS_EXPIRED, STATUS_PENDING, now)
            )
            await self._db.commit()
            
            expired_ids = [row[0] for row in expired]
            logger.info(f"Expired {len(expired_ids)} transactions: {expired_ids}")
            return expired_ids
        
        return []

    async def cancel_transaction(self, tx_id: str, user_id: int) -> bool:
        """Cancel a pending transaction (only by the owner)."""
        await self._db.execute(
            """UPDATE transactions 
               SET status = ?
               WHERE tx_id = ? AND user_id = ? AND status = ?""",
            (STATUS_CANCELLED, tx_id, user_id, STATUS_PENDING)
        )
        await self._db.commit()
        
        tx = await self.get_transaction(tx_id)
        return tx is not None and tx["status"] == STATUS_CANCELLED

    async def check_mutation(self, amount: int, sender: str = "", reference: str = "") -> Optional[Dict]:
        """
        Check if a mutation (incoming payment) matches any pending transaction.
        
        This is called when a new mutation is detected from the e-wallet/bank.
        
        Args:
            amount: The exact amount received
            sender: Sender name/info (optional)
            reference: Transaction reference (optional)
            
        Returns:
            Matched transaction dict if found, None otherwise
        """
        # Record the mutation
        await self._db.execute(
            """INSERT INTO mutations (amount, sender, reference)
               VALUES (?, ?, ?)""",
            (amount, sender, reference)
        )
        await self._db.commit()
        
        # Try to match with a pending transaction
        now = datetime.now().isoformat()
        async with self._db.execute(
            """SELECT * FROM transactions 
               WHERE unique_amount = ? AND status = ? AND expires_at > ?
               ORDER BY created_at ASC
               LIMIT 1""",
            (amount, STATUS_PENDING, now)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                columns = [desc[0] for desc in cursor.description]
                tx = dict(zip(columns, row))
                
                # Mark as paid
                await self.mark_as_paid(tx["tx_id"])
                
                # Update mutation with matched tx
                await self._db.execute(
                    """UPDATE mutations 
                       SET matched_tx_id = ?
                       WHERE amount = ? AND matched_tx_id = ''
                       ORDER BY id DESC LIMIT 1""",
                    (tx["tx_id"], amount)
                )
                await self._db.commit()
                
                logger.info(f"Mutation Rp {amount:,} matched with {tx['tx_id']}")
                return tx
        
        logger.debug(f"Mutation Rp {amount:,} - no matching transaction found")
        return None

    async def get_stats(self) -> Dict:
        """Get payment statistics."""
        stats = {}
        
        async with self._db.execute(
            "SELECT COUNT(*) FROM transactions WHERE status = ?", (STATUS_PENDING,)
        ) as cursor:
            stats["pending"] = (await cursor.fetchone())[0]
        
        async with self._db.execute(
            "SELECT COUNT(*) FROM transactions WHERE status = ?", (STATUS_PAID,)
        ) as cursor:
            stats["paid"] = (await cursor.fetchone())[0]
        
        async with self._db.execute(
            "SELECT COUNT(*) FROM transactions WHERE status = ?", (STATUS_EXPIRED,)
        ) as cursor:
            stats["expired"] = (await cursor.fetchone())[0]
        
        async with self._db.execute(
            "SELECT COALESCE(SUM(total_amount), 0) FROM transactions WHERE status = ?",
            (STATUS_PAID,)
        ) as cursor:
            stats["total_revenue"] = (await cursor.fetchone())[0]
        
        async with self._db.execute("SELECT COUNT(*) FROM transactions") as cursor:
            stats["total_transactions"] = (await cursor.fetchone())[0]
        
        return stats


class MutationChecker:
    """
    Background mutation checker.
    
    This class handles periodic checking of e-wallet/bank mutations.
    You need to implement the actual mutation fetching based on your provider:
    
    Options:
    1. Mitra Bukalapak - Check transaction history manually (scraping approach)
    2. GoPay Merchant - Use unofficial API to check balance/mutations
    3. Manual confirmation - Admin manually confirms payments
    
    For now, this provides a framework with manual confirmation + simulated auto-check.
    """

    def __init__(self, payment_manager: PaymentManager, check_interval: int = 15):
        """
        Args:
            payment_manager: PaymentManager instance
            check_interval: Seconds between mutation checks
        """
        self.pm = payment_manager
        self.check_interval = check_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._on_payment_callback = None

    def set_payment_callback(self, callback):
        """
        Set callback function to be called when payment is verified.
        Callback signature: async def callback(transaction: Dict)
        """
        self._on_payment_callback = callback

    async def start(self):
        """Start the background mutation checking loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        logger.info(f"Mutation checker started (interval: {self.check_interval}s)")

    async def stop(self):
        """Stop the background mutation checking loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Mutation checker stopped")

    async def _check_loop(self):
        """Main checking loop."""
        while self._running:
            try:
                # 1. Check for expired transactions
                await self.pm.mark_expired_transactions()
                
                # 2. Fetch new mutations (implement based on your provider)
                mutations = await self._fetch_mutations()
                
                # 3. Process each mutation
                for mutation in mutations:
                    matched_tx = await self.pm.check_mutation(
                        amount=mutation["amount"],
                        sender=mutation.get("sender", ""),
                        reference=mutation.get("reference", "")
                    )
                    
                    if matched_tx and self._on_payment_callback:
                        await self._on_payment_callback(matched_tx)
                
            except Exception as e:
                logger.error(f"Error in mutation check loop: {e}")
            
            await asyncio.sleep(self.check_interval)

    async def _fetch_mutations(self) -> List[Dict]:
        """
        Fetch new mutations from e-wallet/bank.
        
        ⚠️ IMPLEMENT THIS BASED ON YOUR SETUP:
        
        Option A: Mitra Bukalapak (manual/scraping - not recommended for production)
        Option B: Use Mutasiku API (https://mutasiku.id) - paid service
        Option C: Manual admin confirmation (safest, implemented below)
        
        For now, returns empty list. Payments are confirmed manually by admin
        using /confirm command in the bot, OR you can integrate an API here.
        
        Example integration with Mutasiku:
        
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {MUTASIKU_API_KEY}"}
            async with session.get(
                "https://api.mutasiku.id/v1/mutations",
                headers=headers,
                params={"since": last_check_timestamp}
            ) as resp:
                data = await resp.json()
                return [{"amount": m["amount"], "sender": m["sender"]} for m in data["mutations"]]
        """
        # Placeholder - returns empty list
        # Payments are confirmed manually via /confirm command
        return []

    async def manual_confirm(self, amount: int, sender: str = "") -> Optional[Dict]:
        """
        Manually confirm a payment (admin use).
        Called when admin uses /confirm command.
        
        Args:
            amount: The exact amount received
            sender: Optional sender info
            
        Returns:
            Matched transaction if found
        """
        matched_tx = await self.pm.check_mutation(
            amount=amount,
            sender=sender,
            reference="manual_confirm"
        )
        
        if matched_tx and self._on_payment_callback:
            await self._on_payment_callback(matched_tx)
        
        return matched_tx
