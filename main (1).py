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

# ✅ Sirf tu Admin hai
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
        # ✅ Channels table
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
        await db.commit()
    logger.info("Database initialized")


async def get_active_channels():
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute("SELECT id, channel_username, channel_link, channel_name FROM channels WHERE is_active = 1")).fetchall()
    return rows


async def check_all_channels(bot, user_id: int) -> bool:
    channels = await get_active_channels()
    if not channels:
        return True  # Koi channel nahi toh skip
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
        keyboard.append([InlineKeyboardButton(f"📢 Join {name}", url=ch[2])])
    keyboard.append([InlineKeyboardButton("✅ Maine Sab Join Kar Liya", callback_data="check_join")])

    text = "👋 Hey! Welcome To Bot!\n\n🔴 Pehle Ye Channels Join Karo:\n\n💥 Sab join karne ke baad neeche button dabaao"
    if hasattr(update, 'message') and update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif hasattr(update, 'edit_message_text'):
        await update.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)", (user.id, user.username, user.first_name))
        await db.execute("INSERT OR IGNORE INTO user_balance (user_id) VALUES (?)", (user.id,))
        await db.commit()
        row = await (await db.execute("SELECT is_verified FROM users WHERE user_id = ?", (user.id,))).fetchone()
        is_verified = row[0] if row else 0

    is_member = await check_all_channels(context.bot, user.id)
    if not is_member:
        await send_join_message(update, user.id)
        return

    if is_verified:
        await send_main_menu(update, user.first_name)
    else:
        keyboard = [[InlineKeyboardButton("🔐 Verify", web_app=WebAppInfo(url=WEBAPP_URL))]]
        await update.message.reply_text("🔒 Verify Yourself To Start Bot", reply_markup=InlineKeyboardMarkup(keyboard))


async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    is_member = await check_all_channels(context.bot, user.id)
    if not is_member:
        await query.answer("❌ Aapne abhi bhi sab channels join nahi kiye!", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT is_verified FROM users WHERE user_id = ?", (user.id,))).fetchone()
        is_verified = row[0] if row else 0

    if is_verified:
        await query.edit_message_text("✅ Sab sahi hai!")
        keyboard = [
            [KeyboardButton("💰 Balance"), KeyboardButton("👥 Refer Earn")],
            [KeyboardButton("🎁 Bonus"), KeyboardButton("💸 Withdraw")],
            [KeyboardButton("🏦 Link UPI")],
        ]
        await query.message.reply_text("🏠 Welcome To UPI Giveaway Bot!", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    else:
        keyboard = [[InlineKeyboardButton("🔐 Verify", web_app=WebAppInfo(url=WEBAPP_URL))]]
        await query.edit_message_text("✅ Channels join ho gaye!\n\n🔒 Ab apna device verify karo:", reply_markup=InlineKeyboardMarkup(keyboard))


async def send_main_menu(update: Update, name: str):
    keyboard = [
        [KeyboardButton("💰 Balance"), KeyboardButton("👥 Refer Earn")],
        [KeyboardButton("🎁 Bonus"), KeyboardButton("💸 Withdraw")],
        [KeyboardButton("🏦 Link UPI")],
    ]
    await update.message.reply_text("🏠 Welcome To UPI Giveaway Bot!\n\nHow to Earn: (((CLICK HERE )))", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))


async def web_app_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.message.web_app_data.data
    user = update.effective_user
    try:
        payload = json.loads(data)
        if payload.get("status") == "verified":
            await send_main_menu(update, user.first_name)
        elif payload.get("status") == "blocked":
            await update.message.reply_text("⛔ Verification failed.\nThis device is already linked to another account.")
        else:
            await update.message.reply_text("⚠️ Verification failed. Please try /start again.")
    except Exception as e:
        logger.error(f"web_app_data error: {e}")
        await update.message.reply_text("⚠️ Something went wrong. Try /start again.")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    is_member = await check_all_channels(context.bot, user_id)
    if not is_member:
        await send_join_message(update, user_id)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT is_verified FROM users WHERE user_id = ?", (user_id,))).fetchone()
        is_verified = row[0] if row else 0

    if not is_verified:
        keyboard = [[InlineKeyboardButton("🔐 Verify", web_app=WebAppInfo(url=WEBAPP_URL))]]
        await update.message.reply_text("🔒 Please verify your device first.", reply_markup=InlineKeyboardMarkup(keyboard))
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
        await update.message.reply_text("🏦 Send your UPI ID (e.g. name@upi)")
    else:
        if context.user_data.get('waiting_for_upi'):
            await handle_upi_link(update, user_id, text)
            context.user_data['waiting_for_upi'] = False
        else:
            await update.message.reply_text("Use the menu buttons below.")


async def handle_balance(update, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT balance FROM user_balance WHERE user_id = ?", (user_id,))).fetchone()
    await update.message.reply_text(f"💰 Your Balance: ₹{row[0]:.2f}" if row else "💰 Balance: ₹0.00")


async def handle_refer_earn(update, user_id, context):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT referral_count FROM user_balance WHERE user_id = ?", (user_id,))).fetchone()
    referral_count = row[0] if row else 0
    bot_username = context.bot.username or "Kingwa_bot"
    await update.message.reply_text(
        f"👥 Your Referral Link:\nhttps://t.me/{bot_username}?start={user_id}\n\n"
        f"Total Referrals: {referral_count}\nEarnings: ₹{referral_count * 5:.2f}\n\nEarn ₹5 per referral!"
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
            await update.message.reply_text(f"⏳ Bonus claim karo {hours_left:.1f} hours mein")
            return

    new_balance = balance + 1.0
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE user_balance SET balance = ?, last_bonus_claim = ? WHERE user_id = ?", (new_balance, now, user_id))
        await db.commit()
    await update.message.reply_text(f"✅ Bonus claimed! ₹1.00 added.\nNew Balance: ₹{new_balance:.2f}")


async def handle_withdraw(update, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT balance, upi_id FROM user_balance WHERE user_id = ?", (user_id,))).fetchone()
    balance = row[0] if row else 0.0
    upi_id = row[1] if row else None

    if not upi_id:
        await update.message.reply_text("💸 Pehle UPI link karo '🏦 Link UPI' se")
    elif balance < 50:
        await update.message.reply_text(f"💸 Minimum ₹50 chahiye\nYour Balance: ₹{balance:.2f}")
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE user_balance SET balance = 0.0 WHERE user_id = ?", (user_id,))
            await db.commit()
        await update.message.reply_text(f"💸 Withdrawal Request Submit!\nAmount: ₹{balance:.2f}\nUPI: {upi_id}\n\n24 hours mein milega!")


async def handle_upi_link(update, user_id, upi_id):
    if "@" not in upi_id or len(upi_id) < 5:
        await update.message.reply_text("❌ Invalid UPI. Format: name@upi")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE user_balance SET upi_id = ? WHERE user_id = ?", (upi_id, user_id))
        await db.commit()
    await update.message.reply_text(f"✅ UPI Linked: {upi_id}")


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


# ===================== ADMIN PANEL =====================

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
.container { max-width: 800px; margin: 0 auto; padding: 20px; }
.stats { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; margin-bottom: 24px; }
.stat-card { background: #1e293b; border-radius: 12px; padding: 16px; border: 1px solid #334155; text-align: center; }
.stat-number { font-size: 28px; font-weight: 800; color: #38bdf8; }
.stat-label { font-size: 12px; color: #94a3b8; margin-top: 4px; }
.section { background: #1e293b; border-radius: 12px; padding: 20px; margin-bottom: 16px; border: 1px solid #334155; }
.section h2 { font-size: 16px; font-weight: 700; color: #f1f5f9; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }
.input-row { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
input, select { background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 10px 14px; color: #e2e8f0; font-size: 14px; flex: 1; min-width: 140px; outline: none; }
input:focus { border-color: #38bdf8; }
.btn { padding: 10px 18px; border-radius: 8px; border: none; font-size: 14px; font-weight: 700; cursor: pointer; transition: opacity 0.2s; white-space: nowrap; }
.btn:active { opacity: 0.8; }
.btn-blue { background: #38bdf8; color: #0f172a; }
.btn-green { background: #22c55e; color: #0f172a; }
.btn-red { background: #ef4444; color: white; }
.btn-yellow { background: #f59e0b; color: #0f172a; }
.channel-list { display: flex; flex-direction: column; gap: 8px; }
.channel-item { background: #0f172a; border-radius: 8px; padding: 12px 14px; display: flex; align-items: center; justify-content: space-between; border: 1px solid #334155; }
.channel-name { font-weight: 600; font-size: 14px; }
.channel-user { font-size: 12px; color: #94a3b8; }
.user-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.user-table th { text-align: left; padding: 8px 10px; color: #94a3b8; border-bottom: 1px solid #334155; font-weight: 600; }
.user-table td { padding: 8px 10px; border-bottom: 1px solid #1e293b; }
.verified-badge { background: #22c55e22; color: #22c55e; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.unverified-badge { background: #ef444422; color: #ef4444; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.msg { padding: 10px 14px; border-radius: 8px; font-size: 13px; margin-top: 10px; display: none; }
.msg-success { background: #22c55e22; color: #22c55e; border: 1px solid #22c55e44; }
.msg-error { background: #ef444422; color: #ef4444; border: 1px solid #ef444444; }
.tab-bar { display: flex; gap: 8px; margin-bottom: 20px; flex-wrap: wrap; }
.tab { padding: 8px 16px; border-radius: 8px; border: 1px solid #334155; background: #1e293b; color: #94a3b8; font-size: 13px; font-weight: 600; cursor: pointer; }
.tab.active { background: #38bdf8; color: #0f172a; border-color: #38bdf8; }
.tab-content { display: none; }
.tab-content.active { display: block; }
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>🛡️ Admin Panel</h1>
  </div>
  <span class="badge">ADMIN</span>
</div>

<div class="container">
  <!-- Stats -->
  <div class="stats">
    <div class="stat-card"><div class="stat-number" id="totalUsers">-</div><div class="stat-label">Total Users</div></div>
    <div class="stat-card"><div class="stat-number" id="verifiedUsers">-</div><div class="stat-label">Verified Users</div></div>
    <div class="stat-card"><div class="stat-number" id="totalChannels">-</div><div class="stat-label">Active Channels</div></div>
    <div class="stat-card"><div class="stat-number" id="totalBalance">-</div><div class="stat-label">Total Balance ₹</div></div>
  </div>

  <!-- Tabs -->
  <div class="tab-bar">
    <div class="tab active" onclick="switchTab('channels')">📢 Channels</div>
    <div class="tab" onclick="switchTab('users')">👥 Users</div>
    <div class="tab" onclick="switchTab('bonus')">🎁 Bonus</div>
    <div class="tab" onclick="switchTab('broadcast')">📣 Broadcast</div>
  </div>

  <!-- Channels Tab -->
  <div class="tab-content active" id="tab-channels">
    <div class="section">
      <h2>📢 Channel Add Karo</h2>
      <div class="input-row">
        <input id="chName" placeholder="Channel Name (e.g. My Channel)" />
        <input id="chUsername" placeholder="Username (e.g. mychannel)" />
      </div>
      <div class="input-row">
        <input id="chLink" placeholder="Link (e.g. https://t.me/mychannel)" />
        <button class="btn btn-green" onclick="addChannel()">➕ Add</button>
      </div>
      <div class="msg" id="chMsg"></div>
    </div>
    <div class="section">
      <h2>📋 Active Channels</h2>
      <div class="channel-list" id="channelList">Loading...</div>
    </div>
  </div>

  <!-- Users Tab -->
  <div class="tab-content" id="tab-users">
    <div class="section">
      <h2>🔍 User Search</h2>
      <div class="input-row">
        <input id="searchUserId" placeholder="User ID dalo" type="number" />
        <button class="btn btn-blue" onclick="searchUser()">🔍 Search</button>
      </div>
      <div id="userDetail"></div>
    </div>
    <div class="section">
      <h2>👥 All Users</h2>
      <table class="user-table">
        <thead><tr><th>ID</th><th>Name</th><th>Balance</th><th>Status</th><th>Action</th></tr></thead>
        <tbody id="userTableBody">Loading...</tbody>
      </table>
    </div>
  </div>

  <!-- Bonus Tab -->
  <div class="tab-content" id="tab-bonus">
    <div class="section">
      <h2>🎁 User ko Bonus Do</h2>
      <div class="input-row">
        <input id="bonusUserId" placeholder="User ID" type="number" />
        <input id="bonusAmount" placeholder="Amount (₹)" type="number" />
        <button class="btn btn-green" onclick="addBonus()">➕ Add Bonus</button>
      </div>
      <div class="msg" id="bonusMsg"></div>
    </div>
    <div class="section">
      <h2>🗑️ User ki Balance Reset Karo</h2>
      <div class="input-row">
        <input id="resetUserId" placeholder="User ID" type="number" />
        <button class="btn btn-red" onclick="resetBalance()">🗑️ Reset</button>
      </div>
      <div class="msg" id="resetMsg"></div>
    </div>
    <div class="section">
      <h2>✅ User Unverify Karo</h2>
      <div class="input-row">
        <input id="unverifyUserId" placeholder="User ID" type="number" />
        <button class="btn btn-yellow" onclick="unverifyUser()">🔓 Unverify</button>
      </div>
      <div class="msg" id="unverifyMsg"></div>
    </div>
  </div>

  <!-- Broadcast Tab -->
  <div class="tab-content" id="tab-broadcast">
    <div class="section">
      <h2>📣 Sabko Message Bhejo</h2>
      <textarea id="broadcastMsg" placeholder="Message likho..." style="width:100%;background:#0f172a;border:1px solid #334155;border-radius:8px;padding:12px;color:#e2e8f0;font-size:14px;min-height:100px;resize:vertical;outline:none;margin-bottom:12px;"></textarea>
      <button class="btn btn-blue" onclick="sendBroadcast()">📣 Send to All</button>
      <div class="msg" id="broadcastResult"></div>
    </div>
  </div>
</div>

<script>
const API = '';

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');
}

function showMsg(id, text, ok) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'msg ' + (ok ? 'msg-success' : 'msg-error');
  el.style.display = 'block';
  setTimeout(() => el.style.display = 'none', 3000);
}

async function loadStats() {
  const r = await fetch('/admin/api/stats');
  const d = await r.json();
  document.getElementById('totalUsers').textContent = d.total_users;
  document.getElementById('verifiedUsers').textContent = d.verified_users;
  document.getElementById('totalChannels').textContent = d.total_channels;
  document.getElementById('totalBalance').textContent = '₹' + d.total_balance.toFixed(0);
}

async function loadChannels() {
  const r = await fetch('/admin/api/channels');
  const d = await r.json();
  const list = document.getElementById('channelList');
  if (!d.channels.length) { list.innerHTML = '<div style="color:#94a3b8;font-size:13px;">Koi channel nahi add kiya</div>'; return; }
  list.innerHTML = d.channels.map(ch => `
    <div class="channel-item">
      <div>
        <div class="channel-name">${ch.channel_name || ch.channel_username}</div>
        <div class="channel-user">@${ch.channel_username}</div>
      </div>
      <button class="btn btn-red" onclick="removeChannel(${ch.id})" style="padding:6px 12px;font-size:12px;">🗑️ Remove</button>
    </div>
  `).join('');
}

async function addChannel() {
  const name = document.getElementById('chName').value.trim();
  const username = document.getElementById('chUsername').value.trim().replace('@','');
  const link = document.getElementById('chLink').value.trim();
  if (!username || !link) { showMsg('chMsg', '❌ Username aur Link zaroori hai!', false); return; }
  const r = await fetch('/admin/api/channels/add', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({channel_username: username, channel_link: link, channel_name: name})
  });
  const d = await r.json();
  if (d.success) { showMsg('chMsg', '✅ Channel add ho gaya!', true); loadChannels(); loadStats(); document.getElementById('chName').value=''; document.getElementById('chUsername').value=''; document.getElementById('chLink').value=''; }
  else showMsg('chMsg', '❌ ' + (d.error || 'Error'), false);
}

async function removeChannel(id) {
  if (!confirm('Channel remove karna hai?')) return;
  const r = await fetch('/admin/api/channels/remove', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({channel_id: id}) });
  const d = await r.json();
  if (d.success) { loadChannels(); loadStats(); }
}

async function loadUsers() {
  const r = await fetch('/admin/api/users');
  const d = await r.json();
  const tbody = document.getElementById('userTableBody');
  if (!d.users.length) { tbody.innerHTML = '<tr><td colspan="5" style="color:#94a3b8;">Koi user nahi</td></tr>'; return; }
  tbody.innerHTML = d.users.map(u => `
    <tr>
      <td>${u.user_id}</td>
      <td>${u.first_name || '-'}</td>
      <td>₹${(u.balance||0).toFixed(2)}</td>
      <td><span class="${u.is_verified ? 'verified-badge' : 'unverified-badge'}">${u.is_verified ? 'Verified' : 'Unverified'}</span></td>
      <td><button class="btn btn-yellow" onclick="document.getElementById('bonusUserId').value=${u.user_id};switchTabDirect('bonus')" style="padding:4px 8px;font-size:11px;">+Bonus</button></td>
    </tr>
  `).join('');
}

function switchTabDirect(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelector(`.tab[onclick="switchTab('${name}')"]`).classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');
}

async function searchUser() {
  const uid = document.getElementById('searchUserId').value;
  if (!uid) return;
  const r = await fetch('/admin/api/user/' + uid);
  const d = await r.json();
  const el = document.getElementById('userDetail');
  if (d.error) { el.innerHTML = '<div style="color:#ef4444;margin-top:10px;">User nahi mila</div>'; return; }
  el.innerHTML = `<div style="background:#0f172a;border-radius:8px;padding:14px;margin-top:12px;border:1px solid #334155;">
    <div><b>ID:</b> ${d.user_id}</div>
    <div><b>Name:</b> ${d.first_name || '-'}</div>
    <div><b>Username:</b> @${d.username || '-'}</div>
    <div><b>Balance:</b> ₹${(d.balance||0).toFixed(2)}</div>
    <div><b>Status:</b> ${d.is_verified ? '✅ Verified' : '❌ Unverified'}</div>
    <div><b>UPI:</b> ${d.upi_id || 'Not linked'}</div>
  </div>`;
}

async function addBonus() {
  const uid = document.getElementById('bonusUserId').value;
  const amount = document.getElementById('bonusAmount').value;
  if (!uid || !amount) { showMsg('bonusMsg', '❌ ID aur Amount dono chahiye!', false); return; }
  const r = await fetch('/admin/api/add-bonus', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({user_id: parseInt(uid), amount: parseFloat(amount)}) });
  const d = await r.json();
  showMsg('bonusMsg', d.success ? `✅ ₹${amount} add ho gaya!` : '❌ ' + (d.error||'Error'), d.success);
  if (d.success) loadUsers();
}

async function resetBalance() {
  const uid = document.getElementById('resetUserId').value;
  if (!uid) { showMsg('resetMsg', '❌ User ID dalo!', false); return; }
  if (!confirm('Balance reset karna hai?')) return;
  const r = await fetch('/admin/api/reset-balance', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({user_id: parseInt(uid)}) });
  const d = await r.json();
  showMsg('resetMsg', d.success ? '✅ Balance reset ho gaya!' : '❌ Error', d.success);
  if (d.success) loadUsers();
}

async function unverifyUser() {
  const uid = document.getElementById('unverifyUserId').value;
  if (!uid) { showMsg('unverifyMsg', '❌ User ID dalo!', false); return; }
  if (!confirm('User unverify karna hai?')) return;
  const r = await fetch('/admin/api/unverify-user', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({user_id: parseInt(uid)}) });
  const d = await r.json();
  showMsg('unverifyMsg', d.success ? '✅ User unverify ho gaya!' : '❌ Error', d.success);
  if (d.success) loadUsers();
}

async function sendBroadcast() {
  const msg = document.getElementById('broadcastMsg').value.trim();
  if (!msg) { showMsg('broadcastResult', '❌ Message likho pehle!', false); return; }
  if (!confirm('Sabko message bhejna hai?')) return;
  showMsg('broadcastResult', '📣 Bhej raha hoon...', true);
  const r = await fetch('/admin/api/broadcast', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({message: msg}) });
  const d = await r.json();
  showMsg('broadcastResult', d.success ? `✅ ${d.sent} users ko bheja!` : '❌ Error', d.success);
}

// Load on start
loadStats();
loadChannels();
loadUsers();
setInterval(loadStats, 30000);
</script>
</body>
</html>"""


def is_admin(request: Request) -> bool:
    # Simple admin check via header ya query param
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
            "SELECT u.user_id, u.first_name, u.username, u.is_verified, COALESCE(b.balance,0) FROM users u LEFT JOIN user_balance b ON u.user_id=b.user_id ORDER BY u.created_at DESC LIMIT 100"
        )).fetchall()
    return {"users": [{"user_id": r[0], "first_name": r[1], "username": r[2], "is_verified": r[3], "balance": r[4]} for r in rows]}


@app.get("/admin/api/user/{user_id}")
async def admin_get_user(user_id: int, request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403)
    async with aiosqlite.connect(DB_PATH) as db:
        u = await (await db.execute("SELECT user_id, first_name, username, is_verified FROM users WHERE user_id=?", (user_id,))).fetchone()
        b = await (await db.execute("SELECT balance, upi_id FROM user_balance WHERE user_id=?", (user_id,))).fetchone()
    if not u:
        return {"error": "Not found"}
    return {"user_id": u[0], "first_name": u[1], "username": u[2], "is_verified": u[3], "balance": b[0] if b else 0, "upi_id": b[1] if b else None}


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
        await db.commit()
    return {"status": "verified", "user": {"id": user_id, "first_name": user_data.get("first_name")}}


@app.get("/bot/healthz")
async def health():
    return {"status": "ok"}


async def run_bot():
    global bot_app_global
    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    bot_app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, button_handler))
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(drop_pending_updates=True)
    bot_app_global = bot_app
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
