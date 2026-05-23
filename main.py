import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import string
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import aiosqlite
import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.path.join(os.path.dirname(__file__), "bot_data.db")
PORT = int(os.getenv("PORT", "8000"))

ADMIN_ID = 7856754202

REPLIT_DOMAINS = os.getenv("REPLIT_DOMAINS", "")
MANUAL_WEBAPP_URL = "https://tg-bot2026-dj1f.onrender.com/bot/verify"

if MANUAL_WEBAPP_URL:
    WEBAPP_URL = MANUAL_WEBAPP_URL
elif REPLIT_DOMAINS:
    PUBLIC_HOST = REPLIT_DOMAINS.split(",")[0].strip()
    WEBAPP_URL = f"https://{PUBLIC_HOST}/bot/verify"
else:
    WEBAPP_URL = f"http://localhost:{PORT}/bot/verify"

VSV_API_URL = "https://vsv-gateway-solutions.co.in/Api/api.php"
VSV_TOKEN = "RTCLFTJV"


# ===================== DATABASE =====================

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
                upi_id TEXT,
                vsv_wallet TEXT,
                email TEXT,
                mobile TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_username TEXT NOT NULL,
                channel_link TEXT NOT NULL,
                channel_name TEXT,
                is_active INTEGER DEFAULT 1,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS withdrawal_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                upi_id TEXT,
                vsv_wallet TEXT,
                method TEXT DEFAULT 'upi',
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                processed_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS redeem_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                amount REAL NOT NULL,
                user_id INTEGER NOT NULL,
                email TEXT,
                mobile TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Default settings
        defaults = [
            ("refer_reward", "5"),
            ("min_withdrawal", "50"),
            ("welcome_bonus", "10"),
            ("withdrawal_enabled", "1"),
            ("redeem_code_price", "10"),
        ]
        for key, val in defaults:
            await db.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES (?, ?)", (key, val))
        await db.commit()
    logger.info("Database initialized")


async def get_setting(key: str, default="0"):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT value FROM bot_settings WHERE key=?", (key,))).fetchone()
    return row[0] if row else default


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()


async def get_active_channels():
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute("SELECT id, channel_username, channel_link, channel_name FROM channels WHERE is_active = 1")).fetchall()
    return rows


async def check_all_channels(bot, user_id: int) -> bool:
    channels = await get_active_channels()
    if not channels:
        return True
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=f"@{ch[1]}", user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
                return False
        except Exception as e:
            logger.error(f"Channel check error {ch[1]}: {e}")
            return False
    return True


async def send_join_message(update, user_id: int, bot=None):
    channels = await get_active_channels()
    keyboard = []
    for ch in channels:
        name = ch[3] or ch[1]
        keyboard.append([InlineKeyboardButton(f"Join {name}", url=ch[2])])
    keyboard.append([InlineKeyboardButton("I Have Joined All Channels", callback_data="check_join")])
    text = "Welcome!\n\nPlease join these channels first:\n\nAfter joining all, click the button below."
    if hasattr(update, 'message') and update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif hasattr(update, 'edit_message_text'):
        await update.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


def get_user_keyboard(user_id: int):
    rows = [
        [KeyboardButton("Balance"), KeyboardButton("Refer & Earn")],
        [KeyboardButton("Bonus"), KeyboardButton("Withdraw")],
        [KeyboardButton("Link UPI"), KeyboardButton("Link VSV Wallet")],
        [KeyboardButton("Leaderboard"), KeyboardButton("Redeem Code")],
        [KeyboardButton("Support")],
    ]
    if user_id == ADMIN_ID:
        rows.append([KeyboardButton("Admin Panel")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def send_main_menu(update: Update, name: str, user_id: int):
    await update.message.reply_text(
        f"Welcome {name}!\n\nUse the buttons below to navigate.",
        reply_markup=get_user_keyboard(user_id)
    )


# ===================== COMMAND HANDLERS =====================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    referrer_id = None
    if context.args:
        try:
            referrer_id = int(context.args[0])
            if referrer_id == user.id:
                referrer_id = None
        except:
            pass

    async with aiosqlite.connect(DB_PATH) as db:
        existing = await (await db.execute("SELECT user_id FROM users WHERE user_id=?", (user.id,))).fetchone()
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
            (user.id, user.username, user.first_name)
        )
        await db.execute("INSERT OR IGNORE INTO user_balance (user_id) VALUES (?)", (user.id,))
        await db.commit()

        if not existing and referrer_id:
            refer_reward = float(await get_setting("refer_reward", "5"))
            await db.execute("UPDATE user_balance SET balance = balance + ?, referral_count = referral_count + 1 WHERE user_id=?", (refer_reward, referrer_id))
            await db.commit()
            try:
                await context.bot.send_message(chat_id=referrer_id, text=f"Someone joined using your referral link! You earned Rs.{refer_reward:.2f}")
            except:
                pass

        if not existing:
            welcome_bonus = float(await get_setting("welcome_bonus", "10"))
            if welcome_bonus > 0:
                await db.execute("UPDATE user_balance SET balance = balance + ? WHERE user_id=?", (welcome_bonus, user.id))
                await db.commit()

        row = await (await db.execute("SELECT is_verified FROM users WHERE user_id = ?", (user.id,))).fetchone()
        is_verified = row[0] if row else 0

    is_member = await check_all_channels(context.bot, user.id)
    if not is_member:
        await send_join_message(update, user.id)
        return

    if is_verified:
        await send_main_menu(update, user.first_name, user.id)
    else:
        keyboard = [[InlineKeyboardButton("Verify Device", web_app=WebAppInfo(url=WEBAPP_URL))]]
        await update.message.reply_text("Please verify your device to start using the bot.", reply_markup=InlineKeyboardMarkup(keyboard))


async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    is_member = await check_all_channels(context.bot, user.id)
    if not is_member:
        await query.answer("You have not joined all channels yet!", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT is_verified FROM users WHERE user_id = ?", (user.id,))).fetchone()
        is_verified = row[0] if row else 0

    if is_verified:
        await query.edit_message_text("All channels joined!")
        await query.message.reply_text("Welcome!", reply_markup=get_user_keyboard(user.id))
    else:
        keyboard = [[InlineKeyboardButton("Verify Device", web_app=WebAppInfo(url=WEBAPP_URL))]]
        await query.edit_message_text("Channels joined!\n\nNow verify your device:", reply_markup=InlineKeyboardMarkup(keyboard))


async def web_app_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.message.web_app_data.data
    user = update.effective_user
    try:
        payload = json.loads(data)
        if payload.get("status") == "verified":
            await send_main_menu(update, user.first_name, user.id)
        elif payload.get("status") == "blocked":
            await update.message.reply_text("Verification failed. This device is already linked to another account.")
        else:
            await update.message.reply_text("Verification failed. Please try /start again.")
    except Exception as e:
        logger.error(f"web_app_data error: {e}")
        await update.message.reply_text("Something went wrong. Try /start again.")


# ===================== BUTTON HANDLER =====================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    user = update.effective_user

    # Admin panel button - only admin sees it but handle safely
    if text == "Admin Panel":
        if user_id != ADMIN_ID:
            return
        await handle_admin_panel_menu(update, context)
        return

    # Admin panel sub-actions
    if context.user_data.get('admin_action'):
        await handle_admin_action_input(update, context, text)
        return

    is_member = await check_all_channels(context.bot, user_id)
    if not is_member:
        await send_join_message(update, user_id)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT is_verified FROM users WHERE user_id = ?", (user_id,))).fetchone()
        is_verified = row[0] if row else 0

    if not is_verified:
        keyboard = [[InlineKeyboardButton("Verify Device", web_app=WebAppInfo(url=WEBAPP_URL))]]
        await update.message.reply_text("Please verify your device first.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if text == "Balance":
        await handle_balance(update, user_id)
    elif text == "Refer & Earn":
        await handle_refer_earn(update, user_id, context)
    elif text == "Bonus":
        await handle_bonus(update, user_id)
    elif text == "Withdraw":
        await handle_withdraw(update, user_id, context)
    elif text == "Link UPI":
        context.user_data['waiting_for'] = 'upi'
        await update.message.reply_text("Send your UPI ID (e.g. name@upi)")
    elif text == "Link VSV Wallet":
        context.user_data['waiting_for'] = 'vsv'
        await update.message.reply_text("Send your VSV Wallet number (10 digits)")
    elif text == "Leaderboard":
        await handle_leaderboard(update)
    elif text == "Redeem Code":
        await handle_redeem_code_menu(update, user_id, context)
    elif text == "Support":
        await update.message.reply_text("For support contact: @rishabh_044")
    else:
        waiting = context.user_data.get('waiting_for')
        if waiting == 'upi':
            await handle_upi_link(update, user_id, text)
            context.user_data['waiting_for'] = None
        elif waiting == 'vsv':
            await handle_vsv_link(update, user_id, text)
            context.user_data['waiting_for'] = None
        elif waiting == 'withdraw_amount':
            await handle_withdraw_amount(update, user_id, context, text)
        elif waiting == 'redeem_buy_amount':
            await handle_redeem_buy(update, user_id, context, text)
        elif waiting == 'redeem_email':
            context.user_data['redeem_email'] = text
            context.user_data['waiting_for'] = 'redeem_mobile'
            await update.message.reply_text("Now send your mobile number:")
        elif waiting == 'redeem_mobile':
            await handle_redeem_finalize(update, user_id, context, text)
        elif waiting == 'redeem_use':
            await handle_redeem_use(update, user_id, text)
            context.user_data['waiting_for'] = None
        else:
            await update.message.reply_text("Use the menu buttons.")


# ===================== ADMIN PANEL IN BOT =====================

async def handle_admin_panel_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = [
        [KeyboardButton("Total Users"), KeyboardButton("Withdrawal Requests")],
        [KeyboardButton("Add Channel"), KeyboardButton("Remove Channel")],
        [KeyboardButton("Update Channel"), KeyboardButton("Broadcast Message")],
        [KeyboardButton("Set Refer Reward"), KeyboardButton("Set Min Withdrawal")],
        [KeyboardButton("Set Welcome Bonus"), KeyboardButton("Withdraw ON/OFF")],
        [KeyboardButton("Manual Balance"), KeyboardButton("Approve Withdrawal")],
        [KeyboardButton("Reject Withdrawal"), KeyboardButton("Back to Menu")],
    ]
    await update.message.reply_text(
        "Admin Panel\n\nChoose an action:",
        reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True)
    )
    context.user_data['in_admin'] = True


async def handle_admin_action_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return

    action = context.user_data.get('admin_action')

    if text == "Back to Menu":
        context.user_data.clear()
        await send_main_menu(update, update.effective_user.first_name, user_id)
        return
    if text == "Admin Panel":
        context.user_data.clear()
        await handle_admin_panel_menu(update, context)
        return

    if action == 'add_channel':
        parts = text.split("|")
        if len(parts) < 2:
            await update.message.reply_text("Wrong format. Send:\nChannelName|@username|https://t.me/link")
            return
        name = parts[0].strip()
        username = parts[1].strip().replace("@", "")
        link = parts[2].strip() if len(parts) > 2 else f"https://t.me/{username}"
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO channels (channel_username, channel_link, channel_name) VALUES (?,?,?)", (username, link, name))
            await db.commit()
        await update.message.reply_text(f"Channel added: {name}")
        context.user_data.clear()

    elif action == 'remove_channel':
        try:
            ch_id = int(text)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE channels SET is_active=0 WHERE id=?", (ch_id,))
                await db.commit()
            await update.message.reply_text(f"Channel ID {ch_id} removed.")
        except:
            await update.message.reply_text("Send valid channel ID number.")
        context.user_data.clear()

    elif action == 'update_channel':
        parts = text.split("|")
        if len(parts) < 3:
            await update.message.reply_text("Format: ID|@newusername|https://newlink")
            return
        try:
            ch_id = int(parts[0].strip())
            username = parts[1].strip().replace("@", "")
            link = parts[2].strip()
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE channels SET channel_username=?, channel_link=? WHERE id=?", (username, link, ch_id))
                await db.commit()
            await update.message.reply_text(f"Channel ID {ch_id} updated.")
        except:
            await update.message.reply_text("Invalid format.")
        context.user_data.clear()

    elif action == 'set_refer_reward':
        try:
            val = float(text)
            await set_setting("refer_reward", str(val))
            await update.message.reply_text(f"Refer reward set to Rs.{val}")
        except:
            await update.message.reply_text("Send a valid number.")
        context.user_data.clear()

    elif action == 'set_min_withdrawal':
        try:
            val = float(text)
            await set_setting("min_withdrawal", str(val))
            await update.message.reply_text(f"Minimum withdrawal set to Rs.{val}")
        except:
            await update.message.reply_text("Send a valid number.")
        context.user_data.clear()

    elif action == 'set_welcome_bonus':
        try:
            val = float(text)
            await set_setting("welcome_bonus", str(val))
            await update.message.reply_text(f"Welcome bonus set to Rs.{val}")
        except:
            await update.message.reply_text("Send a valid number.")
        context.user_data.clear()

    elif action == 'broadcast':
        async with aiosqlite.connect(DB_PATH) as db:
            rows = await (await db.execute("SELECT user_id FROM users")).fetchall()
        sent = 0
        for row in rows:
            try:
                await update.get_bot().send_message(chat_id=row[0], text=text)
                sent += 1
                await asyncio.sleep(0.05)
            except:
                pass
        await update.message.reply_text(f"Broadcast sent to {sent} users.")
        context.user_data.clear()

    elif action == 'manual_balance':
        parts = text.split("|")
        if len(parts) < 2:
            await update.message.reply_text("Format: UserID|Amount\n(Use negative amount to deduct)")
            return
        try:
            uid = int(parts[0].strip())
            amount = float(parts[1].strip())
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE user_balance SET balance = balance + ? WHERE user_id=?", (amount, uid))
                await db.commit()
            action_word = "Added" if amount >= 0 else "Deducted"
            await update.message.reply_text(f"{action_word} Rs.{abs(amount)} for user {uid}")
        except:
            await update.message.reply_text("Invalid format.")
        context.user_data.clear()

    elif action == 'approve_withdrawal':
        try:
            req_id = int(text)
            async with aiosqlite.connect(DB_PATH) as db:
                req = await (await db.execute("SELECT user_id, amount, vsv_wallet, upi_id, method FROM withdrawal_requests WHERE id=? AND status='pending'", (req_id,))).fetchone()
                if not req:
                    await update.message.reply_text("Request not found or already processed.")
                    context.user_data.clear()
                    return
                uid, amount, vsv_wallet, upi_id, method = req
                await db.execute("UPDATE withdrawal_requests SET status='approved', processed_at=? WHERE id=?", (datetime.utcnow().isoformat(), req_id))
                await db.commit()

            # Auto pay via VSV API if vsv_wallet
            if method == 'vsv' and vsv_wallet:
                pay_url = f"{VSV_API_URL}?token={VSV_TOKEN}&paytm={vsv_wallet}&amount={amount}&comment=Withdrawal+from+bot"
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(pay_url, timeout=15)
                    await update.message.reply_text(f"Payment API response: {resp.text[:300]}")
                except Exception as e:
                    await update.message.reply_text(f"Payment API error: {e}")
            else:
                await update.message.reply_text(f"Approved! UPI: {upi_id} Amount: Rs.{amount}\nPay manually.")

            try:
                await update.get_bot().send_message(chat_id=uid, text=f"Your withdrawal of Rs.{amount} has been approved!")
            except:
                pass
        except:
            await update.message.reply_text("Send valid request ID.")
        context.user_data.clear()

    elif action == 'reject_withdrawal':
        try:
            req_id = int(text)
            async with aiosqlite.connect(DB_PATH) as db:
                req = await (await db.execute("SELECT user_id, amount FROM withdrawal_requests WHERE id=? AND status='pending'", (req_id,))).fetchone()
                if not req:
                    await update.message.reply_text("Request not found.")
                    context.user_data.clear()
                    return
                uid, amount = req
                await db.execute("UPDATE withdrawal_requests SET status='rejected', processed_at=? WHERE id=?", (datetime.utcnow().isoformat(), req_id))
                # Refund balance
                await db.execute("UPDATE user_balance SET balance = balance + ? WHERE user_id=?", (amount, uid))
                await db.commit()
            try:
                await update.get_bot().send_message(chat_id=uid, text=f"Your withdrawal request of Rs.{amount} has been rejected. Amount refunded to your balance.")
            except:
                pass
            await update.message.reply_text(f"Request {req_id} rejected and amount refunded.")
        except:
            await update.message.reply_text("Send valid request ID.")
        context.user_data.clear()


async def handle_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return

    if text == "Total Users":
        async with aiosqlite.connect(DB_PATH) as db:
            total = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
            verified = (await (await db.execute("SELECT COUNT(*) FROM users WHERE is_verified=1")).fetchone())[0]
        await update.message.reply_text(f"Total Users: {total}\nVerified Users: {verified}")

    elif text == "Withdrawal Requests":
        async with aiosqlite.connect(DB_PATH) as db:
            rows = await (await db.execute(
                "SELECT id, user_id, amount, method, upi_id, vsv_wallet, created_at FROM withdrawal_requests WHERE status='pending' ORDER BY created_at DESC LIMIT 20"
            )).fetchall()
        if not rows:
            await update.message.reply_text("No pending withdrawal requests.")
            return
        msg = "Pending Withdrawal Requests:\n\n"
        for r in rows:
            msg += f"ID: {r[0]} | User: {r[1]} | Rs.{r[2]} | {r[3].upper()}\n"
            if r[3] == 'upi':
                msg += f"UPI: {r[4]}\n"
            else:
                msg += f"VSV: {r[5]}\n"
            msg += f"Date: {r[6][:10]}\n\n"
        await update.message.reply_text(msg)

    elif text == "Add Channel":
        context.user_data['admin_action'] = 'add_channel'
        await update.message.reply_text("Send channel details in format:\nChannelName|@username|https://t.me/link")

    elif text == "Remove Channel":
        async with aiosqlite.connect(DB_PATH) as db:
            rows = await (await db.execute("SELECT id, channel_name, channel_username FROM channels WHERE is_active=1")).fetchall()
        if not rows:
            await update.message.reply_text("No active channels.")
            return
        msg = "Active Channels:\n\n"
        for r in rows:
            msg += f"ID: {r[0]} | {r[1] or r[2]} | @{r[2]}\n"
        msg += "\nSend the channel ID to remove:"
        context.user_data['admin_action'] = 'remove_channel'
        await update.message.reply_text(msg)

    elif text == "Update Channel":
        async with aiosqlite.connect(DB_PATH) as db:
            rows = await (await db.execute("SELECT id, channel_name, channel_username FROM channels WHERE is_active=1")).fetchall()
        if not rows:
            await update.message.reply_text("No active channels.")
            return
        msg = "Active Channels:\n\n"
        for r in rows:
            msg += f"ID: {r[0]} | {r[1] or r[2]} | @{r[2]}\n"
        msg += "\nSend in format: ID|@newusername|https://newlink"
        context.user_data['admin_action'] = 'update_channel'
        await update.message.reply_text(msg)

    elif text == "Set Refer Reward":
        current = await get_setting("refer_reward", "5")
        context.user_data['admin_action'] = 'set_refer_reward'
        await update.message.reply_text(f"Current refer reward: Rs.{current}\nSend new amount:")

    elif text == "Set Min Withdrawal":
        current = await get_setting("min_withdrawal", "50")
        context.user_data['admin_action'] = 'set_min_withdrawal'
        await update.message.reply_text(f"Current minimum withdrawal: Rs.{current}\nSend new amount:")

    elif text == "Set Welcome Bonus":
        current = await get_setting("welcome_bonus", "10")
        context.user_data['admin_action'] = 'set_welcome_bonus'
        await update.message.reply_text(f"Current welcome bonus: Rs.{current}\nSend new amount:")

    elif text == "Withdraw ON/OFF":
        current = await get_setting("withdrawal_enabled", "1")
        new_val = "0" if current == "1" else "1"
        await set_setting("withdrawal_enabled", new_val)
        status = "ENABLED" if new_val == "1" else "DISABLED"
        await update.message.reply_text(f"Withdrawal is now {status}")

    elif text == "Broadcast Message":
        context.user_data['admin_action'] = 'broadcast'
        await update.message.reply_text("Send the message to broadcast to all users:")

    elif text == "Manual Balance":
        context.user_data['admin_action'] = 'manual_balance'
        await update.message.reply_text("Send in format:\nUserID|Amount\n\nExample: 123456|50\nFor deduction: 123456|-20")

    elif text == "Approve Withdrawal":
        async with aiosqlite.connect(DB_PATH) as db:
            rows = await (await db.execute(
                "SELECT id, user_id, amount, method FROM withdrawal_requests WHERE status='pending' LIMIT 10"
            )).fetchall()
        if not rows:
            await update.message.reply_text("No pending requests.")
            return
        msg = "Pending requests:\n"
        for r in rows:
            msg += f"ID: {r[0]} | User: {r[1]} | Rs.{r[2]} | {r[3].upper()}\n"
        msg += "\nSend request ID to approve:"
        context.user_data['admin_action'] = 'approve_withdrawal'
        await update.message.reply_text(msg)

    elif text == "Reject Withdrawal":
        async with aiosqlite.connect(DB_PATH) as db:
            rows = await (await db.execute(
                "SELECT id, user_id, amount, method FROM withdrawal_requests WHERE status='pending' LIMIT 10"
            )).fetchall()
        if not rows:
            await update.message.reply_text("No pending requests.")
            return
        msg = "Pending requests:\n"
        for r in rows:
            msg += f"ID: {r[0]} | User: {r[1]} | Rs.{r[2]} | {r[3].upper()}\n"
        msg += "\nSend request ID to reject (amount will be refunded):"
        context.user_data['admin_action'] = 'reject_withdrawal'
        await update.message.reply_text(msg)

    elif text == "Back to Menu":
        context.user_data.clear()
        await send_main_menu(update, update.effective_user.first_name, user_id)


# ===================== USER FEATURE HANDLERS =====================

async def handle_balance(update, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT balance, referral_count FROM user_balance WHERE user_id = ?", (user_id,))).fetchone()
    balance = row[0] if row else 0.0
    refs = row[1] if row else 0
    await update.message.reply_text(
        f"Your Balance: Rs.{balance:.2f}\n\nTotal Referrals: {refs}"
    )


async def handle_refer_earn(update, user_id, context):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT referral_count FROM user_balance WHERE user_id = ?", (user_id,))).fetchone()
    referral_count = row[0] if row else 0
    refer_reward = await get_setting("refer_reward", "5")
    bot_username = context.bot.username or "bot"
    await update.message.reply_text(
        f"Your Referral Link:\nhttps://t.me/{bot_username}?start={user_id}\n\n"
        f"Total Referrals: {referral_count}\n"
        f"Earn Rs.{refer_reward} per referral!"
    )


async def handle_bonus(update, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT balance, last_bonus_claim FROM user_balance WHERE user_id = ?", (user_id,))).fetchone()
    balance = row[0] if row else 0.0
    last_bonus = row[1] if row else None
    now = datetime.utcnow().isoformat()

    if last_bonus:
        time_diff = (datetime.utcnow() - datetime.fromisoformat(last_bonus)).total_seconds()
        if time_diff < 86400:
            hours_left = (86400 - time_diff) / 3600
            await update.message.reply_text(f"Come back in {hours_left:.1f} hours to claim your daily bonus.")
            return

    new_balance = balance + 1.0
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE user_balance SET balance = ?, last_bonus_claim = ? WHERE user_id = ?", (new_balance, now, user_id))
        await db.commit()
    await update.message.reply_text(f"Daily bonus claimed! Rs.1.00 added.\nNew Balance: Rs.{new_balance:.2f}")


async def handle_withdraw(update, user_id, context):
    withdrawal_enabled = await get_setting("withdrawal_enabled", "1")
    if withdrawal_enabled == "0":
        await update.message.reply_text("Withdrawals are currently disabled. Please try again later.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT balance, upi_id, vsv_wallet FROM user_balance WHERE user_id = ?", (user_id,))).fetchone()
    balance = row[0] if row else 0.0
    upi_id = row[1] if row else None
    vsv_wallet = row[2] if row else None
    min_withdrawal = float(await get_setting("min_withdrawal", "50"))

    if balance < min_withdrawal:
        await update.message.reply_text(f"Minimum withdrawal amount is Rs.{min_withdrawal:.0f}\nYour Balance: Rs.{balance:.2f}")
        return

    if not upi_id and not vsv_wallet:
        await update.message.reply_text("Please link your UPI ID or VSV Wallet first before withdrawing.")
        return

    keyboard = []
    if upi_id:
        keyboard.append([InlineKeyboardButton(f"Withdraw via UPI ({upi_id})", callback_data=f"wd_upi_{user_id}")])
    if vsv_wallet:
        keyboard.append([InlineKeyboardButton(f"Withdraw via VSV Wallet ({vsv_wallet})", callback_data=f"wd_vsv_{user_id}")])

    context.user_data['withdraw_balance'] = balance
    context.user_data['waiting_for'] = 'withdraw_amount'
    context.user_data['withdraw_upi'] = upi_id
    context.user_data['withdraw_vsv'] = vsv_wallet

    await update.message.reply_text(
        f"Your Balance: Rs.{balance:.2f}\n\nSend the amount you want to withdraw (min Rs.{min_withdrawal:.0f}):",
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
    )


async def handle_withdraw_amount(update, user_id, context, text):
    try:
        amount = float(text)
    except:
        await update.message.reply_text("Please send a valid amount.")
        return

    balance = context.user_data.get('withdraw_balance', 0)
    min_withdrawal = float(await get_setting("min_withdrawal", "50"))
    upi_id = context.user_data.get('withdraw_upi')
    vsv_wallet = context.user_data.get('withdraw_vsv')

    if amount < min_withdrawal:
        await update.message.reply_text(f"Minimum withdrawal is Rs.{min_withdrawal:.0f}")
        return
    if amount > balance:
        await update.message.reply_text(f"Insufficient balance. Your balance: Rs.{balance:.2f}")
        return

    method = 'vsv' if vsv_wallet and not upi_id else 'upi'

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE user_balance SET balance = balance - ? WHERE user_id=?", (amount, user_id))
        await db.execute(
            "INSERT INTO withdrawal_requests (user_id, amount, upi_id, vsv_wallet, method) VALUES (?,?,?,?,?)",
            (user_id, amount, upi_id, vsv_wallet, method)
        )
        await db.commit()

    context.user_data['waiting_for'] = None

    # Notify admin
    try:
        admin_msg = f"New Withdrawal Request!\n\nUser ID: {user_id}\nAmount: Rs.{amount:.2f}\nMethod: {method.upper()}\n"
        if method == 'upi':
            admin_msg += f"UPI: {upi_id}"
        else:
            admin_msg += f"VSV Wallet: {vsv_wallet}"
        await update.get_bot().send_message(chat_id=ADMIN_ID, text=admin_msg)
    except:
        pass

    await update.message.reply_text(
        f"Withdrawal request submitted!\nAmount: Rs.{amount:.2f}\nMethod: {method.upper()}\n\nAdmin will process it shortly."
    )


async def handle_upi_link(update, user_id, upi_id):
    if "@" not in upi_id or len(upi_id) < 5:
        await update.message.reply_text("Invalid UPI ID. Format: name@upi")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE user_balance SET upi_id = ? WHERE user_id = ?", (upi_id, user_id))
        await db.commit()
    await update.message.reply_text(f"UPI Linked: {upi_id}")


async def handle_vsv_link(update, user_id, vsv_number):
    vsv_number = vsv_number.strip()
    if not vsv_number.isdigit() or len(vsv_number) != 10:
        await update.message.reply_text("Invalid VSV Wallet number. It must be exactly 10 digits.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE user_balance SET vsv_wallet = ? WHERE user_id = ?", (vsv_number, user_id))
        await db.commit()
    await update.message.reply_text(f"VSV Wallet Linked: {vsv_number}")


async def handle_leaderboard(update):
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT u.first_name, u.username, b.balance, b.referral_count FROM user_balance b JOIN users u ON b.user_id=u.user_id ORDER BY b.balance DESC LIMIT 10"
        )).fetchall()
    if not rows:
        await update.message.reply_text("No data yet.")
        return
    msg = "Top 10 Leaderboard:\n\n"
    for i, r in enumerate(rows, 1):
        name = r[0] or (f"@{r[1]}" if r[1] else "User")
        msg += f"{i}. {name} - Rs.{r[2]:.2f} | {r[3]} Referrals\n"
    await update.message.reply_text(msg)


async def handle_redeem_code_menu(update, user_id, context):
    keyboard = [
        [InlineKeyboardButton("Buy Redeem Code", callback_data="redeem_buy")],
        [InlineKeyboardButton("Use Redeem Code", callback_data="redeem_use")],
    ]
    await update.message.reply_text(
        "Redeem Code Options:\n\nBuy a redeem code (Rs.10 minimum) and receive it on your email.\nOr use an existing redeem code to add balance.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_redeem_buy(update, user_id, context, text):
    try:
        amount = float(text)
    except:
        await update.message.reply_text("Please send a valid amount (minimum Rs.10).")
        return

    redeem_price = float(await get_setting("redeem_code_price", "10"))
    if amount < redeem_price:
        await update.message.reply_text(f"Minimum redeem code amount is Rs.{redeem_price:.0f}")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT balance FROM user_balance WHERE user_id=?", (user_id,))).fetchone()
    balance = row[0] if row else 0.0

    if balance < amount:
        await update.message.reply_text(f"Insufficient balance. Your balance: Rs.{balance:.2f}")
        context.user_data['waiting_for'] = None
        return

    context.user_data['redeem_amount'] = amount
    context.user_data['waiting_for'] = 'redeem_email'
    await update.message.reply_text("Please send your email address to receive the redeem code:")


async def handle_redeem_finalize(update, user_id, context, mobile):
    amount = context.user_data.get('redeem_amount', 0)
    email = context.user_data.get('redeem_email', '')

    if not email or amount <= 0:
        await update.message.reply_text("Something went wrong. Please start again.")
        context.user_data.clear()
        return

    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT balance FROM user_balance WHERE user_id=?", (user_id,))).fetchone()
    balance = row[0] if row else 0.0
    if balance < amount:
        await update.message.reply_text("Insufficient balance.")
        context.user_data.clear()
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE user_balance SET balance = balance - ? WHERE user_id=?", (amount, user_id))
        await db.execute(
            "INSERT INTO redeem_codes (code, amount, user_id, email, mobile, status) VALUES (?,?,?,?,?,'pending')",
            ("PENDING", amount, user_id, email, mobile)
        )
        req_id = (await (await db.execute("SELECT last_insert_rowid()")).fetchone())[0]
        await db.commit()

    context.user_data.clear()

    # Notify admin
    try:
        admin_msg = (
            f"New Redeem Code Request!\n\n"
            f"Request ID: {req_id}\n"
            f"User ID: {user_id}\n"
            f"Amount: Rs.{amount:.2f}\n"
            f"Email: {email}\n"
            f"Mobile: {mobile}\n\n"
            f"Please generate the code manually and send to the user's email."
        )
        await update.get_bot().send_message(chat_id=ADMIN_ID, text=admin_msg)
    except:
        pass

    await update.message.reply_text(
        f"Redeem code request submitted!\n\nAmount: Rs.{amount:.2f}\nEmail: {email}\nMobile: {mobile}\n\nAdmin will send the code to your email shortly."
    )


async def handle_redeem_use(update, user_id, code):
    code = code.strip().upper()
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT id, amount, status FROM redeem_codes WHERE code=?", (code,))).fetchone()
        if not row:
            await update.message.reply_text("Invalid redeem code.")
            return
        if row[2] != 'active':
            await update.message.reply_text("This code has already been used or is not active yet.")
            return
        amount = row[1]
        await db.execute("UPDATE redeem_codes SET status='used' WHERE id=?", (row[0],))
        await db.execute("UPDATE user_balance SET balance = balance + ? WHERE user_id=?", (amount, user_id))
        await db.commit()
    await update.message.reply_text(f"Redeem code applied! Rs.{amount:.2f} added to your balance.")


# ===================== CALLBACK QUERY HANDLER =====================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "check_join":
        await check_join_callback(update, context)
    elif data == "redeem_buy":
        context.user_data['waiting_for'] = 'redeem_buy_amount'
        redeem_price = await get_setting("redeem_code_price", "10")
        await query.message.reply_text(f"Send the amount for the redeem code (minimum Rs.{redeem_price}):")
    elif data == "redeem_use":
        context.user_data['waiting_for'] = 'redeem_use'
        await query.message.reply_text("Send your redeem code:")
    elif data.startswith("wd_upi_"):
        context.user_data['withdraw_method'] = 'upi'
    elif data.startswith("wd_vsv_"):
        context.user_data['withdraw_method'] = 'vsv'


# ===================== VALIDATION =====================

def validate_telegram_init_data(init_data: str, bot_token: str):
    try:
        params = {}
        for item in init_data.split("&"):
            if "=" in item:
                k, v = item.split("=", 1)
                params[k] = v
        received_hash = params.pop("hash", "")
        data_check_string = "\n".join(f"{k}={unquote(v)}" for k, v in sorted(params.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(computed_hash, received_hash):
            return json.loads(unquote(params.get("user", "{}")))
        return None
    except Exception as e:
        logger.error(f"initData validation error: {e}")
        return None


# ===================== FASTAPI =====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/bot/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ===================== ADMIN HTML PANEL =====================

ADMIN_PANEL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin Panel</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; }
.header { background: #1e293b; padding: 16px 20px; border-bottom: 1px solid #334155; display: flex; align-items: center; gap: 12px; }
.header h1 { font-size: 20px; font-weight: 700; color: #38bdf8; }
.badge { background: #ef4444; color: white; font-size: 10px; padding: 2px 8px; border-radius: 10px; font-weight: 700; }
.container { max-width: 900px; margin: 0 auto; padding: 20px; }
.stats { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; margin-bottom: 24px; }
.stat-card { background: #1e293b; border-radius: 12px; padding: 16px; border: 1px solid #334155; text-align: center; }
.stat-number { font-size: 28px; font-weight: 800; color: #38bdf8; }
.stat-label { font-size: 12px; color: #94a3b8; margin-top: 4px; }
.section { background: #1e293b; border-radius: 12px; padding: 20px; margin-bottom: 16px; border: 1px solid #334155; }
.section h2 { font-size: 15px; font-weight: 700; color: #f1f5f9; margin-bottom: 16px; }
.input-row { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
input, select, textarea { background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 10px 14px; color: #e2e8f0; font-size: 14px; flex: 1; min-width: 140px; outline: none; }
input:focus, textarea:focus { border-color: #38bdf8; }
.btn { padding: 10px 18px; border-radius: 8px; border: none; font-size: 14px; font-weight: 700; cursor: pointer; transition: opacity 0.2s; white-space: nowrap; }
.btn:active { opacity: 0.8; }
.btn-blue { background: #38bdf8; color: #0f172a; }
.btn-green { background: #22c55e; color: #0f172a; }
.btn-red { background: #ef4444; color: white; }
.btn-yellow { background: #f59e0b; color: #0f172a; }
.btn-purple { background: #a855f7; color: white; }
.channel-list { display: flex; flex-direction: column; gap: 8px; }
.channel-item { background: #0f172a; border-radius: 8px; padding: 12px 14px; display: flex; align-items: center; justify-content: space-between; border: 1px solid #334155; }
.channel-name { font-weight: 600; font-size: 14px; }
.channel-user { font-size: 12px; color: #94a3b8; }
.user-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.user-table th { text-align: left; padding: 8px 10px; color: #94a3b8; border-bottom: 1px solid #334155; font-weight: 600; }
.user-table td { padding: 8px 10px; border-bottom: 1px solid #1e293b; }
.verified-badge { background: #22c55e22; color: #22c55e; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.unverified-badge { background: #ef444422; color: #ef4444; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.pending-badge { background: #f59e0b22; color: #f59e0b; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.msg { padding: 10px 14px; border-radius: 8px; font-size: 13px; margin-top: 10px; display: none; }
.msg-success { background: #22c55e22; color: #22c55e; border: 1px solid #22c55e44; }
.msg-error { background: #ef444422; color: #ef4444; border: 1px solid #ef444444; }
.tab-bar { display: flex; gap: 8px; margin-bottom: 20px; flex-wrap: wrap; }
.tab { padding: 8px 16px; border-radius: 8px; border: 1px solid #334155; background: #1e293b; color: #94a3b8; font-size: 13px; font-weight: 600; cursor: pointer; }
.tab.active { background: #38bdf8; color: #0f172a; border-color: #38bdf8; }
.tab-content { display: none; }
.tab-content.active { display: block; }
.toggle-btn { padding: 10px 20px; border-radius: 8px; border: none; font-size: 14px; font-weight: 700; cursor: pointer; }
.toggle-on { background: #22c55e; color: #0f172a; }
.toggle-off { background: #ef4444; color: white; }
</style>
</head>
<body>
<div class="header">
  <div><h1>Admin Panel</h1></div>
  <span class="badge">ADMIN</span>
</div>
<div class="container">
  <div class="stats">
    <div class="stat-card"><div class="stat-number" id="totalUsers">-</div><div class="stat-label">Total Users</div></div>
    <div class="stat-card"><div class="stat-number" id="verifiedUsers">-</div><div class="stat-label">Verified Users</div></div>
    <div class="stat-card"><div class="stat-number" id="totalChannels">-</div><div class="stat-label">Active Channels</div></div>
    <div class="stat-card"><div class="stat-number" id="totalBalance">-</div><div class="stat-label">Total Balance</div></div>
  </div>

  <div class="tab-bar">
    <div class="tab active" onclick="switchTab('channels')">Channels</div>
    <div class="tab" onclick="switchTab('users')">Users</div>
    <div class="tab" onclick="switchTab('withdrawals')">Withdrawals</div>
    <div class="tab" onclick="switchTab('settings')">Settings</div>
    <div class="tab" onclick="switchTab('redeem')">Redeem</div>
    <div class="tab" onclick="switchTab('broadcast')">Broadcast</div>
    <div class="tab" onclick="switchTab('balance')">Balance</div>
  </div>

  <!-- Channels Tab -->
  <div class="tab-content active" id="tab-channels">
    <div class="section">
      <h2>Add Channel</h2>
      <div class="input-row">
        <input id="chName" placeholder="Channel Name" />
        <input id="chUsername" placeholder="Username (e.g. mychannel)" />
      </div>
      <div class="input-row">
        <input id="chLink" placeholder="Link (https://t.me/mychannel)" />
        <button class="btn btn-green" onclick="addChannel()">Add</button>
      </div>
      <div class="msg" id="chMsg"></div>
    </div>
    <div class="section">
      <h2>Active Channels</h2>
      <div class="channel-list" id="channelList">Loading...</div>
    </div>
  </div>

  <!-- Users Tab -->
  <div class="tab-content" id="tab-users">
    <div class="section">
      <h2>Search User</h2>
      <div class="input-row">
        <input id="searchUserId" placeholder="User ID" type="number" />
        <button class="btn btn-blue" onclick="searchUser()">Search</button>
      </div>
      <div id="userDetail"></div>
    </div>
    <div class="section">
      <h2>All Users (Latest 100)</h2>
      <table class="user-table">
        <thead><tr><th>ID</th><th>Name</th><th>Balance</th><th>Referrals</th><th>Status</th><th>Action</th></tr></thead>
        <tbody id="userTableBody">Loading...</tbody>
      </table>
    </div>
  </div>

  <!-- Withdrawals Tab -->
  <div class="tab-content" id="tab-withdrawals">
    <div class="section">
      <h2>Pending Withdrawal Requests</h2>
      <div id="withdrawalList">Loading...</div>
    </div>
  </div>

  <!-- Settings Tab -->
  <div class="tab-content" id="tab-settings">
    <div class="section">
      <h2>Per Refer Reward (Rs.)</h2>
      <div class="input-row">
        <input id="referReward" placeholder="Amount" type="number" step="0.1" />
        <button class="btn btn-blue" onclick="saveSetting('refer_reward','referReward','referRewardMsg')">Save</button>
      </div>
      <div class="msg" id="referRewardMsg"></div>
    </div>
    <div class="section">
      <h2>Minimum Withdrawal (Rs.)</h2>
      <div class="input-row">
        <input id="minWithdrawal" placeholder="Amount" type="number" step="1" />
        <button class="btn btn-blue" onclick="saveSetting('min_withdrawal','minWithdrawal','minWithdrawalMsg')">Save</button>
      </div>
      <div class="msg" id="minWithdrawalMsg"></div>
    </div>
    <div class="section">
      <h2>Welcome Bonus (Rs.)</h2>
      <div class="input-row">
        <input id="welcomeBonus" placeholder="Amount" type="number" step="0.1" />
        <button class="btn btn-blue" onclick="saveSetting('welcome_bonus','welcomeBonus','welcomeBonusMsg')">Save</button>
      </div>
      <div class="msg" id="welcomeBonusMsg"></div>
    </div>
    <div class="section">
      <h2>Withdraw Feature</h2>
      <button id="withdrawToggleBtn" class="toggle-btn" onclick="toggleWithdrawal()">Loading...</button>
      <div class="msg" id="withdrawToggleMsg"></div>
    </div>
  </div>

  <!-- Redeem Tab -->
  <div class="tab-content" id="tab-redeem">
    <div class="section">
      <h2>Pending Redeem Code Requests</h2>
      <div id="redeemList">Loading...</div>
    </div>
    <div class="section">
      <h2>Approve Redeem Code (Generate & Send)</h2>
      <p style="font-size:13px;color:#94a3b8;margin-bottom:12px;">Enter the request ID and the code you want to assign. User will be notified via bot.</p>
      <div class="input-row">
        <input id="redeemReqId" placeholder="Request ID" type="number" />
        <input id="redeemCode" placeholder="Redeem Code (e.g. PROMO2024)" />
        <button class="btn btn-green" onclick="approveRedeem()">Approve</button>
      </div>
      <div class="msg" id="redeemApproveMsg"></div>
    </div>
  </div>

  <!-- Broadcast Tab -->
  <div class="tab-content" id="tab-broadcast">
    <div class="section">
      <h2>Broadcast Message to All Users</h2>
      <textarea id="broadcastMsg" placeholder="Write your message..." style="width:100%;background:#0f172a;border:1px solid #334155;border-radius:8px;padding:12px;color:#e2e8f0;font-size:14px;min-height:100px;resize:vertical;outline:none;margin-bottom:12px;"></textarea>
      <button class="btn btn-blue" onclick="sendBroadcast()">Send to All</button>
      <div class="msg" id="broadcastResult"></div>
    </div>
  </div>

  <!-- Balance Tab -->
  <div class="tab-content" id="tab-balance">
    <div class="section">
      <h2>Add / Remove Balance</h2>
      <div class="input-row">
        <input id="balUserId" placeholder="User ID" type="number" />
        <input id="balAmount" placeholder="Amount (negative to deduct)" type="number" step="0.01" />
        <button class="btn btn-green" onclick="changeBalance()">Apply</button>
      </div>
      <div class="msg" id="balMsg"></div>
    </div>
    <div class="section">
      <h2>Reset User Balance</h2>
      <div class="input-row">
        <input id="resetUserId" placeholder="User ID" type="number" />
        <button class="btn btn-red" onclick="resetBalance()">Reset to Zero</button>
      </div>
      <div class="msg" id="resetMsg"></div>
    </div>
    <div class="section">
      <h2>Unverify User</h2>
      <div class="input-row">
        <input id="unverifyUserId" placeholder="User ID" type="number" />
        <button class="btn btn-yellow" onclick="unverifyUser()">Unverify</button>
      </div>
      <div class="msg" id="unverifyMsg"></div>
    </div>
  </div>

</div>

<script>
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');
  if (name === 'withdrawals') loadWithdrawals();
  if (name === 'redeem') loadRedeemRequests();
  if (name === 'settings') loadSettings();
}

function showMsg(id, text, ok) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'msg ' + (ok ? 'msg-success' : 'msg-error');
  el.style.display = 'block';
  setTimeout(() => el.style.display = 'none', 4000);
}

async function loadStats() {
  const r = await fetch('/admin/api/stats');
  const d = await r.json();
  document.getElementById('totalUsers').textContent = d.total_users;
  document.getElementById('verifiedUsers').textContent = d.verified_users;
  document.getElementById('totalChannels').textContent = d.total_channels;
  document.getElementById('totalBalance').textContent = 'Rs.' + d.total_balance.toFixed(0);
}

async function loadChannels() {
  const r = await fetch('/admin/api/channels');
  const d = await r.json();
  const list = document.getElementById('channelList');
  if (!d.channels.length) { list.innerHTML = '<div style="color:#94a3b8;font-size:13px;">No channels added yet</div>'; return; }
  list.innerHTML = d.channels.map(ch => `
    <div class="channel-item">
      <div>
        <div class="channel-name">${ch.channel_name || ch.channel_username}</div>
        <div class="channel-user">@${ch.channel_username}</div>
      </div>
      <div style="display:flex;gap:6px;">
        <button class="btn btn-red" onclick="removeChannel(${ch.id})" style="padding:6px 12px;font-size:12px;">Remove</button>
      </div>
    </div>
  `).join('');
}

async function addChannel() {
  const name = document.getElementById('chName').value.trim();
  const username = document.getElementById('chUsername').value.trim().replace('@','');
  const link = document.getElementById('chLink').value.trim();
  if (!username || !link) { showMsg('chMsg', 'Username and Link are required!', false); return; }
  const r = await fetch('/admin/api/channels/add', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({channel_username: username, channel_link: link, channel_name: name})
  });
  const d = await r.json();
  if (d.success) { showMsg('chMsg', 'Channel added!', true); loadChannels(); loadStats(); document.getElementById('chName').value=''; document.getElementById('chUsername').value=''; document.getElementById('chLink').value=''; }
  else showMsg('chMsg', d.error || 'Error', false);
}

async function removeChannel(id) {
  if (!confirm('Remove this channel?')) return;
  const r = await fetch('/admin/api/channels/remove', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({channel_id: id}) });
  const d = await r.json();
  if (d.success) { loadChannels(); loadStats(); }
}

async function loadUsers() {
  const r = await fetch('/admin/api/users');
  const d = await r.json();
  const tbody = document.getElementById('userTableBody');
  if (!d.users.length) { tbody.innerHTML = '<tr><td colspan="6" style="color:#94a3b8;">No users</td></tr>'; return; }
  tbody.innerHTML = d.users.map(u => `
    <tr>
      <td>${u.user_id}</td>
      <td>${u.first_name || '-'}</td>
      <td>Rs.${(u.balance||0).toFixed(2)}</td>
      <td>${u.referral_count || 0}</td>
      <td><span class="${u.is_verified ? 'verified-badge' : 'unverified-badge'}">${u.is_verified ? 'Verified' : 'Unverified'}</span></td>
      <td><button class="btn btn-yellow" onclick="document.getElementById('balUserId').value=${u.user_id};switchTabByName('balance')" style="padding:4px 8px;font-size:11px;">Balance</button></td>
    </tr>
  `).join('');
}

function switchTabByName(name) {
  document.querySelectorAll('.tab').forEach(t => { if(t.textContent.trim().toLowerCase()===name) t.click(); });
}

async function searchUser() {
  const uid = document.getElementById('searchUserId').value;
  if (!uid) return;
  const r = await fetch('/admin/api/user/' + uid);
  const d = await r.json();
  const el = document.getElementById('userDetail');
  if (d.error) { el.innerHTML = '<div style="color:#ef4444;margin-top:10px;">User not found</div>'; return; }
  el.innerHTML = `<div style="background:#0f172a;border-radius:8px;padding:14px;margin-top:12px;border:1px solid #334155;">
    <div><b>ID:</b> ${d.user_id}</div>
    <div><b>Name:</b> ${d.first_name || '-'}</div>
    <div><b>Username:</b> @${d.username || '-'}</div>
    <div><b>Balance:</b> Rs.${(d.balance||0).toFixed(2)}</div>
    <div><b>Referrals:</b> ${d.referral_count || 0}</div>
    <div><b>Status:</b> ${d.is_verified ? 'Verified' : 'Unverified'}</div>
    <div><b>UPI:</b> ${d.upi_id || 'Not linked'}</div>
    <div><b>VSV Wallet:</b> ${d.vsv_wallet || 'Not linked'}</div>
  </div>`;
}

async function loadWithdrawals() {
  const r = await fetch('/admin/api/withdrawals');
  const d = await r.json();
  const el = document.getElementById('withdrawalList');
  if (!d.requests.length) { el.innerHTML = '<div style="color:#94a3b8;">No pending withdrawals</div>'; return; }
  el.innerHTML = d.requests.map(w => `
    <div style="background:#0f172a;border-radius:8px;padding:14px;margin-bottom:10px;border:1px solid #334155;">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
        <div>
          <div style="font-weight:700;">ID: ${w.id} | User: ${w.user_id}</div>
          <div style="color:#38bdf8;font-size:15px;font-weight:700;">Rs.${w.amount.toFixed(2)}</div>
          <div style="font-size:12px;color:#94a3b8;">${w.method.toUpperCase()} | ${w.method==='upi' ? w.upi_id : w.vsv_wallet}</div>
          <div style="font-size:11px;color:#64748b;">${w.created_at.slice(0,16)}</div>
        </div>
        <div style="display:flex;gap:6px;">
          <button class="btn btn-green" onclick="processWithdrawal(${w.id},'approve')" style="padding:6px 14px;font-size:13px;">Approve</button>
          <button class="btn btn-red" onclick="processWithdrawal(${w.id},'reject')" style="padding:6px 14px;font-size:13px;">Reject</button>
        </div>
      </div>
    </div>
  `).join('');
}

async function processWithdrawal(id, action) {
  if (!confirm(action === 'approve' ? 'Approve this withdrawal?' : 'Reject and refund this withdrawal?')) return;
  const r = await fetch('/admin/api/withdrawal/' + action, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({request_id: id})
  });
  const d = await r.json();
  if (d.success) loadWithdrawals();
  else alert(d.error || 'Error');
}

async function loadSettings() {
  const r = await fetch('/admin/api/settings');
  const d = await r.json();
  document.getElementById('referReward').value = d.refer_reward || '';
  document.getElementById('minWithdrawal').value = d.min_withdrawal || '';
  document.getElementById('welcomeBonus').value = d.welcome_bonus || '';
  const btn = document.getElementById('withdrawToggleBtn');
  const on = d.withdrawal_enabled === '1';
  btn.textContent = on ? 'Withdrawal: ON (Click to Disable)' : 'Withdrawal: OFF (Click to Enable)';
  btn.className = 'toggle-btn ' + (on ? 'toggle-on' : 'toggle-off');
}

async function saveSetting(key, inputId, msgId) {
  const val = document.getElementById(inputId).value.trim();
  if (!val) { showMsg(msgId, 'Enter a value', false); return; }
  const r = await fetch('/admin/api/settings/save', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({key, value: val})
  });
  const d = await r.json();
  showMsg(msgId, d.success ? 'Saved!' : 'Error', d.success);
}

async function toggleWithdrawal() {
  const r = await fetch('/admin/api/settings/toggle-withdrawal', { method: 'POST' });
  const d = await r.json();
  showMsg('withdrawToggleMsg', d.success ? 'Updated!' : 'Error', d.success);
  loadSettings();
}

async function changeBalance() {
  const uid = document.getElementById('balUserId').value;
  const amount = document.getElementById('balAmount').value;
  if (!uid || !amount) { showMsg('balMsg', 'User ID and amount required!', false); return; }
  const r = await fetch('/admin/api/add-bonus', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({user_id: parseInt(uid), amount: parseFloat(amount)}) });
  const d = await r.json();
  showMsg('balMsg', d.success ? 'Balance updated!' : 'Error', d.success);
  if (d.success) loadUsers();
}

async function resetBalance() {
  const uid = document.getElementById('resetUserId').value;
  if (!uid) { showMsg('resetMsg', 'Enter User ID!', false); return; }
  if (!confirm('Reset balance to zero?')) return;
  const r = await fetch('/admin/api/reset-balance', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({user_id: parseInt(uid)}) });
  const d = await r.json();
  showMsg('resetMsg', d.success ? 'Balance reset!' : 'Error', d.success);
  if (d.success) loadUsers();
}

async function unverifyUser() {
  const uid = document.getElementById('unverifyUserId').value;
  if (!uid) { showMsg('unverifyMsg', 'Enter User ID!', false); return; }
  if (!confirm('Unverify this user?')) return;
  const r = await fetch('/admin/api/unverify-user', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({user_id: parseInt(uid)}) });
  const d = await r.json();
  showMsg('unverifyMsg', d.success ? 'User unverified!' : 'Error', d.success);
  if (d.success) loadUsers();
}

async function sendBroadcast() {
  const msg = document.getElementById('broadcastMsg').value.trim();
  if (!msg) { showMsg('broadcastResult', 'Write a message first!', false); return; }
  if (!confirm('Send to all users?')) return;
  showMsg('broadcastResult', 'Sending...', true);
  const r = await fetch('/admin/api/broadcast', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({message: msg}) });
  const d = await r.json();
  showMsg('broadcastResult', d.success ? 'Sent to ' + d.sent + ' users!' : 'Error', d.success);
}

async function loadRedeemRequests() {
  const r = await fetch('/admin/api/redeem-requests');
  const d = await r.json();
  const el = document.getElementById('redeemList');
  if (!d.requests.length) { el.innerHTML = '<div style="color:#94a3b8;">No pending redeem requests</div>'; return; }
  el.innerHTML = d.requests.map(req => `
    <div style="background:#0f172a;border-radius:8px;padding:12px;margin-bottom:8px;border:1px solid #334155;">
      <div><b>ID: ${req.id}</b> | User: ${req.user_id}</div>
      <div>Amount: Rs.${req.amount.toFixed(2)}</div>
      <div>Email: ${req.email}</div>
      <div>Mobile: ${req.mobile}</div>
      <div style="font-size:11px;color:#64748b;">${req.created_at.slice(0,16)}</div>
    </div>
  `).join('');
}

async function approveRedeem() {
  const reqId = document.getElementById('redeemReqId').value;
  const code = document.getElementById('redeemCode').value.trim().toUpperCase();
  if (!reqId || !code) { showMsg('redeemApproveMsg', 'Enter request ID and code', false); return; }
  const r = await fetch('/admin/api/redeem-approve', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({request_id: parseInt(reqId), code})
  });
  const d = await r.json();
  showMsg('redeemApproveMsg', d.success ? 'Redeem code approved and user notified!' : (d.error || 'Error'), d.success);
  if (d.success) { loadRedeemRequests(); document.getElementById('redeemReqId').value=''; document.getElementById('redeemCode').value=''; }
}

loadStats();
loadChannels();
loadUsers();
setInterval(loadStats, 30000);
</script>
</body>
</html>"""


def is_admin(request: Request) -> bool:
    admin_key = request.headers.get("X-Admin-ID") or request.query_params.get("admin_id")
    return str(admin_key) == str(ADMIN_ID)


@app.get("/admin")
async def admin_panel(request: Request):
    admin_id = request.query_params.get("admin_id")
    if str(admin_id) != str(ADMIN_ID):
        raise HTTPException(status_code=403, detail="Access Denied")
    return HTMLResponse(content=ADMIN_PANEL_HTML)


@app.get("/admin/api/stats")
async def admin_stats(request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403)
    async with aiosqlite.connect(DB_PATH) as db:
        total = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        verified = (await (await db.execute("SELECT COUNT(*) FROM users WHERE is_verified=1")).fetchone())[0]
        channels = (await (await db.execute("SELECT COUNT(*) FROM channels WHERE is_active=1")).fetchone())[0]
        balance_row = await (await db.execute("SELECT SUM(balance) FROM user_balance")).fetchone()
        total_balance = balance_row[0] or 0.0
    return {"total_users": total, "verified_users": verified, "total_channels": channels, "total_balance": total_balance}


@app.get("/admin/api/channels")
async def admin_channels(request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403)
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute("SELECT id, channel_username, channel_link, channel_name FROM channels WHERE is_active=1")).fetchall()
    return {"channels": [{"id": r[0], "channel_username": r[1], "channel_link": r[2], "channel_name": r[3]} for r in rows]}


class ChannelAdd(BaseModel):
    channel_username: str
    channel_link: str
    channel_name: str = ""


@app.post("/admin/api/channels/add")
async def admin_add_channel(payload: ChannelAdd, request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO channels (channel_username, channel_link, channel_name) VALUES (?, ?, ?)",
                         (payload.channel_username, payload.channel_link, payload.channel_name))
        await db.commit()
    return {"success": True}


class ChannelRemove(BaseModel):
    channel_id: int


@app.post("/admin/api/channels/remove")
async def admin_remove_channel(payload: ChannelRemove, request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE channels SET is_active=0 WHERE id=?", (payload.channel_id,))
        await db.commit()
    return {"success": True}


@app.get("/admin/api/users")
async def admin_users(request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403)
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT u.user_id, u.first_name, u.username, u.is_verified, COALESCE(b.balance,0), COALESCE(b.referral_count,0), b.upi_id, b.vsv_wallet FROM users u LEFT JOIN user_balance b ON u.user_id=b.user_id ORDER BY u.created_at DESC LIMIT 100"
        )).fetchall()
    return {"users": [{"user_id": r[0], "first_name": r[1], "username": r[2], "is_verified": r[3], "balance": r[4], "referral_count": r[5], "upi_id": r[6], "vsv_wallet": r[7]} for r in rows]}


@app.get("/admin/api/user/{user_id}")
async def admin_get_user(user_id: int, request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403)
    async with aiosqlite.connect(DB_PATH) as db:
        u = await (await db.execute("SELECT user_id, first_name, username, is_verified FROM users WHERE user_id=?", (user_id,))).fetchone()
        b = await (await db.execute("SELECT balance, upi_id, vsv_wallet, referral_count FROM user_balance WHERE user_id=?", (user_id,))).fetchone()
    if not u:
        return {"error": "Not found"}
    return {"user_id": u[0], "first_name": u[1], "username": u[2], "is_verified": u[3],
            "balance": b[0] if b else 0, "upi_id": b[1] if b else None,
            "vsv_wallet": b[2] if b else None, "referral_count": b[3] if b else 0}


class BonusPayload(BaseModel):
    user_id: int
    amount: float


@app.post("/admin/api/add-bonus")
async def admin_add_bonus(payload: BonusPayload, request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE user_balance SET balance = balance + ? WHERE user_id=?", (payload.amount, payload.user_id))
        await db.commit()
    return {"success": True}


class UserIdPayload(BaseModel):
    user_id: int


@app.post("/admin/api/reset-balance")
async def admin_reset_balance(payload: UserIdPayload, request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE user_balance SET balance=0.0 WHERE user_id=?", (payload.user_id,))
        await db.commit()
    return {"success": True}


@app.post("/admin/api/unverify-user")
async def admin_unverify(payload: UserIdPayload, request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_verified=0, device_id=NULL WHERE user_id=?", (payload.user_id,))
        await db.execute("DELETE FROM device_registry WHERE user_id=?", (payload.user_id,))
        await db.commit()
    return {"success": True}


class BroadcastPayload(BaseModel):
    message: str

bot_app_global = None

@app.post("/admin/api/broadcast")
async def admin_broadcast(payload: BroadcastPayload, request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403)
    if not bot_app_global:
        return {"success": False, "error": "Bot not ready"}
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute("SELECT user_id FROM users")).fetchall()
    sent = 0
    for row in rows:
        try:
            await bot_app_global.bot.send_message(chat_id=row[0], text=payload.message)
            sent += 1
            await asyncio.sleep(0.05)
        except:
            pass
    return {"success": True, "sent": sent}


@app.get("/admin/api/withdrawals")
async def admin_withdrawals(request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403)
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT id, user_id, amount, method, upi_id, vsv_wallet, created_at FROM withdrawal_requests WHERE status='pending' ORDER BY created_at DESC"
        )).fetchall()
    return {"requests": [{"id": r[0], "user_id": r[1], "amount": r[2], "method": r[3], "upi_id": r[4], "vsv_wallet": r[5], "created_at": r[6]} for r in rows]}


class WithdrawalActionPayload(BaseModel):
    request_id: int


@app.post("/admin/api/withdrawal/approve")
async def admin_approve_withdrawal(payload: WithdrawalActionPayload, request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403)
    async with aiosqlite.connect(DB_PATH) as db:
        req = await (await db.execute("SELECT user_id, amount, vsv_wallet, upi_id, method FROM withdrawal_requests WHERE id=? AND status='pending'", (payload.request_id,))).fetchone()
        if not req:
            return {"success": False, "error": "Request not found"}
        uid, amount, vsv_wallet, upi_id, method = req
        await db.execute("UPDATE withdrawal_requests SET status='approved', processed_at=? WHERE id=?", (datetime.utcnow().isoformat(), payload.request_id))
        await db.commit()

    api_result = None
    if method == 'vsv' and vsv_wallet and bot_app_global:
        pay_url = f"{VSV_API_URL}?token={VSV_TOKEN}&paytm={vsv_wallet}&amount={amount}&comment=Withdrawal"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(pay_url, timeout=15)
            api_result = resp.text[:200]
        except Exception as e:
            api_result = str(e)

    if bot_app_global:
        try:
            await bot_app_global.bot.send_message(chat_id=uid, text=f"Your withdrawal of Rs.{amount:.2f} has been approved!")
        except:
            pass

    return {"success": True, "api_result": api_result}


@app.post("/admin/api/withdrawal/reject")
async def admin_reject_withdrawal(payload: WithdrawalActionPayload, request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403)
    async with aiosqlite.connect(DB_PATH) as db:
        req = await (await db.execute("SELECT user_id, amount FROM withdrawal_requests WHERE id=? AND status='pending'", (payload.request_id,))).fetchone()
        if not req:
            return {"success": False, "error": "Request not found"}
        uid, amount = req
        await db.execute("UPDATE withdrawal_requests SET status='rejected', processed_at=? WHERE id=?", (datetime.utcnow().isoformat(), payload.request_id))
        await db.execute("UPDATE user_balance SET balance = balance + ? WHERE user_id=?", (amount, uid))
        await db.commit()

    if bot_app_global:
        try:
            await bot_app_global.bot.send_message(chat_id=uid, text=f"Your withdrawal request of Rs.{amount:.2f} was rejected. Amount refunded to your balance.")
        except:
            pass
    return {"success": True}


@app.get("/admin/api/settings")
async def admin_get_settings(request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403)
    keys = ["refer_reward", "min_withdrawal", "welcome_bonus", "withdrawal_enabled"]
    result = {}
    for k in keys:
        result[k] = await get_setting(k)
    return result


class SettingSavePayload(BaseModel):
    key: str
    value: str


@app.post("/admin/api/settings/save")
async def admin_save_setting(payload: SettingSavePayload, request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403)
    allowed_keys = ["refer_reward", "min_withdrawal", "welcome_bonus", "redeem_code_price"]
    if payload.key not in allowed_keys:
        raise HTTPException(status_code=400, detail="Invalid key")
    await set_setting(payload.key, payload.value)
    return {"success": True}


@app.post("/admin/api/settings/toggle-withdrawal")
async def admin_toggle_withdrawal(request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403)
    current = await get_setting("withdrawal_enabled", "1")
    new_val = "0" if current == "1" else "1"
    await set_setting("withdrawal_enabled", new_val)
    return {"success": True, "withdrawal_enabled": new_val}


@app.get("/admin/api/redeem-requests")
async def admin_redeem_requests(request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403)
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT id, user_id, amount, email, mobile, created_at FROM redeem_codes WHERE status='pending' ORDER BY created_at DESC"
        )).fetchall()
    return {"requests": [{"id": r[0], "user_id": r[1], "amount": r[2], "email": r[3], "mobile": r[4], "created_at": r[5]} for r in rows]}


class RedeemApprovePayload(BaseModel):
    request_id: int
    code: str


@app.post("/admin/api/redeem-approve")
async def admin_redeem_approve(payload: RedeemApprovePayload, request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403)
    async with aiosqlite.connect(DB_PATH) as db:
        req = await (await db.execute("SELECT user_id, amount FROM redeem_codes WHERE id=? AND status='pending'", (payload.request_id,))).fetchone()
        if not req:
            return {"success": False, "error": "Request not found"}
        uid, amount = req
        await db.execute("UPDATE redeem_codes SET code=?, status='active' WHERE id=?", (payload.code.upper(), payload.request_id))
        await db.commit()

    if bot_app_global:
        try:
            await bot_app_global.bot.send_message(
                chat_id=uid,
                text=f"Your redeem code for Rs.{amount:.2f} is ready!\n\nCode: {payload.code.upper()}\n\nYou can use this code via the Redeem Code button in the bot."
            )
        except:
            pass
    return {"success": True}


# ===================== BOT ENDPOINTS =====================

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
        row = await (await db.execute("SELECT user_id FROM device_registry WHERE device_id=?", (device_id,))).fetchone()
        if row and row[0] != user_id:
            return {"status": "blocked", "message": "Device already registered."}
        if not row:
            await db.execute("INSERT OR REPLACE INTO device_registry (device_id, user_id) VALUES (?, ?)", (device_id, user_id))
        now = datetime.utcnow().isoformat()
        await db.execute(
            """INSERT INTO users (user_id, username, first_name, is_verified, device_id, verified_at)
               VALUES (?, ?, ?, 1, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET is_verified=1, device_id=excluded.device_id, verified_at=excluded.verified_at""",
            (user_id, user_data.get("username"), user_data.get("first_name"), device_id, now)
        )
        # Give welcome bonus on first verify
        welcome_bonus = float(await get_setting("welcome_bonus", "10"))
        existing_balance = await (await db.execute("SELECT balance FROM user_balance WHERE user_id=?", (user_id,))).fetchone()
        if not existing_balance:
            await db.execute("INSERT OR IGNORE INTO user_balance (user_id, balance) VALUES (?,?)", (user_id, welcome_bonus))
        await db.commit()
    return {"status": "verified", "user": {"id": user_id, "first_name": user_data.get("first_name")}}


@app.get("/bot/healthz")
async def health():
    return {"status": "ok"}


# ===================== BOT MAIN =====================

async def run_bot():
    global bot_app_global
    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CallbackQueryHandler(callback_handler))
    bot_app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, combined_message_handler))
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(drop_pending_updates=True)
    bot_app_global = bot_app
    return bot_app


async def combined_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    # Admin panel navigation buttons
    admin_nav = ["Total Users", "Withdrawal Requests", "Add Channel", "Remove Channel",
                 "Update Channel", "Broadcast Message", "Set Refer Reward", "Set Min Withdrawal",
                 "Set Welcome Bonus", "Withdraw ON/OFF", "Manual Balance", "Approve Withdrawal",
                 "Reject Withdrawal", "Back to Menu"]

    if user_id == ADMIN_ID and (text in admin_nav or context.user_data.get('in_admin') or context.user_data.get('admin_action')):
        if context.user_data.get('admin_action'):
            await handle_admin_action_input(update, context, text)
        else:
            context.user_data['in_admin'] = True
            await handle_admin_text(update, context, text)
        return

    await button_handler(update, context)


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
