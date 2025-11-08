import asyncio
import os
import re
import json
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import aiosqlite
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    MessageHandler, ConversationHandler, filters, ContextTypes
)

# === Load env ===
load_dotenv()
BOT_TOKEN = os.getenv("8572726100:AAEzKy10Lx-7fpt1ZuxtjsW9xYay7lIko0I")
DEFAULT_LANG = os.getenv("DEFAULT_LANG", "en")
DEFAULT_COUNTRY = os.getenv("DEFAULT_COUNTRY", "US").upper()
ADMINS = [int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip().isdigit()]
OPENAI_API_KEY = os.getenv("8572726100:AAEzKy10Lx-7fpt1ZuxtjsW9xYay7lIko0I", "").strip()

# === i18n ===
def load_i18n(lang: str) -> Dict[str, str]:
    path = os.path.join(os.path.dirname(__file__), "i18n", f"{lang}.json")
    if not os.path.exists(path):
        path = os.path.join(os.path.dirname(__file__), "i18n", f"{DEFAULT_LANG}.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# === DB ===
DB_PATH = os.path.join(os.path.dirname(__file__), "svitlo.sqlite3")

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            lang TEXT,
            country TEXT,
            created_at TEXT
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            ts TEXT,
            stress REAL,
            triggers TEXT,
            sleep_hours REAL,
            micro_goal TEXT
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS triggers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            ts TEXT,
            note TEXT
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            ts TEXT,
            item TEXT
        )""")
        await db.commit()

# === Helpers ===
SUICIDE_PATTERNS = re.compile(
    r"\b(kill myself|suicide|end it|self-harm|cut myself|want to die|не хочу жити|суїцид|покінчити|зарізатись|вкоротити|самопошкодження)\b",
    re.IGNORECASE
)

async def get_user(ctx: ContextTypes.DEFAULT_TYPE, user_id: int) -> Dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, lang, country FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            if row:
                return {"user_id": row[0], "lang": row[1], "country": row[2]}
        # create
        async with db.execute("INSERT INTO users (user_id, lang, country, created_at) VALUES (?,?,?,?)",
                              (user_id, DEFAULT_LANG, DEFAULT_COUNTRY, datetime.utcnow().isoformat())):
            await db.commit()
        return {"user_id": user_id, "lang": DEFAULT_LANG, "country": DEFAULT_COUNTRY}

async def set_user_lang(user_id: int, lang: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET lang=? WHERE user_id=?", (lang, user_id))
        await db.commit()

async def set_user_country(user_id: int, country: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET country=? WHERE user_id=?", (country, user_id))
        await db.commit()

async def save_checkin(user_id: int, stress: float, triggers: str, sleep_hours: float, micro_goal: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO checkins (user_id, ts, stress, triggers, sleep_hours, micro_goal) VALUES (?,?,?,?,?,?)",
            (user_id, datetime.utcnow().isoformat(), stress, triggers, sleep_hours, micro_goal)
        )
        await db.commit()

async def save_trigger(user_id: int, note: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO triggers (user_id, ts, note) VALUES (?,?,?)",
                         (user_id, datetime.utcnow().isoformat(), note))
        await db.commit()

async def save_plan_item(user_id: int, item: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO plans (user_id, ts, item) VALUES (?,?,?)",
                         (user_id, datetime.utcnow().isoformat(), item))
        await db.commit()

async def aggregate_report(user_id: int, days: int):
    since = datetime.utcnow() - timedelta(days=days)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT stress, sleep_hours, triggers FROM checkins WHERE user_id=? AND ts>=?",
                              (user_id, since.isoformat())) as cur:
            rows = await cur.fetchall()
    if not rows:
        return None
    stresses = [r[0] for r in rows if r[0] is not None]
    sleeps = [r[1] for r in rows if r[1] is not None]
    trg_texts = " ".join([r[2] or "" for r in rows])
    # naive top words (triggers) extraction
    words = re.findall(r"[A-Za-zА-Яа-яЇїІіЄєҐґ']{3,}", trg_texts)
    freq = {}
    for w in words:
        w = w.lower()
        freq[w] = freq.get(w, 0) + 1
    top = ", ".join([w for w, _ in sorted(freq.items(), key=lambda x: x[1], reverse=True)[:5]]) or "—"
    avg_stress = sum(stresses)/len(stresses) if stresses else 0.0
    avg_sleep = sum(sleeps)/len(sleeps) if sleeps else 0.0
    return {
        "avg_stress": avg_stress,
        "avg_sleep": avg_sleep,
        "n": len(rows),
        "top_triggers": top
    }

# === States ===
DAILY_STRESS, DAILY_TRIGGERS, DAILY_SLEEP, DAILY_GOAL = range(4)
GROUNDING_FLOW = range(1)
PLAN_FLOW = range(1)
TRIGGERS_FLOW = range(1)
BREATH_FLOW = range(1)

# === Crisis guard ===
async def crisis_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    text = (update.message.text if update.message else "") or ""
    if SUICIDE_PATTERNS.search(text):
        user = await get_user(context, update.effective_user.id)
        t = load_i18n(user["lang"])
        await update.message.reply_text(t["crisis_detected"])
        return True
    return False

# === Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(context, update.effective_user.id)
    t = load_i18n(user["lang"])
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("EN", callback_data="lang_en"),
        InlineKeyboardButton("UK", callback_data="lang_uk")
    ]])
    await update.message.reply_text(t["start"], parse_mode=ParseMode.MARKDOWN)
    await update.message.reply_text(t["choose_lang"], reply_markup=kb)

async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    user = await get_user(context, update.effective_user.id)
    t = load_i18n(user["lang"])
    if data.startswith("lang_"):
        lang = data.split("_")[1]
        await set_user_lang(user["user_id"], lang)
        t = load_i18n(lang)
        await q.answer("OK")
        await q.edit_message_text(f"{t['saved']} Language set to {lang.upper()}.")
    else:
        await q.answer("OK")

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(context, update.effective_user.id)
    t = load_i18n(user["lang"])
    await update.message.reply_text(t["settings"].format(lang=user["lang"], country=user["country"]))

async def wildcard_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # parse 'lang en' or 'country US'
    user = await get_user(context, update.effective_user.id)
    t = load_i18n(user["lang"])
    text = update.message.text.strip()
    if text.lower().startswith("lang "):
        lang = text.split()[-1].lower()
        if lang in ("en","uk"):
            await set_user_lang(user["user_id"], lang)
            await update.message.reply_text(t["saved"])
        else:
            await update.message.reply_text("en / uk")
    elif text.lower().startswith("country "):
        c = text.split()[-1].upper()
        if c in ("US","UA"):
            await set_user_country(user["user_id"], c)
            await update.message.reply_text(t["saved"])
        else:
            await update.message.reply_text("US / UA")
    else:
        await update.message.reply_text(t["unknown"])

# --- Daily check-in flow ---
async def daily_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await crisis_guard(update, context): return ConversationHandler.END
    user = await get_user(context, update.effective_user.id)
    t = load_i18n(user["lang"])
    await update.message.reply_text(t["checkin_intro"])
    return DAILY_STRESS

async def daily_stress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await crisis_guard(update, context): return ConversationHandler.END
    user = await get_user(context, update.effective_user.id)
    t = load_i18n(user["lang"])
    try:
        val = float(update.message.text.strip().replace(",", "."))
        val = max(0.0, min(10.0, val))
    except:
        await update.message.reply_text("0–10 please.")
        return DAILY_STRESS
    context.user_data["stress"] = val
    await update.message.reply_text(t["checkin_stress_saved"].format(val=val))
    return DAILY_TRIGGERS

async def daily_triggers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await crisis_guard(update, context): return ConversationHandler.END
    context.user_data["triggers"] = update.message.text.strip()
    user = await get_user(context, update.effective_user.id)
    t = load_i18n(user["lang"])
    await update.message.reply_text(t["checkin_triggers_saved"])
    return DAILY_SLEEP

async def daily_sleep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(context, update.effective_user.id)
    t = load_i18n(user["lang"])
    try:
        hours = float(update.message.text.strip().replace(",", "."))
    except:
        await update.message.reply_text("e.g., 6.5")
        return DAILY_SLEEP
    context.user_data["sleep_hours"] = hours
    await update.message.reply_text(t["checkin_sleep_saved"])
    return DAILY_GOAL

async def daily_goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(context, update.effective_user.id)
    t = load_i18n(user["lang"])
    goal = update.message.text.strip()
    await save_checkin(user["user_id"],
                       context.user_data.get("stress"),
                       context.user_data.get("triggers",""),
                       context.user_data.get("sleep_hours"),
                       goal)
    await update.message.reply_text(t["checkin_done"])
    return ConversationHandler.END

# --- Breathing ---
async def breath(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(context, update.effective_user.id)
    t = load_i18n(user["lang"])
    await update.message.reply_text(t["breath_intro"])
    return 0

async def breath_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(context, update.effective_user.id)
    t = load_i18n(user["lang"])
    if update.message.text.strip().lower() != "go":
        await update.message.reply_text("Type 'go' to begin.")
        return 0
    await update.message.reply_text(t["breath_go"])
    return ConversationHandler.END

# --- Grounding ---
GROUND_STEPS = [
    ("5 things you see", "things you can see around you"),
    ("4 things you touch", "textures or objects"),
    ("3 things you hear", "ambient sounds"),
    ("2 things you smell", "scents, even faint"),
    ("1 thing you taste", "or imagine a taste"),
]
GROUND_STEPS_UK = [
    ("5 що бачиш", "предмети навколо"),
    ("4 що торкаєшся", "текстури чи об'єкти"),
    ("3 що чуєш", "довколишні звуки"),
    ("2 що відчуваєш на запах", "навіть ледь відчутні"),
    ("1 на смак", "або уяви смак"),
]

async def ground(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(context, update.effective_user.id)
    t = load_i18n(user["lang"])
    await update.message.reply_text(t["ground_intro"])
    context.user_data["ground_idx"] = 0
    return 0

async def ground_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(context, update.effective_user.id)
    t = load_i18n(user["lang"])
    idx = context.user_data.get("ground_idx", 0)
    if idx == 0:
        context.user_data["ground_idx"] = 1
        steps = GROUND_STEPS if user["lang"] == "en" else GROUND_STEPS_UK
        count, hint = steps[0][0], steps[0][1]
        await update.message.reply_text(t["ground_step"].format(count=count, sense="", hint=hint))
        return 0
    else:
        steps = GROUND_STEPS if user["lang"] == "en" else GROUND_STEPS_UK
        if idx < len(steps):
            count, hint = steps[idx][0], steps[idx][1]
            context.user_data["ground_idx"] = idx + 1
            await update.message.reply_text(t["ok"] + "\n" + t["ground_step"].format(count=count, sense="", hint=hint))
            return 0
        else:
            await update.message.reply_text("Done. Notice any change in stress 0–10? You can /daily again anytime.")
            return ConversationHandler.END

# --- Plan ---
async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(context, update.effective_user.id)
    t = load_i18n(user["lang"])
    await update.message.reply_text(t["plan_intro"])
    context.user_data["plan_items"] = []
    return 0

async def plan_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(context, update.effective_user.id)
    text = update.message.text.strip()
    if text.lower() == "done":
        for it in context.user_data.get("plan_items", [])[:3]:
            await save_plan_item(user["user_id"], it)
        t = load_i18n(user["lang"])
        await update.message.reply_text(t["plan_saved"])
        return ConversationHandler.END
    else:
        context.user_data.setdefault("plan_items", []).append(text)
        await update.message.reply_text("Added. (type 'done' when finished)")
        return 0

# --- Triggers log ---
async def triggers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(context, update.effective_user.id)
    t = load_i18n(user["lang"])
    await update.message.reply_text(t["triggers_intro"])
    return 0

async def triggers_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(context, update.effective_user.id)
    text = update.message.text.strip()
    if text.lower() == "done":
        t = load_i18n(user["lang"])
        await update.message.reply_text(t["saved"])
        return ConversationHandler.END
    else:
        await save_trigger(user["user_id"], text)
        await update.message.reply_text("Logged. (type 'done' when finished)")
        return 0

# --- Report ---
async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(context, update.effective_user.id)
    t = load_i18n(user["lang"])
    await update.message.reply_text(t["report_intro"])

async def report_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(context, update.effective_user.id)
    t = load_i18n(user["lang"])
    try:
        days = int(update.message.text.strip())
        if days not in (7,30):
            raise ValueError
    except:
        await update.message.reply_text("Reply '7' or '30'.")
        return
    agg = await aggregate_report(user["user_id"], days)
    if not agg:
        await update.message.reply_text("No data yet. Try /daily for a few days.")
        return
    await update.message.reply_text(t["report_ready"].format(days=days, avg=agg["avg_stress"], n=agg["n"], sleep=agg["avg_sleep"], trg=agg["top_triggers"]))

# --- Supportive chat (optional OpenAI) ---
async def fallback_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await crisis_guard(update, context): return
    user = await get_user(context, update.effective_user.id)
    t = load_i18n(user["lang"])
    txt = (update.message.text or "").strip()
    if not OPENAI_API_KEY:
        await update.message.reply_text(t["unknown"])
        return
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    system = (
        "You are Svitlo AI, a mental health training assistant for veterans. "
        "You are NOT a medical or crisis service. "
        "Avoid diagnosis, medications, politics, religion, and graphic trauma details. "
        "Be calm, respectful, brief. Prefer practical exercises (breathing, grounding, micro-goals). "
        "If user mentions self-harm or suicide, refuse and urge to contact local crisis lines."
    )
    user_msg = txt[:2000]
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role":"system","content":system},{"role":"user","content":user_msg}],
        temperature=0.4,
        max_tokens=300,
    )
    out = resp.choices[0].message.content.strip()
    await update.message.reply_text(out)

# --- Admin stats ---
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        u = await (await db.execute("SELECT COUNT(*) FROM users")).fetchone()
        c7 = await (await db.execute("SELECT COUNT(*) FROM checkins WHERE ts>=?", ((datetime.utcnow()-timedelta(days=7)).isoformat(),))).fetchone()
        c30 = await (await db.execute("SELECT COUNT(*) FROM checkins WHERE ts>=?", ((datetime.utcnow()-timedelta(days=30)).isoformat(),))).fetchone()
    await update.message.reply_text(f"Users: {u[0]}\nCheck-ins 7d: {c7[0]}\nCheck-ins 30d: {c30[0]}")

# === Main ===
def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("daily", daily_start)],
        states={
            0: [MessageHandler(filters.TEXT & ~filters.COMMAND, daily_stress)],
            1: [MessageHandler(filters.TEXT & ~filters.COMMAND, daily_triggers)],
            2: [MessageHandler(filters.TEXT & ~filters.COMMAND, daily_sleep)],
            3: [MessageHandler(filters.TEXT & ~filters.COMMAND, daily_goal)],
        },
        fallbacks=[]
    ))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("breath", breath)], states={0:[MessageHandler(filters.TEXT & ~filters.COMMAND, breath_flow)]}, fallbacks=[]))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("ground", ground)], states={0:[MessageHandler(filters.TEXT & ~filters.COMMAND, ground_flow)]}, fallbacks=[]))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("plan", plan)], states={0:[MessageHandler(filters.TEXT & ~filters.COMMAND, plan_flow)]}, fallbacks=[]))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("triggers", triggers)], states={0:[MessageHandler(filters.TEXT & ~filters.COMMAND, triggers_flow)]}, fallbacks=[]))

    app.add_handler(CommandHandler("sleep", lambda u,c: u.message.reply_text(load_i18n((c.application.user_data.get(u.effective_user.id) or {}).get('lang','en')).get("sleep_tips","Sleep tips."))))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("stats", stats))

    app.add_handler(MessageHandler(filters.Regex(r"^(lang\s+(en|uk)|country\s+(US|UA))$"), wildcard_settings))
    app.add_handler(MessageHandler(filters.Regex(r"^(7|30)$"), report_value))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_chat))

    return app

async def main():
    if not BOT_TOKEN:
        print("Missing TELEGRAM_BOT_TOKEN in .env")
        return
    await init_db()
    app = build_app()
    print("Svitlo AI bot is running...")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
