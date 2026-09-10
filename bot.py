import os
import re
import sys
import time
import json
import html
import sqlite3
import asyncio
import logging
import threading
import psutil
import subprocess
import aiohttp
import yarl
import yt_dlp
import syncedlyrics
from pathlib import Path
from dotenv import load_dotenv
from pyrogram import Client, filters, enums, idle
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultPhoto,
    InputTextMessageContent,
    InputMediaPhoto,
    InputMediaVideo,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats
)
from pyrogram.errors import UserNotParticipant
import instaloader
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found in environment")

API_ID = int(os.getenv("API_ID", "2040"))
API_HASH = os.getenv("API_HASH", "b18441a1ff607e10a989891a5462e627")
ADMIN_ID = int(os.getenv("ADMIN_ID", "1429926943"))

# Resolve project root dynamically so it works anywhere without root paths
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / os.getenv("DOWNLOAD_DIR", "downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

DB_PATH = BASE_DIR / "users.db"
COOKIES_FILE = BASE_DIR / "cookies.txt"
NODE_BIN = Path(os.getenv("NODE_BIN", "node"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("DownloaderBot")

app = Client(
    "downloader_bot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=str(BASE_DIR)
)

MEDIA_CACHE = {}
PENDING_ADMIN_ACTION = {}
PENDING_APPEAL_USERS = set()  # Users who clicked the appeal button and are ready to type their message

def store_media_cache(vid_id: str, data: dict):
    # Store in cache with LRU cleanup to prevent RAM bloat (keep max 100 recent items)
    if len(MEDIA_CACHE) > 100:
        oldest_key = next(iter(MEDIA_CACHE))
        MEDIA_CACHE.pop(oldest_key, None)
    MEDIA_CACHE[vid_id] = data

# Safe Queue Management
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(2)  # Max 2 concurrent downloads when queue is enabled
ACTIVE_DOWNLOADS = 0

# Active Download Tasks per User for /stop cancellation: {user_id: asyncio.Task}
ACTIVE_USER_TASKS = {}
# Active Download Tasks per Group for /stop cancellation: {chat_id: {"task": asyncio.Task, "user_id": int}}
ACTIVE_GROUP_TASKS = {}

# Active Cancellation Events for instant clean aborts:
ACTIVE_CANCEL_TOKENS = {}         # {user_id: threading.Event}
ACTIVE_GROUP_CANCEL_TOKENS = {}   # {chat_id: threading.Event}

# Group queue semaphores: {group_chat_id: asyncio.Semaphore(1)}
GROUP_QUEUES = {}
# User queue semaphores (PM sequential downloads): {user_id: asyncio.Semaphore(1)}
USER_QUEUES = {}

# Anti-spam tracker in memory: {user_id: {"timestamps": [float], "warned": bool}}
SPAM_TRACKER = {}

# In-flight active link downloads to prevent duplicate processing while currently downloading
# Stores active scope keys: (uid, norm_url) for PM, (chat_id, norm_url) for Group
ACTIVE_URL_DOWNLOADS = set()

def is_queue_enabled() -> bool:
    # Default: 1 (Enabled) for safety on 1GB VPS, can be toggled by admin
    return get_setting("download_queue", "1") == "1"

def toggle_queue() -> bool:
    current = is_queue_enabled()
    new_val = "0" if current else "1"
    set_setting("download_queue", new_val)
    return new_val == "1"

# ----------------- Database & i18n -----------------

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                lang TEXT DEFAULT 'fa',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shared_links (
                token TEXT PRIMARY KEY,
                url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                reason TEXT,
                banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

init_db()

def get_banned_markup(user_lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr("banned_btn_appeal", user_lang), callback_data="appeal:start")]
    ])

def is_user_banned(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return False
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,))
        return cursor.fetchone() is not None

def ban_user(user_id: int, username: str = "", reason: str = "Spam") -> bool:
    if user_id == ADMIN_ID:
        logger.warning(f"Ignored attempt to ban admin {user_id}")
        return False
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO banned_users (user_id, username, reason)
            VALUES (?, ?, ?)
        """, (user_id, username or "", reason))
        conn.commit()
    return True

def unban_user(user_id_or_username: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        clean = user_id_or_username.strip().lstrip("@")
        if clean.isdigit():
            cursor.execute("DELETE FROM banned_users WHERE user_id = ?", (int(clean),))
        else:
            cursor.execute("DELETE FROM banned_users WHERE LOWER(username) = LOWER(?)", (clean,))
        conn.commit()
        return cursor.rowcount > 0

def get_banned_users() -> list:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, username, reason, banned_at FROM banned_users ORDER BY banned_at DESC")
        return cursor.fetchall()

def get_setting(key: str, default: str = "") -> str:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row[0] if row and row[0] is not None else default

def set_setting(key: str, value: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, value))
        conn.commit()

def get_all_users() -> list:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users")
        return [row[0] for row in cursor.fetchall()]

def get_total_users_count() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        return cursor.fetchone()[0]

def get_force_channels() -> list:
    val = get_setting("force_channels", "").strip()
    if not val:
        # Backward compatibility with single channel
        old_val = get_setting("force_channel", "").strip()
        if old_val:
            return [old_val]
        return []
    return [ch.strip() for ch in val.split(",") if ch.strip()]

def add_force_channel(channel: str) -> bool:
    channels = get_force_channels()
    clean = channel.strip()
    if not clean.startswith("@"):
        clean = f"@{clean}"
    if clean not in channels:
        channels.append(clean)
        set_setting("force_channels", ",".join(channels))
        return True
    return False

def remove_force_channel(channel: str) -> bool:
    channels = get_force_channels()
    clean = channel.strip()
    if not clean.startswith("@"):
        clean = f"@{clean}"
    if clean in channels:
        channels.remove(clean)
        set_setting("force_channels", ",".join(channels))
        return True
    return False

def is_fsub_enabled_for(scope: str) -> bool:
    # scope: 'pm', 'group', 'inline'
    # Default: PM enabled (1), group disabled (0), inline disabled (0)
    default_val = "1" if scope == "pm" else "0"
    return get_setting(f"fsub_{scope}", default_val) == "1"

def toggle_fsub_scope(scope: str) -> bool:
    current = is_fsub_enabled_for(scope)
    new_val = "0" if current else "1"
    set_setting(f"fsub_{scope}", new_val)
    return new_val == "1"

async def get_unjoined_channels(client: Client, user_id: int) -> list:
    channels = get_force_channels()
    if not channels:
        return []
    unjoined = []
    for ch in channels:
        try:
            chat_member = await client.get_chat_member(ch, user_id)
            if chat_member.status not in [enums.ChatMemberStatus.MEMBER, enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
                unjoined.append(ch)
        except UserNotParticipant:
            # User is definitely not in the channel!
            unjoined.append(ch)
        except Exception as e:
            logger.warning(f"Force join check error for {user_id} in {ch}: {e}")
            # If bot itself cannot check (e.g. not admin in channel), do not block
            pass
    return unjoined

def get_multi_force_join_markup(channels: list, user_lang: str) -> InlineKeyboardMarkup:
    buttons = []
    for idx, ch in enumerate(channels, 1):
        clean_ch = ch.lstrip("@")
        url = f"https://t.me/{clean_ch}"
        buttons.append([InlineKeyboardButton(f"📢 {tr('btn_join_channel', user_lang)} {idx} ({ch})", url=url)])
    buttons.append([InlineKeyboardButton(tr("btn_joined_verify", user_lang), callback_data="verify_fsub")])
    return InlineKeyboardMarkup(buttons)

def create_short_token(url: str) -> str:
    import hashlib
    # Generate 10-char clean alphanumeric hash token (well within Telegram's 64-char deep link limit)
    token = hashlib.md5(url.encode()).hexdigest()[:12]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO shared_links (token, url) VALUES (?, ?)", (token, url))
        conn.commit()
    return token

def resolve_short_token(token: str) -> str:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT url FROM shared_links WHERE token = ?", (token,))
        row = cursor.fetchone()
        return row[0] if row else None

def get_chat_lang(chat_id: int, user_id: int = None, is_group: bool = False) -> str:
    # 1. In groups: check if group has custom lang set in settings table, otherwise default to "en"
    if is_group:
        grp_lang = get_setting(f"grp_lang_{chat_id}", "")
        if grp_lang in ["fa", "en"]:
            return grp_lang
        return "en"  # Group default is English

    # 2. In private: check user's saved lang, otherwise default to "en"
    if user_id:
        return get_user_lang(user_id)
    return "en"

def set_group_lang(chat_id: int, lang: str):
    set_setting(f"grp_lang_{chat_id}", lang)

def get_user_lang(user_id: int) -> str:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT lang FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row[0] if row and row[0] in ["fa", "en"] else "en"

def set_user_lang(user_id: int, lang: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO users (user_id, lang) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET lang = excluded.lang
        """, (user_id, lang))
        conn.commit()

MESSAGES = {
    "fa": {
        "choose_lang": (
            "🌐 <b>لطفاً زبان مورد نظر خود را انتخاب کنید:</b>\n"
            "<i>Please select your preferred language:</i>"
        ),
        "welcome": (
            "👋 <b>سلام به ربات دانلودر همه‌کاره خوش اومدی!</b>\n\n"
            "🔗 لینک ویدیوت، موزیک یا عکس از پلتفرم‌های زیر رو بفرست:\n"
            "• <b>اسپاتیفای (Spotify - موزیک، آلبوم، متن)</b>\n"
            "• <b>یوتیوب (YouTube - تا سقف ۲ گیگابایت)</b>\n"
            "• <b>تیک‌تاک (TikTok - بدون واترمارک)</b>\n"
            "• <b>اینستاگرام (Instagram - بهینه‌شده برای آیفون)</b>\n"
            "• <b>پینترست (Pinterest - عکس و ویدیو)</b>\n"
            "• <b>توییتر / ردیت / ساندکلاد</b>\n\n"
            "📖 برای مشاهده راهنمای جامع و ترفندها دستور /help را بزنید.\n"
            "⚙️ برای تغییر زبان می‌توانید از دستور /lang استفاده کنید."
        ),
        "help_title": "📖 <b>راهنمای جامع ربات دانلودر همه‌کاره:</b>",
        "help_text": (
            "📖 <b>راهنمای جامع ربات دانلودر همه‌کاره:</b>\n\n"
            "این ربات یک ابزار سریع و بدون تبلیغات برای دانلود انواع رسانه در تلگرام است.\n\n"
            "🌐 <b>پلتفرم‌های تحت پوشش:</b>\n"
            "• <b>Spotify & SoundCloud:</b> دانلود فایل صوتی ۳۲۰kbps با کاور اصلی + دکمه لیریکس (متن ترانه) + دانلود کامل آلبوم و پلی‌لیست.\n"
            "• <b>YouTube:</b> دانلود ویدیو تا کیفیت 1080p و فایل‌های حجیم تا سقف ۲ گیگابایت + استخراج فایل صوتی MP3.\n"
            "• <b>TikTok:</b> دانلود ویدیوها بدون هیچ واترمارک + دانلود کامل اسلایدرهای عکسی همراه با دکمه MP3 موزیک پس‌زمینه.\n"
            "• <b>Instagram:</b> دانلود مستقیم ریلزها و کلیپ‌ها (بهینه‌شده برای آیفون H.264) + دانلود پست‌های چند اسلایدی (Carousel) در قالب آلبوم رسمی تلگرام.\n"
            "• <b>Pinterest:</b> دانلود مستقیم ویدیوها و تصاویر در ابعاد و رزولوشن اصلی (Original Full-HD).\n"
            "• <b>Twitter / X & Reddit:</b> دانلود مستقیم کلیپ‌ها و تصاویر.\n\n"
            "⚡ <b>قابلیت‌های هوشمند و ویژه:</b>\n"
            "1️⃣ <b>استخراج لینک از متن طولانی:</b> نیازی به پاک کردن متن‌های اضافه یا کپشن‌های فورواردی نیست؛ ربات خودش لینک را از داخل پیام پیدا می‌کند.\n"
            "2️⃣ <b>منوی انتخابی چندلینکی:</b> اگر پیامی حاوی چند لینک مختلف باشد، ربات منوی شیشه‌ای دکمه‌دار می‌دهد و دکمه‌ها پاک نمی‌شوند تا بتوانید همه را یکی‌یکی دریافت کنید.\n"
            "3️⃣ <b>لغو سریع دانلود با دستور /stop:</b> اگر دانلودی طول کشید یا منصرف شدید، با ارسال /stop در پیوی یا گروه، دانلود خودتان فوراً متوقف می‌شود.\n"
            "4️⃣ <b>سیستم محافظت ضداسپم:</b> ارسال مکرر و رگباری لینک اخطار و مسدودسازی به دنبال دارد؛ لطفاً تا پایان پردازش هر دانلود صبور باشید.\n"
            "5️⃣ <b>صف منظم در گروه‌ها:</b> در گروه‌ها دانلودها نوبتی و منظم پردازش می‌شوند تا تداخلی ایجاد نشود.\n"
            "6️⃣ <b>استفاده اینلاین در همه چت‌ها:</b> در هر گروه یا چتی فقط آیدی ربات را تایپ کنید و لینک را بچسبانید (مثال: <code>@Resultscrackbot link</code>).\n"
            "7️⃣ <b>بدون پیام‌های موقت و اسپم:</b> در گروه‌ها و پیوی هیچ پیام لودینگ اضافه‌ای ارسال نمی‌شود و فقط فایل نهایی ارسال می‌گردد.\n\n"
            "⚙️ <b>دستورات کاربردی:</b>\n"
            "/start - استارت و انتخاب زبان اولیه\n"
            "/help - نمایش همین راهنما\n"
            "/stop - لغو و توقف دانلود جاری شما\n"
            "/lang - تغییر زبان ربات (فارسی / انگلیسی)\n"
            "/report - گزارش باگ، انتقاد یا ارتباط با پشتیبانی"
        ),
        "stop_success": "🛑 <b>عملیات دانلود جاری با موفقیت متوقف شد.</b>",
        "stop_no_task": "ℹ️ در حال حاضر هیچ دانلودی برای شما در حال انجام نیست.",
        "stop_not_owner": "⚠️ فقط کاربری که این دانلود را شروع کرده یا مدیران می‌توانند آن را متوقف کنند.",
        "already_processing": "⏳ <b>شما در حال حاضر یک دانلود در حال انجام دارید!</b>\nلطفاً تا پایان آن صبور باشید، یا برای لغو آن از دستور /stop استفاده کنید.",
        "group_queued": "⏳ <i>دانلود دیگری در گروه در حال انجام است؛ درخواست شما در صف قرار گرفت...</i>",
        "user_queued": "⏳ <i>دانلود دیگری برای شما در جریان است؛ درخواست جدید شما در صف قرار گرفت...</i>",
        "spam_warning": "⚠️ <b>هشدار اسپم!</b>\nدرخواست قبلی شما در حال پردازش است؛ لطفاً صبور باشید و پیام مکرر ارسال نکنید.",
        "duplicate_link": "🔁 <b>این لینک در حال حاضر در حال دانلود است!</b>\nلطفاً تا پایان دریافت آن صبور باشید و مجدداً ارسال نکنید.",
        "banned_alert": "🚫 <b>دسترسی شما به ربات مسدود شده است!</b>\n\nامکان دانلود یا ارسال دستور برای حساب شما وجود ندارد.",
        "banned_msg": "🚫 دسترسی شما به این ربات مسدود شده است.",
        "banned_btn_appeal": "📩 ارتباط با ادمین ربات و تجدیدنظر",
        "banned_appeal_prompt": "✍️ <b>ارسال پیام به مدیر ربات:</b>\n\nلطفاً پیام، توضیح یا درخواست رفع مسدودیت خود را در پیام بعدی همین‌جا ارسال کنید تا مستقیماً به ادمین تحویل داده شود.",
        "banned_appeal_sent": "✅ <b>پیام شما با موفقیت برای مدیر ربات ارسال شد.</b>\nبه محض بررسی، نتیجه به شما اطلاع داده خواهد شد.",
        "report_msg": (
            "🛠 <b>ارتباط با توسعه‌دهنده و گزارش مشکل:</b>\n\n"
            "در صورت مشاهده هرگونه باگ، قطعی در دانلود، پیشنهاد یا انتقاد، می‌توانید مستقیماً با برنامه‌نویس و سازنده ربات در ارتباط باشید:\n\n"
            "💻 <a href='https://t.me/EINDRAL'>Developer</a>\n\n"
            "💡 <i>لطفاً هنگام گزارش مشکل، لینک رسانه‌ای که با خطا مواجه شده است را نیز ارسال نمایید.</i>"
        ),
        "extracting": "🔎 <i>در حال استخراج اطلاعات رسانه...</i>",
        "err_extract": "❌ <b>خطا در دریافت اطلاعات:</b>\n<code>{err}</code>",
        "err_size_limit": "⚠️ <b>محدودیت حجم تلگرام:</b>\nحجم این ویدیو بیش از ۲ گیگابایت است. ربات‌ها طبق قوانین تلگرام امکان ارسال فایل‌های بالای ۲ گیگابایت را ندارند.",
        "err_ig_story": "🔒 <b>دانلود استوری اینستاگرام:</b>\nاستوری‌های اینستاگرام طبق قوانین متا نیاز به لاگین و نشست فعال دارند و بدون ورود به اکانت قابل دریافت نیستند. لطفاً لینک ریلز یا پست ارسال نمایید.",
        "btn_video": "🎬 ویدیو {h}p",
        "btn_video_best": "🎬 دانلود ویدیو (بهترین کیفیت)",
        "btn_audio": "🎵 استخراج صوت (MP3)",
        "btn_photo": "🖼 دانلود عکس با کیفیت اصلی",
        "btn_spotify": "🎵 دانلود موزیک (MP3 320kbps)",
        "btn_lyrics": "📜 متن آهنگ (Lyrics)",
        "btn_album": "📥 دانلود کل مجموعه ({count} آهنگ)",
        "choose_quality": "👇 <i>کیفیت مورد نظرت رو برای دانلود انتخاب کن:</i>",
        "choose_image": "👇 <i>برای دریافت تصویر در ابعاد کامل دکمه زیر را لمس کنید:</i>",
        "choose_spotify": "👇 <i>برای دریافت فایل صوتی یا متن دکمه‌های زیر را لمس کنید:</i>",
        "choose_album": "👇 <i>برای شروع دانلود خودکار آهنگ‌های این آلبوم دکمه زیر را بزنید:</i>",
        "status_downloading": "⏳ <i>در حال دانلود {choice}... لطفاً صبور باشید.</i>",
        "status_downloaded": "✅ <b>دانلود انجام شد:</b> {choice}",
        "album_progress": "⏳ <b>در حال دانلود آلبوم:</b> ترک {current} از {total}\n🎵 <i>{title}</i>",
        "album_done": "✅ <b>دانلود تمام آهنگ‌های آلبوم با موفقیت انجام شد!</b>",
        "album_cancelled": "🛑 <b>عملیات دانلود این آلبوم متوقف شد.</b>",
        "no_lyrics": "⚠️ متأسفانه متن این آهنگ یافت نشد.",
        "expired": "⚠️ اطلاعات این رسانه منقضی شده، لطفاً لینک را مجدداً ارسال کنید.",
        "lang_set": "✅ زبان ربات به <b>فارسی 🇮🇷</b> تنظیم شد.",
        "lbl_channel": "کانال/سازنده",
        "lbl_singer": "خواننده",
        "lbl_album": "آلبوم",
        "lbl_year": "سال انتشار",
        "lbl_duration": "مدت زمان",
        "lbl_platform": "پلتفرم",
        "lbl_source": "منبع",
        "lbl_tracks_count": "تعداد آهنگ‌ها",
        "force_join_msg": "🔒 <b>برای استفاده از ربات، لطفاً ابتدا در کانال‌های زیر عضو شوید:</b>\n\nپس از عضویت، دکمه <b>«تأیید عضویت ✅»</b> را لمس کنید.",
        "btn_join_channel": "📢 عضویت در کانال",
        "btn_joined_verify": "✅ عضو شدم / تأیید",
        "not_joined_alert": "❌ شما هنوز در کانال عضو نشده‌اید! لطفاً ابتدا عضو شوید.",
        "joined_success": "🎉 عضویت شما تأیید شد! اکنون می‌توانید لینک‌های خود را ارسال کنید.",
        "multi_links_found": "🔍 <b>تعداد {count} لینک مدیا در پیام شما یافت شد!</b>\n\n👇 <i>لطفاً روی هر کدام که می‌خواهید دانلود شود کلیک کنید:</i>\n💡 <i>(این منو بعد از کلیک پاک نمی‌شود تا بتوانید بقیه لینک‌ها را نیز دریافت کنید)</i>",
        "link_num": "لینک {idx}",
        "btn_dl_platform": "📥 {label} ({idx})",
    },
    "en": {
        "choose_lang": (
            "🌐 <b>Please select your preferred language:</b>\n"
            "<i>لطفاً زبان مورد نظر خود را انتخاب کنید:</i>"
        ),
        "welcome": (
            "👋 <b>Welcome to All-in-One Downloader Bot!</b>\n\n"
            "🔗 Send any video, music, or photo link from:\n"
            "• <b>Spotify (Tracks, Albums, Lyrics)</b>\n"
            "• <b>YouTube (Up to 2 GB via Pyrogram)</b>\n"
            "• <b>TikTok (No Watermark)</b>\n"
            "• <b>Instagram (iPhone/iOS Optimized)</b>\n"
            "• <b>Pinterest (Photos & Videos)</b>\n"
            "• <b>Twitter / X, Reddit, SoundCloud</b>\n\n"
            "📖 Send /help for comprehensive guide and tips.\n"
            "⚙️ Change language anytime with /lang."
        ),
        "help_title": "📖 <b>All-in-One Downloader User Guide:</b>",
        "help_text": (
            "📖 <b>All-in-One Downloader User Guide:</b>\n\n"
            "A fast, distraction-free bot for downloading media directly inside Telegram.\n\n"
            "🌐 <b>Supported Platforms:</b>\n"
            "• <b>Spotify & SoundCloud:</b> High quality 320kbps MP3s with cover art, synchronized lyrics, and full album/playlist downloads.\n"
            "• <b>YouTube:</b> Multiple video resolutions up to 1080p and large files up to 2 GB + MP3 extraction.\n"
            "• <b>TikTok:</b> Videos without watermark + complete photo slideshows with background music MP3 button.\n"
            "• <b>Instagram:</b> Reels & clips (H.264 iOS-friendly) + multi-photo Carousels delivered as native Telegram albums.\n"
            "• <b>Pinterest:</b> Direct video & original full-resolution image downloads.\n"
            "• <b>Twitter / X & Reddit:</b> Direct video and image downloads.\n\n"
            "⚡ <b>Smart Features:</b>\n"
            "1️⃣ <b>Smart Link Extraction:</b> Forward any long post or caption; the bot automatically extracts the media link.\n"
            "2️⃣ <b>Multi-Link Selector:</b> If a message contains multiple links, interactive persistent buttons let you download each item individually.\n"
            "3️⃣ <b>Cancel Anytime with /stop:</b> If a download is too slow or you change your mind, send /stop to immediately cancel your own active task.\n"
            "4️⃣ <b>Anti-Spam & Fair Use:</b> Sending repetitive spam requests triggers automated warnings and temporary/permanent bans.\n"
            "5️⃣ <b>Organized Group Queue:</b> In group chats, downloads are processed sequentially to keep the chat organized.\n"
            "6️⃣ <b>Inline Mode Anywhere:</b> Type <code>@Resultscrackbot link</code> in any chat to search or share media instantly.\n"
            "7️⃣ <b>Clean & Silent:</b> Zero spam or temporary loading messages in groups and PM.\n\n"
            "⚙️ <b>Commands:</b>\n"
            "/start - Start bot & select language\n"
            "/help - View this guide\n"
            "/stop - Cancel and stop your current active download\n"
            "/lang - Switch language (English / Persian)\n"
            "/report - Report issues, bugs or contact support"
        ),
        "stop_success": "🛑 <b>Active download process was cancelled successfully.</b>",
        "stop_no_task": "ℹ️ You do not have any active download in progress.",
        "stop_not_owner": "⚠️ Only the user who started this download or group admins can cancel it.",
        "already_processing": "⏳ <b>You already have an active download in progress!</b>\nPlease wait for it to finish or cancel it with /stop.",
        "group_queued": "⏳ <i>Another download is in progress in this group; your request is queued...</i>",
        "user_queued": "⏳ <i>Another download is in progress for you; your request is queued...</i>",
        "spam_warning": "⚠️ <b>Spam Warning!</b>\nYour previous request is still processing; please wait and do not spam.",
        "duplicate_link": "🔁 <b>This link is currently being downloaded!</b>\nPlease wait until the download finishes instead of resending it.",
        "banned_alert": "🚫 <b>Your access to this bot has been restricted!</b>\n\nDownloading and bot commands are currently disabled for your account.",
        "banned_msg": "🚫 You have been banned from using this bot.",
        "banned_btn_appeal": "📩 Contact Admin & Appeal",
        "banned_appeal_prompt": "✍️ <b>Message Bot Administrator:</b>\n\nPlease type and send your appeal or message below; it will be delivered directly to the bot administrator.",
        "banned_appeal_sent": "✅ <b>Your message has been delivered to the bot administrator.</b>\nYou will be notified once it is reviewed.",
        "report_msg": (
            "🛠 <b>Contact Developer & Issue Reporting:</b>\n\n"
            "If you encounter any bugs, failed downloads, or have suggestions, feel free to contact the developer directly:\n\n"
            "💻 <a href='https://t.me/EINDRAL'>Developer</a>\n\n"
            "💡 <i>Please include the problematic media link when reporting an issue.</i>"
        ),
        "extracting": "🔎 <i>Extracting media information...</i>",
        "err_extract": "❌ <b>Error retrieving information:</b>\n<code>{err}</code>",
        "err_size_limit": "⚠️ <b>Telegram File Size Limit:</b>\nThis video exceeds 2 GB. Bots cannot upload files larger than 2 GB due to Telegram Bot API restrictions.",
        "err_ig_story": "🔒 <b>Instagram Story Download:</b>\nInstagram stories strictly require an active authenticated user session/login. Stories cannot be accessed anonymously. Please send Reels or Posts instead.",
        "btn_video": "🎬 Video {h}p",
        "btn_video_best": "🎬 Download Video (Best Quality)",
        "btn_audio": "🎵 Extract Audio (MP3)",
        "btn_photo": "🖼 Download Image (Original Quality)",
        "btn_spotify": "🎵 Download Track (MP3 320kbps)",
        "btn_lyrics": "📜 Song Lyrics",
        "btn_album": "📥 Download All Tracks ({count} songs)",
        "choose_quality": "👇 <i>Choose your desired download quality:</i>",
        "choose_image": "👇 <i>Tap the button below to get the full-resolution image:</i>",
        "choose_spotify": "👇 <i>Tap the buttons below to download audio or lyrics:</i>",
        "choose_album": "👇 <i>Tap below to download all tracks in this album:</i>",
        "status_downloading": "⏳ <i>Downloading {choice}... Please wait.</i>",
        "status_downloaded": "✅ <b>Downloaded:</b> {choice}",
        "album_progress": "⏳ <b>Downloading Album:</b> Track {current} of {total}\n🎵 <i>{title}</i>",
        "album_done": "✅ <b>All album tracks downloaded and sent successfully!</b>",
        "album_cancelled": "🛑 <b>Album download was cancelled.</b>",
        "no_lyrics": "⚠️ Sorry, lyrics could not be found for this track.",
        "expired": "⚠️ Media info expired. Please send the link again.",
        "lang_set": "✅ Language has been set to <b>English 🇬🇧</b>.",
        "lbl_channel": "Channel/Creator",
        "lbl_singer": "Artist",
        "lbl_album": "Album",
        "lbl_year": "Release Year",
        "lbl_duration": "Duration",
        "lbl_platform": "Platform",
        "lbl_source": "Source",
        "lbl_tracks_count": "Total Tracks",
        "force_join_msg": "🔒 <b>Please join our channel first to use this bot:</b>\n\nAfter joining, click <b>«I have joined ✅»</b>.",
        "btn_join_channel": "📢 Join Channel",
        "btn_joined_verify": "✅ I have joined",
        "not_joined_alert": "❌ You have not joined the channel yet! Please join first.",
        "joined_success": "🎉 Membership verified! You can now send media links.",
        "multi_links_found": "🔍 <b>Found {count} media links in your message!</b>\n\n👇 <i>Please click on whichever item you wish to download:</i>\n💡 <i>(This menu stays active so you can download multiple items)</i>",
        "link_num": "Link {idx}",
        "btn_dl_platform": "📥 {label} ({idx})",
    }
}

def tr(key: str, lang: str, **kwargs) -> str:
    template = MESSAGES.get(lang, MESSAGES["fa"]).get(key, "")
    return template.format(**kwargs) if kwargs else template

def clean_alert_text(text: str) -> str:
    """Strips HTML tags like <b>, </b>, <i>, etc. for plain-text Telegram callback alert popups."""
    return re.sub(r'<[^>]+>', '', text)

def clean_thumbnail_url(thumb: str) -> str:
    """Ensures YouTube and other platform thumbnails use Telegram-supported formats (JPG instead of WebP)."""
    if not thumb:
        return ""
    # YouTube WebP URLs fail in Telegram send_photo (WEBPAGE_MEDIA_EMPTY)
    # Convert https://i.ytimg.com/vi_webp/<id>/maxresdefault.webp -> https://i.ytimg.com/vi/<id>/hqdefault.jpg
    if "i.ytimg.com/vi_webp/" in thumb:
        thumb = thumb.replace("/vi_webp/", "/vi/").replace(".webp", ".jpg")
    elif ".webp" in thumb and "ytimg.com" in thumb:
        thumb = thumb.replace(".webp", ".jpg")
    return thumb

def format_size(bytes_val):
    if not bytes_val:
        return "نامشخص / Unknown"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_val < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} TB"

def format_filesize(bytes_val):
    if not bytes_val or bytes_val <= 0:
        return ""
    mb = bytes_val / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb:.0f} MB"

def format_duration(seconds):
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

SUPPORTED_DOMAINS = [
    "youtube.com", "youtu.be",
    "tiktok.com", "douyin.com",
    "instagram.com", "ddinstagram.com",
    "pinterest.com", "pin.it",
    "spotify.com", "spotify.link",
    "twitter.com", "x.com", "fxtwitter.com", "vxtwitter.com",
    "reddit.com", "redd.it",
    "soundcloud.com"
]

def is_supported_url(url: str) -> bool:
    clean_url = url.lower()
    return any(domain in clean_url for domain in SUPPORTED_DOMAINS)

def is_tiktok(url: str) -> bool:
    return any(domain in url.lower() for domain in ["tiktok.com", "douyin.com"])

def is_pinterest(url: str) -> bool:
    return any(domain in url.lower() for domain in ["pinterest.com", "pin.it"])

def is_spotify_track(url: str) -> bool:
    return "spotify.com/track/" in url.lower() or "spotify.link" in url.lower()

def is_spotify_album(url: str) -> bool:
    return "spotify.com/album/" in url.lower() or "spotify.com/playlist/" in url.lower()

def is_youtube(url: str) -> bool:
    return any(domain in url.lower() for domain in ["youtube.com", "youtu.be"])

def is_soundcloud(url: str) -> bool:
    return "soundcloud.com" in url.lower() or "on.soundcloud.com" in url.lower()

def is_soundcloud_album(url: str) -> bool:
    u = url.lower()
    return is_soundcloud(u) and ("/sets/" in u)

def is_instagram(url: str) -> bool:
    return any(domain in url.lower() for domain in ["instagram.com", "ddinstagram.com"])

def is_instagram_story(url: str) -> bool:
    return "instagram.com/stories/" in url.lower()

def is_twitter(url: str) -> bool:
    return any(d in url.lower() for d in ["twitter.com", "x.com", "fxtwitter.com", "vxtwitter.com"])

def is_direct_clip(url: str) -> bool:
    return any(domain in url.lower() for domain in [
        "instagram.com", "ddinstagram.com",
        "tiktok.com", "douyin.com",
        "twitter.com", "x.com", "fxtwitter.com", "vxtwitter.com",
        "reddit.com", "redd.it"
    ])

def get_platform_label(url: str) -> str:
    u = url.lower()
    if is_spotify_track(u) or is_spotify_album(u):
        return "Spotify 🎵"
    if is_youtube(u):
        return "YouTube 🎬"
    if "instagram.com" in u or "ddinstagram.com" in u:
        return "Instagram 📸"
    if is_tiktok(u):
        return "TikTok 📱"
    if is_pinterest(u):
        return "Pinterest 📌"
    if "twitter.com" in u or "x.com" in u:
        return "Twitter / X 🐦"
    if is_soundcloud(u):
        return "SoundCloud ☁️"
    if "reddit.com" in u or "redd.it" in u:
        return "Reddit 🤖"
    return "Media Link 🔗"

def extract_all_supported_urls(text: str) -> list:
    if not text:
        return []
    # Find all standard URLs
    found = re.findall(r'https?://[^\s<>"]+', text)
    valid = []
    seen = set()
    for u in found:
        u_clean = u.rstrip('.,;!?)\'"`')
        if u_clean not in seen and is_supported_url(u_clean):
            seen.add(u_clean)
            valid.append(u_clean)
    return valid

# ----------------- Extractors -----------------

async def extract_tiktok(url: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        target_url = url
        try:
            async with session.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=10)) as r:
                target_url = str(r.url)
        except Exception as e:
            logger.warning(f"Failed to unshorten TikTok URL {url}: {e}")

        api_url = f"https://www.tikwm.com/api/?url={target_url}"
        async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()
            if data.get("code") != 0 or not data.get("data"):
                if target_url != url:
                    api_url2 = f"https://www.tikwm.com/api/?url={url}"
                    async with session.get(api_url2, timeout=aiohttp.ClientTimeout(total=10)) as resp2:
                        data = await resp2.json()

            if data.get("code") != 0 or not data.get("data"):
                raise ValueError(data.get("msg") or "خطا در استخراج ویدیوی تیک‌تاک")

            d = data["data"]
            images = d.get("images", [])
            is_slideshow = bool(images and len(images) > 0)
            media_list = [{"url": img, "is_video": False} for img in images] if is_slideshow else []

            return {
                "source": "tiktok",
                "media_type": "carousel" if is_slideshow else "video",
                "id": str(d.get("id") or int(time.time())),
                "title": d.get("title") or "TikTok",
                "uploader": d.get("author", {}).get("nickname") or d.get("author", {}).get("unique_id") or "TikTok Creator",
                "duration": d.get("duration", 0),
                "thumbnail": images[0] if is_slideshow else d.get("cover"),
                "play_url": d.get("play"),
                "music_url": d.get("music"),
                "images": images,
                "media_list": media_list
            }

async def extract_pinterest_image(url: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        target_url = url
        try:
            async with session.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=10)) as r:
                target_url = str(r.url)
                html_text = await r.text()
        except Exception as e:
            raise ValueError(f"Pinterest error: {e}")

        img_m = re.search(r'name=\"og:image\" property=\"og:image\"[^>]*content=\"([^\"]+)\"', html_text)
        if not img_m:
            img_m = re.search(r'content=\"([^\"]+)\"[^>]*name=\"og:image\"', html_text)
        
        img_url = img_m.group(1) if img_m else None
        
        title_m = re.search(r'name=\"og:title\"[^>]*content=\"([^\"]+)\"', html_text)
        if not title_m:
            title_m = re.search(r'content=\"([^\"]+)\"[^>]*name=\"og:title\"', html_text)
        raw_title = title_m.group(1) if title_m else 'Pinterest Image'

        if not img_url:
            raise ValueError("تصویری در این لینک پینترست یافت نشد.")

        orig_url = re.sub(r'/(?:[0-9]+x|236x|474x|736x)/', '/originals/', img_url)
        
        return {
            "source": "pinterest",
            "media_type": "photo",
            "id": str(int(time.time())),
            "title": raw_title,
            "uploader": "Pinterest",
            "duration": 0,
            "thumbnail": orig_url,
            "image_url": orig_url,
            "fallback_image_url": img_url
        }

async def extract_spotify_track(url: str):
    headers = {
        "User-Agent": "TelegramBot (like TwitterBot)"
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            html_text = await r.text()

        title_m = re.search(r'property=\"og:title\" content=\"([^\"]+)\"', html_text)
        desc_m = re.search(r'property=\"og:description\" content=\"([^\"]+)\"', html_text)
        img_m = re.search(r'property=\"og:image\" content=\"([^\"]+)\"', html_text)

        title = html.unescape(title_m.group(1)) if title_m else 'Spotify Track'
        desc = html.unescape(desc_m.group(1)) if desc_m else ''
        cover_url = img_m.group(1) if img_m else None

        parts = [p.strip() for p in desc.split('·')] if desc else []
        artist = parts[0] if len(parts) > 0 else 'Unknown Artist'
        album = parts[1] if len(parts) > 1 else ''
        year = parts[3] if len(parts) > 3 else (parts[2] if len(parts) > 2 else '')

        search_query = f"{artist} - {title} audio"

        return {
            "source": "spotify_track",
            "media_type": "audio",
            "id": str(int(time.time())),
            "title": title,
            "artist": artist,
            "album": album,
            "year": year,
            "uploader": artist,
            "duration": 0,
            "thumbnail": cover_url,
            "cover_url": cover_url,
            "search_query": search_query
        }

async def extract_spotify_album(url: str):
    headers_bot = {"User-Agent": "TelegramBot (like TwitterBot)"}
    headers_web = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    async with aiohttp.ClientSession() as session:
        # 1. Fetch metadata (Title, artist, cover) via bot UA
        async with session.get(url, headers=headers_bot, timeout=aiohttp.ClientTimeout(total=10)) as r:
            meta_html = await r.text()

        title_m = re.search(r'property=\"og:title\" content=\"([^\"]+)\"', meta_html)
        desc_m = re.search(r'property=\"og:description\" content=\"([^\"]+)\"', meta_html)
        img_m = re.search(r'property=\"og:image\" content=\"([^\"]+)\"', meta_html)

        raw_title = html.unescape(title_m.group(1)) if title_m else 'Spotify Collection'
        clean_name = raw_title.split(" - Album by")[0].split(" | Spotify")[0].strip()
        cover_url = img_m.group(1) if img_m else None
        desc = html.unescape(desc_m.group(1)) if desc_m else ''

        parts = [p.strip() for p in desc.split('·')] if desc else []
        artist = parts[0] if len(parts) > 0 else 'Spotify'
        if "playlist" in desc.lower():
            artist = parts[1] if len(parts) > 1 else 'Spotify'
            year = ""
        else:
            year = parts[2] if len(parts) > 2 else ''

        # 2. Fetch full track list via web UA
        async with session.get(url, headers=headers_web, timeout=aiohttp.ClientTimeout(total=10)) as r2:
            page_html = await r2.text()

        track_ids = re.findall(r'/track/([a-zA-Z0-9]{22})', page_html)
        seen = set()
        ordered_ids = [t for t in track_ids if not (t in seen or seen.add(t))]

        if not ordered_ids:
            # Fallback regex for newer Spotify web client layout
            track_ids_alt = re.findall(r'open\.spotify\.com(?:/intl-[a-z]{2})?/track/([a-zA-Z0-9]{22})', page_html)
            ordered_ids = [t for t in track_ids_alt if not (t in seen or seen.add(t))]

        if not ordered_ids:
            raise ValueError("No tracks found in Spotify album/playlist")

        is_pl = "playlist" in url.lower()
        return {
            "source": "spotify_album",
            "media_type": "album",
            "is_playlist": is_pl,
            "id": str(int(time.time())),
            "title": clean_name,
            "artist": artist,
            "year": year,
            "uploader": artist,
            "thumbnail": cover_url,
            "cover_url": cover_url,
            "track_ids": ordered_ids,
            "tracks_count": len(ordered_ids)
        }

async def extract_soundcloud_album(url: str):
    # Native fast SoundCloud album/playlist scraper with automatic DRM filter
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=12)) as r:
                page = await r.text()

        m = re.findall(r'<script>window\.__sc_hydration\s*=\s*(\[.*?\]);</script>', page)
        if m:
            data = json.loads(m[0])
            pl_data = None
            for item in data:
                if item.get('hydratable') == 'playlist':
                    pl_data = item.get('data', {})
                    break

            if pl_data:
                title = pl_data.get('title') or 'SoundCloud Playlist'
                uploader = pl_data.get('user', {}).get('username') or 'SoundCloud'
                raw_tracks = pl_data.get('tracks', [])
                artwork = pl_data.get('artwork_url')
                if artwork:
                    artwork = artwork.replace('-large.', '-original.')

                # Extract client_id dynamically from SoundCloud Web scripts
                scripts = re.findall(r'src=\"(https://[^\"]+assets[^\"]+\.js)\"', page)
                client_id = None
                async with aiohttp.ClientSession(headers=headers) as s:
                    for s_url in scripts:
                        try:
                            async with s.get(s_url, timeout=aiohttp.ClientTimeout(total=5)) as sr:
                                js = await sr.text()
                                mc = re.search(r'client_id[:=][\"|\']([a-zA-Z0-9]{32})[\"|\']', js)
                                if mc:
                                    client_id = mc.group(1)
                                    break
                        except Exception:
                            pass

                missing_ids = [str(t['id']) for t in raw_tracks if not t.get('title')]
                populated = [t for t in raw_tracks if t.get('title')]

                if client_id and missing_ids:
                    batch_size = 50
                    async with aiohttp.ClientSession(headers=headers) as s:
                        for i in range(0, len(missing_ids), batch_size):
                            b_ids = missing_ids[i:i + batch_size]
                            api_url = f"https://api-v2.soundcloud.com/tracks?ids=" + "%2C".join(b_ids) + f"&client_id={client_id}"
                            try:
                                async with s.get(api_url, timeout=aiohttp.ClientTimeout(total=8)) as ar:
                                    if ar.status == 200:
                                        b_data = await ar.json()
                                        populated.extend(b_data)
                            except Exception as e:
                                logger.debug(f"SoundCloud track batch fetch error: {e}")

                # Filter out DRM-protected or unstreamable tracks cleanly!
                valid_tracks = []
                for t in populated:
                    if t.get('policy') == 'BLOCK':
                        continue
                    media = t.get('media', {})
                    transcodings = media.get('transcodings', [])
                    # Skip tracks that only offer DRM encrypted protocols (cbc-encrypted-hls / ctr-encrypted-hls)
                    playable = any('encrypted' not in tc.get('format', {}).get('protocol', '') for tc in transcodings)
                    if playable or (not transcodings and t.get('streamable')):
                        thumb = (t.get('artwork_url') or '').replace('-large.', '-original.')
                        valid_tracks.append({
                            'title': t.get('title') or 'Audio',
                            'artist': t.get('user', {}).get('username') or uploader,
                            'duration': int((t.get('duration') or 0) / 1000),
                            'url': t.get('permalink_url'),
                            'thumbnail': thumb or artwork
                        })

                if not artwork and valid_tracks and valid_tracks[0].get('thumbnail'):
                    artwork = valid_tracks[0]['thumbnail']

                if valid_tracks:
                    return {
                        'source': 'soundcloud_album',
                        'media_type': 'album',
                        'is_playlist': True,
                        'id': str(pl_data.get('id') or int(time.time())),
                        'title': title,
                        'artist': uploader,
                        'uploader': uploader,
                        'year': str(pl_data.get('release_year') or ''),
                        'thumbnail': artwork,
                        'cover_url': artwork,
                        'tracks': valid_tracks,
                        'tracks_count': len(valid_tracks)
                    }
    except Exception as e:
        logger.warning(f"Native SoundCloud scraper failed: {e}, falling back to yt-dlp...")

    # Fallback to yt-dlp with ignoreerrors if native parser cannot run
    loop = asyncio.get_running_loop()
    def _extract():
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'ignoreerrors': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            res = ydl.extract_info(url, download=False)
            entries = [e for e in list(res.get('entries', [])) if e is not None]
            tracks = []
            for e in entries:
                tracks.append({
                    'title': e.get('title') or 'Audio',
                    'artist': e.get('uploader') or res.get('uploader') or 'SoundCloud',
                    'duration': int(e.get('duration') or 0),
                    'url': e.get('webpage_url') or e.get('url'),
                    'thumbnail': e.get('thumbnail') or res.get('thumbnail')
                })
            
            best_thumb = res.get('thumbnail')
            if not best_thumb and res.get('thumbnails'):
                best_thumb = res['thumbnails'][-1].get('url')
            if not best_thumb and tracks and tracks[0].get('thumbnail'):
                best_thumb = tracks[0]['thumbnail']

            return {
                'source': 'soundcloud_album',
                'media_type': 'album',
                'is_playlist': True,
                'id': str(res.get('id') or int(time.time())),
                'title': res.get('title') or 'SoundCloud Set',
                'artist': res.get('uploader') or 'SoundCloud',
                'uploader': res.get('uploader') or 'SoundCloud',
                'year': str(res.get('release_year') or ''),
                'thumbnail': best_thumb,
                'cover_url': best_thumb,
                'tracks': tracks,
                'tracks_count': len(tracks)
            }
    return await loop.run_in_executor(None, _extract)

async def extract_instagram_photo_fallback(url: str):
    # High-performance multi-photo / carousel / image extractor for Instagram
    shortcode_match = re.search(r'instagram\.com/(?:p|reel)/([A-Za-z0-9_-]+)', url)
    if not shortcode_match:
        raise ValueError("Invalid Instagram URL")
    shortcode = shortcode_match.group(1)

    loop = asyncio.get_running_loop()

    def _extract_via_instaloader():
        L = instaloader.Instaloader(quiet=True, download_comments=False, save_metadata=False)
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        media_urls = []
        is_sidecar = (post.typename == 'GraphSidecar')
        if is_sidecar:
            for node in post.get_sidecar_nodes():
                media_urls.append({
                    "url": node.video_url if node.is_video else node.display_url,
                    "is_video": node.is_video
                })
        else:
            media_urls.append({
                "url": post.video_url if post.is_video else post.url,
                "is_video": post.is_video
            })
        
        # Check if any audio is attached
        music_url = None
        for n in post.get_sidecar_nodes() if is_sidecar else [post]:
            if n.is_video and n.video_url:
                music_url = n.video_url
                break

        caption = post.caption or f"Post by {post.owner_username}"
        return {
            "source": "instagram_carousel" if is_sidecar else "instagram_photo",
            "media_type": "carousel" if is_sidecar else ("video" if post.is_video else "photo"),
            "id": shortcode,
            "title": caption[:100],
            "uploader": post.owner_username or "Instagram",
            "duration": 0,
            "thumbnail": media_urls[0]["url"] if media_urls else None,
            "image_url": media_urls[0]["url"] if media_urls else None,
            "media_list": media_urls,
            "music_url": music_url,
            "formats": []
        }

    try:
        return await loop.run_in_executor(None, _extract_via_instaloader)
    except Exception as e:
        logger.warning(f"Instaloader failed for {shortcode}: {e}, trying HTML fallback...")

    # Fallback to HTML meta scraper if instaloader encounters any block
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    clean_ig_url = f"https://www.instagram.com/p/{shortcode}/"
    async with aiohttp.ClientSession(headers=headers) as session:
        page = ""
        try:
            async with session.get(clean_ig_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                page = await resp.text()
        except Exception:
            pass

        og_match = re.search(r'<meta\s+property=[\"\']og:image[\"\']\s+content=[\"\']([^\"\']+)[\"\']', page)
        m_img_url = html.unescape(og_match.group(1)) if og_match else None

        if not m_img_url:
            raise ValueError("No media found in Instagram post")

        m_title = re.search(r'<meta\s+property=[\"\']og:title[\"\']\s+content=[\"\']([^\"\']+)[\"\']', page)
        raw_title = html.unescape(m_title.group(1)) if m_title else "Instagram Post"

        m_desc = re.search(r'<meta\s+property=[\"\']og:description[\"\']\s+content=[\"\']([^\"\']+)[\"\']', page)
        uploader = "Instagram"
        if m_desc:
            desc_text = html.unescape(m_desc.group(1))
            u_match = re.search(r'-\s*([A-Za-z0-9_.]+)\s+on', desc_text)
            if u_match:
                uploader = u_match.group(1)

        return {
            "source": "instagram_photo",
            "media_type": "photo",
            "id": shortcode,
            "title": raw_title,
            "uploader": uploader,
            "duration": 0,
            "thumbnail": m_img_url,
            "image_url": m_img_url,
            "media_list": [{"url": m_img_url, "is_video": False}],
            "formats": []
        }

async def resolve_redirect_url(url: str) -> str:
    # Resolve shortened / redirect links like spotify.link, bit.ly, etc.
    if any(sh in url.lower() for sh in ["spotify.link", "pin.it", "vt.tiktok.com", "vm.tiktok.com", "on.soundcloud.com"]):
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            async with aiohttp.ClientSession(headers=headers) as s:
                async with s.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    return str(r.url)
        except Exception:
            pass
    return url

async def extract_info(url: str):
    url = await resolve_redirect_url(url)
    if is_instagram_story(url):
        raise ValueError("IG_STORY_LOGIN_REQUIRED")

    if is_tiktok(url):
        return await extract_tiktok(url)

    if is_spotify_album(url):
        return await extract_spotify_album(url)

    if is_spotify_track(url):
        return await extract_spotify_track(url)

    if is_soundcloud_album(url):
        return await extract_soundcloud_album(url)

    if is_pinterest(url):
        try:
            return await _extract_ytdlp(url)
        except Exception as e:
            err_str = str(e).lower()
            if "no video formats found" in err_str or "unsupported" in err_str or "error" in err_str:
                return await extract_pinterest_image(url)
            raise e

    if is_instagram(url):
        try:
            return await _extract_ytdlp(url)
        except Exception as e:
            err_str = str(e).lower()
            if "no video formats found" in err_str or "no video in this post" in err_str:
                return await extract_instagram_photo_fallback(url)
            raise e

    if is_twitter(url):
        try:
            return await _extract_ytdlp(url)
        except Exception as e:
            err_str = str(e).lower()
            if "no video could be found" in err_str or "no video formats found" in err_str or "not a video" in err_str:
                return await extract_twitter_fallback(url)
            raise e

    return await _extract_ytdlp(url)

async def extract_twitter_fallback(url: str):
    # Parse tweet status ID and query VxTwitter API for media
    m = re.search(r'status/(\d+)', url)
    if not m:
        raise ValueError("Invalid Twitter/X URL")
    tweet_id = m.group(1)
    api_url = f"https://api.vxtwitter.com/twitter/status/{tweet_id}"
    
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(api_url) as resp:
            if resp.status != 200:
                raise ValueError(f"Twitter API returned {resp.status}")
            data = await resp.json()

    text = data.get("text") or "Twitter / X Post"
    media_urls = data.get("mediaURLs") or []
    media_ext = data.get("media_extended") or []

    if not media_urls and not media_ext:
        raise ValueError("No media found in tweet")

    # If it has images
    photos = []
    video_url = None
    for item in media_ext:
        m_type = item.get("type")
        u = item.get("url")
        if m_type == "video" or m_type == "gif":
            video_url = u
            break
        elif m_type == "image":
            photos.append(u)

    if video_url:
        return {
            'id': tweet_id,
            'title': text,
            'url': video_url,
            'duration': 0,
            'uploader': data.get("user_name", "Twitter User"),
            'type': 'video',
            'extractor': 'twitter'
        }

    if photos:
        if len(photos) == 1:
            return {
                'id': tweet_id,
                'title': text,
                'url': photos[0],
                'duration': 0,
                'uploader': data.get("user_name", "Twitter User"),
                'media_type': 'photo',
                'thumbnail': photos[0],
                'photo_urls': photos,
                'extractor': 'twitter'
            }
        else:
            return {
                'id': tweet_id,
                'title': text,
                'url': photos[0],
                'duration': 0,
                'uploader': data.get("user_name", "Twitter User"),
                'media_type': 'carousel',
                'thumbnail': photos[0],
                'media_list': [{'url': u, 'is_video': False} for u in photos],
                'extractor': 'twitter'
            }

    raise ValueError("No downloadable media found in tweet")

async def _extract_ytdlp(url: str):
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'noplaylist': True,
    }
    if COOKIES_FILE.exists():
        ydl_opts['cookiefile'] = str(COOKIES_FILE)
    if NODE_BIN.exists():
        ydl_opts['js_runtimes'] = {'node': {'path': str(NODE_BIN)}}

    loop = asyncio.get_running_loop()
    def _extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            res = ydl.extract_info(url, download=False)
            res["source"] = "ytdlp"
            res["media_type"] = "video"
            return res
    return await loop.run_in_executor(None, _extract)

# ----------------- Downloaders -----------------

async def reply_autodel(target_msg, text: str, delay: int = 30, parse_mode=enums.ParseMode.HTML):
    """Sends a temporary notice message and automatically deletes it after `delay` seconds."""
    try:
        sent = await target_msg.reply(text, parse_mode=parse_mode)
        async def _del_task():
            await asyncio.sleep(delay)
            try:
                await sent.delete()
            except Exception:
                pass
        asyncio.create_task(_del_task())
        return sent
    except Exception:
        return None

def make_progress_bar(percent: float, length: int = 10) -> str:
    # Generates a sleek, animated progress bar: ▰▰▰▰▰▱▱▱▱▱ 50%
    percent = max(0.0, min(100.0, percent))
    filled_len = int(round(length * percent / 100))
    bar = '▰' * filled_len + '▱' * (length - filled_len)
    return f"{bar} {percent:.1f}%"

def generate_video_thumbnail(video_path: str, thumb_path: str):
    # Generates a clear, crisp JPG thumbnail frame from 1-second mark using ffmpeg
    try:
        cmd = [
            "ffmpeg", "-y",
            "-ss", "00:00:01",
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "2",
            thumb_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
        # Fallback to frame at 00:00:00 if video < 1s
        cmd[2] = "00:00:00"
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    except Exception as e:
        logger.debug(f"Failed to generate video thumbnail: {e}")
    return None

def get_media_metadata(file_path: str):
    # Extract real width, height, duration, and whether audio stream exists via ffprobe
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            file_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        lines = res.stdout.strip().split("\n")
        w = int(lines[0]) if len(lines) > 0 and lines[0].isdigit() else None
        h = int(lines[1]) if len(lines) > 1 and lines[1].isdigit() else None
        dur = int(float(lines[2])) if len(lines) > 2 and lines[2].replace('.', '', 1).isdigit() else None

        # Check audio stream presence (silent GIFs, muted clips have NO audio stream)
        cmd_a = [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1:nokey=1",
            file_path
        ]
        res_a = subprocess.run(cmd_a, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        has_audio = bool(res_a.stdout.strip())

        return w, h, dur, has_audio
    except Exception as e:
        logger.debug(f"ffprobe metadata extraction error: {e}")
        return None, None, None, True

async def download_file_url(url: str, dest_path: Path):
    # TikTok and CDN servers require specific mobile / app User-Agents to prevent 403 Forbidden
    if "tiktok" in url.lower() or "byteoversea" in url.lower() or "ibytedtos" in url.lower():
        ua = "com.zhiliaoapp.musically/2022600030 (Linux; U; Android 7.1.2; es_ES; SM-G988N; Build/NRD90M;tt-ok/3.12.13.1)"
    else:
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

    headers = {"User-Agent": ua}
    # Preserve exact encoded query params (like %2F in TikTok HMAC signatures) without yarl unquoting
    target_url = yarl.URL(url, encoded=True)
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(target_url) as resp:
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                while True:
                    chunk = await resp.content.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)

async def download_spotify_track(info: dict, progress_callback=None, cancel_token=None):
    if cancel_token and cancel_token.is_set():
        raise asyncio.CancelledError("Cancelled before download")

    timestamp = int(time.time() * 1000)
    search_query = info.get("search_query") or f"{info.get('artist')} - {info.get('title')} audio"
    out_template = str(DOWNLOAD_DIR / f"{timestamp}_%(id)s.%(ext)s")

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': out_template,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }],
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
    }
    if COOKIES_FILE.exists():
        ydl_opts['cookiefile'] = str(COOKIES_FILE)
    if NODE_BIN.exists():
        ydl_opts['js_runtimes'] = {'node': {'path': str(NODE_BIN)}}

    loop = asyncio.get_running_loop()
    last_update_time = 0

    def _spotify_hook(d):
        nonlocal last_update_time
        if cancel_token and cancel_token.is_set():
            raise yt_dlp.utils.DownloadCancelled("Download cancelled via /stop")
        if not progress_callback:
            return
        if d.get('status') == 'downloading':
            now = time.time()
            if now - last_update_time >= 1.0:
                last_update_time = now
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                downloaded = d.get('downloaded_bytes') or 0
                if total > 0:
                    pct = min(100.0, (downloaded / total) * 100.0)
                    bar_str = make_progress_bar(pct)
                    asyncio.run_coroutine_threadsafe(progress_callback(bar_str, pct), loop)
        elif d.get('status') == 'finished':
            bar_str = make_progress_bar(100.0)
            asyncio.run_coroutine_threadsafe(progress_callback(bar_str, 100.0), loop)

    if progress_callback or cancel_token:
        ydl_opts['progress_hooks'] = [_spotify_hook]

    def _download():
        if cancel_token and cancel_token.is_set():
            raise yt_dlp.utils.DownloadCancelled("Download cancelled via /stop")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            res = ydl.extract_info(f"ytsearch1:{search_query}", download=True)
            entry = res['entries'][0]
            filename = ydl.prepare_filename(entry)
            base = os.path.splitext(filename)[0]
            mp3_file = base + ".mp3"
            duration = entry.get("duration", 0)
            return mp3_file, duration

    try:
        mp3_path, track_duration = await loop.run_in_executor(None, _download)
    except yt_dlp.utils.DownloadCancelled:
        raise asyncio.CancelledError("Download aborted via /stop")

    cover_url = info.get("cover_url")
    cover_temp = DOWNLOAD_DIR / f"{timestamp}_cover.jpg"
    has_cover = False

    if cover_url and os.path.exists(mp3_path):
        try:
            await download_file_url(cover_url, cover_temp)
            if cover_temp.exists():
                has_cover = True
                audio = ID3(mp3_path)
                with open(cover_temp, 'rb') as albumart:
                    audio.add(APIC(
                        encoding=3,
                        mime='image/jpeg',
                        type=3,
                        desc='Cover',
                        data=albumart.read()
                    ))
                audio.add(TIT2(encoding=3, text=info.get("title", "")))
                audio.add(TPE1(encoding=3, text=info.get("artist", "")))
                if info.get("album"):
                    audio.add(TALB(encoding=3, text=info.get("album", "")))
                audio.save(v2_version=3)
        except Exception as e:
            logger.warning(f"Error tagging MP3: {e}")

    return mp3_path, (str(cover_temp) if has_cover else None), track_duration

async def download_media(url: str, quality_req: str, cached_data: dict, is_audio: bool = False, progress_callback = None, cancel_token = None):
    global ACTIVE_DOWNLOADS
    use_queue = is_queue_enabled()

    if use_queue:
        async with DOWNLOAD_SEMAPHORE:
            ACTIVE_DOWNLOADS += 1
            try:
                return await _download_media_internal(url, quality_req, cached_data, is_audio, progress_callback, cancel_token)
            finally:
                ACTIVE_DOWNLOADS = max(0, ACTIVE_DOWNLOADS - 1)
    else:
        return await _download_media_internal(url, quality_req, cached_data, is_audio, progress_callback, cancel_token)

async def _download_media_internal(url: str, quality_req: str, cached_data: dict, is_audio: bool = False, progress_callback = None, cancel_token = None):
    if cancel_token and cancel_token.is_set():
        raise asyncio.CancelledError("Cancelled before download")

    timestamp = int(time.time() * 1000)
    info = cached_data["info"]
    source = info.get("source", "ytdlp")

    if source == "spotify_track":
        mp3_file, cover_file, dur = await download_spotify_track(info, progress_callback=progress_callback, cancel_token=cancel_token)
        info["cover_file"] = cover_file
        info["duration"] = dur
        return mp3_file, info

    if source == "tiktok":
        if is_audio and info.get("music_url"):
            out_file = DOWNLOAD_DIR / f"{timestamp}_{info['id']}.mp3"
            await download_file_url(info["music_url"], out_file)
            return str(out_file), info
        elif info.get("play_url"):
            out_file = DOWNLOAD_DIR / f"{timestamp}_{info['id']}.mp4"
            await download_file_url(info["play_url"], out_file)
            return str(out_file), info
        else:
            raise ValueError("Direct stream URL not found")

    if info.get("media_type") == "photo" or source in ["pinterest", "instagram_photo", "twitter"]:
        if info.get("media_type") == "photo":
            img_url = info.get("url") or info.get("image_url") or info.get("thumbnail") or info.get("fallback_image_url")
            ext = "jpg" if ".jpg" in img_url.lower() else ("png" if ".png" in img_url.lower() else ("webp" if ".webp" in img_url.lower() else "jpg"))
            out_file = DOWNLOAD_DIR / f"{timestamp}_{info['id']}.{ext}"
            try:
                await download_file_url(img_url, out_file)
            except Exception:
                if info.get("fallback_image_url"):
                    await download_file_url(info["fallback_image_url"], out_file)
                else:
                    raise
            return str(out_file), info

    out_template = str(DOWNLOAD_DIR / f"{timestamp}_%(id)s.%(ext)s")
    if is_audio:
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': out_template,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
        }
    else:
        # Prefer native pre-muxed mp4 formats (h264+aac) to avoid slow CPU transcoding!
        if is_instagram(url):
            # Instagram already delivers H.264 mp4; do NOT re-encode with CPU!
            fmt = "best"
            ydl_opts = {
                'format': fmt,
                'outtmpl': out_template,
                'merge_output_format': 'mp4',
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True,
            }
        else:
            if quality_req == "best":
                fmt = "bestvideo*[ext=mp4]+bestaudio[ext=m4a]/bestvideo*+bestaudio/best[ext=mp4]/best"
            else:
                fmt = f"bestvideo*[height<={quality_req}][ext=mp4]+bestaudio[ext=m4a]/bestvideo*[height<={quality_req}]+bestaudio/best[height<={quality_req}]/best"

            ydl_opts = {
                'format': fmt,
                'outtmpl': out_template,
                'merge_output_format': 'mp4',
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True,
            }

    if COOKIES_FILE.exists():
        ydl_opts['cookiefile'] = str(COOKIES_FILE)
    if NODE_BIN.exists():
        ydl_opts['js_runtimes'] = {'node': {'path': str(NODE_BIN)}}

    loop = asyncio.get_running_loop()

    last_update_time = 0

    def _ytdl_hook(d):
        nonlocal last_update_time
        if cancel_token and cancel_token.is_set():
            raise yt_dlp.utils.DownloadCancelled("Download cancelled via /stop")
        if not progress_callback:
            return
        if d.get('status') == 'downloading':
            now = time.time()
            if now - last_update_time >= 1.0:
                last_update_time = now
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or (d.get('fragment_count') if d.get('fragment_count') else 0)
                downloaded = d.get('downloaded_bytes') or (d.get('fragment_index') if d.get('fragment_index') else 0)
                if total > 0:
                    pct = min(100.0, (downloaded / total) * 100.0)
                    bar_str = make_progress_bar(pct)
                    asyncio.run_coroutine_threadsafe(progress_callback(bar_str, pct), loop)
        elif d.get('status') == 'finished':
            bar_str = make_progress_bar(100.0)
            asyncio.run_coroutine_threadsafe(progress_callback(bar_str, 100.0), loop)

    if progress_callback or cancel_token:
        ydl_opts['progress_hooks'] = [_ytdl_hook]

    def _download():
        if cancel_token and cancel_token.is_set():
            raise yt_dlp.utils.DownloadCancelled("Download cancelled via /stop")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_res = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info_res)
            if is_audio:
                filename = os.path.splitext(filename)[0] + ".mp3"
            else:
                base = os.path.splitext(filename)[0]
                if os.path.exists(base + ".mp4"):
                    filename = base + ".mp4"
            return filename, info_res
            
    try:
        filename, info_res = await loop.run_in_executor(None, _download)
    except yt_dlp.utils.DownloadCancelled:
        raise asyncio.CancelledError("Download aborted via /stop")

    # If audio download, download cover art image if available
    cover_file = None
    if is_audio:
        cover_url = info_res.get("thumbnail")
        if not cover_url and info_res.get("thumbnails"):
            cover_url = info_res["thumbnails"][-1].get("url")
        if cover_url:
            cover_temp = DOWNLOAD_DIR / f"{timestamp}_cover.jpg"
            try:
                headers = {"User-Agent": "Mozilla/5.0"}
                async with aiohttp.ClientSession(headers=headers) as s:
                    async with s.get(cover_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status == 200:
                            data = await r.read()
                            with open(cover_temp, "wb") as f:
                                f.write(data)
                            cover_file = str(cover_temp)
            except Exception as e:
                logger.debug(f"Could not download audio cover: {e}")

    info_res["cover_file"] = cover_file
    return filename, info_res

# ----------------- Global Ban Interceptor -----------------

@app.on_message(group=-2)
async def global_ban_interceptor(client: Client, message: Message):
    """Blocks banned users completely across PM and Groups for all commands and downloads."""
    if not message.from_user or message.from_user.id == ADMIN_ID:
        return

    uid = message.from_user.id
    if not is_user_banned(uid):
        return

    is_group = message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]

    # In groups: silently stop processing if banned user sends any command or link
    if is_group:
        # If user sends a command or link, drop it completely so bot ignores banned user in groups!
        if (message.text and (message.text.startswith("/") or re.search(r'https?://', message.text))):
            message.stop_propagation()
        return

    # In PM: allow only non-start messages to reach the banned_user_pm_relay for appeals
    if message.text and message.text.startswith("/start"):
        user_lang = get_user_lang(uid)
        await message.reply(
            MESSAGES[user_lang]["banned_alert"],
            reply_markup=get_banned_markup(user_lang),
            parse_mode=enums.ParseMode.HTML
        )
        message.stop_propagation()
        return

    # Other PM commands (/help, /stop, /lang, /report) are blocked with notice
    if message.text and message.text.startswith("/"):
        user_lang = get_user_lang(uid)
        await message.reply(
            MESSAGES[user_lang]["banned_alert"],
            reply_markup=get_banned_markup(user_lang),
            parse_mode=enums.ParseMode.HTML
        )
        message.stop_propagation()
        return

# ----------------- Banned User Appeal Relay -----------------

@app.on_callback_query(filters.regex(r"^appeal:start"))
async def appeal_start_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    user_lang = get_user_lang(uid)
    PENDING_APPEAL_USERS.add(uid)
    await callback.answer()
    await callback.message.reply(
        MESSAGES[user_lang]["banned_appeal_prompt"],
        parse_mode=enums.ParseMode.HTML
    )

@app.on_message(filters.private & ~filters.command("start"), group=-1)
async def banned_user_pm_relay(client: Client, message: Message):
    if not message.from_user or message.from_user.id == ADMIN_ID:
        return
    
    uid = message.from_user.id
    if not is_user_banned(uid):
        return

    user_lang = get_user_lang(uid)

    # If the user hasn't clicked "Contact Admin & Appeal" button yet, show ban notice with the button!
    if uid not in PENDING_APPEAL_USERS:
        await message.reply(
            MESSAGES[user_lang]["banned_alert"],
            reply_markup=get_banned_markup(user_lang),
            parse_mode=enums.ParseMode.HTML
        )
        message.stop_propagation()
        return

    # User clicked appeal and is sending their message -> relay to bot ADMIN_ID!
    PENDING_APPEAL_USERS.discard(uid)
    admin_lang = get_user_lang(ADMIN_ID)
    first_name = html.escape(message.from_user.first_name or "")
    username = f"@{message.from_user.username}" if message.from_user.username else ("ندارد" if admin_lang == "fa" else "None")
    msg_content = html.escape(message.text or message.caption or ("[ارسال فایل یا مدیا]" if admin_lang == "fa" else "[Sent file or media]"))

    if admin_lang == "fa":
        relay_text = (
            f"📩 <b>پیام جدید از کاربر مسدودشده:</b>\n\n"
            f"👤 <b>نام:</b> {first_name}\n"
            f"🆔 <b>آیدی عددی:</b> <code>{uid}</code>\n"
            f"🔗 <b>نام کاربری:</b> {username}\n\n"
            f"💬 <b>متن پیام / درخواست:</b>\n"
            f"<blockquote>{msg_content}</blockquote>"
        )
        btn_unban_label = "🔓 رفع مسدودیت (Unban)"
    else:
        relay_text = (
            f"📩 <b>New Appeal from Banned User:</b>\n\n"
            f"👤 <b>Name:</b> {first_name}\n"
            f"🆔 <b>User ID:</b> <code>{uid}</code>\n"
            f"🔗 <b>Username:</b> {username}\n\n"
            f"💬 <b>Message / Appeal:</b>\n"
            f"<blockquote>{msg_content}</blockquote>"
        )
        btn_unban_label = "🔓 Unban User"

    quick_unban_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(btn_unban_label, callback_data=f"unban_quick:{uid}")]
    ])

    try:
        await client.send_message(
            ADMIN_ID,
            relay_text,
            reply_markup=quick_unban_markup,
            parse_mode=enums.ParseMode.HTML
        )
        await message.reply(MESSAGES[user_lang]["banned_appeal_sent"], parse_mode=enums.ParseMode.HTML)
    except Exception as e:
        logger.error(f"Failed to relay banned user appeal to admin: {e}")
        await message.reply(MESSAGES[user_lang]["banned_alert"], reply_markup=get_banned_markup(user_lang), parse_mode=enums.ParseMode.HTML)

    message.stop_propagation()

@app.on_callback_query(filters.regex(r"^unban_quick:(\d+)"))
async def quick_unban_callback(client: Client, callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔️ Unauthorized", show_alert=True)
        return

    admin_lang = get_user_lang(ADMIN_ID)
    target_uid = callback.matches[0].group(1)
    unbanned = unban_user(target_uid)

    if unbanned:
        alert_txt = "✅ کاربر رفع مسدودیت شد!" if admin_lang == "fa" else "✅ User was unbanned successfully!"
        await callback.answer(alert_txt, show_alert=True)
        try:
            curr_text = callback.message.text.html if callback.message.text else ""
            status_line = "✅ <b>این کاربر توسط شما رفع مسدودیت شد.</b>" if admin_lang == "fa" else "✅ <b>This user has been unbanned by you.</b>"
            updated_text = f"{curr_text}\n\n{status_line}"
            await callback.edit_message_text(updated_text, reply_markup=None, parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass

        # Notify user in PM
        try:
            target_lang = get_user_lang(int(target_uid))
            unban_notice = (
                "🎉 <b>حساب شما توسط مدیریت ربات رفع مسدودیت شد!</b>\nاکنون می‌توانید از تمامی امکانات ربات استفاده کنید."
                if target_lang == "fa" else
                "🎉 <b>Your account has been unbanned by the administrator!</b>\nYou may now use all features of the bot."
            )
            await client.send_message(int(target_uid), unban_notice, parse_mode=enums.ParseMode.HTML)
        except Exception as e:
            logger.debug(f"Could not send unban notification to user {target_uid}: {e}")
    else:
        fail_txt = "⚠️ این کاربر قبلاً آن‌بن شده است یا یافت نشد." if admin_lang == "fa" else "⚠️ This user was already unbanned or not found."
        await callback.answer(fail_txt, show_alert=True)

# ----------------- Handlers -----------------

@app.on_message(filters.command("start"))
async def start_handler(client: Client, message: Message):
    user_lang = get_user_lang(message.from_user.id) if message.from_user else "fa"
    uid = message.from_user.id if message.from_user else message.chat.id

    if message.from_user and message.from_user.id != ADMIN_ID:
        if is_user_banned(uid):
            await message.reply(MESSAGES[user_lang]["banned_alert"], reply_markup=get_banned_markup(user_lang), parse_mode=enums.ParseMode.HTML)
            return

    # Check Force Join on start command
    if message.from_user and message.from_user.id != ADMIN_ID and is_fsub_enabled_for("pm"):
        unjoined = await get_unjoined_channels(client, message.from_user.id)
        if unjoined:
            await message.reply(
                tr("force_join_msg", user_lang),
                reply_markup=get_multi_force_join_markup(unjoined, user_lang),
                parse_mode=enums.ParseMode.HTML
            )
            return

    # Check if there is a deep-link payload (e.g. /start dl_... or /start sp_... or /start u_ENCODEDURL)
    if len(message.command) > 1:
        payload = message.command[1]
        try:
            resolved_url = resolve_short_token(payload)
            if resolved_url:
                message.text = resolved_url
                await url_handler(client, message)
                return
            elif payload.startswith("dl_"):
                vid_id = payload[3:]
                fake_url = f"https://www.youtube.com/watch?v={vid_id}"
                message.text = fake_url
                await url_handler(client, message)
                return
            elif payload.startswith("sp_"):
                track_id = payload[3:]
                fake_url = f"https://open.spotify.com/track/{track_id}"
                message.text = fake_url
                await url_handler(client, message)
                return
        except Exception as e:
            logger.error(f"Failed to process start payload {payload}: {e}")

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇮🇷 فارسی", callback_data="lang:fa"),
            InlineKeyboardButton("🇬🇧 English", callback_data="lang:en")
        ]
    ])
    await message.reply(
        MESSAGES["fa"]["choose_lang"],
        reply_markup=markup,
        parse_mode=enums.ParseMode.HTML
    )

@app.on_message(filters.command("help"))
async def help_command_handler(client: Client, message: Message):
    is_group = message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]
    user_lang = get_chat_lang(message.chat.id, message.from_user.id if message.from_user else None, is_group)
    help_body = MESSAGES[user_lang]["help_text"]
    await message.reply(help_body, parse_mode=enums.ParseMode.HTML)

@app.on_message(filters.command("stop"))
async def stop_command_handler(client: Client, message: Message):
    is_group = message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]
    user_lang = get_chat_lang(message.chat.id, message.from_user.id if message.from_user else None, is_group)
    uid = message.from_user.id if message.from_user else message.chat.id

    stopped = False
    not_owner = False

    # 1. In groups: only the person who started this download (or chat admins) can stop it!
    if is_group:
        grp_info = ACTIVE_GROUP_TASKS.get(message.chat.id)
        if grp_info:
            task_owner_id = grp_info.get("user_id")
            is_chat_admin = False
            if uid == ADMIN_ID:
                is_chat_admin = True
            else:
                try:
                    cm = await client.get_chat_member(message.chat.id, uid)
                    if cm.status in [enums.ChatMemberStatus.OWNER, enums.ChatMemberStatus.ADMINISTRATOR]:
                        is_chat_admin = True
                except Exception:
                    pass

            if task_owner_id == uid or is_chat_admin:
                g_token = ACTIVE_GROUP_CANCEL_TOKENS.pop(message.chat.id, None)
                if g_token:
                    g_token.set()
                if task_owner_id:
                    u_token = ACTIVE_CANCEL_TOKENS.pop(task_owner_id, None)
                    if u_token:
                        u_token.set()

                g_task = grp_info.get("task")
                if g_task and not g_task.done():
                    g_task.cancel()
                ACTIVE_GROUP_TASKS.pop(message.chat.id, None)
                if task_owner_id:
                    ACTIVE_USER_TASKS.pop(task_owner_id, None)
                stopped = True
            else:
                not_owner = True
        else:
            u_token = ACTIVE_CANCEL_TOKENS.pop(uid, None)
            if u_token:
                u_token.set()
                stopped = True
            task = ACTIVE_USER_TASKS.pop(uid, None)
            if task and not task.done():
                task.cancel()
                stopped = True
    else:
        # 2. In PM:
        u_token = ACTIVE_CANCEL_TOKENS.pop(uid, None)
        if u_token:
            u_token.set()
            stopped = True
        task = ACTIVE_USER_TASKS.pop(uid, None)
        if task and not task.done():
            task.cancel()
            stopped = True

    if stopped:
        await reply_autodel(message, MESSAGES[user_lang]["stop_success"], delay=30)
    elif not_owner:
        await reply_autodel(message, MESSAGES[user_lang]["stop_not_owner"], delay=30)
    else:
        await reply_autodel(message, MESSAGES[user_lang]["stop_no_task"], delay=30)

@app.on_message(filters.command("ban") & filters.user(ADMIN_ID))
async def direct_ban_command(client: Client, message: Message):
    admin_lang = get_user_lang(ADMIN_ID)
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(
            "⚠️ استفاده: <code>/ban 12345678</code> یا <code>/ban @username</code>" if admin_lang == "fa" else "⚠️ Usage: <code>/ban 12345678</code> or <code>/ban @username</code>",
            parse_mode=enums.ParseMode.HTML
        )
        return

    raw_target = args[1].strip()
    clean_target = raw_target.lstrip("@")
    if clean_target == str(ADMIN_ID):
        await message.reply(
            "⚠️ امکان مسدود کردن ادمین ربات وجود ندارد!" if admin_lang == "fa" else "⚠️ You cannot ban the bot admin!",
            parse_mode=enums.ParseMode.HTML
        )
        return
    uid_to_ban = None
    uname_to_ban = ""
    if clean_target.isdigit():
        uid_to_ban = int(clean_target)
    else:
        uname_to_ban = clean_target
        try:
            u_obj = await client.get_users(clean_target)
            if u_obj:
                uid_to_ban = u_obj.id
                uname_to_ban = u_obj.username or clean_target
        except Exception:
            pass

    if uid_to_ban:
        if uid_to_ban == ADMIN_ID:
            await message.reply(
                "⚠️ امکان مسدود کردن ادمین ربات وجود ندارد!" if admin_lang == "fa" else "⚠️ You cannot ban the bot admin!",
                parse_mode=enums.ParseMode.HTML
            )
            return
        ban_user(uid_to_ban, uname_to_ban, reason="Admin Direct /ban Command")
        await message.reply(
            f"✅ کاربر <code>{uid_to_ban}</code> (@{uname_to_ban or 'ندارد'}) با موفقیت مسدود شد." if admin_lang == "fa" else f"✅ User <code>{uid_to_ban}</code> (@{uname_to_ban or 'none'}) was banned successfully.",
            parse_mode=enums.ParseMode.HTML
        )
        # Instantly notify the banned user in PM
        try:
            target_lang = get_user_lang(uid_to_ban)
            await client.send_message(
                uid_to_ban,
                MESSAGES[target_lang]["banned_alert"],
                reply_markup=get_banned_markup(target_lang),
                parse_mode=enums.ParseMode.HTML
            )
        except Exception as e:
            logger.debug(f"Could not send instant ban notification to {uid_to_ban}: {e}")
    else:
        await message.reply(
            "❌ شناسه کاربر نامعتبر است یا پیدا نشد." if admin_lang == "fa" else "❌ Invalid user ID or could not resolve username.",
            parse_mode=enums.ParseMode.HTML
        )

@app.on_message(filters.command("unban") & filters.user(ADMIN_ID))
async def direct_unban_command(client: Client, message: Message):
    admin_lang = get_user_lang(ADMIN_ID)
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(
            "⚠️ استفاده: <code>/unban 12345678</code> یا <code>/unban @username</code>" if admin_lang == "fa" else "⚠️ Usage: <code>/unban 12345678</code> or <code>/unban @username</code>",
            parse_mode=enums.ParseMode.HTML
        )
        return

    raw_target = args[1].strip()
    res = unban_user(raw_target)
    if res:
        await message.reply(
            f"✅ کاربر <code>{raw_target}</code> با موفقیت رفع مسدودیت شد." if admin_lang == "fa" else f"✅ User <code>{raw_target}</code> was unbanned successfully.",
            parse_mode=enums.ParseMode.HTML
        )
    else:
        await message.reply(
            "⚠️ این کاربر در لیست مسدودشدگان یافت نشد." if admin_lang == "fa" else "⚠️ User not found in banned list.",
            parse_mode=enums.ParseMode.HTML
        )

@app.on_message(filters.command("report"))
async def report_command_handler(client: Client, message: Message):
    is_group = message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]
    user_lang = get_chat_lang(message.chat.id, message.from_user.id if message.from_user else None, is_group)
    report_body = MESSAGES[user_lang]["report_msg"]
    # Send clean text without inline buttons so Telegram renders the rich profile link preview!
    await message.reply(
        report_body,
        disable_web_page_preview=False,
        parse_mode=enums.ParseMode.HTML
    )

@app.on_message(filters.command("lang"))
async def lang_command_handler(client: Client, message: Message):
    is_group = message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]
    current_lang = get_chat_lang(message.chat.id, message.from_user.id if message.from_user else None, is_group)

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇮🇷 فارسی", callback_data=f"lang:fa:{'grp' if is_group else 'pm'}"),
            InlineKeyboardButton("🇬🇧 English", callback_data=f"lang:en:{'grp' if is_group else 'pm'}")
        ]
    ])
    await message.reply(
        MESSAGES[current_lang]["choose_lang"],
        reply_markup=markup,
        parse_mode=enums.ParseMode.HTML
    )

@app.on_callback_query(filters.regex(r"^lang:"))
async def language_selected_callback(client: Client, callback: CallbackQuery):
    parts = callback.data.split(":")
    lang = parts[1]
    is_grp_target = len(parts) > 2 and parts[2] == "grp"

    if is_grp_target:
        # Check if user is admin in group
        try:
            member = await client.get_chat_member(callback.message.chat.id, callback.from_user.id)
            if member.status not in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
                await callback.answer("❌ Only group admins can change the group language!\nفقط ادمین‌های گروه می‌توانند زبان گروه را تغییر دهند.", show_alert=True)
                return
        except Exception:
            pass
        set_group_lang(callback.message.chat.id, lang)
    else:
        set_user_lang(callback.from_user.id, lang)

    await callback.answer()
    await callback.edit_message_text(
        tr("lang_set", lang) + ("" if is_grp_target else ("\n\n" + tr("welcome", lang))),
        parse_mode=enums.ParseMode.HTML
    )

@app.on_message(filters.text & filters.regex(r'https?://[^\s]+'))
async def url_handler(client: Client, message: Message, specific_url: str = None):
    is_group = message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]
    # Check if user is banned
    uid = message.from_user.id if message.from_user else message.chat.id
    if message.from_user and message.from_user.id != ADMIN_ID:
        if is_user_banned(uid):
            user_lang = get_chat_lang(message.chat.id, uid, message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP])
            try:
                await message.reply(MESSAGES[user_lang]["banned_alert"], reply_markup=get_banned_markup(user_lang), parse_mode=enums.ParseMode.HTML)
            except Exception:
                pass
            return

    # Anti-spam protection
    if message.from_user and message.from_user.id != ADMIN_ID and not specific_url:
        now = time.time()
        user_data = SPAM_TRACKER.setdefault(uid, {"timestamps": [], "warned": False})
        # Keep only timestamps from last 10 seconds
        user_data["timestamps"] = [ts for ts in user_data["timestamps"] if now - ts < 10.0]
        user_data["timestamps"].append(now)

        user_lang = get_chat_lang(message.chat.id, uid, message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP])

        # If more than 6 messages in 10s -> Auto Ban!
        if len(user_data["timestamps"]) >= 6:
            username = message.from_user.username or message.from_user.first_name or ""
            ban_user(uid, username, reason="Auto-ban: Frequent Spamming")
            await message.reply(MESSAGES[user_lang]["banned_alert"], reply_markup=get_banned_markup(user_lang), parse_mode=enums.ParseMode.HTML)
            logger.warning(f"User {uid} ({username}) auto-banned for spamming!")
            return

        # If 3 or more requests in 10s -> Warn user!
        if len(user_data["timestamps"]) >= 3:
            if not user_data["warned"]:
                user_data["warned"] = True
                await reply_autodel(message, MESSAGES[user_lang]["spam_warning"], delay=30)
                return
        else:
            user_data["warned"] = False

    # Track this task for cancellation via /stop
    current_task = asyncio.current_task()
    cancel_token = threading.Event()

    # Duplicate Link In-Flight Guard (Checked BEFORE entering queue!):
    # If this exact link is CURRENTLY being downloaded, reject immediately so it never enters the queue!
    scope_key = None
    target_check_url = specific_url
    if not target_check_url and message.text:
        found_urls = extract_all_supported_urls(message.text)
        if len(found_urls) == 1:
            target_check_url = found_urls[0]

    if target_check_url:
        norm_url = re.sub(r'[?&](s|stkn|igsh|igshid|utm_\w+|si|feature)=[^&]*', '', target_check_url).rstrip('?&')
        scope_key = (message.chat.id, norm_url) if is_group else (uid, norm_url)
        if scope_key in ACTIVE_URL_DOWNLOADS:
            user_lang = get_chat_lang(message.chat.id, uid, is_group)
            await reply_autodel(message, MESSAGES[user_lang]["duplicate_link"], delay=30)
            return
        ACTIVE_URL_DOWNLOADS.add(scope_key)

    queued_notice = None
    try:
        # In Groups: process downloads one-by-one per group using a Group Semaphore
        if is_group:
            grp_sem = GROUP_QUEUES.setdefault(message.chat.id, asyncio.Semaphore(1))
            if grp_sem.locked():
                user_lang = get_chat_lang(message.chat.id, uid, is_group=True)
                # Keep hourglass notice on screen until download actually starts!
                queued_notice = await message.reply(MESSAGES[user_lang]["group_queued"], parse_mode=enums.ParseMode.HTML)
            async with grp_sem:
                if queued_notice:
                    try:
                        await queued_notice.delete()
                    except Exception:
                        pass
                    queued_notice = None
                ACTIVE_GROUP_TASKS[message.chat.id] = {"task": current_task, "user_id": uid}
                ACTIVE_GROUP_CANCEL_TOKENS[message.chat.id] = cancel_token
                ACTIVE_USER_TASKS[uid] = current_task
                ACTIVE_CANCEL_TOKENS[uid] = cancel_token
                await _process_url_handler(client, message, specific_url, cancel_token=cancel_token)
        else:
            # In PM: admin can process concurrently; normal users are queued sequentially
            if uid == ADMIN_ID:
                ACTIVE_USER_TASKS[uid] = current_task
                ACTIVE_CANCEL_TOKENS[uid] = cancel_token
                await _process_url_handler(client, message, specific_url, cancel_token=cancel_token)
            else:
                usr_sem = USER_QUEUES.setdefault(uid, asyncio.Semaphore(1))
                if usr_sem.locked():
                    user_lang = get_chat_lang(message.chat.id, uid, is_group=False)
                    # Keep hourglass notice on screen until download actually starts!
                    queued_notice = await message.reply(MESSAGES[user_lang]["user_queued"], parse_mode=enums.ParseMode.HTML)
                async with usr_sem:
                    if queued_notice:
                        try:
                            await queued_notice.delete()
                        except Exception:
                            pass
                        queued_notice = None
                    ACTIVE_USER_TASKS[uid] = current_task
                    ACTIVE_CANCEL_TOKENS[uid] = cancel_token
                    await _process_url_handler(client, message, specific_url, cancel_token=cancel_token)
    except asyncio.CancelledError:
        cancel_token.set()
        logger.info(f"Task for user {uid} was cancelled via /stop.")
    finally:
        if queued_notice:
            try:
                await queued_notice.delete()
            except Exception:
                pass
        ACTIVE_CANCEL_TOKENS.pop(uid, None)
        if is_group and ACTIVE_GROUP_CANCEL_TOKENS.get(message.chat.id) == cancel_token:
            ACTIVE_GROUP_CANCEL_TOKENS.pop(message.chat.id, None)
        if is_group and ACTIVE_GROUP_TASKS.get(message.chat.id, {}).get("task") == current_task:
            ACTIVE_GROUP_TASKS.pop(message.chat.id, None)
        if scope_key and scope_key in ACTIVE_URL_DOWNLOADS:
            ACTIVE_URL_DOWNLOADS.discard(scope_key)
        if message.from_user and ACTIVE_USER_TASKS.get(uid) == current_task:
            ACTIVE_USER_TASKS.pop(uid, None)

async def _process_url_handler(client: Client, message: Message, specific_url: str = None, cancel_token = None):
    # If specific_url is provided (e.g. from multi-link button), process only that one
    if specific_url:
        supported_urls = [specific_url]
    else:
        supported_urls = extract_all_supported_urls(message.text)

    if not supported_urls:
        return

    is_group = message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]
    user_lang = get_chat_lang(message.chat.id, message.from_user.id if message.from_user else None, is_group)

    # Check Force Join subscription based on scope (PM vs Group)
    if message.from_user and message.from_user.id != ADMIN_ID:
        should_check = False
        if not is_group and is_fsub_enabled_for("pm"):
            should_check = True
        elif is_group and is_fsub_enabled_for("group"):
            should_check = True

        if should_check:
            unjoined = await get_unjoined_channels(client, message.from_user.id)
            if unjoined:
                await message.reply(
                    tr("force_join_msg", user_lang),
                    reply_markup=get_multi_force_join_markup(unjoined, user_lang),
                    parse_mode=enums.ParseMode.HTML
                )
                return

    # If message contains MULTIPLE media links (2 or more), ask user with buttons!
    # (Do NOT delete this message when a button is clicked, user can click multiple items!)
    if len(supported_urls) > 1 and not specific_url:
        buttons = []
        for idx, u in enumerate(supported_urls[:6], 1):
            lbl = get_platform_label(u)
            token = create_short_token(u)
            lnk_txt = tr("link_num", user_lang, idx=idx)
            buttons.append([InlineKeyboardButton(f"📥 {lbl} ({lnk_txt})", callback_data=f"sel_url:{token}")])

        markup = InlineKeyboardMarkup(buttons)
        multi_msg = tr("multi_links_found", user_lang, count=len(supported_urls))
        await message.reply(multi_msg, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        return

    # Exactly ONE link to process!
    url = supported_urls[0]
    logger.info(f"Processing URL for Chat {message.chat.id}: {url}")

    # Pure Chat Action: never spam temporary text messages in PM or group!
    # Default to TYPING while extracting info, then set exact action (UPLOAD_VIDEO, UPLOAD_PHOTO, UPLOAD_AUDIO) once media type is known!
    await client.send_chat_action(message.chat.id, enums.ChatAction.TYPING)

    try:
        info = await extract_info(url)
    except Exception as e:
        logger.error(f"Extraction error: {e}", exc_info=True)
        err_str = str(e)
        if "IG_STORY_LOGIN_REQUIRED" in err_str:
            await message.reply(MESSAGES[user_lang]["err_ig_story"], parse_mode=enums.ParseMode.HTML)
        else:
            safe_err = html.escape(err_str[:300])
            await message.reply(tr("err_extract", user_lang, err=safe_err), parse_mode=enums.ParseMode.HTML)
        return

    vid_id = str(info.get('id') or int(time.time()))
    raw_title = info.get('title', 'Media')
    title = html.escape(raw_title)
    duration_str = format_duration(info.get('duration'))
    uploader = html.escape(info.get('artist') or info.get('uploader') or info.get('channel') or "Unknown")
    thumbnail = clean_thumbnail_url(info.get('thumbnail') or info.get('cover_url'))

    store_media_cache(vid_id, {
        'url': url,
        'title': raw_title,
        'info': info
    })

    # 1. Spotify Track & SoundCloud Track (Consistent for both PM and Groups!)
    if info.get("source") in ["spotify_track", "ytdlp"] and (is_spotify_track(url) or is_soundcloud(url)):
        album_val = html.escape(info.get('album')) if info.get('album') else ""
        year_val = html.escape(info.get('year')) if info.get('year') else ""
        platform_name = "SoundCloud" if is_soundcloud(url) else "Spotify"
        
        caption_lines = [f"🎵 <b>{title}</b>\n"]
        caption_lines.append(f"👤 <b>{tr('lbl_singer', user_lang)}:</b> {uploader}")
        if album_val:
            caption_lines.append(f"💽 <b>{tr('lbl_album', user_lang)}:</b> {album_val}")
        if year_val:
            caption_lines.append(f"📅 <b>{tr('lbl_year', user_lang)}:</b> {year_val}")
        caption_lines.append(f"🟢 <b>{tr('lbl_platform', user_lang)}:</b> {platform_name}")
        caption = "\n".join(caption_lines)

        # 1. Send single cover card with clean metadata
        cover_msg = None
        if thumbnail:
            try:
                cover_msg = await message.reply_photo(photo=thumbnail, caption=caption, parse_mode=enums.ParseMode.HTML)
            except Exception:
                cover_msg = await message.reply(text=caption, parse_mode=enums.ParseMode.HTML)
        else:
            cover_msg = await message.reply(text=caption, parse_mode=enums.ParseMode.HTML)

        # 2. Download & send 320kbps audio with live progress bar on the cover card!
        await client.send_chat_action(message.chat.id, enums.ChatAction.UPLOAD_AUDIO)
        file_path = None
        cover_file = None
        try:
            last_sp_edit = 0
            async def _sp_progress(bar_str: str, pct: float, custom_prefix: str = None):
                nonlocal last_sp_edit
                now = time.time()
                if now - last_sp_edit < 1.2 and pct < 100.0:
                    return
                last_sp_edit = now
                if cover_msg:
                    try:
                        pfx = custom_prefix or ("⏳ <i>Downloading track...</i>" if user_lang == "en" else "⏳ <i>در حال دانلود آهنگ...</i>")
                        txt = f"{caption}\n\n{pfx}\n{bar_str}"
                        if cover_msg.photo:
                            await cover_msg.edit_caption(txt, parse_mode=enums.ParseMode.HTML)
                        else:
                            await cover_msg.edit_text(txt, parse_mode=enums.ParseMode.HTML)
                    except Exception:
                        pass

            # Initial progress bar 0.0%
            await _sp_progress(make_progress_bar(0.0), 0.0)

            file_path, dl_info = await download_media(url, "320", cached_data=MEDIA_CACHE[vid_id], is_audio=True, progress_callback=_sp_progress, cancel_token=cancel_token)
            cover_file = dl_info.get("cover_file")
            bot_user = await client.get_me()
            bot_link = f"https://t.me/{bot_user.username}" if bot_user.username else ""
            bot_credit = f"<a href='{bot_link}'>⚡️ @{bot_user.username}</a>" if bot_user.username else ""
            track_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton(tr("btn_lyrics", user_lang), callback_data=f"lyr:{vid_id}")]
            ])
            thumb_path = cover_file if (cover_file and os.path.exists(cover_file)) else None
            dur_val = int(dl_info.get("duration") or 0)

            clean_caption = (
                f"🎵 <b>{html.escape(title)}</b>\n"
                f"👤 <b>{html.escape(uploader)}</b>\n\n"
                f"{bot_credit}"
            )

            async def _upload_sp_progress(current, total):
                if total > 0:
                    pct = (current / total) * 100.0
                    bar_str = make_progress_bar(pct)
                    up_pfx = "📤 <i>Uploading track...</i>" if user_lang == "en" else "📤 <i>در حال ارسال آهنگ...</i>"
                    await _sp_progress(bar_str, pct, custom_prefix=up_pfx)

            await message.reply_audio(
                audio=file_path,
                title=title,
                performer=uploader,
                duration=dur_val if dur_val > 0 else None,
                thumb=thumb_path,
                caption=clean_caption,
                reply_markup=track_markup,
                progress=_upload_sp_progress,
                parse_mode=enums.ParseMode.HTML
            )
            # Final state on cover card
            if cover_msg:
                try:
                    done_line = "✅ <b>Downloaded:</b> MP3 (320kbps)" if user_lang == "en" else "✅ <b>دانلود انجام شد:</b> MP3 (320kbps)"
                    if cover_msg.photo:
                        await cover_msg.edit_caption(f"{caption}\n\n{done_line}", parse_mode=enums.ParseMode.HTML)
                    else:
                        await cover_msg.edit_text(f"{caption}\n\n{done_line}", parse_mode=enums.ParseMode.HTML)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Audio download failed: {e}")
            try:
                await message.reply(tr("err_extract", user_lang, err=html.escape(str(e)[:200])), parse_mode=enums.ParseMode.HTML)
            except Exception:
                pass
        finally:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
            if cover_file and os.path.exists(cover_file):
                os.remove(cover_file)
        return

    # 2. Spotify Album / Playlist or SoundCloud Set / Album
    if info.get("source") in ["spotify_album", "soundcloud_album"]:
        count = info.get("tracks_count", 0)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(tr("btn_album", user_lang, count=count), callback_data=f"alb:{vid_id}")]
        ])
        year_val = html.escape(info.get('year')) if info.get('year') else ""
        year_str = f"📅 <b>{tr('lbl_year', user_lang)}:</b> {year_val}\n" if year_val else ""
        if info.get("source") == "soundcloud_album":
            type_label = "SoundCloud Album / Playlist"
        else:
            type_label = "Spotify Playlist" if info.get("is_playlist") else "Spotify Album"
        caption = (
            f"💽 <b>{title}</b>\n\n"
            f"👤 <b>{tr('lbl_singer', user_lang)}:</b> {uploader}\n"
            f"🔢 <b>{tr('lbl_tracks_count', user_lang)}:</b> {count}\n"
            f"{year_str}"
            f"🟢 <b>{tr('lbl_platform', user_lang)}:</b> {type_label}\n\n"
            f"{tr('choose_album', user_lang)}"
        )
        try:
            if thumbnail:
                await message.reply_photo(photo=thumbnail, caption=caption, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
            else:
                await message.reply(text=caption, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        except Exception:
            await message.reply(text=caption, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        return

    # 3. Instagram Carousel / Multi-Photo Post -> Send as high-res Telegram Album (MediaGroup)!
    if info.get("media_type") == "carousel" and info.get("media_list"):
        media_list = info.get("media_list", [])
        await client.send_chat_action(message.chat.id, enums.ChatAction.UPLOAD_PHOTO)
        bot_user = await client.get_me()
        bot_link = f"https://t.me/{bot_user.username}" if bot_user.username else ""
        bot_credit = f"<a href='{bot_link}'>⚡️ @{bot_user.username}</a>" if bot_user.username else ""
        caption = f"🖼 <b>{html.escape(title)}</b>\n\n{bot_credit}"

        temp_files = []
        try:
            for idx, item in enumerate(media_list):
                t_stamp = int(time.time() * 1000) + idx
                ext = "mp4" if item.get("is_video") else "jpg"
                fpath = DOWNLOAD_DIR / f"{t_stamp}_{info['id']}_{idx}.{ext}"
                await download_file_url(item["url"], fpath)
                temp_files.append((str(fpath), item.get("is_video", False)))

            # Smart Carousel Chunking:
            # First send initial chunks (without caption), and put caption ONLY on the final collection!
            # E.g. for 16 photos: first 6 photos without caption, then the last 10 photos WITH caption!
            chunks = []
            total = len(temp_files)
            if total <= 10:
                chunks.append(temp_files)
            else:
                rem = total % 10
                if rem > 0:
                    chunks.append(temp_files[:rem])
                    for i in range(rem, total, 10):
                        chunks.append(temp_files[i:i + 10])
                else:
                    for i in range(0, total, 10):
                        chunks.append(temp_files[i:i + 10])

            for ch_idx, chunk in enumerate(chunks):
                is_last_chunk = (ch_idx == len(chunks) - 1)
                group_media = []
                for idx, (f_path, is_vid) in enumerate(chunk):
                    # Attach caption only to the first photo of the FINAL chunk so it sits at the bottom nicely!
                    cap = caption if (is_last_chunk and idx == 0) else None
                    if is_vid:
                        group_media.append(InputMediaVideo(media=f_path, caption=cap, parse_mode=enums.ParseMode.HTML))
                    else:
                        group_media.append(InputMediaPhoto(media=f_path, caption=cap, parse_mode=enums.ParseMode.HTML))

                sent_msgs = await message.reply_media_group(media=group_media)
                # Only show audio button if the carousel actually has an audio track (e.g. TikTok slideshow or video slides)
                has_audio_track = bool(info.get("music_url") or any(item.get("is_video") for item in media_list))
                if is_last_chunk and sent_msgs and has_audio_track:
                    audio_markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton(tr("btn_audio", user_lang), callback_data=f"dl:a:{vid_id}:mp3")]
                    ])
                    try:
                        await message.reply(
                            f"🎵 <i>{tr('btn_audio', user_lang)}</i>",
                            reply_markup=audio_markup,
                            parse_mode=enums.ParseMode.HTML
                        )
                    except Exception:
                        pass
                await asyncio.sleep(1.0)
        except Exception as e:
            logger.error(f"Carousel delivery failed: {e}", exc_info=True)
            try:
                await message.reply(tr("err_extract", user_lang, err=html.escape(str(e)[:200])), parse_mode=enums.ParseMode.HTML)
            except Exception:
                pass
        finally:
            for f_path, _ in temp_files:
                if os.path.exists(f_path):
                    try:
                        os.remove(f_path)
                    except Exception:
                        pass
        return

    # 4. Photos (Pinterest, Single Instagram Photo, Twitter/X photo) -> Direct High-Res Photo Delivery!
    if info.get("media_type") == "photo" or info.get("type") == "photo":
        await client.send_chat_action(message.chat.id, enums.ChatAction.UPLOAD_PHOTO)
        file_path = None
        try:
            file_path, _ = await download_media(url, "original", cached_data=MEDIA_CACHE[vid_id], is_audio=False)
            bot_user = await client.get_me()
            bot_link = f"https://t.me/{bot_user.username}" if bot_user.username else ""
            bot_credit = f"<a href='{bot_link}'>⚡️ @{bot_user.username}</a>" if bot_user.username else ""

            caption = (
                f"🖼 <b>{html.escape(title)}</b>\n\n"
                f"{bot_credit}"
            )
            await message.reply_photo(
                photo=file_path,
                caption=caption,
                parse_mode=enums.ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Photo download failed: {e}")
            try:
                await message.reply(tr("err_extract", user_lang, err=html.escape(str(e)[:200])), parse_mode=enums.ParseMode.HTML)
            except Exception:
                pass
        finally:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
        return

    # 4. Videos: ONLY YouTube gets the quality selection menu!
    # All other video platforms (Instagram, TikTok, Twitter/X, Reddit, Pinterest video) -> Direct Video Delivery!
    if not is_youtube(url):
        await client.send_chat_action(message.chat.id, enums.ChatAction.UPLOAD_VIDEO)
        file_path = None
        status_msg = None
        try:
            # Send a sleek live progress message so the user can see downloading status
            init_bar = make_progress_bar(0.0)
            status_text = f"⏳ <i>Downloading...</i>\n{init_bar}" if user_lang == "en" else f"⏳ <i>در حال دریافت ویدیو...</i>\n{init_bar}"
            status_msg = await message.reply(status_text, parse_mode=enums.ParseMode.HTML)

            last_edit_time = 0

            async def _direct_progress(bar_text: str, pct: float, custom_text: str = None):
                nonlocal last_edit_time
                now = time.time()
                # Update at most once per 1.5 seconds to respect Telegram rate limits
                if now - last_edit_time < 1.5 and pct < 100.0:
                    return
                last_edit_time = now
                if status_msg:
                    try:
                        p_text = custom_text or (f"⏳ <i>Downloading...</i>\n{bar_text}" if user_lang == "en" else f"⏳ <i>در حال دریافت ویدیو...</i>\n{bar_text}")
                        await status_msg.edit_text(p_text, parse_mode=enums.ParseMode.HTML)
                    except Exception:
                        pass

            file_path, dl_info = await download_media(url, "best", cached_data=MEDIA_CACHE[vid_id], is_audio=False, progress_callback=_direct_progress, cancel_token=cancel_token)

            # Check if file size exceeds Telegram's 2GB bot limit
            if file_path and os.path.exists(file_path):
                f_size = os.path.getsize(file_path)
                if f_size > (2000 * 1024 * 1024):
                    os.remove(file_path)
                    file_path = None
                    if status_msg:
                        try:
                            await status_msg.delete()
                        except Exception:
                            pass
                    await reply_autodel(message, tr("err_size_limit", user_lang), delay=30)
                    return

            bot_user = await client.get_me()
            bot_link = f"https://t.me/{bot_user.username}" if bot_user.username else ""
            bot_credit = f"<a href='{bot_link}'>⚡️ @{bot_user.username}</a>" if bot_user.username else ""

            # Probe exact width, height, duration, and check whether audio stream exists
            v_w, v_h, v_dur, has_audio = get_media_metadata(file_path)

            # ONLY show Audio extraction button if the video actually contains an audio track! (Mute GIFs/clips don't get useless button)
            if has_audio:
                # Estimate MP3 filesize based on duration (128kbps ~ 16 KB/s)
                audio_sz_label = ""
                if v_dur and v_dur > 0:
                    est_audio_bytes = int(v_dur * 16 * 1024)
                    audio_sz_label = f" ({format_filesize(est_audio_bytes)})"

                video_markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{tr('btn_audio', user_lang)}{audio_sz_label}", callback_data=f"dl:a:{vid_id}:mp3")]
                ])
            else:
                video_markup = None

            clean_caption = (
                f"🎬 <b>{html.escape(title)}</b>\n\n"
                f"{bot_credit}"
            )

            # Generate crisp thumbnail from video frame so Telegram desktop/mobile never shows black card
            thumb_gen_path = str(DOWNLOAD_DIR / f"thumb_{vid_id}.jpg")
            video_thumb = generate_video_thumbnail(file_path, thumb_gen_path)

            async def _upload_progress(current, total):
                if status_msg and total > 0:
                    pct = (current / total) * 100.0
                    bar_str = make_progress_bar(pct)
                    p_text = f"📤 <i>Uploading...</i>\n{bar_str}" if user_lang == "en" else f"📤 <i>در حال ارسال ویدیو...</i>\n{bar_str}"
                    await _direct_progress(bar_str, pct, custom_text=p_text)

            await message.reply_video(
                video=file_path,
                caption=clean_caption,
                reply_markup=video_markup,
                width=v_w,
                height=v_h,
                duration=v_dur,
                thumb=video_thumb,
                supports_streaming=True,
                progress=_upload_progress,
                parse_mode=enums.ParseMode.HTML
            )
            if video_thumb and os.path.exists(video_thumb):
                try:
                    os.remove(video_thumb)
                except Exception:
                    pass
            # Delete progress status message once video is sent
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Direct video download failed: {e}")
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            try:
                await message.reply(tr("err_extract", user_lang, err=html.escape(str(e)[:200])), parse_mode=enums.ParseMode.HTML)
            except Exception:
                pass
        finally:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
        return

    # YouTube: Send preview card with multiple resolution buttons
    # Filter out any quality options exceeding 2GB (2048 MB) and append estimated filesize
    buttons = []
    formats = info.get('formats', [])
    dur = info.get('duration') or 0
    MAX_2GB_BYTES = 2000 * 1024 * 1024  # Safe 2000MB cap

    # Find size of best audio stream to compute combined muxed size
    best_audio_sz = 0
    for f in formats:
        if f.get('vcodec') == 'none' and f.get('acodec') != 'none':
            s = f.get('filesize') or f.get('filesize_approx')
            if not s and f.get('tbr') and dur:
                s = int((f['tbr'] * 1024 / 8) * dur)
            if s and s > best_audio_sz:
                best_audio_sz = s

    # Map heights to estimated size and filter > 2GB
    height_sizes = {}
    for f in formats:
        h = f.get('height')
        vc = f.get('vcodec', 'none')
        ext = f.get('ext')
        if not h or vc == 'none' or h not in [360, 480, 720, 1080, 1440, 2160]:
            continue

        s = f.get('filesize') or f.get('filesize_approx')
        if not s and f.get('tbr') and dur:
            s = int((f['tbr'] * 1024 / 8) * dur)

        tot_sz = s if f.get('acodec') != 'none' else ((s or 0) + best_audio_sz)

        # Skip options that exceed 2GB!
        if tot_sz > MAX_2GB_BYTES:
            continue

        if h not in height_sizes or ext == 'mp4':
            height_sizes[h] = tot_sz

    # Two-column layout: Left column has Quality / Audio buttons, Right column displays estimated file size (display-only)
    buttons = []
    for h in sorted(height_sizes.keys()):
        sz = height_sizes[h]
        sz_label = format_filesize(sz) if sz and sz > 0 else "—"
        buttons.append([
            InlineKeyboardButton(tr("btn_video", user_lang, h=h), callback_data=f"dl:v:{vid_id}:{h}"),
            InlineKeyboardButton(f"💾 {sz_label}", callback_data=f"sz:{vid_id}:{sz_label}")
        ])

    if not height_sizes:
        buttons.append([
            InlineKeyboardButton(tr("btn_video_best", user_lang), callback_data=f"dl:v:{vid_id}:best"),
            InlineKeyboardButton("💾 —", callback_data=f"sz:{vid_id}:—")
        ])

    audio_sz = format_filesize(best_audio_sz) if best_audio_sz > 0 else "—"
    buttons.append([
        InlineKeyboardButton(tr("btn_audio", user_lang), callback_data=f"dl:a:{vid_id}:mp3"),
        InlineKeyboardButton(f"💾 {audio_sz}", callback_data=f"sz:{vid_id}:{audio_sz}")
    ])

    markup = InlineKeyboardMarkup(buttons)
    dur_line = f"⏱ <b>{tr('lbl_duration', user_lang)}:</b> {duration_str}\n\n" if duration_str else "\n"
    caption = (
        f"🎬 <b>{title}</b>\n\n"
        f"👤 <b>{tr('lbl_channel', user_lang)}:</b> {uploader}\n"
        f"{dur_line}"
        f"{tr('choose_quality', user_lang)}"
    )

    try:
        if thumbnail:
            await message.reply_photo(photo=thumbnail, caption=caption, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        else:
            await message.reply(text=caption, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
    except Exception:
        await message.reply(text=caption, reply_markup=markup, parse_mode=enums.ParseMode.HTML)

# ----------------- Inline Query Handler -----------------

@app.on_inline_query()
async def inline_query_handler(client: Client, inline_query: InlineQuery):
    query_text = inline_query.query.strip()
    user_lang = get_user_lang(inline_query.from_user.id) if inline_query.from_user else "en"
    logger.info(f"Inline query received: '{query_text}'")

    # Check if user is banned
    if inline_query.from_user and inline_query.from_user.id != ADMIN_ID:
        if is_user_banned(inline_query.from_user.id):
            await inline_query.answer([], switch_pm_text="🚫 شما از ربات مسدود شده‌اید" if user_lang == "fa" else "🚫 You are banned from using this bot", switch_pm_parameter="banned", cache_time=5)
            return

    # Check Force Join subscription for inline mode if enabled
    if inline_query.from_user and inline_query.from_user.id != ADMIN_ID and is_fsub_enabled_for("inline"):
        unjoined = await get_unjoined_channels(client, inline_query.from_user.id)
        if unjoined:
            clean_first = unjoined[0].lstrip("@")
            title_t = "🔒 برای استفاده ابتدا عضو کانال اسپانسر شوید" if user_lang == "fa" else "🔒 Please join sponsor channel first"
            desc_t = f"کلیک کنید تا به کانال {unjoined[0]} بروید و عضو شوید." if user_lang == "fa" else f"Click to join {unjoined[0]} channel."
            msg_t = f"🔒 لطفاً برای استفاده از ربات در کانال اسپانسر عضو شوید:\nhttps://t.me/{clean_first}" if user_lang == "fa" else f"🔒 Please join our sponsor channel to use the bot:\nhttps://t.me/{clean_first}"
            btn_t = f"📢 عضویت در {unjoined[0]}" if user_lang == "fa" else f"📢 Join {unjoined[0]}"
            sw_pm = "🔒 عضویت در کانال اسپانسر الزامی است" if user_lang == "fa" else "🔒 Sponsor channel join required"

            results = [
                InlineQueryResultArticle(
                    title=title_t,
                    description=desc_t,
                    input_message_content=InputTextMessageContent(msg_t),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(btn_t, url=f"https://t.me/{clean_first}")]
                    ]),
                    thumb_url="https://cdn-icons-png.flaticon.com/512/3064/3064155.png"
                )
            ]
            await inline_query.answer(
                results=results,
                switch_pm_text=sw_pm,
                switch_pm_parameter="fsub",
                cache_time=1
            )
            return

    if not query_text:
        await inline_query.answer(
            results=[],
            switch_pm_text="🔗 Paste any video/music link here...",
            switch_pm_parameter="help",
            cache_time=5
        )
        return

    match = re.search(r'https?://[^\s]+', query_text)
    if not match:
        # Search query mode! Search YouTube for keywords (e.g. 'eminem')
        loop = asyncio.get_running_loop()
        def _search():
            ydl_opts = {'quiet': True, 'extract_flat': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(f"ytsearch5:{query_text}", download=False)

        try:
            search_data = await loop.run_in_executor(None, _search)
            entries = search_data.get('entries', [])
            search_results = []
            for item in entries:
                if not item:
                    continue
                v_title = html.escape(item.get('title', 'Video'))
                v_url = item.get('url') or f"https://www.youtube.com/watch?v={item.get('id')}"
                v_id = item.get('id')
                v_thumb = f"https://i.ytimg.com/vi/{v_id}/hqdefault.jpg" if v_id else "https://telegra.ph/file/2dc255eb5db02d73fcf02.jpg"
                v_dur = format_duration(item.get('duration'))

                # Quick button to download via bot in PM
                bot_user = await client.get_me()
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚡ Download", url=f"https://t.me/{bot_user.username}?start=dl_{v_id}")]
                ])

                search_results.append(
                    InlineQueryResultArticle(
                        id=str(v_id or time.time()),
                        title=item.get('title', 'Video'),
                        description=f"{item.get('uploader') or 'YouTube'} | {v_dur}",
                        thumb_url=v_thumb,
                        input_message_content=InputTextMessageContent(
                            f"🎬 <b>{v_title}</b>\n\n🔗 {v_url}\n\n🤖 @{bot_user.username}",
                            parse_mode=enums.ParseMode.HTML
                        ),
                        reply_markup=kb
                    )
                )
            await inline_query.answer(results=search_results, cache_time=15)
        except Exception as e:
            logger.debug(f"Inline search failed: {e}")
            await inline_query.answer(results=[], cache_time=5)
        return

    url = match.group(0).strip()

    # Fast oEmbed path for YouTube in inline query (0.07s instead of 10s heavy yt-dlp extraction)
    # Telegram inline queries expire in ~5 seconds; oEmbed prevents QUERY_ID_INVALID timeout!
    if is_youtube(url):
        try:
            oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
            headers = {"User-Agent": "Mozilla/5.0"}
            async with aiohttp.ClientSession(headers=headers) as s:
                async with s.get(oembed_url, timeout=aiohttp.ClientTimeout(total=2.5)) as r:
                    if r.status == 200:
                        oe_data = await r.json()
                        raw_title = oe_data.get('title') or 'YouTube Video'
                        title = html.escape(raw_title)
                        uploader = html.escape(oe_data.get('author_name') or 'YouTube')
                        thumbnail = oe_data.get('thumbnail_url') or "https://telegra.ph/file/2dc255eb5db02d73fcf02.jpg"
                        bot_user = await client.get_me()
                        short_token = create_short_token(url)
                        pm_dl_url = f"https://t.me/{bot_user.username}?start={short_token}"

                        markup = InlineKeyboardMarkup([
                            [InlineKeyboardButton("⚡ دانلود ویدیو / Download Video", url=pm_dl_url)]
                        ])
                        caption = (
                            f"🎬 <b>{title}</b>\n\n"
                            f"👤 <b>{tr('lbl_channel', user_lang)}:</b> {uploader}\n\n"
                            f"🤖 @{bot_user.username}"
                        )
                        results = [
                            InlineQueryResultPhoto(
                                photo_url=thumbnail,
                                thumb_url=thumbnail,
                                title=title,
                                description=f"{uploader} | YouTube Video",
                                caption=caption,
                                parse_mode=enums.ParseMode.HTML,
                                reply_markup=markup
                            )
                        ]
                        await inline_query.answer(results=results, cache_time=15)
                        return
        except Exception as e:
            logger.debug(f"YouTube fast oEmbed failed: {e}")

    try:
        info = await extract_info(url)
    except Exception as e:
        logger.debug(f"Inline extract failed for {url}: {e}")
        await inline_query.answer(
            results=[],
            switch_pm_text=f"❌ Error: {str(e)[:40]}",
            switch_pm_parameter="err",
            cache_time=5
        )
        return

    vid_id = str(info.get('id') or int(time.time()))
    raw_title = info.get('title', 'Media')
    title = html.escape(raw_title)
    uploader = html.escape(info.get('artist') or info.get('uploader') or info.get('channel') or "Unknown")
    thumbnail = clean_thumbnail_url(info.get('thumbnail') or info.get('cover_url') or "https://telegra.ph/file/2dc255eb5db02d73fcf02.jpg")
    duration_str = format_duration(info.get('duration'))

    store_media_cache(vid_id, {
        'url': url,
        'title': raw_title,
        'info': info
    })

    bot_user = await client.get_me()

    # In inline query results (sent in other chats), standard callback buttons for file sending
    # cannot upload media directly into third-party chats without file_id.
    # We provide a clean Deep-Link button to download directly in bot's PM!
    pm_dl_btn = InlineKeyboardButton("⚡ دانلود در ربات / Download in Bot", url=f"https://t.me/{bot_user.username}")

    # Generate clean short token (e.g. 12-char alphanumeric hash) stored in DB
    short_token = create_short_token(url)
    pm_dl_url = f"https://t.me/{bot_user.username}?start={short_token}"

    # 1. Spotify Track & SoundCloud Track
    if info.get("source") in ["spotify_track", "ytdlp"] and (is_spotify_track(url) or is_soundcloud(url)):
        plat = "SoundCloud" if is_soundcloud(url) else "Spotify"
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ دانلود این موزیک / Download Track", url=pm_dl_url)]
        ])
        album_val = html.escape(info.get('album')) if info.get('album') else ""
        caption_lines = [f"🎵 <b>{title}</b>\n"]
        caption_lines.append(f"👤 <b>{tr('lbl_singer', user_lang)}:</b> {uploader}")
        if album_val:
            caption_lines.append(f"💽 <b>{tr('lbl_album', user_lang)}:</b> {album_val}")
        caption_lines.append(f"🟢 <b>{tr('lbl_platform', user_lang)}:</b> {plat}\n")
        caption = "\n".join(caption_lines)

    # 2. Spotify Album / Playlist or SoundCloud Set
    elif info.get("source") in ["spotify_album", "soundcloud_album"]:
        count = info.get("tracks_count", 0)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(tr("btn_album", user_lang, count=count), url=pm_dl_url)]
        ])
        if info.get("source") == "soundcloud_album":
            type_label = "SoundCloud Set / Album"
        else:
            type_label = "Spotify Playlist" if info.get("is_playlist") else "Spotify Album"
        caption = (
            f"💽 <b>{title}</b>\n\n"
            f"👤 <b>{tr('lbl_singer', user_lang)}:</b> {uploader}\n"
            f"🔢 <b>{tr('lbl_tracks_count', user_lang)}:</b> {count}\n"
            f"🟢 <b>{tr('lbl_platform', user_lang)}:</b> {type_label}"
        )

    # 3. Photos & Carousels (Pinterest, Twitter, Instagram)
    elif info.get("media_type") in ["photo", "carousel"]:
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(tr("btn_photo", user_lang), url=pm_dl_url)]
        ])
        caption = (
            f"🖼 <b>{title}</b>\n\n"
            f"📌 <b>{tr('lbl_source', user_lang)}:</b> {uploader}"
        )

    # 4. Videos
    else:
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ دانلود ویدیو / Download Video", url=pm_dl_url)]
        ])
        dur_line = f"⏱ <b>{tr('lbl_duration', user_lang)}:</b> {duration_str}\n\n" if duration_str else "\n"
        caption = (
            f"🎬 <b>{title}</b>\n\n"
            f"👤 <b>{tr('lbl_channel', user_lang)}:</b> {uploader}\n"
            f"{dur_line}"
        )

    # Build inline result
    results = [
        InlineQueryResultPhoto(
            photo_url=thumbnail,
            thumb_url=thumbnail,
            title=title,
            description=f"{uploader} | {duration_str if duration_str else 'Media'}",
            caption=caption,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=markup
        )
    ]

    await inline_query.answer(results=results, cache_time=10)

# ----------------- Lyrics Handler -----------------

@app.on_callback_query(filters.regex(r"^lyr:"))
async def lyrics_callback(client: Client, callback: CallbackQuery):
    user_lang = get_user_lang(callback.from_user.id)
    vid_id = callback.data.split(":", 1)[1]
    title = ""
    artist = ""

    # Check if data is directly in cache or encoded as artist@@title
    if vid_id in MEDIA_CACHE:
        title = MEDIA_CACHE[vid_id]['info'].get('title', '')
        artist = MEDIA_CACHE[vid_id]['info'].get('artist', '')
    elif "@@" in vid_id:
        artist, title = vid_id.split("@@", 1)
    else:
        await callback.answer(tr("expired", user_lang), show_alert=True)
        return

    loop = asyncio.get_running_loop()
    def _fetch_lyrics():
        # syncedlyrics cleans timestamps or returns pure text
        raw = syncedlyrics.search(f"{artist} {title}")
        if not raw:
            return None
        # remove [00:12.34] lrc timestamps for readable telegram presentation
        clean = re.sub(r'\[\d{2}:\d{2}\.\d{2,3}\]', '', raw).strip()
        return clean

    lyrics_text = await loop.run_in_executor(None, _fetch_lyrics)

    if not lyrics_text:
        await callback.message.reply(tr("no_lyrics", user_lang), parse_mode=enums.ParseMode.HTML)
        return

    # Trim if exceeds telegram 4096 character limit
    if len(lyrics_text) > 3800:
        lyrics_text = lyrics_text[:3800] + "\n..."

    lyrics_msg = (
        f"📜 <b>{html.escape(title)}</b>\n"
        f"👤 <b>{html.escape(artist)}</b>\n\n"
        f"{html.escape(lyrics_text)}"
    )
    await callback.message.reply(lyrics_msg, parse_mode=enums.ParseMode.HTML)

# ----------------- Album Handler -----------------

@app.on_callback_query(filters.regex(r"^alb:"))
async def album_callback(client: Client, callback: CallbackQuery):
    user_lang = get_user_lang(callback.from_user.id)
    uid = callback.from_user.id
    is_group = callback.message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]

    # Check if user is banned
    if is_user_banned(uid):
        await callback.answer(clean_alert_text(MESSAGES[user_lang]["banned_msg"]), show_alert=True)
        return

    # Check User Concurrency Lock (One active download per user at a time!)
    if uid != ADMIN_ID:
        existing_task = ACTIVE_USER_TASKS.get(uid)
        if existing_task and not existing_task.done():
            await callback.answer(clean_alert_text(MESSAGES[user_lang]["already_processing"]), show_alert=True)
            return

    vid_id = callback.data.split(":")[1]
    data = MEDIA_CACHE.get(vid_id)
    if not data:
        await callback.answer(clean_alert_text(tr("expired", user_lang)), show_alert=True)
        return

    # In-Flight Duplicate Check: If this album is already downloading, reject immediately!
    norm_url = re.sub(r'[?&](s|stkn|igsh|igshid|utm_\w+|si|feature)=[^&]*', '', data['url']).rstrip('?&')
    scope_key = (callback.message.chat.id, norm_url) if is_group else (uid, norm_url)
    if scope_key in ACTIVE_URL_DOWNLOADS:
        await callback.answer(clean_alert_text(MESSAGES[user_lang]["duplicate_link"]), show_alert=True)
        return
    ACTIVE_URL_DOWNLOADS.add(scope_key)

    await callback.answer()

    info = data["info"]
    source = info.get("source")
    is_sc = (source == "soundcloud_album")
    album_items = info.get("tracks", []) if is_sc else info.get("track_ids", [])
    total = len(album_items)

    # Edit album preview message
    current_caption = callback.message.caption or callback.message.text or ""
    cleaned_base = re.sub(r'\n*👇[^\n]+', '', current_caption).strip()

    try:
        init_alb_bar = make_progress_bar(0.0)
        init_txt = f"{cleaned_base}\n\n⏳ <i>Downloading album tracks (0/{total})...</i>\n{init_alb_bar}"
        if callback.message.caption is not None:
            await callback.edit_message_caption(caption=init_txt, reply_markup=None)
        elif callback.message.text is not None:
            await callback.edit_message_text(text=init_txt, reply_markup=None)
    except Exception:
        pass

    bot_user = await client.get_me()
    bot_tag = f"@{bot_user.username}" if bot_user.username else ""

    cancel_token = threading.Event()
    ACTIVE_CANCEL_TOKENS[uid] = cancel_token
    if is_group:
        ACTIVE_GROUP_CANCEL_TOKENS[callback.message.chat.id] = cancel_token

    async def _process_album_download():
        for index, item in enumerate(album_items, 1):
            if cancel_token.is_set():
                logger.info(f"Album download for user {uid} was cancelled before track {index}")
                break

            try:
                if is_sc:
                    song_title = item.get('title', 'Audio')
                    performer_name = item.get('artist') or info.get('artist') or 'SoundCloud'
                    t_url = item.get('url')
                    dur = item.get('duration') or 0
                    cover_file = None
                else:
                    t_url = f"https://open.spotify.com/track/{item}"
                    t_info = await extract_spotify_track(t_url)
                    song_title = t_info.get('title', 'Audio')
                    performer_name = t_info.get('artist') or info.get('artist') or 'Unknown'

                # Live Per-Track progress callback on the album card
                last_track_edit = 0
                async def _album_track_progress(bar_str: str, pct: float, status_pfx: str = None):
                    nonlocal last_track_edit
                    if cancel_token.is_set():
                        return
                    now = time.time()
                    if now - last_track_edit < 1.2 and pct < 100.0:
                        return
                    last_track_edit = now
                    try:
                        pfx = status_pfx or tr('album_progress', user_lang, current=index, total=total, title=html.escape(song_title))
                        card_txt = f"{cleaned_base}\n\n{pfx}\n{bar_str}"
                        if callback.message.caption is not None:
                            await callback.edit_message_caption(caption=card_txt)
                        elif callback.message.text is not None:
                            await callback.edit_message_text(text=card_txt)
                    except Exception:
                        pass

                # Initial 0.0% bar for this specific track
                await _album_track_progress(make_progress_bar(0.0), 0.0)

                if is_sc:
                    mp3_path, dl_info = await download_media(t_url, "320", cached_data={'info': item}, is_audio=True, progress_callback=_album_track_progress, cancel_token=cancel_token)
                    cover_file = dl_info.get("cover_file")
                    dur = int(dl_info.get("duration") or dur)
                else:
                    mp3_path, cover_file, dur = await download_spotify_track(t_info, progress_callback=_album_track_progress, cancel_token=cancel_token)
                
                if cancel_token.is_set():
                    if mp3_path and os.path.exists(mp3_path):
                        os.remove(mp3_path)
                    if cover_file and os.path.exists(cover_file):
                        os.remove(cover_file)
                    break

                if mp3_path and os.path.exists(mp3_path):
                    thumb = cover_file if (cover_file and os.path.exists(cover_file)) else None

                    track_markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton(tr("btn_lyrics", user_lang), callback_data=f"lyr:{performer_name[:30]}@@{song_title[:30]}")]
                    ])

                    async def _upload_album_track_progress(current, total_bytes):
                        if cancel_token.is_set():
                            return
                        if total_bytes > 0:
                            pct = (current / total_bytes) * 100.0
                            bar_str = make_progress_bar(pct)
                            up_pfx = (f"📤 <b>Uploading:</b> Track {index} of {total}\n🎵 <i>{html.escape(song_title)}</i>"
                                      if user_lang == "en" else
                                      f"📤 <b>در حال ارسال:</b> ترک {index} از {total}\n🎵 <i>{html.escape(song_title)}</i>")
                            await _album_track_progress(bar_str, pct, status_pfx=up_pfx)

                    await callback.message.reply_audio(
                        audio=mp3_path,
                        title=song_title,
                        performer=performer_name,
                        duration=int(dur or 0),
                        thumb=thumb,
                        caption=f"🎵 <b>{html.escape(song_title)}</b>\n👤 <b>{html.escape(performer_name)}</b>\n\n🤖 {bot_tag}",
                        reply_markup=track_markup,
                        progress=_upload_album_track_progress,
                        parse_mode=enums.ParseMode.HTML
                    )

                    if os.path.exists(mp3_path):
                        os.remove(mp3_path)
                    if cover_file and os.path.exists(cover_file):
                        os.remove(cover_file)

            except asyncio.CancelledError:
                cancel_token.set()
                logger.info(f"Album download for user {uid} was cancelled via /stop.")
                break
            except Exception as e:
                if cancel_token.is_set():
                    break
                logger.error(f"Failed to download album track {item}: {e}")
                continue

        # Check if cancelled or completed
        if cancel_token.is_set():
            try:
                cancelled_text = tr("album_cancelled", user_lang)
                if callback.message.caption is not None:
                    await callback.edit_message_caption(caption=f"{cleaned_base}\n\n{cancelled_text}")
                elif callback.message.text is not None:
                    await callback.edit_message_text(text=f"{cleaned_base}\n\n{cancelled_text}")
            except Exception:
                pass
            return

        # Mark album finished
        try:
            done_text = tr("album_done", user_lang)
            if callback.message.caption is not None:
                await callback.edit_message_caption(caption=f"{cleaned_base}\n\n{done_text}")
            elif callback.message.text is not None:
                await callback.edit_message_text(text=f"{cleaned_base}\n\n{done_text}")
        except Exception:
            pass

    current_task = asyncio.current_task()
    if current_task:
        ACTIVE_USER_TASKS[uid] = current_task
        if is_group:
            ACTIVE_GROUP_TASKS[callback.message.chat.id] = {"task": current_task, "user_id": uid}

    try:
        if is_group:
            grp_sem = GROUP_QUEUES.setdefault(callback.message.chat.id, asyncio.Semaphore(1))
            async with grp_sem:
                await _process_album_download()
        else:
            if uid == ADMIN_ID:
                await _process_album_download()
            else:
                usr_sem = USER_QUEUES.setdefault(uid, asyncio.Semaphore(1))
                async with usr_sem:
                    await _process_album_download()
    finally:
        ACTIVE_URL_DOWNLOADS.discard(scope_key)
        ACTIVE_CANCEL_TOKENS.pop(uid, None)
        if is_group and ACTIVE_GROUP_CANCEL_TOKENS.get(callback.message.chat.id) == cancel_token:
            ACTIVE_GROUP_CANCEL_TOKENS.pop(callback.message.chat.id, None)
        if is_group and ACTIVE_GROUP_TASKS.get(callback.message.chat.id, {}).get("task") == current_task:
            ACTIVE_GROUP_TASKS.pop(callback.message.chat.id, None)
        if uid and ACTIVE_USER_TASKS.get(uid) == current_task:
            ACTIVE_USER_TASKS.pop(uid, None)

# ----------------- Download Callback -----------------

@app.on_callback_query(filters.regex(r"^dl:"))
async def download_callback(client: Client, callback: CallbackQuery):
    user_lang = get_user_lang(callback.from_user.id)
    uid = callback.from_user.id

    # Check if user is banned
    if is_user_banned(uid):
        await callback.answer(clean_alert_text(MESSAGES[user_lang]["banned_msg"]), show_alert=True)
        return

    # Check User Concurrency Lock (One active download per user at a time!)
    if uid != ADMIN_ID:
        existing_task = ACTIVE_USER_TASKS.get(uid)
        if existing_task and not existing_task.done():
            await callback.answer(clean_alert_text(MESSAGES[user_lang]["already_processing"]), show_alert=True)
            return

    _, media_type, vid_id, quality = callback.data.split(":")
    
    data = MEDIA_CACHE.get(vid_id)
    if not data:
        await callback.answer(clean_alert_text(tr("expired", user_lang)), show_alert=True)
        return

    # In-Flight Duplicate Check: If this exact link is currently downloading, reject immediately!
    norm_url = re.sub(r'[?&](s|stkn|igsh|igshid|utm_\w+|si|feature)=[^&]*', '', data['url']).rstrip('?&')
    is_group = callback.message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]
    scope_key = (callback.message.chat.id, norm_url) if is_group else (uid, norm_url)
    if scope_key in ACTIVE_URL_DOWNLOADS:
        await callback.answer(clean_alert_text(MESSAGES[user_lang]["duplicate_link"]), show_alert=True)
        return
    ACTIVE_URL_DOWNLOADS.add(scope_key)

    await callback.answer()

    # Register active task for /stop tracking
    current_task = asyncio.current_task()
    cancel_token = threading.Event()
    if current_task:
        ACTIVE_USER_TASKS[uid] = current_task
        ACTIVE_CANCEL_TOKENS[uid] = cancel_token
        if callback.message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
            ACTIVE_GROUP_TASKS[callback.message.chat.id] = {"task": current_task, "user_id": uid}
            ACTIVE_GROUP_CANCEL_TOKENS[callback.message.chat.id] = cancel_token

    if media_type == "p":
        choice_label = tr("btn_photo", user_lang)
    elif media_type in ["a", "s"]:
        choice_label = "MP3 (320kbps)" if media_type == "s" else "MP3"
    elif quality == "best":
        choice_label = tr("btn_video_best", user_lang)
    else:
        choice_label = f"{quality}p"

    current_caption = callback.message.caption or callback.message.text or ""
    cleaned_base = re.sub(r'\n*👇[^\n]+', '', current_caption).strip()

    init_bar = make_progress_bar(0.0)
    status_line = f"⏳ <i>Downloading {choice_label}...</i>\n{init_bar}" if user_lang == "en" else f"⏳ <i>در حال دانلود {choice_label}...</i>\n{init_bar}"
    new_caption = f"{cleaned_base}\n\n{status_line}"

    try:
        if callback.message.caption is not None:
            await callback.edit_message_caption(caption=new_caption, reply_markup=None)
        elif callback.message.text is not None:
            await callback.edit_message_text(text=new_caption, reply_markup=None)
        else:
            await callback.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        logger.debug(f"Could not update in-place status on preview: {e}")

    file_path = None
    cover_file = None
    async def _process_single_download():
        nonlocal file_path, cover_file
        try:
            url = data['url']
            is_audio = (media_type in ["a", "s"])
            is_photo = (media_type == "p")
            
            last_progress_edit = 0
            async def _update_download_progress(bar_text: str, pct: float, status_prefix: str = None):
                nonlocal last_progress_edit
                now = time.time()
                if now - last_progress_edit < 1.2 and pct < 100.0:
                    return
                last_progress_edit = now
                try:
                    pfx = status_prefix or (f"⏳ <i>Downloading {choice_label}...</i>" if user_lang == "en" else f"⏳ <i>در حال دانلود {choice_label}...</i>")
                    updated_text = f"{cleaned_base}\n\n{pfx}\n{bar_text}"
                    if callback.message.caption is not None:
                        await callback.edit_message_caption(caption=updated_text)
                    elif callback.message.text is not None:
                        await callback.edit_message_text(text=updated_text)
                except Exception:
                    pass

            file_path, info = await download_media(url, quality, cached_data=data, is_audio=is_audio, progress_callback=_update_download_progress, cancel_token=cancel_token)
            cover_file = info.get("cover_file")

            if cancel_token.is_set():
                return

            if not os.path.exists(file_path):
                raise FileNotFoundError("Output file not found!")

            file_size = os.path.getsize(file_path)
            # Pyrogram handles up to 2000 MB!
            if file_size > 2000 * 1024 * 1024:
                err_msg = tr("file_too_large", user_lang, size=format_size(file_size))
                if callback.message.caption is not None:
                    await callback.edit_message_caption(caption=f"{cleaned_base}\n\n{err_msg}")
                elif callback.message.text is not None:
                    await callback.edit_message_text(text=f"{cleaned_base}\n\n{err_msg}")
                return

            async def _upload_progress_cb(current, total):
                if total > 0:
                    pct = (current / total) * 100.0
                    bar_str = make_progress_bar(pct)
                    up_pfx = f"📤 <i>Uploading {choice_label}...</i>" if user_lang == "en" else f"📤 <i>در حال ارسال {choice_label}...</i>"
                    await _update_download_progress(bar_str, pct, status_prefix=up_pfx)

            title = data['title']
            bot_user = await client.get_me()
            bot_tag = f"@{bot_user.username}" if bot_user.username else ""

            bot_link = f"https://t.me/{bot_user.username}" if bot_user.username else ""
            bot_credit = f"<a href='{bot_link}'>⚡️ @{bot_user.username}</a>" if bot_user.username else ""

            if is_photo:
                await callback.message.reply_photo(
                    photo=file_path,
                    caption=f"🖼 <b>{html.escape(title)}</b>\n\n{bot_credit}",
                    parse_mode=enums.ParseMode.HTML
                )
            elif is_audio:
                performer_name = info.get('artist') or info.get('uploader') or 'Unknown'
                thumb_path = cover_file if (cover_file and os.path.exists(cover_file)) else None
                duration_val = int(info.get("duration") or 0)

                track_markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton(tr("btn_lyrics", user_lang), callback_data=f"lyr:{vid_id}")]
                ])

                clean_audio_caption = (
                    f"🎵 <b>{html.escape(title)}</b>\n"
                    f"👤 <b>{html.escape(performer_name)}</b>\n\n"
                    f"{bot_credit}"
                )

                await callback.message.reply_audio(
                    audio=file_path,
                    title=title,
                    performer=performer_name,
                    duration=int(duration_val or 0),
                    thumb=thumb_path,
                    caption=clean_audio_caption,
                    reply_markup=track_markup if is_audio and info.get("source") == "spotify_track" else None,
                    progress=_upload_progress_cb,
                    parse_mode=enums.ParseMode.HTML
                )
            else:
                clean_video_caption = (
                    f"🎬 <b>{html.escape(title)}</b>\n\n"
                    f"{bot_credit}"
                )
                v_w, v_h, v_dur, _ = get_media_metadata(file_path)
                thumb_gen_path = str(DOWNLOAD_DIR / f"thumb_cb_{vid_id}.jpg")
                video_thumb = generate_video_thumbnail(file_path, thumb_gen_path)

                await callback.message.reply_video(
                    video=file_path,
                    caption=clean_video_caption,
                    width=v_w,
                    height=v_h,
                    duration=v_dur,
                    thumb=video_thumb,
                    supports_streaming=True,
                    progress=_upload_progress_cb,
                    parse_mode=enums.ParseMode.HTML
                )
                if video_thumb and os.path.exists(video_thumb):
                    try:
                        os.remove(video_thumb)
                    except Exception:
                        pass

            done_line = tr("status_downloaded", user_lang, choice=choice_label)
            try:
                if callback.message.caption is not None:
                    await callback.edit_message_caption(caption=f"{cleaned_base}\n\n{done_line}")
                elif callback.message.text is not None:
                    await callback.edit_message_text(text=f"{cleaned_base}\n\n{done_line}")
            except Exception as e:
                logger.debug(f"Could not edit final caption: {e}")

        except asyncio.CancelledError:
            cancel_token.set()
            logger.info(f"Download for user {uid} was cancelled via /stop.")
            try:
                cancelled_text = "🛑 <b>عملیات دانلود متوقف شد.</b>" if user_lang == "fa" else "🛑 <b>Download was cancelled.</b>"
                if callback.message.caption is not None:
                    await callback.edit_message_caption(caption=f"{cleaned_base}\n\n{cancelled_text}")
                elif callback.message.text is not None:
                    await callback.edit_message_text(text=f"{cleaned_base}\n\n{cancelled_text}")
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Download/Send failed: {e}", exc_info=True)
            safe_err = html.escape(str(e)[:300])
            try:
                err_text = tr("err_extract", user_lang, err=safe_err)
                if callback.message.caption is not None:
                    await callback.edit_message_caption(caption=f"{cleaned_base}\n\n{err_text}")
                elif callback.message.text is not None:
                    await callback.edit_message_text(text=f"{cleaned_base}\n\n{err_text}")
            except Exception:
                pass

    try:
        if callback.message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
            grp_sem = GROUP_QUEUES.setdefault(callback.message.chat.id, asyncio.Semaphore(1))
            async with grp_sem:
                await _process_single_download()
        else:
            if uid == ADMIN_ID:
                await _process_single_download()
            else:
                usr_sem = USER_QUEUES.setdefault(uid, asyncio.Semaphore(1))
                async with usr_sem:
                    await _process_single_download()
    finally:
        ACTIVE_URL_DOWNLOADS.discard(scope_key)
        ACTIVE_CANCEL_TOKENS.pop(uid, None)
        if callback.message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
            if ACTIVE_GROUP_CANCEL_TOKENS.get(callback.message.chat.id) == cancel_token:
                ACTIVE_GROUP_CANCEL_TOKENS.pop(callback.message.chat.id, None)
            if ACTIVE_GROUP_TASKS.get(callback.message.chat.id, {}).get("task") == current_task:
                ACTIVE_GROUP_TASKS.pop(callback.message.chat.id, None)
        if uid and ACTIVE_USER_TASKS.get(uid) == current_task:
            ACTIVE_USER_TASKS.pop(uid, None)

        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"Cleaned up file: {file_path}")
            except Exception as e:
                logger.warning(f"Cleanup error: {e}")

        if cover_file and os.path.exists(cover_file):
            try:
                os.remove(cover_file)
            except Exception:
                pass

@app.on_callback_query(filters.regex(r"^sz:"))
async def size_display_callback(client: Client, callback: CallbackQuery):
    user_lang = get_user_lang(callback.from_user.id)
    parts = callback.data.split(":")
    sz = parts[-1] if len(parts) > 2 else "—"
    msg = f"💾 حجم تخمینی: {sz}" if user_lang == "fa" else f"💾 Estimated size: {sz}"
    await callback.answer(msg, show_alert=False)

# ----------------- Admin Panel & Force Join Callbacks -----------------

@app.on_callback_query(filters.regex(r"^sel_url:(.+)$"))
async def select_url_callback(client: Client, callback: CallbackQuery):
    user_lang = get_user_lang(callback.from_user.id)
    token = callback.matches[0].group(1)
    target_url = resolve_short_token(token)
    if not target_url:
        await callback.answer(tr("expired", user_lang), show_alert=True)
        return

    lbl = get_platform_label(target_url)
    start_txt = f"⏳ دانلود {lbl} آغاز شد..." if user_lang == "fa" else f"⏳ Downloading {lbl} started..."
    await callback.answer(start_txt, show_alert=False)

    # Trigger url_handler specifically for this chosen URL (do NOT delete the multi-link selector message!)
    msg_copy = callback.message
    msg_copy.from_user = callback.from_user
    asyncio.create_task(url_handler(client, msg_copy, specific_url=target_url))

@app.on_callback_query(filters.regex(r"^verify_fsub$"))
async def verify_fsub_callback(client: Client, callback: CallbackQuery):
    user_id = callback.from_user.id
    user_lang = get_user_lang(user_id)
    unjoined = await get_unjoined_channels(client, user_id)
    if not unjoined:
        await callback.answer(tr("joined_success", user_lang), show_alert=True)
        try:
            await callback.message.delete()
        except Exception:
            pass
    else:
        await callback.answer(tr("not_joined_alert", user_lang), show_alert=True)
        # Update buttons with only remaining unjoined channels
        try:
            await callback.message.edit_reply_markup(reply_markup=get_multi_force_join_markup(unjoined, user_lang))
        except Exception:
            pass

ADMIN_TEXTS = {
    "fa": {
        "title": "👑 <b>به پنل مدیریت ربات دانلودر خوش آمدید:</b>\n\nیکی از بخش‌های زیر را انتخاب کنید:",
        "btn_stats": "📊 وضعیت و منابع سرور",
        "btn_users": "👥 آمار کاربران ربات",
        "btn_fsub": "🔒 تنظیم کانال عضویت اجباری",
        "btn_bcast": "📢 ارسال پیام همگانی (Broadcast)",
        "btn_fwd": "🔄 فوروارد همگانی (Forward)",
        "btn_bans": "🚫 مدیریت کاربران مسدود (Ban/Unban)",
        "btn_admin_help": "📖 راهنمای دستورات ادمین",
        "btn_refresh": "🔄 بروزرسانی آمار",
        "btn_back": "🔙 بازگشت به منو",
        "btn_queue_toggle": "🛡 سوییچ صف دانلود: {state}",
        "queue_status_active": "✅ روشن (برای خاموش کردن کلیک کنید)",
        "queue_status_inactive": "❌ خاموش (برای روشن کردن کلیک کنید)",
        "unauthorized": "دسترسی غیرمجاز!",
        "stats_loading": "در حال دریافت آمار سرور...",
        "queue_toggled": "وضعیت صف تغییر کرد!",
        "stats_header": "🖥 <b>وضعیت لحظه‌ای منابع و تحلیل سرور:</b>",
        "lbl_cpu": "پردازنده (CPU)",
        "lbl_ram": "حافظه رم (RAM)",
        "lbl_disk": "فضای دیسک",
        "lbl_uptime": "آپ‌تایم سرور",
        "lbl_queue_status": "وضعیت صف دانلود ایمن",
        "active_prot": "✅ فعال (محافظت روشن)",
        "inactive_prot": "❌ غیرفعال (دانلود مستقیم آزاد)",
        "hw_cap_title": "🎯 <b>ظرفیت تخمینی سخت‌افزار سرور:</b>",
        "suitable_for": "مناسب برای",
        "days": "روز",
        "hours": "ساعت",
        "minutes": "دقیقه",
        "free_of": "آزاد از",
        "cores": "هسته",
        "users_stats": "👥 <b>آمار کاربران ربات:</b>\n\n👤 <b>تعداد کل کاربران استارت‌زده:</b> <code>{total} نفر</code>\n💽 <b>دیتابیس:</b> <code>SQLite (user-space)</code>",
        "fsub_title": "🔒 <b>مدیریت پیشرفته عضویت اجباری (Force Join):</b>\n\n📢 <b>کانال‌های اسپانسر فعلی:</b>\n{channels}\n\n⚙️ <b>وضعیت قفل در بخش‌های مختلف:</b>\n👤 پیوی ربات: <b>{pm}</b>\n👥 گروه‌ها: <b>{grp}</b>\n⚡ حالت اینلاین: <b>{inl}</b>\n\n<i>می‌توانید کانال اضافه/حذف کنید و قفل هر بخش را به دلخواه روشن یا خاموش کنید.</i>",
        "no_channels": "<i>هیچ کانالی تنظیم نشده (قفل خاموش)</i>",
        "state_enabled": "✅ فعال",
        "state_disabled": "❌ غیرفعال",
        "btn_add_ch": "➕ افزودن کانال جدید",
        "btn_del_ch": "➖ حذف کانال",
        "btn_pm_scope": "👤 پیوی: {state}",
        "btn_grp_scope": "👥 گروه: {state}",
        "btn_inl_scope": "⚡ اینلاین: {state}",
        "ask_channel": "📢 لطفاً <b>آیدی کانال اسپانسر</b> را همراه با @ ارسال کنید:\n(مثلاً: <code>@MyChannel</code>)\n\n<i>نکته: ربات حتماً باید در کانال شما ادمین باشد تا بتواند عضویت کاربران را چک کند.</i>",
        "btn_cancel": "❌ انصراف",
        "no_channels_to_del": "هیچ کانالی برای حذف وجود ندارد!",
        "select_del_ch": "🗑 <b>روی کانالی که می‌خواهید از لیست عضویت اجباری حذف شود کلیک کنید:</b>",
        "bcast_prompt": "📢 <b>ارسال پیام همگانی (Broadcast):</b>\n\nپیامی که می‌خواهید برای تمام کاربران ربات ارسال شود را بفرستید (متن، عکس، گیف یا فایل).\nپیام دقیقاً به همان صورت کپی و برای همه ارسال خواهد شد.",
        "fwd_prompt": "🔄 <b>فوروارد همگانی (Forward):</b>\n\nپستی که می‌خواهید برای تمام کاربران فوروارد شود را به اینجا فوروارد یا ارسال کنید.",
        "bcast_start": "⏳ ارسال پیام همگانی برای {count} کاربر شروع شد...",
        "fwd_start": "⏳ فوروارد همگانی برای {count} کاربر شروع شد...",
        "bcast_done": "✅ <b>ارسال همگانی پایان یافت!</b>\n\n📨 <b>موفق:</b> {sent} کاربر\n🚫 <b>ناموفق / بلاک:</b> {blocked} کاربر",
        "fwd_done": "✅ <b>فوروارد همگانی پایان یافت!</b>\n\n📨 <b>موفق:</b> {sent} کاربر\n🚫 <b>ناموفق / بلاک:</b> {blocked} کاربر",
        "ch_added": "✅ کانال <code>{ch}</code> با موفقیت به لیست قفل عضویت اجباری اضافه شد!\nمی‌توانید برای مشاهده یا اضافه کردن کانال‌های بیشتر به پنل /admin مراجعه کنید.",
        "ch_exists": "⚠️ کانال <code>{ch}</code> از قبل در لیست وجود دارد!",
    },
    "en": {
        "title": "👑 <b>Welcome to Downloader Bot Admin Panel:</b>\n\nPlease select an option below:",
        "btn_stats": "📊 Server Resources & Stats",
        "btn_users": "👥 Bot Users Statistics",
        "btn_fsub": "🔒 Force Join Subscription",
        "btn_bcast": "📢 Broadcast Message",
        "btn_fwd": "🔄 Forward Broadcast",
        "btn_bans": "🚫 Banned Users & Anti-Spam",
        "btn_admin_help": "📖 Admin Commands & Guide",
        "btn_refresh": "🔄 Refresh Stats",
        "btn_back": "🔙 Back to Menu",
        "btn_queue_toggle": "🛡 Download Queue: {state}",
        "queue_status_active": "✅ ON (Click to turn OFF)",
        "queue_status_inactive": "❌ OFF (Click to turn ON)",
        "unauthorized": "Unauthorized access!",
        "stats_loading": "Fetching server statistics...",
        "queue_toggled": "Queue status changed!",
        "stats_header": "🖥 <b>Real-time Server Resources & AI Analysis:</b>",
        "lbl_cpu": "Processor (CPU)",
        "lbl_ram": "RAM Memory",
        "lbl_disk": "Disk Space",
        "lbl_uptime": "Server Uptime",
        "lbl_queue_status": "Safe Download Queue",
        "active_prot": "✅ Active (Protection ON)",
        "inactive_prot": "❌ Disabled (Direct Parallel)",
        "hw_cap_title": "🎯 <b>Hardware Estimated Capacity:</b>",
        "suitable_for": "Suitable for",
        "days": "days",
        "hours": "hours",
        "minutes": "mins",
        "free_of": "free of",
        "cores": "cores",
        "users_stats": "👥 <b>Bot Users Statistics:</b>\n\n👤 <b>Total Started Users:</b> <code>{total} users</code>\n💽 <b>Database:</b> <code>SQLite (user-space)</code>",
        "fsub_title": "🔒 <b>Advanced Force Join Management:</b>\n\n📢 <b>Current Sponsor Channels:</b>\n{channels}\n\n⚙️ <b>Lock Status by Scope:</b>\n👤 Private PM: <b>{pm}</b>\n👥 Groups: <b>{grp}</b>\n⚡ Inline Mode: <b>{inl}</b>\n\n<i>You can add/remove channels and toggle locks per environment.</i>",
        "no_channels": "<i>No channels configured (Lock OFF)</i>",
        "state_enabled": "✅ Active",
        "state_disabled": "❌ Disabled",
        "btn_add_ch": "➕ Add New Channel",
        "btn_del_ch": "➖ Remove Channel",
        "btn_pm_scope": "👤 PM: {state}",
        "btn_grp_scope": "👥 Group: {state}",
        "btn_inl_scope": "⚡ Inline: {state}",
        "ask_channel": "📢 Please send the <b>Channel Username</b> with @:\n(e.g. <code>@MyChannel</code>)\n\n<i>Note: The bot must be promoted to administrator in the channel.</i>",
        "btn_cancel": "❌ Cancel",
        "no_channels_to_del": "No channels configured to delete!",
        "select_del_ch": "🗑 <b>Select which channel you want to remove from force join:</b>",
        "bcast_prompt": "📢 <b>Broadcast Message:</b>\n\nSend any message (text, photo, gif, or file) to copy and send to all users.",
        "fwd_prompt": "🔄 <b>Forward Broadcast:</b>\n\nForward any post to forward to all bot users.",
        "bcast_start": "⏳ Broadcasting message to {count} users...",
        "fwd_start": "⏳ Forwarding message to {count} users...",
        "bcast_done": "✅ <b>Broadcast Finished!</b>\n\n📨 <b>Delivered:</b> {sent} users\n🚫 <b>Failed / Blocked:</b> {blocked} users",
        "fwd_done": "✅ <b>Forward Broadcast Finished!</b>\n\n📨 <b>Delivered:</b> {sent} users\n🚫 <b>Failed / Blocked:</b> {blocked} users",
        "ch_added": "✅ Channel <code>{ch}</code> added to Force Join list!\nYou can view or add more via /admin.",
        "ch_exists": "⚠️ Channel <code>{ch}</code> is already in the list!",
    }
}

@app.on_message(filters.command("admin") & filters.private)
async def admin_panel_handler(client: Client, message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    admin_lang = get_user_lang(ADMIN_ID)
    t = ADMIN_TEXTS[admin_lang]
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(t["btn_stats"], callback_data="adm:stats")],
        [InlineKeyboardButton(t["btn_users"], callback_data="adm:users")],
        [InlineKeyboardButton(t["btn_fsub"], callback_data="adm:fsub")],
        [InlineKeyboardButton(t["btn_bcast"], callback_data="adm:bcast")],
        [InlineKeyboardButton(t["btn_fwd"], callback_data="adm:fwd")],
        [InlineKeyboardButton(t["btn_bans"], callback_data="adm:bans")],
        [InlineKeyboardButton(t["btn_admin_help"], callback_data="adm:help")]
    ])
    await message.reply(t["title"], reply_markup=markup, parse_mode=enums.ParseMode.HTML)

@app.on_callback_query(filters.regex(r"^adm:(.+)$"))
async def admin_callback_handler(client: Client, callback: CallbackQuery):
    admin_lang = get_user_lang(ADMIN_ID)
    t = ADMIN_TEXTS[admin_lang]

    if callback.from_user.id != ADMIN_ID:
        await callback.answer(t["unauthorized"], show_alert=True)
        return

    action = callback.matches[0].group(1)

    if action == "menu":
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(t["btn_stats"], callback_data="adm:stats")],
            [InlineKeyboardButton(t["btn_users"], callback_data="adm:users")],
            [InlineKeyboardButton(t["btn_fsub"], callback_data="adm:fsub")],
            [InlineKeyboardButton(t["btn_bcast"], callback_data="adm:bcast")],
            [InlineKeyboardButton(t["btn_fwd"], callback_data="adm:fwd")],
            [InlineKeyboardButton(t["btn_bans"], callback_data="adm:bans")],
            [InlineKeyboardButton(t["btn_admin_help"], callback_data="adm:help")]
        ])
        await callback.edit_message_text(t["title"], reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        return

    if action == "bans":
        banned = get_banned_users()
        if admin_lang == "fa":
            b_list_str = "\n".join([f"• <code>{uid}</code> (@{uname or 'ندارد'}) - {reason}" for uid, uname, reason, _ in banned[:15]]) if banned else "<i>هیچ کاربری مسدود نیست.</i>"
            text = (
                "🚫 <b>مدیریت کاربران مسدود (Ban / Anti-Spam):</b>\n\n"
                f"👥 <b>تعداد کاربران بن‌شده:</b> <code>{len(banned)} نفر</code>\n\n"
                f"📋 <b>لیست مسدودشدگان اخیر:</b>\n{b_list_str}\n\n"
                "برای مسدود یا آزاد کردن کاربر، گزینه‌های زیر را انتخاب کنید:"
            )
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ مسدودسازی کاربر (Ban)", callback_data="adm:add_ban"),
                 InlineKeyboardButton("➖ رفع مسدودی (Unban)", callback_data="adm:rm_ban")],
                [InlineKeyboardButton("🔙 بازگشت به منو", callback_data="adm:menu")]
            ])
        else:
            b_list_str = "\n".join([f"• <code>{uid}</code> (@{uname or 'none'}) - {reason}" for uid, uname, reason, _ in banned[:15]]) if banned else "<i>No banned users.</i>"
            text = (
                "🚫 <b>Banned Users & Anti-Spam Management:</b>\n\n"
                f"👥 <b>Total Banned:</b> <code>{len(banned)} users</code>\n\n"
                f"📋 <b>Recent Banned Users:</b>\n{b_list_str}\n\n"
                "Choose an action below to ban or unban:"
            )
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Ban User", callback_data="adm:add_ban"),
                 InlineKeyboardButton("➖ Unban User", callback_data="adm:rm_ban")],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="adm:menu")]
            ])
        await callback.edit_message_text(text, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        return

    if action == "add_ban":
        PENDING_ADMIN_ACTION[ADMIN_ID] = "add_ban"
        cancel_text = "❌ انصراف" if admin_lang == "fa" else "❌ Cancel"
        prompt = (
            "🚫 لطفاً <b>شناسه عددی (User ID)</b> یا <b>یوزرنیم (@username)</b> کاربری که می‌خواهید مسدود شود را ارسال کنید:"
        ) if admin_lang == "fa" else (
            "🚫 Please send the <b>Numeric User ID</b> or <b>@username</b> of the user you want to ban:"
        )
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(cancel_text, callback_data="adm:bans")]])
        await callback.edit_message_text(prompt, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        return

    if action == "rm_ban":
        PENDING_ADMIN_ACTION[ADMIN_ID] = "rm_ban"
        cancel_text = "❌ انصراف" if admin_lang == "fa" else "❌ Cancel"
        prompt = (
            "✅ لطفاً <b>شناسه عددی (User ID)</b> یا <b>یوزرنیم (@username)</b> کاربری که می‌خواهید رفع مسدودیت شود را ارسال کنید:"
        ) if admin_lang == "fa" else (
            "✅ Please send the <b>Numeric User ID</b> or <b>@username</b> of the user you want to unban:"
        )
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(cancel_text, callback_data="adm:bans")]])
        await callback.edit_message_text(prompt, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        return

    if action == "help":
        if admin_lang == "fa":
            help_adm = (
                "👑 <b>راهنمای جامع پنل مدیریت ربات:</b>\n\n"
                "🔹 <b>📊 وضعیت و منابع سرور:</b>\n"
                "بررسی زنده درصد مصرف CPU، حافظه رم، هارد و آپ‌تایم سرور به همراه تحلیل‌گر هوشمند سخت‌افزار و دکمه کنترل صف ایمن دانلود.\n\n"
                "🔹 <b>🛡 سیستم صف ایمن (Queue):</b>\n"
                "با روشن بودن صف، دانلودهای همزمان کنترل می‌شوند تا سرورهای با رم ۱ گیگابایت دچار کِرَش (OOM) نشوند. اگر سرور قدرتمندی دارید می‌توانید آن را خاموش کنید.\n\n"
                "🔹 <b>🔒 عضویت اجباری (Force Join):</b>\n"
                "امکان افزودن چندین کانال اسپانسر به صورت همزمان. ربات باید در کانال‌ها ادمین باشد. همچنین می‌توانید قفل را به تفکیک برای پیوی، گروه‌ها و حالت اینلاین روشن یا خاموش کنید.\n\n"
                "🔹 <b>📢 ارسال همگانی (Broadcast):</b>\n"
                "ارسال هر نوع پیام، تصویر یا فایل با فرمت کپی به تمام کاربرانی که تاکنون ربات را استارت زده‌اند همراه با گزارش زنده.\n\n"
                "🔹 <b>🔄 فوروارد همگانی (Forward):</b>\n"
                "فوروارد مستقیم یک پست از کانال یا چت به تمام کاربران ربات.\n\n"
                "🔹 <b>🚫 مدیریت کاربران مسدود (Ban/Unban):</b>\n"
                "مشاهده لیست افراد مسدود، بن کردن دستی با شناسه عددی یا یوزرنیم، و رفع مسدودی کاربران خاطی.\n\n"
                "💡 <i>نکته: تمام تنظیمات در دیتابیس لوکال ذخیره شده و هیچ وابستگی به روت یا سرویس‌های خارجی وجود ندارد.</i>"
            )
        else:
            help_adm = (
                "👑 <b>Admin Panel Comprehensive Guide:</b>\n\n"
                "🔹 <b>📊 Server Resources & AI Analysis:</b>\n"
                "Live monitoring of CPU, RAM, Disk, and uptime with hardware estimation capacity and Safe Queue controls.\n\n"
                "🔹 <b>🛡 Safe Download Queue:</b>\n"
                "Prevents VPS memory exhaustion (OOM crashes) on 1GB/2GB servers by controlling concurrent heavy conversions. Can be disabled on high-end servers for maximum parallel speed.\n\n"
                "🔹 <b>🔒 Multi-Channel Force Join:</b>\n"
                "Add/remove multiple sponsor channels with granular toggles for Private PM, Groups, and Inline mode. The bot must be promoted to administrator in the target channels.\n\n"
                "🔹 <b>📢 Broadcast Message:</b>\n"
                "Copies and delivers any message, media, or file to all registered bot users with real-time success and blocked statistics.\n\n"
                "🔹 <b>🔄 Forward Broadcast:</b>\n"
                "Forwards a post from your channel directly to all bot users.\n\n"
                "🔹 <b>🚫 Banned Users & Anti-Spam:</b>\n"
                "View recent blocked users, manually ban by numeric ID or username, and unban users.\n\n"
                "💡 <i>Note: All settings persist in a lightweight SQLite database and run rootless.</i>"
            )

        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(t["btn_back"], callback_data="adm:menu")]
        ])
        await callback.edit_message_text(help_adm, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        return

    if action in ["stats", "tog_queue"]:
        if action == "tog_queue":
            toggle_queue()
            await callback.answer(t["queue_toggled"])
        else:
            await callback.answer(t["stats_loading"])

        cpu_usage = psutil.cpu_percent(interval=0.3)
        cpu_count = psutil.cpu_count(logical=True) or 1
        mem = psutil.virtual_memory()
        mem_used_mb = mem.used // (1024 * 1024)
        mem_total_mb = mem.total // (1024 * 1024)
        disk = psutil.disk_usage('/')
        disk_free_gb = disk.free // (1024 * 1024 * 1024)
        disk_total_gb = disk.total // (1024 * 1024 * 1024)

        uptime_sec = int(time.time() - psutil.boot_time())
        days, rem = divmod(uptime_sec, 86400)
        hours, rem = divmod(rem, 3600)
        mins = rem // 60
        uptime_str = f"{days} {t['days']} {hours} {t['hours']} {mins} {t['minutes']}"

        queue_active = is_queue_enabled()
        queue_state_str = t["active_prot"] if queue_active else t["inactive_prot"]
        queue_btn_text = t["btn_queue_toggle"].format(state=t["queue_status_active"] if queue_active else t["queue_status_inactive"])

        if mem_total_mb <= 1200:
            rec_users = "50 - 150 users" if admin_lang == "en" else "۵۰ تا ۱۵۰ کاربر فعال"
            rec_reason = (
                "⚠️ <b>System Recommendation:</b>\nServer RAM is ~1GB. To prevent OOM crashes during peak hours, "
                "<b>keeping download queue ENABLED is strongly advised!</b>"
            ) if admin_lang == "en" else (
                "⚠️ <b>پیشنهاد هوشمند سیستم:</b>\n"
                "رم سرور شما حدود <b>۱ گیگابایت</b> است. برای جلوگیری از کمبود رم (OOM Crash) و هنگ سرور، "
                "<b>روشن بودن سیستم صف اکیداً توصیه می‌شود!</b> اگر کاربران شما بیش از ۵۰ نفر هستند حتماً صف را روشن نگه دارید."
            )
        elif mem_total_mb <= 2500:
            rec_users = "200 - 500 users" if admin_lang == "en" else "۲۰۰ تا ۵۰۰ کاربر فعال"
            rec_reason = (
                "ℹ️ <b>System Recommendation:</b>\nServer RAM is ~2GB. Up to 200 concurrent downloads work smoothly without queue. "
                "Enable queue if concurrent users exceed 200."
            ) if admin_lang == "en" else (
                "ℹ️ <b>پیشنهاد هوشمند سیستم:</b>\n"
                "رم سرور شما حدود <b>۲ گیگابایت</b> است. تا ۲۰۰ کاربر همزمان بدون صف پاسخگوست. "
                "اگر تعداد کاربران همزمان به بیش از ۲۰۰ نفر رسید، صف را فعال کنید."
            )
        else:
            rec_users = "1000+ users" if admin_lang == "en" else "۱۰۰۰+ کاربر همزمان"
            rec_reason = (
                "⚡ <b>System Recommendation:</b>\nHigh performance hardware detected! Parallel downloads run without limits."
            ) if admin_lang == "en" else (
                "⚡ <b>پیشنهاد هوشمند سیستم:</b>\n"
                "منابع سرور شما بسیار قدرتمند است! سرور به راحتی پردازش‌های موازی را هندل می‌کند و نیازی به فعال‌سازی صف ندارید مگر در ترافیک‌های میلیونی."
            )

        text = (
            f"{t['stats_header']}\n\n"
            f"⚡ <b>{t['lbl_cpu']}:</b> <code>{cpu_usage}% ({cpu_count} {t['cores']})</code>\n"
            f"🧠 <b>{t['lbl_ram']}:</b> <code>{mem_used_mb}MB / {mem_total_mb}MB ({mem.percent}%)</code>\n"
            f"💾 <b>{t['lbl_disk']}:</b> <code>{disk_free_gb}GB {t['free_of']} {disk_total_gb}GB ({disk.percent}%)</code>\n"
            f"⏱ <b>{t['lbl_uptime']}:</b> <code>{uptime_str}</code>\n"
            f"🛡 <b>{t['lbl_queue_status']}:</b> <b>{queue_state_str}</b>\n\n"
            f"{t['hw_cap_title']}\n"
            f"• {t['suitable_for']}: <b>{rec_users}</b>\n\n"
            f"{rec_reason}"
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(queue_btn_text, callback_data="adm:tog_queue")],
            [InlineKeyboardButton(t["btn_refresh"], callback_data="adm:stats")],
            [InlineKeyboardButton(t["btn_back"], callback_data="adm:menu")]
        ])
        await callback.edit_message_text(text, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        return

    if action == "users":
        total_users = get_total_users_count()
        text = t["users_stats"].format(total=total_users)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(t["btn_back"], callback_data="adm:menu")]
        ])
        await callback.edit_message_text(text, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        return

    if action == "fsub":
        channels = get_force_channels()
        ch_list_str = "\n".join([f"• <code>{c}</code>" for c in channels]) if channels else t["no_channels"]
        pm_state = t["state_enabled"] if is_fsub_enabled_for("pm") else t["state_disabled"]
        grp_state = t["state_enabled"] if is_fsub_enabled_for("group") else t["state_disabled"]
        inl_state = t["state_enabled"] if is_fsub_enabled_for("inline") else t["state_disabled"]

        text = t["fsub_title"].format(channels=ch_list_str, pm=pm_state, grp=grp_state, inl=inl_state)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(t["btn_add_ch"], callback_data="adm:add_fsub"),
             InlineKeyboardButton(t["btn_del_ch"], callback_data="adm:del_fsub_menu")],
            [InlineKeyboardButton(t["btn_pm_scope"].format(state=pm_state), callback_data="adm:tog_pm"),
             InlineKeyboardButton(t["btn_grp_scope"].format(state=grp_state), callback_data="adm:tog_group")],
            [InlineKeyboardButton(t["btn_inl_scope"].format(state=inl_state), callback_data="adm:tog_inline")],
            [InlineKeyboardButton(t["btn_back"], callback_data="adm:menu")]
        ])
        await callback.edit_message_text(text, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        return

    if action in ["tog_pm", "tog_group", "tog_inline"]:
        scope = action.split("_")[1]
        toggle_fsub_scope(scope)
        await callback.answer(t["queue_toggled"])

        channels = get_force_channels()
        ch_list_str = "\n".join([f"• <code>{c}</code>" for c in channels]) if channels else t["no_channels"]
        pm_state = t["state_enabled"] if is_fsub_enabled_for("pm") else t["state_disabled"]
        grp_state = t["state_enabled"] if is_fsub_enabled_for("group") else t["state_disabled"]
        inl_state = t["state_enabled"] if is_fsub_enabled_for("inline") else t["state_disabled"]

        text = t["fsub_title"].format(channels=ch_list_str, pm=pm_state, grp=grp_state, inl=inl_state)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(t["btn_add_ch"], callback_data="adm:add_fsub"),
             InlineKeyboardButton(t["btn_del_ch"], callback_data="adm:del_fsub_menu")],
            [InlineKeyboardButton(t["btn_pm_scope"].format(state=pm_state), callback_data="adm:tog_pm"),
             InlineKeyboardButton(t["btn_grp_scope"].format(state=grp_state), callback_data="adm:tog_group")],
            [InlineKeyboardButton(t["btn_inl_scope"].format(state=inl_state), callback_data="adm:tog_inline")],
            [InlineKeyboardButton(t["btn_back"], callback_data="adm:menu")]
        ])
        await callback.edit_message_text(text, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        return

    if action == "add_fsub":
        PENDING_ADMIN_ACTION[ADMIN_ID] = "add_fsub"
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(t["btn_cancel"], callback_data="adm:fsub")]
        ])
        await callback.edit_message_text(t["ask_channel"], reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        return

    if action == "del_fsub_menu":
        channels = get_force_channels()
        if not channels:
            await callback.answer(t["no_channels_to_del"], show_alert=True)
            return
        buttons = []
        for ch in channels:
            buttons.append([InlineKeyboardButton(f"🗑 {ch}", callback_data=f"adm:rm_fsub:{ch}")])
        buttons.append([InlineKeyboardButton(t["btn_back"], callback_data="adm:fsub")])
        await callback.edit_message_text(
            t["select_del_ch"],
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=enums.ParseMode.HTML
        )
        return

    if action.startswith("rm_fsub:"):
        target_ch = action[len("rm_fsub:"):]
        remove_force_channel(target_ch)
        await callback.answer(f"{target_ch} removed!", show_alert=True)

        channels = get_force_channels()
        ch_list_str = "\n".join([f"• <code>{c}</code>" for c in channels]) if channels else t["no_channels"]
        pm_state = t["state_enabled"] if is_fsub_enabled_for("pm") else t["state_disabled"]
        grp_state = t["state_enabled"] if is_fsub_enabled_for("group") else t["state_disabled"]
        inl_state = t["state_enabled"] if is_fsub_enabled_for("inline") else t["state_disabled"]

        text = t["fsub_title"].format(channels=ch_list_str, pm=pm_state, grp=grp_state, inl=inl_state)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(t["btn_add_ch"], callback_data="adm:add_fsub"),
             InlineKeyboardButton(t["btn_del_ch"], callback_data="adm:del_fsub_menu")],
            [InlineKeyboardButton(t["btn_pm_scope"].format(state=pm_state), callback_data="adm:tog_pm"),
             InlineKeyboardButton(t["btn_grp_scope"].format(state=grp_state), callback_data="adm:tog_group")],
            [InlineKeyboardButton(t["btn_inl_scope"].format(state=inl_state), callback_data="adm:tog_inline")],
            [InlineKeyboardButton(t["btn_back"], callback_data="adm:menu")]
        ])
        await callback.edit_message_text(text, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        return

    if action == "bcast":
        PENDING_ADMIN_ACTION[ADMIN_ID] = "broadcast"
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(t["btn_cancel"], callback_data="adm:menu")]
        ])
        await callback.edit_message_text(t["bcast_prompt"], reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        return

    if action == "fwd":
        PENDING_ADMIN_ACTION[ADMIN_ID] = "forward"
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(t["btn_cancel"], callback_data="adm:menu")]
        ])
        await callback.edit_message_text(t["fwd_prompt"], reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        return

@app.on_message(filters.private & ~filters.command(["start", "lang", "admin"]))
async def admin_pending_inputs(client: Client, message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    action = PENDING_ADMIN_ACTION.get(ADMIN_ID)
    if not action:
        # If it's a URL, let url_handler catch it (url_handler regex matches URLs)
        return

    admin_lang = get_user_lang(ADMIN_ID)
    t = ADMIN_TEXTS[admin_lang]

    if action == "add_fsub":
        PENDING_ADMIN_ACTION.pop(ADMIN_ID, None)
        raw_input = message.text.strip() if message.text else ""
        if not raw_input.startswith("@"):
            raw_input = f"@{raw_input}"
        added = add_force_channel(raw_input)
        if added:
            await message.reply(t["ch_added"].format(ch=raw_input), parse_mode=enums.ParseMode.HTML)
        else:
            await message.reply(t["ch_exists"].format(ch=raw_input), parse_mode=enums.ParseMode.HTML)
        return

    if action == "broadcast":
        PENDING_ADMIN_ACTION.pop(ADMIN_ID, None)
        users = get_all_users()
        status_msg = await message.reply(t["bcast_start"].format(count=len(users)))
        sent_count = 0
        blocked_count = 0
        for uid in users:
            try:
                await message.copy(chat_id=uid)
                sent_count += 1
                await asyncio.sleep(0.05)
            except Exception:
                blocked_count += 1

        await status_msg.edit_text(
            t["bcast_done"].format(sent=sent_count, blocked=blocked_count),
            parse_mode=enums.ParseMode.HTML
        )
        return

    if action == "forward":
        PENDING_ADMIN_ACTION.pop(ADMIN_ID, None)
        users = get_all_users()
        status_msg = await message.reply(t["fwd_start"].format(count=len(users)))
        sent_count = 0
        blocked_count = 0
        for uid in users:
            try:
                await message.forward(chat_id=uid)
                sent_count += 1
                await asyncio.sleep(0.05)
            except Exception:
                blocked_count += 1

        await status_msg.edit_text(
            t["fwd_done"].format(sent=sent_count, blocked=blocked_count),
            parse_mode=enums.ParseMode.HTML
        )
        return

    if action == "add_ban":
        PENDING_ADMIN_ACTION.pop(ADMIN_ID, None)
        target = message.text.strip() if message.text else ""
        if not target:
            return
        clean_target = target.lstrip("@")
        if clean_target == str(ADMIN_ID):
            await message.reply(
                "⚠️ امکان مسدود کردن ادمین ربات وجود ندارد!" if admin_lang == "fa" else "⚠️ You cannot ban the bot admin!",
                parse_mode=enums.ParseMode.HTML
            )
            return
        uid_to_ban = None
        uname_to_ban = ""
        if clean_target.isdigit():
            uid_to_ban = int(clean_target)
        else:
            uname_to_ban = clean_target
            # try to resolve via get_users
            try:
                u_obj = await client.get_users(clean_target)
                if u_obj:
                    uid_to_ban = u_obj.id
                    uname_to_ban = u_obj.username or clean_target
            except Exception:
                pass

        if uid_to_ban:
            if uid_to_ban == ADMIN_ID:
                await message.reply(
                    "⚠️ امکان مسدود کردن ادمین ربات وجود ندارد!" if admin_lang == "fa" else "⚠️ You cannot ban the bot admin!",
                    parse_mode=enums.ParseMode.HTML
                )
                return
            ban_user(uid_to_ban, uname_to_ban, reason="Admin Manual Ban")
            await message.reply(
                f"✅ کاربر <code>{uid_to_ban}</code> (@{uname_to_ban or 'ندارد'}) با موفقیت مسدود شد." if admin_lang == "fa" else f"✅ User <code>{uid_to_ban}</code> (@{uname_to_ban or 'none'}) was banned successfully.",
                parse_mode=enums.ParseMode.HTML
            )
            # Instantly notify the banned user in PM
            try:
                target_lang = get_user_lang(uid_to_ban)
                await client.send_message(
                    uid_to_ban,
                    MESSAGES[target_lang]["banned_alert"],
                    reply_markup=get_banned_markup(target_lang),
                    parse_mode=enums.ParseMode.HTML
                )
            except Exception as e:
                logger.debug(f"Could not send instant ban notification to {uid_to_ban}: {e}")
        else:
            await message.reply(
                "❌ شناسه کاربر نامعتبر است یا پیدا نشد." if admin_lang == "fa" else "❌ Invalid user ID or could not resolve username.",
                parse_mode=enums.ParseMode.HTML
            )
        return

    if action == "rm_ban":
        PENDING_ADMIN_ACTION.pop(ADMIN_ID, None)
        target = message.text.strip() if message.text else ""
        if not target:
            return
        res = unban_user(target)
        if res:
            await message.reply(
                f"✅ کاربر <code>{target}</code> با موفقیت رفع مسدودیت شد." if admin_lang == "fa" else f"✅ User <code>{target}</code> was unbanned successfully.",
                parse_mode=enums.ParseMode.HTML
            )
        else:
            await message.reply(
                "⚠️ این کاربر در لیست مسدودشدگان یافت نشد." if admin_lang == "fa" else "⚠️ User not found in banned list.",
                parse_mode=enums.ParseMode.HTML
            )
        return

async def register_bot_commands(client: Client):
    """Registers official bot commands in Telegram for both PM and Groups."""
    try:
        # Commands for Private Chats (PM)
        pm_commands = [
            BotCommand("start", "🚀 Start bot / انتخاب زبان"),
            BotCommand("help", "📖 User Guide & Platforms / راهنمای کامل"),
            BotCommand("stop", "🛑 Cancel active download / لغو دانلود جاری"),
            BotCommand("lang", "🌐 Switch language / تغییر زبان"),
            BotCommand("report", "🛠 Contact developer / ارتباط با پشتیبانی")
        ]
        await client.set_bot_commands(pm_commands, scope=BotCommandScopeAllPrivateChats())

        # Commands for Group Chats
        group_commands = [
            BotCommand("help", "📖 Bot Guide / راهنمای ربات"),
            BotCommand("stop", "🛑 Cancel your download in group / لغو دانلود خودتان"),
            BotCommand("lang", "🌐 Group language / تنظیم زبان گروه"),
            BotCommand("report", "🛠 Support / ارتباط با پشتیبانی")
        ]
        await client.set_bot_commands(group_commands, scope=BotCommandScopeAllGroupChats())

        logger.info("Official bot commands registered successfully in Telegram!")
    except Exception as e:
        logger.warning(f"Could not register bot commands: {e}")

def main():
    logger.info("Bot is starting via Pyrogram (MTProto)...")
    async def _start_and_register():
        await app.start()
        await register_bot_commands(app)
        logger.info("Bot is running and ready for messages!")
        await idle()
        await app.stop()

    try:
        app.run(_start_and_register())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")

if __name__ == "__main__":
    main()
