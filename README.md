# Svitlo AI — Telegram MVP

Mental health training assistant for veterans (EN/UK). **Not medical service.**

## Features
- /daily — daily check-in (stress 0–10, triggers, sleep hours, micro-goal)
- /breath — box breathing pacing
- /ground — grounding 5-4-3-2-1
- /sleep — quick sleep hygiene tips
- /plan — set up to 3 micro-goals
- /triggers — log triggers anytime
- /report — summary for last 7/30 days (avg stress, sleep, top triggers)
- /settings — set language (en/uk) and helpline country (US/UA)
- Crisis guard: detects self-harm intent and shows helplines (US 988; UA 7333)

Optional supportive chat via OpenAI (keeps to training, not therapy).

## Quick start
1. Create bot with @BotFather and get token.
2. Clone files and create `.env` from `.env.example`:
```
TELEGRAM_BOT_TOKEN=123456:ABC...
OPENAI_API_KEY= # optional
DEFAULT_LANG=en
DEFAULT_COUNTRY=US
ADMINS=
```
3. Install deps & run:
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```
4. DM your bot in Telegram: `/start`

## Docker
```
docker build -t svitlo-bot .
docker run --env-file .env --name svitlo svitlo-bot
```

## Notes
- SQLite is stored in svitlo.sqlite3 (same folder).
- This is NOT a diagnostic or medical tool.
- Add/adjust helplines in i18n files and /settings.
- Expand reports to PDF later if needed.
