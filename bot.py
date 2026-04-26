"""
Telegram Reseller Shop Bot
Single-file implementation using python-telegram-bot (async).
Features: User login (phone share), Wallet, Dynamic Panels/Plans/Keys,
Orders, Transactions, Statistics, Support, Admin commands.
"""

import os
import io
import sqlite3
import logging
import asyncio
from datetime import datetime
from contextlib import contextmanager

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

def _parse_admin_ids():
    ids = []
    for k in ("ADMIN_ID_1", "ADMIN_ID_2", "ADMIN_ID"):
        v = os.environ.get(k, "").strip()
        if v:
            for part in v.split(","):
                part = part.strip()
                if part.isdigit():
                    ids.append(int(part))
    return list(dict.fromkeys(ids))  # dedupe preserve order

def _parse_usernames(*keys):
    names = []
    for k in keys:
        v = os.environ.get(k, "").strip()
        if v:
            for part in v.split(","):
                part = part.strip().lstrip("@")
                if part:
                    names.append(part)
    return list(dict.fromkeys(names))

ADMIN_IDS = _parse_admin_ids()
# Public-facing admin usernames (shown in Support / Contact buttons).
# ADMIN_USERNAME_1 is intentionally hidden from all user-facing UI.
PUBLIC_ADMIN_USERNAMES = _parse_usernames("ADMIN_USERNAME_2", "ADMIN_USERNAME")
_DEFAULT_ADMIN_USERNAME = PUBLIC_ADMIN_USERNAMES[0] if PUBLIC_ADMIN_USERNAMES else "admin"

def support_username() -> str:
    try:
        return get_setting("support_username", _DEFAULT_ADMIN_USERNAME) or _DEFAULT_ADMIN_USERNAME
    except Exception:
        return _DEFAULT_ADMIN_USERNAME

PRIMARY_ADMIN_USERNAME = _DEFAULT_ADMIN_USERNAME

BACKUP_CHAT_ID = os.environ.get("BACKUP_CHAT_ID", "").strip()
BACKUP_INTERVAL_MIN = int(os.environ.get("BACKUP_INTERVAL_MIN", "30") or 30)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("shopbot")

# ============================================================
# DATABASE
# ============================================================
# When ``DATABASE_URL`` is set, ``db_adapter`` transparently routes every
# query to PostgreSQL so data persists across redeploys. Otherwise it falls
# back to a local SQLite file at ``DB_PATH`` (default ``shop.db``).
from db_adapter import db, init_db, USE_PG, DB_PATH  # noqa: E402

def get_setting(key: str, default=None):
    with db() as c:
        r = c.execute("SELECT value FROM bot_settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default

def set_setting(key: str, value: str):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO bot_settings(key,value) VALUES(?,?)", (key, value))

def get_effective_price(uid: int, plan_id: int, base_price: float) -> float:
    with db() as c:
        r = c.execute("SELECT price FROM custom_pricing WHERE user_id=? AND plan_id=?",
                      (uid, plan_id)).fetchone()
    return float(r["price"]) if r else float(base_price)

# ============================================================
# HELPERS
# ============================================================
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def now_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def get_user(uid: int):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()

def back_button(callback="main_menu", text="⬅️ Back"):
    return InlineKeyboardButton(text, callback_data=callback)

def fmt_money(v) -> str:
    try:
        return f"{float(v):.2f}"
    except Exception:
        return "0.00"

def md_esc(value) -> str:
    """Escape characters that are special in legacy Telegram Markdown.

    The legacy Markdown parse mode treats `_`, `*`, `` ` `` and `[` as entity
    delimiters. Unescaped values coming from user-controlled fields (names,
    phone numbers, bot usernames, transaction types like `admin_add`, etc.)
    routinely break message rendering with `Can't parse entities` errors,
    which makes the corresponding button look like it does nothing.
    """
    if value is None:
        return ""
    s = str(value)
    for ch in ("\\", "_", "*", "`", "["):
        s = s.replace(ch, "\\" + ch)
    return s

# ============================================================
# UI: MAIN MENU
# ============================================================
def main_menu_text(user_row) -> str:
    name = user_row["name"] or "Friend"
    bal = fmt_money(user_row["balance"])
    spent = fmt_money(user_row["total_spent"])
    with db() as c:
        total_orders = c.execute(
            "SELECT COUNT(*) FROM orders WHERE user_id=?", (user_row["telegram_id"],)
        ).fetchone()[0]
    return (
        "🏪 *WELCOME TO SHOP*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"👋 Hello, *{name}*!\n\n"
        "━━ 💳 YOUR ACCOUNT ━━\n"
        f"├ 💰 Balance: *${bal}*\n"
        f"├ 🛍️ Purchases: *{total_orders}*\n"
        f"└ 💸 Total Spent: *${spent}*\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📝 Select an option below:"
    )

def main_menu_kb(uid: int = 0):
    rows = [
        [
            InlineKeyboardButton("🛒 Shop", callback_data="shop"),
            InlineKeyboardButton("📦 My Orders", callback_data="my_orders"),
        ],
        [
            InlineKeyboardButton("📊 My Statistics", callback_data="stats"),
            InlineKeyboardButton("💰 My Balance", callback_data="balance"),
        ],
        [
            InlineKeyboardButton("💳 Transactions", callback_data="transactions"),
            InlineKeyboardButton("💬 Support", callback_data="support"),
        ],
        [
            InlineKeyboardButton("👤 Profile", callback_data="profile"),
            InlineKeyboardButton("📁 Files", callback_data="pub_files"),
        ],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)

async def send_main_menu_to_chat(context: ContextTypes.DEFAULT_TYPE, uid: int):
    user_row = get_user(uid)
    if not user_row or user_row["status"] != "active":
        return
    await context.bot.send_message(
        chat_id=uid,
        text=main_menu_text(user_row),
        reply_markup=main_menu_kb(uid),
        parse_mode=ParseMode.MARKDOWN,
    )

async def send_main_menu(update_or_query, context: ContextTypes.DEFAULT_TYPE, uid: int):
    user_row = get_user(uid)
    if not user_row:
        await prompt_login(update_or_query, context)
        return
    if user_row["status"] == "pending" and not is_admin(uid):
        msg = ("⏳ *Account Pending Approval*\n━━━━━━━━━━━━━━━━━━\n"
               "📩 Your registration is awaiting admin review.\n🔔 You'll be notified once approved.")
        if hasattr(update_or_query, "message") and update_or_query.message:
            await update_or_query.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        else:
            await update_or_query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        return
    if user_row["status"] in ("banned", "rejected") and not is_admin(uid):
        msg = "🚫 Your account is no longer active. Contact support."
        if hasattr(update_or_query, "message") and update_or_query.message:
            await update_or_query.message.reply_text(msg)
        else:
            await update_or_query.edit_message_text(msg)
        return
    text = main_menu_text(user_row)
    kb = main_menu_kb(uid)
    if hasattr(update_or_query, "message") and update_or_query.message:
        await update_or_query.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    else:
        await update_or_query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

# ============================================================
# LOGIN FLOW
# ============================================================
async def prompt_login(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🔐🚪 *LOGIN REQUIRED* 🚪🔐\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "👋 Please log in to continue. 🙏✨"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Login 🔓", callback_data="login")]])
    if hasattr(update_or_query, "message") and update_or_query.message:
        await update_or_query.message.reply_text(
            text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update_or_query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    uid = update.effective_user.id
    # Make sure any old persistent reply keyboard (e.g. the legacy "🔄 Restart"
    # button) is removed for returning users.
    await update.message.reply_text(
        "👋🎉 Welcome! Use the menu below. ⬇️",
        reply_markup=ReplyKeyboardRemove(),
    )
    if get_user(uid):
        await send_main_menu(update, context, uid)
    else:
        await prompt_login(update, context)

async def on_login_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Share Phone Number 📲", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await q.message.reply_text(
        "📱🔢 Please share your phone number to log in. 🔐",
        reply_markup=kb,
    )
    context.user_data["awaiting_contact"] = True

async def on_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact
    uid = update.effective_user.id
    if not contact or contact.user_id != uid:
        await update.message.reply_text(
            "❌🚫 Phone validation failed! You must share your OWN phone number. 📵",
        )
        return
    name = (update.effective_user.full_name or "User").strip()
    username = update.effective_user.username or "—"
    phone = contact.phone_number
    is_new = False
    with db() as c:
        existing = c.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()
        if not existing:
            is_new = True
            initial_status = "active" if is_admin(uid) else "pending"
            c.execute(
                "INSERT INTO users (telegram_id,name,phone,join_date,status) VALUES (?,?,?,?,?)",
                (uid, name, phone, now_str(), initial_status),
            )
        else:
            c.execute("UPDATE users SET name=?, phone=? WHERE telegram_id=?", (name, phone, uid))
    context.user_data.pop("awaiting_contact", None)

    user_row = get_user(uid)
    if user_row["status"] == "pending":
        await update.message.reply_text(
            "⏳ *Registration received*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Your account is pending admin approval.\n"
            "You'll be notified once approved.",
            parse_mode=ParseMode.MARKDOWN,
        )
        if is_new:
            await notify_admins_new_user(context, uid, name, username, phone)
        return
    if user_row["status"] == "banned":
        await update.message.reply_text("🚫 Your account is banned.")
        return
    await update.message.reply_text("✅ Login successful")
    await send_main_menu(update, context, uid)

async def on_admin_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    if context.user_data.get("flow") != "file_upload":
        return
    msg = update.message
    file_id = None; name = None; mime = None
    if msg.document:
        file_id = msg.document.file_id
        name = msg.document.file_name or f"document_{msg.document.file_unique_id}"
        mime = msg.document.mime_type
    elif msg.photo:
        ph = msg.photo[-1]
        file_id = ph.file_id; name = f"photo_{ph.file_unique_id}.jpg"; mime = "image/jpeg"
    elif msg.video:
        file_id = msg.video.file_id
        name = msg.video.file_name or f"video_{msg.video.file_unique_id}.mp4"
        mime = msg.video.mime_type
    elif msg.audio:
        file_id = msg.audio.file_id
        name = msg.audio.file_name or f"audio_{msg.audio.file_unique_id}.mp3"
        mime = msg.audio.mime_type
    elif msg.voice:
        file_id = msg.voice.file_id; name = f"voice_{msg.voice.file_unique_id}.ogg"; mime = "audio/ogg"
    if not file_id:
        return
    with db() as c:
        c.execute("INSERT INTO files(name,file_id,mime,uploaded_by,date) VALUES(?,?,?,?,?)",
                  (name, file_id, mime, uid, now_str()))
    context.user_data.pop("flow", None)
    await msg.reply_text(f"✅ Stored *{name}*", parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📁 Open All Files", callback_data="adm_files")]]))

async def notify_admins_new_user(context, uid, name, username, phone):
    user_row = get_user(uid)
    join = user_row["join_date"] if user_row else now_str()
    text = (
        "🆕 *New User Registered*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Name: *{name}*\n"
        f"Username: @{username}\n"
        f"Phone: `{phone}`\n"
        f"ID: `{uid}`\n"
        f"Joined: {join}"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"usr_appr:{uid}"),
            InlineKeyboardButton("🚫 Reject", callback_data=f"usr_rej:{uid}"),
        ],
        [InlineKeyboardButton("👤 View Details", callback_data=f"usr_view:{uid}")],
    ])
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.warning("notify_admins_new_user failed for %s: %s", admin_id, e)

# ============================================================
# CALLBACK ROUTING
# ============================================================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id
    data = q.data or ""

    if data == "login":
        await on_login_click(update, context)
        return

    # Require login
    user_row = get_user(uid)
    if not user_row:
        await q.answer("Please log in first.", show_alert=True)
        await prompt_login(q, context)
        return
    if user_row["status"] == "banned" and not is_admin(uid):
        await q.answer("Your account has been banned.", show_alert=True)
        return
    if user_row["status"] == "pending" and not is_admin(uid):
        await q.answer("Your account is pending admin approval.", show_alert=True)
        return
    if user_row["status"] == "rejected" and not is_admin(uid):
        await q.answer("Your registration was rejected.", show_alert=True)
        return

    await q.answer()

    if data == "main_menu":
        await send_main_menu(q, context, uid)
    elif data == "shop":
        await show_platforms(q)
    elif data.startswith("plat:"):
        await show_panels(q, data.split(":", 1)[1])
    elif data.startswith("panel:"):
        await show_plans(q, int(data.split(":", 1)[1]))
    elif data.startswith("plan:"):
        await show_plan_detail(q, int(data.split(":", 1)[1]))
    elif data.startswith("buy:"):
        await do_buy(q, uid, int(data.split(":", 1)[1]))
    elif data == "my_orders":
        await show_orders(q, uid)
    elif data == "stats":
        await show_stats(q, uid)
    elif data == "profile":
        await show_profile(q)
    elif data == "balance":
        await show_balance(q, uid)
    elif data == "transactions":
        await show_transactions(q, uid)
    elif data == "support":
        await show_support(q)
    # Public files (admin uploads them, anyone can browse/download)
    elif data == "pub_files":
        await show_pub_files(q)
    elif data.startswith("pub_file_get:"):
        fid = int(data.split(":", 1)[1])
        with db() as c:
            r = c.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if not r:
            await q.answer("Not found.", show_alert=True); return
        try:
            await context.bot.send_document(
                chat_id=uid, document=r["file_id"], caption=f"📁 {r['name']}"
            )
        except Exception:
            try:
                await context.bot.send_photo(
                    chat_id=uid, photo=r["file_id"], caption=f"📁 {r['name']}"
                )
            except Exception:
                try:
                    await context.bot.send_video(
                        chat_id=uid, video=r["file_id"], caption=f"📁 {r['name']}"
                    )
                except Exception:
                    try:
                        await context.bot.send_audio(
                            chat_id=uid, audio=r["file_id"], caption=f"📁 {r['name']}"
                        )
                    except Exception as e:
                        await q.answer(f"Failed: {e}", show_alert=True); return
        await q.answer("Sent.")
    # Admin actions
    elif data == "admin_panel":
        if not is_admin(uid):
            await q.answer("Not authorized.", show_alert=True)
            return
        await show_admin_panel(q)
    elif data == "adm_help":
        if not is_admin(uid):
            await q.answer("Not authorized.", show_alert=True)
            return
        await show_admin_help_inline(q)
    # ===== New admin submenus =====
    elif data == "adm_keys":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await show_adm_keys(q)
    elif data == "adm_search":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await show_adm_search(q)
    elif data == "adm_settings":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await show_adm_settings(q)
    elif data == "adm_pending":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await show_adm_pending(q)
    elif data == "adm_products":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await show_adm_products(q)
    elif data.startswith("adm_prod:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await show_adm_product_detail(q, int(data.split(":", 1)[1]))
    elif data == "adm_balance":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await show_adm_balance_menu(q)
    elif data == "adm_stats":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await show_adm_stats(q)
    elif data == "adm_pricing":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await show_adm_pricing(q)
    elif data == "adm_pricing_add":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        context.user_data["flow"] = "pricing_uid"
        await q.edit_message_text(
            "💎 *Set Custom Price* — Step 1/3\n\nSend the *User ID*.\n/cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[back_button("adm_pricing")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data.startswith("adm_pricing_del:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        _, tuid, tpid = data.split(":")
        with db() as c:
            c.execute("DELETE FROM custom_pricing WHERE user_id=? AND plan_id=?", (int(tuid), int(tpid)))
        await q.answer("Removed.")
        await show_adm_pricing(q)
    elif data == "adm_access":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await show_adm_access(q)
    elif data == "adm_ban_uid":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        context.user_data["flow"] = "ban_uid"
        await q.edit_message_text(
            "🚫 Send the *User ID* to ban.\n/cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[back_button("adm_access")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "adm_reset":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await show_adm_reset(q)
    elif data == "adm_reset_sold":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        with db() as c:
            n = c.execute("UPDATE keys SET is_sold=0 WHERE is_sold=1").rowcount
        await q.answer(f"Reset {n} keys.", show_alert=True)
        await show_adm_reset(q)
    elif data == "adm_reset_orders":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        with db() as c:
            n = c.execute("DELETE FROM orders").rowcount
        await q.answer(f"Deleted {n} orders.", show_alert=True)
        await show_adm_reset(q)
    elif data == "adm_reset_txns":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        with db() as c:
            n = c.execute("DELETE FROM transactions").rowcount
        await q.answer(f"Deleted {n} transactions.", show_alert=True)
        await show_adm_reset(q)
    elif data == "adm_reset_bal":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        with db() as c:
            c.execute("UPDATE users SET balance=0, total_deposit=0, total_spent=0")
        await q.answer("All balances reset.", show_alert=True)
        await show_adm_reset(q)
    elif data == "adm_files":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await show_adm_files(q, context)
    elif data == "adm_file_upload":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        context.user_data["flow"] = "file_upload"
        await q.edit_message_text(
            "📤 *Upload File*\n\nSend any document, photo, video or audio now.\nIt will be stored and downloadable from this menu.\n\n/cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[back_button("adm_files")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "adm_file_db":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        if USE_PG:
            await q.answer(
                "DB backup-to-Telegram is disabled when using PostgreSQL. "
                "Use your hosting provider's backup tools instead.",
                show_alert=True,
            )
            return
        try:
            with open(DB_PATH, "rb") as f:
                await context.bot.send_document(chat_id=uid, document=f,
                    filename="shop.db", caption="💾 Database backup")
            await q.answer("Sent.", show_alert=False)
        except Exception as e:
            await q.answer(f"Failed: {e}", show_alert=True)
    elif data.startswith("adm_file_get:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        fid = int(data.split(":", 1)[1])
        with db() as c:
            r = c.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if not r:
            await q.answer("Not found.", show_alert=True); return
        try:
            await context.bot.send_document(chat_id=uid, document=r["file_id"],
                caption=f"📁 {r['name']}")
        except Exception:
            try:
                await context.bot.send_photo(chat_id=uid, photo=r["file_id"], caption=f"📁 {r['name']}")
            except Exception as e:
                await q.answer(f"Failed: {e}", show_alert=True); return
        await q.answer("Sent.")
    elif data.startswith("adm_file_del:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        fid = int(data.split(":", 1)[1])
        with db() as c:
            c.execute("DELETE FROM files WHERE id=?", (fid,))
        await q.answer("Deleted.")
        await show_adm_files(q, context)
    elif data == "adm_view_keys":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await show_adm_keys_pick_plan(q, "view")
    elif data == "adm_export_keys":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await show_adm_keys_pick_plan(q, "export")
    elif data == "adm_del_keys":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await show_adm_keys_pick_plan(q, "del")
    elif data.startswith("adm_kview:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await adm_view_plan_keys(q, int(data.split(":", 1)[1]))
    elif data.startswith("adm_kexport:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await adm_export_plan_keys(q, context, int(data.split(":", 1)[1]))
    elif data.startswith("adm_kdel:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await adm_del_plan_keys_menu(q, int(data.split(":", 1)[1]))
    elif data.startswith("adm_kdelu:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        pid = int(data.split(":", 1)[1])
        with db() as c:
            n = c.execute("DELETE FROM keys WHERE plan_id=? AND is_sold=0", (pid,)).rowcount
        await q.answer(f"Deleted {n} unsold.", show_alert=True)
        await adm_del_plan_keys_menu(q, pid)
    elif data.startswith("adm_kdels:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        pid = int(data.split(":", 1)[1])
        with db() as c:
            n = c.execute("DELETE FROM keys WHERE plan_id=? AND is_sold=1", (pid,)).rowcount
        await q.answer(f"Deleted {n} sold.", show_alert=True)
        await adm_del_plan_keys_menu(q, pid)
    elif data.startswith("adm_kdela:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        pid = int(data.split(":", 1)[1])
        with db() as c:
            n = c.execute("DELETE FROM keys WHERE plan_id=?", (pid,)).rowcount
        await q.answer(f"Deleted {n} keys.", show_alert=True)
        await adm_del_plan_keys_menu(q, pid)
    elif data.startswith("adm_plans_mgr:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await show_adm_plans_manager(q, int(data.split(":", 1)[1]))
    elif data.startswith("adm_del_plan:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        plan_id = int(data.split(":", 1)[1])
        with db() as c:
            row = c.execute("SELECT panel_id FROM plans WHERE id=?", (plan_id,)).fetchone()
            c.execute("DELETE FROM keys WHERE plan_id=?", (plan_id,))
            c.execute("DELETE FROM plans WHERE id=?", (plan_id,))
        await q.answer("Plan deleted.", show_alert=True)
        if row:
            await show_adm_plans_manager(q, row["panel_id"])
        else:
            await show_adm_products(q)
    elif data == "adm_set_support":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        context.user_data["flow"] = "set_support"
        await q.edit_message_text(
            f"📞 *Set Support Username*\n\nCurrent: @{support_username()}\n\nSend the new username (without @).\n/cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[back_button("adm_settings")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "adm_db_cleanup":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await show_adm_db_cleanup(q)
    elif data == "adm_vacuum":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("VACUUM")
            await q.answer("✅ Vacuumed.", show_alert=True)
        except Exception as e:
            await q.answer(f"Failed: {e}", show_alert=True)
        await show_adm_db_cleanup(q)
    elif data == "adm_clean_sold":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        with db() as c:
            n = c.execute("DELETE FROM keys WHERE is_sold=1").rowcount
        await q.answer(f"Deleted {n} sold keys.", show_alert=True)
        await show_adm_db_cleanup(q)
    elif data == "adm_clean_rej":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        with db() as c:
            n = c.execute("DELETE FROM users WHERE status='rejected'").rowcount
        await q.answer(f"Deleted {n} users.", show_alert=True)
        await show_adm_db_cleanup(q)
    elif data.startswith("adm_del_prod:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        pid = int(data.split(":", 1)[1])
        with db() as c:
            c.execute("DELETE FROM panels WHERE id=?", (pid,))
        await q.edit_message_text(
            f"✅ Product `{pid}` deleted.",
            reply_markup=InlineKeyboardMarkup([[back_button("adm_products", "⬅️ Back")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "adm_search_uid":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        context.user_data["flow"] = "search_uid"
        await q.edit_message_text(
            "🔍 Send the *User ID* (numeric) to search.\n\n/cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[back_button("adm_search")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "adm_search_uname":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        context.user_data["flow"] = "search_uname"
        await q.edit_message_text(
            "🔍 Send the *Name* (or part of it) to search.\n\n/cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[back_button("adm_search")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "adm_search_key":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        context.user_data["flow"] = "search_key"
        await q.edit_message_text(
            "🔍 Send the *Key* (or part of it) to search.\n\n/cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[back_button("adm_search")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
    # ===== User approval =====
    elif data.startswith("usr_appr:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        target = int(data.split(":", 1)[1])
        with db() as c:
            c.execute("UPDATE users SET status='active' WHERE telegram_id=?", (target,))
        try:
            await context.bot.send_message(
                chat_id=target,
                text="✅ *Your account has been approved*\nYou can now use the bot.",
                parse_mode=ParseMode.MARKDOWN,
            )
            await send_main_menu_to_chat(context, target)
        except Exception:
            pass
        await q.answer("Approved ✅")
        try:
            await q.edit_message_text(
                (q.message.text or "") + f"\n\n✅ Approved by {update.effective_user.full_name}",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
    elif data.startswith("usr_rej:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        target = int(data.split(":", 1)[1])
        if is_admin(target):
            await q.answer("Cannot reject an admin.", show_alert=True); return
        with db() as c:
            cur = c.execute("SELECT status FROM users WHERE telegram_id=?", (target,)).fetchone()
            new_status = "banned" if (cur and cur["status"] == "active") else "rejected"
            c.execute("UPDATE users SET status=? WHERE telegram_id=?", (new_status, target))
        try:
            msg = ("🚫 You have been banned by an admin." if new_status == "banned"
                   else "❌ Your registration was rejected. Contact support if needed.")
            await context.bot.send_message(chat_id=target, text=msg)
        except Exception:
            pass
        await q.answer(f"{'Banned' if new_status=='banned' else 'Rejected'}.")
        try:
            await q.edit_message_text(
                (q.message.text or "") + f"\n\n🚫 {new_status.title()} by {update.effective_user.full_name}",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
    elif data.startswith("usr_view:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await show_user_details(q, int(data.split(":", 1)[1]))
    elif data == "usr_appr_all":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        with db() as c:
            pending = c.execute("SELECT telegram_id FROM users WHERE status='pending'").fetchall()
            c.execute("UPDATE users SET status='active' WHERE status='pending'")
        notified = 0
        for r in pending:
            try:
                await context.bot.send_message(
                    chat_id=r["telegram_id"],
                    text="✅🎉 *Your account has been APPROVED!* 🎊\n\nTap /start to begin.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                notified += 1
            except Exception:
                pass
        await q.edit_message_text(
            f"✅ Approved *{len(pending)}* users ({notified} notified).",
            reply_markup=InlineKeyboardMarkup([[back_button("admin_panel", "⬅️ Back to Menu")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "adm_broadcast":
        if not is_admin(uid):
            await q.answer("Not authorized.", show_alert=True)
            return
        context.user_data["awaiting_broadcast"] = True
        await q.edit_message_text(
            "📢 Send the broadcast message now (or /cancel).",
            reply_markup=InlineKeyboardMarkup([[back_button("admin_panel")]]),
        )
    # ===== Add Panel flow =====
    elif data == "adm_add_panel":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        context.user_data["flow"] = "panel_name"
        await q.edit_message_text(
            "📝 *Add Panel — Step 1/2*\nSend the panel *name* (e.g. `Fluorite`).\n\n/cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[back_button("admin_panel")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data.startswith("adm_pp:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        platform = data.split(":", 1)[1]
        name = context.user_data.pop("panel_name_value", None)
        context.user_data.pop("flow", None)
        if not name or platform not in PLATFORMS:
            await q.edit_message_text("❌ Flow lost. Try again.",
                reply_markup=InlineKeyboardMarkup([[back_button("admin_panel")]])); return
        with db() as c:
            cur = c.execute("INSERT INTO panels (name, platform) VALUES (?,?)", (name, platform))
            pid = cur.lastrowid
        await q.edit_message_text(
            f"✅ Panel created!\n\n📦 *{name}* ({platform})\nID: `{pid}`",
            reply_markup=InlineKeyboardMarkup([[back_button("admin_panel")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
    # ===== Add Plan flow =====
    elif data == "adm_add_plan":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await _show_panel_picker(q, "adm_pl_pick", title="➕ *Add Plan* — pick a panel:")
    elif data.startswith("adm_pl_pick:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        panel_id = int(data.split(":", 1)[1])
        context.user_data["flow"] = f"plan_name:{panel_id}"
        await q.edit_message_text(
            "📝 *Add Plan — Step 2/3*\nSend the plan *name* (e.g. `30 Days`).\n\n/cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[back_button("adm_add_plan")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
    # ===== Add Keys flow =====
    elif data == "adm_add_keys":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await _show_plan_picker(q, "adm_kk_pick", title="🔑 *Add Keys* — pick a plan:")
    elif data.startswith("adm_kk_pick:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        plan_id = int(data.split(":", 1)[1])
        context.user_data["flow"] = f"keys_paste:{plan_id}"
        await q.edit_message_text(
            f"📥 Paste keys for plan `{plan_id}` (one per line).\n\n/cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[back_button("adm_add_keys")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
    # ===== Stock view =====
    elif data == "adm_stock":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await _show_plan_picker(q, "adm_st_pick", title="📦 *Stock* — pick a plan:")
    elif data.startswith("adm_st_pick:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        plan_id = int(data.split(":", 1)[1])
        with db() as c:
            plan = c.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
            unsold = c.execute("SELECT COUNT(*) FROM keys WHERE plan_id=? AND is_sold=0", (plan_id,)).fetchone()[0]
            sold = c.execute("SELECT COUNT(*) FROM keys WHERE plan_id=? AND is_sold=1", (plan_id,)).fetchone()[0]
        await q.edit_message_text(
            f"📦 *{plan['name']}* (id `{plan_id}`)\nAvailable: *{unsold}*\nSold: *{sold}*",
            reply_markup=InlineKeyboardMarkup([[back_button("adm_stock")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
    # ===== Pick-user balance flows =====
    elif data == "adm_addbal_pick":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        context.user_data["flow"] = "addbal_uid"
        await q.edit_message_text(
            "➕ *Add Balance — Step 1/2*\nSend the *user_id* (numeric).\n\n/cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[back_button("admin_panel")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "adm_dedbal_pick":
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        context.user_data["flow"] = "dedbal_uid"
        await q.edit_message_text(
            "➖ *Deduct Balance — Step 1/2*\nSend the *user_id* (numeric).\n\n/cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[back_button("admin_panel")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "adm_users_mgmt" or data.startswith("adm_users:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        await show_users_management(q)
    elif data.startswith("adm_users_list:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        _, kind, page = data.split(":")
        await show_users_list(q, kind, int(page))
    elif data.startswith("txn_appr:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        order_id = data.split(":", 1)[1]
        try:
            await q.edit_message_text(
                q.message.text + f"\n\n✅ *Approved by admin* ({update.effective_user.full_name})",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            await q.answer("Approved.")
    elif data.startswith("txn_ban:"):
        if not is_admin(uid): await q.answer("Not authorized.", show_alert=True); return
        _, target_s, order_id = data.split(":", 2)
        target = int(target_s)
        with db() as c:
            c.execute("UPDATE users SET status='banned' WHERE telegram_id=?", (target,))
        try:
            await context.bot.send_message(
                chat_id=target,
                text="🚫 Your account has been banned by an admin. Contact support if you believe this is a mistake.",
            )
        except Exception:
            pass
        try:
            await q.edit_message_text(
                q.message.text + f"\n\n🚫 *User `{target}` has been BANNED* by {update.effective_user.full_name}",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            await q.answer("Banned.")
    elif data.startswith("adm_addbal:"):
        target = int(data.split(":", 1)[1])
        context.user_data["admin_action"] = ("add", target)
        await q.edit_message_text(
            f"Send the amount to ADD to user `{target}`'s balance.",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data.startswith("adm_dedbal:"):
        target = int(data.split(":", 1)[1])
        context.user_data["admin_action"] = ("deduct", target)
        await q.edit_message_text(
            f"Send the amount to DEDUCT from user `{target}`'s balance.",
            parse_mode=ParseMode.MARKDOWN,
        )

# ============================================================
# SHOP FLOW
# ============================================================
PLATFORMS = ["Android", "iOS", "Windows"]
PLATFORM_EMOJI = {"Android": "🤖", "iOS": "🍏", "Windows": "🪟"}

async def show_platforms(q):
    kb = [[InlineKeyboardButton(f"{PLATFORM_EMOJI.get(p,'📱')} {p}", callback_data=f"plat:{p}")] for p in PLATFORMS]
    kb.append([back_button("main_menu")])
    await q.edit_message_text(
        "🛒🛍️ *SHOP* 🛍️🛒\n━━━━━━━━━━━━━━━━━━\n📱✨ Select Platform 👇",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN,
    )

async def show_panels(q, platform: str):
    with db() as c:
        rows = c.execute("SELECT * FROM panels WHERE platform=? ORDER BY name", (platform,)).fetchall()
    kb = [[InlineKeyboardButton(f"📦 {r['name']}", callback_data=f"panel:{r['id']}")] for r in rows]
    kb.append([back_button("shop")])
    text = f"📦🗂️ *Select Panel* — {PLATFORM_EMOJI.get(platform,'📱')} {platform}\n━━━━━━━━━━━━━━━━━━"
    if not rows:
        text += "\n\n📭😔 No panels available for this platform yet."
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def show_plans(q, panel_id: int):
    with db() as c:
        panel = c.execute("SELECT * FROM panels WHERE id=?", (panel_id,)).fetchone()
        if not panel:
            await q.edit_message_text("❌ Panel not found.", reply_markup=InlineKeyboardMarkup([[back_button("shop")]]))
            return
        plans = c.execute("SELECT * FROM plans WHERE panel_id=? ORDER BY price", (panel_id,)).fetchall()
    kb = []
    lines = [f"📋✨ *Select Plan* — 📦 {panel['name']}", "━━━━━━━━━━━━━━━━━━", ""]
    if not plans:
        lines.append("📭😔 No plans available yet.")
    for p in plans:
        with db() as c:
            stock = c.execute(
                "SELECT COUNT(*) FROM keys WHERE plan_id=? AND is_sold=0", (p["id"],)
            ).fetchone()[0]
        stock_emoji = "✅" if stock > 0 else "🔴"
        lines.append(f"🔹 *{p['name']}* — 💵 ${fmt_money(p['price'])} {stock_emoji} Stock: {stock}")
        kb.append([InlineKeyboardButton(f"📋 {p['name']} • 💵 ${fmt_money(p['price'])}", callback_data=f"plan:{p['id']}")])
    kb.append([back_button(f"plat:{panel['platform']}")])
    await q.edit_message_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN
    )

async def show_plan_detail(q, plan_id: int):
    with db() as c:
        plan = c.execute("SELECT p.*, pa.name AS panel_name, pa.platform AS platform, pa.id AS panel_id FROM plans p JOIN panels pa ON pa.id=p.panel_id WHERE p.id=?", (plan_id,)).fetchone()
        if not plan:
            await q.edit_message_text("❌ Plan not found.", reply_markup=InlineKeyboardMarkup([[back_button("shop")]]))
            return
        stock = c.execute("SELECT COUNT(*) FROM keys WHERE plan_id=? AND is_sold=0", (plan_id,)).fetchone()[0]
    eff = get_effective_price(q.from_user.id, plan_id, plan["price"])
    price_line = f"💵 Price: *${fmt_money(eff)}*"
    if eff != plan["price"]:
        price_line += f" 💎 _(VIP, was ${fmt_money(plan['price'])})_"
    text = (
        f"📋✨ *{plan['name']}* ✨📋\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{PLATFORM_EMOJI.get(plan['platform'],'📱')} Platform: *{plan['platform']}*\n"
        f"📦 Panel: *{plan['panel_name']}*\n"
        f"{price_line}\n"
        f"{'✅' if stock > 0 else '🔴'} Stock: *{stock}*\n"
    )
    kb = [
        [InlineKeyboardButton("✅💸 Buy Now 🛒", callback_data=f"buy:{plan_id}")],
        [back_button(f"panel:{plan['panel_id']}")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

# ============================================================
# BUY (ATOMIC)
# ============================================================
_buy_lock = asyncio.Lock()

async def do_buy(q, uid: int, plan_id: int):
    async with _buy_lock:
        with db() as c:
            try:
                c.execute("BEGIN IMMEDIATE")
                user = c.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()
                plan = c.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
                if not plan:
                    c.execute("ROLLBACK")
                    await q.edit_message_text("❌ Plan not found.",
                        reply_markup=InlineKeyboardMarkup([[back_button("shop")]]))
                    return
                effective_price = get_effective_price(uid, plan_id, plan["price"])
                if user["balance"] < effective_price:
                    c.execute("ROLLBACK")
                    await q.edit_message_text(
                        f"❌💸 *Insufficient Balance* 😔\nNeeded: 💵 ${fmt_money(effective_price)}\nYour balance: 💰 ${fmt_money(user['balance'])}",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("💰 My Balance", callback_data="balance")],
                            [back_button(f"plan:{plan_id}")],
                        ]),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return
                key_row = c.execute(
                    "SELECT id, key FROM keys WHERE plan_id=? AND is_sold=0 LIMIT 1", (plan_id,)
                ).fetchone()
                if not key_row:
                    c.execute("ROLLBACK")
                    await q.edit_message_text(
                        "❌🔴 *Out of stock* for this plan! 😢 Please try again later. ⏳",
                        reply_markup=InlineKeyboardMarkup([[back_button(f"plan:{plan_id}")]]),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return
                # Mark sold (atomic check)
                upd = c.execute(
                    "UPDATE keys SET is_sold=1 WHERE id=? AND is_sold=0", (key_row["id"],)
                )
                if upd.rowcount == 0:
                    c.execute("ROLLBACK")
                    await q.edit_message_text(
                        "❌ That key was just taken. Please try again.",
                        reply_markup=InlineKeyboardMarkup([[back_button(f"plan:{plan_id}")]]),
                    )
                    return
                c.execute(
                    "UPDATE users SET balance=balance-?, total_spent=total_spent+? WHERE telegram_id=?",
                    (effective_price, effective_price, uid),
                )
                c.execute(
                    "INSERT INTO orders (user_id, plan_id, key, date) VALUES (?,?,?,?)",
                    (uid, plan_id, key_row["key"], now_str()),
                )
                c.execute(
                    "INSERT INTO transactions (user_id, amount, type, date) VALUES (?,?,?,?)",
                    (uid, -float(effective_price), "purchase", now_str()),
                )
                c.execute("COMMIT")
            except Exception as e:
                try:
                    c.execute("ROLLBACK")
                except Exception:
                    pass
                logger.exception("buy failed: %s", e)
                await q.edit_message_text(
                    "❌ An error occurred during purchase. Please try again.",
                    reply_markup=InlineKeyboardMarkup([[back_button("shop")]]),
                )
                return

    text = (
        "✅🎉 *Purchase Successful!* 🎊✨\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔑✨ Your Key:\n"
        f"`{key_row['key']}`\n\n"
        "🙏 Thank you for your purchase! 💖"
    )
    kb = [
        [InlineKeyboardButton("📦📋 My Orders", callback_data="my_orders")],
        [back_button("main_menu")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

    # Get the freshly inserted order id and notify all admins
    with db() as c:
        order = c.execute(
            "SELECT id FROM orders WHERE user_id=? AND key=? ORDER BY id DESC LIMIT 1",
            (uid, key_row["key"]),
        ).fetchone()
        u = c.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()
    order_id = order["id"] if order else 0
    notif_text = (
        "🛎️🚨 *NEW PURCHASE* 🚨🛎️\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Order #*{order_id}*\n"
        f"👤 {u['name']} (`{uid}`)\n"
        f"📱 {u['phone']}\n"
        f"📦 {plan['name']} — 💵 ${fmt_money(plan['price'])}\n"
        f"🔑 `{key_row['key']}`\n"
        f"💰 Balance after: ${fmt_money(u['balance'])}\n"
        f"📅 {now_str()}"
    )
    notif_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅👍 Approve", callback_data=f"txn_appr:{order_id}"),
        InlineKeyboardButton("🚫⛔ Ban User", callback_data=f"txn_ban:{uid}:{order_id}"),
    ]])
    for admin_id in ADMIN_IDS:
        try:
            await q.get_bot().send_message(
                chat_id=admin_id,
                text=notif_text,
                reply_markup=notif_kb,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.warning("admin notify failed for %s: %s", admin_id, e)

# ============================================================
# MY ORDERS
# ============================================================
async def show_orders(q, uid: int):
    with db() as c:
        rows = c.execute(
            """SELECT o.*, pl.name AS plan_name, pa.name AS panel_name, pa.platform AS platform
               FROM orders o
               JOIN plans pl ON pl.id=o.plan_id
               JOIN panels pa ON pa.id=pl.panel_id
               WHERE o.user_id=? ORDER BY o.id DESC LIMIT 50""",
            (uid,),
        ).fetchall()
    if not rows:
        await q.edit_message_text(
            "📭😔 No orders yet! 🛒 Start shopping to see your purchases here. ✨",
            reply_markup=InlineKeyboardMarkup([[back_button("main_menu")]]),
        )
        return
    lines = ["📦📋 *MY ORDERS* 📋📦", "━━━━━━━━━━━━━━━━━━"]
    for r in rows:
        lines.append(
            f"\n🆔 #{r['id']} • 📅 {r['date']}\n"
            f"{PLATFORM_EMOJI.get(r['platform'],'📱')} {r['platform']} → 📦 {r['panel_name']} → 📋 {r['plan_name']}\n"
            f"🔑 `{r['key']}`"
        )
    await q.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([[back_button("main_menu")]]),
        parse_mode=ParseMode.MARKDOWN,
    )

# ============================================================
# STATISTICS
# ============================================================
async def show_stats(q, uid: int):
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()
        total_orders = c.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (uid,)).fetchone()[0]
        unique_panels = c.execute(
            """SELECT COUNT(DISTINCT pa.id) FROM orders o
               JOIN plans pl ON pl.id=o.plan_id
               JOIN panels pa ON pa.id=pl.panel_id
               WHERE o.user_id=?""",
            (uid,),
        ).fetchone()[0]
        first = c.execute("SELECT MIN(date) FROM orders WHERE user_id=?", (uid,)).fetchone()[0]
        last = c.execute("SELECT MAX(date) FROM orders WHERE user_id=?", (uid,)).fetchone()[0]
    status_emoji = "✅" if u["status"] == "active" else "🚫"
    text = (
        "📊📈 *MY STATISTICS* 📈📊\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "━━ 👤 ACCOUNT 👤 ━━\n"
        f"🏷️ Name: *{u['name']}*\n"
        f"🆔 Telegram ID: `{u['telegram_id']}`\n"
        f"📱 Phone: {u['phone']}\n"
        f"{status_emoji} Status: {u['status']}\n"
        f"📅 Join Date: {u['join_date']}\n\n"
        "━━ 💰 FINANCIAL 💰 ━━\n"
        f"💵 Balance: *${fmt_money(u['balance'])}*\n"
        f"📥 Total Deposited: ${fmt_money(u['total_deposit'])}\n"
        f"💸 Total Spent: ${fmt_money(u['total_spent'])}\n\n"
        "━━ 🛒 PURCHASES 🛒 ━━\n"
        f"📦 Total Orders: *{total_orders}*\n"
        f"🔑 Total Keys: *{total_orders}*\n"
        f"🗂️ Unique Panels: *{unique_panels}*\n"
        f"🥇 First Purchase: {first or '—'}\n"
        f"🕒 Last Purchase: {last or '—'}\n"
    )
    await q.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[back_button("main_menu")]]),
        parse_mode=ParseMode.MARKDOWN,
    )

# ============================================================
# BALANCE
# ============================================================
async def show_balance(q, uid: int):
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()
        total_orders = c.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (uid,)).fetchone()[0]
        last = c.execute("SELECT MAX(date) FROM orders WHERE user_id=?", (uid,)).fetchone()[0]
    text = (
        "💰💵 *MY BALANCE* 💵💰\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💵 Balance: *${fmt_money(u['balance'])}*\n"
        f"💸 Total Spent: ${fmt_money(u['total_spent'])}\n"
        f"🛍️ Purchases: {total_orders}\n"
        f"🕒 Last Purchase: {last or '—'}\n"
    )
    kb = [
        [InlineKeyboardButton("➕💬 Contact Admin to Top Up 💰", url=f"https://t.me/{support_username()}")],
        [back_button("main_menu")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

# ============================================================
# TRANSACTIONS
# ============================================================
async def show_transactions(q, uid: int):
    with db() as c:
        rows = c.execute(
            "SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 50",
            (uid,),
        ).fetchall()
    if not rows:
        await q.edit_message_text(
            "📭😔 No transactions yet! 💳",
            reply_markup=InlineKeyboardMarkup([[back_button("main_menu")]]),
        )
        return
    lines = ["💳🧾 *TRANSACTIONS* 🧾💳", "━━━━━━━━━━━━━━━━━━"]
    for r in rows:
        sign = "➕💚" if r["amount"] >= 0 else "➖💔"
        lines.append(f"{sign} ${fmt_money(abs(r['amount']))} • 🏷️ {r['type']} • 📅 {r['date']}")
    await q.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([[back_button("main_menu")]]),
        parse_mode=ParseMode.MARKDOWN,
    )

# ============================================================
# SUPPORT
# ============================================================
async def _show_panel_picker(q, prefix: str, title: str):
    with db() as c:
        rows = c.execute("SELECT * FROM panels ORDER BY platform, name").fetchall()
    kb = []
    for r in rows:
        kb.append([InlineKeyboardButton(
            f"{r['platform']} • {r['name']} (id {r['id']})",
            callback_data=f"{prefix}:{r['id']}"
        )])
    if not rows:
        title += "\n\n📭 No panels yet. Use ➕ Add Panel first."
    kb.append([back_button("admin_panel")])
    await q.edit_message_text(title, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def _show_plan_picker(q, prefix: str, title: str):
    with db() as c:
        rows = c.execute(
            """SELECT pl.id, pl.name, pl.price, pa.name AS panel_name, pa.platform
               FROM plans pl JOIN panels pa ON pa.id=pl.panel_id
               ORDER BY pa.platform, pa.name, pl.price"""
        ).fetchall()
    kb = []
    for r in rows:
        kb.append([InlineKeyboardButton(
            f"{r['platform']} • {r['panel_name']} → {r['name']} (${fmt_money(r['price'])})",
            callback_data=f"{prefix}:{r['id']}"
        )])
    if not rows:
        title += "\n\n📭 No plans yet. Use ➕ Add Plan first."
    kb.append([back_button("admin_panel")])
    await q.edit_message_text(title, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def show_admin_panel(q):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with db() as c:
        total_users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        approved_users = c.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0]
        pending_users = c.execute("SELECT COUNT(*) FROM users WHERE status='pending'").fetchone()[0]
        total_panels = c.execute("SELECT COUNT(*) FROM panels").fetchone()[0]
        total_plans = c.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
        total_keys = c.execute("SELECT COUNT(*) FROM keys").fetchone()[0]
        total_orders = c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        revenue = c.execute("SELECT COALESCE(SUM(total_spent),0) FROM users").fetchone()[0]
        today_orders = c.execute("SELECT COUNT(*) FROM orders WHERE date >= ?", (today,)).fetchone()[0]
        today_sales = c.execute(
            "SELECT COALESCE(SUM(-amount),0) FROM transactions WHERE type='purchase' AND date >= ?",
            (today,),
        ).fetchone()[0]
    pct_approved = int((approved_users / total_users) * 100) if total_users else 0
    text = (
        "👑 *Master Admin Panel*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📊 *QUICK STATS*\n"
        f"┣ 👥 Users: *{total_users}* ({pct_approved}% approved)\n"
        f"┣ 📦 Products: *{total_panels}*\n"
        f"┣ 🔑 Keys: *{total_keys}*\n"
        f"┣ 🛒 Orders: *{total_orders}*\n"
        f"┗ 💰 Revenue: *${fmt_money(revenue)}*\n\n"
        "📅 *TODAY*\n"
        f"┣ 🛒 Orders: *{today_orders}*\n"
        f"┗ 💵 Sales: *${fmt_money(today_sales)}*\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎲 _Select a menu below:_"
    )
    kb = [
        [
            InlineKeyboardButton("📦 Products", callback_data="adm_products"),
            InlineKeyboardButton("🔑 Keys", callback_data="adm_keys"),
        ],
        [
            InlineKeyboardButton("👥 Users", callback_data="adm_users:0"),
            InlineKeyboardButton("💰 Balance", callback_data="adm_balance"),
        ],
        [
            InlineKeyboardButton("💎 Custom Pricing", callback_data="adm_pricing"),
            InlineKeyboardButton("🔒 Access Control", callback_data="adm_access"),
        ],
        [
            InlineKeyboardButton(f"⏳ Pending ({pending_users})", callback_data="adm_pending"),
            InlineKeyboardButton("📊 Statistics", callback_data="adm_stats"),
        ],
        [
            InlineKeyboardButton("🔄 Reset Mgmt", callback_data="adm_reset"),
            InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast"),
        ],
        [
            InlineKeyboardButton("📁 All Files", callback_data="adm_files"),
            InlineKeyboardButton("🔍 Search", callback_data="adm_search"),
        ],
        [InlineKeyboardButton("⚙️ Settings", callback_data="adm_settings")],
        [back_button("main_menu")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

# ============================================================
# ADMIN: SUBMENU PAGES
# ============================================================
async def show_adm_keys(q):
    with db() as c:
        total_panels = c.execute("SELECT COUNT(*) FROM panels").fetchone()[0]
        unsold = c.execute("SELECT COUNT(*) FROM keys WHERE is_sold=0").fetchone()[0]
        sold = c.execute("SELECT COUNT(*) FROM keys WHERE is_sold=1").fetchone()[0]
    status = "✅ Good" if unsold > 50 else ("⚠️ Low" if unsold > 0 else "🔴 Empty")
    text = (
        "🔑 *KEYS MANAGEMENT*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📊 *INVENTORY*\n"
        f"┣ 📦 Products: *{total_panels}*\n"
        f"┣ 🔑 Keys in Stock: *{unsold}*\n"
        f"┣ 📈 Total Sold: *{sold}*\n"
        f"┗ 📋 Stock Status: *{status}*\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📝 _Select an action below:_"
    )
    kb = [
        [InlineKeyboardButton("📤 Upload Keys", callback_data="adm_add_keys")],
        [InlineKeyboardButton("👁️ View Keys", callback_data="adm_view_keys")],
        [InlineKeyboardButton("📥 Export Keys", callback_data="adm_export_keys")],
        [InlineKeyboardButton("📦 Stock", callback_data="adm_stock")],
        [InlineKeyboardButton("🗑️ Delete Keys", callback_data="adm_del_keys")],
        [InlineKeyboardButton("🔍 Search Key", callback_data="adm_search_key")],
        [back_button("admin_panel", "⬅️ Back to Menu")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def show_adm_search(q):
    with db() as c:
        total_users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_panels = c.execute("SELECT COUNT(*) FROM panels").fetchone()[0]
        total_keys = c.execute("SELECT COUNT(*) FROM keys").fetchone()[0]
    text = (
        "🔍 *SEARCH*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📊 *DATABASE*\n"
        f"┣ 👥 Users: *{total_users}*\n"
        f"┣ 📦 Products: *{total_panels}*\n"
        f"┗ 🔑 Keys: *{total_keys}*\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📝 _What do you want to search?_"
    )
    kb = [
        [InlineKeyboardButton("👤 Search User by ID", callback_data="adm_search_uid")],
        [InlineKeyboardButton("👤 Search User by Name", callback_data="adm_search_uname")],
        [InlineKeyboardButton("🔑 Search Key", callback_data="adm_search_key")],
        [back_button("admin_panel", "⬅️ Back to Menu")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def show_adm_settings(q):
    me = await q.get_bot().get_me()
    with db() as c:
        total_orders = c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        revenue = c.execute("SELECT COALESCE(SUM(total_spent),0) FROM users").fetchone()[0]
    admin_id_display = ADMIN_IDS[0] if ADMIN_IDS else "—"
    text = (
        "⚙️ *BOT SETTINGS*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📋 *BOT INFO*\n"
        f"┣ 🤖 Bot: @{md_esc(me.username)}\n"
        f"┣ 🔥 Name: *{md_esc(me.first_name)}*\n"
        f"┣ 🔌 Status: 🟢 Active\n"
        f"┣ 👤 Admin ID: `{admin_id_display}`\n"
        f"┗ 📅 Started: {now_str()}\n\n"
        "📞 *SUPPORT*\n"
        f"┗ 👤 Username: @{md_esc(support_username())}\n\n"
        "💰 *SALES*\n"
        f"┣ 🛒 Total Orders: *{total_orders}*\n"
        f"┗ 💵 Total Sales: *${fmt_money(revenue)}*\n\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    kb = [
        [InlineKeyboardButton("📞 Set Support Username", callback_data="adm_set_support")],
        [InlineKeyboardButton("🗑️ Database Cleanup", callback_data="adm_db_cleanup")],
        [back_button("admin_panel", "⬅️ Back to Menu")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def show_adm_pending(q):
    with db() as c:
        rows = c.execute(
            "SELECT * FROM users WHERE status='pending' ORDER BY join_date DESC LIMIT 20"
        ).fetchall()
    if not rows:
        await q.edit_message_text(
            "⏳ *PENDING USERS*\n━━━━━━━━━━━━━━━━━━\n\n📭 No pending users! 🎉",
            reply_markup=InlineKeyboardMarkup([[back_button("admin_panel", "⬅️ Back to Menu")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    lines = ["⏳ *PENDING USERS*", "━━━━━━━━━━━━━━━━━━", ""]
    for r in rows:
        first_name = (r["name"] or "User").split()[0]
        lines.append(
            f"⏳ *{first_name}*\n"
            f"┣ 🆔 ID: `{r['telegram_id']}`\n"
            f"┣ 📞 Phone: `{r['phone']}`\n"
            f"┗ 📅 Date: {(r['join_date'] or '')[:10]}"
        )
        lines.append("")
    lines.append("👇 _Click to approve/reject_")
    kb = []
    for r in rows:
        first_name = (r["name"] or "User").split()[0][:12]
        kb.append([
            InlineKeyboardButton(f"✅ {first_name}", callback_data=f"usr_appr:{r['telegram_id']}"),
            InlineKeyboardButton("🚫", callback_data=f"usr_rej:{r['telegram_id']}"),
            InlineKeyboardButton("👤", callback_data=f"usr_view:{r['telegram_id']}"),
        ])
    kb.append([InlineKeyboardButton("✅ Approve All", callback_data="usr_appr_all")])
    kb.append([back_button("admin_panel", "⬅️ Back to Menu")])
    await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def show_adm_products(q):
    with db() as c:
        rows = c.execute(
            """SELECT pa.*,
                      (SELECT COUNT(*) FROM plans pl WHERE pl.panel_id=pa.id) AS variants,
                      (SELECT COUNT(*) FROM keys k JOIN plans pl ON pl.id=k.plan_id WHERE pl.panel_id=pa.id AND k.is_sold=0) AS stock,
                      (SELECT COUNT(*) FROM keys k JOIN plans pl ON pl.id=k.plan_id WHERE pl.panel_id=pa.id AND k.is_sold=1) AS sold
               FROM panels pa ORDER BY pa.platform, pa.name"""
        ).fetchall()
    text = "📦 *PRODUCTS*\n━━━━━━━━━━━━━━━━━━\n\n"
    if not rows:
        text += "📭 No products yet. Tap *Add Product* below."
    else:
        for r in rows:
            text += (
                f"{PLATFORM_EMOJI.get(r['platform'],'📱')} *{r['name']}* ({r['platform']})\n"
                f"┣ 📋 Variants: *{r['variants']}*  🔑 Stock: *{r['stock']}*  📈 Sold: *{r['sold']}*\n\n"
            )
    kb = [[InlineKeyboardButton("➕ Add Product", callback_data="adm_add_panel")]]
    for r in rows[:20]:
        kb.append([InlineKeyboardButton(
            f"{PLATFORM_EMOJI.get(r['platform'],'📱')} {r['name']}",
            callback_data=f"adm_prod:{r['id']}"
        )])
    kb.append([back_button("admin_panel", "⬅️ Back to Menu")])
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def show_adm_product_detail(q, pid: int):
    with db() as c:
        p = c.execute("SELECT * FROM panels WHERE id=?", (pid,)).fetchone()
        if not p:
            await q.edit_message_text("❌ Product not found.",
                reply_markup=InlineKeyboardMarkup([[back_button("adm_products")]]))
            return
        plans = c.execute("SELECT * FROM plans WHERE panel_id=? ORDER BY price", (pid,)).fetchall()
        stock = c.execute(
            "SELECT COUNT(*) FROM keys k JOIN plans pl ON pl.id=k.plan_id WHERE pl.panel_id=? AND k.is_sold=0",
            (pid,)).fetchone()[0]
        sold = c.execute(
            "SELECT COUNT(*) FROM keys k JOIN plans pl ON pl.id=k.plan_id WHERE pl.panel_id=? AND k.is_sold=1",
            (pid,)).fetchone()[0]
    text = (
        "📋 *Product Details*\n"
        f"┣ 📋 Status: 🟢 Active\n"
        f"┣ {PLATFORM_EMOJI.get(p['platform'],'📱')} Platform: *{p['platform']}*\n"
        f"┣ 📋 Variants: *{len(plans)}*\n"
        f"┣ 🔑 Total Stock: *{stock}*\n"
        f"┣ 📈 Total Sold: *{sold}*\n"
        f"┗ 🆔 ID: `{p['id']}`\n\n"
        "📦 *Variants:*\n"
    )
    if not plans:
        text += "  📭 No variants yet.\n"
    else:
        for pl in plans:
            with db() as c:
                kc = c.execute("SELECT COUNT(*) FROM keys WHERE plan_id=? AND is_sold=0", (pl["id"],)).fetchone()[0]
            text += f"  ┣ ⏱️ *{pl['name']}* → 💵 ${fmt_money(pl['price'])} ({kc} keys)\n"
    kb = [
        [InlineKeyboardButton("📋 Manage Plans (Add/Delete)", callback_data=f"adm_plans_mgr:{pid}")],
        [
            InlineKeyboardButton("🔑 Add Keys", callback_data="adm_add_keys"),
            InlineKeyboardButton("📦 Stock", callback_data="adm_stock"),
        ],
        [InlineKeyboardButton("🗑️ Delete Product", callback_data=f"adm_del_prod:{pid}")],
        [back_button("adm_products", "⬅️ Back")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def show_adm_balance_menu(q):
    text = (
        "💰 *BALANCE MANAGEMENT*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Choose an action:"
    )
    kb = [
        [InlineKeyboardButton("➕ Add Balance", callback_data="adm_addbal_pick")],
        [InlineKeyboardButton("➖ Deduct Balance", callback_data="adm_dedbal_pick")],
        [back_button("admin_panel", "⬅️ Back to Menu")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def show_adm_stats(q):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with db() as c:
        total_users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = c.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0]
        pending = c.execute("SELECT COUNT(*) FROM users WHERE status='pending'").fetchone()[0]
        banned = c.execute("SELECT COUNT(*) FROM users WHERE status='banned'").fetchone()[0]
        total_orders = c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        today_orders = c.execute("SELECT COUNT(*) FROM orders WHERE date >= ?", (today,)).fetchone()[0]
        revenue = c.execute("SELECT COALESCE(SUM(total_spent),0) FROM users").fetchone()[0]
        today_sales = c.execute(
            "SELECT COALESCE(SUM(-amount),0) FROM transactions WHERE type='purchase' AND date >= ?", (today,)
        ).fetchone()[0]
        top = c.execute(
            """SELECT pl.name, COUNT(*) AS c FROM orders o
               JOIN plans pl ON pl.id=o.plan_id GROUP BY pl.id ORDER BY c DESC LIMIT 5"""
        ).fetchall()
    text = (
        "📊 *STATISTICS*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "👥 *USERS*\n"
        f"┣ Total: *{total_users}*\n"
        f"┣ ✅ Active: *{active}*\n"
        f"┣ ⏳ Pending: *{pending}*\n"
        f"┗ 🚫 Banned: *{banned}*\n\n"
        "🛒 *ORDERS*\n"
        f"┣ Total: *{total_orders}*\n"
        f"┗ Today: *{today_orders}*\n\n"
        "💰 *REVENUE*\n"
        f"┣ All-time: *${fmt_money(revenue)}*\n"
        f"┗ Today: *${fmt_money(today_sales)}*\n\n"
        "🏆 *TOP PLANS*\n"
    )
    if not top:
        text += "  —\n"
    else:
        for i, t in enumerate(top, 1):
            text += f"  {i}. {t['name']} — {t['c']} sales\n"
    kb = [[back_button("admin_panel", "⬅️ Back to Menu")]]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def show_user_details(q, target: int):
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE telegram_id=?", (target,)).fetchone()
        orders = c.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (target,)).fetchone()[0]
    if not u:
        await q.edit_message_text("❌ User not found.",
            reply_markup=InlineKeyboardMarkup([[back_button("admin_panel")]]))
        return
    status_emoji = {"active": "✅", "pending": "⏳", "banned": "🚫", "rejected": "❌"}.get(u["status"], "❓")
    text = (
        "👤 *USER DETAILS*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"┣ 🔥 Name: *{u['name']}*\n"
        f"┣ 🆔 ID: `{u['telegram_id']}`\n"
        f"┣ 📞 Phone: `{u['phone']}`\n"
        f"┣ {status_emoji} Status: *{u['status']}*\n"
        f"┣ 📅 Joined: {u['join_date']}\n"
        f"┣ 💵 Balance: *${fmt_money(u['balance'])}*\n"
        f"┣ 📥 Deposited: ${fmt_money(u['total_deposit'])}\n"
        f"┣ 💸 Spent: ${fmt_money(u['total_spent'])}\n"
        f"┗ 🛒 Orders: *{orders}*"
    )
    kb = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"usr_appr:{target}"),
            InlineKeyboardButton("🚫 Reject/Ban", callback_data=f"usr_rej:{target}"),
        ],
        [
            InlineKeyboardButton("➕ Add Bal", callback_data=f"adm_addbal:{target}"),
            InlineKeyboardButton("➖ Ded Bal", callback_data=f"adm_dedbal:{target}"),
        ],
        [back_button("admin_panel", "⬅️ Back to Menu")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

# ============================================================
# ADMIN: NEW REAL SCREENS (pricing, access, reset, files, keys, settings)
# ============================================================
async def show_adm_pricing(q):
    with db() as c:
        rows = c.execute(
            """SELECT cp.user_id, cp.plan_id, cp.price, u.name, pl.name AS plan_name, pl.price AS base
               FROM custom_pricing cp
               JOIN users u ON u.telegram_id=cp.user_id
               JOIN plans pl ON pl.id=cp.plan_id
               ORDER BY u.name LIMIT 30"""
        ).fetchall()
    text = "💎 *CUSTOM PRICING*\n━━━━━━━━━━━━━━━━━━\n\n"
    if not rows:
        text += "📭 No custom prices set.\n\nUse the button below to give a user a special price on a plan."
    else:
        text += f"Active overrides: *{len(rows)}*\n\n"
        for r in rows:
            text += (f"👤 {md_esc(r['name'])} → 📦 {md_esc(r['plan_name'])}\n"
                     f"   💵 ${fmt_money(r['price'])} _(base ${fmt_money(r['base'])})_\n")
    kb = [[InlineKeyboardButton("➕ Set Custom Price", callback_data="adm_pricing_add")]]
    for r in rows[:15]:
        kb.append([InlineKeyboardButton(
            f"🗑️ {(r['name'] or '')[:14]} · {(r['plan_name'] or '')[:14]}",
            callback_data=f"adm_pricing_del:{r['user_id']}:{r['plan_id']}"
        )])
    kb.append([back_button("admin_panel", "⬅️ Back to Menu")])
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def show_adm_access(q):
    with db() as c:
        banned = c.execute("SELECT * FROM users WHERE status='banned' ORDER BY name LIMIT 30").fetchall()
        rejected = c.execute("SELECT * FROM users WHERE status='rejected' ORDER BY name LIMIT 30").fetchall()
    text = (
        "🔒 *ACCESS CONTROL*\n━━━━━━━━━━━━━━━━━━\n\n"
        f"🚫 Banned: *{len(banned)}*\n"
        f"❌ Rejected: *{len(rejected)}*\n\n"
        "Tap a user to restore (set active):"
    )
    kb = []
    for u in banned:
        kb.append([InlineKeyboardButton(f"🚫 {u['name'][:25]}", callback_data=f"usr_appr:{u['telegram_id']}")])
    for u in rejected:
        kb.append([InlineKeyboardButton(f"❌ {u['name'][:25]}", callback_data=f"usr_appr:{u['telegram_id']}")])
    kb.append([InlineKeyboardButton("🚫 Ban a User by ID", callback_data="adm_ban_uid")])
    kb.append([back_button("admin_panel", "⬅️ Back to Menu")])
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def show_adm_reset(q):
    text = (
        "🔄 *RESET MANAGEMENT*\n━━━━━━━━━━━━━━━━━━\n\n"
        "⚠️ *DANGER ZONE* — actions are immediate and irreversible.\n\n"
        "Choose what to reset:"
    )
    kb = [
        [InlineKeyboardButton("🔁 Reset SOLD keys → unsold", callback_data="adm_reset_sold")],
        [InlineKeyboardButton("🗑️ Delete ALL orders", callback_data="adm_reset_orders")],
        [InlineKeyboardButton("🗑️ Delete ALL transactions", callback_data="adm_reset_txns")],
        [InlineKeyboardButton("💰 Reset ALL user balances → 0", callback_data="adm_reset_bal")],
        [back_button("admin_panel", "⬅️ Back to Menu")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def show_adm_files(q, context):
    with db() as c:
        rows = c.execute("SELECT * FROM files ORDER BY date DESC LIMIT 30").fetchall()
    text = (
        "📁 *All Files*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Stored: *{len(rows)}*\n\n"
    )
    if not rows:
        text += "_No files uploaded yet. Tap Upload File and send any document, photo or video._"
    else:
        for r in rows:
            text += f"• {md_esc(r['name'])}  _( {(r['date'] or '')[:10]} )_\n"
    kb = [
        [InlineKeyboardButton("📤 Upload File", callback_data="adm_file_upload")],
        [InlineKeyboardButton("💾 Backup Database (shop.db)", callback_data="adm_file_db")],
    ]
    for r in rows[:15]:
        kb.append([
            InlineKeyboardButton(f"📥 {r['name'][:24]}", callback_data=f"adm_file_get:{r['id']}"),
            InlineKeyboardButton("🗑️", callback_data=f"adm_file_del:{r['id']}"),
        ])
    kb.append([back_button("admin_panel", "⬅️ Back to Menu")])
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def show_pub_files(q):
    """Public, read-only view of all files uploaded by admins.

    Anyone logged in can browse and download these files. Admin-only actions
    (upload, delete, raw DB backup) are not exposed here.
    """
    with db() as c:
        rows = c.execute("SELECT * FROM files ORDER BY date DESC LIMIT 30").fetchall()
    text = (
        "📁 *Files*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Available: *{len(rows)}*\n\n"
    )
    if not rows:
        text += "_No files available yet. Check back later._"
    else:
        for r in rows:
            text += f"• {md_esc(r['name'])}  _( {(r['date'] or '')[:10]} )_\n"
        text += "\n_Tap a file below to download._"
    kb = []
    for r in rows[:20]:
        kb.append([InlineKeyboardButton(
            f"📥 {r['name'][:30]}", callback_data=f"pub_file_get:{r['id']}"
        )])
    kb.append([back_button("main_menu", "⬅️ Back to Menu")])
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def show_adm_keys_pick_plan(q, action: str):
    """action: view | export | del"""
    with db() as c:
        plans = c.execute(
            """SELECT pl.id, pl.name, pa.name AS panel_name, pa.platform,
                      (SELECT COUNT(*) FROM keys WHERE plan_id=pl.id) AS total
               FROM plans pl JOIN panels pa ON pa.id=pl.panel_id
               ORDER BY pa.platform, pa.name, pl.price"""
        ).fetchall()
    titles = {"view": "👁️ VIEW KEYS", "export": "📥 EXPORT KEYS", "del": "🗑️ DELETE KEYS"}
    text = f"*{titles[action]}*\n━━━━━━━━━━━━━━━━━━\n\nPick a plan:"
    kb = []
    for p in plans:
        kb.append([InlineKeyboardButton(
            f"{PLATFORM_EMOJI.get(p['platform'],'📱')} {p['panel_name']} · {p['name']} ({p['total']})",
            callback_data=f"adm_k{action}:{p['id']}"
        )])
    kb.append([back_button("adm_keys", "⬅️ Back")])
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def adm_view_plan_keys(q, plan_id: int):
    with db() as c:
        plan = c.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        rows = c.execute("SELECT key, is_sold FROM keys WHERE plan_id=? ORDER BY id DESC LIMIT 40", (plan_id,)).fetchall()
    if not plan:
        await q.edit_message_text("❌ Plan not found.",
            reply_markup=InlineKeyboardMarkup([[back_button("adm_keys")]]))
        return
    text = f"👁️ *Keys for {plan['name']}* (showing {len(rows)})\n━━━━━━━━━━━━━━━━━━\n\n"
    if not rows:
        text += "📭 No keys."
    else:
        for r in rows:
            mark = "🔴" if r["is_sold"] else "✅"
            text += f"{mark} `{r['key']}`\n"
    await q.edit_message_text(text,
        reply_markup=InlineKeyboardMarkup([[back_button("adm_view_keys", "⬅️ Back")]]),
        parse_mode=ParseMode.MARKDOWN)

async def adm_export_plan_keys(q, context, plan_id: int):
    with db() as c:
        plan = c.execute("SELECT pl.*, pa.name AS panel_name FROM plans pl JOIN panels pa ON pa.id=pl.panel_id WHERE pl.id=?", (plan_id,)).fetchone()
        rows = c.execute("SELECT key, is_sold FROM keys WHERE plan_id=? ORDER BY id", (plan_id,)).fetchall()
    if not plan:
        await q.edit_message_text("❌ Plan not found."); return
    lines = [f"# {plan['panel_name']} — {plan['name']}", ""]
    for r in rows:
        lines.append(f"{'[SOLD] ' if r['is_sold'] else ''}{r['key']}")
    data = "\n".join(lines).encode("utf-8")
    bio = io.BytesIO(data); bio.name = f"keys_{plan_id}.txt"
    await context.bot.send_document(chat_id=q.from_user.id, document=bio,
        filename=bio.name, caption=f"📥 Export: {plan['name']} ({len(rows)} keys)")
    await q.edit_message_text(f"✅ Exported *{len(rows)}* keys.",
        reply_markup=InlineKeyboardMarkup([[back_button("adm_export_keys", "⬅️ Back")]]),
        parse_mode=ParseMode.MARKDOWN)

async def adm_del_plan_keys_menu(q, plan_id: int):
    with db() as c:
        plan = c.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        unsold = c.execute("SELECT COUNT(*) FROM keys WHERE plan_id=? AND is_sold=0", (plan_id,)).fetchone()[0]
        sold = c.execute("SELECT COUNT(*) FROM keys WHERE plan_id=? AND is_sold=1", (plan_id,)).fetchone()[0]
    if not plan:
        await q.edit_message_text("❌ Plan not found."); return
    text = (f"🗑️ *Delete keys for {plan['name']}*\n━━━━━━━━━━━━━━━━━━\n\n"
            f"✅ Unsold: *{unsold}*\n🔴 Sold: *{sold}*\n\n⚠️ Irreversible.")
    kb = [
        [InlineKeyboardButton(f"🗑️ Delete UNSOLD ({unsold})", callback_data=f"adm_kdelu:{plan_id}")],
        [InlineKeyboardButton(f"🗑️ Delete SOLD ({sold})", callback_data=f"adm_kdels:{plan_id}")],
        [InlineKeyboardButton(f"🗑️ Delete ALL", callback_data=f"adm_kdela:{plan_id}")],
        [back_button("adm_del_keys", "⬅️ Back")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def show_adm_plans_manager(q, panel_id: int):
    with db() as c:
        panel = c.execute("SELECT * FROM panels WHERE id=?", (panel_id,)).fetchone()
        plans = c.execute("SELECT * FROM plans WHERE panel_id=? ORDER BY price", (panel_id,)).fetchall()
    if not panel:
        await q.edit_message_text("❌ Not found."); return
    text = f"📋 *Manage Plans — {panel['name']}*\n━━━━━━━━━━━━━━━━━━\n\n"
    if not plans:
        text += "📭 No plans yet."
    else:
        for p in plans:
            text += f"• {p['name']} — ${fmt_money(p['price'])}\n"
    kb = [[InlineKeyboardButton("➕ Add Plan", callback_data=f"adm_pl_pick:{panel_id}")]]
    for p in plans:
        kb.append([InlineKeyboardButton(f"🗑️ {p['name'][:30]}", callback_data=f"adm_del_plan:{p['id']}")])
    kb.append([back_button(f"adm_prod:{panel_id}", "⬅️ Back")])
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def show_adm_db_cleanup(q):
    text = (
        "🗑️ *DATABASE CLEANUP*\n━━━━━━━━━━━━━━━━━━\n\n"
        "Pick an action:"
    )
    kb = [
        [InlineKeyboardButton("🧹 VACUUM (compact DB)", callback_data="adm_vacuum")],
        [InlineKeyboardButton("🗑️ Delete SOLD keys", callback_data="adm_clean_sold")],
        [InlineKeyboardButton("🗑️ Delete rejected users", callback_data="adm_clean_rej")],
        [back_button("adm_settings", "⬅️ Back")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def stub_screen(q, title: str):
    text = (
        f"{title}\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🚧 _Coming soon!_ 🛠️\n\n"
        "This feature is not configured yet."
    )
    await q.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[back_button("admin_panel", "⬅️ Back to Menu")]]),
        parse_mode=ParseMode.MARKDOWN,
    )

async def show_admin_help_inline(q):
    text = (
        "📖 *ADMIN COMMANDS*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "`/addpanel <name> <platform>`\n"
        "  platforms: Android, iOS, Windows\n\n"
        "`/addplan <panel_id> <name...> <price>`\n\n"
        "`/addkeys <plan_id>`\n"
        "  then paste keys, one per line\n\n"
        "`/stock <plan_id>`\n"
        "`/users` — paginated list with ➕/➖ buttons\n"
        "`/addbalance <user_id> <amount>`\n"
        "`/deductbalance <user_id> <amount>`\n"
        "`/broadcast <message>`\n"
        "`/cancel` — cancel pending input\n\n"
        "*USER COMMANDS*\n"
        "`/start` — open shop / login\n"
        "`/help` — show this list (admins)\n"
    )
    kb = [[back_button("admin_panel")]]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def show_profile(q):
    uid = q.from_user.id
    u = get_user(uid)
    if not u:
        await q.edit_message_text("Please /start first."); return
    with db() as c:
        total_orders = c.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (uid,)).fetchone()[0]
        last = c.execute(
            "SELECT date FROM orders WHERE user_id=? ORDER BY date DESC LIMIT 1", (uid,)
        ).fetchone()
        last_txn = c.execute(
            "SELECT amount,type,date FROM transactions WHERE user_id=? ORDER BY date DESC LIMIT 1", (uid,)
        ).fetchone()
    status_icon = STATUS_ICON.get(u["status"], "•") if "STATUS_ICON" in globals() else "✅"
    last_txn_str = "—"
    if last_txn:
        last_txn_str = (
            f"{md_esc(last_txn['type'])} ${fmt_money(abs(last_txn['amount']))}"
            f" · {md_esc(last_txn['date'])}"
        )
    text = (
        "👤 *MY PROFILE*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🪪 *ACCOUNT*\n"
        f"├ 🔥 Name: *{md_esc(u['name'])}*\n"
        f"├ 🆔 ID: `{u['telegram_id']}`\n"
        f"├ 📞 Phone: `{md_esc(u['phone'])}`\n"
        f"├ {status_icon} Status: *{md_esc((u['status'] or '').title())}*\n"
        f"└ 📅 Joined: {md_esc(u['join_date'])}\n\n"
        "💼 *WALLET*\n"
        f"├ 💰 Balance: *${fmt_money(u['balance'])}*\n"
        f"├ 📥 Total Deposited: *${fmt_money(u['total_deposit'])}*\n"
        f"└ 💸 Total Spent: *${fmt_money(u['total_spent'])}*\n\n"
        "🛒 *ACTIVITY*\n"
        f"├ 📦 Orders: *{total_orders}*\n"
        f"├ 🕒 Last Purchase: {md_esc(last['date']) if last else '—'}\n"
        f"└ 💳 Last Txn: {last_txn_str}"
    )
    kb = [
        [
            InlineKeyboardButton("📦 My Orders", callback_data="my_orders"),
            InlineKeyboardButton("💳 Transactions", callback_data="transactions"),
        ],
        [InlineKeyboardButton("💬 Contact Support", url=f"https://t.me/{support_username()}")],
        [back_button("main_menu")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def show_support(q):
    su = support_username()
    text = (
        "💬 *Customer Support*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Need help? Tap the button below to contact support."
    )
    kb_rows = [
        [InlineKeyboardButton(f"💬 Contact @{su}", url=f"https://t.me/{su}")],
        [back_button("main_menu")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode=ParseMode.MARKDOWN)

# ============================================================
# RESTART BUTTON / TEXT HANDLER
# ============================================================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    uid = update.effective_user.id

    # Admin balance-amount input
    if is_admin(uid) and "admin_action" in context.user_data:
        action, target = context.user_data["admin_action"]
        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError()
        except ValueError:
            await update.message.reply_text("❌ Invalid amount. Send a positive number.")
            return
        await _apply_balance_change(context, target, amount, action)
        context.user_data.pop("admin_action", None)
        await update.message.reply_text(
            f"✅ {'Added' if action=='add' else 'Deducted'} ${fmt_money(amount)} "
            f"{'to' if action=='add' else 'from'} user `{target}`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # If user not logged in and trying to chat → re-prompt login
    if not get_user(uid):
        await prompt_login(update, context)

# ============================================================
# ADMIN: BALANCE CHANGE HELPER
# ============================================================
async def _apply_balance_change(context, target_id: int, amount: float, action: str):
    delta = amount if action == "add" else -amount
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE telegram_id=?", (target_id,)).fetchone()
        if not u:
            return False
        c.execute(
            "UPDATE users SET balance = balance + ?, total_deposit = total_deposit + ? WHERE telegram_id=?",
            (delta, amount if action == "add" else 0, target_id),
        )
        c.execute(
            "INSERT INTO transactions (user_id, amount, type, date) VALUES (?,?,?,?)",
            (target_id, delta, "admin_add" if action == "add" else "admin_deduct", now_str()),
        )
    try:
        msg = (
            f"💰 Your balance was {'credited' if action=='add' else 'debited'} "
            f"by ${fmt_money(amount)} by an admin."
        )
        await context.bot.send_message(chat_id=target_id, text=msg)
    except Exception:
        pass
    return True

# ============================================================
# ADMIN COMMANDS
# ============================================================
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("❌ Not authorized.")
            return
        return await func(update, context)
    return wrapper

@admin_only
async def cmd_addbalance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Usage: /addbalance <user_id> <amount>")
        return
    try:
        uid = int(args[0]); amt = float(args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid arguments.")
        return
    ok = await _apply_balance_change(context, uid, amt, "add")
    await update.message.reply_text("✅ Done." if ok else "❌ User not found.")

@admin_only
async def cmd_deductbalance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Usage: /deductbalance <user_id> <amount>")
        return
    try:
        uid = int(args[0]); amt = float(args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid arguments.")
        return
    ok = await _apply_balance_change(context, uid, amt, "deduct")
    await update.message.reply_text("✅ Done." if ok else "❌ User not found.")

@admin_only
async def cmd_addpanel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addpanel <name> <platform>\nPlatforms: Android, iOS, Windows")
        return
    *name_parts, platform = context.args
    if not name_parts:
        await update.message.reply_text("❌ Provide a name and platform.")
        return
    name = " ".join(name_parts)
    if platform not in PLATFORMS:
        await update.message.reply_text(f"❌ Platform must be one of: {', '.join(PLATFORMS)}")
        return
    with db() as c:
        cur = c.execute("INSERT INTO panels (name, platform) VALUES (?,?)", (name, platform))
        pid = cur.lastrowid
    await update.message.reply_text(f"✅ Panel created. ID: {pid}")

@admin_only
async def cmd_addplan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text("Usage: /addplan <panel_id> <name...> <price>")
        return
    try:
        panel_id = int(context.args[0])
        price = float(context.args[-1])
        name = " ".join(context.args[1:-1])
    except ValueError:
        await update.message.reply_text("❌ Invalid arguments.")
        return
    if not name:
        await update.message.reply_text("❌ Provide a plan name.")
        return
    with db() as c:
        if not c.execute("SELECT 1 FROM panels WHERE id=?", (panel_id,)).fetchone():
            await update.message.reply_text("❌ Panel not found.")
            return
        cur = c.execute("INSERT INTO plans (panel_id, name, price) VALUES (?,?,?)", (panel_id, name, price))
        pid = cur.lastrowid
    await update.message.reply_text(f"✅ Plan created. ID: {pid}")

@admin_only
async def cmd_addkeys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /addkeys <plan_id>\nThen send the keys (one per line) in the next message.")
        return
    try:
        plan_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid plan_id.")
        return
    with db() as c:
        if not c.execute("SELECT 1 FROM plans WHERE id=?", (plan_id,)).fetchone():
            await update.message.reply_text("❌ Plan not found.")
            return
    context.user_data["awaiting_keys_for_plan"] = plan_id
    await update.message.reply_text(
        f"📥 Send keys for plan `{plan_id}` (one per line). Send /cancel to abort.",
        parse_mode=ParseMode.MARKDOWN,
    )

@admin_only
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for k in ("awaiting_keys_for_plan", "admin_action", "awaiting_broadcast",
              "flow", "panel_name_value"):
        context.user_data.pop(k, None)
    await update.message.reply_text("🚫 Cancelled.")

async def admin_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle multi-step admin text inputs before falling through to on_text."""
    uid = update.effective_user.id
    if not is_admin(uid) or not update.message or not update.message.text:
        return await on_text(update, context)

    text = update.message.text

    flow = context.user_data.get("flow")
    if flow:
        # ---- Add Panel (name) ----
        if flow == "panel_name":
            name = text.strip()
            if not name:
                await update.message.reply_text("❌ Name cannot be empty.")
                return
            context.user_data["panel_name_value"] = name
            kb = [[InlineKeyboardButton(p, callback_data=f"adm_pp:{p}")] for p in PLATFORMS]
            kb.append([back_button("admin_panel")])
            await update.message.reply_text(
                f"📝 *Add Panel — Step 2/2*\nName: *{name}*\nPick the platform:",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        # ---- Add Plan (name) ----
        if flow.startswith("plan_name:"):
            panel_id = int(flow.split(":", 1)[1])
            name = text.strip()
            if not name:
                await update.message.reply_text("❌ Name cannot be empty.")
                return
            context.user_data["flow"] = f"plan_price:{panel_id}:{name}"
            await update.message.reply_text(
                f"📝 *Add Plan — Step 3/3*\nName: *{name}*\nNow send the *price* (e.g. `5.99`).",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        if flow.startswith("plan_price:"):
            _, panel_id_s, plan_name = flow.split(":", 2)
            try:
                price = float(text.strip())
                if price < 0: raise ValueError()
            except ValueError:
                await update.message.reply_text("❌ Invalid price. Send a number like `5.99`.")
                return
            with db() as c:
                cur = c.execute(
                    "INSERT INTO plans (panel_id, name, price) VALUES (?,?,?)",
                    (int(panel_id_s), plan_name, price),
                )
                pid = cur.lastrowid
            context.user_data.pop("flow", None)
            kb = [
                [InlineKeyboardButton("🔑 Add Keys to this plan", callback_data=f"adm_kk_pick:{pid}")],
                [back_button("admin_panel")],
            ]
            await update.message.reply_text(
                f"✅ Plan created!\n\n📋 *{plan_name}* — ${fmt_money(price)}\nID: `{pid}`",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        # ---- Add Keys ----
        if flow.startswith("keys_paste:"):
            plan_id = int(flow.split(":", 1)[1])
            keys = [k.strip() for k in text.splitlines() if k.strip()]
            if not keys:
                await update.message.reply_text("❌ No keys provided.")
                return
            with db() as c:
                c.executemany(
                    "INSERT INTO keys (plan_id, key, is_sold) VALUES (?,?,0)",
                    [(plan_id, k) for k in keys],
                )
            context.user_data.pop("flow", None)
            await update.message.reply_text(
                f"✅ Added *{len(keys)}* key(s) to plan `{plan_id}`.",
                reply_markup=InlineKeyboardMarkup([[back_button("admin_panel")]]),
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        # ---- Add / Deduct balance via picker ----
        if flow in ("addbal_uid", "dedbal_uid"):
            try:
                target = int(text.strip())
            except ValueError:
                await update.message.reply_text("❌ Invalid user ID. Send a number.")
                return
            if not get_user(target):
                await update.message.reply_text("❌ User not found.")
                return
            action = "add" if flow == "addbal_uid" else "deduct"
            context.user_data["flow"] = f"{action}bal_amt:{target}"
            await update.message.reply_text(
                f"Step 2/2 — send the *amount* to {'add to' if action=='add' else 'deduct from'} user `{target}`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        # ---- File upload (handled in on_document/on_media wrapper too) ----
        if flow == "file_upload":
            await update.message.reply_text("Please send a *document, photo, video or audio* (not text).",
                parse_mode=ParseMode.MARKDOWN)
            return
        # ---- Set support username ----
        if flow == "set_support":
            context.user_data.pop("flow", None)
            new_u = text.strip().lstrip("@")
            if not new_u or " " in new_u:
                await update.message.reply_text("❌ Invalid username.")
                return
            set_setting("support_username", new_u)
            await update.message.reply_text(
                f"✅ Support username updated to @{new_u}.",
                reply_markup=InlineKeyboardMarkup([[back_button("adm_settings", "⬅️ Back")]]),
            )
            return
        # ---- Ban by UID ----
        if flow == "ban_uid":
            context.user_data.pop("flow", None)
            try:
                target = int(text.strip())
            except ValueError:
                await update.message.reply_text("❌ Invalid ID."); return
            if is_admin(target):
                await update.message.reply_text("❌ Cannot ban an admin."); return
            with db() as c:
                r = c.execute("UPDATE users SET status='banned' WHERE telegram_id=?", (target,)).rowcount
            if not r:
                await update.message.reply_text("📭 User not found.")
            else:
                try:
                    await context.bot.send_message(chat_id=target,
                        text="🚫 You have been banned by an admin.")
                except Exception: pass
                await update.message.reply_text(
                    f"🚫 User `{target}` banned.",
                    reply_markup=InlineKeyboardMarkup([[back_button("adm_access", "⬅️ Back")]]),
                    parse_mode=ParseMode.MARKDOWN,
                )
            return
        # ---- Custom pricing 3-step flow ----
        if flow == "pricing_uid":
            try:
                target = int(text.strip())
            except ValueError:
                await update.message.reply_text("❌ Invalid ID."); return
            if not get_user(target):
                await update.message.reply_text("📭 User not found."); return
            context.user_data["flow"] = f"pricing_pid:{target}"
            await update.message.reply_text(
                "💎 Step 2/3 — Send the *Plan ID* to set custom price for.\n_(Use 🔍 Search → Keys, or check Products → variants list.)_",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        if flow.startswith("pricing_pid:"):
            target = int(flow.split(":", 1)[1])
            try:
                plan_id = int(text.strip())
            except ValueError:
                await update.message.reply_text("❌ Invalid plan ID."); return
            with db() as c:
                p = c.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
            if not p:
                await update.message.reply_text("📭 Plan not found."); return
            context.user_data["flow"] = f"pricing_amt:{target}:{plan_id}"
            await update.message.reply_text(
                f"💎 Step 3/3 — Plan: *{p['name']}* (base ${fmt_money(p['price'])})\nSend the *custom price* (number).",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        if flow.startswith("pricing_amt:"):
            _, tuid, tpid = flow.split(":")
            context.user_data.pop("flow", None)
            try:
                price = float(text.strip())
                if price < 0: raise ValueError
            except ValueError:
                await update.message.reply_text("❌ Invalid price."); return
            with db() as c:
                c.execute("INSERT OR REPLACE INTO custom_pricing(user_id,plan_id,price) VALUES(?,?,?)",
                          (int(tuid), int(tpid), price))
            await update.message.reply_text(
                f"✅ Custom price set: user `{tuid}` → plan `{tpid}` = *${fmt_money(price)}*",
                reply_markup=InlineKeyboardMarkup([[back_button("adm_pricing", "⬅️ Back")]]),
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        # ---- Search flows ----
        if flow == "search_uid":
            context.user_data.pop("flow", None)
            try:
                target = int(text.strip())
            except ValueError:
                await update.message.reply_text("❌ Invalid ID. Send a number.")
                return
            u = get_user(target)
            if not u:
                await update.message.reply_text("📭 No user found with that ID.",
                    reply_markup=InlineKeyboardMarkup([[back_button("adm_search", "⬅️ Back")]]))
                return
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 View Details", callback_data=f"usr_view:{target}")]])
            await update.message.reply_text(
                f"✅ Found:\n👤 *{u['name']}*\n🆔 `{u['telegram_id']}`\n📞 `{u['phone']}`\n💵 ${fmt_money(u['balance'])}",
                reply_markup=kb, parse_mode=ParseMode.MARKDOWN,
            )
            return
        if flow == "search_uname":
            context.user_data.pop("flow", None)
            q = text.strip()
            with db() as c:
                rows = c.execute(
                    "SELECT * FROM users WHERE name LIKE ? ORDER BY join_date DESC LIMIT 20",
                    (f"%{q}%",),
                ).fetchall()
            if not rows:
                await update.message.reply_text("📭 No users matched.",
                    reply_markup=InlineKeyboardMarkup([[back_button("adm_search", "⬅️ Back")]]))
                return
            lines = [f"🔍 Found *{len(rows)}* user(s):"]
            kb = []
            for r in rows:
                lines.append(f"• {r['name']} — `{r['telegram_id']}`")
                kb.append([InlineKeyboardButton(f"👤 {r['name'][:20]}", callback_data=f"usr_view:{r['telegram_id']}")])
            kb.append([back_button("adm_search", "⬅️ Back")])
            await update.message.reply_text("\n".join(lines),
                reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
            return
        if flow == "search_key":
            context.user_data.pop("flow", None)
            q = text.strip()
            with db() as c:
                rows = c.execute(
                    """SELECT k.*, pl.name AS plan_name, pa.name AS panel_name, pa.platform
                       FROM keys k JOIN plans pl ON pl.id=k.plan_id
                       JOIN panels pa ON pa.id=pl.panel_id
                       WHERE k.key LIKE ? LIMIT 10""",
                    (f"%{q}%",),
                ).fetchall()
            if not rows:
                await update.message.reply_text("📭 No keys matched.",
                    reply_markup=InlineKeyboardMarkup([[back_button("adm_search", "⬅️ Back")]]))
                return
            lines = [f"🔍 Found *{len(rows)}* key(s):"]
            for r in rows:
                state = "🔴 sold" if r["is_sold"] else "✅ available"
                lines.append(f"\n🔑 `{r['key']}`\n   📦 {r['panel_name']} → {r['plan_name']} • {state}")
            await update.message.reply_text("\n".join(lines),
                reply_markup=InlineKeyboardMarkup([[back_button("adm_search", "⬅️ Back")]]),
                parse_mode=ParseMode.MARKDOWN)
            return
        if flow.startswith("addbal_amt:") or flow.startswith("dedbal_amt:"):
            action = "add" if flow.startswith("addbal_amt:") else "deduct"
            target = int(flow.split(":", 1)[1])
            try:
                amount = float(text.strip())
                if amount <= 0: raise ValueError()
            except ValueError:
                await update.message.reply_text("❌ Invalid amount.")
                return
            ok = await _apply_balance_change(context, target, amount, action)
            context.user_data.pop("flow", None)
            await update.message.reply_text(
                (f"✅ {'Added' if action=='add' else 'Deducted'} ${fmt_money(amount)} "
                 f"{'to' if action=='add' else 'from'} user `{target}`.") if ok else "❌ Failed.",
                reply_markup=InlineKeyboardMarkup([[back_button("admin_panel")]]),
                parse_mode=ParseMode.MARKDOWN,
            )
            return

    if "awaiting_keys_for_plan" in context.user_data:
        plan_id = context.user_data.pop("awaiting_keys_for_plan")
        keys = [k.strip() for k in text.splitlines() if k.strip()]
        if not keys:
            await update.message.reply_text("❌ No keys provided.")
            return
        with db() as c:
            c.executemany(
                "INSERT INTO keys (plan_id, key, is_sold) VALUES (?,?,0)",
                [(plan_id, k) for k in keys],
            )
        await update.message.reply_text(f"✅ Added {len(keys)} key(s) to plan `{plan_id}`.", parse_mode=ParseMode.MARKDOWN)
        return

    if context.user_data.get("awaiting_broadcast"):
        context.user_data.pop("awaiting_broadcast", None)
        with db() as c:
            users = [r["telegram_id"] for r in c.execute("SELECT telegram_id FROM users").fetchall()]
        sent = 0; failed = 0
        for tid in users:
            try:
                await context.bot.send_message(chat_id=tid, text=f"📢 {text}")
                sent += 1
            except Exception:
                failed += 1
        await update.message.reply_text(f"✅ Broadcast sent to {sent} users ({failed} failed).")
        return

    return await on_text(update, context)

@admin_only
async def cmd_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /stock <plan_id>")
        return
    try:
        plan_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid plan_id.")
        return
    with db() as c:
        plan = c.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            await update.message.reply_text("❌ Plan not found.")
            return
        unsold = c.execute("SELECT COUNT(*) FROM keys WHERE plan_id=? AND is_sold=0", (plan_id,)).fetchone()[0]
        sold = c.execute("SELECT COUNT(*) FROM keys WHERE plan_id=? AND is_sold=1", (plan_id,)).fetchone()[0]
    await update.message.reply_text(
        f"📦 Plan `{plan_id}` ({plan['name']})\nAvailable: {unsold}\nSold: {sold}",
        parse_mode=ParseMode.MARKDOWN,
    )

@admin_only
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await admin_users_page(update, 0)

STATUS_ICON = {"active": "✅", "pending": "⏳", "banned": "🚫", "rejected": "❌"}

async def show_users_management(update_or_q):
    with db() as c:
        total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        approved = c.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0]
        pending = c.execute("SELECT COUNT(*) FROM users WHERE status='pending'").fetchone()[0]
        banned = c.execute("SELECT COUNT(*) FROM users WHERE status='banned'").fetchone()[0]
    pct = int(approved / total * 100) if total else 0
    text = (
        "👥 *USERS MANAGEMENT*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📊 *OVERVIEW*\n"
        f"┣ 👥 Total Users: *{total}*\n"
        f"┣ ✅ Approved: *{approved}* ({pct}%)\n"
        f"┣ ⏳ Pending: *{pending}*\n"
        f"┗ 🚫 Banned: *{banned}*\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "_Select an action below:_"
    )
    kb = [
        [InlineKeyboardButton("🔍 Search by Chat ID", callback_data="adm_search_uid")],
        [InlineKeyboardButton("🔍 Search by Name/Phone", callback_data="adm_search_uname")],
        [InlineKeyboardButton("👥 View All Users", callback_data="adm_users_list:all:0")],
        [InlineKeyboardButton("✅ Approved Users", callback_data="adm_users_list:active:0")],
        [InlineKeyboardButton("⏳ Pending Users", callback_data="adm_users_list:pending:0")],
        [InlineKeyboardButton("🚫 Banned Users", callback_data="adm_users_list:banned:0")],
        [back_button("admin_panel", "⬅️ Back to Menu")],
    ]
    if hasattr(update_or_q, "edit_message_text"):
        await update_or_q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    else:
        await update_or_q.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def show_users_list(q, kind: str, page: int):
    PAGE_SIZE = 15
    where = "" if kind == "all" else "WHERE status=?"
    args = () if kind == "all" else (kind,)
    with db() as c:
        total = c.execute(f"SELECT COUNT(*) FROM users {where}", args).fetchone()[0]
        rows = c.execute(
            f"SELECT telegram_id, name, status FROM users {where} ORDER BY join_date DESC LIMIT ? OFFSET ?",
            (*args, PAGE_SIZE, page * PAGE_SIZE),
        ).fetchall()
    titles = {"all": "All Users", "active": "Approved Users",
              "pending": "Pending Users", "banned": "Banned Users"}
    text = f"👥 *{titles[kind]}* ({total})\n━━━━━━━━━━━━━━━━━━"
    kb = []
    for r in rows:
        icon = STATUS_ICON.get(r["status"], "•")
        nm = (r["name"] or "User")[:20]
        kb.append([InlineKeyboardButton(
            f"{icon} {nm} ({r['telegram_id']})",
            callback_data=f"usr_view:{r['telegram_id']}"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"adm_users_list:{kind}:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"adm_users_list:{kind}:{page+1}"))
    if nav: kb.append(nav)
    kb.append([back_button("adm_users_mgmt", "⬅️ Back")])
    if not rows:
        text += "\n\n📭 No users."
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def admin_users_page(update_or_q, page: int):
    # legacy entrypoint -> new management screen
    await show_users_management(update_or_q)

@admin_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        msg = " ".join(context.args)
        with db() as c:
            users = [r["telegram_id"] for r in c.execute("SELECT telegram_id FROM users").fetchall()]
        sent = 0; failed = 0
        for tid in users:
            try:
                await context.bot.send_message(chat_id=tid, text=f"📢 {msg}")
                sent += 1
            except Exception:
                failed += 1
        await update.message.reply_text(f"✅ Broadcast sent to {sent} users ({failed} failed).")
    else:
        context.user_data["awaiting_broadcast"] = True
        await update.message.reply_text("📢 Send the broadcast message now (or /cancel).")

@admin_only
async def cmd_admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠️ *ADMIN COMMANDS*\n"
        "/addbalance <user_id> <amount>\n"
        "/deductbalance <user_id> <amount>\n"
        "/addpanel <name> <platform>  (platforms: Android, iOS, Windows)\n"
        "/addplan <panel_id> <name...> <price>\n"
        "/addkeys <plan_id>  (then paste keys, one per line)\n"
        "/stock <plan_id>\n"
        "/users\n"
        "/broadcast <message>\n"
        "/cancel  — cancel pending admin input",
        parse_mode=ParseMode.MARKDOWN,
    )

# ============================================================
# MAIN
# ============================================================
async def auto_backup_job(context: ContextTypes.DEFAULT_TYPE):
    """Periodic backup of shop.db to BACKUP_CHAT_ID."""
    if not BACKUP_CHAT_ID:
        return
    if USE_PG:
        # When using PostgreSQL the DB lives off-host; rely on the provider's
        # backup tooling instead of mailing a SQLite file to Telegram.
        return
    if not os.path.exists(DB_PATH):
        return
    try:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        with open(DB_PATH, "rb") as f:
            await context.bot.send_document(
                chat_id=int(BACKUP_CHAT_ID),
                document=f,
                filename=f"shop-{ts}.db",
                caption=f"🗄️ Auto backup · {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            )
        logger.info("Auto-backup sent to %s", BACKUP_CHAT_ID)
    except Exception as e:
        logger.warning("Auto-backup failed: %s", e)

async def on_db_restore_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """If an admin sends/forwards a .db file to the bot, replace shop.db with it."""
    msg = update.effective_message
    if not msg or not msg.document:
        return
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        return
    name = (msg.document.file_name or "").lower()
    if not (name.endswith(".db") or name.endswith(".sqlite") or name.endswith(".sqlite3")):
        return
    if USE_PG:
        await msg.reply_text(
            "ℹ️ This bot is running on PostgreSQL, so SQLite restore is disabled. "
            "Restore your database directly through your hosting provider."
        )
        return
    try:
        f = await msg.document.get_file()
        tmp_path = DB_PATH + ".restore"
        await f.download_to_drive(tmp_path)
        # Sanity check: must be a valid sqlite file
        try:
            test = sqlite3.connect(tmp_path)
            test.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            test.close()
        except Exception as e:
            os.remove(tmp_path)
            await msg.reply_text(f"❌ Not a valid SQLite file: {e}")
            return
        # Backup current then swap
        if os.path.exists(DB_PATH):
            os.replace(DB_PATH, DB_PATH + ".prev")
        os.replace(tmp_path, DB_PATH)
        await msg.reply_text(
            "✅ Database restored from your file.\n"
            "Previous DB saved as `shop.db.prev`.\n"
            "Restart the bot for changes to take full effect."
        )
        logger.info("DB restored from admin upload by uid=%s", uid)
    except Exception as e:
        await msg.reply_text(f"❌ Restore failed: {e}")

def start_keepalive_server():
    """Tiny HTTP server so Render free web services / UptimeRobot can ping us."""
    port = int(os.environ.get("PORT", "0") or 0)
    if not port:
        return
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK - bot alive")
        def log_message(self, *a, **kw): pass

    def serve():
        try:
            HTTPServer(("0.0.0.0", port), H).serve_forever()
        except Exception as e:
            logger.warning("keepalive server stopped: %s", e)
    threading.Thread(target=serve, daemon=True).start()
    logger.info("Keepalive HTTP server listening on :%d", port)

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN env var is required.")
    init_db()
    start_keepalive_server()
    logger.info("Admin IDs: %s | Public admin usernames: %s", ADMIN_IDS, PUBLIC_ADMIN_USERNAMES)

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_admin_help))
    app.add_handler(CommandHandler("admin", cmd_admin_help))
    app.add_handler(CommandHandler("addbalance", cmd_addbalance))
    app.add_handler(CommandHandler("deductbalance", cmd_deductbalance))
    app.add_handler(CommandHandler("addpanel", cmd_addpanel))
    app.add_handler(CommandHandler("addplan", cmd_addplan))
    app.add_handler(CommandHandler("addkeys", cmd_addkeys))
    app.add_handler(CommandHandler("stock", cmd_stock))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Contact (login)
    app.add_handler(MessageHandler(filters.CONTACT, on_contact))
    # DB restore: admin sends/forwards a .db file → replace shop.db (runs BEFORE on_admin_media)
    app.add_handler(MessageHandler(
        filters.Document.FileExtension("db") | filters.Document.FileExtension("sqlite") | filters.Document.FileExtension("sqlite3"),
        on_db_restore_doc,
    ))
    app.add_handler(MessageHandler(
        (filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE),
        on_admin_media,
    ))

    # Callback queries
    app.add_handler(CallbackQueryHandler(on_callback))

    # Free text (admin multi-step + restart)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text_router))

    # Schedule auto-backup
    if BACKUP_CHAT_ID and app.job_queue:
        app.job_queue.run_repeating(
            auto_backup_job,
            interval=BACKUP_INTERVAL_MIN * 60,
            first=60,
            name="auto_backup",
        )
        logger.info("Auto-backup scheduled every %d min → chat %s", BACKUP_INTERVAL_MIN, BACKUP_CHAT_ID)
    elif not BACKUP_CHAT_ID:
        logger.info("Auto-backup disabled (set BACKUP_CHAT_ID env var to enable).")

    logger.info("Bot starting (polling)…")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    import asyncio

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
