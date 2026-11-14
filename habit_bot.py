"""Telegram micro-habit bot + a 365-day A1–A2 Japanese/Italian audio course.

Render web service: BOT_TOKEN must be set as an environment variable.
Daily lessons, habits and /review do not require paid content APIs.
MP3 pronunciation is generated on demand by edge-tts (online, unofficial service).
"""

import asyncio
import datetime as dt
import json
import logging
import os
import random
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import edge_tts
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN", "")
TIMEZONE = os.environ.get("HABIT_TIMEZONE", "Asia/Taipei")
TZ = ZoneInfo(TIMEZONE)
HABIT_TIMES = ["09:00", "13:00", "16:00", "20:00"]
LANGUAGE_TIME = os.environ.get("LANGUAGE_TIME", "08:30")
DATA_FILE = Path(os.environ.get("HABIT_DATA_FILE", "habit_data.json"))
COURSE_FILE = Path(__file__).with_name("language_course_365.json")
AUTO_AUDIO_DEFAULT = os.environ.get("AUTO_AUDIO_DEFAULT", "true").lower() in ("1", "true", "yes", "on")
TTS_VOICES = {
    "ja": os.environ.get("TTS_VOICE_JA", "ja-JP-NanamiNeural"),
    "it": os.environ.get("TTS_VOICE_IT", "it-IT-IsabellaNeural"),
}
TTS_RATE = os.environ.get("TTS_RATE", "-15%")  # slower for beginners
AUDIO_CACHE_LIMIT = 24  # pairs from recent lessons; process memory only
AUDIO_CACHE = OrderedDict()

HABITS = [
    "💧 Drink a glass of water.",
    "🧍 Stand up and stretch for 30 seconds.",
    "🌬️ Take 10 slow, deep breaths.",
    "👀 Look at something 20 feet away for 20 seconds.",
    "🚶 Walk around for 2 minutes.",
    "🙆 Roll your shoulders back 5 times.",
]


class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Habit + language bot is alive")

    def log_message(self, *args):
        pass


def start_web_server():
    port = int(os.environ.get("PORT", "8080"))
    HTTPServer(("0.0.0.0", port), PingHandler).serve_forever()


def local_today():
    return dt.datetime.now(TZ).date()


def load_data():
    if not DATA_FILE.exists():
        return {}
    with DATA_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def save_data(data):
    # One process only. Atomic replacement prevents truncated JSON on normal restarts.
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(temp, DATA_FILE)


def get_user(data, chat_id):
    key = str(chat_id)
    if key not in data:
        data[key] = {}
    user = data[key]
    for name, default in (("streak", 0), ("best", 0), ("total", 0),
                          ("last_done", None), ("subscribed", False),
                          ("auto_audio", AUTO_AUDIO_DEFAULT),
                          ("course_start", None),
                          ("completed_lessons", []), ("studied_dates", []),
                          ("last_language_sent", None)):
        user.setdefault(name, default)
    return user


def read_course():
    data = json.loads(COURSE_FILE.read_text(encoding="utf-8"))
    lessons = data["lessons"]
    if len(lessons) != 365 or [a["day"] for a in lessons] != list(range(1, 366)):
        raise ValueError("Course must contain exactly days 1–365 in order")
    return lessons


LESSONS = read_course()


def course_day(user, day=None):
    """One new lesson each calendar day after /start, even across restarts.

    After 365 days the course cycles back to day 1; no new content beyond 365.
    """
    today = day or local_today()
    start_text = user.get("course_start")
    if not start_text:
        return 1
    start = dt.date.fromisoformat(start_text)
    elapsed = max(0, (today - start).days)
    return (elapsed % 365) + 1


def lesson(day):
    if day < 1 or day > 365:
        raise ValueError("Lesson number must be between 1 and 365")
    return LESSONS[day - 1]


def review_days(day):
    # Spaced retrieval: yesterday, a week ago, and a month ago.
    return [day - n for n in (1, 7, 30) if day > n]


def lesson_buttons(day):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔊 Japanese", callback_data=f"play:ja:{day}"),
         InlineKeyboardButton("🔊 Italian", callback_data=f"play:it:{day}")],
        [InlineKeyboardButton("✅ I practised", callback_data=f"studied:{day}"),
         InlineKeyboardButton("🧠 Review", callback_data=f"review:{day}")],
        [InlineKeyboardButton("💬 Mini-dialogue", callback_data=f"dialogue:{day}")],
    ])


def format_lesson(day, translations=True):
    item = lesson(day)
    ja, it = item["japanese"], item["italian"]
    lines = [
        f"📖 Language lesson · Day {day}/365 · {item['level']}",
        f"Topic: {item['topic']}",
        f"Goal: {item['goal']}", "",
        "🇯🇵 JAPANESE", ja["sentence"],
        f"Reading: {ja['reading']}",
    ]
    if translations:
        lines.extend((f"English: {ja['english']}", f"Tip: {ja['vocab']}"))
    lines.extend(("", "🇮🇹 ITALIAN", it["sentence"]))
    if translations:
        lines.extend((f"English: {it['english']}", f"Tip: {it['vocab']}"))
    lines.extend(("", f"🎯 Try it: {item['challenge']}",
                  "Listen, repeat aloud, and use /review for spaced revision."))
    if day > 1:
        # Keep review genuinely daily: one short prompt in the scheduled message,
        # with full 1/7/30-day revision accessible through /review.
        offsets = [x for x in (30, 7, 1) if day > x]
        offset = offsets[(day - 1) % len(offsets)]
        old = lesson(day - offset)
        lines.extend(("", f"🧠 Quick recall · Day {day - offset} ({offset} day(s) ago)",
                      f"JP: {old['japanese']['sentence']}",
                      f"IT: {old['italian']['sentence']}",
                      "Can you remember the meaning? Use /review to check."))
    return "\n".join(lines)


def format_review(day, translations=False):
    past = review_days(day)
    if not past:
        return "No earlier lessons yet. Practise today's two sentences aloud!"
    lines = [f"🧠 Spaced revision for day {day}",
             "Try to remember the meaning before revealing answers."]
    for back, prior in zip((1, 7, 30), past):
        l = lesson(prior)
        lines.extend(("", f"Day {prior} · {l['topic']}",
                      f"JP: {l['japanese']['sentence']}",
                      f"IT: {l['italian']['sentence']}"))
        if translations:
            lines.extend((f"JP answer: {l['japanese']['english']}",
                          f"IT answer: {l['italian']['english']}"))
    return "\n".join(lines)


def format_dialogue(day):
    item = lesson(day)
    pair = item['dialogue']
    return (f"💬 Conversation practice · Day {day} · {item['topic']}\n"
            "Read role A aloud, then answer as role B. Switch roles!\n\n"
            f"🇯🇵 JAPANESE\nA: {pair['ja'][0]}\nB: {pair['ja'][1]}\n\n"
            f"🇮🇹 ITALIAN\nA: {pair['it'][0]}\nB: {pair['it'][1]}\n\n"
            f"ENGLISH\nA: {pair['en'][0]}\nB: {pair['en'][1]}\n\n"
            "Try replacing one detail to make your own conversation.")


async def dialogue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(load_data(), update.effective_chat.id)
    day = course_day(user)
    await update.message.reply_text(format_dialogue(day))


def review_buttons(day):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Reveal English answers", callback_data=f"answers:{day}")],
        [InlineKeyboardButton("🔊 Today's Japanese", callback_data=f"play:ja:{day}"),
         InlineKeyboardButton("🔊 Today's Italian", callback_data=f"play:it:{day}")],
    ])


async def synthesize_audio(language, day):
    """Generate an MP3 on demand; avoid API keys and large committed media files.

    Edge TTS is a third-party online service, not a guaranteed production API.
    """
    if language not in TTS_VOICES:
        raise ValueError("Invalid language")
    key = (language, day, TTS_VOICES[language], TTS_RATE)
    if key in AUDIO_CACHE:
        AUDIO_CACHE.move_to_end(key)
        return AUDIO_CACHE[key]
    item = lesson(day)
    text = item["japanese" if language == "ja" else "italian"]["sentence"]
    speaker = edge_tts.Communicate(text, voice=TTS_VOICES[language], rate=TTS_RATE)
    chunks = bytearray()
    async def collect():
        async for packet in speaker.stream():
            if packet["type"] == "audio":
                chunks.extend(packet["data"])
                if len(chunks) > 8_000_000:
                    raise ValueError("Generated audio unexpectedly large")
    await asyncio.wait_for(collect(), timeout=30)
    if not chunks:
        raise RuntimeError("Text-to-speech provider returned no audio")
    result = bytes(chunks)
    AUDIO_CACHE[key] = result
    if len(AUDIO_CACHE) > AUDIO_CACHE_LIMIT:
        AUDIO_CACHE.popitem(last=False)
    return result


async def send_pronunciation(bot, chat_id, language, day):
    data = await synthesize_audio(language, day)
    name = "Japanese" if language == "ja" else "Italian"
    audio = InputFile(BytesIO(data), filename=f"day_{day:03d}_{language}.mp3")
    await bot.send_audio(chat_id=chat_id, audio=audio,
                         title=f"Day {day} — {name} pronunciation",
                         performer="Language Practice Bot",
                         caption=f"🔊 Day {day} · {name} · listen, pause, repeat")


def _time(text):
    hours, minutes = map(int, text.split(":"))
    return dt.time(hours, minutes, tzinfo=TZ)


def schedule_user(job_queue, chat_id):
    for prefix in ("habit:", "language:", str(chat_id)):
        for job in job_queue.get_jobs_by_name(f"{prefix}{chat_id}" if prefix != str(chat_id) else prefix):
            job.schedule_removal()
    for time_text in HABIT_TIMES:
        job_queue.run_daily(send_nudge, time=_time(time_text),
                            chat_id=chat_id, name=f"habit:{chat_id}")
    job_queue.run_daily(send_language, time=_time(LANGUAGE_TIME),
                        chat_id=chat_id, name=f"language:{chat_id}")


async def restore_jobs(application):
    """Restore PTB in-memory jobs from saved JSON after a process restart."""
    for chat_id, user in load_data().items():
        if user.get("subscribed", False):
            try:
                schedule_user(application.job_queue, int(chat_id))
            except (TypeError, ValueError):
                LOG.warning("Skipped invalid chat ID in storage")


async def send_lesson_to_chat(context, chat_id, day, auto_audio=False):
    await context.bot.send_message(chat_id=chat_id, text=format_lesson(day),
                                   reply_markup=lesson_buttons(day))
    if auto_audio:
        for language in ("ja", "it"):
            try:
                await send_pronunciation(context.bot, chat_id, language, day)
            except Exception:
                # Never let a TTS outage prevent the written lesson.
                LOG.exception("Automatic audio unavailable for %s, day %s", language, day)


async def send_language(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    data = load_data()
    user = get_user(data, chat_id)
    if not user["subscribed"]:
        return
    if not user["course_start"]:
        user["course_start"] = local_today().isoformat()
    today = local_today().isoformat()
    if user["last_language_sent"] == today:
        return
    day = course_day(user)
    await context.bot.send_message(chat_id=chat_id, text=format_lesson(day),
                                   reply_markup=lesson_buttons(day))
    # Mark the text as sent before generating audio, so TTS outages can't duplicate it.
    user["last_language_sent"] = today
    save_data(data)
    if user["auto_audio"]:
        for language in ("ja", "it"):
            try:
                await send_pronunciation(context.bot, chat_id, language, day)
            except Exception:
                LOG.exception("TTS failed for %s day %s", language, day)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    data = load_data()
    user = get_user(data, chat_id)
    user["subscribed"] = True
    if not user["course_start"]:
        user["course_start"] = local_today().isoformat()
    save_data(data)
    schedule_user(context.job_queue, chat_id)
    await update.message.reply_text(
        "👋 Your micro-habits and 365-day language course are active!\n\n"
        f"Habits: {', '.join(HABIT_TIMES)}\n"
        f"Japanese + Italian lesson: {LANGUAGE_TIME} ({TIMEZONE})\n"
        "You'll receive a new pair daily, from A1 to A2.\n"
        f"Current course day: {course_day(user)}/365\n"
        f"Automatic MP3 messages: {'ON' if user['auto_audio'] else 'OFF'}\n\n"
        "Commands:\n"
        "/today — today's two sentences and audio buttons\n"
        "/listen — send both pronunciation recordings\n"
        "/review — revise days 1, 7 and 30 back\n"
        "/dialogue — practise a two-person conversation\n"
        "/practice — hide English to test yourself\n"
        "/lesson 25 — browse any course day\n"
        "/progress — learning progress\n"
        "/audioauto on|off — automatic daily MP3s\n"
        "/nudge, /stats — habit reminders and streaks\n"
        "/stop — pause scheduled messages"
    )


async def today_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user = get_user(data, update.effective_chat.id)
    if not user["course_start"]:
        user["course_start"] = local_today().isoformat()
        save_data(data)
    day = course_day(user)
    await update.message.reply_text(format_lesson(day), reply_markup=lesson_buttons(day))


async def listen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user = get_user(data, update.effective_chat.id)
    if not user["course_start"]:
        user["course_start"] = local_today().isoformat()
        save_data(data)
    day = course_day(user)
    for language in ("ja", "it"):
        try:
            await send_pronunciation(context.bot, update.effective_chat.id, language, day)
        except Exception:
            LOG.exception("Manual listen failed")
            await update.message.reply_text(
                "Audio is temporarily unavailable. Please retry later; /today still works."
            )


async def lesson_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        day = int(context.args[0])
        lesson(day)
    except (ValueError, IndexError):
        await update.message.reply_text("Use /lesson 1 through /lesson 365, for example /lesson 42.")
        return
    await update.message.reply_text(format_lesson(day), reply_markup=lesson_buttons(day))


async def practice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(load_data(), update.effective_chat.id)
    day = course_day(user)
    await update.message.reply_text(format_lesson(day, translations=False),
                                    reply_markup=lesson_buttons(day))


async def review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(load_data(), update.effective_chat.id)
    day = course_day(user)
    await update.message.reply_text(format_review(day), reply_markup=review_buttons(day))


def study_streak(dates, now):
    days = set(dates)
    cursor = now if now.isoformat() in days else now - dt.timedelta(days=1)
    count = 0
    while cursor.isoformat() in days:
        count += 1
        cursor -= dt.timedelta(days=1)
    return count


async def progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(load_data(), update.effective_chat.id)
    day = course_day(user)
    completed = len(set(user["completed_lessons"]))
    await update.message.reply_text(
        f"📚 365-day language programme\n"
        f"Current lesson: {day}/365 · {lesson(day)['level']}\n"
        f"Lessons practised: {completed}/365\n"
        f"Learning streak: {study_streak(user['studied_dates'], local_today())} day(s)\n"
        "\nTip: Listen twice, repeat, and tap ✅ I practised."
    )


async def audio_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_text("Use /audioauto on or /audioauto off.")
        return
    enabled = context.args[0].lower() == "on"
    data = load_data()
    get_user(data, update.effective_chat.id)["auto_audio"] = enabled
    save_data(data)
    await update.message.reply_text(
        f"Automatic daily Japanese and Italian MP3s: {'ON' if enabled else 'OFF'}.\n"
        "You can always play a lesson via /today or /listen."
    )


async def handle_language_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":")
    command = parts[0]
    try:
        day = int(parts[-1])
        lesson(day)
    except (ValueError, IndexError):
        await query.answer("Lesson not found", show_alert=True)
        return
    await query.answer()  # acknowledge immediately, before any network TTS request
    chat_id = query.message.chat_id
    if command == "play" and len(parts) == 3 and parts[1] in ("ja", "it"):
        try:
            await send_pronunciation(context.bot, chat_id, parts[1], day)
        except Exception:
            LOG.exception("Audio button failed")
            await context.bot.send_message(chat_id, "Audio unavailable right now. Try again later.")
    elif command == "dialogue":
        await context.bot.send_message(chat_id, format_dialogue(day))
    elif command == "review":
        await context.bot.send_message(chat_id, format_review(day), reply_markup=review_buttons(day))
    elif command == "answers":
        await context.bot.send_message(chat_id, format_review(day, translations=True))
    elif command == "studied":
        data = load_data()
        user = get_user(data, chat_id)
        if day not in user["completed_lessons"]:
            user["completed_lessons"].append(day)
        today = local_today().isoformat()
        if today not in user["studied_dates"]:
            user["studied_dates"].append(today)
        save_data(data)
        await context.bot.send_message(
            chat_id, f"✅ Day {day} practised! Learning streak: "
                     f"{study_streak(user['studied_dates'], local_today())} day(s)."
        )


async def nudge_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await deliver_nudge(context, update.effective_chat.id)


async def send_nudge(context: ContextTypes.DEFAULT_TYPE):
    await deliver_nudge(context, context.job.chat_id)


async def deliver_nudge(context, chat_id):
    habit = random.choice(HABITS)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done", callback_data="done")]])
    await context.bot.send_message(chat_id, text=f"Time for a micro-habit:\n\n{habit}",
                                   reply_markup=keyboard)


async def on_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = load_data()
    user = get_user(data, query.message.chat_id)
    today = local_today()
    yesterday = today - dt.timedelta(days=1)
    if user["last_done"] != today.isoformat():
        user["streak"] = user["streak"] + 1 if user["last_done"] == yesterday.isoformat() else 1
        user["best"] = max(user["best"], user["streak"])
        user["last_done"] = today.isoformat()
    user["total"] += 1
    save_data(data)
    await query.edit_message_text(
        text=f"{query.message.text}\n\n✅ Done! Habit streak: {user['streak']} day(s)."
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(load_data(), update.effective_chat.id)
    await update.message.reply_text(
        f"📊 Habit progress\nCurrent streak: {user['streak']} day(s)\n"
        f"Best streak: {user['best']} day(s)\nTotal actions: {user['total']}"
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    for prefix in ("habit:", "language:", ""):
        for job in context.job_queue.get_jobs_by_name(f"{prefix}{chat_id}"):
            job.schedule_removal()
    data = load_data()
    get_user(data, chat_id)["subscribed"] = False
    save_data(data)
    await update.message.reply_text("Scheduled habits and language lessons paused. /start resumes them.")


def main():
    if not TOKEN:
        raise SystemExit("Set BOT_TOKEN in your Render environment; do not hardcode it.")
    threading.Thread(target=start_web_server, daemon=True).start()
    app = Application.builder().token(TOKEN).post_init(restore_jobs).build()
    for command, handler in (
        ("start", start), ("nudge", nudge_now), ("stats", stats),
        ("today", today_language), ("listen", listen), ("lesson", lesson_command),
        ("practice", practice), ("review", review), ("dialogue", dialogue_command), ("progress", progress),
        ("audioauto", audio_auto), ("stop", stop),
    ):
        app.add_handler(CommandHandler(command, handler))
    app.add_handler(CallbackQueryHandler(on_done, pattern=r"^done$"))
    app.add_handler(CallbackQueryHandler(
        handle_language_buttons,
        pattern=r"^(?:play:(?:ja|it):\d{1,3}|studied:\d{1,3}|review:\d{1,3}|dialogue:\d{1,3}|answers:\d{1,3})$",
    ))
    LOG.info("Bot ready with 365 lessons. UTC+8 schedule: %s", LANGUAGE_TIME)
    app.run_polling()


if __name__ == "__main__":
    main()
