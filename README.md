# Clinic Bot 🦷: AI Appointment Assistant

An AI agent that handles patient conversations for a dental clinic, the way a receptionist would on WhatsApp: it answers questions about services and prices, checks real availability, books appointments and cancels them.

## Why it's reliable

The AI never touches the database directly. It can only call five tools, and **all business rules live in code, not in the prompt**:

- It can't offer a time slot without checking real availability first.
- Double-booking is impossible: every booking is re-validated against the schedule.
- Past dates, weekends and out-of-hours requests are rejected by the code.
- A patient can only cancel appointments tied to their own phone number.
- If a tool fails, the agent gets an error message and recovers instead of crashing.

It also works with **two AI providers** (Google Gemini or Anthropic Claude), chosen in one line of config. With Gemini, if the main model is overloaded or out of quota, the bot switches to a backup model within seconds and skips the failing one for a few minutes, instead of leaving the patient waiting.

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

## Roadmap

- [ ] Connect to real WhatsApp (Twilio / WhatsApp Business API)
- [ ] Automatic reminders the day before each appointment
- [ ] Store conversations in the database
- [ ] Deploy to a server (Railway, Render or Fly.io)

---

The code comments and the assistant itself are in Spanish, since it was built for clinics in Latin America.
