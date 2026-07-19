import asyncio
import logging
import sqlite3
import os
import datetime
import asyncio
import logging
import sqlite3
import os
import re
import aiohttp
import aiofiles
import tempfile
import shutil
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InlineQuery,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InlineQueryResultCachedGif,
    InlineQueryResultCachedVoice,
    FSInputFile,
)

try:
    from keep_alive import keep_alive  # برای Replit یا محیط مشابه
except ImportError:
    # روی Railway/سرورهای دیگر keep_alive لازم نیست
    def keep_alive():
        pass

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "123456789"))

# مسیر دیتابیس: اگر متغیر محیطی DB_PATH ست باشد (مثلاً روی Volume ماندگار Railway
# مثل /data/bot_data.db) از آن استفاده می‌شود، وگرنه فایل محلی. این‌طور با هر
# redeploy روی Railway دیتابیس پاک نمی‌شود.
DB = os.getenv("DB_PATH", "bot_data.db")

# ساخت پوشه‌ی مقصد در صورت نیاز (مثلاً /data یا /botdata)
_db_dir = os.path.dirname(DB)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)

logging.basicConfig(level=logging.INFO)

# اتصال به دیتابیس
db = sqlite3.connect(DB, check_same_thread=False)
cur = db.cursor()


# اطمینان از وجود ستون last_used
def ensure_last_used_column():
    cur.execute("PRAGMA table_info(memes)")
    cols = [r[1] for r in cur.fetchall()]
    if "last_used" not in cols:
        try:
            cur.execute("ALTER TABLE memes ADD COLUMN last_used TEXT")
            db.commit()
        except Exception:
            pass


ensure_last_used_column()

# ================= DB INIT =================
cur.executescript("""
CREATE TABLE IF NOT EXISTS allowed_users(
    user_id INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS admins(
    user_id INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS categories(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS memes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER,
    file_id TEXT,
    file_type TEXT,
    caption TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    added_by INTEGER,
    added_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_used TEXT
);

CREATE TABLE IF NOT EXISTS config(
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS user_meme_usage(
    user_id INTEGER,
    meme_id INTEGER,
    used_at TEXT,
    PRIMARY KEY (user_id, meme_id)
);
""")

cur.execute(
    "INSERT OR IGNORE INTO admins (user_id, added_by) VALUES (?, ?)",
    (OWNER_ID, OWNER_ID),
)
db.commit()

for c in []:
    cur.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", (c,))
db.commit()


# ================= HELPERS =================
def get_config(key, default=""):
    cur.execute("SELECT value FROM config WHERE key=?", (key,))
    r = cur.fetchone()
    return r[0] if r else default


def set_config(key, value):
    cur.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (key, value))
    db.commit()


def is_admin(user_id: int) -> bool:
    cur.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
    return bool(cur.fetchone())


def is_allowed(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    cur.execute("SELECT 1 FROM allowed_users WHERE user_id = ?", (user_id,))
    return bool(cur.fetchone())


def update_last_used(mid: int):
    ts = datetime.datetime.utcnow().isoformat()
    cur.execute("UPDATE memes SET last_used = ? WHERE id = ?", (ts, mid))
    db.commit()


def update_user_meme_usage(user_id: int, mid: int):
    """ثبت استفاده‌ی یک کاربر مشخص از یک میم (برای مرتب‌سازی شخصی inline)"""
    ts = datetime.datetime.utcnow().isoformat()
    cur.execute(
        "INSERT OR REPLACE INTO user_meme_usage (user_id, meme_id, used_at) VALUES (?, ?, ?)",
        (user_id, mid, ts),
    )
    db.commit()


# ================= KEYBOARDS =================
def normal_kb(is_admin_user=False):
    kb = [
        [KeyboardButton(text="🎲 گیف تصادفی")],
        [KeyboardButton(text="🎙 ویس تصادفی")],
        [KeyboardButton(text="🎥 ویدیو تصادفی")],
        [KeyboardButton(text="🔍 جستجو")],
        [KeyboardButton(text="📜 آخرین میم‌ها")],
    ]
    if is_admin_user:
        kb.append([KeyboardButton(text="🛠 پنل ادمین")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def admin_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ کاربر عادی"), KeyboardButton(text="🗑 حذف کاربر")],
            [KeyboardButton(text="🛡️ ادمین جدید"), KeyboardButton(text="🗑 حذف ادمین")],
            [
                KeyboardButton(text="📤 افزودن میم"),
                KeyboardButton(text="🗂 مدیریت میم‌ها"),
            ],
            [KeyboardButton(text="🗑 حذف میم"), KeyboardButton(text="📦 بکاپ دیتابیس")],
            [
                KeyboardButton(text="📋 لیست کاربران"),
                KeyboardButton(text="📋 لیست ادمین‌ها"),
            ],
            [
                KeyboardButton(text="✏️ تغییر متن استارت"),
                KeyboardButton(text="⬇️ دانلودر"),
            ],
            [KeyboardButton(text="🔙 برگشت به معمولی")],
        ],
        resize_keyboard=True,
    )


def manage_categories_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ دسته"), KeyboardButton(text="➖ دسته")],
            [KeyboardButton(text="📂 لیست دسته‌ها"), KeyboardButton(text="🔙 برگشت")],
        ],
        resize_keyboard=True,
    )


def manage_memes_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📋 تمام میم‌ها"),
                KeyboardButton(text="🔎 جستجوی میم برای ویرایش"),
            ],
            [KeyboardButton(text="🗃 مدیریت دسته‌بندی"), KeyboardButton(text="🔙 برگشت")],
        ],
        resize_keyboard=True,
    )


def back_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔙 برگشت")]], resize_keyboard=True
    )


def downloader_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎬 دانلود یوتیوب")],
            [KeyboardButton(text="📸 دانلود اینستاگرام")],
            [KeyboardButton(text="🔎 جستجو در Deezer")],
            [KeyboardButton(text="🔎 جستجو در Spotify")],
            [KeyboardButton(text="🔎 جستجو در SoundCloud")],
            [KeyboardButton(text="🔙 برگشت به ادمین")],
        ],
        resize_keyboard=True,
    )


# ================= BOT =================
# ---- سقف حجم آپلود ----
# با Bot API عمومی تلگرام سقف آپلود ربات‌ها ۵۰MB است.
# اگر یک «Local Bot API Server» راه انداخته باشی و آدرسش را در متغیر محیطی
# TG_API_URL بگذاری، سقف به ۲GB افزایش می‌یابد.
LOCAL_API_URL = os.getenv("TG_API_URL", "").strip()

if LOCAL_API_URL:
    # نرمال‌سازی آدرس: اگر http:// و پورت را ننوشته باشی، خودمان اضافه می‌کنیم
    if not LOCAL_API_URL.startswith(("http://", "https://")):
        LOCAL_API_URL = "http://" + LOCAL_API_URL
    # اگر پورتی مشخص نشده، پیش‌فرض 8081 (پورت سرور Bot API محلی)
    _after_scheme = LOCAL_API_URL.split("://", 1)[1]
    if ":" not in _after_scheme.split("/", 1)[0]:
        LOCAL_API_URL = LOCAL_API_URL.rstrip("/") + ":8081"

    # با سرور محلی سقف ۲ گیگابایت است
    MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024
    MAX_UPLOAD_LABEL = "2GB"
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import TelegramAPIServer

    _api_server = TelegramAPIServer.from_base(LOCAL_API_URL)
    _session = AiohttpSession(api=_api_server)
    bot = Bot(BOT_TOKEN, session=_session)
    logging.info(f"استفاده از Local Bot API Server: {LOCAL_API_URL} (سقف 2GB)")
else:
    # Bot API عمومی: سقف ۵۰ مگابایت
    MAX_UPLOAD_SIZE = 50 * 1024 * 1024
    MAX_UPLOAD_LABEL = "50MB"
    bot = Bot(BOT_TOKEN)

dp = Dispatcher()
states = {}
context_data = {}


# ================= START =================
@dp.message(CommandStart())
async def start(msg: types.Message):
    text = get_config("start_text", "👋 به ربات میم خوش اومدی!\n\nاز دکمه‌ها استفاده کن")
    kb = normal_kb(is_admin(msg.from_user.id))
    await msg.answer(text, reply_markup=kb)


# ================= BACK BUTTON =================
@dp.message(F.text == "🔙 برگشت")
async def go_back(msg: types.Message):
    states.pop(msg.from_user.id, None)
    context_data.pop(msg.from_user.id, None)
    if is_admin(msg.from_user.id):
        await msg.answer("برگشتی به پنل ادمین.", reply_markup=admin_kb())
    else:
        await msg.answer(
            "برگشتی به منوی معمولی.", reply_markup=normal_kb(is_admin(msg.from_user.id))
        )


@dp.message(F.text == "🔙 برگشت به معمولی")
async def back_to_normal(msg: types.Message):
    await msg.answer(
        "برگشت به منوی معمولی", reply_markup=normal_kb(is_admin(msg.from_user.id))
    )


# ================= تغییر متن استارت =================
@dp.message(F.text == "✏️ تغییر متن استارت")
async def change_start_text(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("دسترسی ندارید!")
    states[msg.from_user.id] = "change_start"
    await msg.answer("متن جدید استارت را ارسال کنید:", reply_markup=back_kb())


@dp.message(F.text, lambda m: states.get(m.from_user.id) == "change_start")
async def change_start_finish(msg: types.Message):
    set_config("start_text", msg.text)
    states.pop(msg.from_user.id, None)
    await msg.answer("متن استارت تغییر کرد.", reply_markup=admin_kb())


# ================= ADMIN PANEL =================
@dp.message(F.text == "🛠 پنل ادمین")
async def open_admin_panel(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("دسترسی ندارید!")
    await msg.answer("پنل مدیریت", reply_markup=admin_kb())


# ================= مدیریت میم‌ها (منوی جدید) =================
@dp.message(F.text == "🗂 مدیریت میم‌ها")
async def manage_menu(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("دسترسی ندارید!")
    await msg.answer(
        "مدیریت میم‌ها: یکی از گزینه‌ها را انتخاب کنید.", reply_markup=manage_memes_kb()
    )


# ================= مدیریت دسته‌بندی (دکمه جدید در منوی مدیریت میم‌ها) =================
@dp.message(F.text == "🗃 مدیریت دسته‌بندی")
async def manage_categories_menu(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("دسترسی ندارید!")
    await msg.answer(
        "مدیریت دسته‌بندی‌ها: یکی از گزینه‌ها را انتخاب کنید.",
        reply_markup=manage_categories_kb(),
    )


# ================= RANDOM MEMES =================
@dp.message(F.text == "🎲 گیف تصادفی")
async def random_gif(msg: types.Message):
    if not is_allowed(msg.from_user.id):
        return await msg.answer("شما اجازه استفاده ندارید.")
    cur.execute(
        "SELECT file_id, caption FROM memes WHERE file_type = 'animation' ORDER BY RANDOM() LIMIT 1"
    )
    row = cur.fetchone()
    if row:
        await msg.answer_animation(row[0], caption=row[1] or "")
    else:
        await msg.answer("هیچ گیفی موجود نیست!")


@dp.message(F.text == "🎙 ویس تصادفی")
async def random_voice(msg: types.Message):
    if not is_allowed(msg.from_user.id):
        return await msg.answer("شما اجازه استفاده ندارید.")
    cur.execute(
        "SELECT file_id, caption FROM memes WHERE file_type = 'voice' ORDER BY RANDOM() LIMIT 1"
    )
    row = cur.fetchone()
    if row:
        await msg.answer_voice(row[0], caption=row[1] or "")
    else:
        await msg.answer("هیچ ویسی موجود نیست!")


@dp.message(F.text == "🎥 ویدیو تصادفی")
async def random_video(msg: types.Message):
    if not is_allowed(msg.from_user.id):
        return await msg.answer("شما اجازه استفاده ندارید.")
    cur.execute(
        "SELECT file_id, caption FROM memes WHERE file_type = 'video' ORDER BY RANDOM() LIMIT 1"
    )
    row = cur.fetchone()
    if row:
        await msg.answer_video(row[0], caption=row[1] or "")
    else:
        await msg.answer("هیچ ویدیویی موجود نیست!")


# ================= SEARCH BUTTON TEXT (INLINE SWITCH) =================
@dp.message(F.text == "🔍 جستجو")
async def search_prompt(msg: types.Message):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔎 شروع جستجو",
                    switch_inline_query="",  # باز شدن انتخاب چت‌ها و شروع اینلاین مود
                )
            ]
        ]
    )
    await msg.answer("برای جستجو یکی از گزینه‌های زیر را انتخاب کن:", reply_markup=kb)


# ================= INLINE MODE (فیلتر دسته؛ نمایش اسم به جای تگ) =================
@dp.inline_query()
async def inline_search(query: InlineQuery):
    user_id = query.from_user.id
    logging.info(f"inline_query از {user_id}: {query.query!r}")
    if not is_allowed(user_id):
        return await query.answer(
            [],
            switch_pm_text="شما اجازه استفاده ندارید",
            switch_pm_parameter="start",
            cache_time=1,
            is_personal=True,
        )

    q = query.query.strip()
    category_filter = None
    term = ""
    if q.lower().startswith("cat:"):
        parts = q.split(" ", 1)
        cat_part = parts[0][4:].strip()
        category_filter = cat_part
        term = parts[1].strip() if len(parts) > 1 else ""
    else:
        term = q

    rows = []
    # تضمین وجود جدول استفاده‌ی شخصی (مثلاً بعد از ریستور دیتابیسی که این جدول را ندارد)
    cur.execute(
        "CREATE TABLE IF NOT EXISTS user_meme_usage("
        "user_id INTEGER, meme_id INTEGER, used_at TEXT, "
        "PRIMARY KEY (user_id, meme_id))"
    )
    # با LEFT JOIN استفاده‌ی شخصی هر کاربر را می‌آوریم و اول بر اساس آن مرتب می‌کنیم،
    # سپس بر اساس last_used سراسری. این باعث می‌شود میم‌هایی که خودِ کاربر
    # اخیراً استفاده کرده در صدر نتایج inline او بیایند.
    order_clause = (
        "ORDER BY (u.used_at IS NULL), u.used_at DESC, "
        "COALESCE(m.last_used, m.added_at) DESC LIMIT 50"
    )
    base_select = (
        "SELECT m.id, m.file_id, m.file_type, m.caption "
        "FROM memes m "
        "LEFT JOIN user_meme_usage u ON u.meme_id = m.id AND u.user_id = ? "
    )
    if category_filter:
        cur.execute("SELECT id FROM categories WHERE name = ?", (category_filter,))
        r = cur.fetchone()
        if r:
            cat_id = r[0]
            if term:
                like = f"%{term}%"
                cur.execute(
                    base_select
                    + "WHERE m.category_id = ? AND (m.tags LIKE ? OR m.caption LIKE ?) "
                    + order_clause,
                    (user_id, cat_id, like, like),
                )
            else:
                cur.execute(
                    base_select + "WHERE m.category_id = ? " + order_clause,
                    (user_id, cat_id),
                )
        else:
            rows = []
    else:
        if not term:
            cur.execute(base_select + order_clause, (user_id,))
        else:
            like = f"%{term}%"
            cur.execute(
                base_select
                + "WHERE m.tags LIKE ? OR m.caption LIKE ? "
                + order_clause,
                (user_id, like, like),
            )

    rows = cur.fetchall()
    results = []
    for mid, file_id, file_type, caption in rows:
        rid = str(mid)
        if file_type == "photo":
            results.append(
                InlineQueryResultCachedPhoto(
                    id=rid, photo_file_id=file_id, caption=caption or ""
                )
            )
        elif file_type == "animation":
            results.append(
                InlineQueryResultCachedGif(
                    id=rid, gif_file_id=file_id, caption=caption or ""
                )
            )
        elif file_type == "video":
            results.append(
                InlineQueryResultCachedVideo(
                    id=rid,
                    video_file_id=file_id,
                    title=caption or "ویدیو",
                    caption=caption or "",
                )
            )
        elif file_type == "voice":
            results.append(
                InlineQueryResultCachedVoice(
                    id=rid,
                    voice_file_id=file_id,
                    title=caption or "ویس",
                    caption=caption or "",
                )
            )

    try:
        await query.answer(results, cache_time=1, is_personal=True)
    except Exception as e:
        logging.exception("inline answer error")
        try:
            await query.answer([], cache_time=1, is_personal=True)
        except Exception:
            pass


# ثبت انتخاب inline: وقتی کاربر یک میم را از حالت inline انتخاب و ارسال می‌کند،
# آن میم برای همان کاربر به صدر نتایج بعدی می‌آید.
@dp.chosen_inline_result()
async def on_chosen_inline(chosen: types.ChosenInlineResult):
    try:
        mid = int(chosen.result_id)
    except (TypeError, ValueError):
        return
    user_id = chosen.from_user.id
    update_user_meme_usage(user_id, mid)
    update_last_used(mid)


# ================= لیست آخرین میم‌ها (نمایش اسم بدون تگ) =================
@dp.message(F.text == "📜 آخرین میم‌ها")
async def last_memes_list(msg: types.Message):
    if not is_allowed(msg.from_user.id):
        return await msg.answer("شما اجازه استفاده ندارید.")

    LIMIT = 30
    cur.execute(
        "SELECT id, file_type, caption FROM memes ORDER BY COALESCE(last_used, added_at) DESC LIMIT ?",
        (LIMIT,),
    )
    memes = cur.fetchall()

    if not memes:
        return await msg.answer("هیچ میمی وجود ندارد.")

    rows = []
    for mid, ftype, caption in memes:
        short = (caption[:40] + "...") if caption else "بدون اسم"
        label = f"ID {mid} | {ftype} | {short}"
        if is_admin(msg.from_user.id):
            btn_preview = InlineKeyboardButton(
                text=label, callback_data=f"preview_meme:{mid}"
            )
            rows.append([btn_preview])
        else:
            btn_send = InlineKeyboardButton(text=label, callback_data=f"sendm:{mid}")
            rows.append([btn_send])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await msg.answer("📜 آخرین میم‌های استفاده‌شده:", reply_markup=kb)


# ================= پیش‌نمایش برای ادمین =================
@dp.callback_query(F.data.startswith("preview_meme:"))
async def preview_meme_callback(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید.", show_alert=True)
    try:
        mid = int(callback.data.split(":")[1])
    except:
        return await callback.answer("شناسه نامعتبر.", show_alert=True)

    cur.execute("SELECT file_id, file_type, caption FROM memes WHERE id = ?", (mid,))
    row = cur.fetchone()
    if not row:
        return await callback.answer("میم پیدا نشد.", show_alert=True)

    file_id, file_type, caption = row
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛠 مدیریت این میم", callback_data=f"manage_{mid}"
                )
            ],
            [InlineKeyboardButton(text="🔙 بستن", callback_data="close_preview")],
        ]
    )

    try:
        if file_type == "photo":
            await callback.message.reply_photo(
                photo=file_id, caption=caption or "", reply_markup=kb
            )
        elif file_type == "animation":
            await callback.message.reply_animation(
                animation=file_id, caption=caption or "", reply_markup=kb
            )
        elif file_type == "video":
            await callback.message.reply_video(
                video=file_id, caption=caption or "", reply_markup=kb
            )
        elif file_type == "voice":
            await callback.message.reply_voice(
                voice=file_id, caption=caption or "", reply_markup=kb
            )
        else:
            return await callback.answer("نوع فایل پشتیبانی نمی‌شود.", show_alert=True)
    except Exception:
        return await callback.answer("خطا در ارسال پیش‌نمایش.", show_alert=True)

    await callback.answer()


@dp.callback_query(F.data == "close_preview")
async def close_preview(callback: types.CallbackQuery):
    try:
        await callback.message.delete()
    except:
        pass
    await callback.answer()


# ================= ارسال میم برای کاربر عادی (از لیست) =================
@dp.callback_query(F.data.startswith("sendm:"))
async def send_meme_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if not is_allowed(user_id):
        return await callback.answer("شما اجازه استفاده ندارید.", show_alert=True)
    try:
        mid = int(callback.data.split(":")[1])
    except:
        return await callback.answer("شناسه نامعتبر است.", show_alert=True)

    cur.execute("SELECT file_id, file_type, caption FROM memes WHERE id = ?", (mid,))
    row = cur.fetchone()
    if not row:
        return await callback.answer("میم پیدا نشد.", show_alert=True)

    file_id, file_type, caption = row
    try:
        if file_type == "photo":
            await callback.message.reply_photo(photo=file_id, caption=caption or "")
        elif file_type == "animation":
            await callback.message.reply_animation(
                animation=file_id, caption=caption or ""
            )
        elif file_type == "video":
            await callback.message.reply_video(video=file_id, caption=caption or "")
        elif file_type == "voice":
            await callback.message.reply_voice(voice=file_id, caption=caption or "")
        else:
            return await callback.answer("نوع فایل پشتیبانی نمی‌شود.", show_alert=True)
    except Exception:
        return await callback.answer("ارسال میم با خطا مواجه شد.", show_alert=True)

    update_last_used(mid)
    update_user_meme_usage(user_id, mid)
    await callback.answer()


# ================= حذف میم (تأیید دو مرحله‌ای) =================
@dp.message(F.text == "🗑 حذف میم")
async def delete_meme_start(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("فقط ادمین می‌تواند میم حذف کند.")
    LIMIT = 50
    cur.execute(
        "SELECT id, file_type, caption FROM memes ORDER BY id DESC LIMIT ?", (LIMIT,)
    )
    memes = cur.fetchall()
    if not memes:
        return await msg.answer("هیچ میمی برای حذف وجود ندارد.")
    rows = []
    for mid, ftype, caption in memes:
        short = (caption[:40] + "...") if caption else "بدون اسم"
        label = f"ID {mid} | {ftype} | {short}"
        # دکمه‌ای که مرحله اول: نمایش گزینه حذف (و سپس تأیید)
        btn_delete = InlineKeyboardButton(
            text=f"🗑 حذف — {label}", callback_data=f"ask_delm:{mid}"
        )
        rows.append([btn_delete])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await msg.answer(
        "میم مورد نظر را برای حذف انتخاب کنید (تأیید لازم است):", reply_markup=kb
    )


@dp.callback_query(F.data.startswith("ask_delm:"))
async def ask_delete_meme_callback(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید.", show_alert=True)
    try:
        mid = int(callback.data.split(":")[1])
    except:
        return await callback.answer("شناسه نامعتبر.", show_alert=True)

    cur.execute("SELECT file_type, caption FROM memes WHERE id = ?", (mid,))
    row = cur.fetchone()
    if not row:
        return await callback.answer(
            "این میم وجود ندارد یا قبلاً حذف شده.", show_alert=True
        )

    ftype, caption = row
    short = (caption[:100] + "...") if caption else "بدون اسم"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ تأیید حذف", callback_data=f"confirm_delm:{mid}"
                ),
                InlineKeyboardButton(text="❌ لغو", callback_data=f"cancel_delm:{mid}"),
            ]
        ]
    )
    # پیام تأیید را ارسال یا ویرایش کن
    try:
        await callback.message.answer(
            f"آیا مطمئن هستید می‌خواهید میم زیر را حذف کنید?\n\nID: {mid}\nنوع: {ftype}\nاسم: {short}",
            reply_markup=kb,
        )
    except:
        await callback.answer("خطا در نمایش پنجره تأیید.", show_alert=True)
    await callback.answer()


@dp.callback_query(F.data.startswith("confirm_delm:"))
async def confirm_delete_meme_callback(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید.", show_alert=True)
    try:
        mid = int(callback.data.split(":")[1])
    except:
        return await callback.answer("شناسه نامعتبر.", show_alert=True)

    cur.execute("SELECT 1 FROM memes WHERE id = ?", (mid,))
    if not cur.fetchone():
        return await callback.answer(
            "این میم وجود ندارد یا قبلاً حذف شده.", show_alert=True
        )

    cur.execute("DELETE FROM memes WHERE id = ?", (mid,))
    db.commit()
    await callback.answer("میم حذف شد.", show_alert=True)
    try:
        # تلاش برای حذف پیام تأیید (اگر ممکن است)
        await callback.message.delete()
    except:
        pass


@dp.callback_query(F.data.startswith("cancel_delm:"))
async def cancel_delete_meme_callback(callback: types.CallbackQuery):
    try:
        await callback.answer("حذف لغو شد.", show_alert=True)
        try:
            await callback.message.delete()
        except:
            pass
    except:
        pass


# ================= نمایش و ویرایش تمام میم‌ها =================
@dp.message(F.text == "📋 تمام میم‌ها")
async def all_memes_list(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("دسترسی ندارید.")
    cur.execute("SELECT id, file_type, caption FROM memes ORDER BY id DESC")
    memes = cur.fetchall()
    if not memes:
        return await msg.answer("هیچ میمی وجود ندارد.")
    rows = []
    for mid, ftype, caption in memes:
        short = (caption[:40] + "...") if caption else "بدون اسم"
        label = f"ID {mid} | {ftype} | {short}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"manage_{mid}")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await msg.answer("📋 تمام میم‌ها (برای ویرایش روی هر مورد بزنید):", reply_markup=kb)


# ================= مدیریت یک میم (منوی ویرایش) =================
@dp.callback_query(F.data.startswith("manage_"))
async def manage_meme_callback(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید.", show_alert=True)
    try:
        mid = int(callback.data.split("_")[1])
    except:
        return await callback.answer("شناسه نامعتبر.", show_alert=True)
    cur.execute("SELECT id, caption, tags, category_id FROM memes WHERE id = ?", (mid,))
    row = cur.fetchone()
    if not row:
        return await callback.answer("میم پیدا نشد.", show_alert=True)
    mid, caption, tags, cat_id = row
    cur.execute("SELECT name FROM categories WHERE id = ?", (cat_id,))
    cat_row = cur.fetchone()
    cat_name = cat_row[0] if cat_row else "بدون دسته"
    text = f"میم ID: {mid}\nاسم: {caption or 'بدون اسم'}\nتگ‌ها: {tags or 'ندارد'}\nدسته: {cat_name}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ ویرایش اسم", callback_data=f"edit_name:{mid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏷 ویرایش تگ‌ها", callback_data=f"edit_tags:{mid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📂 تغییر دسته", callback_data=f"edit_cat:{mid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 حذف میم (تأیید)", callback_data=f"ask_delm:{mid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🆔 نمایش آی‌دی", callback_data=f"show_id:{mid}"
                )
            ],
            [InlineKeyboardButton(text="🔙 بستن", callback_data="close_preview")],
        ]
    )
    await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


# ================= هندلرهای ویرایش =================
@dp.callback_query(F.data.startswith("edit_name:"))
async def edit_name_start(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید.", show_alert=True)
    try:
        mid = int(callback.data.split(":")[1])
    except:
        return await callback.answer("شناسه نامعتبر.", show_alert=True)
    states[callback.from_user.id] = "editing_name"
    context_data[callback.from_user.id] = {"mid": mid}
    await callback.message.answer("اسم جدید را ارسال کنید:", reply_markup=back_kb())
    await callback.answer()


@dp.callback_query(F.data.startswith("edit_tags:"))
async def edit_tags_start(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید.", show_alert=True)
    try:
        mid = int(callback.data.split(":")[1])
    except:
        return await callback.answer("شناسه نامعتبر.", show_alert=True)
    states[callback.from_user.id] = "editing_tags"
    context_data[callback.from_user.id] = {"mid": mid}
    await callback.message.answer(
        "تگ‌های جدید را ارسال کنید (با فاصله جدا کنید):", reply_markup=back_kb()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("edit_cat:"))
async def edit_cat_start(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید.", show_alert=True)
    try:
        mid = int(callback.data.split(":")[1])
    except:
        return await callback.answer("شناسه نامعتبر.", show_alert=True)
    cur.execute("SELECT id, name FROM categories ORDER BY name")
    cats = cur.fetchall()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=name, callback_data=f"set_cat:{mid}:{cid}")]
            for cid, name in [(c[0], c[1]) for c in cats]
        ]
    )
    await callback.message.answer("دسته جدید را انتخاب کنید:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("set_cat:"))
async def set_cat_callback(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید.", show_alert=True)
    parts = callback.data.split(":")
    if len(parts) != 3:
        return await callback.answer("داده نامعتبر.", show_alert=True)
    try:
        mid = int(parts[1])
        cid = int(parts[2])
    except:
        return await callback.answer("شناسه نامعتبر.", show_alert=True)
    cur.execute("UPDATE memes SET category_id = ? WHERE id = ?", (cid, mid))
    db.commit()
    await callback.answer("دسته میم تغییر کرد.", show_alert=True)
    try:
        await callback.message.delete()
    except:
        pass


@dp.callback_query(F.data.startswith("show_id:"))
async def show_id_callback(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید.", show_alert=True)
    try:
        mid = int(callback.data.split(":")[1])
    except:
        return await callback.answer("شناسه نامعتبر.", show_alert=True)
    await callback.answer(f"ID میم: {mid}", show_alert=True)


@dp.message(lambda m: states.get(m.from_user.id) == "editing_name")
async def finish_edit_name(msg: types.Message):
    data = context_data.get(msg.from_user.id, {})
    mid = data.get("mid")
    if not mid:
        states.pop(msg.from_user.id, None)
        context_data.pop(msg.from_user.id, None)
        return await msg.answer("خطا؛ دوباره تلاش کنید.", reply_markup=admin_kb())
    new_name = msg.text.strip()
    if not new_name:
        return await msg.answer("اسم نمی‌تواند خالی باشد.", reply_markup=back_kb())
    cur.execute("UPDATE memes SET caption = ? WHERE id = ?", (new_name, mid))
    db.commit()
    states.pop(msg.from_user.id, None)
    context_data.pop(msg.from_user.id, None)
    await msg.answer("اسم میم به‌روزرسانی شد.", reply_markup=admin_kb())


@dp.message(lambda m: states.get(m.from_user.id) == "editing_tags")
async def finish_edit_tags(msg: types.Message):
    data = context_data.get(msg.from_user.id, {})
    mid = data.get("mid")
    if not mid:
        states.pop(msg.from_user.id, None)
        context_data.pop(msg.from_user.id, None)
        return await msg.answer("خطا؛ دوباره تلاش کنید.", reply_markup=admin_kb())
    new_tags = msg.text.strip()
    cur.execute("UPDATE memes SET tags = ? WHERE id = ?", (new_tags, mid))
    db.commit()
    states.pop(msg.from_user.id, None)
    context_data.pop(msg.from_user.id, None)
    await msg.answer("تگ‌های میم به‌روزرسانی شد.", reply_markup=admin_kb())


# ================= لیست دسته‌ها → نمایش میم‌های داخل هر دسته =================
@dp.message(F.text == "📂 لیست دسته‌ها")
async def list_categories(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("فقط ادمین می‌تواند این بخش را ببیند.")
    cur.execute("SELECT id, name FROM categories ORDER BY id")
    cats = cur.fetchall()
    if not cats:
        return await msg.answer("هیچ دسته‌ای وجود ندارد.")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{cid} — {name}", callback_data=f"cat_info_{cid}"
                )
            ]
            for cid, name in cats
        ]
    )
    await msg.answer("📂 لیست دسته‌ها:", reply_markup=kb)


@dp.callback_query(F.data.startswith("cat_info_"))
async def cat_info_callback(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید", show_alert=True)
    try:
        cid = int(callback.data.split("_")[-1])
    except:
        return await callback.answer("شناسه نامعتبر", show_alert=True)
    cur.execute("SELECT name FROM categories WHERE id = ?", (cid,))
    r = cur.fetchone()
    if not r:
        return await callback.answer("دسته پیدا نشد.", show_alert=True)
    cat_name = r[0]
    cur.execute(
        "SELECT id, file_type, caption FROM memes WHERE category_id = ? ORDER BY COALESCE(last_used, added_at) DESC",
        (cid,),
    )
    memes = cur.fetchall()
    if not memes:
        return await callback.answer(
            f"هیچ میمی در دسته {cat_name} وجود ندارد.", show_alert=True
        )
    rows = []
    for mid, ftype, caption in memes:
        short = (caption[:40] + "...") if caption else "بدون اسم"
        label = f"ID {mid} | {ftype} | {short}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"manage_{mid}")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await callback.message.answer(f"میم‌های دسته {cat_name}:", reply_markup=kb)
    await callback.answer()


# ================= مدیریت دسته‌ها: افزودن و حذف با لیست و callback (تأیید دو مرحله‌ای) =================
@dp.message(F.text == "➕ دسته")
async def add_category_start(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("دسترسی ندارید!")
    states[msg.from_user.id] = "add_category"
    await msg.answer("نام دسته جدید را وارد کنید:", reply_markup=back_kb())


@dp.message(F.text, lambda m: states.get(m.from_user.id) == "add_category")
async def add_category_finish(msg: types.Message):
    name = msg.text.strip()
    if not name:
        return await msg.answer("نام دسته نمی‌تواند خالی باشد.")
    cur.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
    db.commit()
    await msg.answer("دسته اضافه شد.", reply_markup=manage_categories_kb())
    states.pop(msg.from_user.id, None)


@dp.message(F.text == "➖ دسته")
async def remove_category_start(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("دسترسی ندارید!")

    cur.execute("SELECT id, name FROM categories ORDER BY id")
    cats = cur.fetchall()

    if not cats:
        return await msg.answer(
            "هیچ دسته‌ای وجود ندارد.", reply_markup=manage_categories_kb()
        )

    rows = []
    for cid, name in cats:
        # مرحله اول: نمایش دکمه‌ای که پنجره تأیید را باز می‌کند
        rows.append(
            [InlineKeyboardButton(text=f"{name}", callback_data=f"ask_delcat:{cid}")]
        )

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await msg.answer("برای حذف یک دسته، روی آن بزن (تأیید لازم است):", reply_markup=kb)


@dp.callback_query(F.data.startswith("ask_delcat:"))
async def ask_delete_category_callback(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید.", show_alert=True)
    try:
        cid = int(callback.data.split(":")[1])
    except:
        return await callback.answer("شناسه نامعتبر.", show_alert=True)

    cur.execute("SELECT name FROM categories WHERE id = ?", (cid,))
    r = cur.fetchone()
    if not r:
        return await callback.answer(
            "این دسته وجود ندارد یا قبلاً حذف شده.", show_alert=True
        )
    name = r[0]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ تأیید حذف", callback_data=f"confirm_delcat:{cid}"
                ),
                InlineKeyboardButton(
                    text="❌ لغو", callback_data=f"cancel_delcat:{cid}"
                ),
            ]
        ]
    )
    try:
        await callback.message.answer(
            f"آیا مطمئن هستید می‌خواهید دسته '{name}' را حذف کنید؟\nتوجه: میم‌های مرتبط حذف نخواهند شد اما دسته از بین می‌رود.",
            reply_markup=kb,
        )
    except:
        await callback.answer("خطا در نمایش پنجره تأیید.", show_alert=True)
    await callback.answer()


@dp.callback_query(F.data.startswith("confirm_delcat:"))
async def confirm_delete_category_callback(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید.", show_alert=True)
    try:
        cid = int(callback.data.split(":")[1])
    except:
        return await callback.answer("شناسه نامعتبر.", show_alert=True)

    cur.execute("SELECT name FROM categories WHERE id = ?", (cid,))
    r = cur.fetchone()
    if not r:
        return await callback.answer(
            "این دسته وجود ندارد یا قبلاً حذف شده.", show_alert=True
        )
    # حذف دسته
    cur.execute("DELETE FROM categories WHERE id=?", (cid,))
    db.commit()
    await callback.answer("دسته حذف شد.", show_alert=True)
    try:
        await callback.message.delete()
    except:
        pass


@dp.callback_query(F.data.startswith("cancel_delcat:"))
async def cancel_delete_category_callback(callback: types.CallbackQuery):
    try:
        await callback.answer("حذف دسته لغو شد.", show_alert=True)
        try:
            await callback.message.delete()
        except:
            pass
    except:
        pass


# ================= بکاپ دیتابیس =================
@dp.message(F.text == "📦 بکاپ دیتابیس")
async def backup_database(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("فقط ادمین می‌تواند بکاپ بگیرد.")
    ts = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    backup_name = f"backup_{ts}.db"
    try:
        try:
            db.commit()
        except:
            pass
        dest_conn = sqlite3.connect(backup_name)
        with dest_conn:
            db.backup(dest_conn)
        dest_conn.close()
        await msg.answer("در حال آماده‌سازی و ارسال بکاپ دیتابیس...")
        await bot.send_document(
            chat_id=msg.chat.id,
            document=FSInputFile(backup_name),
            caption=f"نسخه پشتیبان دیتابیس ({ts})",
        )
        try:
            os.remove(backup_name)
        except:
            pass
    except Exception as e:
        logging.exception("backup error")
        return await msg.answer(f"خطا در تهیه بکاپ دیتابیس: {e}")


# ================= ریستور دیتابیس =================
# (حذف شد — به دلیل تداخل با سرور محلی Bot API)


# ================= دیتاسنتر (فیلهات و لینک مستقیم) =================
@dp.message(F.text == "🌐 دیتاسنتر")
async def datacenter_menu(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("دسترسی ندارید!")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 فایل بفرست → لینک مستقیم بگیر", callback_data="dc_upload")],
            [InlineKeyboardButton(text="📥 لینک مستقیم بده → فایل بگیر", callback_data="dc_download")],
            [InlineKeyboardButton(text="🔙 بستن", callback_data="dc_close")],
        ]
    )
    await msg.answer(
        "🌐 **دیتاسنتر**\n\n"
        "دو حالت داری:\n\n"
        "**1️⃣ آپلود**: فایل بفرست، ربات لینک مستقیم دانلود (سرور محلی یا سرویس عمومی) رو می‌فرسته.\n"
        "**2️⃣ دانلود**: روی 'لینک بده → فایل بگیر' بزن و لینک مستقیم رو بفرست، ربات فایل رو می‌گیره و برات می‌فرسته.",
        reply_markup=kb,
        parse_mode="Markdown",
    )


@dp.callback_query(F.data == "dc_close")
async def dc_close(callback: types.CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "dc_upload")
async def dc_upload_start(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید", show_alert=True)
    states[callback.from_user.id] = "dc_upload"
    await callback.message.edit_text(
        "📤 **حالت آپلود**\n\n"
        "فایل مورد نظر را ارسال کن. ربات لینک مستقیم دانلودش رو (که می‌تونی چند بار استفاده کنی) برات می‌فرسته.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 بازگشت", callback_data="dc_back")]
            ]
        ),
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.callback_query(F.data == "dc_download")
async def dc_download_start(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید", show_alert=True)
    states[callback.from_user.id] = "dc_download"
    await callback.message.edit_text(
        "📥 **حالت دانلود از لینک**\n\n"
        "لینک مستقیم فایل را بفرست (مثلاً `https://example.com/file.zip`).\n"
        "ربات فایل را از اون آدرس می‌گیره و برایت ارسال می‌کنه.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 بازگشت", callback_data="dc_back")]
            ]
        ),
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.callback_query(F.data == "dc_back")
async def dc_back(callback: types.CallbackQuery):
    states.pop(callback.from_user.id, None)
    await dc_menu_from_callback(callback)
    await callback.answer()


async def dc_menu_from_callback(callback: types.CallbackQuery):
    """نمایش منوی دیتاسنتر از inline callback"""
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 فایل بفرست → لینک مستقیم بگیر", callback_data="dc_upload")],
            [InlineKeyboardButton(text="📥 لینک مستقیم بده → فایل بگیر", callback_data="dc_download")],
            [InlineKeyboardButton(text="🔙 بستن", callback_data="dc_close")],
        ]
    )
    await callback.message.edit_text(
        "🌐 **دیتاسنتر**\n\n"
        "دو حالت داری:\n\n"
        "**1️⃣ آپلود**: فایل بفرست، ربات لینک مستقیم دانلود (سرور محلی یا سرویس عمومی) رو می‌فرسته.\n"
        "**2️⃣ دانلود**: روی 'لینک بده → فایل بگیر' بزن و لینک مستقیم رو بفرست، ربات فایل رو می‌گیره و برات می‌فرسته.",
        reply_markup=kb,
        parse_mode="Markdown",
    )


# 📤 دریافت هر نوع فایل → ساخت لینک مستقیم
@dp.message(F.content_type.in_({"document", "video", "photo", "voice", "animation", "audio", "sticker"}),
           lambda m: states.get(m.from_user.id) == "dc_upload")
async def dc_upload_receive(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return
    states.pop(msg.from_user.id, None)

    file_id = None
    fname_hint = "file"
    if msg.document:
        file_id = msg.document.file_id
        fname_hint = msg.document.file_name or "document"
    elif msg.video:
        file_id = msg.video.file_id
        fname_hint = msg.video.file_name or "video.mp4"
    elif msg.photo:
        file_id = msg.photo[-1].file_id  # بالاترین رزولوشن
        fname_hint = "photo.jpg"
    elif msg.voice:
        file_id = msg.voice.file_id
        fname_hint = "voice.ogg"
    elif msg.animation:
        file_id = msg.animation.file_id
        fname_hint = msg.animation.file_name or "animation.mp4"
    elif msg.audio:
        file_id = msg.audio.file_id
        fname_hint = msg.audio.file_name or "audio.mp3"
    elif msg.sticker:
        file_id = msg.sticker.file_id
        fname_hint = "sticker.webp"

    if not file_id:
        return await msg.answer("❌ فایل معتبری پیدا نشد.", reply_markup=admin_kb())

    # گرفتن file_info برای ساخت URL
    try:
        file_info = await bot.get_file(file_id)
    except Exception as e:
        logging.exception("get_file error")
        return await msg.answer(f"❌ خطا در دریافت اطلاعات فایل: {e}", reply_markup=admin_kb())

    raw_path = (file_info.file_path or "").replace("\\", "/")
    if not raw_path:
        return await msg.answer("❌ file_path خالی برگشت.", reply_markup=admin_kb())

    # ساخت لینک از سرور محلی (اگه فعال باشه) یا عمومی تلگرام
    if BOT_TOKEN in raw_path:
        rel_path = raw_path.split(BOT_TOKEN, 1)[1].lstrip("/")
    else:
        parts = [p for p in raw_path.split("/") if p]
        rel_path = "/".join(parts[-2:]) if len(parts) >= 2 else raw_path.lstrip("/")

    if LOCAL_API_URL:
        direct = f"{LOCAL_API_URL.rstrip('/')}/file/bot{BOT_TOKEN}/{rel_path}"
        kind = "🟢 سرور محلی (Local Bot API)"
    else:
        direct = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{rel_path}"
        kind = "🍎 سرور عمومی تلگرام"

    sz = (file_info.file_size or 0) / (1024 * 1024)
    text = (
        f"✅ **لینک مستقیم فایل:**\n\n"
        f"📄 نام: `{fname_hint}`\n"
        f"📦 حجم: `{sz:.2f} MB`\n"
        f"🔗 لینک: {direct}\n\n"
        f"(**سرویس:** {kind})\n\n"
        f"⚠️ این لینک رو عمومی نکن — مستقیم به حساب رباتت متصله."
    )
    await msg.answer(text, parse_mode="Markdown", reply_markup=admin_kb())


# 📥 دریافت لینک → دانلود فایل و ارسال
@dp.message(F.text, lambda m: states.get(m.from_user.id) == "dc_download")
async def dc_download_url(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return
    url = msg.text.strip()
    states.pop(msg.from_user.id, None)
    if not (url.startswith("http://") or url.startswith("https://")):
        return await msg.answer("❌ باید یه لینک http/https معتبر باشه.", reply_markup=admin_kb())

    wait = await msg.answer("⏳ در حال دانلود...", reply_markup=admin_kb())
    tmp = tempfile.mkdtemp()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url) as resp:
                if resp.status != 200:
                    return await wait.edit_text(f"❌ خطای HTTP {resp.status}")
                # سعی می‌کنیم نام فایل رو از header به‌دست بیاریم
                fname = None
                cd = resp.headers.get("Content-Disposition", "")
                if "filename=" in cd:
                    fname = cd.split("filename=", 1)[1].strip('"').strip("'").split(";")[0]
                if not fname:
                    fname = url.split("/")[-1].split("?")[0] or "downloaded_file"

                # از آخر URL حدس می‌زنیم پسوند
                ext = os.path.splitext(fname)[1] or ""
                fp = os.path.join(tmp, fname if fname else "file" + ext)
                with open(fp, "wb") as f:
                    while True:
                        chunk = await resp.content.read(64 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
        sz = os.path.getsize(fp)
        if sz <= 0:
            return await wait.edit_text("❌ فایل خالی برگشت.", reply_markup=admin_kb())
        if sz > MAX_UPLOAD_SIZE:
            return await wait.edit_text(
                f"❌ حجم فایل {sz/1024/1024:.1f}MB بیش از سقف {MAX_UPLOAD_LABEL} هست!", reply_markup=admin_kb()
            )
        await msg.answer_document(document=FSInputFile(fp, filename=os.path.basename(fp)), caption=f"📥 فایل دانلودی از:\n{url[:200]}")
        await wait.delete()
    except Exception as e:
        logging.exception("dc_download error")
        try:
            await wait.edit_text(f"❌ خطا در دانلود:\n{e}", reply_markup=admin_kb())
        except Exception:
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================= افزودن میم (اسم اجباری، تگ اختیاری، ترتیب: اسم -> تگ -> فایل) =================
@dp.message(F.text == "📤 افزودن میم")
async def add_meme_start(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("دسترسی ندارید!")
    states[msg.from_user.id] = "meme_cat"
    context_data[msg.from_user.id] = {}

    cur.execute("SELECT id, name FROM categories ORDER BY name")
    cats = cur.fetchall()

    # 🔥 کیبورد اینلاین + دکمه برگشت
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=name, callback_data=f"select_cat_{cat_id}")]
            for cat_id, name in cats
        ]
        + [[InlineKeyboardButton(text="🔙 برگشت", callback_data="meme_back_to_admin")]]
    )

    await msg.answer(
        "یکی از دسته‌های موجود را انتخاب کنید یا نام دسته جدید بنویسید:", reply_markup=kb
    )


# 🔥 هندلر دکمه برگشت اینلاین در مرحله انتخاب دسته
@dp.callback_query(F.data == "meme_back_to_admin")
async def meme_back_to_admin(callback: types.CallbackQuery):
    states.pop(callback.from_user.id, None)
    context_data.pop(callback.from_user.id, None)

    try:
        await callback.message.edit_text("افزودن میم لغو شد.", reply_markup=admin_kb())
    except:
        await callback.message.answer("افزودن میم لغو شد.", reply_markup=admin_kb())

    await callback.answer()


@dp.callback_query(F.data.startswith("select_cat_"))
async def select_existing_cat(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید", show_alert=True)

    cat_id = int(callback.data.split("_")[2])
    context_data.setdefault(callback.from_user.id, {})["cat_id"] = cat_id
    states[callback.from_user.id] = "meme_name"

    await callback.message.edit_text("اسم (caption) میم را وارد کنید (الزامی):")
    await callback.answer("دسته انتخاب شد")


@dp.message(F.text, lambda m: states.get(m.from_user.id) == "meme_cat")
async def meme_choose_cat(msg: types.Message):
    cat_name = msg.text.strip()
    if not cat_name:
        return await msg.answer("نام دسته نمی‌تواند خالی باشد.", reply_markup=back_kb())

    cur.execute("SELECT id FROM categories WHERE name = ?", (cat_name,))
    row = cur.fetchone()

    if row:
        cat_id = row[0]
    else:
        cur.execute("INSERT INTO categories (name) VALUES (?)", (cat_name,))
        db.commit()
        cat_id = cur.lastrowid

    context_data.setdefault(msg.from_user.id, {})["cat_id"] = cat_id
    states[msg.from_user.id] = "meme_name"

    await msg.answer("اسم (caption) میم را وارد کنید (الزامی):", reply_markup=back_kb())


@dp.message(F.text, lambda m: states.get(m.from_user.id) == "meme_name")
async def meme_name(msg: types.Message):
    name = msg.text.strip()
    if not name:
        return await msg.answer(
            "اسم اجباری است. لطفاً یک اسم وارد کنید.", reply_markup=back_kb()
        )

    context_data.setdefault(msg.from_user.id, {})["caption"] = name
    states[msg.from_user.id] = "meme_tags"

    await msg.answer(
        "تگ‌ها را وارد کنید (اختیاری، با فاصله جدا کنید).\nاگر نمی‌خواهید تگ بگذارید، 'بدون' بنویسید یا خالی بفرستید.",
        reply_markup=back_kb(),
    )


@dp.message(F.text, lambda m: states.get(m.from_user.id) == "meme_tags")
async def meme_tags(msg: types.Message):
    tags = msg.text.strip()
    if tags.lower() == "بدون":
        tags = ""

    context_data.setdefault(msg.from_user.id, {})["tags"] = tags
    states[msg.from_user.id] = "meme_send"

    await msg.answer(
        "حالا فایل (عکس، گیف، ویدیو یا ویس) را ارسال کنید.\nکپشن داخل فایل نادیده گرفته می‌شود.",
        reply_markup=back_kb(),
    )


@dp.message(
    (F.photo | F.animation | F.video | F.voice),
    lambda m: states.get(m.from_user.id) == "meme_send",
)
async def save_meme(msg: types.Message):
    data = context_data.get(msg.from_user.id)

    if not data or "cat_id" not in data or "caption" not in data:
        await msg.answer("خطا! دوباره شروع کنید.", reply_markup=admin_kb())
        states.pop(msg.from_user.id, None)
        context_data.pop(msg.from_user.id, None)
        return

    # تشخیص نوع فایل
    if msg.photo:
        file_id = msg.photo[-1].file_id
        file_type = "photo"
    elif msg.animation:
        file_id = msg.animation.file_id
        file_type = "animation"
    elif msg.video:
        file_id = msg.video.file_id
        file_type = "video"
    elif msg.voice:
        file_id = msg.voice.file_id
        file_type = "voice"
    else:
        return await msg.answer(
            "فقط عکس، گیف، ویدیو یا ویس قبول می‌شود.", reply_markup=back_kb()
        )

    caption = data.get("caption") or ""
    tags = data.get("tags") or ""

    cur.execute(
        """
        INSERT INTO memes (category_id, file_id, file_type, caption, tags, added_by)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (data["cat_id"], file_id, file_type, caption, tags, msg.from_user.id),
    )
    db.commit()

    states.pop(msg.from_user.id, None)
    context_data.pop(msg.from_user.id, None)

    await msg.answer("✅ میم با موفقیت اضافه شد!", reply_markup=admin_kb())


# ================= ADMIN: افزودن/حذف کاربر و ادمین، لیست کاربران و ادمین‌ها =================
@dp.message(F.text == "➕ کاربر عادی")
async def add_user_start(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("دسترسی ندارید!")
    states[msg.from_user.id] = "add_user"
    await msg.answer("آیدی عددی کاربر را ارسال کنید:", reply_markup=back_kb())


@dp.message(F.text, lambda m: states.get(m.from_user.id) == "add_user")
async def add_user_finish(msg: types.Message):
    try:
        user_id = int(msg.text.strip())
        cur.execute(
            "INSERT OR IGNORE INTO allowed_users (user_id, added_by) VALUES (?,?)",
            (user_id, msg.from_user.id),
        )
        db.commit()
        await msg.answer("کاربر اضافه شد.", reply_markup=admin_kb())
    except:
        await msg.answer("آیدی معتبر نیست.")
    states.pop(msg.from_user.id, None)


@dp.message(F.text == "🗑 حذف کاربر")
async def remove_user_start(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("دسترسی ندارید!")
    states[msg.from_user.id] = "remove_user"
    await msg.answer("آیدی عددی کاربر را ارسال کنید:", reply_markup=back_kb())


@dp.message(F.text, lambda m: states.get(m.from_user.id) == "remove_user")
async def remove_user_finish(msg: types.Message):
    try:
        user_id = int(msg.text.strip())
        cur.execute("DELETE FROM allowed_users WHERE user_id=?", (user_id,))
        db.commit()
        await msg.answer("کاربر حذف شد.", reply_markup=admin_kb())
    except:
        await msg.answer("آیدی معتبر نیست.")
    states.pop(msg.from_user.id, None)


@dp.message(F.text == "🛡️ ادمین جدید")
async def add_admin_start(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("دسترسی ندارید!")
    states[msg.from_user.id] = "add_admin"
    await msg.answer("آیدی عددی ادمین جدید را ارسال کنید:", reply_markup=back_kb())


@dp.message(F.text, lambda m: states.get(m.from_user.id) == "add_admin")
async def add_admin_finish(msg: types.Message):
    try:
        user_id = int(msg.text.strip())
        cur.execute(
            "INSERT OR IGNORE INTO admins (user_id, added_by) VALUES (?,?)",
            (user_id, msg.from_user.id),
        )
        db.commit()
        await msg.answer("ادمین اضافه شد.", reply_markup=admin_kb())
    except:
        await msg.answer("آیدی معتبر نیست.")
    states.pop(msg.from_user.id, None)


@dp.message(F.text == "🗑 حذف ادمین")
async def remove_admin_start(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("دسترسی ندارید!")
    states[msg.from_user.id] = "remove_admin"
    await msg.answer("آیدی عددی ادمین را ارسال کنید:", reply_markup=back_kb())


@dp.message(F.text, lambda m: states.get(m.from_user.id) == "remove_admin")
async def remove_admin_finish(msg: types.Message):
    try:
        user_id = int(msg.text.strip())
        if user_id == OWNER_ID:
            return await msg.answer("نمی‌توان صاحب ربات را حذف کرد.")
        cur.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        db.commit()
        await msg.answer("ادمین حذف شد.", reply_markup=admin_kb())
    except:
        await msg.answer("آیدی معتبر نیست.")
    states.pop(msg.from_user.id, None)


@dp.message(F.text == "📋 لیست کاربران")
async def list_users(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("فقط ادمین می‌تواند این بخش را ببیند.")
    cur.execute("SELECT user_id, added_at FROM allowed_users ORDER BY added_at DESC")
    users = cur.fetchall()
    if not users:
        return await msg.answer("هیچ کاربری ثبت نشده.")
    rows = []
    for uid, added_at in users:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{uid} — {added_at}", callback_data=f"user_info_{uid}"
                ),
                InlineKeyboardButton(text="🗑 حذف", callback_data=f"del_user_{uid}"),
            ]
        )
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await msg.answer("📋 لیست کاربران:", reply_markup=kb)


@dp.callback_query(F.data.startswith("del_user_"))
async def delete_user_callback(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید", show_alert=True)
    user_id = int(callback.data.split("_")[-1])
    cur.execute("DELETE FROM allowed_users WHERE user_id=?", (user_id,))
    db.commit()
    await callback.answer("کاربر حذف شد.", show_alert=True)
    try:
        await callback.message.delete()
    except:
        pass


@dp.message(F.text == "📋 لیست ادمین‌ها")
async def list_admins(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("فقط ادمین می‌تواند این بخش را ببیند.")
    cur.execute("SELECT user_id, added_at FROM admins ORDER BY added_at DESC")
    admins = cur.fetchall()
    if not admins:
        return await msg.answer("هیچ ادمینی ثبت نشده.")
    rows = []
    for uid, added_at in admins:
        label = "👑 صاحب ربات" if uid == OWNER_ID else "ادمین"
        if uid == OWNER_ID:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{uid} — {label}", callback_data=f"admin_info_{uid}"
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{uid} — {label}", callback_data=f"admin_info_{uid}"
                    ),
                    InlineKeyboardButton(
                        text="🗑 حذف", callback_data=f"del_admin_{uid}"
                    ),
                ]
            )
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await msg.answer("👮‍♂️ لیست ادمین‌ها:", reply_markup=kb)


@dp.callback_query(F.data.startswith("del_admin_"))
async def delete_admin_callback(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید", show_alert=True)
    user_id = int(callback.data.split("_")[-1])
    if user_id == OWNER_ID:
        return await callback.answer("نمی‌توان صاحب ربات را حذف کرد.", show_alert=True)
    cur.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
    db.commit()
    await callback.answer("ادمین حذف شد.", show_alert=True)
    try:
        await callback.message.delete()
    except:
        pass


# ================= جستجوی میم برای ویرایش (هندلر منوی مدیریت) =================
@dp.message(F.text == "🔎 جستجوی میم برای ویرایش")
async def search_meme_for_edit(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("دسترسی ندارید.")
    states[msg.from_user.id] = "search_edit"
    await msg.answer(
        "عبارت جستجو را وارد کنید (می‌توانید از تگ یا اسم استفاده کنید). برای جستجو در یک دسته خاص بنویسید: cat:نام_دسته عبارت",
        reply_markup=back_kb(),
    )


@dp.message(F.text, lambda m: states.get(m.from_user.id) == "search_edit")
async def handle_search_edit(msg: types.Message):
    q = msg.text.strip()
    if not q:
        return await msg.answer("عبارت خالی است.", reply_markup=admin_kb())
    category_filter = None
    term = q
    if q.lower().startswith("cat:"):
        parts = q.split(" ", 1)
        category_filter = parts[0][4:].strip()
        term = parts[1].strip() if len(parts) > 1 else ""
    rows = []
    if category_filter:
        cur.execute("SELECT id FROM categories WHERE name = ?", (category_filter,))
        r = cur.fetchone()
        if r:
            cid = r[0]
            if term:
                like = f"%{term}%"
                cur.execute(
                    "SELECT id, file_type, caption FROM memes WHERE category_id = ? AND (tags LIKE ? OR caption LIKE ?) ORDER BY COALESCE(last_used, added_at) DESC LIMIT 50",
                    (cid, like, like),
                )
            else:
                cur.execute(
                    "SELECT id, file_type, caption FROM memes WHERE category_id = ? ORDER BY COALESCE(last_used, added_at) DESC LIMIT 50",
                    (cid,),
                )
        else:
            rows = []
    else:
        like = f"%{term}%"
        cur.execute(
            "SELECT id, file_type, caption FROM memes WHERE tags LIKE ? OR caption LIKE ? ORDER BY COALESCE(last_used, added_at) DESC LIMIT 50",
            (like, like),
        )
    rows = cur.fetchall()
    if not rows:
        states.pop(msg.from_user.id, None)
        return await msg.answer("نتیجه‌ای پیدا نشد.", reply_markup=admin_kb())
    kb_rows = []
    for mid, ftype, caption in rows:
        short = (caption[:40] + "...") if caption else "بدون اسم"
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"ID {mid} | {ftype} | {short}", callback_data=f"manage_{mid}"
                )
            ]
        )
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    states.pop(msg.from_user.id, None)
    await msg.answer("نتایج جستجو (برای ویرایش روی مورد بزنید):", reply_markup=kb)


# ==========================================
# ============== DOWNLOADER ================
# ==========================================


def make_progress_bar(percent: float, length: int = 20) -> str:
    """ساخت نوار پیشرفت متنی"""
    filled = int(length * percent / 100)
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {percent:.1f}%"


# ==========================================
# ========== COOKIE MANAGEMENT =============
# ==========================================
# مسیر فایل کوکی که yt-dlp استفاده می‌کند
# مسیر فایل کوکی: اولویت با پوشه‌ی ولوم (همان‌جا که دیتابیس است)،
# سپس فایل کنار کد. متغیر YT_COOKIES هم هنوز پشتیبانی می‌شود.
_VOL_DIR = os.path.dirname(DB) or "/data"
COOKIES_FILE = os.path.join(_VOL_DIR, "cookies.txt")
if not os.path.exists(COOKIES_FILE):
    COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")


def _prepare_cookies_file():
    """
    اگر کوکی داخل متغیر محیطی YT_COOKIES باشد، آن را در فایل cookies.txt می‌نویسد.
    این کار مخصوص محیط Deployment (Publish) در Replit است که مرورگر در دسترس نیست.
    """
    env_cookies = os.getenv("YT_COOKIES", "").strip()
    if env_cookies:
        try:
            # اگر \n به صورت متن ذخیره شده باشد، به خط واقعی تبدیل می‌کنیم
            content = env_cookies.replace("\\n", "\n")
            if not content.startswith("# Netscape"):
                content = "# Netscape HTTP Cookie File\n" + content
            with open(COOKIES_FILE, "w", encoding="utf-8") as f:
                f.write(content)
            logging.info("cookies.txt از متغیر محیطی YT_COOKIES ساخته شد.")
        except Exception as e:
            logging.warning(f"نوشتن فایل کوکی ناموفق بود: {e}")


def apply_cookies(ydl_opts: dict) -> dict:
    """
    گزینه‌های کوکی و ضدربات را به تنظیمات yt-dlp اضافه می‌کند.
    این باعث می‌شود در حالت Publish (IP دیتاسنتر) یوتیوب خطای
    «Sign in to confirm you're not a bot» ندهد.
    """
    # استفاده از فایل کوکی در صورت وجود
    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        ydl_opts["cookiefile"] = COOKIES_FILE
        logging.info("یوتیوب: از فایل کوکی استفاده می‌شود.")
    else:
        logging.warning("یوتیوب: فایل کوکی یافت نشد! در صورت بلاک شدن توسط یوتیوب، متغیر YT_COOKIES را ست کنید.")

    # User-Agent واقعی تا درخواست شبیه مرورگر معمولی باشد
    ydl_opts.setdefault("http_headers", {})
    ydl_opts["http_headers"].setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )

    # استفاده از کلاینت‌های جایگزین یوتیوب که کمتر بلاک می‌شوند
    ydl_opts.setdefault("extractor_args", {})
    ydl_opts["extractor_args"].setdefault(
        "youtube", {"player_client": ["android", "web"]}
    )

    # تلاش مجدد در صورت خطای موقتی
    ydl_opts.setdefault("retries", 3)
    ydl_opts.setdefault("fragment_retries", 3)
    return ydl_opts


# ساخت فایل کوکی هنگام شروع (اگر متغیر محیطی ست شده باشد)
_prepare_cookies_file()


async def update_progress_message(msg: types.Message, text: str):
    """آپدیت پیام پیشرفت"""
    try:
        await msg.edit_text(text)
    except:
        pass


# ---- ورود به بخش دانلودر ----
@dp.message(F.text == "⬇️ دانلودر")
async def downloader_panel(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("دسترسی ندارید!")
    await msg.answer(
        "🎬 به بخش دانلودر خوش آمدید!\n\nاز منوی زیر انتخاب کنید:",
        reply_markup=downloader_kb(),
    )


@dp.message(F.text == "🔙 برگشت به ادمین")
async def back_to_admin_from_downloader(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return
    states.pop(msg.from_user.id, None)
    context_data.pop(msg.from_user.id, None)
    await msg.answer("برگشت به پنل ادمین", reply_markup=admin_kb())




def split_file(path: str, limit: int) -> list:
    """تقسیم فایل به پارت‌های حداکثر `limit` بایت. برمی‌گرداند لیست مسیرهای پارت."""
    import math
    size = os.path.getsize(path)
    n = max(1, math.ceil(size / limit))
    part_size = math.ceil(size / n)
    base = path + ".part"
    parts = []
    with open(path, "rb") as f:
        for i in range(n):
            chunk = f.read(part_size)
            if not chunk:
                break
            pp = f"{base}{i+1:03d}"
            with open(pp, "wb") as pf:
                pf.write(chunk)
            parts.append(pp)
    return parts


# ==========================================
# =========== YOUTUBE DOWNLOADER ===========
# ==========================================


@dp.message(F.text == "🎬 دانلود یوتیوب")
async def youtube_downloader_start(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("دسترسی ندارید!")
    states[msg.from_user.id] = "yt_url"
    await msg.answer(
        "🎬 **دانلود از یوتیوب**\n\nلینک ویدیو (youtube / youtu.be) را ارسال کنید:",
        reply_markup=back_kb(),
        parse_mode="Markdown",
    )


@dp.message(F.text, lambda m: states.get(m.from_user.id) == "yt_url")
async def youtube_get_url(msg: types.Message):
    url = msg.text.strip()
    yt_pattern = r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+"
    if not re.match(yt_pattern, url):
        return await msg.answer("❌ لینک یوتیوب معتبر نیست. دوباره ارسال کنید.")
    if "list=" in url:
        url = url.split("&")[0]

    context_data[msg.from_user.id] = {"yt_url": url}
    states[msg.from_user.id] = "yt_quality"
    wait = await msg.answer("⏳ در حال دریافت اطلاعات ویدیو...")

    try:
        import yt_dlp

        # کلاینت‌هایی که فرمت‌های کامل (تا 4K) را برمی‌گردانند
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "ignoreerrors": True,
            "extractor_args": {
                "youtube": {
                    "player_client": ["tv", "web", "android_vr", "web_safari"],
                }
            },
        }

        def get_info():
            with yt_dlp.YoutubeDL(apply_cookies(ydl_opts)) as ydl:
                return ydl.extract_info(url, download=False)

        info = await asyncio.get_event_loop().run_in_executor(None, get_info)
        title = info.get("title", "ویدیو")
        dur = info.get("duration", 0)
        dur_s = f"{dur // 60}:{dur % 60:02d}" if dur else "نامشخص"
        context_data[msg.from_user.id]["title"] = title

        # جمع‌آوری فرمت‌های ویدیویی با ارتفاع >= 360 (طبق درخواست)
        vq = {}  # height -> format_id
        for f in info.get("formats", []):
            h = f.get("height")
            fid = f.get("format_id")
            if not h or not fid:
                continue
            if h < 360:
                continue
            if f.get("vcodec", "none") == "none":
                continue
            if h not in vq:
                vq[h] = fid
            else:
                # اگر قبلاً بود، همان را نگه می‌داریم (اولین بهترین است)
                pass

        heights = sorted(vq.keys())
        logging.info(f"[YT] {len(heights)} کیفیت پیدا شد: {heights}")

        if not heights:
            return await wait.edit_text(
                "❌ هیچ فرمت ویدیویی پیدا نشد.\n"
                "یوتیوب در IP دیتاسنتر محدود کرده. کوکی معتبر (YT_COOKIES) لازم است."
            )

        kb = [[InlineKeyboardButton(text="🔄 رفرش", callback_data="ytdl_refresh")]]
        for h in heights:
            kb.append([InlineKeyboardButton(text=f"🎬 {h}p", callback_data=f"ytdl_video_{h}_{vq[h]}")])
        kb.append([
            InlineKeyboardButton(text="🎵 MP3 128", callback_data="ytdl_audio_128"),
            InlineKeyboardButton(text="🎵 MP3 320", callback_data="ytdl_audio_320"),
        ])
        kb.append([InlineKeyboardButton(text="🎼 FLAC", callback_data="ytdl_audio_flac")])
        kb.append([InlineKeyboardButton(text="❌ لغو", callback_data="ytdl_cancel")])

        await wait.edit_text(
            f"🎬 **{title}**\n⏱ {dur_s}\n\nکیفیت را انتخاب کنید (از ۳۶۰p تا {max(heights)}p):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            parse_mode="Markdown",
        )
    except Exception as e:
        await wait.edit_text(f"❌ خطا در دریافت اطلاعات:\n{str(e)[:300]}")
        states.pop(msg.from_user.id, None)
        context_data.pop(msg.from_user.id, None)


@dp.callback_query(F.data == "ytdl_refresh")
async def yt_refresh(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید", show_alert=True)
    await callback.answer("🔄")
    saved = context_data.get(callback.from_user.id, {})
    if not saved.get("yt_url"):
        return await callback.message.edit_text("❌ لینک یافت نشد. دوباره شروع کنید.")
    class _M:
        text = saved["yt_url"]
        from_user = callback.from_user
    await youtube_get_url(_M())


@dp.callback_query(F.data.startswith("ytdl_"))
async def youtube_download_callback(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید", show_alert=True)
    data = callback.data
    uid = callback.from_user.id
    if data == "ytdl_cancel":
        states.pop(uid, None)
        context_data.pop(uid, None)
        await callback.message.edit_text("❌ لغو شد.")
        return await callback.answer()

    ud = context_data.get(uid, {})
    url = ud.get("yt_url")
    title = ud.get("title", "ویدیو")
    if not url:
        return await callback.answer("خطا: لینک پیدا نشد.", show_alert=True)

    await callback.answer()
    prog = await callback.message.edit_text("⏳ در حال آماده‌سازی...")
    states.pop(uid, None)
    context_data.pop(uid, None)

    tmp = tempfile.mkdtemp()
    try:
        import yt_dlp

        last = [0]
        def hook(d):
            if d.get("status") == "downloading":
                try:
                    p = float(d.get("_percent_str", "0%").replace("%", "").strip() or 0)
                    if p - last[0] >= 10 or p >= 99:
                        last[0] = p
                        asyncio.run_coroutine_threadsafe(
                            prog.edit_text(
                                f"⬇️ **دانلود...**\n{make_progress_bar(p)}\n"
                                f"🚀 {d.get('_speed_str','نامشخص')} | ⏳ {d.get('_eta_str','نامشخص')}",
                                parse_mode="Markdown",
                            ),
                            asyncio.get_event_loop(),
                        )
                except Exception:
                    pass

        is_audio = data.startswith("ytdl_audio_")
        if is_audio:
            q = data.replace("ytdl_audio_", "")
            if q == "flac":
                post = [{"key": "FFmpegExtractAudio", "preferredcodec": "flac"}]
                label = "🎼 FLAC"
            else:
                post = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": q}]
                label = f"🎵 MP3 {q}K"
            opts = {
                "format": "bestaudio/best",
                "outtmpl": os.path.join(tmp, "%(title)s.%(ext)s"),
                "postprocessors": post,
                "quiet": True,
                "progress_hooks": [hook],
            }
        else:
            parts = data.replace("ytdl_video_", "").split("_")
            h = parts[0]
            fid = parts[1] if len(parts) > 1 else None
            label = f"🎬 {h}p"
            if fid:
                opts = {
                    "format": f"{fid}+bestaudio/best",
                    "outtmpl": os.path.join(tmp, "%(title)s.%(ext)s"),
                    "merge_output_format": "mp4",
                    "quiet": True,
                    "progress_hooks": [hook],
                }
            else:
                opts = {
                    "format": f"bestvideo[height<={h}]+bestaudio/best",
                    "outtmpl": os.path.join(tmp, "%(title)s.%(ext)s"),
                    "merge_output_format": "mp4",
                    "quiet": True,
                    "progress_hooks": [hook],
                }

        def dl():
            with yt_dlp.YoutubeDL(apply_cookies(opts)) as ydl:
                ydl.download([url])

        await prog.edit_text(f"⬇️ **دانلود {label}...**\n{make_progress_bar(0)}\n⏳ صبر کنید...", parse_mode="Markdown")
        await asyncio.get_event_loop().run_in_executor(None, dl)

        files = [str(p) for p in Path(tmp).glob("*") if p.is_file()]
        if not files:
            return await prog.edit_text("❌ فایلی دانلود نشد!")

        fp = files[0]
        sz = os.path.getsize(fp)

        PART_LIMIT = 2 * 1024 * 1024 * 1024  # 2GB
        if sz > PART_LIMIT:
            await prog.edit_text("✅ دانلود کامل شد!\n📤 در حال تقسیم به پارت‌های ۲GB...")
            part_paths = split_file(fp, PART_LIMIT)
            total = len(part_paths)
            for i, pp in enumerate(part_paths, 1):
                inp = FSInputFile(pp, filename=os.path.basename(pp))
                if is_audio:
                    await callback.message.answer_audio(audio=inp, title=title, caption=f"🎵 {title}\n{label}\n📦 پارت {i}/{total}")
                else:
                    await callback.message.answer_video(video=inp, caption=f"🎬 {title}\n{label}\n📦 پارت {i}/{total}", supports_streaming=True)
                os.remove(pp)
            await prog.delete()
        else:
            if sz > MAX_UPLOAD_SIZE:
                return await prog.edit_text(f"❌ حجم ({sz // (1024*1024)}MB) بیش از {MAX_UPLOAD_LABEL} است!")
            await prog.edit_text("✅ دانلود کامل شد!\n📤 در حال آپلود...")
            inp = FSInputFile(fp, filename=os.path.basename(fp))
            if is_audio:
                await callback.message.answer_audio(audio=inp, title=title, caption=f"🎵 {title}\n{label}")
            else:
                await callback.message.answer_video(video=inp, caption=f"🎬 {title}\n{label}", supports_streaming=True)
            await prog.delete()
    except Exception as e:
        await prog.edit_text(f"❌ خطا در دانلود:\n{str(e)[:300]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ==========================================
# =========== INSTAGRAM DOWNLOADER ==========
# ==========================================


@dp.message(F.text == "📸 دانلود اینستاگرام")
async def instagram_downloader_start(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("دسترسی ندارید!")
    states[msg.from_user.id] = "insta_url"
    await msg.answer(
        "📸 **دانلود از اینستاگرام**\n\n"
        "لینک پست، ریلز یا استوری اینستاگرام را ارسال کنید:\n\n"
        "مثال:\n`https://www.instagram.com/p/...`",
        reply_markup=back_kb(),
        parse_mode="Markdown",
    )


@dp.message(F.text, lambda m: states.get(m.from_user.id) == "insta_url")
async def instagram_get_url(msg: types.Message):
    url = msg.text.strip()
    if "instagram.com/" not in url:
        return await msg.answer("❌ لینک اینستاگرام معتبر نیست. دوباره ارسال کنید.")
    # حذف پارامترهای اضافی (مثل utm_source)
    url = url.split("?")[0]

    context_data[msg.from_user.id] = {"insta_url": url}
    states[msg.from_user.id] = "insta_ready"
    wait = await msg.answer("⏳ در حال دریافت اطلاعات...")

    try:
        import yt_dlp

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "ignoreerrors": True,
        }

        def get_info():
            with yt_dlp.YoutubeDL(apply_cookies(ydl_opts)) as ydl:
                return ydl.extract_info(url, download=False)

        info = await asyncio.get_event_loop().run_in_executor(None, get_info)
        title = info.get("title", "اینستاگرام") or "پست اینستاگرام"
        context_data[msg.from_user.id]["title"] = title

        # جمع‌آوری فرمت‌ها
        fmt_video = None
        fmt_audio = None
        for f in info.get("formats", []):
            if f.get("vcodec", "none") != "none" and f.get("acodec", "none") != "none":
                # فرمت ترکیبی (صدا+تصویر) بهترین است
                if not fmt_video or (f.get("tbr", 0) or 0) > (fmt_video.get("tbr", 0) or 0):
                    fmt_video = f
            elif f.get("vcodec", "none") != "none":
                if not fmt_video or (f.get("tbr", 0) or 0) > (fmt_video.get("tbr", 0) or 0):
                    fmt_video = f
            elif f.get("acodec", "none") != "none":
                if not fmt_audio or (f.get("tbr", 0) or 0) > (fmt_audio.get("tbr", 0) or 0):
                    fmt_audio = f

        # ساخت کیبورد
        kb = []
        if fmt_video:
            fid = fmt_video.get("format_id")
            label = "🎬 ویدیو"
            # تشخیص کیفیت
            height = fmt_video.get("height", "")

            kb.append([InlineKeyboardButton(text=f"🎬 دانلود ویدیو ({height}p)", callback_data=f"insta_video_{fid}")])

        # اگه ویدیو هست، نیازی به صدا جدا نیست (ترکیبی داریم)
        kb.append([InlineKeyboardButton(text="❌ لغو", callback_data="insta_cancel")])

        await wait.edit_text(
            f"📸 <b>{title[:50]}</b>\n\nنوع دانلود را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            parse_mode="HTML",
        )
    except Exception as e:
        await wait.edit_text(f"❌ خطا در دریافت اطلاعات:\n{str(e)[:300]}")
        states.pop(msg.from_user.id, None)
        context_data.pop(msg.from_user.id, None)


@dp.callback_query(F.data.startswith("insta_"))
async def instagram_download_callback(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید", show_alert=True)
    data = callback.data
    uid = callback.from_user.id
    if data == "insta_cancel":
        states.pop(uid, None)
        context_data.pop(uid, None)
        await callback.message.edit_text("❌ لغو شد.")
        return await callback.answer()

    ud = context_data.get(uid, {})
    url = ud.get("insta_url")
    # تمیز کردن عنوان برای HTML
    title = (ud.get("title", "اینستاگرام") or "پست اینستاگرام").replace("<", "&lt;").replace(">", "&gt;")
    if not url:
        return await callback.answer("خطا: لینک پیدا نشد.", show_alert=True)

    await callback.answer()
    prog = await callback.message.edit_text("⏳ در حال آماده‌سازی...")
    states.pop(uid, None)
    context_data.pop(uid, None)

    tmp = tempfile.mkdtemp()
    try:
        import yt_dlp

        fid = data.replace("insta_video_", "")
        opts = {
            "format": f"{fid}+bestaudio/best" if fid else "bestvideo+bestaudio/best",
            "outtmpl": os.path.join(tmp, "%(title)s.%(ext)s"),
            "merge_output_format": "mp4",
            "quiet": True,
        }

        def dl():
            with yt_dlp.YoutubeDL(apply_cookies(opts)) as ydl:
                ydl.download([url])

        await prog.edit_text("⬇️ <b>در حال دانلود از اینستاگرام...</b>\n⏳ صبر کنید...", parse_mode="HTML")
        await asyncio.get_event_loop().run_in_executor(None, dl)

        files = [str(p) for p in Path(tmp).glob("*") if p.is_file()]
        if not files:
            return await prog.edit_text("❌ فایلی دانلود نشد!")

        fp = files[0]
        sz = os.path.getsize(fp)

        # تمیز کردن عنوان برای کپشن
        caption_title = title[:100]

        PART_LIMIT = 2 * 1024 * 1024 * 1024
        if sz > PART_LIMIT:
            await prog.edit_text("✅ دانلود کامل شد!\n📤 در حال تقسیم...")
            part_paths = split_file(fp, PART_LIMIT)
            for i, pp in enumerate(part_paths, 1):
                inp = FSInputFile(pp, filename=os.path.basename(pp))
                await callback.message.answer_video(video=inp, caption=f"📸 <b>{caption_title}</b>\n📦 پارت {i}/{len(part_paths)}", parse_mode="HTML", supports_streaming=True)
                os.remove(pp)
            await prog.delete()
            return
        elif sz > MAX_UPLOAD_SIZE:
            return await prog.edit_text(f"❌ حجم بیش از {MAX_UPLOAD_LABEL} است!")

        await prog.edit_text("✅ دانلود کامل شد!\n📤 در حال آپلود...")
        inp = FSInputFile(fp, filename=os.path.basename(fp))
        await callback.message.answer_video(video=inp, caption=f"📸 <b>{caption_title}</b>", parse_mode="HTML", supports_streaming=True)
        await prog.delete()
    except Exception as e:
        await prog.edit_text(f"❌ خطا در دانلود:\n{str(e)[:300]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ==========================================
# ============ DEEZER SEARCH ===============
# ==========================================


@dp.message(F.text == "🔎 جستجو در Spotify")
async def spotify_search_start(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("دسترسی ندارید!")
    states[msg.from_user.id] = "spotify_search"
    await msg.answer(
        "🔎 **جستجو در Spotify**\n\nنام آهنگ یا هنرمند را وارد کنید:",
        reply_markup=back_kb(),
        parse_mode="Markdown",
    )


@dp.message(F.text, lambda m: states.get(m.from_user.id) == "spotify_search")
async def spotify_do_search(msg: types.Message):
    query = msg.text.strip()
    if not query:
        return await msg.answer("عبارت جستجو نمیتونه خالی باشه.")
    states.pop(msg.from_user.id, None)
    wait_msg = await msg.answer(f"🔍 در حال جستجوی `{query}` در Spotify...")
    try:
        import yt_dlp
        ydl_opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
        loop = asyncio.get_event_loop()
        def search():
            with yt_dlp.YoutubeDL(apply_cookies(ydl_opts)) as ydl:
                return ydl.extract_info(f"ytsearch10:{query} audio", download=False)
        info = await loop.run_in_executor(None, search)
        entries = info.get("entries") or []
        if not entries:
            return await wait_msg.edit_text("❌ نتیجهای پیدا نشد.")
        kb_rows = []
        results_text = "🎵 **نتایج جستجو در Spotify:**\n\n"
        for i, e in enumerate(entries[:10], 1):
            title = e.get("title", "نامشخص")
            dur = e.get("duration", 0)
            dur_s = f"{dur // 60}:{dur % 60:02d}" if dur else ""
            url = e.get("webpage_url") or e.get("url") or ""
            results_text += f"{i}. {title} ({dur_s})\n"
            kb_rows.append([
                InlineKeyboardButton(
                    text=f"{i}. {title[:35]}",
                    callback_data=f"sp_dl|{i}|{url}",
                )
            ])
        kb_rows.append([InlineKeyboardButton(text="❌ بستن", callback_data="sp_close")])
        await wait_msg.edit_text(
            results_text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
            parse_mode="Markdown",
        )
    except Exception as e:
        await wait_msg.edit_text(f"❌ خطا در جستجو:\n{str(e)[:200]}")


@dp.callback_query(F.data == "sp_close")
async def spotify_close(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("sp_dl|"))
async def spotify_select_track(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید", show_alert=True)
    parts = callback.data.split("|", 2)
    if len(parts) < 3:
        return await callback.answer("bad data", show_alert=True)
    idx = parts[1]
    url = parts[2]
    context_data[callback.from_user.id] = {"spotify_url": url, "spotify_idx": idx}
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 128K MP3", callback_data=f"spdl_128")],
        [InlineKeyboardButton(text="🎵 320K MP3", callback_data=f"spdl_320")],
        [InlineKeyboardButton(text="🎼 FLAC", callback_data=f"spdl_flac")],
        [InlineKeyboardButton(text="❌ لغو", callback_data="spdl_cancel")],
    ])
    await callback.message.edit_text("🎵 کیفیت مورد نظر را انتخاب کنید:", reply_markup=kb)


@dp.callback_query(lambda c: c.data.startswith("spdl_"))
async def spotify_download(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید", show_alert=True)
    data = callback.data
    if data == "spdl_cancel":
        await callback.message.edit_text("❌ لغو شد.")
        return await callback.answer()
    q = data.replace("spdl_", "")  # 128 / 320 / flac
    ud = context_data.get(callback.from_user.id, {})
    url = ud.get("spotify_url")
    if not url:
        return await callback.answer("لینک پیدا نشد.", show_alert=True)
    await callback.answer()
    prog = await callback.message.edit_text("⏳ در حال دانلود...")
    context_data.pop(callback.from_user.id, None)
    tmp = tempfile.mkdtemp()
    try:
        info_title = "unknown"
        # روش اول: spotdl (اگر نصب باشه و Spotify URL باشه)
        if "open.spotify.com" in url:
            try:
                from spotdl import Spotdl
                spot = Spotdl()
                song_info = spot.search([url])
                if song_info:
                    info_title = f"{song_info[0].artist} - {song_info[0].name}"
                    result = await asyncio.get_event_loop().run_in_executor(None, lambda: spot.download(song_info[0]))
                    if result and len(result) > 0:
                        fp = str(result[0])
                        if fp and os.path.getsize(fp) > 0:
                            sz = os.path.getsize(fp)
                            if sz > MAX_UPLOAD_SIZE:
                                return await prog.edit_text(f"❌ حجم بیش از {MAX_UPLOAD_LABEL}!")
                            ext = os.path.splitext(fp)[1] or ".mp3"
                            final_name = f"{info_title[:80]}{ext}"
                            inp = FSInputFile(fp, filename=final_name)
                            await callback.message.answer_audio(
                                audio=inp, caption=f"🎵 <b>Spotify</b>\n{label}", title=info_title[:100], parse_mode="HTML"
                            )
                            await prog.delete()
                            return
            except Exception as e_spot:
                logging.warning(f"spotdl failed: {e_spot}")
                await prog.edit_text("⏳ spotdl ناموفق، تلاش با yt-dlp...")

        # روش دوم: yt-dlp
        import yt_dlp
        if q == "flac":
            post = [{"key": "FFmpegExtractAudio", "preferredcodec": "flac"}]
            label = "🎼 FLAC"
        else:
            post = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": q}]
            label = f"🎵 MP3 {q}K"
        opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(tmp, "%(title)s.%(ext)s"),
            "postprocessors": post,
            "quiet": True,
        }
        def dl():
            with yt_dlp.YoutubeDL(apply_cookies(opts)) as ydl:
                return ydl.extract_info(url, download=True)
        info = await asyncio.get_event_loop().run_in_executor(None, dl)
        info_title = info.get("title", info_title) if info else info_title
        files = [str(p) for p in Path(tmp).glob("*") if p.is_file()]
        if not files:
            return await prog.edit_text("❌ فایلی دانلود نشد!")
        fp = files[0]
        sz = os.path.getsize(fp)
        if sz > MAX_UPLOAD_SIZE:
            return await prog.edit_text(f"❌ حجم بیش از {MAX_UPLOAD_LABEL}!")
        title_clean = info_title[:100]
        ext = os.path.splitext(fp)[1] or ".mp3"
        final_name = f"{title_clean}{ext}"
        inp = FSInputFile(fp, filename=final_name)
        await callback.message.answer_audio(audio=inp, caption=f"🎵 <b>{title_clean}</b>\n{label}", title=title_clean, parse_mode="HTML")
        await prog.delete()
    except Exception as e:
        await prog.edit_text(f"❌ خطا:\n{str(e)[:300]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@dp.message(F.text == "🔎 جستجو در Deezer")
async def deezer_search_start(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("دسترسی ندارید!")
    states[msg.from_user.id] = "deezer_search"
    await msg.answer(
        "🔎 **جستجو در Deezer**\n\nنام آهنگ یا هنرمند را وارد کنید:",
        reply_markup=back_kb(),
        parse_mode="Markdown",
    )


@dp.message(F.text, lambda m: states.get(m.from_user.id) == "deezer_search")
async def deezer_do_search(msg: types.Message):
    query = msg.text.strip()
    if not query:
        return await msg.answer("عبارت جستجو نمی‌تواند خالی باشد.")

    states.pop(msg.from_user.id, None)

    wait_msg = await msg.answer(f"🔍 در حال جستجوی `{query}` در Deezer...")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.deezer.com/search", params={"q": query, "limit": 10}
            ) as resp:
                data = await resp.json()

        tracks = data.get("data", [])
        if not tracks:
            await wait_msg.edit_text("❌ نتیجه‌ای پیدا نشد.")
            return

        kb_rows = []
        results_text = "🎵 **نتایج جستجو در Deezer:**\n\n"

        for i, track in enumerate(tracks[:10], 1):
            track_id = track.get("id")
            title = track.get("title", "نامشخص")
            artist = track.get("artist", {}).get("name", "نامشخص")
            duration = track.get("duration", 0)
            dur_str = f"{duration // 60}:{duration % 60:02d}"

            results_text += f"{i}. **{title}** - {artist} ({dur_str})\n"

            kb_rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{i}. {title[:30]} - {artist[:20]}",
                        callback_data=f"dz_dl_{track_id}",
                    )
                ]
            )

        kb_rows.append([InlineKeyboardButton(text="❌ بستن", callback_data="dz_close")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

        await wait_msg.edit_text(results_text, reply_markup=kb, parse_mode="Markdown")

    except Exception as e:
        await wait_msg.edit_text(f"❌ خطا در جستجو:\n{str(e)[:200]}")


@dp.callback_query(F.data == "dz_close")
async def deezer_close(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.answer()


@dp.callback_query(F.data.startswith("dz_dl_"))
async def deezer_select_track(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید", show_alert=True)

    track_id = callback.data.replace("dz_dl_", "")
    context_data[callback.from_user.id] = {"deezer_track_id": track_id}

    await callback.answer()

    # نمایش انتخاب کیفیت
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎵 128K MP3", callback_data=f"dzdl_128_{track_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎵 320K MP3", callback_data=f"dzdl_320_{track_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎼 FLAC", callback_data=f"dzdl_flac_{track_id}"
                )
            ],
            [InlineKeyboardButton(text="❌ لغو", callback_data="dzdl_cancel")],
        ]
    )

    await callback.message.edit_text("🎵 کیفیت دانلود را انتخاب کنید:", reply_markup=kb)


@dp.callback_query(F.data == "dzdl_cancel")
async def deezer_download_cancel(callback: types.CallbackQuery):
    context_data.pop(callback.from_user.id, None)
    await callback.message.edit_text("❌ دانلود لغو شد.")
    await callback.answer()


@dp.callback_query(F.data.startswith("dzdl_"))
async def deezer_download_track(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید", show_alert=True)

    if callback.data == "dzdl_cancel":
        return

    parts = callback.data.split("_")
    if len(parts) < 3:
        return await callback.answer("داده نامعتبر", show_alert=True)

    quality = parts[1]
    track_id = parts[2]

    await callback.answer()

    quality_labels = {"128": "MP3 128K", "320": "MP3 320K", "flac": "FLAC"}
    quality_label = quality_labels.get(quality, "MP3 320K")

    progress_msg = await callback.message.edit_text(
        f"⬇️ در حال دانلود از Deezer ({quality_label})...\n\n{make_progress_bar(0)}"
    )

    tmp_dir = tempfile.mkdtemp()

    try:
        # دریافت اطلاعات آهنگ از Deezer API
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.deezer.com/track/{track_id}") as resp:
                track_info = await resp.json()

        track_title = track_info.get("title", "track")
        artist_name = track_info.get("artist", {}).get("name", "")
        isrc = track_info.get("isrc", "")

        await update_progress_message(
            progress_msg,
            f"⬇️ **دانلود از Deezer ({quality_label})**\n\n"
            f"{make_progress_bar(20)}\n\n"
            f"🎵 {track_title} - {artist_name}",
        )

        # استفاده از spotdl با ISRC یا نام آهنگ برای دانلود
        loop = asyncio.get_event_loop()

        search_query = f"{track_title} {artist_name}"

        def do_download_deezer():
            import subprocess
            import sys

            if quality == "flac":
                format_arg = "flac"
                bitrate_arg = "flac"
            else:
                format_arg = "mp3"
                bitrate_arg = quality + "k"

            # جستجو با spotdl
            cmd = [
                sys.executable,
                "-m",
                "spotdl",
                f"'{search_query}'",
                "--output",
                tmp_dir,
                "--format",
                format_arg,
                "--bitrate",
                bitrate_arg,
                "--no-cache",
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            return result.returncode, result.stdout, result.stderr

        await update_progress_message(
            progress_msg,
            f"⬇️ **دانلود از Deezer ({quality_label})**\n\n"
            f"{make_progress_bar(50)}\n\n"
            f"⏳ در حال دانلود...",
        )

        returncode, stdout, stderr = await loop.run_in_executor(
            None, do_download_deezer
        )

        # پیدا کردن فایل
        extensions = ["*.mp3", "*.flac", "*.m4a", "*.ogg"]
        downloaded_files = []
        for ext in extensions:
            downloaded_files.extend(Path(tmp_dir).glob(ext))

        if not downloaded_files:
            # تلاش با yt-dlp
            await update_progress_message(
                progress_msg, f"⬇️ در حال تلاش با روش دیگر...\n\n{make_progress_bar(60)}"
            )

            def do_ytdlp_search():
                import yt_dlp

                if quality == "flac":
                    format_arg = "bestaudio/best"
                    postprocessors = [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "flac",
                        }
                    ]
                else:
                    format_arg = "bestaudio/best"
                    postprocessors = [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": quality,
                        }
                    ]

                ydl_opts = {
                    "format": format_arg,
                    "outtmpl": os.path.join(tmp_dir, "%(title)s.%(ext)s"),
                    "postprocessors": postprocessors,
                    "quiet": True,
                    "default_search": "ytsearch1",
                }

                with yt_dlp.YoutubeDL(apply_cookies(ydl_opts)) as ydl:
                    ydl.download([f"ytsearch1:{search_query}"])

            await loop.run_in_executor(None, do_ytdlp_search)

            for ext in extensions:
                downloaded_files.extend(Path(tmp_dir).glob(ext))

        await update_progress_message(
            progress_msg,
            f"⬇️ **دانلود از Deezer ({quality_label})**\n\n"
            f"{make_progress_bar(95)}\n\n"
            f"📤 در حال آپلود...",
        )

        if not downloaded_files:
            await progress_msg.edit_text("❌ دانلود ناموفق بود!")
            return

        for file_path in downloaded_files:
            file_size = os.path.getsize(str(file_path))
            if file_size > MAX_UPLOAD_SIZE:
                await callback.message.answer(
                    f"⚠️ فایل حجیم است ({file_size // (1024 * 1024)}MB)"
                )
                continue

            input_file = FSInputFile(str(file_path), filename=file_path.name)
            await callback.message.answer_audio(
                audio=input_file,
                caption=f"🎵 {track_title} - {artist_name}\n🎼 {quality_label}\n📀 Deezer",
            )

        await progress_msg.delete()

    except Exception as e:
        await progress_msg.edit_text(f"❌ خطا:\n{str(e)[:300]}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        context_data.pop(callback.from_user.id, None)


# ==========================================
# ========== SOUNDCLOUD SEARCH =============
# ==========================================


@dp.message(F.text == "🔎 جستجو در SoundCloud")
async def soundcloud_search_start(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("دسترسی ندارید!")
    states[msg.from_user.id] = "soundcloud_search"
    await msg.answer(
        "🔎 **جستجو در SoundCloud**\n\nنام آهنگ یا هنرمند را وارد کنید:",
        reply_markup=back_kb(),
        parse_mode="Markdown",
    )


@dp.message(F.text, lambda m: states.get(m.from_user.id) == "soundcloud_search")
async def soundcloud_do_search(msg: types.Message):
    query = msg.text.strip()
    if not query:
        return await msg.answer("عبارت جستجو نمی‌تواند خالی باشد.")

    states.pop(msg.from_user.id, None)

    wait_msg = await msg.answer(f"🔍 در حال جستجوی `{query}` در SoundCloud...")

    try:
        import yt_dlp

        loop = asyncio.get_event_loop()

        def do_search():
            ydl_opts = {
                "quiet": True,
                "extract_flat": "in_playlist",
                "no_warnings": True,
            }
            with yt_dlp.YoutubeDL(apply_cookies(ydl_opts)) as ydl:
                results = ydl.extract_info(f"scsearch10:{query}", download=False)
                return results

        results = await loop.run_in_executor(None, do_search)

        entries = results.get("entries", []) if results else []

        if not entries:
            await wait_msg.edit_text("❌ نتیجه‌ای پیدا نشد.")
            return

        kb_rows = []
        results_text = "🎵 **نتایج جستجو در SoundCloud:**\n\n"

        for i, entry in enumerate(entries[:10], 1):
            title = entry.get("title", "نامشخص")
            uploader = entry.get("uploader", "نامشخص")
            # با extract_flat گاهی url ناقص است؛ اولویت با webpage_url سپس url سپس permalink
            url = (
                entry.get("webpage_url")
                or entry.get("url")
                or entry.get("permalink_url", "")
            )
            if not url:
                continue
            duration = entry.get("duration", 0)
            dur_str = (
                f"{int(duration) // 60}:{int(duration) % 60:02d}" if duration else "?"
            )

            results_text += f"{i}. **{title}** - {uploader} ({dur_str})\n"

            # encode url برای callback_data
            import hashlib

            url_hash = hashlib.md5(url.encode()).hexdigest()[:8]

            # ذخیره url در context با hash
            context_data[f"sc_{url_hash}"] = url

            kb_rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{i}. {title[:30]} - {uploader[:15]}",
                        callback_data=f"sc_dl_{url_hash}",
                    )
                ]
            )

        kb_rows.append([InlineKeyboardButton(text="❌ بستن", callback_data="sc_close")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

        await wait_msg.edit_text(results_text, reply_markup=kb, parse_mode="Markdown")

    except Exception as e:
        await wait_msg.edit_text(f"❌ خطا در جستجو:\n{str(e)[:200]}")


@dp.callback_query(F.data == "sc_close")
async def soundcloud_close(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.answer()


@dp.callback_query(F.data.startswith("sc_dl_"))
async def soundcloud_select_track(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید", show_alert=True)

    url_hash = callback.data.replace("sc_dl_", "")
    url = context_data.get(f"sc_{url_hash}")

    if not url:
        return await callback.answer(
            "لینک منقضی شده. دوباره جستجو کنید.", show_alert=True
        )

    context_data[callback.from_user.id] = {"sc_url": url, "sc_hash": url_hash}
    await callback.answer()

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎵 128K MP3", callback_data=f"scdl_128_{url_hash}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎵 320K MP3", callback_data=f"scdl_320_{url_hash}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎼 FLAC (بهترین کیفیت)", callback_data=f"scdl_flac_{url_hash}"
                )
            ],
            [InlineKeyboardButton(text="❌ لغو", callback_data="scdl_cancel")],
        ]
    )

    await callback.message.edit_text("🎵 کیفیت دانلود را انتخاب کنید:", reply_markup=kb)


@dp.callback_query(F.data == "scdl_cancel")
async def soundcloud_download_cancel(callback: types.CallbackQuery):
    context_data.pop(callback.from_user.id, None)
    await callback.message.edit_text("❌ دانلود لغو شد.")
    await callback.answer()


@dp.callback_query(F.data.startswith("scdl_"))
async def soundcloud_download_track(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("دسترسی ندارید", show_alert=True)

    if callback.data == "scdl_cancel":
        return

    parts = callback.data.split("_")
    if len(parts) < 3:
        return await callback.answer("داده نامعتبر", show_alert=True)

    quality = parts[1]
    url_hash = parts[2]

    url = context_data.get(f"sc_{url_hash}")
    if not url:
        return await callback.answer("لینک منقضی شده.", show_alert=True)

    await callback.answer()

    quality_labels = {"128": "MP3 128K", "320": "MP3 320K", "flac": "FLAC"}
    quality_label = quality_labels.get(quality, "MP3 320K")

    progress_msg = await callback.message.edit_text(
        f"⬇️ در حال دانلود از SoundCloud ({quality_label})...\n\n{make_progress_bar(0)}"
    )

    tmp_dir = tempfile.mkdtemp()

    try:
        import yt_dlp

        loop = asyncio.get_event_loop()
        last_percent = [0]

        def progress_hook(d):
            if d["status"] == "downloading":
                try:
                    percent_str = d.get("_percent_str", "0%").strip().replace("%", "")
                    percent = float(percent_str)

                    if percent - last_percent[0] >= 10 or percent >= 99:
                        last_percent[0] = percent
                        bar = make_progress_bar(percent)
                        speed = d.get("_speed_str", "نامشخص")
                        eta = d.get("_eta_str", "نامشخص")

                        asyncio.run_coroutine_threadsafe(
                            update_progress_message(
                                progress_msg,
                                f"⬇️ **SoundCloud ({quality_label})**\n\n"
                                f"{bar}\n\n"
                                f"🚀 سرعت: {speed}\n"
                                f"⏳ زمان باقیمانده: {eta}",
                            ),
                            asyncio.get_event_loop(),
                        )
                except:
                    pass

        if quality == "flac":
            postprocessors = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "flac",
                }
            ]
        else:
            postprocessors = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": quality,
                }
            ]

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(tmp_dir, "%(title)s.%(ext)s"),
            "postprocessors": postprocessors,
            "quiet": True,
            "progress_hooks": [progress_hook],
        }

        def do_download():
            with yt_dlp.YoutubeDL(apply_cookies(ydl_opts)) as ydl:
                info = ydl.extract_info(url, download=True)
                return info.get("title", "آهنگ")

        title = await loop.run_in_executor(None, do_download)

        # پیدا کردن فایل
        extensions = ["*.mp3", "*.flac", "*.m4a", "*.ogg"]
        downloaded_files = []
        for ext in extensions:
            downloaded_files.extend(Path(tmp_dir).glob(ext))

        if not downloaded_files:
            await progress_msg.edit_text("❌ دانلود ناموفق بود!")
            return

        await update_progress_message(
            progress_msg, f"✅ دانلود کامل!\n📤 در حال آپلود..."
        )

        for file_path in downloaded_files:
            file_size = os.path.getsize(str(file_path))
            if file_size > MAX_UPLOAD_SIZE:
                await callback.message.answer(
                    f"⚠️ فایل حجیم است ({file_size // (1024 * 1024)}MB) - قابل ارسال نیست"
                )
                continue

            input_file = FSInputFile(str(file_path), filename=file_path.name)
            await callback.message.answer_audio(
                audio=input_file,
                caption=f"🎵 {title}\n🎼 {quality_label}\n☁️ SoundCloud",
            )

        await progress_msg.delete()

    except Exception as e:
        await progress_msg.edit_text(f"❌ خطا:\n{str(e)[:300]}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        context_data.pop(callback.from_user.id, None)
        # پاک کردن hash از context
        context_data.pop(f"sc_{url_hash}", None)


# ================= RUN =================
async def main():
    if not BOT_TOKEN:
        print("BOT_TOKEN تنظیم نشده!")
        return
    await dp.start_polling(bot)


if __name__ == "__main__":
    keep_alive()
    asyncio.run(main())
	