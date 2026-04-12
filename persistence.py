"""
persistence.py — Save/load conversation sessions and manage bookmarks.
Sessions are stored as JSON files in ./querymind_sessions/.
Bookmarks are stored in ./querymind_bookmarks.json.

FIX #4: save_session appends timestamp if filename already exists (no silent overwrite).
FIX #13: add/remove_bookmark accept current in-memory list to avoid redundant disk reads.
FIX #17: auto_save_session saves to a fixed _autosave slot after every message.
"""

from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent import AgentResponse

SESSIONS_DIR   = Path("./querymind_sessions")
BOOKMARKS_FILE = Path("./querymind_bookmarks.json")
AUTOSAVE_NAME  = "_autosave"


def _ensure() -> None:
    SESSIONS_DIR.mkdir(exist_ok=True)


# ── Serialization helpers ───────────────────────────────────────────────────────

def _resp_to_dict(resp) -> dict:
    return {
        "sql":                 getattr(resp, "sql", ""),
        "explanation":         getattr(resp, "explanation", ""),
        "modification_type":   getattr(resp, "modification_type", "new"),
        "changes_made":        getattr(resp, "changes_made", []),
        "context_notes":       getattr(resp, "context_notes", ""),
        "error":               getattr(resp, "error", None),
        "is_ambiguous":        getattr(resp, "is_ambiguous", False),
        "clarifying_question": getattr(resp, "clarifying_question", ""),
        "confidence":          getattr(resp, "confidence", 1.0),
        "uses_cte":            getattr(resp, "uses_cte", False),
        "zero_rows_hint":      getattr(resp, "zero_rows_hint", ""),
    }


def _dict_to_resp(d: dict):
    from agent import AgentResponse
    return AgentResponse(
        sql                  = d.get("sql", ""),
        explanation          = d.get("explanation", ""),
        modification_type    = d.get("modification_type", "new"),
        changes_made         = d.get("changes_made", []),
        context_notes        = d.get("context_notes", ""),
        error                = d.get("error"),
        is_ambiguous         = d.get("is_ambiguous", False),
        clarifying_question  = d.get("clarifying_question", ""),
        confidence           = d.get("confidence", 1.0),
        uses_cte             = d.get("uses_cte", False),
        zero_rows_hint       = d.get("zero_rows_hint", ""),
    )


def _serialize_turns(chat_log: list) -> list:
    turns = []
    for entry in chat_log:
        turns.append({
            "user":        entry.get("user", ""),
            "response":    _resp_to_dict(entry.get("response")),
            "db_error":    entry.get("db_error"),
            "elapsed":     entry.get("elapsed", 0),
            "current_sql": entry.get("current_sql", ""),
            "bookmarked":  entry.get("bookmarked", False),
            "healed":      entry.get("healed", False),
        })
    return turns


# ── Sessions ────────────────────────────────────────────────────────────────────

def save_session(chat_log: list, name: str | None = None) -> str:
    """
    Serialize chat_log to JSON. Returns the saved session slug.
    FIX #4: if a file with the same slug already exists, appends a timestamp
    suffix instead of silently overwriting it.
    """
    _ensure()
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = name.strip() if name and name.strip() else f"session_{ts}"
    slug = "".join(c if c.isalnum() or c in "_-" else "_" for c in slug)

    # Avoid silent overwrite — append timestamp if file exists
    path = SESSIONS_DIR / f"{slug}.json"
    if path.exists():
        slug = f"{slug}_{ts}"
        path = SESSIONS_DIR / f"{slug}.json"

    with open(path, "w") as f:
        json.dump({"name": slug, "created": ts, "turns": _serialize_turns(chat_log)},
                  f, indent=2)
    return slug


def auto_save_session(chat_log: list) -> None:
    """
    FIX #17: Silently overwrite the _autosave slot after every message.
    This preserves work if the browser closes unexpectedly.
    """
    if not chat_log:
        return
    _ensure()
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = SESSIONS_DIR / f"{AUTOSAVE_NAME}.json"
    try:
        with open(path, "w") as f:
            json.dump({"name": AUTOSAVE_NAME, "created": ts,
                       "turns": _serialize_turns(chat_log)}, f, indent=2)
    except Exception:
        pass


def list_sessions() -> list[dict]:
    """Return session metadata sorted newest-first. Excludes the autosave slot."""
    _ensure()
    out = []
    for p in sorted(SESSIONS_DIR.glob("*.json"),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        if p.stem == AUTOSAVE_NAME:
            continue
        try:
            with open(p) as f:
                d = json.load(f)
            out.append({
                "filename": p.stem,
                "name":     d.get("name", p.stem),
                "created":  d.get("created", ""),
                "turns":    len(d.get("turns", [])),
            })
        except Exception:
            pass
    return out


def load_session(filename: str) -> list[dict]:
    """Load and deserialize a session. DataFrames will be None (re-run to restore)."""
    path = SESSIONS_DIR / f"{filename}.json"
    if not path.exists():
        return []
    with open(path) as f:
        data = json.load(f)
    log = []
    for t in data.get("turns", []):
        log.append({
            "user":        t.get("user", ""),
            "response":    _dict_to_resp(t.get("response", {})),
            "df":          None,
            "db_error":    t.get("db_error"),
            "elapsed":     t.get("elapsed", 0),
            "current_sql": t.get("current_sql", ""),
            "bookmarked":  t.get("bookmarked", False),
            "healed":      t.get("healed", False),
        })
    return log


def load_autosave() -> list[dict]:
    """Load the autosave slot if it exists."""
    return load_session(AUTOSAVE_NAME)


def has_autosave() -> bool:
    return (SESSIONS_DIR / f"{AUTOSAVE_NAME}.json").exists()


def delete_session(filename: str) -> bool:
    path = SESSIONS_DIR / f"{filename}.json"
    if path.exists():
        path.unlink()
        return True
    return False


# ── Bookmarks ───────────────────────────────────────────────────────────────────

def load_bookmarks() -> list[dict]:
    if not BOOKMARKS_FILE.exists():
        return []
    try:
        with open(BOOKMARKS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save_bookmarks(bm: list[dict]) -> None:
    with open(BOOKMARKS_FILE, "w") as f:
        json.dump(bm, f, indent=2)


def add_bookmark(question: str, sql: str, explanation: str,
                 current: list[dict] | None = None) -> list[dict]:
    """
    FIX #13: Accept current in-memory bookmark list to avoid a redundant disk read.
    Falls back to reading from disk if current is not provided.
    """
    bm = current if current is not None else load_bookmarks()
    if not any(b["sql"] == sql for b in bm):
        bm = [{
            "question":    question,
            "sql":         sql,
            "explanation": explanation,
            "created":     datetime.now().strftime("%Y-%m-%d %H:%M"),
        }] + bm
        _save_bookmarks(bm)
    return bm


def remove_bookmark(sql: str, current: list[dict] | None = None) -> list[dict]:
    """
    FIX #13: Accept current in-memory bookmark list to avoid a redundant disk read.
    Falls back to reading from disk if current is not provided.
    """
    source = current if current is not None else load_bookmarks()
    bm     = [b for b in source if b["sql"] != sql]
    _save_bookmarks(bm)
    return bm















# """
# persistence.py — Save/load conversation sessions and manage bookmarks.
# Sessions are stored as JSON files in ./querymind_sessions/.
# Bookmarks are stored in ./querymind_bookmarks.json.
# """

# from __future__ import annotations
# import json
# from datetime import datetime
# from pathlib import Path
# from typing import TYPE_CHECKING

# if TYPE_CHECKING:
#     from agent import AgentResponse

# SESSIONS_DIR    = Path("./querymind_sessions")
# BOOKMARKS_FILE  = Path("./querymind_bookmarks.json")


# def _ensure() -> None:
#     SESSIONS_DIR.mkdir(exist_ok=True)


# # ── Serialization helpers ───────────────────────────────────────────────────────

# def _resp_to_dict(resp) -> dict:
#     return {
#         "sql":                getattr(resp, "sql", ""),
#         "explanation":        getattr(resp, "explanation", ""),
#         "modification_type":  getattr(resp, "modification_type", "new"),
#         "changes_made":       getattr(resp, "changes_made", []),
#         "context_notes":      getattr(resp, "context_notes", ""),
#         "error":              getattr(resp, "error", None),
#         "is_ambiguous":       getattr(resp, "is_ambiguous", False),
#         "clarifying_question":getattr(resp, "clarifying_question", ""),
#         "confidence":         getattr(resp, "confidence", 1.0),
#         "uses_cte":           getattr(resp, "uses_cte", False),
#     }


# def _dict_to_resp(d: dict):
#     from agent import AgentResponse
#     return AgentResponse(
#         sql                  = d.get("sql", ""),
#         explanation          = d.get("explanation", ""),
#         modification_type    = d.get("modification_type", "new"),
#         changes_made         = d.get("changes_made", []),
#         context_notes        = d.get("context_notes", ""),
#         error                = d.get("error"),
#         is_ambiguous         = d.get("is_ambiguous", False),
#         clarifying_question  = d.get("clarifying_question", ""),
#         confidence           = d.get("confidence", 1.0),
#         uses_cte             = d.get("uses_cte", False),
#     )


# # ── Sessions ────────────────────────────────────────────────────────────────────

# def save_session(chat_log: list, name: str | None = None) -> str:
#     """Serialize chat_log to JSON. Returns the saved session slug."""
#     _ensure()
#     ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
#     slug = name.strip() if name and name.strip() else f"session_{ts}"
#     slug = "".join(c if c.isalnum() or c in "_-" else "_" for c in slug)
#     path = SESSIONS_DIR / f"{slug}.json"

#     turns = []
#     for entry in chat_log:
#         turns.append({
#             "user":        entry.get("user", ""),
#             "response":    _resp_to_dict(entry.get("response")),
#             "db_error":    entry.get("db_error"),
#             "elapsed":     entry.get("elapsed", 0),
#             "current_sql": entry.get("current_sql", ""),
#             "bookmarked":  entry.get("bookmarked", False),
#             "healed":      entry.get("healed", False),
#         })

#     with open(path, "w") as f:
#         json.dump({"name": slug, "created": ts, "turns": turns}, f, indent=2)
#     return slug


# def list_sessions() -> list[dict]:
#     """Return session metadata sorted newest-first."""
#     _ensure()
#     out = []
#     for p in sorted(SESSIONS_DIR.glob("*.json"),
#                     key=lambda x: x.stat().st_mtime, reverse=True):
#         try:
#             with open(p) as f:
#                 d = json.load(f)
#             out.append({
#                 "filename": p.stem,
#                 "name":     d.get("name", p.stem),
#                 "created":  d.get("created", ""),
#                 "turns":    len(d.get("turns", [])),
#             })
#         except Exception:
#             pass
#     return out


# def load_session(filename: str) -> list[dict]:
#     """Load and deserialize a session. DataFrames will be None (re-run to restore)."""
#     path = SESSIONS_DIR / f"{filename}.json"
#     if not path.exists():
#         return []
#     with open(path) as f:
#         data = json.load(f)
#     log = []
#     for t in data.get("turns", []):
#         log.append({
#             "user":        t.get("user", ""),
#             "response":    _dict_to_resp(t.get("response", {})),
#             "df":          None,        # DataFrames not serialized — re-run to restore
#             "db_error":    t.get("db_error"),
#             "elapsed":     t.get("elapsed", 0),
#             "current_sql": t.get("current_sql", ""),
#             "bookmarked":  t.get("bookmarked", False),
#             "healed":      t.get("healed", False),
#         })
#     return log


# def delete_session(filename: str) -> bool:
#     path = SESSIONS_DIR / f"{filename}.json"
#     if path.exists():
#         path.unlink()
#         return True
#     return False


# # ── Bookmarks ───────────────────────────────────────────────────────────────────

# def load_bookmarks() -> list[dict]:
#     if not BOOKMARKS_FILE.exists():
#         return []
#     try:
#         with open(BOOKMARKS_FILE) as f:
#             return json.load(f)
#     except Exception:
#         return []


# def _save_bookmarks(bm: list[dict]) -> None:
#     with open(BOOKMARKS_FILE, "w") as f:
#         json.dump(bm, f, indent=2)


# def add_bookmark(question: str, sql: str, explanation: str) -> list[dict]:
#     bm = load_bookmarks()
#     if not any(b["sql"] == sql for b in bm):
#         bm.insert(0, {
#             "question":    question,
#             "sql":         sql,
#             "explanation": explanation,
#             "created":     datetime.now().strftime("%Y-%m-%d %H:%M"),
#         })
#         _save_bookmarks(bm)
#     return bm


# def remove_bookmark(sql: str) -> list[dict]:
#     bm = [b for b in load_bookmarks() if b["sql"] != sql]
#     _save_bookmarks(bm)
#     return bm