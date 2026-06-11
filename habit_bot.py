"""
Micro-Habit Telegram Bot  (Render free-tier version)
-----------------------------------------------------
Same bot as before, plus a tiny web server so Render's FREE web-service
tier will run it. An external pinger (UptimeRobot / cron-job.org) hits
the web server every few minutes to keep the service awake.

Setup on Render: set the BOT_TOKEN environment variable (do NOT hardcode
your token when the code lives in a public GitHub repo).
"""

import os
import json
import random
import threading
import datetime as dt
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ---------------------------------------------------------------------------
# CONFIG  --  edit these
# ---------------------------------------------------------------------------

# On Render you set this in the dashboard (Environment tab), NOT here.
TOKEN = os.environ.get("BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")

TIMEZONE = "Asia/Taipei"
NUDGE_TIMES = ["10:00", "11:00", "12:00", "14:00", "15:00", "16:00", "17:00", "18:00"]

HABITS = [
    "\U0001F4A7 Drink a glass of water.",
    "\U0001F9CD Stand up and stretch for 30 seconds.",
    "\U0001F32C\uFE0F Take 10 slow, deep breaths.",
    "\U0001F440 Look at something 20 feet away for 20 seconds (rest your eyes).",
    "\U0001F6B6 Walk around for 2 minutes.",
    "\U0001F646 Roll your shoulders back 5 times."
]

DATA_FILE = "habit_data.json"

# ---------------------------------------------------------------------------
# Keep-alive web server (so Render's free web-service tier runs the bot)
# ---------------------------------------------------------------------------

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Habit bot is alive!")
        
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        
    def log_message(self, *args):  # silence noisy request logs
        pass

def start_web_server():
    # Render provides the port to bind to via the PORT env var.
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    server.serve_forever()

# ---------------------------------------------------------------------------
# Tiny JSON "database"
# ---------------------------------------------------------------------------

def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_data(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def get_user(data: dict, chat_id: int) -> dict:
    key = str(chat_id)
    if key not in data:
        data[key] = {"streak": 0, "best": 0, "total": 0, "last_done": None}
    return data[key]

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    data = load_data()
    get_user(data, chat_id)
    save_data(data)

    for job in context.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()

    tz = ZoneInfo(TIMEZONE)
    for t in NUDGE_TIMES:
        hour, minute = map(int, t.split(":"))
        context.job_queue.run_daily(
            send_nudge,
            time=dt.time(hour=hour, minute=minute, tzinfo=tz),
            chat_id=chat_id,
            name=str(chat_id),
        )

    times = ", ".join(NUDGE_TIMES)
    await update.message.reply_text(
        "\U0001F44B You're set up!\n\n"
        f"I'll nudge you with one small healthy action at: {times} "
        f"({TIMEZONE}).\n\n"
        "Tap \u2705 Done when you complete one to build your streak.\n\n"
        "Commands:\n"
        "/nudge \u2013 get one right now\n"
        "/stats \u2013 see your streak\n"
        "/stop \u2013 pause the daily nudges"
    )

async def nudge_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _deliver_nudge(context, update.effective_chat.id)

async def send_nudge(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _deliver_nudge(context, context.job.chat_id)

async def _deliver_nudge(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    habit = random.choice(HABITS)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("\u2705 Done", callback_data="done")]]
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"Time for a micro-habit:\n\n{habit}",
        reply_markup=keyboard,
    )

async def on_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    data = load_data()
    user = get_user(data, chat_id)

    today = dt.date.today().isoformat()
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()

    if user["last_done"] == today:
        user["total"] += 1
        msg = f"Nice, another one done today! \U0001F4AA\nStreak: {user['streak']} days."
    else:
        if user["last_done"] == yesterday:
            user["streak"] += 1
        else:
            user["streak"] = 1
        user["best"] = max(user["best"], user["streak"])
        user["total"] += 1
        user["last_done"] = today
        msg = f"Done! \U0001F389\n\U0001F525 Streak: {user['streak']} day(s)"
        if user["streak"] == user["best"] and user["best"] > 1:
            msg += "  (new best!)"

    save_data(data)
    await query.edit_message_text(text=f"{query.message.text}\n\n{msg}")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_user(data, update.effective_chat.id)
    await update.message.reply_text(
        f"\U0001F4CA Your progress\n\n"
        f"\U0001F525 Current streak: {user['streak']} day(s)\n"
        f"\U0001F3C6 Best streak: {user['best']} day(s)\n"
        f"\u2705 Total actions: {user['total']}"
    )

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    for job in context.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    await update.message.reply_text(
        "\u23F8\uFE0F Daily nudges paused. Send /start to turn them back on."
    )

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if TOKEN == "PASTE_YOUR_TOKEN_HERE":
        raise SystemExit(
            "Set your bot token first via the BOT_TOKEN environment variable."
        )

    # Start the keep-alive web server in a background thread.
    threading.Thread(target=start_web_server, daemon=True).start()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("nudge", nudge_now))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CallbackQueryHandler(on_done, pattern="^done$"))

    print("Bot is running. Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
