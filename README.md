# AI vs AI — Live Debate

A small Flask web app where two AIs — **Claude** (Anthropic) and **ChatGPT**
(OpenAI) — discuss a topic you choose. They argue from real reasoning, challenge
each other's weak or invalid claims, concede when they're wrong, and try to reach
a shared conclusion. You watch the whole three-way conversation stream in real
time and a neutral moderator writes a closing synthesis.

## How it works

```
Browser ──POST /api/start──▶ Flask ──▶ DebateEngine (background thread)
   ▲                                          │ emits events
   └──────── SSE  /api/stream/<id> ◀──────────┘ (token-by-token)
```

- **`app.py`** — Flask routes, session registry, Server-Sent Events.
- **`debate.py`** — the turn-by-turn engine: system prompts, transcript
  bookkeeping, consensus detection, early stop, and the closing synthesis.
- **`llm.py`** — vendor-agnostic streaming wrappers for Anthropic and OpenAI.
- **`templates/`, `static/`** — the single-page UI.

The conversation ends when **both** models signal `CONSENSUS: yes`, or when the
round limit is hit — so it lasts about as long as a real human conversation,
never "for hours."

## Run it

```bash
cd /Users/ignaciobeinesfurcada/dev/interaction
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open <http://127.0.0.1:5000>, paste your two API keys, enter a topic, and
hit **Start debate**.

### Saving keys in a `.env` file

So you don't paste keys every time, copy the example and fill it in:

```bash
cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY and OPENAI_API_KEY
```

On startup the app loads `.env`. Those key fields in the UI then show
"✓ loaded from .env" and become optional — leave them blank to use the saved
keys, or type a different key to override just for that run. You can also set
`CLAUDE_MODEL` / `CHATGPT_MODEL` there to change the default models. `.env` is
git-ignored so your keys never get committed.

## Notes

- **Keys** are kept in memory only for the lifetime of a session and are never
  written to disk. They're sent from your browser to your own local server.
- **Models** default to `claude-sonnet-4-6` and `gpt-4o`; change them in the UI.
- **Who starts** lets you pick which AI opens the conversation (the opener also
  writes the closing synthesis); the other replies.
- **Max rounds** (1–12) caps the length. Each turn is also length-capped to keep
  things conversational.
- "Verify keys before starting" does a tiny probe call so you get an instant,
  clear error instead of failing mid-debate. Uncheck it to skip (saves two calls).


what's the best European city to live as a 26-year old digital nomad, single. Not on a budget, but also not super expensive stuff. prioritize work life balance. i do a lot of sports, like to hang out in cool places, and enjoy time outside. 