"""Post-debate analytics.

Given a stored conversation (topic, outcome, transcript), compute a rich set of
deterministic, data-science-flavoured metrics — verbosity, lexical diversity,
readability, sentiment, hedging/concession/challenge rates, a consensus
trajectory, vocabulary overlap, and a couple of creative composites (a debate
"archetype" and who "held their ground"). No extra API calls — it's all derived
from the text, so it's free, fast, and reproducible.

``analyze(conversation)`` returns the per-debate stats dict (stored as JSON).
``aggregate(list_of_stats)`` rolls many debates up into a cross-debate dashboard.
"""

import math
import re
from collections import Counter
from typing import Dict, List

KEYS = ("claude", "chatgpt")
NAMES = {"claude": "Claude", "chatgpt": "ChatGPT"}

_WORD_RE = re.compile(r"[a-zA-Z']+")
_SENT_RE = re.compile(r"[.!?]+")

STOPWORDS = set("""
a an the and or but if then else when while of to in on at by for with about as
into through during before after above below from up down out off over under
again further once here there all any both each few more most other some such no
nor not only own same so than too very can will just don't should now i me my we
our you your he she it its they them this that these those is are was were be been
being have has had do does did would could may might must shall it's i'm we're
they're that's there's what which who whom whose how where why because while also
their his her us let lets get got make made really quite even still much many
""".split())

# Tiny sentiment lexicon (enough for a relative signal, not absolute truth).
POS = set("""
good great strong excellent compelling clear valid fair agree agreed agreement
benefit benefits advantage advantages better best welcome positive reasonable
genuine right correct accurate sound robust solid wins helpful comprehensive
appealing attractive standout sweet ideal optimal accessible""".split())
NEG = set("""
bad weak wrong poor flawed unsupported overstated expensive barrier problem catch
issue stricter harder difficult costly drawback drawbacks disagree against
unfortunately limitation limitations risk risks fails fail tricky doubt unclear
prohibitive complicated complexity concern concerns""".split())

# Phrase signals (matched on lowercased text).
CONCESSION = [
    "you're right", "you are right", "fair point", "good point", "valid point",
    "i concede", "concede", "i agree", "i'll grant", "point taken", "i was wrong",
    "i should have", "i see your point", "you make a compelling",
    "you make some compelling", "i'll give you", "that's a good point",
    "you raise a good", "i take your point", "happy to concede",
]
CHALLENGE = [
    "however", "i disagree", "that's not", "i'd push back", "i push back",
    "the problem is", "the catch", "not quite", "on the contrary",
    "i'm not convinced", "the issue is", "i'd argue", "i would argue",
    "that overstates", "that's a stretch", "but the", "the flaw", "the downside",
    "a critical", "the key catch", "doesn't hold",
]
HEDGES = [
    "maybe", "perhaps", "might", "could", "i think", "arguably", "possibly",
    "seems", "somewhat", "relatively", "fairly", "probably", "i suppose",
    "to some extent", "in some ways", "i'd say",
]


def _words(text: str) -> List[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def _sentences(text: str) -> int:
    parts = [s for s in _SENT_RE.split(text or "") if s.strip()]
    return max(1, len(parts))


def _syllables(word: str) -> int:
    word = word.lower()
    groups = re.findall(r"[aeiouy]+", word)
    count = len(groups)
    if word.endswith("e") and count > 1:
        count -= 1  # silent-e heuristic
    return max(1, count)


def _phrase_hits(text_lower: str, phrases: List[str]) -> int:
    return sum(text_lower.count(p) for p in phrases)


def _round2(x):
    try:
        return round(float(x), 2)
    except (TypeError, ValueError):
        return 0.0


def _speaker_stats(msgs: List[Dict]) -> Dict:
    text = "\n".join(m.get("text", "") for m in msgs)
    low = text.lower()
    words = _words(text)
    n_words = len(words)
    n_msgs = len(msgs)
    sentences = sum(_sentences(m.get("text", "")) for m in msgs)
    content = [w for w in words if w not in STOPWORDS and len(w) > 2]
    unique = set(words)
    syllables = sum(_syllables(w) for w in words)
    questions = sum(m.get("text", "").count("?") for m in msgs)

    pos = sum(1 for w in words if w in POS)
    neg = sum(1 for w in words if w in NEG)
    sentiment = (pos - neg) / max(1, pos + neg)

    if n_words and sentences:
        flesch = 206.835 - 1.015 * (n_words / sentences) - 84.6 * (syllables / n_words)
    else:
        flesch = 0.0

    concessions = _phrase_hits(low, CONCESSION)
    challenges = _phrase_hits(low, CHALLENGE)
    hedges = _phrase_hits(low, HEDGES)

    top = Counter(content).most_common(8)

    return {
        "messages": n_msgs,
        "words": n_words,
        "chars": len(text),
        "avg_words_per_message": _round2(n_words / n_msgs) if n_msgs else 0,
        "avg_sentence_length": _round2(n_words / sentences) if sentences else 0,
        "questions_asked": questions,
        "lexical_diversity": _round2(len(unique) / n_words) if n_words else 0,
        "reading_ease": _round2(max(0.0, min(100.0, flesch))),
        "sentiment": _round2(sentiment),
        "hedging": hedges,
        "concessions": concessions,
        "challenges": challenges,
        # assertiveness > 0 => pushed more than conceded
        "assertiveness": challenges - concessions,
        "top_terms": [{"term": t, "count": c} for t, c in top],
        "_content_set": set(content),  # internal, stripped before return
    }


def analyze(conversation: Dict) -> Dict:
    messages = conversation.get("messages") or []
    reason = conversation.get("reason")
    status = conversation.get("status")
    models = {
        "claude": conversation.get("claude_model"),
        "chatgpt": conversation.get("chatgpt_model"),
    }

    ai_msgs = [m for m in messages if m.get("speaker") in KEYS]
    per = {}
    for key in KEYS:
        per[key] = _speaker_stats([m for m in ai_msgs if m.get("speaker") == key])

    # Vocabulary overlap (Jaccard of content words) — shared linguistic ground.
    set_a = per["claude"].pop("_content_set", set())
    set_b = per["chatgpt"].pop("_content_set", set())
    union = set_a | set_b
    overlap = len(set_a & set_b) / len(union) if union else 0.0
    shared_terms = [t for t, _ in Counter(
        [w for w in set_a & set_b]
    ).most_common()][:8] if (set_a & set_b) else []

    # Consensus trajectory + when each side first agreed.
    curve = []
    first_yes = {"claude": None, "chatgpt": None}
    turn_index = {"claude": 0, "chatgpt": 0}
    for m in ai_msgs:
        k = m["speaker"]
        turn_index[k] += 1
        c = m.get("consensus")
        curve.append({
            "speaker": k,
            "turn": turn_index[k],
            "consensus": c,
            "words": len(_words(m.get("text", ""))),
        })
        if c is True and first_yes[k] is None:
            first_yes[k] = turn_index[k]

    ai_turns = len(ai_msgs)
    rounds_used = math.ceil(ai_turns / 2) if ai_turns else 0
    turns_to_agreement = ai_turns if reason == "consensus" else None

    # Who "held their ground": the one who conceded less / agreed later moved less.
    def movement(k):
        m = per[k]["concessions"]
        if first_yes[k] is not None:
            m += first_yes[k]  # earlier 'yes' => moved sooner => higher movement
        return m

    mv = {k: movement(k) for k in KEYS}
    if not ai_turns:
        held = None
    elif mv["claude"] == mv["chatgpt"]:
        held = "mutual"
    else:
        held = min(KEYS, key=lambda k: mv[k])  # least movement held ground

    # Verbosity dominance.
    w_claude, w_chatgpt = per["claude"]["words"], per["chatgpt"]["words"]
    total_words = w_claude + w_chatgpt
    verbosity_share = {
        "claude": _round2(w_claude / total_words) if total_words else 0,
        "chatgpt": _round2(w_chatgpt / total_words) if total_words else 0,
    }

    archetype = _archetype(reason, ai_turns, verbosity_share)

    return {
        "topic": conversation.get("topic", ""),
        "models": models,
        "outcome": reason or status or "unknown",
        "status": status,
        "ai_turns": ai_turns,
        "rounds_used": rounds_used,
        "turns_to_agreement": turns_to_agreement,
        "reached_consensus": reason == "consensus",
        "total_messages": len(messages),
        "per_speaker": {k: per[k] for k in KEYS},
        "verbosity_share": verbosity_share,
        "total_words": total_words,
        "vocabulary_overlap": _round2(overlap),
        "shared_terms": shared_terms,
        "first_consensus_turn": first_yes,
        "consensus_curve": curve,
        "held_ground": held,
        "archetype": archetype,
    }


def _archetype(reason, ai_turns, verbosity_share) -> str:
    skew = abs(verbosity_share["claude"] - verbosity_share["chatgpt"])
    if reason == "consensus":
        if ai_turns <= 2:
            base = "Instant agreement"
        elif ai_turns <= 4:
            base = "Smooth convergence"
        else:
            base = "Hard-fought consensus"
    elif reason == "stopped":
        base = "Cut short"
    elif reason == "max_rounds":
        base = "Unresolved standoff"
    else:
        base = "Incomplete"
    if skew >= 0.35 and ai_turns:
        base += " · one-sided"
    return base


def aggregate(rows: List[Dict]) -> Dict:
    """Roll up many per-debate stats into a cross-debate dashboard."""
    stats = [r for r in rows if r]
    n = len(stats)
    if n == 0:
        return {"total_debates": 0}

    consensus = sum(1 for s in stats if s.get("reached_consensus"))
    ai_turns = [s.get("ai_turns", 0) for s in stats]
    tta = [s["turns_to_agreement"] for s in stats if s.get("turns_to_agreement")]

    head = {}
    for k in KEYS:
        words = [s["per_speaker"][k]["words"] for s in stats if s.get("per_speaker")]
        head[k] = {
            "name": NAMES[k],
            "total_words": sum(words),
            "avg_words_per_debate": _round2(sum(words) / n),
            "concessions": sum(s["per_speaker"][k]["concessions"] for s in stats if s.get("per_speaker")),
            "challenges": sum(s["per_speaker"][k]["challenges"] for s in stats if s.get("per_speaker")),
            "held_ground": sum(1 for s in stats if s.get("held_ground") == k),
            "avg_reading_ease": _round2(
                sum(s["per_speaker"][k]["reading_ease"] for s in stats if s.get("per_speaker")) / n
            ),
            "avg_sentiment": _round2(
                sum(s["per_speaker"][k]["sentiment"] for s in stats if s.get("per_speaker")) / n
            ),
        }

    archetypes = Counter(s.get("archetype", "Incomplete") for s in stats)

    return {
        "total_debates": n,
        "consensus_rate": _round2(consensus / n),
        "consensus_count": consensus,
        "avg_ai_turns": _round2(sum(ai_turns) / n),
        "avg_turns_to_agreement": _round2(sum(tta) / len(tta)) if tta else None,
        "head_to_head": head,
        "archetypes": [{"label": k, "count": v} for k, v in archetypes.most_common()],
    }
