"""
Telegram Bot - QRIS Payment (Auto-Confirm via MacroDroid)
Bot menerima pembayaran via QRIS Mitra Bukalapak.
Auto-confirm: MacroDroid kirim notifikasi langsung ke bot via Telegram API.

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
"""

import os
import re
import logging
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

from qris_converter import static_to_dynamic, generate_qr_image
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


def extract_amount_from_text(text: str) -> Optional[int]:
    """Extract nominal Rupiah dari teks notifikasi Mitra Bukalapak."""
    patterns = [
        r'[Rr]p\.?\s*([0-9][0-9.,]*)',
        r'sebesar\s*[Rr]p\.?\s*([0-9][0-9.,]*)',
        r'(\d{1,3}(?:[.,]\d{3})+)',
        r'(\d{4,})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            amount_str = match.group(1).replace(".", "").replace(",", "")
            try:
                amount = int(amount_str)
                if 1000 <= amount <= 100000000:
                    return amount
            except ValueError:
                continue
    return None


# ========================
# AUTO-CONFIRM (dari MacroDroid)
# ========================

async def auto_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Tangkap pesan dari MacroDroid (dikirim sebagai admin).
    MacroDroid kirim teks notifikasi Mitra Bukalapak langsung ke bot.
    Bot parse nominal → auto match → auto confirm.
    """
    global payment_manager, mutation_checker

    # Hanya proses pesan dari ADMIN (MacroDroid kirim sebagai admin)
    if not is_admin(update.effective_user.id):
        return

    text = update.message.text or ""

    # Skip jika ini command biasa (bukan dari MacroDroid)
    if text.startswith("/"):
        return

    # Extract nominal dari teks notifikasi
    amount = extract_amount_from_text(text)

    if not amount:
        # Bukan notifikasi pembayaran, abaikan
        return

    # Coba match dengan pending transaction
    matched_tx = await mutation_checker.manual_confirm(amount, "MacroDroid Auto")

    if matched_tx:
        logger.info(f"AUTO-CONFIRM: {matched_tx['tx_id']} = Rp {amount:,}")

        # Notif ke admin
        await update.message.reply_text(
            f"\u2705 <b>Auto-Confirmed!</b>\n\n"
            f"TX: <code>{matched_tx['tx_id']}</code>\n"
            f"Nominal: {format_rupiah(matched_tx['total_amount'])}\n"
            f"Produk: {matched_tx['product_name'] or '-'}",
            parse_mode="HTML"
        )

        # Notif ke buyer
        try:
            if matched_tx.get("chat_id") and matched_tx["chat_id"] != 0:
                await context.bot.send_message(
                    chat_id=matched_tx["chat_id"],
                    text=(
                        f"\u2705 <b>Pembayaran Berhasil!</b>\n\n"
                        f"Transaksi <code>{matched_tx['tx_id']}</code> dikonfirmasi.\n"
                        f"Nominal: {format_rupiah(matched_tx['total_amount'])}\n\n"
                        f"Terima kasih! \U0001f64f"
                    ),
                    parse_mode="HTML"
                )
        except Exception as e:
            logger.error(f"Failed to notify buyer: {e}")
    else:
        # Ada nominal tapi tidak cocok dengan pending manapun
        logger.info(f"MacroDroid: Rp {amount:,} - no match")


# ========================
# USER COMMANDS
# ========================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"\U0001f44b Halo <b>{user.first_name}</b>!\n\n"
        f"Bot Pembayaran QRIS Otomatis.\n\n"
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
        "2. Scan QRIS yang dikirim bot\n"
        "3. Bayar TEPAT sesuai nominal\n"
        "4. Pembayaran dikonfirmasi otomatis!\n\n"
        f"\u26a0\ufe0f Expired: {EXPIRY_MINUTES} menit\n"
        "\u26a0\ufe0f Jangan bulatkan nominal!",
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
    if amount < 100:
        await update.message.reply_text("\u274c Minimal Rp 100")
        return
    if amount > 10000000:
        await update.message.reply_text("\u274c Maksimal Rp 10.000.000")
        return
    await _process_payment(update, context, user.id, chat_id, amount, product_name)


async def _process_payment(update, context, user_id, chat_id, amount, product_name=""):
    if not QRIS_STATIC:
        await update.effective_message.reply_text("\u26a0\ufe0f Bot belum dikonfigurasi.")
        return
    msg = await update.effective_message.reply_text("\u23f3 Memproses...")
    try:
        tx = await payment_manager.create_transaction(user_id=user_id, chat_id=chat_id, base_amount=amount, product_name=product_name)
        dynamic_qris = static_to_dynamic(QRIS_STATIC, tx["unique_amount"])
        qr_buffer = generate_qr_image(dynamic_qris)
        text = (
            f"\U0001f9fe <b>Invoice Pembayaran</b>\n"
            f"{'\u2500' * 28}\n"
            f"\U0001f4dd ID: <code>{tx['tx_id']}</code>\n"
            f"\U0001f6d2 Produk: {product_name or 'Pembayaran'}\n"
            f"\U0001f4b0 Harga: {format_rupiah(tx['base_amount'])}\n"
            f"\U0001f522 <b>BAYAR: {format_rupiah(tx['unique_amount'])}</b>\n"
            f"{'\u2500' * 28}\n\n"
            f"\U0001f4f1 <b>Scan QRIS di atas</b>\n\n"
            f"\u26a0\ufe0f Bayar TEPAT <b>{format_rupiah(tx['unique_amount'])}</b>\n"
            f"\u23f0 Expired: {EXPIRY_MINUTES} menit"
        )
        await msg.delete()
        await context.bot.send_photo(chat_id=chat_id, photo=qr_buffer, caption=text, parse_mode="HTML")
        context.job_queue.run_once(_check_expiry, when=EXPIRY_MINUTES * 60, data={"tx_id": tx["tx_id"], "chat_id": chat_id}, name=f"exp_{tx['tx_id']}")
    except Exception as e:
        logger.error(f"Payment error: {e}")
        await msg.edit_text(f"\u274c Error: {e}")


async def _check_expiry(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    tx = await payment_manager.get_transaction(data["tx_id"])
    if tx and tx["status"] == "pending":
        await payment_manager.mark_expired_transactions()
        await context.bot.send_message(chat_id=data["chat_id"], text=f"\u23f0 Transaksi <code>{data['tx_id']}</code> expired.\n/bayar untuk buat baru.", parse_mode="HTML")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("pay_"):
        amount_str = data.replace("pay_", "")
        if amount_str == "custom":
            await query.edit_message_text("\U0001f4b0 Ketik: <code>/bayar 75000 TopUp Game</code>", parse_mode="HTML")
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
    await update.message.reply_text(f"\U0001f6ab Dibatalkan." if ok else "\u274c Gagal.")


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


# ========================
# STARTUP
# ========================

async def post_init(application: Application):
    global payment_manager, mutation_checker

    payment_manager = PaymentManager(db_path="payments.db", expiry_minutes=EXPIRY_MINUTES)
    await payment_manager.initialize()

    mutation_checker = MutationChecker(payment_manager=payment_manager, check_interval=CHECK_INTERVAL)
    await mutation_checker.start()

    logger.info("Bot ready! Auto-confirm via MacroDroid active.")


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

    # User commands
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

    # Callbacks (inline buttons)
    application.add_handler(CallbackQueryHandler(callback_handler))

    # AUTO-CONFIRM: Tangkap semua pesan teks dari admin (MacroDroid)
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.User(user_id=ADMIN_IDS),
        auto_confirm_handler
    ))

    print("\U0001f916 Bot started! (Auto-confirm via MacroDroid)")
    print(f"   QRIS: {'\u2705' if QRIS_STATIC else '\u274c'}")
    print(f"   Expiry: {EXPIRY_MINUTES} min")
    print(f"   Admin: {ADMIN_IDS}")
    print(f"   Mode: AUTO (MacroDroid → Telegram → Bot)")

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
