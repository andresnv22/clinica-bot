# Clinic Bot 🦷: AI Appointment Assistant

An AI agent that handles patient conversations for a dental clinic, the way a receptionist would on WhatsApp: it answers questions about services and prices, checks real availability, books appointments and cancels them.

## Why it's reliable

The AI never touches the database directly. It can only call five tools, and **all business rules live in code, not in the prompt**:

- It can't offer a time slot without checking real availability first.
- **No double-booking, even when patients book at the same time**: the check and the insert run in one locked database transaction (tested with 20 simultaneous bookings for the same slot).
- **Retries never duplicate an appointment**: booking the same slot twice for the same patient returns the existing one.
- **The AI never handles the patient's phone number**: the system injects it, so a patient can only see and cancel their own appointments, even if they try to trick the bot.
- Past dates, weekends, holidays, out-of-hours and far-future requests are rejected by the code.
- Times are understood the way people write them ("3pm", "3:30 p.m.", "15:00").
- Dates and "now" use the clinic's time zone, not the server's.

**Patients never see a technical error.** If the AI provider fails, the patient gets a friendly message, the conversation rolls back to a clean state, and the details go to the server log. Two quick messages from the same patient are handled in order instead of getting mixed.

It works with **two AI providers** (Google Gemini or Anthropic Claude), chosen in one line of config. With Gemini, if the main model is overloaded or out of quota, the bot switches to a backup model within seconds and skips the failing one for a few minutes.

## How it works

```
Patient ──> FastAPI (/chat) ──> Agent (Gemini or Claude) ──> Tools ──> SQLite
                                      ▲                          │
                                      └──────── result ──────────┘
```

| File | What it does |
|---|---|
| `app/tools.py` | The 5 tools the agent can use, plus the business rules |
| `app/agent.py` | The agent loop (one version for each provider) and the assistant's personality |
| `app/db.py` | Database: services, appointments, opening hours |
| `app/main.py` | Web server and error handling |
| `static/index.html` | WhatsApp-style demo with the clinic's live schedule |

**Stack:** Python, FastAPI, SQLite, Google Gemini API, Anthropic Claude API (tool use / function calling).

## Run it locally

1. Get an API key:
   - **Gemini** (has a free tier): https://aistudio.google.com
   - **Claude**: https://console.anthropic.com
2. Copy `.env.example` to `.env`, paste your key and choose the provider with `PROVEEDOR=gemini` or `PROVEEDOR=claude`.
3. Run it (requires [uv](https://docs.astral.sh/uv/)):

```bash
uv run uvicorn app.main:app --reload
```

4. Open http://localhost:8000

## Tests

```bash
uv run pytest                          # 47 tests of the business rules, no AI calls, under 1 second
uv run python -m scripts.prueba_real   # tricky real conversations against the AI (uses API quota)
```

## Before going to production

- Use a **paid API plan**: free tiers get overloaded and run out of quota.
- Protect the `/citas` endpoint (the clinic's schedule) with a password.
- Store conversations in the database instead of memory.
- Load the clinic's holidays into `DIAS_CERRADOS` (`app/db.py`).

## Roadmap

- [ ] Connect to real WhatsApp (Twilio / WhatsApp Business API)
- [ ] Automatic reminders the day before each appointment
- [ ] Store conversations in the database
- [ ] Deploy to a server (Railway, Render or Fly.io)

---

The code comments and the assistant itself are in Spanish, since it was built for clinics in Latin America.
