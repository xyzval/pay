# Bot Telegram QRIS Payment Gateway

Bot Telegram untuk menerima pembayaran otomatis via QRIS Mitra Bukalapak.  
Bot mengkonversi QRIS statis menjadi QRIS dinamis (dengan nominal unik) sehingga setiap pembayaran bisa diidentifikasi secara otomatis.

## Cara Kerja

```
User ketik /bayar 50000
       │
       ▼
Bot generate nominal unik (misal: 50.347)
       │
       ▼
QRIS statis dikonversi ke QRIS dinamis (dengan nominal 50.347)
       │
       ▼
Bot kirim gambar QR ke user
       │
       ▼
User scan & bayar TEPAT Rp 50.347
       │
       ▼
Admin cek mutasi → /confirm 50347
       │
       ▼
Bot notif ke user: "Pembayaran Berhasil!"
```

## Fitur

- Konversi QRIS statis ke dinamis (dengan nominal)
- Nominal unik per transaksi untuk identifikasi pembayaran
- Inline keyboard untuk pilih nominal
- Auto-expire transaksi (default 30 menit)
- Notifikasi otomatis ke buyer & admin
- Riwayat transaksi per user
- Database SQLite (ringan, tanpa setup server)
- Admin panel: konfirmasi manual, statistik, lihat pending

## Prasyarat

- Python 3.9+
- Akun Telegram Bot (dari @BotFather)
- QRIS Mitra Bukalapak (data string dari QR Code)

## Instalasi

### 1. Clone / Download Project

```bash
cd qris-telegram-bot
```

### 2. Buat Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
# atau
venv\Scripts\activate     # Windows
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Konfigurasi .env

```bash
cp .env.example .env
nano .env  # edit sesuai data Anda
```

Isi file `.env`:

| Variable | Keterangan |
|----------|-----------|
| `BOT_TOKEN` | Token bot dari @BotFather |
| `ADMIN_IDS` | ID Telegram admin (pisah koma) |
| `QRIS_STATIC` | Data string QRIS Mitra Bukalapak |
| `EXPIRY_MINUTES` | Waktu expired pembayaran (default: 30) |
| `CHECK_INTERVAL` | Interval cek mutasi dalam detik (default: 15) |

### 5. Jalankan Bot

```bash
python bot.py
```

## Cara Mendapatkan Data QRIS

1. Buka aplikasi **Mitra Bukalapak**
2. Masuk ke menu **QRIS / Terima Pembayaran**
3. Screenshot QR Code yang ditampilkan
4. Decode QR menggunakan salah satu cara:
   - Website: https://webqr.com atau https://zxing.org/w/decode.jspx
   - Aplikasi: QR Scanner yang bisa copy text
5. Hasilnya berupa string panjang dimulai dengan `0002010101...`
6. Copy string tersebut ke `QRIS_STATIC` di file `.env`

## Cara Mendapatkan Bot Token

1. Buka Telegram, cari **@BotFather**
2. Kirim `/newbot`
3. Ikuti instruksi (nama bot, username bot)
4. Copy token yang diberikan ke `BOT_TOKEN` di `.env`

## Cara Mendapatkan User ID (Admin)

1. Buka Telegram, cari **@userinfobot**
2. Kirim `/start`
3. Bot akan membalas ID Anda
4. Masukkan ID tersebut ke `ADMIN_IDS` di `.env`

## Commands

### User Commands

| Command | Keterangan |
|---------|-----------|
| `/start` | Mulai bot |
| `/bayar [nominal] [keterangan]` | Buat pembayaran baru |
| `/cek [TX_ID]` | Cek status transaksi |
| `/riwayat` | Lihat riwayat transaksi |
| `/batal [TX_ID]` | Batalkan transaksi pending |
| `/help` | Panduan penggunaan |

### Admin Commands

| Command | Keterangan |
|---------|-----------|
| `/confirm [nominal]` | Konfirmasi pembayaran (cocokkan nominal) |
| `/stats` | Lihat statistik transaksi |
| `/pending` | Lihat semua transaksi pending |

## Alur Konfirmasi Pembayaran

### Mode Manual (Default)

1. User bayar via QRIS
2. Admin cek mutasi di aplikasi Mitra Bukalapak
3. Admin lihat ada pembayaran masuk, misal Rp 50.347
4. Admin ketik `/confirm 50347` di bot
5. Bot otomatis cocokkan dengan transaksi pending
6. User dapat notifikasi pembayaran berhasil

### Mode Otomatis (Advanced)

Untuk mode full-otomatis, Anda bisa mengintegrasikan layanan cek mutasi seperti:

- **Mutasiku** (https://mutasiku.id) - API cek mutasi GoPay/DANA
- **Moota** (https://moota.co) - API cek mutasi bank
- Custom scraping (tidak disarankan)

Edit file `payment_manager.py`, pada method `_fetch_mutations()` di class `MutationChecker`.

## Struktur File

```
qris-telegram-bot/
├── bot.py              # Main bot (entry point)
├── qris_converter.py   # Konversi QRIS statis ke dinamis
├── payment_manager.py  # Manajemen transaksi & mutasi
├── requirements.txt    # Python dependencies
├── .env.example        # Template konfigurasi
├── .gitignore          # Git ignore rules
└── README.md           # Dokumentasi (file ini)
```

## Deploy ke VPS/Server

### Menggunakan systemd (Linux)

1. Copy project ke server
2. Install dependencies
3. Buat service file:

```bash
sudo nano /etc/systemd/system/qris-bot.service
```

```ini
[Unit]
Description=QRIS Telegram Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/qris-telegram-bot
Environment=PATH=/home/ubuntu/qris-telegram-bot/venv/bin
ExecStart=/home/ubuntu/qris-telegram-bot/venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

4. Aktifkan service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable qris-bot
sudo systemctl start qris-bot
```

5. Cek status:

```bash
sudo systemctl status qris-bot
```

### Menggunakan Docker (Opsional)

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
```

```bash
docker build -t qris-bot .
docker run -d --name qris-bot --env-file .env qris-bot
```

## FAQ

**Q: Kenapa nominalnya ada angka unik (misal 50.347 bukan 50.000)?**  
A: Angka unik digunakan untuk mengidentifikasi pembayaran. Karena QRIS Mitra Bukalapak tidak punya webhook/callback, kita perlu cara untuk mencocokkan pembayaran yang masuk dengan transaksi di bot.

**Q: Bagaimana kalau 2 user bayar nominal yang sama?**  
A: Bot otomatis generate angka unik yang berbeda untuk setiap transaksi, jadi tidak akan bentrok.

**Q: Apakah aman?**  
A: Data QRIS dan token bot disimpan di file `.env` lokal (tidak di-commit ke git). Database transaksi tersimpan lokal di SQLite.

**Q: Bisa pakai QRIS selain Mitra Bukalapak?**  
A: Ya! Bisa pakai QRIS statis dari provider manapun (GoPay Merchant, DANA Merchant, OVO, dll). Yang penting copy data string dari QR Code-nya.

**Q: Apakah bisa full otomatis tanpa admin /confirm?**  
A: Bisa, dengan mengintegrasikan API cek mutasi. Lihat bagian "Mode Otomatis" di atas.

## Lisensi

MIT License - Bebas digunakan dan dimodifikasi.

## Disclaimer

Bot ini menggunakan metode konversi QRIS statis ke dinamis yang merupakan teknik umum. Pastikan penggunaan sesuai dengan ketentuan layanan Mitra Bukalapak dan regulasi Bank Indonesia terkait QRIS.
