"""
Telegram Bot - QRIS Payment Gateway
Bot untuk menerima pembayaran otomatis via QRIS Mitra Bukalapak.
Sekarang dilengkapi fitur Payment Gateway (API Key, Merchant Management).

User Commands:
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

Admin - Merchant Management:
  /addmerchant   - Daftarkan merchant baru
  /merchants     - Lihat daftar merchant
  /merchantinfo  - Info detail merchant
  /revokekey     - Revoke & regenerate API key
  /setfee        - Set fee merchant
  /deactivate    - Nonaktifkan merchant
  /activate      - Aktifkan kembali merchant
  /gatewaystats  - Statistik gateway keseluruhan
"""

import os
import logging
import asyncio
import threading
from datetime import datetime
from typing import Dict

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from qris_converter import static_to_dynamic, generate_qr_image
from payment_manager import PaymentManager, MutationChecker
from merchant_manager import MerchantManager
from webhook_sender import WebhookSender

# Load environment variables
load_dotenv()

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
QRIS_STATIC = os.getenv("QRIS_STATIC", "")
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
merchant_manager: MerchantManager = None
webhook_sender: WebhookSender = None


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
        "pending": "\u23f3",
        "paid": "\u2705",
        "expired": "\u274c",
        "cancelled": "\U0001f6ab"
    }
    emoji = status_emoji.get(tx["status"], "\u2753")
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
# USER COMMAND HANDLERS
# ========================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user = update.effective_user
    welcome_text = (
        f"\U0001f44b Halo <b>{user.first_name}</b>!\n\n"
        f"Selamat datang di Bot Pembayaran QRIS.\n"
        f"Bot ini menerima pembayaran otomatis via QRIS.\n\n"
        f"\U0001f4cb <b>Menu:</b>\n"
        f"/bayar - Buat pembayaran baru\n"
        f"/cek - Cek status pembayaran\n"
        f"/riwayat - Riwayat transaksi\n"
        f"/batal - Batalkan pembayaran\n"
        f"/help - Bantuan\n\n"
        f"\U0001f4a1 Ketik /bayar untuk mulai pembayaran."
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    help_text = (
        "\u2139\ufe0f <b>Panduan Penggunaan Bot</b>\n\n"
        "<b>Cara Bayar:</b>\n"
        "1. Ketik /bayar [nominal] [keterangan]\n"
        "   Contoh: <code>/bayar 50000 Pulsa 50rb</code>\n"
        "2. Bot akan mengirim QRIS dengan nominal unik\n"
        "3. Scan dan bayar TEPAT sesuai nominal yang tertera\n"
        "4. Pembayaran akan dikonfirmasi otomatis\n\n"
        f"<b>\u26a0\ufe0f Penting:</b>\n"
        f"\u2022 Bayar TEPAT sesuai nominal (termasuk angka unik)\n"
        f"\u2022 Pembayaran expired dalam {EXPIRY_MINUTES} menit\n"
        f"\u2022 Jangan bayar 2x untuk 1 transaksi\n\n"
        "<b>Commands:</b>\n"
        "/bayar [nominal] [ket] - Buat pembayaran\n"
        "/cek [TX_ID] - Cek status transaksi\n"
        "/riwayat - Lihat riwayat\n"
        "/batal [TX_ID] - Batalkan transaksi\n"
    )
    await update.message.reply_text(help_text, parse_mode="HTML")


async def bayar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /bayar command."""
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
            "\U0001f4b3 <b>Pilih Nominal Pembayaran:</b>\n\n"
            "Atau ketik: <code>/bayar [nominal] [keterangan]</code>\n"
            "Contoh: <code>/bayar 75000 TopUp Game</code>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return

    amount = int(args[0])
    product_name = " ".join(args[1:]) if len(args) > 1 else ""
    if amount < 1000:
        await update.message.reply_text("\u274c Nominal minimal Rp 1.000")
        return
    if amount > 10000000:
        await update.message.reply_text("\u274c Nominal maksimal Rp 10.000.000")
        return
    await _process_payment(update, context, user.id, chat_id, amount, product_name)



async def _process_payment(update, context, user_id, chat_id, amount, product_name=""):
    """Process payment: create transaction and send QRIS."""
    global payment_manager
    if not QRIS_STATIC:
        await update.effective_message.reply_text("\u26a0\ufe0f Bot belum dikonfigurasi.")
        return
    processing_msg = await update.effective_message.reply_text("\u23f3 Memproses pembayaran...")
    try:
        tx = await payment_manager.create_transaction(
            user_id=user_id, chat_id=chat_id,
            base_amount=amount, product_name=product_name
        )
        dynamic_qris = static_to_dynamic(QRIS_STATIC, tx["unique_amount"])
        qr_buffer = generate_qr_image(dynamic_qris)
        payment_text = (
            f"\U0001f9fe <b>Invoice Pembayaran</b>\n"
            f"{'\u2500' * 28}\n"
            f"\U0001f4dd ID: <code>{tx['tx_id']}</code>\n"
            f"\U0001f6d2 Produk: {product_name or 'Pembayaran'}\n"
            f"\U0001f4b0 Nominal: {format_rupiah(tx['base_amount'])}\n"
            f"\U0001f522 <b>Total Bayar: {format_rupiah(tx['unique_amount'])}</b>\n"
            f"{'\u2500' * 28}\n\n"
            f"\U0001f4f1 <b>Scan QRIS di atas untuk membayar</b>\n\n"
            f"\u26a0\ufe0f <b>PENTING:</b>\n"
            f"\u2022 Bayar TEPAT <b>{format_rupiah(tx['unique_amount'])}</b>\n"
            f"\u2022 Jangan dibulatkan!\n"
            f"\u2022 Expired dalam <b>{EXPIRY_MINUTES} menit</b>\n\n"
            f"\u23f0 Batas waktu: {tx['expires_at'][:16].replace('T', ' ')}"
        )
        await processing_msg.delete()
        await context.bot.send_photo(
            chat_id=chat_id, photo=qr_buffer,
            caption=payment_text, parse_mode="HTML"
        )
        context.job_queue.run_once(
            _check_expiry, when=EXPIRY_MINUTES * 60,
            data={"tx_id": tx["tx_id"], "chat_id": chat_id, "user_id": user_id},
            name=f"expiry_{tx['tx_id']}"
        )
    except Exception as e:
        logger.error(f"Error processing payment: {e}")
        await processing_msg.edit_text(f"\u274c Terjadi kesalahan: {str(e)}")


async def _check_expiry(context: ContextTypes.DEFAULT_TYPE):
    """Job callback to check if transaction has expired."""
    global payment_manager
    data = context.job.data
    tx = await payment_manager.get_transaction(data["tx_id"])
    if tx and tx["status"] == "pending":
        await payment_manager.mark_expired_transactions()
        await context.bot.send_message(
            chat_id=data["chat_id"],
            text=f"\u23f0 <b>Pembayaran Expired</b>\n\nTransaksi <code>{data['tx_id']}</code> telah expired.\nSilakan buat pembayaran baru dengan /bayar",
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
                "\U0001f4b0 <b>Masukkan Nominal</b>\n\nKetik: <code>/bayar 75000 TopUp Game</code>",
                parse_mode="HTML"
            )
            return
        amount = int(amount_str)
        await query.edit_message_text(f"\u23f3 Memproses pembayaran {format_rupiah(amount)}...")
        await _process_payment(update, context, user_id, chat_id, amount)



async def cek_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cek command."""
    global payment_manager
    args = context.args
    if not args:
        user_id = update.effective_user.id
        txs = await payment_manager.get_user_transactions(user_id, limit=1)
        if not txs:
            await update.message.reply_text("\U0001f4ed Tidak ada transaksi.\nGunakan /bayar untuk membuat pembayaran.")
            return
        tx = txs[0]
    else:
        tx = await payment_manager.get_transaction(args[0].upper())
        if not tx:
            await update.message.reply_text(f"\u274c Transaksi <code>{args[0].upper()}</code> tidak ditemukan.", parse_mode="HTML")
            return
    status_text = format_transaction(tx)
    if tx["status"] == "pending":
        status_text += f"\n\n\u23f3 Menunggu pembayaran {format_rupiah(tx['total_amount'])}"
    await update.message.reply_text(status_text, parse_mode="HTML")


async def riwayat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /riwayat command."""
    global payment_manager
    txs = await payment_manager.get_user_transactions(update.effective_user.id, limit=10)
    if not txs:
        await update.message.reply_text("\U0001f4ed Belum ada riwayat transaksi.")
        return
    text = "\U0001f4dc <b>Riwayat Transaksi</b>\n\n"
    for tx in txs:
        text += format_transaction(tx) + "\n\n"
    await update.message.reply_text(text, parse_mode="HTML")


async def batal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /batal command."""
    global payment_manager
    args = context.args
    user_id = update.effective_user.id
    if not args:
        txs = await payment_manager.get_user_transactions(user_id, limit=5)
        pending = [tx for tx in txs if tx["status"] == "pending"]
        if not pending:
            await update.message.reply_text("\u2705 Tidak ada transaksi pending untuk dibatalkan.")
            return
        tx_id = pending[0]["tx_id"]
    else:
        tx_id = args[0].upper()
    success = await payment_manager.cancel_transaction(tx_id, user_id)
    if success:
        await update.message.reply_text(f"\U0001f6ab Transaksi <code>{tx_id}</code> berhasil dibatalkan.", parse_mode="HTML")
    else:
        await update.message.reply_text(f"\u274c Gagal membatalkan <code>{tx_id}</code>.", parse_mode="HTML")



# ========================
# ADMIN COMMANDS - PAYMENT
# ========================

async def confirm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /confirm command (Admin only)."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Anda bukan admin.")
        return
    args = context.args
    if not args or not args[0].isdigit():
        pending = await payment_manager.get_pending_transactions()
        if not pending:
            await update.message.reply_text("\u2705 Tidak ada transaksi pending.")
            return
        text = "\U0001f4cb <b>Transaksi Pending:</b>\n\n"
        for tx in pending[:20]:
            text += f"\u2022 <code>{tx['tx_id']}</code> - <b>{format_rupiah(tx['unique_amount'])}</b> (User: {tx['user_id']})\n"
        text += f"\n\U0001f4a1 Gunakan: <code>/confirm [nominal]</code>"
        await update.message.reply_text(text, parse_mode="HTML")
        return

    amount = int(args[0])
    sender = " ".join(args[1:]) if len(args) > 1 else ""
    matched_tx = await mutation_checker.manual_confirm(amount, sender)

    if matched_tx:
        await update.message.reply_text(
            f"\u2705 <b>Pembayaran Dikonfirmasi!</b>\n\n"
            f"TX ID: <code>{matched_tx['tx_id']}</code>\n"
            f"User: {matched_tx['user_id']}\n"
            f"Nominal: {format_rupiah(matched_tx['total_amount'])}",
            parse_mode="HTML"
        )
        # Notify buyer
        try:
            await context.bot.send_message(
                chat_id=matched_tx["chat_id"],
                text=f"\u2705 <b>Pembayaran Berhasil!</b>\n\nTransaksi <code>{matched_tx['tx_id']}</code> dikonfirmasi.\nNominal: {format_rupiah(matched_tx['total_amount'])}\n\nTerima kasih! \U0001f64f",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to notify user: {e}")

        # Send webhook if this is a merchant transaction
        await _send_merchant_webhook(matched_tx)
    else:
        await update.message.reply_text(
            f"\u274c Tidak ada transaksi pending dengan nominal {format_rupiah(amount)}.",
            parse_mode="HTML"
        )


async def _send_merchant_webhook(tx: Dict):
    """Send webhook to merchant if transaction belongs to one."""
    global merchant_manager, webhook_sender
    if not merchant_manager:
        return
    mtx = await merchant_manager.get_merchant_transaction(tx["tx_id"])
    if not mtx:
        return
    # Mark merchant transaction as paid
    await merchant_manager.mark_merchant_transaction_paid(tx["tx_id"])
    mtx["status"] = "paid"
    mtx["paid_at"] = datetime.now().isoformat()

    merchant = await merchant_manager.get_merchant(mtx["merchant_id"])
    if merchant and mtx.get("callback_url"):
        result = await webhook_sender.send_payment_success(
            url=mtx["callback_url"],
            transaction=mtx,
            webhook_secret=merchant.get("webhook_secret", "")
        )
        response_text = result.get("response", result.get("error", ""))[:200]
        await merchant_manager.mark_webhook_sent(tx["tx_id"], response_text)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats command (Admin only)."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Anda bukan admin.")
        return
    stats = await payment_manager.get_stats()
    text = (
        "\U0001f4ca <b>Statistik Pembayaran</b>\n\n"
        f"\U0001f4e6 Total Transaksi: {stats['total_transactions']}\n"
        f"\u23f3 Pending: {stats['pending']}\n"
        f"\u2705 Sukses: {stats['paid']}\n"
        f"\u274c Expired: {stats['expired']}\n\n"
        f"\U0001f4b0 Total Revenue: {format_rupiah(stats['total_revenue'])}"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /pending command (Admin only)."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Anda bukan admin.")
        return
    pending = await payment_manager.get_pending_transactions()
    if not pending:
        await update.message.reply_text("\u2705 Tidak ada transaksi pending.")
        return
    text = f"\u23f3 <b>Pending ({len(pending)})</b>\n\n"
    for tx in pending[:20]:
        text += f"\u2022 <code>{tx['tx_id']}</code> | {format_rupiah(tx['unique_amount'])} | User:{tx['user_id']}\n"
    text += f"\n\U0001f4a1 <code>/confirm [nominal]</code>"
    await update.message.reply_text(text, parse_mode="HTML")



# ========================
# ADMIN COMMANDS - MERCHANT MANAGEMENT
# ========================

async def addmerchant_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle /addmerchant command (Admin only).
    Usage: /addmerchant [nama] [webhook_url] [fee_percent]
    Example: /addmerchant "Toko Pulsa ABC" https://example.com/webhook 1.5
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Anda bukan admin.")
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "\U0001f4dd <b>Daftarkan Merchant Baru</b>\n\n"
            "Format:\n<code>/addmerchant [nama] [webhook_url] [fee%]</code>\n\n"
            "Contoh:\n<code>/addmerchant TokoPulsaABC https://bot.com/webhook 1.5</code>\n\n"
            "Parameter:\n"
            "\u2022 nama - Nama merchant (tanpa spasi, atau pakai _)\n"
            "\u2022 webhook_url - URL callback (opsional, isi - jika skip)\n"
            "\u2022 fee% - Fee persen per transaksi (default: 1.0)\n",
            parse_mode="HTML"
        )
        return

    name = args[0].replace("_", " ")
    webhook_url = args[1] if len(args) > 1 and args[1] != "-" else ""
    fee_percent = float(args[2]) if len(args) > 2 else 1.0

    merchant = await merchant_manager.create_merchant(
        name=name,
        webhook_url=webhook_url,
        fee_percent=fee_percent
    )

    text = (
        f"\u2705 <b>Merchant Berhasil Didaftarkan!</b>\n\n"
        f"\U0001f3e2 Nama: {merchant['name']}\n"
        f"\U0001f194 Merchant ID: <code>{merchant['merchant_id']}</code>\n"
        f"\U0001f511 API Key: <code>{merchant['api_key']}</code>\n"
        f"\U0001f510 Webhook Secret: <code>{merchant['webhook_secret']}</code>\n"
        f"\U0001f517 Webhook URL: {merchant['webhook_url'] or '-'}\n"
        f"\U0001f4b8 Fee: {merchant['fee_percent']}%\n\n"
        f"\u26a0\ufe0f <b>SIMPAN API KEY!</b> Hanya ditampilkan sekali.\n\n"
        f"<b>Contoh penggunaan API:</b>\n"
        f"<code>curl -X POST http://SERVER:8000/api/v1/payment/create \\\n"
        f"  -H \"Authorization: Bearer {merchant['api_key']}\" \\\n"
        f"  -H \"Content-Type: application/json\" \\\n"
        f"  -d '{{\"amount\": 50000, \"product_name\": \"Pulsa\"}}'</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def merchants_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /merchants command (Admin only) - list all merchants."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Anda bukan admin.")
        return

    merchants = await merchant_manager.list_merchants(active_only=False)
    if not merchants:
        await update.message.reply_text("\U0001f4ed Belum ada merchant terdaftar.\nGunakan /addmerchant untuk menambahkan.")
        return

    text = f"\U0001f3e2 <b>Daftar Merchant ({len(merchants)})</b>\n\n"
    for m in merchants[:20]:
        status = "\u2705" if m["is_active"] else "\u274c"
        text += (
            f"{status} <b>{m['name']}</b>\n"
            f"   ID: <code>{m['merchant_id']}</code>\n"
            f"   Fee: {m['fee_percent']}% | Webhook: {'Ya' if m['webhook_url'] else 'Tidak'}\n\n"
        )
    await update.message.reply_text(text, parse_mode="HTML")


async def merchantinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /merchantinfo [merchant_id] - detail info."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Anda bukan admin.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: <code>/merchantinfo MCH-XXXXXXXX</code>", parse_mode="HTML")
        return

    merchant = await merchant_manager.get_merchant(args[0].upper())
    if not merchant:
        await update.message.reply_text("\u274c Merchant tidak ditemukan.")
        return

    stats = await merchant_manager.get_merchant_stats(merchant["merchant_id"])
    text = (
        f"\U0001f3e2 <b>Detail Merchant</b>\n\n"
        f"Nama: {merchant['name']}\n"
        f"ID: <code>{merchant['merchant_id']}</code>\n"
        f"Status: {'Aktif \u2705' if merchant['is_active'] else 'Nonaktif \u274c'}\n"
        f"Fee: {merchant['fee_percent']}% + Rp {merchant['fee_fixed']}\n"
        f"Webhook: {merchant['webhook_url'] or '-'}\n"
        f"Dibuat: {merchant['created_at'][:16]}\n\n"
        f"\U0001f4ca <b>Statistik:</b>\n"
        f"\u23f3 Pending: {stats['pending']}\n"
        f"\u2705 Paid: {stats['paid']}\n"
        f"\U0001f4b0 Volume: {format_rupiah(stats['total_volume'])}\n"
        f"\U0001f4b8 Fee Terkumpul: {format_rupiah(stats['total_fees'])}\n"
        f"\U0001f4b5 Net Merchant: {format_rupiah(stats['total_net'])}"
    )
    await update.message.reply_text(text, parse_mode="HTML")



async def revokekey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /revokekey [merchant_id] - revoke and regenerate API key."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Anda bukan admin.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: <code>/revokekey MCH-XXXXXXXX</code>", parse_mode="HTML")
        return

    new_key = await merchant_manager.revoke_api_key(args[0].upper())
    if new_key:
        await update.message.reply_text(
            f"\u2705 API Key berhasil di-revoke!\n\n"
            f"Merchant: <code>{args[0].upper()}</code>\n"
            f"New API Key: <code>{new_key}</code>\n\n"
            f"\u26a0\ufe0f Simpan key baru! Key lama sudah tidak berlaku.",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("\u274c Merchant tidak ditemukan.")


async def setfee_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setfee [merchant_id] [percent] - set merchant fee."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Anda bukan admin.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: <code>/setfee MCH-XXXXXXXX 1.5</code>\nContoh: fee 1.5%",
            parse_mode="HTML"
        )
        return

    merchant_id = args[0].upper()
    fee_percent = float(args[1])
    success = await merchant_manager.update_fee(merchant_id, fee_percent=fee_percent)
    if success:
        await update.message.reply_text(
            f"\u2705 Fee merchant <code>{merchant_id}</code> diubah ke <b>{fee_percent}%</b>",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("\u274c Gagal mengubah fee.")


async def deactivate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /deactivate [merchant_id]."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Anda bukan admin.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: <code>/deactivate MCH-XXXXXXXX</code>", parse_mode="HTML")
        return
    await merchant_manager.deactivate_merchant(args[0].upper())
    await update.message.reply_text(f"\U0001f6ab Merchant <code>{args[0].upper()}</code> dinonaktifkan.", parse_mode="HTML")


async def activate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /activate [merchant_id]."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Anda bukan admin.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: <code>/activate MCH-XXXXXXXX</code>", parse_mode="HTML")
        return
    await merchant_manager.activate_merchant(args[0].upper())
    await update.message.reply_text(f"\u2705 Merchant <code>{args[0].upper()}</code> diaktifkan kembali.", parse_mode="HTML")


async def gatewaystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /gatewaystats - overall gateway statistics."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Anda bukan admin.")
        return

    gw_stats = await merchant_manager.get_gateway_stats()
    pm_stats = await payment_manager.get_stats()

    text = (
        "\U0001f680 <b>Gateway Statistics</b>\n\n"
        f"<b>Merchants:</b>\n"
        f"\u2022 Total: {gw_stats['total_merchants']}\n"
        f"\u2022 Aktif: {gw_stats['active_merchants']}\n\n"
        f"<b>Transaksi (via API):</b>\n"
        f"\u2022 Total Paid: {gw_stats['total_paid_transactions']}\n"
        f"\u2022 Volume: {format_rupiah(gw_stats['total_volume'])}\n"
        f"\u2022 Fee Earned: {format_rupiah(gw_stats['total_fees_earned'])}\n\n"
        f"<b>Semua Transaksi:</b>\n"
        f"\u2022 Total: {pm_stats['total_transactions']}\n"
        f"\u2022 Pending: {pm_stats['pending']}\n"
        f"\u2022 Paid: {pm_stats['paid']}\n"
        f"\u2022 Revenue: {format_rupiah(pm_stats['total_revenue'])}"
    )
    await update.message.reply_text(text, parse_mode="HTML")



# ========================
# APPLICATION SETUP
# ========================

async def post_init(application: Application):
    """Initialize services after bot starts."""
    global payment_manager, mutation_checker, merchant_manager, webhook_sender

    payment_manager = PaymentManager(db_path="payments.db", expiry_minutes=EXPIRY_MINUTES)
    await payment_manager.initialize()

    merchant_manager = MerchantManager(db_path="payments.db")
    await merchant_manager.initialize(payment_manager._db)

    webhook_sender = WebhookSender(max_retries=3, timeout=10)

    mutation_checker = MutationChecker(payment_manager=payment_manager, check_interval=CHECK_INTERVAL)

    async def notify_payment(tx: Dict):
        try:
            if tx.get("chat_id") and tx["chat_id"] != 0:
                await application.bot.send_message(
                    chat_id=tx["chat_id"],
                    text=f"\u2705 <b>Pembayaran Berhasil!</b>\n\nTransaksi <code>{tx['tx_id']}</code> dikonfirmasi.\nNominal: {format_rupiah(tx['total_amount'])}\n\nTerima kasih! \U0001f64f",
                    parse_mode="HTML"
                )
            for admin_id in ADMIN_IDS:
                await application.bot.send_message(
                    chat_id=admin_id,
                    text=f"\U0001f4b0 <b>Payment!</b> TX:<code>{tx['tx_id']}</code> | {format_rupiah(tx['total_amount'])}",
                    parse_mode="HTML"
                )
            # Send merchant webhook
            await _send_merchant_webhook(tx)
        except Exception as e:
            logger.error(f"Notification error: {e}")

    mutation_checker.set_payment_callback(notify_payment)
    await mutation_checker.start()

    # Start API server in background thread
    _start_api_server()

    logger.info("Bot + API Gateway initialized!")


async def post_shutdown(application: Application):
    """Cleanup on shutdown."""
    global payment_manager, mutation_checker
    if mutation_checker:
        await mutation_checker.stop()
    if payment_manager:
        await payment_manager.close()
    logger.info("Bot shutdown complete.")


def _start_api_server():
    """Start FastAPI server in a background thread."""
    def run():
        import uvicorn
        from api_server import app
        uvicorn.run(app, host="0.0.0.0", port=API_PORT, log_level="warning")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    logger.info(f"API Server started on port {API_PORT}")


def main():
    """Main entry point."""
    if not BOT_TOKEN:
        print("\u274c ERROR: BOT_TOKEN not set!")
        return
    if not QRIS_STATIC:
        print("\u26a0\ufe0f WARNING: QRIS_STATIC not set!")
    if not ADMIN_IDS:
        print("\u26a0\ufe0f WARNING: ADMIN_IDS not set!")

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

    # Admin - Payment
    application.add_handler(CommandHandler("confirm", confirm_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("pending", pending_command))

    # Admin - Merchant Management
    application.add_handler(CommandHandler("addmerchant", addmerchant_command))
    application.add_handler(CommandHandler("merchants", merchants_command))
    application.add_handler(CommandHandler("merchantinfo", merchantinfo_command))
    application.add_handler(CommandHandler("revokekey", revokekey_command))
    application.add_handler(CommandHandler("setfee", setfee_command))
    application.add_handler(CommandHandler("deactivate", deactivate_command))
    application.add_handler(CommandHandler("activate", activate_command))
    application.add_handler(CommandHandler("gatewaystats", gatewaystats_command))

    # Callbacks
    application.add_handler(CallbackQueryHandler(callback_handler))

    print("\U0001f916 Bot + API Gateway started!")
    print(f"   Admin IDs: {ADMIN_IDS}")
    print(f"   QRIS: {'\u2705' if QRIS_STATIC else '\u274c'}")
    print(f"   API Port: {API_PORT}")
    print(f"   Expiry: {EXPIRY_MINUTES} min")

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
