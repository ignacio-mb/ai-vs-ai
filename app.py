"""Flask web app: two AIs debate a user-chosen topic, streamed live to the browser.

Architecture
------------
* ``POST /api/start``  -> validates keys, spins up a background thread running the
  :class:`debate.DebateEngine`, returns a session id.
* ``GET  /api/stream/<sid>`` -> Server-Sent Events; relays every engine event to
  the browser as it happens (token-by-token).
* ``POST /api/stop/<sid>``   -> politely asks a running debate to stop.

API keys may be supplied per-request from the browser, or loaded once from a
local ``.env`` file (ANTHROPIC_API_KEY / OPENAI_API_KEY). Browser-supplied keys
live only in memory for the duration of a session; env keys stay in the server
process. Either way, nothing is written back to disk by the app.
"""

import json
import os
import queue
import threading
import uuid
from typing import Dict

from flask import (
    Flask, Response, jsonify, redirect, render_template, request, session, url_for,
)

try:
    from dotenv import load_dotenv
    load_dotenv()  # read a .env file in the working dir, if present
except ImportError:
    pass  # python-dotenv is optional; env vars still work without it

import analytics
import auth
import store
from debate import DebateEngine, Participant
from llm import LLMError, list_models, validate_key

app = Flask(__name__)
auth.init_app(app)
store.init_db()


def _status_for(reason):
    """Map an engine end-reason to a stored status."""
    if reason == "stopped":
        return "stopped"
    if reason in ("consensus", "max_rounds"):
        return "done"
    return "error"

# Keys/models loaded from the environment (.env). Used as a fallback whenever the
# browser leaves a field blank, so the user can configure once and forget.
ENV_CLAUDE_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ENV_CHATGPT_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
ENV_CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "").strip()
ENV_CHATGPT_MODEL = os.environ.get("CHATGPT_MODEL", "").strip()

# Managed-keys mode (for public/cloud deploys): users never supply API keys; the
# server always uses the owner-provided env keys and ignores anything from the
# browser. The key fields are also hidden in the UI.
MANAGED_KEYS = os.environ.get("MANAGED_KEYS", "0") == "1"
if MANAGED_KEYS and not (ENV_CLAUDE_KEY and ENV_CHATGPT_KEY):
    app.logger.warning(
        "MANAGED_KEYS=1 but ANTHROPIC_API_KEY/OPENAI_API_KEY are not both set; "
        "debates will fail until they are configured."
    )

# In-memory session registry. Each session holds its event queue, the engine,
# and a stop flag. Fine for a single-process local tool; swap for Redis if you
# ever run multiple workers.
SESSIONS: Dict[str, Dict] = {}
SESSIONS_LOCK = threading.Lock()

# Sentinel pushed onto a session's queue to tell the SSE generator to close.
_CLOSE = object()


def _default_models():
    return {
        "claude": ENV_CLAUDE_MODEL or "claude-sonnet-4-6",
        "chatgpt": ENV_CHATGPT_MODEL or "gpt-4o",
    }


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.route("/login")
def login_page():
    if not auth.ENABLED or session.get("user"):
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True, silent=True) or {}
    if auth.check(data.get("username"), data.get("password")):
        auth.login(data.get("username"))
        return jsonify({"ok": True})
    return jsonify({"error": "Invalid username or password."}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    auth.logout()
    return jsonify({"ok": True})


@app.route("/")
def index():
    # Tell the UI which keys are already available from .env so it can mark those
    # fields optional and avoid asking again.
    keys_loaded = {
        "claude": bool(ENV_CLAUDE_KEY),
        "chatgpt": bool(ENV_CHATGPT_KEY),
    }
    return render_template(
        "index.html",
        page="debate",
        default_models=_default_models(),
        keys_loaded=keys_loaded,
        managed_keys=MANAGED_KEYS,
        auth_enabled=auth.ENABLED,
        current_user=session.get("user"),
    )


# Maps the UI's vendor name to (provider, env key getter).
_VENDORS = {
    "claude": ("anthropic", lambda: ENV_CLAUDE_KEY),
    "chatgpt": ("openai", lambda: ENV_CHATGPT_KEY),
}


@app.route("/api/models", methods=["POST"])
def models():
    """List a vendor's available chat models, fetched live from their API.

    Uses a key typed in the browser if provided, else the .env key.
    """
    data = request.get_json(force=True, silent=True) or {}
    vendor = (data.get("provider") or "").strip().lower()
    if vendor not in _VENDORS:
        return jsonify({"error": "Unknown provider."}), 400

    provider, env_key = _VENDORS[vendor]
    # In managed mode, never honour a browser-supplied key.
    if MANAGED_KEYS:
        api_key = env_key()
    else:
        api_key = (data.get("api_key") or "").strip() or env_key()
    if not api_key:
        return jsonify({"error": "No API key yet — enter one or set it in .env."}), 400

    try:
        found = list_models(provider, api_key)
    except LLMError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({
        "provider": vendor,
        "models": found,
        "default": _default_models()[vendor],
    })


@app.route("/api/start", methods=["POST"])
def start():
    data = request.get_json(force=True, silent=True) or {}

    topic = (data.get("topic") or "").strip()
    if not topic:
        return jsonify({"error": "Please enter a topic to discuss."}), 400

    if MANAGED_KEYS:
        # Public deployment: always use the owner's keys, never the browser's.
        claude_key, chatgpt_key = ENV_CLAUDE_KEY, ENV_CHATGPT_KEY
        if not (claude_key and chatgpt_key):
            return jsonify({"error": "This deployment is missing its API keys. "
                            "Contact the administrator."}), 503
    else:
        # Prefer a key typed in the browser; otherwise fall back to the .env value.
        claude_key = (data.get("claude_key") or "").strip() or ENV_CLAUDE_KEY
        chatgpt_key = (data.get("chatgpt_key") or "").strip() or ENV_CHATGPT_KEY
        missing = []
        if not claude_key:
            missing.append("Claude")
        if not chatgpt_key:
            missing.append("ChatGPT")
        if missing:
            return jsonify({"error": "Missing API key for: " + ", ".join(missing) +
                            ". Enter it above or set it in your .env file."}), 400

    claude_model = (data.get("claude_model") or _default_models()["claude"]).strip()
    chatgpt_model = (data.get("chatgpt_model") or _default_models()["chatgpt"]).strip()

    try:
        max_rounds = int(data.get("max_rounds") or 6)
    except (TypeError, ValueError):
        max_rounds = 6
    max_rounds = max(1, min(max_rounds, 12))

    # Optional: a quick connectivity probe so the user gets an instant, clear
    # error instead of a failure three messages into the debate.
    if data.get("validate", True):
        err = validate_key("anthropic", claude_key, claude_model)
        if err:
            return jsonify({"error": "Claude key/model check failed: " + err}), 400
        err = validate_key("openai", chatgpt_key, chatgpt_model)
        if err:
            return jsonify({"error": "ChatGPT key/model check failed: " + err}), 400

    participants = [
        Participant("claude", "Claude", "anthropic", claude_model, claude_key, "claude"),
        Participant("chatgpt", "ChatGPT", "openai", chatgpt_model, chatgpt_key, "chatgpt"),
    ]

    # The user picks who opens; participants[0] is the opener (and writes the
    # closing synthesis). Default to Claude if the value is missing/unknown.
    starter = (data.get("starter") or "claude").strip().lower()
    if starter == "chatgpt":
        participants.reverse()

    sid = uuid.uuid4().hex
    q: "queue.Queue" = queue.Queue()
    stop_flag = {"stop": False}

    def emit(event: Dict):
        q.put(event)

    def should_stop():
        return stop_flag["stop"]

    engine = DebateEngine(
        topic=topic,
        participants=participants,
        emit=emit,
        max_rounds=max_rounds,
        should_stop=should_stop,
    )

    session = {
        "queue": q,
        "stop_flag": stop_flag,
        "topic": topic,
        "engine": engine,
        "busy": True,        # a turn is currently being generated
    }

    # Record the conversation immediately so it shows up in history even if the
    # process is killed mid-debate; the transcript is filled in as it finishes.
    store.create_conversation(sid, topic, starter, claude_model, chatgpt_model)

    def persist():
        store.save_transcript(
            sid, engine.transcript,
            status=_status_for(engine.final_reason),
            reason=engine.final_reason,
        )
        # Post-process analytics (best-effort: never let it break persistence).
        try:
            conv = store.get_conversation(sid)
            if conv:
                store.save_analytics(sid, analytics.analyze(conv))
        except Exception as exc:  # noqa: BLE001
            app.logger.warning("analytics failed for %s: %s", sid, exc)

    def run():
        # The session stays alive after the debate ends so the user can ask
        # follow-up questions; we only close it on an explicit /api/end.
        try:
            engine.run()
        finally:
            session["busy"] = False
            session["persist"] = persist
            persist()

    thread = threading.Thread(target=run, daemon=True)
    session["thread"] = thread

    with SESSIONS_LOCK:
        SESSIONS[sid] = session

    thread.start()
    return jsonify({"session_id": sid, "topic": topic})


@app.route("/api/comment/<sid>", methods=["POST"])
def comment(sid):
    """Let the human pose a follow-up question that BOTH AIs then answer."""
    with SESSIONS_LOCK:
        session = SESSIONS.get(sid)
    if session is None:
        return jsonify({"error": "Unknown session."}), 404
    if session["busy"]:
        return jsonify({"error": "The AIs are still responding — hold on."}), 409

    data = request.get_json(force=True, silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Type a question first."}), 400

    engine = session["engine"]
    # A prior Stop would leave the flag set; clear it so the follow-up can run.
    session["stop_flag"]["stop"] = False
    session["busy"] = True

    def run():
        try:
            engine.answer_user(question)
        finally:
            session["busy"] = False
            persist = session.get("persist")
            if persist:
                persist()  # save the transcript including the follow-up exchange

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/end/<sid>", methods=["POST"])
def end(sid):
    """Close a session for good and let the SSE stream shut down."""
    with SESSIONS_LOCK:
        session = SESSIONS.get(sid)
    if session is None:
        return jsonify({"ok": True})
    session["stop_flag"]["stop"] = True
    session["queue"].put(_CLOSE)
    return jsonify({"ok": True})


@app.route("/api/stream/<sid>")
def stream(sid):
    with SESSIONS_LOCK:
        session = SESSIONS.get(sid)
    if session is None:
        return jsonify({"error": "Unknown session."}), 404

    q = session["queue"]

    def generate():
        # Tell the client which topic this stream is for.
        yield _sse({"type": "connected", "topic": session["topic"]})
        while True:
            try:
                event = q.get(timeout=15)
            except queue.Empty:
                # Heartbeat keeps proxies and the browser from dropping the
                # idle connection.
                yield ": keep-alive\n\n"
                continue
            if event is _CLOSE:
                yield _sse({"type": "closed"})
                break
            yield _sse(event)
        _cleanup(sid)

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
        "Connection": "keep-alive",
    }
    return Response(generate(), headers=headers)


@app.route("/api/stop/<sid>", methods=["POST"])
def stop(sid):
    with SESSIONS_LOCK:
        session = SESSIONS.get(sid)
    if session is None:
        return jsonify({"error": "Unknown session."}), 404
    session["stop_flag"]["stop"] = True
    return jsonify({"ok": True})


@app.route("/api/history")
def history_list():
    return jsonify({"conversations": store.list_conversations()})


@app.route("/api/history/<conv_id>")
def history_get(conv_id):
    conv = store.get_conversation(conv_id)
    if conv is None:
        return jsonify({"error": "Not found."}), 404
    return jsonify(conv)


@app.route("/api/history/<conv_id>", methods=["DELETE"])
def history_delete(conv_id):
    return jsonify({"deleted": store.delete_conversation(conv_id)})


@app.route("/analytics")
def analytics_page():
    return render_template(
        "analytics.html",
        page="analytics",
        auth_enabled=auth.ENABLED,
        current_user=session.get("user"),
    )


@app.route("/api/analytics")
def analytics_all():
    """Cross-debate dashboard: global aggregates + a per-debate index."""
    rows = store.list_analytics()
    overview = analytics.aggregate([r["stats"] for r in rows])
    debates = [{
        "id": r["id"],
        "created_at": r["created_at"],
        "topic": r["stats"].get("topic", ""),
        "outcome": r["stats"].get("outcome"),
        "archetype": r["stats"].get("archetype"),
        "ai_turns": r["stats"].get("ai_turns"),
        "held_ground": r["stats"].get("held_ground"),
        "total_words": r["stats"].get("total_words"),
    } for r in rows]
    return jsonify({"overview": overview, "debates": debates})


@app.route("/api/analytics/<conv_id>")
def analytics_one(conv_id):
    stats = store.get_analytics(conv_id)
    if stats is None:
        # Compute on the fly for older conversations that predate analytics.
        conv = store.get_conversation(conv_id)
        if conv is None:
            return jsonify({"error": "Not found."}), 404
        stats = analytics.analyze(conv)
        store.save_analytics(conv_id, stats)
    return jsonify(stats)


def _cleanup(sid):
    with SESSIONS_LOCK:
        SESSIONS.pop(sid, None)


def _sse(event: Dict) -> str:
    return "data: " + json.dumps(event) + "\n\n"


if __name__ == "__main__":
    # threaded=True is essential: the SSE request and the debate thread run
    # concurrently per session. Bind to 0.0.0.0 and $PORT so the same entrypoint
    # works locally and inside a container (Render sets PORT).
    port = int(os.environ.get("PORT", "5000"))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port, threaded=True, debug=False)
