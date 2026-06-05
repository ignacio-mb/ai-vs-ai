/* Analytics dashboard: global aggregates + per-debate deep dive.
 * Visualizations are dependency-free (CSS bars + inline SVG sparkline). */

(function () {
  "use strict";

  var NAMES = { claude: "Claude", chatgpt: "ChatGPT" };
  var ACCENT = { claude: "var(--claude)", chatgpt: "var(--chatgpt)" };

  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function pct(x) { return Math.round((x || 0) * 100) + "%"; }
  function fmtDate(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    return isNaN(d.getTime()) ? iso : d.toLocaleString(undefined,
      { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function outcomeLabel(o) {
    return ({ consensus: "consensus", max_rounds: "round limit", stopped: "stopped" }[o]) || o || "—";
  }

  /* ---- overview ---- */

  function card(label, value, sub) {
    return el("div", "card",
      '<div class="card-value">' + esc(value) + "</div>" +
      '<div class="card-label">' + esc(label) + "</div>" +
      (sub ? '<div class="card-sub">' + esc(sub) + "</div>" : ""));
  }

  function renderOverview(ov) {
    var cards = document.getElementById("overview-cards");
    cards.innerHTML = "";
    if (!ov || !ov.total_debates) {
      cards.appendChild(card("Debates", 0));
      document.getElementById("head-to-head").innerHTML = "";
      document.getElementById("archetypes").innerHTML = "";
      return;
    }
    cards.appendChild(card("Debates", ov.total_debates));
    cards.appendChild(card("Consensus rate", pct(ov.consensus_rate), ov.consensus_count + " reached agreement"));
    cards.appendChild(card("Avg AI turns", ov.avg_ai_turns));
    cards.appendChild(card("Avg turns to agree",
      ov.avg_turns_to_agreement == null ? "—" : ov.avg_turns_to_agreement));

    // head to head
    var h2h = document.getElementById("head-to-head");
    h2h.innerHTML = "";
    var hh = ov.head_to_head || {};
    var metrics = [
      ["held_ground", "Held their ground", false],
      ["concessions", "Concessions made", false],
      ["challenges", "Challenges raised", false],
      ["avg_words_per_debate", "Avg words / debate", false],
      ["avg_reading_ease", "Reading ease (0–100)", false],
      ["avg_sentiment", "Avg sentiment (−1…1)", true],
    ];
    metrics.forEach(function (m) {
      h2h.appendChild(compareRow(m[1], hh.claude ? hh.claude[m[0]] : 0,
        hh.chatgpt ? hh.chatgpt[m[0]] : 0, m[2]));
    });

    // archetypes
    var arch = document.getElementById("archetypes");
    arch.innerHTML = "";
    (ov.archetypes || []).forEach(function (a) {
      arch.appendChild(el("span", "chip", esc(a.label) + ' <b>' + a.count + "</b>"));
    });
  }

  // A two-sided comparison bar (Claude vs ChatGPT) for one metric.
  function compareRow(label, a, b, signed) {
    a = a || 0; b = b || 0;
    var max = Math.max(Math.abs(a), Math.abs(b), signed ? 1 : 0.0001);
    var row = el("div", "cmp");
    row.appendChild(el("div", "cmp-label", esc(label)));
    var bars = el("div", "cmp-bars");
    var aw = Math.round(Math.abs(a) / max * 100);
    var bw = Math.round(Math.abs(b) / max * 100);
    bars.appendChild(el("div", "cmp-side cmp-a",
      '<span class="cmp-num">' + esc(a) + '</span>' +
      '<span class="cmp-fill" style="width:' + aw + '%;background:' + ACCENT.claude + '"></span>'));
    bars.appendChild(el("div", "cmp-side cmp-b",
      '<span class="cmp-fill" style="width:' + bw + '%;background:' + ACCENT.chatgpt + '"></span>' +
      '<span class="cmp-num">' + esc(b) + '</span>'));
    row.appendChild(bars);
    return row;
  }

  /* ---- per-debate index + detail ---- */

  function renderIndex(debates) {
    var list = document.getElementById("debate-index");
    var empty = document.getElementById("analytics-empty");
    list.innerHTML = "";
    empty.classList.toggle("hidden", debates.length > 0);
    debates.forEach(function (d) {
      var li = el("li", "history-item");
      var main = el("div", "h-main",
        '<div class="h-topic">' + esc(d.topic) + "</div>" +
        '<div class="h-meta">' + fmtDate(d.created_at) +
        '<span class="sep">•</span>' + (d.ai_turns || 0) + " turns" +
        '<span class="sep">•</span>' + (d.total_words || 0) + " words" +
        (d.held_ground && d.held_ground !== "mutual"
          ? '<span class="sep">•</span>' + esc(NAMES[d.held_ground] || d.held_ground) + " held ground"
          : "") + "</div>");
      var badge = el("span", "status-badge status-" + (d.outcome || "running"),
        esc(d.archetype || outcomeLabel(d.outcome)));
      li.appendChild(main);
      li.appendChild(badge);
      li.addEventListener("click", function () { loadDetail(d.id); });
      list.appendChild(li);
    });
  }

  function statRow(label, a, b) {
    return '<tr><td class="m-label">' + esc(label) + "</td>" +
      '<td class="m-a">' + esc(a) + "</td>" +
      '<td class="m-b">' + esc(b) + "</td></tr>";
  }

  function sparkline(curve) {
    // word-count per AI turn, colored by speaker; agreement markers as dots.
    if (!curve || !curve.length) return "";
    var W = 320, H = 60, pad = 6;
    var max = Math.max.apply(null, curve.map(function (c) { return c.words || 0; })) || 1;
    var step = curve.length > 1 ? (W - 2 * pad) / (curve.length - 1) : 0;
    var pts = curve.map(function (c, i) {
      var x = pad + i * step;
      var y = H - pad - (c.words / max) * (H - 2 * pad);
      return { x: x, y: y, c: c };
    });
    var path = pts.map(function (p, i) { return (i ? "L" : "M") + p.x.toFixed(1) + " " + p.y.toFixed(1); }).join(" ");
    var dots = pts.map(function (p) {
      var color = p.c.consensus === true ? "var(--chatgpt)" : (p.c.consensus === false ? "var(--muted)" : "var(--border)");
      return '<circle cx="' + p.x.toFixed(1) + '" cy="' + p.y.toFixed(1) + '" r="3.2" fill="' + color + '"></circle>';
    }).join("");
    return '<svg viewBox="0 0 ' + W + " " + H + '" class="spark" preserveAspectRatio="none">' +
      '<path d="' + path + '" fill="none" stroke="var(--accent)" stroke-width="1.6"></path>' +
      dots + "</svg>";
  }

  function termChips(terms) {
    if (!terms || !terms.length) return '<span class="muted-note">—</span>';
    return terms.map(function (t) {
      var label = t.term ? (t.term + (t.count ? " ·" + t.count : "")) : t;
      return '<span class="chip sm">' + esc(label) + "</span>";
    }).join("");
  }

  function loadDetail(id) {
    var box = document.getElementById("debate-detail");
    box.classList.remove("hidden");
    box.innerHTML = '<p class="muted-note">Loading analysis…</p>';
    box.scrollIntoView({ behavior: "smooth", block: "start" });
    fetch("/api/analytics/" + id)
      .then(function (r) { return r.json(); })
      .then(function (s) { renderDetail(box, s); })
      .catch(function () { box.innerHTML = '<p class="error">Could not load analysis.</p>'; });
  }

  function renderDetail(box, s) {
    var cl = s.per_speaker.claude, cg = s.per_speaker.chatgpt;
    var held = s.held_ground === "mutual" ? "Both moved equally"
      : (s.held_ground ? (NAMES[s.held_ground] || s.held_ground) + " held their ground" : "—");

    var rows = [
      statRow("Messages", cl.messages, cg.messages),
      statRow("Words", cl.words, cg.words),
      statRow("Avg words / message", cl.avg_words_per_message, cg.avg_words_per_message),
      statRow("Avg sentence length", cl.avg_sentence_length, cg.avg_sentence_length),
      statRow("Questions asked", cl.questions_asked, cg.questions_asked),
      statRow("Lexical diversity", cl.lexical_diversity, cg.lexical_diversity),
      statRow("Reading ease (0–100)", cl.reading_ease, cg.reading_ease),
      statRow("Sentiment (−1…1)", cl.sentiment, cg.sentiment),
      statRow("Hedging phrases", cl.hedging, cg.hedging),
      statRow("Concessions", cl.concessions, cg.concessions),
      statRow("Challenges", cl.challenges, cg.challenges),
      statRow("Assertiveness", cl.assertiveness, cg.assertiveness),
    ].join("");

    box.innerHTML =
      '<div class="detail-head">' +
        '<h3>' + esc(s.topic) + "</h3>" +
        '<span class="status-badge status-' + esc(s.outcome) + '">' + esc(s.archetype) + "</span>" +
      "</div>" +
      '<div class="detail-callouts">' +
        callout("Outcome", outcomeLabel(s.outcome)) +
        callout("AI turns", s.ai_turns) +
        callout("Turns to agree", s.turns_to_agreement == null ? "—" : s.turns_to_agreement) +
        callout("Held ground", held) +
        callout("Vocabulary overlap", pct(s.vocabulary_overlap)) +
      "</div>" +

      '<div class="detail-grid">' +
        '<div class="detail-block">' +
          '<h4>Verbosity split</h4>' +
          splitBar(s.verbosity_share.claude, s.verbosity_share.chatgpt) +
        "</div>" +
        '<div class="detail-block">' +
          '<h4>Message length over time</h4>' +
          sparkline(s.consensus_curve) +
          '<div class="spark-legend"><span class="dot-c"></span>conceded ·' +
            '<span class="dot-m"></span>arguing · line = words</div>' +
        "</div>" +
      "</div>" +

      '<div class="detail-block">' +
        '<h4>Per-speaker metrics</h4>' +
        '<table class="metrics"><thead><tr><th></th>' +
          '<th class="m-a">' + esc(NAMES.claude) + "</th>" +
          '<th class="m-b">' + esc(NAMES.chatgpt) + "</th></tr></thead>" +
          "<tbody>" + rows + "</tbody></table>" +
      "</div>" +

      '<div class="detail-grid">' +
        '<div class="detail-block"><h4>Claude’s top terms</h4>' + termChips(cl.top_terms) + "</div>" +
        '<div class="detail-block"><h4>ChatGPT’s top terms</h4>' + termChips(cg.top_terms) + "</div>" +
      "</div>" +
      '<div class="detail-block"><h4>Shared vocabulary</h4>' + termChips(s.shared_terms) + "</div>";
  }

  function callout(label, value) {
    return '<div class="callout"><div class="co-label">' + esc(label) +
      '</div><div class="co-value">' + esc(value) + "</div></div>";
  }

  function splitBar(a, b) {
    return '<div class="split">' +
      '<div class="split-a" style="width:' + pct(a) + '" title="Claude ' + pct(a) + '"></div>' +
      '<div class="split-b" style="width:' + pct(b) + '" title="ChatGPT ' + pct(b) + '"></div>' +
      "</div><div class=\"split-legend\"><span>Claude " + pct(a) + "</span><span>ChatGPT " + pct(b) + "</span></div>";
  }

  /* ---- boot ---- */

  fetch("/api/analytics")
    .then(function (r) { return r.json(); })
    .then(function (data) {
      renderOverview(data.overview);
      renderIndex(data.debates || []);
    })
    .catch(function () {
      document.getElementById("analytics-empty").textContent = "Could not load analytics.";
      document.getElementById("analytics-empty").classList.remove("hidden");
    });

  var logoutBtn = document.getElementById("logout-btn");
  if (logoutBtn) {
    logoutBtn.addEventListener("click", function () {
      fetch("/api/logout", { method: "POST" }).finally(function () {
        window.location.href = "/login";
      });
    });
  }
})();
