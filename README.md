<div align="center">

# ⚡️ All-in-One Media & Music Telegram Downloader Bot

[![Python](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/)
[![Framework](https://img.shields.io/badge/Framework-Pyrogram%20(MTProto)-orange.svg)](https://github.com/pyrogram/pyrogram)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![User-Space](https://img.shields.io/badge/Rootless-100%25%20Portable-success.svg)](#)

ربات همه‌کاره و قدرتمند دانلود رسانه و موسیقی از شبکه‌های اجتماعی برای تلگرام، با معماری سبک، کاملاً بدون روت (Rootless) و سازگار با انواع سرورهای اشتراکی و VPS.

A high-performance, asynchronous Telegram media and music downloader bot powered by **Pyrogram (MTProto)** and **yt-dlp**, built for portable, rootless hosting.

[فارسی](#فارسی) • [English](#english)

---

</div>

## 🌐 Supported Platforms / پلتفرم‌های تحت پوشش

| Platform / پلتفرم | Features / امکانات |
| :--- | :--- |
| **YouTube** | ویدیوهای 1080p, 720p, 480p, 360p، شورتس و استخراج صوت اختصاصی MP3 |
| **Instagram** | ریلز، پست‌های تک‌عکس، ویدیو، آلبوم‌های چندتایی (Carousels) با کپشن و استخراج موزیک |
| **Spotify** | دانلود ۳۲۰kbps قطعات منفرد، آلبوم‌ها و پلی‌لیست‌ها با تگ‌های رسمی ID3، کاور و لیریکس همگام |
| **SoundCloud** | قطعات تک با کاور و تگ، آلبوم‌ها/ست‌ها با عبور هوشمند از قطعات قفل‌شده (DRM/Go+ Bypass) |
| **TikTok** | دانلود ویدیوهای بدون واترمارک (HD) و اسلایدهای تصویری چندتایی |
| **Pinterest** | دانلود مستقیم تصاویر با کیفیت اصلی و ویدیوها |
| **Twitter / X** | ویدیوها، گیف‌های بی‌صدا، تک‌عکس و پست‌های چندعکس |
| **Reddit** | ویدیوها و گیف‌های صوتی با کیفیت بالا |

---

## ✨ Key Features / ویژگی‌های کلیدی

- **⚡️ 2GB File Upload Support (Pyrogram MTProto):** آپلود سریع فایل‌ها تا سقف ۲ گیگابایت بدون محدودیت Bot API استاندارد.
- **🛡 Safe Concurrency & Smart Queues:** مدیریت خودکار صف دانلود برای سرورهای کم‌مصرف (۱ گیگ رم)، جلوگیری از کرش OOM، و گارد هوشمند لینک تکراری.
- **🌐 100% Bilingual Parity (FA / EN):** دو زبانه بودن کامل تمام منوها، دستورات، پنل مدیریت، اخطارها و نوتیفیکیشن‌ها.
- **👑 Full-Featured Admin Panel (`/admin`):** وضعیت زنده منابع سرور (CPU, RAM, Uptime)، قفل عضویت اجباری چندکاناله (FSub) تفکیک‌شده، برودکست و فوروارد همگانی، و سیستم بن/آن‌بن.
- **📩 Banned Users In-Chat Appeal:** سیستم هوشمند ارسال درخواست تجدیدنظر برای کاربران مسدودشده با دکمه آن‌بن مستقیم برای ادمین.
- **🔍 Inline Mode Integration:** امکان جستجوی سریع و اشتراک مستقیم رسانه‌ها در هر چت یا گروه.
- **🧹 Non-Root Janitor:** اسکریپت دوره‌ای مستقل برای پاکسازی فایل‌های یتیم بدون تداخل با دانلودهای زنده.

---

## 🚀 Quick Start / نصب و راه‌اندازی سریع

### روش ۱: نصب تک‌کلیکه با اسکریپت خودکار (توصیه‌شده)

```bash
git clone https://github.com/EINDRAL/all-in-one-downloader-bot.git
cd all-in-one-downloader-bot
bash install.sh
```
اسکریپت به صورت تعاملی توکن ربات (`BOT_TOKEN`) و آیدی عددی ادمین (`ADMIN_ID`) را دریافت کرده و ربات را آماده اجرا می‌کند.

---

### روش ۲: نصب دستی

۱. مخزن را کلون کنید:
```bash
git clone https://github.com/EINDRAL/all-in-one-downloader-bot.git
cd all-in-one-downloader-bot
```

۲. محیط مجازی پایتون را ایجاد و فعال کنید:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

۳. فایل پیکربندی را بسازید:
```bash
cp .env.example .env
nano .env
```
مقادیر `BOT_TOKEN` (از @BotFather) و `ADMIN_ID` (آیدی عددی ادمین) را وارد کنید.

۴. ربات را اجرا کنید:
```bash
python bot.py
```

---

## ⚙️ Configuration / پیکربندی (`.env`)

| متغیر | توضیحات | نمونه |
| :--- | :--- | :--- |
| `BOT_TOKEN` | توکن دریافتی از BotFather | `123456789:ABCdefGh...` |
| `ADMIN_ID` | شناسه عددی اکانت ادمین اصلی | `1429926943` |
| `DOWNLOAD_DIR` | پوشه ذخیره موقت فایل‌ها | `downloads` |

> 💡 **نکته یوتیوب:** در صورت نیاز به دور زدن محدودیت‌های سنی یا ربات‌سنجی یوتیوب در برخی سرورها، فایل `cookies.txt` را در ریشه پروژه قرار دهید (این فایل به طور خودکار شناسایی و استفاده خواهد شد).

---

## 👨‍💻 Developer & Credits / سازنده

- **Author:** Mohammad Yousef Morovajnia
- **Telegram:** [@EINDRAL](https://t.me/EINDRAL)
- **GitHub:** [@EINDRAL](https://github.com/EINDRAL)

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
