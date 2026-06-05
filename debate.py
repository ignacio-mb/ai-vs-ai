"""The conversation engine.

Two AI participants take turns discussing a user-supplied topic. The engine is
deliberately vendor-agnostic: every participant is just a ``Participant`` with a
provider name, a model and an API key. Events (message start, streamed tokens,
message end, status, final conclusion, errors) are pushed to a callback so the
web layer can relay them to the browser in real time.
"""

import re
from typing import Callable, Dict, List, Optional

from llm import LLMError, stream


# A turn ends with a line like "CONSENSUS: yes" — the model's own judgement of
# whether the discussion has reached agreement. We parse it out, act on it, and
# hide it from the displayed text.
CONSENSUS_RE = re.compile(r"^\s*CONSENSUS\s*:\s*(yes|no)\b", re.IGNORECASE | re.MULTILINE)


class Participant:
    def __init__(self, key, name, provider, model, api_key, accent):
        self.key = key            # stable id, e.g. "claude"
        self.name = name          # display name, e.g. "Claude"
        self.provider = provider  # "anthropic" | "openai"
        self.model = model
        self.api_key = api_key
        self.accent = accent      # css accent class for the UI


def build_system_prompt(me: Participant, other: Participant, topic: str) -> str:
    return (
        "You are {me}, an AI taking part in a live, three-way conversation. The "
        "other voices are {other} (a different AI) and a human moderator who is "
        "watching but mostly silent.\n\n"
        "TOPIC UNDER DISCUSSION:\n\"{topic}\"\n\n"
        "HOW TO CONDUCT YOURSELF:\n"
        "- Think rigorously and argue from real reasoning and evidence, not "
        "vibes. Bring genuine substance.\n"
        "- Scrutinise {other}'s claims. If something is unsupported, fallacious, "
        "factually wrong, or overstated, say so plainly and explain why.\n"
        "- Hold yourself to the same standard. If you realise something you said "
        "was wrong or weak, or if {other} makes a point that genuinely lands, "
        "concede it openly. Intellectual honesty matters more than winning.\n"
        "- Where you believe you are right, try to actually persuade {other} with "
        "clear logic and evidence rather than just restating your view.\n"
        "- Aim to converge on a shared, well-reasoned conclusion. Identify common "
        "ground explicitly. It is acceptable to keep disagreeing on a genuinely "
        "contested point, but make the remaining disagreement precise.\n\n"
        "STYLE:\n"
        "- This is a spoken-style conversation, not an essay. Keep every message "
        "to AT MOST TWO SHORT paragraphs. Be concise and do not repeat points "
        "already made.\n"
        "- Address {other} directly. Do not narrate stage directions or roleplay.\n"
        "- Do not pretend to be {other} or speak on their behalf.\n\n"
        "AT THE END OF EVERY MESSAGE, on its own final line, output exactly one "
        "of these (this is a control signal, keep it terse):\n"
        "CONSENSUS: yes   (you believe you and {other} have substantively reached "
        "agreement, or nothing useful is left to resolve)\n"
        "CONSENSUS: no    (there is still a real disagreement worth pursuing)"
    ).format(me=me.name, other=other.name, topic=topic)


def build_messages(transcript: List[Dict], me: Participant) -> List[Dict[str, str]]:
    """Render the shared transcript from ``me``'s point of view.

    ``me``'s own turns become assistant messages; everything else (the opponent
    and the moderator kickoff) becomes user messages. Consecutive same-role
    messages are merged so the result strictly alternates, which keeps the
    Anthropic API happy.
    """
    raw = []
    for entry in transcript:
        role = "assistant" if entry["speaker"] == me.key else "user"
        speaker_label = entry.get("display", entry["speaker"])
        # Prefix opponent/moderator turns with who is talking so the model has
        # clear attribution inside a merged block.
        if role == "user":
            content = "[{}]: {}".format(speaker_label, entry["text"])
        else:
            content = entry["text"]
        raw.append({"role": role, "content": content})

    merged: List[Dict[str, str]] = []
    for msg in raw:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"] += "\n\n" + msg["content"]
        else:
            merged.append(dict(msg))

    # Anthropic requires the first message to be from the user.
    if merged and merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": "Begin the discussion."})
    if not merged:
        merged.append({"role": "user", "content": "Begin the discussion."})
    return merged


def parse_consensus(text: str) -> Optional[bool]:
    match = None
    for match in CONSENSUS_RE.finditer(text):
        pass  # keep only the last occurrence
    if match is None:
        return None
    return match.group(1).lower() == "yes"


def strip_consensus(text: str) -> str:
    return CONSENSUS_RE.sub("", text).strip()


class DebateEngine:
    def __init__(
        self,
        topic: str,
        participants: List[Participant],
        emit: Callable[[Dict], None],
        max_rounds: int = 6,
        max_tokens: int = 500,  # generous headroom for two short paragraphs
        should_stop: Optional[Callable[[], bool]] = None,
    ):
        self.topic = topic
        self.participants = participants
        self.emit = emit
        self.max_rounds = max_rounds
        self.max_tokens = max_tokens
        self.should_stop = should_stop or (lambda: False)
        self.transcript: List[Dict] = []
        # How the debate ended: "consensus" | "max_rounds" | "stopped" | None
        # (None means it never reached a clean end, e.g. an error).
        self.final_reason: Optional[str] = None

    def _add(self, speaker, display, text, consensus=None):
        self.transcript.append({
            "speaker": speaker,
            "display": display,
            "text": text,
            "consensus": consensus,
        })

    def run(self):
        """Drive the whole conversation. Blocking; meant to run in a thread."""
        try:
            self._run()
        except LLMError as exc:
            self.emit({"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - never kill the thread silently
            self.emit({"type": "error", "message": "Unexpected error: {}".format(exc)})

    def _run(self):
        a, b = self.participants
        kickoff = (
            "Welcome. Today's topic is: \"{topic}\". {a} will open, then {b} "
            "responds, and you'll go back and forth. Be sharp but honest, and try "
            "to reach a shared conclusion. {a}, please begin."
        ).format(topic=self.topic, a=a.name, b=b.name)
        self._add("moderator", "Moderator", kickoff)
        self.emit({
            "type": "message",
            "speaker": "moderator",
            "name": "Moderator",
            "accent": "moderator",
            "text": kickoff,
        })

        order = [a, b]
        last_consensus: Dict[str, Optional[bool]] = {a.key: None, b.key: None}
        turns_taken = 0

        for round_index in range(self.max_rounds):
            self.emit({
                "type": "status",
                "message": "Round {} of {}".format(round_index + 1, self.max_rounds),
                "round": round_index + 1,
                "max_rounds": self.max_rounds,
            })
            for speaker in order:
                if self.should_stop():
                    self.final_reason = "stopped"
                    self.emit({"type": "status", "message": "Stopped by user."})
                    self.emit({"type": "done", "reason": "stopped"})
                    return
                other = b if speaker is a else a
                last_consensus[speaker.key] = self._take_turn(speaker, other)
                turns_taken += 1

                # Check for agreement after EVERY turn (not just per round), so
                # the debate ends the moment they actually converge — but only
                # once both have spoken at least once.
                if turns_taken >= 2 and self._agreement_reached(
                    a, b, last_consensus, speaker
                ):
                    self.emit({"type": "status", "message": "Agreement reached."})
                    self._conclude(reason="consensus")
                    return

        self.emit({"type": "status", "message": "Reached the conversation limit."})
        self._conclude(reason="max_rounds")

    def _agreement_reached(self, a, b, last_consensus, current_speaker) -> bool:
        """Decide whether the two AIs have substantively agreed.

        Two signals are combined:
        1. Fast path — both sides' most recent self-reported ``CONSENSUS`` flag
           is an explicit "yes". Cheap, no extra API call.
        2. Backstop — a neutral judge reads the latest exchange and rules on
           whether they have actually converged. This catches the common case
           where a model simply forgets to emit the ``CONSENSUS`` line while
           plainly agreeing (which is exactly how debates used to drag on).

        If the speaker who just talked explicitly said they are still debating,
        we trust that and skip the (costly) judge call entirely.
        """
        if last_consensus[current_speaker.key] is False:
            return False  # this speaker says there's still a real disagreement

        if last_consensus[a.key] and last_consensus[b.key]:
            return True  # both explicitly signalled consensus

        # The marker is missing or one-sided; verify with the neutral judge.
        return self._judge_agreement(a, b)

    def _judge_agreement(self, a, b) -> bool:
        """Silent post-processing classifier: have the two latest turns agreed?"""
        latest: Dict[str, str] = {}
        for entry in reversed(self.transcript):
            if entry["speaker"] in (a.key, b.key) and entry["speaker"] not in latest:
                latest[entry["speaker"]] = entry["text"]
            if len(latest) == 2:
                break
        if len(latest) < 2:
            return False

        judge = sorted(self.participants, key=lambda p: p.key)[0]  # stable choice
        system = (
            "You are a strict, silent judge monitoring a debate between two AIs "
            "about: \"{topic}\". Decide whether they have SUBSTANTIVELY REACHED "
            "AGREEMENT: they now endorse the same overall conclusion and no "
            "material point is still being actively contested. If they are simply "
            "restating or echoing a conclusion they both already accept, that "
            "counts as agreement — answer YES. Only answer NO if there is a real, "
            "unresolved disagreement still being argued. Reply with exactly one "
            "word: YES or NO."
        ).format(topic=self.topic)
        prompt = (
            "{a} most recently said:\n\"{ta}\"\n\n"
            "{b} most recently said:\n\"{tb}\"\n\n"
            "Have they substantively reached agreement? Answer YES or NO."
        ).format(a=a.name, ta=latest[a.key], b=b.name, tb=latest[b.key])

        try:
            out = stream(
                judge.provider,
                api_key=judge.api_key,
                model=judge.model,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=5,
                on_token=lambda _chunk: None,  # silent: nothing goes to the UI
            )
        except LLMError:
            return False  # never let a judge failure derail the debate
        return out.strip().upper().startswith("Y")

    def answer_user(self, question: str):
        """Handle a follow-up question from the human, addressed to both AIs.

        Runs after the debate has concluded. The question is appended to the
        shared transcript and each participant answers it in turn (in the
        original speaking order), so both address the human directly.
        """
        try:
            self._answer_user(question)
        except LLMError as exc:
            self.emit({"type": "error", "message": str(exc)})
            self.emit({"type": "comment_done"})
        except Exception as exc:  # noqa: BLE001
            self.emit({"type": "error", "message": "Unexpected error: {}".format(exc)})
            self.emit({"type": "comment_done"})

    def _answer_user(self, question: str):
        self._add("user", "You", question)
        self.emit({
            "type": "message",
            "speaker": "user",
            "name": "You",
            "accent": "user",
            "text": question,
        })
        self.emit({"type": "status", "message": "Both AIs are answering your question..."})

        a, b = self.participants
        for speaker in (a, b):
            if self.should_stop():
                break
            other = b if speaker is a else a
            self._take_turn(speaker, other, question=question)

        self.emit({"type": "status", "message": "Ready for another question."})
        self.emit({"type": "comment_done"})

    def _take_turn(self, speaker, other, question=None) -> Optional[bool]:
        self.emit({
            "type": "message_start",
            "speaker": speaker.key,
            "name": speaker.name,
            "accent": speaker.accent,
        })
        system = build_system_prompt(speaker, other, self.topic)
        if question:
            # A human just asked both AIs something directly; prioritise it.
            system += (
                "\n\nTHE HUMAN MODERATOR HAS JUST ASKED YOU AND {other} A DIRECT "
                "QUESTION:\n\"{q}\"\nAnswer the human's question first and directly, "
                "in your own voice. You may still react to {other}, but the "
                "human's question takes priority. Keep the two-paragraph limit."
            ).format(other=other.name, q=question)
        messages = build_messages(self.transcript, speaker)

        def on_token(chunk: str):
            self.emit({"type": "token", "speaker": speaker.key, "text": chunk})

        raw_text = stream(
            speaker.provider,
            api_key=speaker.api_key,
            model=speaker.model,
            system=system,
            messages=messages,
            max_tokens=self.max_tokens,
            on_token=on_token,
        )

        consensus = parse_consensus(raw_text)
        clean = strip_consensus(raw_text)
        self._add(speaker.key, speaker.name, clean, consensus=consensus)
        self.emit({
            "type": "message_end",
            "speaker": speaker.key,
            "name": speaker.name,
            "consensus": consensus,
            "text": clean,
        })
        return consensus

    def _conclude(self, reason: str):
        """Ask a neutral moderator to write the joint wrap-up.

        The author is chosen independently of who opened the debate (stable by
        participant key) so the synthesis never reflects the opener's vendor.
        The model is also told to stand outside the debate and adopt neither
        debater's voice or position.
        """
        self.final_reason = reason
        self.emit({"type": "status", "message": "Writing a closing synthesis..."})
        author = sorted(self.participants, key=lambda p: p.key)[0]
        names = sorted(p.name for p in self.participants)
        system = (
            "You are an impartial third-party moderator who did NOT take part in "
            "the debate. Two AIs, {a} and {b}, argued the topic: \"{topic}\". You "
            "are neither of them — write from your own neutral standpoint, not in "
            "the voice of either debater, and do not favour the side you may have "
            "argued. Read the full transcript and write a short, even-handed "
            "wrap-up that states: (1) what they agreed on, (2) any points that "
            "remain genuinely unresolved, and (3) the single most defensible "
            "overall conclusion, if one exists. Be honest if they did not fully "
            "agree. Take no side. No CONSENSUS line."
        ).format(a=names[0], b=names[1], topic=self.topic)

        transcript_text = "\n\n".join(
            "{}: {}".format(e["display"], e["text"]) for e in self.transcript
        )
        messages = [{"role": "user", "content": "Transcript:\n\n" + transcript_text +
                     "\n\nWrite the closing synthesis now."}]

        self.emit({
            "type": "message_start",
            "speaker": "moderator",
            "name": "Conclusion",
            "accent": "moderator",
        })

        def on_token(chunk: str):
            self.emit({"type": "token", "speaker": "moderator", "text": chunk})

        try:
            text = stream(
                author.provider,
                api_key=author.api_key,
                model=author.model,
                system=system,
                messages=messages,
                max_tokens=600,
                on_token=on_token,
            )
        except LLMError as exc:
            self.emit({"type": "error", "message": str(exc)})
            self.emit({"type": "done", "reason": reason})
            return

        self._add("moderator", "Conclusion", text)
        self.emit({
            "type": "message_end",
            "speaker": "moderator",
            "name": "Conclusion",
            "consensus": None,
            "text": text,
        })
        self.emit({"type": "done", "reason": reason})
