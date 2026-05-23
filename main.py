import asyncio
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import aiosqlite
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.path.join(os.path.dirname(__file__), "bot_data.db")
PORT = int(os.getenv("PORT", "8000"))

# ✅ Apna channel username
CHANNEL_USERNAME = "freepromochannels"
CHANNEL_LINK = "https://t.me/freepromochannels"

REPLIT_DOMAINS = os.getenv("REPLIT_DOMAINS", "")
MANUAL_WEBAPP_URL = "https://tg-bot2026-dj1f.onrender.com/bot/verify"

if MANUAL_WEBAPP_URL:
    WEBAPP_URL = MANUAL_WEBAPP_URL
elif REPLIT_DOMAINS:
    PUBLIC_HOST = REPLIT_DOMAINS.split(",")[0].strip()
    WEBAPP_URL = f"https://{PUBLIC_HOST}/bot/verify"
else:
    WEBAPP_URL = f"http://localhost:{PORT}/bot/verify"

logger.info(f"Mini App URL: {WEBAPP_URL}")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                is_verified INTEGER DEFAULT 0,
                device_id TEXT,
                verified_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS device_registry (
                device_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                registered_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_balance (
                user_id INTEGER PRIMARY KEY,
                balance REAL DEFAULT 0.0,
                referral_count INTEGER DEFAULT 0,
                last_bonus_claim TEXT,
                upi_id TEXT
            )
        """)
        await db.commit()
    logger.info("Database initialized")


# ✅ Channel membership check function
async def check_channel_member(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(
            chat_id=f"@{CHANNEL_USERNAME}",
            user_id=user_id
        )
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.error(f"Channel check error: {e}")
        return False


# ✅ Channel join buttons bhejo
async def send_join_message(update_or_query, user_id: int, bot=None):
    keyboard = [
        [InlineKeyboardButton("📢 Join Channel", url=CHANNEL_LINK)],
        [InlineKeyboardButton("✅ Maine Join Kar Liya", callback_data="check_join")],
    ]
    text = (
        "👋 Hey There! Welcome To Bot!\n\n"
        "🔴 Must Join Our Channel To Use Bot\n\n"
        "💥 After Joining Click Below Button"
    )

    if hasattr(update_or_query, 'message') and update_or_query.message:
        await update_or_query.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif hasattr(update_or_query, 'edit_message_text'):
        await update_or_query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    print("START COMMAND RECEIVED")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
            (user.id, user.username, user.first_name),
        )
        await db.execute(
            "INSERT OR IGNORE INTO user_balance (user_id) VALUES (?)",
            (user.id,)
        )
        await db.commit()
        row = await (
            await db.execute("SELECT is_verified FROM users WHERE user_id = ?", (user.id,))
        ).fetchone()
        is_verified = row[0] if row else 0

    # ✅ Pehle channel check karo
    is_member = await check_channel_member(context.bot, user.id)

    if not is_member:
        # Channel join nahi kiya — join buttons dikhao
        await send_join_message(update, user.id)
        return

    # Channel join kar liya — ab verify check karo
    if is_verified:
        await send_main_menu(update, user.first_name)
    else:
        keyboard = [[InlineKeyboardButton("🔐 Verify", web_app=WebAppInfo(url=WEBAPP_URL))]]
        await update.message.reply_text(
            "🔒 Verify Yourself To Start Bot",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


# ✅ "Maine Join Kar Liya" button handler
async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    is_member = await check_channel_member(context.bot, user.id)

    if not is_member:
        # Abhi bhi join nahi kiya
        await query.answer(
            "❌ Aapne channel join nahi kiya! Pehle join karo.",
            show_alert=True
        )
        return

    # Join kar liya — verify check karo
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (
            await db.execute("SELECT is_verified FROM users WHERE user_id = ?", (user.id,))
        ).fetchone()
        is_verified = row[0] if row else 0

    if is_verified:
        await query.edit_message_text("✅ Sab sahi hai! Menu load ho raha hai...")
        await send_main_menu_callback(query, user.first_name)
    else:
        keyboard = [[InlineKeyboardButton("🔐 Verify", web_app=WebAppInfo(url=WEBAPP_URL))]]
        await query.edit_message_text(
            "✅ Channel join ho gaya!\n\n🔒 Ab apna device verify karo:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def send_main_menu(update: Update, name: str):
    keyboard = [
        [KeyboardButton("💰 Balance"), KeyboardButton("👥 Refer Earn")],
        [KeyboardButton("🎁 Bonus"), KeyboardButton("💸 Withdraw")],
        [KeyboardButton("🏦 Link UPI")],
    ]
    await update.message.reply_text(
        "🏠 Welcome To UPI Giveaway Bot!\n\nHow to Earn: (((CLICK HERE )))",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )


async def send_main_menu_callback(query, name: str):
    keyboard = [
        [KeyboardButton("💰 Balance"), KeyboardButton("👥 Refer Earn")],
        [KeyboardButton("🎁 Bonus"), KeyboardButton("💸 Withdraw")],
        [KeyboardButton("🏦 Link UPI")],
    ]
    await query.message.reply_text(
        "🏠 Welcome To UPI Giveaway Bot!\n\nHow to Earn: (((CLICK HERE )))",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )


async def web_app_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.message.web_app_data.data
    user = update.effective_user
    try:
        payload = json.loads(data)
        if payload.get("status") == "verified":
            await send_main_menu(update, user.first_name)
        elif payload.get("status") == "blocked":
            await update.message.reply_text(
                "⛔ Verification failed.\nThis device is already linked to another account."
            )
        else:
            await update.message.reply_text("⚠️ Verification failed. Please try /start again.")
    except Exception as e:
        logger.error(f"web_app_data error: {e}")
        await update.message.reply_text("⚠️ Something went wrong. Try /start again.")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    # ✅ Pehle channel check karo
    is_member = await check_channel_member(context.bot, user_id)
    if not is_member:
        await send_join_message(update, user_id)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        row = await (
            await db.execute("SELECT is_verified FROM users WHERE user_id = ?", (user_id,))
        ).fetchone()
        is_verified = row[0] if row else 0

    if not is_verified:
        keyboard = [[InlineKeyboardButton("🔐 Verify", web_app=WebAppInfo(url=WEBAPP_URL))]]
        await update.message.reply_text(
            "🔒 Please verify your device first.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if text == "💰 Balance":
        await handle_balance(update, user_id)
    elif text == "👥 Refer Earn":
        await handle_refer_earn(update, user_id, context)
    elif text == "🎁 Bonus":
        await handle_bonus(update, user_id)
    elif text == "💸 Withdraw":
        await handle_withdraw(update, user_id)
    elif text == "🏦 Link UPI":
        context.user_data['waiting_for_upi'] = True
        await update.message.reply_text("🏦 Send your UPI ID to link it (e.g. name@upi)")
    else:
        if context.user_data.get('waiting_for_upi'):
            await handle_upi_link(update, user_id, text)
            context.user_data['waiting_for_upi'] = False
        else:
            await update.message.reply_text("Use the menu buttons below.")


async def handle_balance(update: Update, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (
            await db.execute("SELECT balance FROM user_balance WHERE user_id = ?", (user_id,))
        ).fetchone()
        balance = row[0] if row else 0.0
    await update.message.reply_text(f"💰 Your Balance: ₹{balance:.2f}")


async def handle_refer_earn(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (
            await db.execute("SELECT referral_count FROM user_balance WHERE user_id = ?", (user_id,))
        ).fetchone()
        referral_count = row[0] if row else 0

    bot_username = context.bot.username or "Kingwa_bot"
    referral_earnings = referral_count * 5

    await update.message.reply_text(
        f"👥 Your Referral Link:\nhttps://t.me/{bot_username}?start={user_id}\n\n"
        f"Total Referrals: {referral_count}\n"
        f"Earnings from Referrals: ₹{referral_earnings:.2f}\n\n"
        f"Earn ₹5 per referral!"
    )


async def handle_bonus(update: Update, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (
            await db.execute("SELECT balance, last_bonus_claim FROM user_balance WHERE user_id = ?", (user_id,))
        ).fetchone()
        balance = row[0] if row else 0.0
        last_bonus = row[1] if row else None

    now = datetime.utcnow().isoformat()
    can_claim = True

    if last_bonus:
        last_claim = datetime.fromisoformat(last_bonus)
        time_diff = (datetime.utcnow() - last_claim).total_seconds()
        if time_diff < 86400:
            hours_left = (86400 - time_diff) / 3600
            await update.message.reply_text(f"⏳ You can claim bonus in {hours_left:.1f} hours")
            can_claim = False

    if can_claim:
        new_balance = balance + 1.0
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE user_balance SET balance = ?, last_bonus_claim = ? WHERE user_id = ?",
                (new_balance, now, user_id)
            )
            await db.commit()
        await update.message.reply_text(
            f"✅ Bonus claimed! ₹1.00 added.\nNew Balance: ₹{new_balance:.2f}"
        )


async def handle_withdraw(update: Update, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (
            await db.execute("SELECT balance, upi_id FROM user_balance WHERE user_id = ?", (user_id,))
        ).fetchone()
        balance = row[0] if row else 0.0
        upi_id = row[1] if row else None

    if not upi_id:
        await update.message.reply_text("💸 Please link your UPI ID first using '🏦 Link UPI' button")
    elif balance < 50:
        await update.message.reply_text(f"💸 Minimum withdrawal: ₹50\nYour Balance: ₹{balance:.2f}")
    else:
        await update.message.reply_text(
            f"💸 Withdrawal Request:\nAmount: ₹{balance:.2f}\nUPI: {upi_id}\n\n"
            f"Your request has been submitted. You'll receive the amount within 24 hours."
        )
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE user_balance SET balance = 0.0 WHERE user_id = ?",
                (user_id,)
            )
            await db.commit()


async def handle_upi_link(update: Update, user_id: int, upi_id: str):
    if "@" not in upi_id or len(upi_id) < 5:
        await update.message.reply_text("❌ Invalid UPI format. Please use format like name@upi")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE user_balance SET upi_id = ? WHERE user_id = ?",
            (upi_id, user_id)
        )
        await db.commit()
    await update.message.reply_text(f"✅ UPI ID linked successfully: {upi_id}")


def validate_telegram_init_data(init_data: str, bot_token: str):
    try:
        params = {}
        for item in init_data.split("&"):
            if "=" in item:
                k, v = item.split("=", 1)
                params[k] = v

        received_hash = params.pop("hash", "")
        data_check_string = "\n".join(
            f"{k}={unquote(v)}" for k, v in sorted(params.items())
        )

        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if hmac.compare_digest(computed_hash, received_hash):
            user_str = params.get("user", "{}")
            return json.loads(unquote(user_str))
        return None
    except Exception as e:
        logger.error(f"initData validation error: {e}")
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/bot/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class VerifyRequest(BaseModel):
    init_data: str
    device_id: str


@app.get("/bot/verify")
async def serve_verify_page():
    from fastapi.responses import Response
    html_path = STATIC_DIR / "verify.html"
    content = html_path.read_text(encoding="utf-8")
    headers = {
        "X-Frame-Options": "ALLOWALL",
        "Content-Security-Policy": "default-src * 'unsafe-inline' 'unsafe-eval' data: blob:;",
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-cache",
    }
    return Response(content=content, media_type="text/html", headers=headers)


@app.post("/bot/api/verify-device")
async def verify_device(payload: VerifyRequest):
    user_data = validate_telegram_init_data(payload.init_data, BOT_TOKEN)
    if not user_data:
        raise HTTPException(status_code=403, detail="Invalid Telegram session")

    user_id = user_data.get("id")
    if not user_id:
        raise HTTPException(status_code=400, detail="User ID missing")

    device_id = payload.device_id

    async with aiosqlite.connect(DB_PATH) as db:
        row = await (
            await db.execute(
                "SELECT user_id FROM device_registry WHERE device_id = ?", (device_id,)
            )
        ).fetchone()

        if row:
            existing_user = row[0]
            if existing_user != user_id:
                return {
                    "status": "blocked",
                    "message": "This device is already registered with another account.",
                }

        if not row:
            await db.execute(
                "INSERT OR REPLACE INTO device_registry (device_id, user_id) VALUES (?, ?)",
                (device_id, user_id),
            )

        now = datetime.utcnow().isoformat()
        await db.execute(
            """INSERT INTO users (user_id, username, first_name, is_verified, device_id, verified_at)
               VALUES (?, ?, ?, 1, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 is_verified = 1, device_id = excluded.device_id, verified_at = excluded.verified_at""",
            (user_id, user_data.get("username"), user_data.get("first_name"), device_id, now),
        )
        await db.commit()

    return {
        "status": "verified",
        "user": {
            "id": user_id,
            "first_name": user_data.get("first_name"),
            "username": user_data.get("username"),
        },
    }


@app.get("/bot/healthz")
async def health():
    return {"status": "ok"}


async def run_bot():
    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start_command))
    # ✅ Channel join check callback
    bot_app.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    bot_app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, button_handler))
    logger.info(f"Starting bot polling...")
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(drop_pending_updates=True)
    return bot_app


async def main():
    bot_app = await run_bot()
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
