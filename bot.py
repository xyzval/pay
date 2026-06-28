"""
Telegram Bot - QRIS Payment via Saweria Webhook (Resmi)
Auto-confirm pembayaran menggunakan webhook resmi dari Saweria.

Flow:
1. User /bayar → Bot kirim link Saweria + nominal unik
2. Customer bayar di Saweria
3. Saweria kirim webhook ke bot
4. Bot auto-match & confirm

User Commands:
  /start   - Mulai bot
  /bayar   - Buat pembayaran baru
  /cek     - Cek status pembayaran
  /riwayat - Lihat riwayat transaksi
  /batal   - Batalkan pembayaran
  /help    - Bantuan

Admin Commands:
  /confirm - Konfirmasi manual (backup)
  /stats   - Statistik
  /pending - Lihat transaksi pending
  /addmerchant - Daftarkan merchant baru (dapat API Key)
  /merchants   - Lihat daftar merchant
"""

import os
import re
import logging
import threading
from datetime import datetime
from typing import Dict, Optional

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

from payment_manager import PaymentManager, MutationChecker

# Load environment variables
load_dotenv()

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
SAWERIA_USERNAME = os.getenv("SAWERIA_USERNAME", "nvatryn")
EXPIRY_MINUTES = int(os.getenv("EXPIRY_MINUTES", "30"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "15"))
API_PORT = int(os.getenv("API_PORT", "8000"))

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global instances
payment_manager: PaymentManager = None
mutation_checker: MutationChecker = None


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def format_rupiah(amount: int) -> str:
    return f"Rp {amount:,.0f}".replace(",", ".")


def format_transaction(tx: Dict) -> str:
    emoji = {"pending": "\u23f3", "paid": "\u2705", "expired": "\u274c", "cancelled": "\U0001f6ab"}.get(tx["status"], "\u2753")
    text = f"{emoji} <b>{tx['tx_id']}</b>\n   Produk: {tx['product_name'] or '-'}\n   Nominal: {format_rupiah(tx['total_amount'])}\n   Status: {tx['status'].upper()}\n   Dibuat: {tx['created_at'][:16]}"
    if tx["status"] == "paid" and tx.get("paid_at"):
        text += f"\n   Dibayar: {tx['paid_at'][:16]}"
    return text


# ========================
# USER COMMANDS
# ========================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"\U0001f44b Halo <b>{user.first_name}</b>!\n\n"
        f"Bot Pembayaran Otomatis via QRIS.\n\n"
        f"\U0001f4cb <b>Menu:</b>\n"
        f"/bayar - Buat pembayaran\n"
        f"/cek - Cek status\n"
        f"/riwayat - Riwayat transaksi\n"
        f"/batal - Batalkan pembayaran\n"
        f"/help - Bantuan\n\n"
        f"\U0001f4a1 Ketik /bayar untuk mulai.",
        parse_mode="HTML"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "\u2139\ufe0f <b>Cara Bayar:</b>\n\n"
        "1. Ketik /bayar [nominal] [keterangan]\n"
        "2. Buka link pembayaran yang dikirim bot\n"
        "3. Bayar via QRIS/transfer di halaman Saweria\n"
        "4. Pembayaran dikonfirmasi otomatis!\n\n"
        f"\u26a0\ufe0f Expired: {EXPIRY_MINUTES} menit",
        parse_mode="HTML"
    )


async def bayar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    args = context.args

    if not args or not args[0].isdigit():
        keyboard = [
            [InlineKeyboardButton("Rp 10.000", callback_data="pay_10000"),
             InlineKeyboardButton("Rp 25.000", callback_data="pay_25000")],
            [InlineKeyboardButton("Rp 50.000", callback_data="pay_50000"),
             InlineKeyboardButton("Rp 100.000", callback_data="pay_100000")],
            [InlineKeyboardButton("Rp 200.000", callback_data="pay_200000"),
             InlineKeyboardButton("Rp 500.000", callback_data="pay_500000")],
            [InlineKeyboardButton("\U0001f4b0 Nominal Lain", callback_data="pay_custom")],
        ]
        await update.message.reply_text(
            "\U0001f4b3 <b>Pilih Nominal:</b>\n\nAtau ketik: <code>/bayar 50000 Pulsa XL</code>",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )
        return

    amount = int(args[0])
    product_name = " ".join(args[1:]) if len(args) > 1 else ""
    if amount < 1:
        await update.message.reply_text("\u274c Minimal Rp 1")
        return
    if amount > 10000000:
        await update.message.reply_text("\u274c Maksimal Rp 10.000.000")
        return
    await _process_payment(update, context, user.id, chat_id, amount, product_name)


async def _process_payment(update, context, user_id, chat_id, amount, product_name=""):
    msg = await update.effective_message.reply_text("\u23f3 Memproses...")
    try:
        tx = await payment_manager.create_transaction(
            user_id=user_id, chat_id=chat_id,
            base_amount=amount, product_name=product_name
        )

        # Link Saweria dengan nominal unik
        saweria_link = f"https://saweria.co/{SAWERIA_USERNAME}"

        text = (
            f"\U0001f9fe <b>Invoice Pembayaran</b>\n"
            f"{'\u2500' * 28}\n"
            f"\U0001f4dd ID: <code>{tx['tx_id']}</code>\n"
            f"\U0001f6d2 Produk: {product_name or 'Pembayaran'}\n"
            f"\U0001f4b0 Harga: {format_rupiah(tx['base_amount'])}\n"
            f"\U0001f522 <b>BAYAR: {format_rupiah(tx['unique_amount'])}</b>\n"
            f"{'\u2500' * 28}\n\n"
            f"\U0001f517 <b>Link Pembayaran:</b>\n"
            f"{saweria_link}\n\n"
            f"\u26a0\ufe0f <b>PENTING:</b>\n"
            f"\u2022 Buka link di atas\n"
            f"\u2022 Isi TEPAT <b>{format_rupiah(tx['unique_amount'])}</b>\n"
            f"\u2022 Bayar via QRIS/Transfer\n"
            f"\u2022 Otomatis terkonfirmasi!\n\n"
            f"\u23f0 Expired: {EXPIRY_MINUTES} menit"
        )

        await msg.delete()
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")

        context.job_queue.run_once(
            _check_expiry, when=EXPIRY_MINUTES * 60,
            data={"tx_id": tx["tx_id"], "chat_id": chat_id},
            name=f"exp_{tx['tx_id']}"
        )
    except Exception as e:
        logger.error(f"Payment error: {e}")
        await msg.edit_text(f"\u274c Error: {e}")


async def _check_expiry(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    tx = await payment_manager.get_transaction(data["tx_id"])
    if tx and tx["status"] == "pending":
        await payment_manager.mark_expired_transactions()
        await context.bot.send_message(
            chat_id=data["chat_id"],
            text=f"\u23f0 Transaksi <code>{data['tx_id']}</code> expired.\n/bayar untuk buat baru.",
            parse_mode="HTML"
        )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("pay_"):
        amount_str = data.replace("pay_", "")
        if amount_str == "custom":
            await query.edit_message_text(
                "\U0001f4b0 Ketik: <code>/bayar 75000 TopUp Game</code>",
                parse_mode="HTML"
            )
            return
        amount = int(amount_str)
        await query.edit_message_text(f"\u23f3 Memproses {format_rupiah(amount)}...")
        await _process_payment(update, context, query.from_user.id, query.message.chat_id, amount)


async def cek_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        txs = await payment_manager.get_user_transactions(update.effective_user.id, limit=1)
        if not txs:
            await update.message.reply_text("\U0001f4ed Tidak ada transaksi.")
            return
        tx = txs[0]
    else:
        tx = await payment_manager.get_transaction(args[0].upper())
        if not tx:
            await update.message.reply_text("\u274c Tidak ditemukan.")
            return
    await update.message.reply_text(format_transaction(tx), parse_mode="HTML")


async def riwayat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txs = await payment_manager.get_user_transactions(update.effective_user.id, limit=10)
    if not txs:
        await update.message.reply_text("\U0001f4ed Belum ada riwayat.")
        return
    text = "\U0001f4dc <b>Riwayat:</b>\n\n"
    for tx in txs:
        text += format_transaction(tx) + "\n\n"
    await update.message.reply_text(text, parse_mode="HTML")


async def batal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id
    if not args:
        txs = await payment_manager.get_user_transactions(user_id, limit=5)
        pending = [t for t in txs if t["status"] == "pending"]
        if not pending:
            await update.message.reply_text("\u2705 Tidak ada pending.")
            return
        tx_id = pending[0]["tx_id"]
    else:
        tx_id = args[0].upper()
    ok = await payment_manager.cancel_transaction(tx_id, user_id)
    await update.message.reply_text(
        f"\U0001f6ab Dibatalkan." if ok else "\u274c Gagal."
    )


# ========================
# ADMIN COMMANDS
# ========================

async def confirm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual confirm (backup)."""
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if not args or not args[0].isdigit():
        pending = await payment_manager.get_pending_transactions()
        if not pending:
            await update.message.reply_text("\u2705 Tidak ada pending.")
            return
        text = "\U0001f4cb <b>Pending:</b>\n\n"
        for tx in pending[:20]:
            text += f"\u2022 <code>{tx['tx_id']}</code> - <b>{format_rupiah(tx['unique_amount'])}</b>\n"
        text += "\n<code>/confirm [nominal]</code>"
        await update.message.reply_text(text, parse_mode="HTML")
        return

    amount = int(args[0])
    matched = await mutation_checker.manual_confirm(amount)
    if matched:
        await update.message.reply_text(f"\u2705 Confirmed! {matched['tx_id']}")
        try:
            if matched.get("chat_id") and matched["chat_id"] != 0:
                await context.bot.send_message(
                    chat_id=matched["chat_id"],
                    text=f"\u2705 <b>Pembayaran Berhasil!</b>\n\n<code>{matched['tx_id']}</code>\n{format_rupiah(matched['total_amount'])}\n\nTerima kasih! \U0001f64f",
                    parse_mode="HTML"
                )
        except:
            pass
    else:
        await update.message.reply_text(f"\u274c Tidak ada transaksi Rp {amount}")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    s = await payment_manager.get_stats()
    await update.message.reply_text(
        f"\U0001f4ca <b>Stats</b>\n\nTotal: {s['total_transactions']}\n\u23f3 Pending: {s['pending']}\n\u2705 Paid: {s['paid']}\n\u274c Expired: {s['expired']}\n\U0001f4b0 Revenue: {format_rupiah(s['total_revenue'])}",
        parse_mode="HTML"
    )


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    pending = await payment_manager.get_pending_transactions()
    if not pending:
        await update.message.reply_text("\u2705 Kosong.")
        return
    text = f"\u23f3 <b>Pending ({len(pending)}):</b>\n\n"
    for tx in pending[:20]:
        text += f"\u2022 <code>{tx['tx_id']}</code> | {format_rupiah(tx['unique_amount'])}\n"
    await update.message.reply_text(text, parse_mode="HTML")


async def addmerchant_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Daftarkan merchant baru. Usage: /addmerchant [nama] [callback_url]"""
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "\U0001f4dd <b>Daftarkan Merchant:</b>\n\n"
            "<code>/addmerchant NamaToko https://callback.url</code>\n\n"
            "Callback URL opsional (isi - jika skip)",
            parse_mode="HTML"
        )
        return

    import gateway_api
    name = args[0].replace("_", " ")
    callback = args[1] if len(args) > 1 and args[1] != "-" else ""
    merchant = await gateway_api.create_merchant(name, callback)

    await update.message.reply_text(
        f"\u2705 <b>Merchant Terdaftar!</b>\n\n"
        f"\U0001f3e2 Nama: {merchant['name']}\n"
        f"\U0001f194 ID: <code>{merchant['merchant_id']}</code>\n"
        f"\U0001f511 API Key: <code>{merchant['api_key']}</code>\n"
        f"\U0001f517 Callback: {merchant['callback_url'] or '-'}\n\n"
        f"\u26a0\ufe0f <b>SIMPAN API KEY! Hanya muncul sekali.</b>\n\n"
        f"<b>Contoh pakai API:</b>\n"
        f"<code>curl -X POST http://SERVER:{API_PORT}/api/create-payment \\\n"
        f"  -H \"Authorization: Bearer {merchant['api_key']}\" \\\n"
        f"  -H \"Content-Type: application/json\" \\\n"
        f"  -d '{{\"amount\": 50000, \"product_name\": \"Pulsa\"}}'</code>",
        parse_mode="HTML"
    )


async def merchants_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List merchants."""
    if not is_admin(update.effective_user.id):
        return
    import gateway_api
    async with gateway_api.db.execute("SELECT * FROM merchants ORDER BY created_at DESC") as c:
        rows = await c.fetchall()
        cols = [d[0] for d in c.description]
        merchants = [dict(zip(cols, r)) for r in rows]

    if not merchants:
        await update.message.reply_text("\U0001f4ed Belum ada merchant.")
        return

    text = f"\U0001f3e2 <b>Merchants ({len(merchants)}):</b>\n\n"
    for m in merchants[:20]:
        status = "\u2705" if m["is_active"] else "\u274c"
        text += f"{status} <b>{m['name']}</b> | <code>{m['merchant_id']}</code>\n"
    await update.message.reply_text(text, parse_mode="HTML")


# ========================
# STARTUP
# ========================

async def post_init(application: Application):
    global payment_manager, mutation_checker

    payment_manager = PaymentManager(db_path="payments.db", expiry_minutes=EXPIRY_MINUTES)
    await payment_manager.initialize()

    mutation_checker = MutationChecker(payment_manager=payment_manager, check_interval=CHECK_INTERVAL)

    # Callback saat pembayaran terkonfirmasi
    async def on_paid(tx: Dict):
        try:
            if tx.get("chat_id") and tx["chat_id"] != 0:
                await application.bot.send_message(
                    chat_id=tx["chat_id"],
                    text=f"\u2705 <b>Pembayaran Berhasil!</b>\n\n<code>{tx['tx_id']}</code>\n{format_rupiah(tx['total_amount'])}\n\nTerima kasih! \U0001f64f",
                    parse_mode="HTML"
                )
            for aid in ADMIN_IDS:
                await application.bot.send_message(
                    chat_id=aid,
                    text=f"\U0001f4b0 PAID: {tx['tx_id']} | {format_rupiah(tx['total_amount'])}",
                    parse_mode="HTML"
                )
        except Exception as e:
            logger.error(f"Notify error: {e}")

    mutation_checker.set_payment_callback(on_paid)
    await mutation_checker.start()

    # Start Gateway API server (Saweria webhook + Merchant API)
    import gateway_api
    gateway_api.payment_manager = payment_manager
    gateway_api.on_payment_confirmed = on_paid
    gateway_api.SAWERIA_USERNAME = SAWERIA_USERNAME
    gateway_api.db = payment_manager._db

    # Init merchant tables
    await gateway_api.init_merchant_db()

    def run_server():
        import uvicorn
        uvicorn.run(gateway_api.app, host="0.0.0.0", port=API_PORT, log_level="info")

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()

    logger.info(f"Payment Gateway API on port {API_PORT}")
    logger.info("Bot ready!")


async def post_shutdown(application: Application):
    if mutation_checker:
        await mutation_checker.stop()
    if payment_manager:
        await payment_manager.close()


def main():
    if not BOT_TOKEN:
        print("\u274c BOT_TOKEN not set!")
        return

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("bayar", bayar_command))
    application.add_handler(CommandHandler("cek", cek_command))
    application.add_handler(CommandHandler("riwayat", riwayat_command))
    application.add_handler(CommandHandler("batal", batal_command))
    application.add_handler(CommandHandler("confirm", confirm_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CommandHandler("addmerchant", addmerchant_command))
    application.add_handler(CommandHandler("merchants", merchants_command))
    application.add_handler(CallbackQueryHandler(callback_handler))

    print("\U0001f916 Bot started! (Payment Gateway + Saweria Auto-Confirm)")
    print(f"   Saweria: https://saweria.co/{SAWERIA_USERNAME}")
    print(f"   API Docs: http://0.0.0.0:{API_PORT}/docs")
    print(f"   Webhook: http://0.0.0.0:{API_PORT}/callback/saweria")
    print(f"   Expiry: {EXPIRY_MINUTES} min")

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
