"""Conversation memory: turns, LLM-ready history, and on-disk session persistence."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

PERSIST_ROWS = 200  # rows of each result kept when a session is saved


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    content: str = ""
    action: str = ""  # query | clarify | answer | error | pending
    sql: str = ""
    explanation: str = ""
    title: str = ""
    chart: dict | None = None
    options: list[str] = field(default_factory=list)
    follow_ups: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: float = 0.0
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    attempts: int = 0
    steps: list[dict] = field(default_factory=list)  # multi-hop reasoning steps: purpose, sql, row_count, error, rows
    feedback: int | None = None      # 1 = 👍, 0 = 👎, None = not rated
    feedback_note: str = ""
    used_examples: int = 0           # how many learned examples informed this answer
    question: str = ""               # the user question this turn answered (for feedback)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    ts: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    # The dataframe is kept in memory only; `rows` is its persisted copy.
    _df: pd.DataFrame | None = field(default=None, repr=False, compare=False)

    @property
    def df(self) -> pd.DataFrame | None:
        if self._df is None and self.columns:
            self._df = pd.DataFrame(self.rows, columns=self.columns)
        return self._df

    def set_df(self, df: pd.DataFrame | None) -> None:
        self._df = df
        if df is None:
            self.rows, self.columns = [], []
            return
        self.columns = [str(c) for c in df.columns]
        head = df.head(PERSIST_ROWS).copy()
        for c in head.columns:  # make JSON-safe
            if not pd.api.types.is_numeric_dtype(head[c]) and not pd.api.types.is_bool_dtype(head[c]):
                head[c] = head[c].astype(str)
        self.rows = json.loads(head.to_json(orient="records", date_format="iso"))

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__ if k != "_df"}

    def history_text(self) -> str:
        """How this turn is shown to the LLM in later prompts."""
        if self.role == "user":
            return self.content
        parts = []
        if self.steps:
            parts.append("[Reasoning steps] " + "; ".join(
                f"{i}. {s.get('purpose', '')} ({'error' if s.get('error') else str(s.get('row_count', 0)) + ' rows'})"
                for i, s in enumerate(self.steps, 1)))
        if self.action == "query" and self.sql:
            parts.append(f"[Ran SQL]\n{self.sql}")
            if self.error:
                parts.append(f"[Error] {self.error}")
            else:
                parts.append(f"[Result] {self.row_count} rows; columns: {', '.join(self.columns)}")
                if self.rows:
                    preview = pd.DataFrame(self.rows[:5], columns=self.columns).to_csv(index=False).strip()
                    parts.append(f"[First rows]\n{preview}")
        elif self.action == "pending" and self.sql:
            parts.append(f"[Proposed SQL awaiting user confirmation]\n{self.sql}")
        if self.content:
            parts.append(self.content[:800])
        return "\n".join(parts) or "(no content)"


@dataclass
class Conversation:
    id: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4])
    title: str = "New chat"
    database: str = ""       # human-readable label of the database this chat is about
    source_key: str = ""     # stable id of that database, e.g. "dataset:chinook" or "connection:Sales DB"
    summary: str = ""        # running LLM summary of turns that fell out of the recent-history window
    summary_upto: int = 0    # number of turns already folded into `summary`
    turns: list[Turn] = field(default_factory=list)

    def add(self, turn: Turn) -> Turn:
        self.turns.append(turn)
        if self.title == "New chat" and turn.role == "user" and turn.content:
            self.title = turn.content[:60]
        return turn

    def last_assistant(self) -> Turn | None:
        return next((t for t in reversed(self.turns) if t.role == "assistant"), None)

    def _window_start(self, max_turns: int) -> int:
        return max(0, len(self.turns) - max_turns * 2)

    def turns_to_summarize(self, max_turns: int = 8, batch: int = 4) -> list[Turn]:
        """Older turns not yet in the running summary (returned in batches to keep LLM calls rare)."""
        pending = self.turns[self.summary_upto : self._window_start(max_turns)]
        return pending if len(pending) >= batch else []

    def llm_history(self, max_turns: int = 8) -> list[dict]:
        """Recent turns as chat messages, preceded by the running summary of older turns (or a digest)."""
        start = self._window_start(max_turns)
        recent = self.turns[start:]
        messages: list[dict] = []
        unsummarized = [t.content for t in self.turns[self.summary_upto if self.summary else 0 : start] if t.role == "user"]
        preface = []
        if self.summary:
            preface.append(f"Summary of our earlier conversation:\n{self.summary}")
        if unsummarized:
            preface.append("Earlier I also asked about: " + "; ".join(q[:80] for q in unsummarized[-15:]))
        if preface:
            messages.append({"role": "user", "content": "(" + "\n".join(preface) + ")"})
            messages.append({"role": "assistant", "content": "Noted, I'll keep that context in mind."})
        for t in recent:
            messages.append({"role": t.role, "content": t.history_text()})
        return messages

    # ---------------------------------------------------------- persistence
    def save(self, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{self.id}.json"
        data = {"id": self.id, "title": self.title, "database": self.database, "source_key": self.source_key,
                "summary": self.summary, "summary_upto": self.summary_upto,
                "updated": datetime.now().isoformat(timespec="seconds"), "turns": [t.to_dict() for t in self.turns]}
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "Conversation":
        data = json.loads(path.read_text(encoding="utf-8"))
        fields = set(Turn.__dataclass_fields__) - {"_df"}
        turns = [Turn(**{k: v for k, v in t.items() if k in fields}) for t in data.get("turns", [])]
        return cls(id=data["id"], title=data.get("title", "Chat"), database=data.get("database", ""),
                   source_key=data.get("source_key", ""), summary=data.get("summary", ""),
                   summary_upto=int(data.get("summary_upto", 0) or 0), turns=turns)

    @staticmethod
    def list_saved(folder: Path) -> list[dict]:
        """Saved chats, newest first: [{path, title, database, source_key, updated}]."""
        if not folder.exists():
            return []
        out = []
        for p in folder.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            out.append({"path": p, "title": d.get("title", p.stem), "database": d.get("database", ""),
                        "source_key": d.get("source_key", ""), "updated": d.get("updated") or p.stem})
        return sorted(out, key=lambda r: (r["updated"], r["path"].stat().st_mtime), reverse=True)

    @classmethod
    def latest_for(cls, folder: Path, source_key: str) -> "Conversation | None":
        """The most recent saved chat for a database, so switching back resumes where you left off."""
        if not source_key:
            return None
        for row in cls.list_saved(folder):
            if row["source_key"] == source_key:
                try:
                    return cls.load(row["path"])
                except Exception:
                    return None
        return None
