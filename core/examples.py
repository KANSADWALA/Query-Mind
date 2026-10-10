"""Learning from feedback: answers the user marks 👍 become worked examples for similar future questions,
and answers marked 👎 (with an optional note) become "avoid this" hints.

Stored per database in data/feedback.json:
  {"examples": [{"source_key", "question", "sql", "good": true|false, "note", "ts", "turn_id"}]}
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR

FEEDBACK_FILE = DATA_DIR / "feedback.json"
STOP = {"the", "a", "an", "of", "in", "on", "for", "to", "by", "and", "or", "is", "are", "was", "were", "what", "which",
        "who", "how", "show", "me", "give", "list", "with", "from", "each", "per", "all", "do", "does", "did", "that"}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = set()
    for w in words:
        if w in STOP or len(w) < 2:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 3 else w)
    return out


class ExampleStore:
    def __init__(self, path: Path = FEEDBACK_FILE):
        self.path = path

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("examples", [])
        except (json.JSONDecodeError, OSError):
            return []

    def _write(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"examples": items}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def record(self, source_key: str, question: str, sql: str, good: bool, note: str = "", turn_id: str = "") -> None:
        """Save or update feedback for one answer (one entry per turn; re-rating replaces it)."""
        items = [e for e in self._read() if not (turn_id and e.get("turn_id") == turn_id)]
        if good:  # avoid duplicate positives for the same question+sql
            items = [e for e in items if not (e.get("good") and e.get("source_key") == source_key
                                              and e.get("question", "").strip().lower() == question.strip().lower())]
        items.append({"source_key": source_key, "question": question.strip(), "sql": sql.strip(), "good": good,
                      "note": note.strip(), "ts": datetime.now().isoformat(timespec="seconds"), "turn_id": turn_id})
        self._write(items[-500:])

    def remove(self, turn_id: str) -> None:
        self._write([e for e in self._read() if e.get("turn_id") != turn_id])

    def for_source(self, source_key: str) -> list[dict]:
        return [e for e in self._read() if e.get("source_key") == source_key]

    def similar(self, source_key: str, question: str, k_good: int = 3, k_bad: int = 2, min_score: float = 0.2) -> tuple[list, list]:
        """Most similar 👍 and 👎 examples for this database, by word overlap (Jaccard)."""
        q = _tokens(question)
        if not q:
            return [], []
        scored = []
        for e in self.for_source(source_key):
            t = _tokens(e.get("question", ""))
            if t:
                score = len(q & t) / len(q | t)
                if score >= min_score:
                    scored.append((score, e))
        scored.sort(key=lambda x: -x[0])
        good = [e for _, e in scored if e.get("good")][:k_good]
        bad = [e for _, e in scored if not e.get("good")][:k_bad]
        return good, bad

    def stats(self, source_key: str) -> tuple[int, int]:
        items = self.for_source(source_key)
        return sum(1 for e in items if e.get("good")), sum(1 for e in items if not e.get("good"))


def examples_block(good: list[dict], bad: list[dict]) -> str:
    """Prompt section with the retrieved examples (empty string when there are none)."""
    if not good and not bad:
        return ""
    parts = ["\nLEARNED FROM USER FEEDBACK ON THIS DATABASE"]
    if good:
        parts.append("Verified answers the user confirmed were correct (follow their logic, table choices and filters):")
        for e in good:
            parts.append(f"Q: {e['question']}\nSQL: {e['sql']}")
    if bad:
        parts.append("Answers the user marked as WRONG (do not repeat these mistakes):")
        for e in bad:
            parts.append(f"Q: {e['question']}\nRejected SQL: {e['sql']}" + (f"\nUser's note: {e['note']}" if e.get("note") else ""))
    return "\n".join(parts) + "\n"
