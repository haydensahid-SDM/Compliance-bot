"""
Compliance Update Bot — Telethon Version
=========================================
Uses a Telegram USER account (via Telethon) to:
1. Read full chat history on startup and index all past client updates
2. Listen for new messages and index them in real time
3. Respond to /update [Client Name] commands from executives

Extra setup vs the bot version:
- Requires API_ID and API_HASH from https://my.telegram.org
- Runs as a user account, not a bot account
- On first run, will ask for your phone number + verification code (one time only)

Environment Variables Required:
- API_ID         → from https://my.telegram.org (a number like 12345678)
- API_HASH       → from https://my.telegram.org (a string like abc123def456...)
- BOT_TOKEN      → from @BotFather (for the bot that responds to execs)
- COMPLIANCE_CHAT_ID → the group chat ID (negative number)
- EXEC_CHAT_IDS  → comma-separated Telegram user IDs of execs allowed to query
                   (optional — if empty, anyone who messages the bot can query)
"""

import os
import re
import logging
import asyncio
import sqlite3
from datetime import datetime

from telethon import TelegramClient, events
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
API_ID             = int(os.getenv("API_ID", "0"))
API_HASH           = os.getenv("API_HASH", "")
BOT_TOKEN          = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
COMPLIANCE_CHAT_ID = int(os.getenv("COMPLIANCE_CHAT_ID", "0"))
SESSION_NAME       = "compliance_session"
DB_PATH            = "compliance_updates.db"
HISTORY_LIMIT      = 500

EXEC_IDS_RAW     = os.getenv("EXEC_CHAT_IDS", "")
ALLOWED_EXEC_IDS = set(int(x.strip()) for x in EXEC_IDS_RAW.split(",") if x.strip())

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS client_updates (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            client_name  TEXT    NOT NULL,
            raw_message  TEXT    NOT NULL,
            message_date TEXT    NOT NULL,
            message_id   INTEGER UNIQUE,
            created_at   TEXT    DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def save_update(client_name, raw_message, message_date, message_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR IGNORE INTO client_updates (client_name, raw_message, message_date, message_id)
        VALUES (?, ?, ?, ?)
    """, (client_name, raw_message, message_date, message_id))
    conn.commit()
    conn.close()


def get_latest_update(client_name):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT client_name, raw_message, message_date
        FROM client_updates
        WHERE LOWER(client_name) LIKE LOWER(?)
        ORDER BY message_date DESC
        LIMIT 1
    """, (f"%{client_name}%",))
    row = cursor.fetchone()
    conn.close()
    return row


def list_all_clients():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("SELECT DISTINCT client_name FROM client_updates ORDER BY client_name")
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


# ══════════════════════════════════════════════
#  MESSAGE PARSER
# ══════════════════════════════════════════════

CLIENT_HEADER_RE = re.compile(r"Remaining Items for (.+?):", re.IGNORECASE)

def parse_client_update(text):
    match = CLIENT_HEADER_RE.search(text)
    return match.group(1).strip() if match else None


# ══════════════════════════════════════════════
#  TELETHON — reads history + listens live
# ══════════════════════════════════════════════

async def index_message(msg):
    if not msg.text:
        return
    client_name = parse_client_update(msg.text)
    if client_name:
        message_date = msg.date.strftime("%Y-%m-%d %H:%M:%S UTC")
        save_update(client_name, msg.text, message_date, msg.id)
        logger.info(f"Indexed: {client_name} (msg_id={msg.id})")


async def run_telethon():
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()
    logger.info("Telethon connected. Loading history...")

    count = 0
    async for msg in client.iter_messages(COMPLIANCE_CHAT_ID, limit=HISTORY_LIMIT):
        await index_message(msg)
        count += 1
    logger.info(f"History loaded: {count} messages scanned.")

    @client.on(events.NewMessage(chats=COMPLIANCE_CHAT_ID))
    async def on_new(event):
        await index_message(event.message)

    @client.on(events.MessageEdited(chats=COMPLIANCE_CHAT_ID))
    async def on_edit(event):
        await index_message(event.message)

    await client.run_until_disconnected()


# ══════════════════════════════════════════════
#  BOT — responds to exec queries
# ══════════════════════════════════════════════

async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_EXEC_IDS and user_id not in ALLOWED_EXEC_IDS:
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: `/update [Client Name]`\nExample: `/update Xit Strategy World LLC`",
            parse_mode="Markdown"
        )
        return

    query = " ".join(context.args).strip()
    row = get_latest_update(query)

    if not row:
        clients = list_all_clients()
        if clients:
            client_list = "\n".join(f"• {c}" for c in clients[:20])
            await update.message.reply_text(
                f"No updates found for *{query}*.\n\nKnown clients:\n{client_list}",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"No updates found for *{query}*.", parse_mode="Markdown")
        return

    client_name, raw_message, message_date = row
    await update.message.reply_text(
        f"📋 *Latest update for {client_name}*\n"
        f"🕐 *Posted:* {message_date}\n"
        f"{'─' * 30}\n{raw_message}",
        parse_mode="Markdown"
    )


async def cmd_clients(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clients = list_all_clients()
    if not clients:
        await update.message.reply_text("No client updates indexed yet.")
        return
    await update.message.reply_text(
        f"📁 *Clients ({len(clients)} total):*\n" + "\n".join(f"• {c}" for c in clients),
        parse_mode="Markdown"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Compliance Update Bot*\n\n"
        "`/update [Client Name]` — Get latest update\n"
        "`/clients` — List all clients\n"
        "`/help` — Show this message\n\n"
        "_Partial names work: `/update Xit Strategy` matches \"Xit Strategy World LLC\"_",
        parse_mode="Markdown"
    )


async def run_bot():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CommandHandler("clients", cmd_clients))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Bot polling...")
    await asyncio.Event().wait()


# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════

async def main():
    init_db()
    await asyncio.gather(run_telethon(), run_bot())


if __name__ == "__main__":
    asyncio.run(main())
