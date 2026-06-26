"""
Telegram Bot - QRIS Payment Gateway
Bot untuk menerima pembayaran otomatis via QRIS Mitra Bukalapak.

Commands:
  /start      - Mulai bot
  /bayar      - Buat pembayaran baru
  /cek        - Cek status pembayaran
  /riwayat    - Lihat riwayat transaksi
  /batal      - Batalkan pembayaran pending
  /help       - Bantuan

Admin Commands:
  /confirm    - Konfirmasi pembayaran manual
  /stats      - Lihat statistik
  /pending    - Lihat semua transaksi pending
"""

import os
import logging
import asyncio
from datetime import datetime
from typing import Dict

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from qris_converter import static_to_dynamic, generate_qr_image, get_merchant_name
from payment_manager import PaymentManager, MutationChecker

# Load environment variables
load_dotenv()

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
QRIS_STATIC = os.getenv("QRIS_STATIC", "")
EXPIRY_MINUTES = int(os.getenv("EXPIRY_MINUTES", "30"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "15"))

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global instances
payment_manager: PaymentManager = None
mutation_checker: MutationChecker = None


# ========================
# HELPER FUNCTIONS
# ========================

def is_admin(user_id: int) -> bool:
    """Check if user is admin."""
    return user_id in ADMIN_IDS


def format_rupiah(amount: int) -> str:
    """Format amount to Rupiah string."""
    return f"Rp {amount:,.0f}".replace(",", ".")


def format_transaction(tx: Dict) -> str:
    """Format transaction dict to readable string."""
    status_emoji = {
        "pending": "⏳",
        "paid": "✅",
        "expired": "❌",
        "cancelled": "🚫"
    }
    emoji = status_emoji.get(tx["status"], "❓")
    
    text = (
        f"{emoji} <b>{tx['tx_id']}</b>\n"
        f"   Produk: {tx['product_name'] or '-'}\n"
        f"   Nominal: {format_rupiah(tx['total_amount'])}\n"
        f"   Status: {tx['status'].upper()}\n"
        f"   Dibuat: {tx['created_at'][:16]}"
    )
    
    if tx["status"] == "paid" and tx.get("paid_at"):
        text += f"\n   Dibayar: {tx['paid_at'][:16]}"
    
    return text


# ========================
# PAYMENT CALLBACK
# ========================

async def on_payment_verified(transaction: Dict):
    """Called when a payment is verified (auto or manual)."""
    # This will be called by the mutation checker
    # We need to send notification to the user
    # The bot application reference will be set during startup
    pass


# ========================
# COMMAND HANDLERS
# ========================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user = update.effective_user
    
    welcome_text = (
        f"👋 Halo <b>{user.first_name}</b>!\n\n"
        f"Selamat datang di Bot Pembayaran QRIS.\n"
        f"Bot ini menerima pembayaran otomatis via QRIS.\n\n"
        f"📋 <b>Menu:</b>\n"
        f"/bayar - Buat pembayaran baru\n"
        f"/cek - Cek status pembayaran\n"
        f"/riwayat - Riwayat transaksi\n"
        f"/batal - Batalkan pembayaran\n"
        f"/help - Bantuan\n\n"
        f"💡 Ketik /bayar untuk mulai pembayaran."
    )
    
    await update.message.reply_text(welcome_text, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    help_text = (
        "ℹ️ <b>Panduan Penggunaan Bot</b>\n\n"
        "<b>Cara Bayar:</b>\n"
        "1. Ketik /bayar [nominal] [keterangan]\n"
        "   Contoh: <code>/bayar 50000 Pulsa 50rb</code>\n"
        "2. Bot akan mengirim QRIS dengan nominal unik\n"
        "3. Scan dan bayar TEPAT sesuai nominal yang tertera\n"
        "4. Pembayaran akan dikonfirmasi otomatis\n\n"
        "<b>⚠️ Penting:</b>\n"
        "• Bayar TEPAT sesuai nominal (termasuk angka unik)\n"
        "• Pembayaran expired dalam {expiry} menit\n"
        "• Jangan bayar 2x untuk 1 transaksi\n\n"
        "<b>Commands:</b>\n"
        "/bayar [nominal] [ket] - Buat pembayaran\n"
        "/cek [TX_ID] - Cek status transaksi\n"
        "/riwayat - Lihat riwayat\n"
        "/batal [TX_ID] - Batalkan transaksi\n"
    ).format(expiry=EXPIRY_MINUTES)
    
    await update.message.reply_text(help_text, parse_mode="HTML")


async def bayar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle /bayar command.
    Usage: /bayar [nominal] [keterangan]
    Example: /bayar 50000 Beli Pulsa
    """
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Parse arguments
    args = context.args
    
    if not args or not args[0].isdigit():
        # Show amount selection buttons
        keyboard = [
            [
                InlineKeyboardButton("Rp 10.000", callback_data="pay_10000"),
                InlineKeyboardButton("Rp 25.000", callback_data="pay_25000"),
            ],
            [
                InlineKeyboardButton("Rp 50.000", callback_data="pay_50000"),
                InlineKeyboardButton("Rp 100.000", callback_data="pay_100000"),
            ],
            [
                InlineKeyboardButton("Rp 200.000", callback_data="pay_200000"),
                InlineKeyboardButton("Rp 500.000", callback_data="pay_500000"),
            ],
            [
                InlineKeyboardButton("💰 Nominal Lain", callback_data="pay_custom"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "💳 <b>Pilih Nominal Pembayaran:</b>\n\n"
            "Atau ketik: <code>/bayar [nominal] [keterangan]</code>\n"
            "Contoh: <code>/bayar 75000 TopUp Game</code>",
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
        return
    
    # Parse amount and product name
    amount = int(args[0])
    product_name = " ".join(args[1:]) if len(args) > 1 else ""
    
    if amount < 1000:
        await update.message.reply_text("❌ Nominal minimal Rp 1.000")
        return
    
    if amount > 10000000:
        await update.message.reply_text("❌ Nominal maksimal Rp 10.000.000")
        return
    
    # Create transaction
    await _process_payment(update, context, user.id, chat_id, amount, product_name)


async def _process_payment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    amount: int,
    product_name: str = ""
):
    """Process payment: create transaction and send QRIS."""
    global payment_manager
    
    # Check for QRIS configuration
    if not QRIS_STATIC:
        await update.effective_message.reply_text(
            "⚠️ Bot belum dikonfigurasi. Hubungi admin."
        )
        return
    
    # Send "processing" message
    processing_msg = await update.effective_message.reply_text("⏳ Memproses pembayaran...")
    
    try:
        # Create transaction with unique amount
        tx = await payment_manager.create_transaction(
            user_id=user_id,
            chat_id=chat_id,
            base_amount=amount,
            product_name=product_name
        )
        
        # Convert QRIS static to dynamic with the unique amount
        dynamic_qris = static_to_dynamic(QRIS_STATIC, tx["unique_amount"])
        
        # Generate QR code image
        qr_buffer = generate_qr_image(dynamic_qris)
        
        # Format message
        payment_text = (
            f"🧾 <b>Invoice Pembayaran</b>\n"
            f"{'─' * 28}\n"
            f"📝 ID: <code>{tx['tx_id']}</code>\n"
            f"🛒 Produk: {product_name or 'Pembayaran'}\n"
            f"💰 Nominal: {format_rupiah(tx['base_amount'])}\n"
            f"🔢 <b>Total Bayar: {format_rupiah(tx['unique_amount'])}</b>\n"
            f"{'─' * 28}\n\n"
            f"📱 <b>Scan QRIS di atas untuk membayar</b>\n\n"
            f"⚠️ <b>PENTING:</b>\n"
            f"• Bayar TEPAT <b>{format_rupiah(tx['unique_amount'])}</b>\n"
            f"• Jangan dibulatkan!\n"
            f"• Expired dalam <b>{EXPIRY_MINUTES} menit</b>\n\n"
            f"⏰ Batas waktu: {tx['expires_at'][:16].replace('T', ' ')}"
        )
        
        # Delete processing message
        await processing_msg.delete()
        
        # Send QR image with caption
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=qr_buffer,
            caption=payment_text,
            parse_mode="HTML"
        )
        
        # Schedule expiry notification
        context.job_queue.run_once(
            _check_expiry,
            when=EXPIRY_MINUTES * 60,
            data={"tx_id": tx["tx_id"], "chat_id": chat_id, "user_id": user_id},
            name=f"expiry_{tx['tx_id']}"
        )
        
    except Exception as e:
        logger.error(f"Error processing payment: {e}")
        await processing_msg.edit_text(f"❌ Terjadi kesalahan: {str(e)}")


async def _check_expiry(context: ContextTypes.DEFAULT_TYPE):
    """Job callback to check if transaction has expired."""
    global payment_manager
    data = context.job.data
    tx = await payment_manager.get_transaction(data["tx_id"])
    
    if tx and tx["status"] == "pending":
        await payment_manager.mark_expired_transactions()
        await context.bot.send_message(
            chat_id=data["chat_id"],
            text=(
                f"⏰ <b>Pembayaran Expired</b>\n\n"
                f"Transaksi <code>{data['tx_id']}</code> telah expired.\n"
                f"Silakan buat pembayaran baru dengan /bayar"
            ),
            parse_mode="HTML"
        )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callbacks."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    
    if data.startswith("pay_"):
        amount_str = data.replace("pay_", "")
        
        if amount_str == "custom":
            await query.edit_message_text(
                "💰 <b>Masukkan Nominal</b>\n\n"
                "Ketik nominal yang ingin dibayar:\n"
                "Contoh: <code>/bayar 75000 TopUp Game</code>",
                parse_mode="HTML"
            )
            return
        
        amount = int(amount_str)
        await query.edit_message_text(f"⏳ Memproses pembayaran {format_rupiah(amount)}...")
        await _process_payment(update, context, user_id, chat_id, amount)


async def cek_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cek command - check transaction status."""
    global payment_manager
    args = context.args
    
    if not args:
        # Show user's latest pending transaction
        user_id = update.effective_user.id
        txs = await payment_manager.get_user_transactions(user_id, limit=1)
        
        if not txs:
            await update.message.reply_text(
                "📭 Tidak ada transaksi.\nGunakan /bayar untuk membuat pembayaran."
            )
            return
        
        tx = txs[0]
    else:
        tx_id = args[0].upper()
        tx = await payment_manager.get_transaction(tx_id)
        
        if not tx:
            await update.message.reply_text(f"❌ Transaksi <code>{tx_id}</code> tidak ditemukan.", parse_mode="HTML")
            return
    
    status_text = format_transaction(tx)
    
    if tx["status"] == "pending":
        status_text += f"\n\n⏳ Menunggu pembayaran {format_rupiah(tx['total_amount'])}"
        status_text += f"\n⏰ Expired: {tx['expires_at'][:16].replace('T', ' ')}"
    
    await update.message.reply_text(status_text, parse_mode="HTML")


async def riwayat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /riwayat command - show transaction history."""
    global payment_manager
    user_id = update.effective_user.id
    
    txs = await payment_manager.get_user_transactions(user_id, limit=10)
    
    if not txs:
        await update.message.reply_text(
            "📭 Belum ada riwayat transaksi.\nGunakan /bayar untuk membuat pembayaran."
        )
        return
    
    text = "📜 <b>Riwayat Transaksi</b>\n\n"
    for tx in txs:
        text += format_transaction(tx) + "\n\n"
    
    await update.message.reply_text(text, parse_mode="HTML")


async def batal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /batal command - cancel pending transaction."""
    global payment_manager
    args = context.args
    user_id = update.effective_user.id
    
    if not args:
        # Cancel latest pending transaction
        txs = await payment_manager.get_user_transactions(user_id, limit=5)
        pending = [tx for tx in txs if tx["status"] == "pending"]
        
        if not pending:
            await update.message.reply_text("✅ Tidak ada transaksi pending untuk dibatalkan.")
            return
        
        tx = pending[0]
        tx_id = tx["tx_id"]
    else:
        tx_id = args[0].upper()
    
    success = await payment_manager.cancel_transaction(tx_id, user_id)
    
    if success:
        await update.message.reply_text(
            f"🚫 Transaksi <code>{tx_id}</code> berhasil dibatalkan.",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            f"❌ Gagal membatalkan transaksi <code>{tx_id}</code>.\n"
            f"Pastikan transaksi milik Anda dan masih pending.",
            parse_mode="HTML"
        )


# ========================
# ADMIN COMMANDS
# ========================

async def confirm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle /confirm command (Admin only).
    Usage: /confirm [nominal]
    Example: /confirm 50123
    
    Manually confirms a payment by matching the exact amount.
    """
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ Anda bukan admin.")
        return
    
    args = context.args
    
    if not args or not args[0].isdigit():
        # Show pending transactions
        pending = await payment_manager.get_pending_transactions()
        
        if not pending:
            await update.message.reply_text("✅ Tidak ada transaksi pending.")
            return
        
        text = "📋 <b>Transaksi Pending:</b>\n\n"
        for tx in pending[:20]:
            text += (
                f"• <code>{tx['tx_id']}</code> - "
                f"<b>{format_rupiah(tx['unique_amount'])}</b> "
                f"(User: {tx['user_id']})\n"
            )
        text += f"\n💡 Gunakan: <code>/confirm [nominal]</code>"
        
        await update.message.reply_text(text, parse_mode="HTML")
        return
    
    amount = int(args[0])
    sender = " ".join(args[1:]) if len(args) > 1 else ""
    
    # Try to match
    matched_tx = await mutation_checker.manual_confirm(amount, sender)
    
    if matched_tx:
        await update.message.reply_text(
            f"✅ <b>Pembayaran Dikonfirmasi!</b>\n\n"
            f"TX ID: <code>{matched_tx['tx_id']}</code>\n"
            f"User: {matched_tx['user_id']}\n"
            f"Nominal: {format_rupiah(matched_tx['total_amount'])}\n"
            f"Produk: {matched_tx['product_name'] or '-'}",
            parse_mode="HTML"
        )
        
        # Notify the buyer
        try:
            await context.bot.send_message(
                chat_id=matched_tx["chat_id"],
                text=(
                    f"✅ <b>Pembayaran Berhasil!</b>\n\n"
                    f"Transaksi <code>{matched_tx['tx_id']}</code> telah dikonfirmasi.\n"
                    f"Nominal: {format_rupiah(matched_tx['total_amount'])}\n"
                    f"Produk: {matched_tx['product_name'] or 'Pembayaran'}\n\n"
                    f"Terima kasih! 🙏"
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to notify user: {e}")
    else:
        await update.message.reply_text(
            f"❌ Tidak ada transaksi pending dengan nominal {format_rupiah(amount)}.\n"
            f"Pastikan nominal PERSIS sama (termasuk angka unik).",
            parse_mode="HTML"
        )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats command (Admin only)."""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ Anda bukan admin.")
        return
    
    stats = await payment_manager.get_stats()
    
    text = (
        "📊 <b>Statistik Pembayaran</b>\n\n"
        f"📦 Total Transaksi: {stats['total_transactions']}\n"
        f"⏳ Pending: {stats['pending']}\n"
        f"✅ Sukses: {stats['paid']}\n"
        f"❌ Expired: {stats['expired']}\n\n"
        f"💰 Total Revenue: {format_rupiah(stats['total_revenue'])}"
    )
    
    await update.message.reply_text(text, parse_mode="HTML")


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /pending command (Admin only) - show all pending transactions."""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ Anda bukan admin.")
        return
    
    pending = await payment_manager.get_pending_transactions()
    
    if not pending:
        await update.message.reply_text("✅ Tidak ada transaksi pending saat ini.")
        return
    
    text = f"⏳ <b>Transaksi Pending ({len(pending)})</b>\n\n"
    for tx in pending[:20]:
        text += (
            f"• <code>{tx['tx_id']}</code>\n"
            f"  User: {tx['user_id']} | "
            f"Bayar: <b>{format_rupiah(tx['unique_amount'])}</b>\n"
            f"  Produk: {tx['product_name'] or '-'}\n"
            f"  Expires: {tx['expires_at'][:16].replace('T', ' ')}\n\n"
        )
    
    if len(pending) > 20:
        text += f"... dan {len(pending) - 20} lainnya"
    
    text += f"\n💡 Konfirmasi: <code>/confirm [nominal]</code>"
    
    await update.message.reply_text(text, parse_mode="HTML")


# ========================
# APPLICATION SETUP
# ========================

async def post_init(application: Application):
    """Initialize services after bot starts."""
    global payment_manager, mutation_checker
    
    # Initialize payment manager
    payment_manager = PaymentManager(
        db_path="payments.db",
        expiry_minutes=EXPIRY_MINUTES
    )
    await payment_manager.initialize()
    
    # Initialize mutation checker
    mutation_checker = MutationChecker(
        payment_manager=payment_manager,
        check_interval=CHECK_INTERVAL
    )
    
    # Set callback for auto-notification when payment is verified
    async def notify_payment(tx: Dict):
        try:
            await application.bot.send_message(
                chat_id=tx["chat_id"],
                text=(
                    f"✅ <b>Pembayaran Berhasil!</b>\n\n"
                    f"Transaksi <code>{tx['tx_id']}</code> telah dikonfirmasi.\n"
                    f"Nominal: {format_rupiah(tx['total_amount'])}\n"
                    f"Produk: {tx['product_name'] or 'Pembayaran'}\n\n"
                    f"Terima kasih! 🙏"
                ),
                parse_mode="HTML"
            )
            
            # Notify admin
            for admin_id in ADMIN_IDS:
                await application.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"💰 <b>Payment Received!</b>\n"
                        f"TX: <code>{tx['tx_id']}</code>\n"
                        f"User: {tx['user_id']}\n"
                        f"Amount: {format_rupiah(tx['total_amount'])}"
                    ),
                    parse_mode="HTML"
                )
        except Exception as e:
            logger.error(f"Failed to send payment notification: {e}")
    
    mutation_checker.set_payment_callback(notify_payment)
    await mutation_checker.start()
    
    logger.info("Bot initialized successfully!")


async def post_shutdown(application: Application):
    """Cleanup on shutdown."""
    global payment_manager, mutation_checker
    
    if mutation_checker:
        await mutation_checker.stop()
    if payment_manager:
        await payment_manager.close()
    
    logger.info("Bot shutdown complete.")


def main():
    """Main entry point."""
    if not BOT_TOKEN:
        print("❌ ERROR: BOT_TOKEN not set in .env file!")
        print("Please set your Telegram bot token in .env")
        return
    
    if not QRIS_STATIC:
        print("⚠️ WARNING: QRIS_STATIC not set in .env file!")
        print("Bot will start but payments won't work until QRIS is configured.")
    
    if not ADMIN_IDS:
        print("⚠️ WARNING: ADMIN_IDS not set in .env file!")
        print("No admin can use /confirm command.")
    
    # Build application
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    
    # Register handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("bayar", bayar_command))
    application.add_handler(CommandHandler("cek", cek_command))
    application.add_handler(CommandHandler("riwayat", riwayat_command))
    application.add_handler(CommandHandler("batal", batal_command))
    
    # Admin commands
    application.add_handler(CommandHandler("confirm", confirm_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("pending", pending_command))
    
    # Callback handler for inline buttons
    application.add_handler(CallbackQueryHandler(callback_handler))
    
    # Start bot
    print("🤖 Bot started! Press Ctrl+C to stop.")
    print(f"   Admin IDs: {ADMIN_IDS}")
    print(f"   QRIS configured: {'✅' if QRIS_STATIC else '❌'}")
    print(f"   Expiry: {EXPIRY_MINUTES} minutes")
    print(f"   Check interval: {CHECK_INTERVAL} seconds")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
