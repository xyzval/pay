# QRIS Payment Gateway

Payment Gateway QRIS untuk menerima pembayaran otomatis via QRIS Mitra Bukalapak.  
Dilengkapi REST API + API Key system agar bisa digunakan oleh bot/website lain sebagai payment gateway.

## Arsitektur

```
┌────────────────────────────────────────────────────────────┐
│  PAYMENT GATEWAY (Server Anda)                             │
│                                                            │
│  ┌──────────────┐     ┌──────────────┐                    │
│  │ Telegram Bot │     │  REST API    │                    │
│  │  (bot.py)    │     │ (api_server) │                    │
│  │              │     │  Port 8000   │                    │
│  └──────┬───────┘     └──────┬───────┘                    │
│         │                    │                             │
│         ▼                    ▼                             │
│  ┌─────────────────────────────────────┐                  │
│  │       Payment Manager               │                  │
│  │  + QRIS Converter (statis→dinamis)  │                  │
│  │  + Merchant Manager (API keys)      │                  │
│  │  + Webhook Sender                   │                  │
│  │  + SQLite Database                  │                  │
│  └─────────────────────────────────────┘                  │
└────────────────────────────────────────────────────────────┘
         │                    │
         ▼                    ▼
  ┌────────────┐    ┌─────────────────┐
  │ Direct Pay │    │ Merchant Bots   │
  │ via Bot    │    │ via API Key     │
  └────────────┘    └─────────────────┘
```

## Fitur

### Payment Gateway
- REST API dengan autentikasi API Key
- Multi-merchant support
- Fee system (persentase + fixed)
- Webhook callback ke merchant (HMAC-SHA256 signed)
- QRIS dinamis otomatis (dari QRIS statis Mitra Bukalapak)
- Nominal unik per transaksi
- Auto-expire transaksi
- API documentation (Swagger UI di /docs)

### Telegram Bot
- Pembayaran langsung via bot (/bayar)
- Admin: konfirmasi manual, statistik
- Admin: merchant management (add, revoke, deactivate)
- Notifikasi otomatis ke buyer & admin

## Quick Start

```bash
# 1. Clone repo
git clone https://github.com/xyzval/pay.git
cd pay

# 2. Install dependencies
pip install -r requirements.txt

# 3. Setup konfigurasi
cp .env.example .env
# Edit .env → isi BOT_TOKEN, ADMIN_IDS, QRIS_STATIC

# 4. Jalankan (Bot + API berjalan bersamaan)
python bot.py
```

Bot Telegram dan API Server (port 8000) langsung jalan bersamaan.

## Konfigurasi (.env)

| Variable | Keterangan |
|----------|-----------|
| `BOT_TOKEN` | Token bot dari @BotFather |
| `ADMIN_IDS` | ID Telegram admin (pisah koma) |
| `QRIS_STATIC` | Data string QRIS Mitra Bukalapak |
| `EXPIRY_MINUTES` | Waktu expired pembayaran (default: 30) |
| `CHECK_INTERVAL` | Interval cek mutasi (default: 15 detik) |
| `API_PORT` | Port REST API (default: 8000) |

## API Documentation

Setelah bot jalan, buka Swagger UI: `http://SERVER_IP:8000/docs`

### Endpoints

| Method | Endpoint | Keterangan |
|--------|----------|-----------|
| POST | `/api/v1/payment/create` | Buat pembayaran baru |
| GET | `/api/v1/payment/status/{tx_id}` | Cek status pembayaran |
| POST | `/api/v1/payment/cancel/{tx_id}` | Batalkan pembayaran |
| GET | `/api/v1/merchant/balance` | Lihat saldo & statistik |
| GET | `/api/v1/merchant/transactions` | Riwayat transaksi |
| GET | `/health` | Health check |

### Autentikasi

Semua endpoint memerlukan header:
```
Authorization: Bearer PAY-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### Contoh: Buat Pembayaran

```bash
curl -X POST http://localhost:8000/api/v1/payment/create \
  -H "Authorization: Bearer PAY-your-api-key-here" \
  -H "Content-Type: application/json" \
  -d '{
    "amount": 50000,
    "product_name": "Pulsa XL 50rb",
    "callback_url": "https://yourbot.com/webhook",
    "metadata": "{\"order_id\": \"ORD-123\"}"
  }'
```

Response:
```json
{
  "success": true,
  "data": {
    "tx_id": "TX1234567890123",
    "amount": 50347,
    "base_amount": 50000,
    "fee_amount": 503,
    "net_amount": 49844,
    "qris_string": "000201010211...",
    "qr_image_base64": "iVBORw0KGgo...",
    "status": "pending",
    "expires_at": "2025-01-01T12:30:00"
  }
}
```

### Contoh: Cek Status

```bash
curl http://localhost:8000/api/v1/payment/status/TX1234567890123 \
  -H "Authorization: Bearer PAY-your-api-key-here"
```

### Webhook Callback

Saat pembayaran dikonfirmasi, gateway mengirim POST ke webhook URL:

```json
{
  "event": "payment.success",
  "data": {
    "tx_id": "TX1234567890123",
    "merchant_id": "MCH-ABCD1234",
    "amount": 50347,
    "fee_amount": 503,
    "net_amount": 49844,
    "status": "paid",
    "paid_at": "2025-01-01T12:15:00",
    "metadata": "{\"order_id\": \"ORD-123\"}"
  },
  "timestamp": 1704110100
}
```

Headers yang dikirim:
```
Content-Type: application/json
X-Webhook-Event: payment.success
X-Webhook-Signature: hmac-sha256-signature
X-Webhook-Timestamp: unix-timestamp
```

Verifikasi signature:
```python
import hmac, hashlib
expected = hmac.new(
    webhook_secret.encode(),
    request_body.encode(),
    hashlib.sha256
).hexdigest()
assert expected == request.headers["X-Webhook-Signature"]
```

## Bot Commands

### User
| Command | Keterangan |
|---------|-----------|
| `/start` | Mulai bot |
| `/bayar [nominal] [ket]` | Buat pembayaran |
| `/cek [TX_ID]` | Cek status |
| `/riwayat` | Riwayat transaksi |
| `/batal [TX_ID]` | Batalkan pembayaran |

### Admin - Payment
| Command | Keterangan |
|---------|-----------|
| `/confirm [nominal]` | Konfirmasi pembayaran |
| `/stats` | Statistik |
| `/pending` | Lihat transaksi pending |

### Admin - Merchant
| Command | Keterangan |
|---------|-----------|
| `/addmerchant [nama] [webhook] [fee%]` | Daftarkan merchant |
| `/merchants` | Lihat semua merchant |
| `/merchantinfo [ID]` | Detail merchant |
| `/revokekey [ID]` | Revoke API key |
| `/setfee [ID] [percent]` | Ubah fee |
| `/deactivate [ID]` | Nonaktifkan merchant |
| `/activate [ID]` | Aktifkan merchant |
| `/gatewaystats` | Statistik gateway |

## Alur Merchant (Bot Lain Pakai Gateway Ini)

```
1. Admin daftarkan merchant:
   /addmerchant BotPulsaXYZ https://botxyz.com/webhook 1.5

2. Merchant dapat API Key:
   PAY-a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6

3. Bot merchant panggil API:
   POST /api/v1/payment/create
   → Dapat QR image (base64) + tx_id

4. Bot merchant tampilkan QR ke customer

5. Customer scan & bayar

6. Admin /confirm → Gateway kirim webhook ke merchant

7. Bot merchant terima webhook → proses order
```

## Struktur File

```
pay/
├── bot.py               ← Main (Bot + API launcher)
├── api_server.py        ← REST API (FastAPI)
├── qris_converter.py    ← QRIS statis → dinamis
├── payment_manager.py   ← Transaksi & mutasi
├── merchant_manager.py  ← Merchant & API key
├── webhook_sender.py    ← Webhook ke merchant
├── requirements.txt     ← Dependencies
├── .env                 ← Konfigurasi (private)
├── .env.example         ← Template konfigurasi
├── .gitignore           ← Ignore rules
└── README.md            ← Dokumentasi (file ini)
```

## Deploy

### Systemd (VPS Linux)

```ini
[Unit]
Description=QRIS Payment Gateway
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/pay
ExecStart=/home/ubuntu/pay/venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["python", "bot.py"]
```

```bash
docker build -t qris-gateway .
docker run -d --name qris-gateway -p 8000:8000 --env-file .env qris-gateway
```

## Lisensi

MIT License
