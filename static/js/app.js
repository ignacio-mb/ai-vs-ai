/* Front-end controller.
 * Posts the setup form, opens an SSE stream, and renders the live debate. */

(function () {
  "use strict";

  var els = {
    setup: document.getElementById("setup"),
    form: document.getElementById("start-form"),
    startBtn: document.getElementById("start-btn"),
    setupError: document.getElementById("setup-error"),
    conversation: document.getElementById("conversation"),
    topicDisplay: document.getElementById("topic-display"),
    thread: document.getElementById("thread"),
    status: document.getElementById("status"),
    stopBtn: document.getElementById("stop-btn"),
    resetBtn: document.getElementById("reset-btn"),
    jumpBtn: document.getElementById("jump-btn"),
    commentForm: document.getElementById("comment-form"),
    commentInput: document.getElementById("comment-input"),
    historyPanel: document.getElementById("history-panel"),
    historyList: document.getElementById("history-list"),
    historyEmpty: document.getElementById("history-empty"),
    historyRefresh: document.getElementById("history-refresh"),
  };

  var state = {
    sessionId: null,
    source: null,
    activeBubble: null,   // the <div.bubble> currently streaming
    activeRaw: "",        // raw text accumulated for the active bubble
    activeSpeaker: null,
    pinned: true,         // is the view stuck to the bottom (auto-follow)?
    ended: false,         // has at least one round finished?
  };

  /* ---------- rendering helpers ---------- */

  function renderMarkdown(text) {
    if (window.marked && typeof window.marked.parse === "function") {
      return window.marked.parse(text);
    }
    // Fallback: escape + paragraph breaks.
    var esc = text
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    return esc.split(/\n{2,}/).map(function (p) {
      return "<p>" + p.replace(/\n/g, "<br>") + "</p>";
    }).join("");
  }

  var NEAR_BOTTOM_PX = 90;

  function distanceFromBottom() {
    var t = els.thread;
    return t.scrollHeight - t.scrollTop - t.clientHeight;
  }

  // Only follow new content if the user is already near the bottom. If they've
  // scrolled up to read, leave their position alone and surface a jump button.
  function scrollDown(force) {
    if (!state.pinned && !force) return;
    window.requestAnimationFrame(function () {
      els.thread.scrollTop = els.thread.scrollHeight;
    });
  }

  function onThreadScroll() {
    state.pinned = distanceFromBottom() < NEAR_BOTTOM_PX;
    els.jumpBtn.classList.toggle("hidden", state.pinned);
  }

  els.thread.addEventListener("scroll", onThreadScroll, { passive: true });

  els.jumpBtn.addEventListener("click", function () {
    state.pinned = true;
    els.jumpBtn.classList.add("hidden");
    scrollDown(true);
  });

  function startMessage(speaker, name, accent) {
    var wrap = document.createElement("div");
    wrap.className = "msg " + (accent || speaker);

    var meta = document.createElement("div");
    meta.className = "meta";
    var dot = document.createElement("span");
    dot.className = "who-dot";
    var label = document.createElement("span");
    label.textContent = name;
    meta.appendChild(dot);
    meta.appendChild(label);

    var bubble = document.createElement("div");
    bubble.className = "bubble cursor";
    bubble.textContent = "";

    wrap.appendChild(meta);
    wrap.appendChild(bubble);
    els.thread.appendChild(wrap);

    state.activeBubble = bubble;
    state.activeMeta = meta;
    state.activeRaw = "";
    state.activeSpeaker = speaker;
    scrollDown();
  }

  function appendToken(text) {
    if (!state.activeBubble) return;
    state.activeRaw += text;
    // While streaming, show plain text for speed; render markdown at the end.
    state.activeBubble.textContent = state.activeRaw;
    scrollDown();
  }

  function endMessage(consensus, finalText) {
    if (!state.activeBubble) return;
    var text = (finalText != null && finalText !== "") ? finalText : state.activeRaw;
    state.activeBubble.classList.remove("cursor");
    state.activeBubble.innerHTML = renderMarkdown(text);
    if (consensus === true || consensus === false) {
      var badge = document.createElement("span");
      badge.className = "consensus-badge " + (consensus ? "consensus-yes" : "consensus-no");
      badge.textContent = consensus ? "agrees" : "still debating";
      state.activeMeta.appendChild(badge);
    }
    state.activeBubble = null;
    state.activeMeta = null;
    state.activeRaw = "";
    scrollDown();
  }

  /* Moderator messages arrive whole (no token stream). */
  function wholeMessage(speaker, name, accent, text) {
    startMessage(speaker, name, accent);
    endMessage(null, text);
  }

  function banner(kind, text) {
    var b = document.createElement("div");
    b.className = "banner " + kind;
    b.textContent = text;
    els.thread.appendChild(b);
    scrollDown();
  }

  function setStatus(text) {
    els.status.textContent = text || "";
  }

  /* ---------- event handling ---------- */

  function handleEvent(ev) {
    switch (ev.type) {
      case "connected":
        setStatus("Connected.");
        break;
      case "message":           // whole, non-streamed (moderator kickoff)
        wholeMessage(ev.speaker, ev.name, ev.accent, ev.text);
        break;
      case "message_start":
        startMessage(ev.speaker, ev.name, ev.accent);
        break;
      case "token":
        appendToken(ev.text);
        break;
      case "message_end":
        endMessage(ev.consensus, ev.text);
        break;
      case "status":
        setStatus(ev.message);
        break;
      case "error":
        // If an error lands mid-message, close the open bubble first.
        if (state.activeBubble) endMessage(null, state.activeRaw);
        banner("err", "⚠ " + ev.message);
        setStatus("Error");
        break;
      case "done":
        finishUp(ev.reason);
        break;
      case "comment_done":
        // The AIs finished answering a follow-up; re-open the input.
        setCommentBusy(false);
        setStatus("Ready for another question.");
        break;
      case "closed":
        // Server closed the session for good.
        closeStream();
        break;
    }
  }

  function finishUp(reason) {
    var msg = "Conversation ended.";
    if (reason === "consensus") msg = "✓ The AIs reached consensus.";
    else if (reason === "max_rounds") msg = "Conversation ended (round limit).";
    else if (reason === "stopped") msg = "Conversation stopped.";
    banner("done", msg);
    setStatus("Done — ask them a follow-up below.");
    els.stopBtn.classList.add("hidden");
    els.resetBtn.classList.remove("hidden");
    // Keep the stream OPEN so follow-up questions can be answered live.
    state.ended = true;
    els.commentForm.classList.remove("hidden");
    setCommentBusy(false);
    els.commentInput.focus();
  }

  function setCommentBusy(busy) {
    els.commentForm.classList.toggle("busy", busy);
    els.commentInput.disabled = busy;
    if (!busy) els.commentInput.placeholder = "Ask Claude and ChatGPT a follow-up question…";
    else els.commentInput.placeholder = "Waiting for their answers…";
  }

  function closeStream() {
    if (state.source) {
      state.source.close();
      state.source = null;
    }
  }

  function openStream(sid) {
    var source = new EventSource("/api/stream/" + sid);
    state.source = source;
    source.onmessage = function (e) {
      var data;
      try { data = JSON.parse(e.data); } catch (err) { return; }
      handleEvent(data);
    };
    source.onerror = function () {
      // EventSource auto-reconnects; only treat as fatal if we're not done.
      if (!els.resetBtn.classList.contains("hidden")) return;
      setStatus("Connection interrupted…");
    };
  }

  /* ---------- form submit ---------- */

  els.form.addEventListener("submit", function (e) {
    e.preventDefault();
    els.setupError.classList.add("hidden");
    els.startBtn.disabled = true;
    els.startBtn.textContent = "Starting…";

    // In managed-keys mode the key/validate inputs aren't rendered.
    var claudeKeyEl = document.getElementById("claude_key");
    var chatgptKeyEl = document.getElementById("chatgpt_key");
    var validateEl = document.getElementById("validate");
    var payload = {
      topic: document.getElementById("topic").value,
      claude_key: claudeKeyEl ? claudeKeyEl.value : "",
      chatgpt_key: chatgptKeyEl ? chatgptKeyEl.value : "",
      claude_model: document.getElementById("claude_model").value,
      chatgpt_model: document.getElementById("chatgpt_model").value,
      starter: document.getElementById("starter").value,
      max_rounds: parseInt(document.getElementById("max_rounds").value, 10) || 6,
      validate: validateEl ? validateEl.checked : false,
    };

    fetch("/api/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok) {
          throw new Error(res.j.error || "Failed to start.");
        }
        state.sessionId = res.j.session_id;
        state.pinned = true;
        state.ended = false;
        els.topicDisplay.textContent = res.j.topic;
        els.setup.classList.add("hidden");
        els.historyPanel.classList.add("hidden");
        els.conversation.classList.remove("hidden");
        els.thread.innerHTML = "";
        els.resetBtn.classList.add("hidden");
        els.stopBtn.classList.remove("hidden");
        els.commentForm.classList.add("hidden");
        els.jumpBtn.classList.add("hidden");
        setStatus("Starting…");
        openStream(state.sessionId);
      })
      .catch(function (err) {
        els.setupError.textContent = err.message;
        els.setupError.classList.remove("hidden");
      })
      .finally(function () {
        els.startBtn.disabled = false;
        els.startBtn.textContent = "Start debate";
      });
  });

  els.stopBtn.addEventListener("click", function () {
    if (!state.sessionId) return;
    setStatus("Stopping…");
    fetch("/api/stop/" + state.sessionId, { method: "POST" });
  });

  /* ---------- follow-up question to both AIs ---------- */

  els.commentForm.addEventListener("submit", function (e) {
    e.preventDefault();
    var question = els.commentInput.value.trim();
    if (!question || !state.sessionId) return;

    setCommentBusy(true);
    els.commentInput.value = "";
    // Jump down so the user sees their question and the incoming answers.
    state.pinned = true;
    els.jumpBtn.classList.add("hidden");

    fetch("/api/comment/" + state.sessionId, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: question }),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok) {
          banner("err", "⚠ " + (res.j.error || "Could not send question."));
          setCommentBusy(false);
        }
        // Success: answers stream in over SSE; comment_done re-enables input.
      })
      .catch(function () {
        banner("err", "⚠ Could not send question.");
        setCommentBusy(false);
      });
  });

  els.resetBtn.addEventListener("click", showSetup);

  function showSetup() {
    if (state.sessionId) {
      fetch("/api/end/" + state.sessionId, { method: "POST" });
    }
    closeStream();
    state.sessionId = null;
    els.conversation.classList.add("hidden");
    els.setup.classList.remove("hidden");
    els.historyPanel.classList.remove("hidden");
    loadHistory();
  }

  /* ---------- history ---------- */

  function statusLabel(reason, status) {
    if (reason === "consensus") return "consensus";
    if (reason === "max_rounds") return "round limit";
    if (reason === "stopped") return "stopped";
    return status || "running";
  }

  function statusClass(status) {
    return "status-badge status-" + (status || "running");
  }

  function fmtDate(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  }

  function loadHistory() {
    fetch("/api/history")
      .then(function (r) { return r.json(); })
      .then(function (j) { renderHistory(j.conversations || []); })
      .catch(function () { /* history is non-critical */ });
  }

  function renderHistory(items) {
    els.historyList.innerHTML = "";
    els.historyEmpty.classList.toggle("hidden", items.length > 0);
    items.forEach(function (it) {
      var li = document.createElement("li");
      li.className = "history-item";

      var main = document.createElement("div");
      main.className = "h-main";
      var topic = document.createElement("div");
      topic.className = "h-topic";
      topic.textContent = it.topic;
      var meta = document.createElement("div");
      meta.className = "h-meta";
      meta.innerHTML = fmtDate(it.created_at) +
        '<span class="sep">•</span>' + (it.message_count || 0) + " messages" +
        '<span class="sep">•</span>' + it.claude_model + " vs " + it.chatgpt_model;
      main.appendChild(topic);
      main.appendChild(meta);

      var badge = document.createElement("span");
      badge.className = statusClass(it.status);
      badge.textContent = statusLabel(it.reason, it.status);

      var del = document.createElement("button");
      del.className = "h-delete";
      del.type = "button";
      del.textContent = "✕";
      del.title = "Delete";
      del.addEventListener("click", function (e) {
        e.stopPropagation();
        if (!window.confirm("Delete this debate from history?")) return;
        fetch("/api/history/" + it.id, { method: "DELETE" })
          .then(function () { loadHistory(); });
      });

      li.appendChild(main);
      li.appendChild(badge);
      li.appendChild(del);
      li.addEventListener("click", function () { viewConversation(it.id); });
      els.historyList.appendChild(li);
    });
  }

  function renderSavedMessage(m) {
    startMessage(m.speaker, m.display || m.speaker, m.speaker);
    endMessage(typeof m.consensus === "boolean" ? m.consensus : null, m.text);
  }

  function viewConversation(id) {
    fetch("/api/history/" + id)
      .then(function (r) {
        if (!r.ok) throw new Error("Not found");
        return r.json();
      })
      .then(function (conv) {
        state.sessionId = null;        // read-only: no live session
        state.pinned = true;
        els.setup.classList.add("hidden");
        els.historyPanel.classList.add("hidden");
        els.conversation.classList.remove("hidden");
        els.topicDisplay.textContent = conv.topic;
        els.thread.innerHTML = "";
        els.stopBtn.classList.add("hidden");
        els.commentForm.classList.add("hidden");
        els.jumpBtn.classList.add("hidden");
        els.resetBtn.classList.remove("hidden");
        setStatus("Saved · " + statusLabel(conv.reason, conv.status));
        (conv.messages || []).forEach(renderSavedMessage);
        scrollDown(true);
      })
      .catch(function () {
        banner("err", "⚠ Could not load that conversation.");
      });
  }

  els.historyRefresh.addEventListener("click", loadHistory);

  var logoutBtn = document.getElementById("logout-btn");
  if (logoutBtn) {
    logoutBtn.addEventListener("click", function () {
      fetch("/api/logout", { method: "POST" }).finally(function () {
        window.location.href = "/login";
      });
    });
  }

  /* ---------- voice input (Web Speech API) ---------- */

  function initVoice() {
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    var buttons = document.querySelectorAll("[data-mic]");
    if (!SR) {
      // Browser doesn't support speech recognition (e.g. Firefox) — hide mics.
      for (var i = 0; i < buttons.length; i++) buttons[i].classList.add("unsupported");
      return;
    }
    for (var j = 0; j < buttons.length; j++) {
      var btn = buttons[j];
      var target = document.getElementById(btn.getAttribute("data-mic"));
      if (target) wireMic(btn, target, SR);
    }
  }

  function wireMic(btn, target, SR) {
    var rec = null;
    var listening = false;
    var base = "";

    function setListening(on) {
      listening = on;
      btn.classList.toggle("listening", on);
      btn.title = on ? "Listening… click to stop" : "Dictate";
    }

    btn.addEventListener("click", function () {
      if (listening) { if (rec) { try { rec.stop(); } catch (e) {} } return; }

      rec = new SR();
      rec.lang = navigator.language || "en-US";
      rec.interimResults = true;   // live partial transcript
      rec.continuous = false;      // auto-stops after a natural pause

      // Append dictation to whatever is already in the field.
      base = target.value ? target.value.replace(/\s+$/, "") + " " : "";

      rec.onstart = function () { setListening(true); };
      rec.onend = function () {
        setListening(false);
        rec = null;
        target.focus();            // so the user can immediately press Enter
      };
      rec.onerror = function (e) {
        setListening(false);
        if (e && e.error === "not-allowed") {
          alert("Microphone access was blocked. Allow it in your browser’s site settings to use voice input.");
        }
      };
      rec.onresult = function (e) {
        var interim = "", finalText = "";
        for (var i = e.resultIndex; i < e.results.length; i++) {
          var t = e.results[i][0].transcript;
          if (e.results[i].isFinal) finalText += t;
          else interim += t;
        }
        target.value = base + finalText + interim;
        if (finalText) base += finalText;  // fold final in so it isn't re-shown
      };

      try { rec.start(); }
      catch (err) { setListening(false); }
    });
  }

  initVoice();

  /* ---------- model dropdowns (fetched live from the providers) ---------- */

  function optionValues(select) {
    var vals = [];
    for (var i = 0; i < select.options.length; i++) vals.push(select.options[i].value);
    return vals;
  }

  function loadModels(provider) {
    var select = document.getElementById(provider + "_model");
    var hint = document.getElementById(provider + "_model_hint");
    var btn = document.querySelector('[data-reload="' + provider + '"]');
    var keyEl = document.getElementById(provider + "_key");
    var apiKey = keyEl ? keyEl.value.trim() : "";

    hint.classList.remove("err");
    hint.textContent = "Loading models…";
    if (btn) btn.classList.add("spin");

    return fetch("/api/models", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: provider, api_key: apiKey }),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok) {
          hint.classList.add("err");
          hint.textContent = res.j.error || "Could not load models.";
          return;
        }
        // Remember the currently-selected value so we can keep it if possible.
        var prev = select.value || select.getAttribute("data-default");
        var preferred = res.j.default || prev;

        select.innerHTML = "";
        res.j.models.forEach(function (m) {
          var o = document.createElement("option");
          o.value = m.id;
          o.textContent = m.label || m.id;
          select.appendChild(o);
        });

        var vals = optionValues(select);
        if (vals.indexOf(preferred) !== -1) select.value = preferred;
        else if (vals.indexOf(prev) !== -1) select.value = prev;

        hint.textContent = res.j.models.length + " models available";
      })
      .catch(function () {
        hint.classList.add("err");
        hint.textContent = "Could not load models.";
      })
      .finally(function () {
        if (btn) btn.classList.remove("spin");
      });
  }

  // Reload buttons.
  var reloadBtns = document.querySelectorAll("[data-reload]");
  for (var i = 0; i < reloadBtns.length; i++) {
    (function (btn) {
      btn.addEventListener("click", function () {
        loadModels(btn.getAttribute("data-reload"));
      });
    })(reloadBtns[i]);
  }

  // When a key is typed/pasted into a field, auto-load that vendor's models.
  ["claude", "chatgpt"].forEach(function (provider) {
    var keyEl = document.getElementById(provider + "_key");
    if (!keyEl) return;
    keyEl.addEventListener("change", function () {
      if (keyEl.value.trim()) loadModels(provider);
    });
  });

  // On load, auto-populate any vendor whose key is already available from .env.
  var loaded = window.KEYS_LOADED || {};
  ["claude", "chatgpt"].forEach(function (provider) {
    if (loaded[provider]) loadModels(provider);
  });

  // Initial load.
  loadHistory();
})();
