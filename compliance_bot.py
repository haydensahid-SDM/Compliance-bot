"""
Compliance Update Telegram Bot
================================
This bot does two things:
1. Monitors a compliance group chat and indexes all client update messages
2. Lets executives query the latest update for any client via /update [Client Name]

Setup Instructions:
-------------------
1. Create a bot via @BotFather on Telegram → get your BOT_TOKEN
2. Add the bot to your compliance group chat (and give it admin/read access)
3. Set COMPLIANCE_CHAT_ID to your compliance group's chat ID
   (Send a message in the group, then visit:
    https://api.telegram.org/bot<BOT_TOKEN>/getUpdates to find the chat ID)
4. Install dependencies:  pip install python-telegram-bot==20.7 aiosqlite
5. Set environment variables or edit the CONFIG section below
6. Run:  python compliance_bot.py
"""

import os
import re
import logging
import asyncio
import sqlite3
from datetime import datetime
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ─────────────────────────────────────────────
#  CONFIG  ← Edit these or set as env vars
# ─────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# The numeric ID of your compliance group chat (negative number, e.g. -1001234567890)
COMPLIANCE_CHAT_ID = int(os.getenv("COMPLIANCE_CHAT_ID", "0"))

# SQLite DB path (stores all parsed client updates)
DB_PATH = "compliance_updates.db"

# ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════

def init_db():
    """Create the SQLite table if it doesn't exist."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS client_updates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            client_name TEXT    NOT NULL,
            raw_message TEXT    NOT NULL,
            message_date TEXT   NOT NULL,
            message_id  INTEGER,
            chat_id     INTEGER,
            created_at  TEXT    DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def save_update(client_name: str, raw_message: str, message_date: str,
                message_id: int, chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO client_updates (client_name, raw_message, message_date, message_id, chat_id)
        VALUES (?, ?, ?, ?, ?)
    """, (client_name, raw_message, message_date, message_id, chat_id))
    conn.commit()
    conn.close()


def get_latest_update(client_name: str):
    """
    Fuzzy search: returns the most recent update whose client_name
    contains the search string (case-insensitive).
    """
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
    return row  # (client_name, raw_message, message_date) or None


def list_all_clients():
    """Return a deduplicated list of client names in the DB."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT DISTINCT client_name FROM client_updates ORDER BY client_name
    """)
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


# ══════════════════════════════════════════════
#  MESSAGE PARSER
# ══════════════════════════════════════════════

# Matches: "Remaining Items for <Client Name>:" at the start of a line
CLIENT_HEADER_RE = re.compile(
    r"Remaining Items for (.+?):",
    re.IGNORECASE
)


def parse_client_update(text: str):
    """
    Returns the client name if the message looks like a compliance update,
    otherwise returns None.
    """
    match = CLIENT_HEADER_RE.search(text)
    if match:
        return match.group(1).strip()
    return None


# ══════════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════════

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Runs in the compliance group chat.
    Detects and indexes client update messages automatically.
    """
    msg = update.message
    if not msg or not msg.text:
        return

    # Only index messages from the designated compliance chat
    if msg.chat_id != COMPLIANCE_CHAT_ID:
        return

    client_name = parse_client_update(msg.text)
    if client_name:
        message_date = msg.date.strftime("%Y-%m-%d %H:%M:%S UTC")
        save_update(
            client_name=client_name,
            raw_message=msg.text,
            message_date=message_date,
            message_id=msg.message_id,
            chat_id=msg.chat_id,
        )
        logger.info(f"Indexed update for client: {client_name}")


async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /update [Client Name]
    Can be used in any private chat or group where the bot is present.
    Returns the most recent compliance update for the named client.
    """
    if not context.args:
        await update.message.reply_text(
            "ℹ️ Usage: `/update [Client Name]`\n"
            "Example: `/update Xit Strategy World LLC`",
            parse_mode="Markdown"
        )
        return

    query = " ".join(context.args).strip()
    row = get_latest_update(query)

    if not row:
        # Show available clients to help the exec
        clients = list_all_clients()
        if clients:
            client_list = "\n".join(f"• {c}" for c in clients[:20])
            await update.message.reply_text(
                f"❌ No updates found for *{query}*.\n\n"
                f"Known clients:\n{client_list}",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"❌ No updates found for *{query}*. "
                "The compliance chat may not have posted any updates yet.",
                parse_mode="Markdown"
            )
        return

    client_name, raw_message, message_date = row
    response = (
        f"📋 *Latest update for {client_name}*\n"
        f"🕐 *Received:* {message_date}\n"
        f"{'─' * 30}\n"
        f"{raw_message}"
    )
    await update.message.reply_text(response, parse_mode="Markdown")


async def cmd_clients(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /clients — List all clients that have updates indexed.
    """
    clients = list_all_clients()
    if not clients:
        await update.message.reply_text("No client updates indexed yet.")
        return

    client_list = "\n".join(f"• {c}" for c in clients)
    await update.message.reply_text(
        f"📁 *Clients with updates ({len(clients)} total):*\n{client_list}",
        parse_mode="Markdown"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Compliance Update Bot*\n\n"
        "*Commands:*\n"
        "`/update [Client Name]` — Get latest update for a client\n"
        "`/clients` — List all clients with indexed updates\n"
        "`/help` — Show this message\n\n"
        "_Tip: Partial names work too — `/update Xit Strategy` will match "
        "\"Xit Strategy World LLC\"_",
        parse_mode="Markdown"
    )


# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("ERROR: Please set your BOT_TOKEN in the script or as an environment variable.")
        return
    if COMPLIANCE_CHAT_ID == 0:
        print("ERROR: Please set your COMPLIANCE_CHAT_ID in the script or as an environment variable.")
        return

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands (work in any chat)
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CommandHandler("clients", cmd_clients))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))

    # Passive listener — indexes messages posted in the compliance group
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_group_message
        )
    )

    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
