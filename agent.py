"""
agent.py — Enhanced Groq conversational SQL agent.

Features:
  • Ambiguity detection + clarifying questions
  • Confidence scoring
  • CTE / multi-hop detection
  • Self-healing SQL (auto-retry on DB error)
  • Conversation summarization (compresses history after N turns)
  • Rate limiting (max calls/minute)
  • Auto-retry on transient Groq API errors
  • zero_rows_hint field for empty result explanations
"""

from __future__ import annotations
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field

from groq import Groq

GROQ_MODEL          = "llama-3.3-70b-versatile"
MAX_HISTORY_TURNS   = 20    # compress history after this many turns
SUMMARY_KEEP_TURNS  = 5     # keep last N turns after compression
RATE_LIMIT_CALLS    = 25    # max Groq API calls per minute
RATE_LIMIT_WINDOW   = 60    # seconds
API_RETRY_ATTEMPTS  = 2     # retries on transient Groq errors
API_RETRY_DELAY     = 2.0   # seconds between retries


# ── Rate Limiter ────────────────────────────────────────────────────────────────

class RateLimiter:
    def __init__(self, max_calls=RATE_LIMIT_CALLS, window=RATE_LIMIT_WINDOW):
        self.max_calls = max_calls
        self.window    = window
        self._ts: deque = deque()

    def check(self) -> tuple[bool, float]:
        now = time.time()
        while self._ts and self._ts[0] < now - self.window:
            self._ts.popleft()
        if len(self._ts) >= self.max_calls:
            return False, self.window - (now - self._ts[0])
        self._ts.append(now)
        return True, 0.0

    @property
    def calls_used(self) -> int:
        now = time.time()
        return sum(1 for t in self._ts if t > now - self.window)


# ── Response dataclass ──────────────────────────────────────────────────────────

@dataclass
class AgentResponse:
    sql:                  str
    explanation:          str
    modification_type:    str          # new/additive/restrictive/transformative/expansive
    changes_made:         list[str]
    context_notes:        str
    is_ambiguous:         bool  = False
    clarifying_question:  str   = ""
    confidence:           float = 1.0
    uses_cte:             bool  = False
    zero_rows_hint:       str   = ""   # explanation when query returns 0 rows
    error:                str | None = None
    raw_json:             dict  = field(default_factory=dict)


# ── System prompt ───────────────────────────────────────────────────────────────

def build_system_prompt(schema: str) -> str:
    return f"""You are an expert SQL assistant with perfect conversational memory and advanced reasoning.
Translate natural language into SQLite-compatible SQL using the FULL conversation history.

━━━━━━━━━━━━━━━━━━━━━━
DATABASE SCHEMA
━━━━━━━━━━━━━━━━━━━━━━
{schema}

━━━━━━━━━━━━━━━━━━━━━━
CONVERSATION MEMORY
━━━━━━━━━━━━━━━━━━━━━━
1. Always read full history before generating SQL.
2. Resolve ALL pronouns: "it/that/this" → previous entity; "those customers" → filtered customers;
   "same data" → same metrics, different filter; "them/they" → last query's subjects.
3. For follow-ups, EVOLVE the previous SQL—do not start from scratch.

━━━━━━━━━━━━━━━━━━━━━━
AMBIGUITY
━━━━━━━━━━━━━━━━━━━━━━
If the question is too vague (e.g., "show me data", "what about sales?"), set is_ambiguous=true
and provide a specific clarifying_question. Still generate best-guess SQL.

━━━━━━━━━━━━━━━━━━━━━━
COMPLEX QUERIES
━━━━━━━━━━━━━━━━━━━━━━
Use CTEs (WITH clauses) for multi-step logic. Set uses_cte=true when you do.

━━━━━━━━━━━━━━━━━━━━━━
MODIFICATION TYPES
━━━━━━━━━━━━━━━━━━━━━━
new | additive | restrictive | transformative | expansive

━━━━━━━━━━━━━━━━━━━━━━
CONFIDENCE (0.0–1.0)
━━━━━━━━━━━━━━━━━━━━━━
Low (<0.7) when: ambiguous intent, unclear schema mapping, unusual aggregation.

━━━━━━━━━━━━━━━━━━━━━━
ZERO ROWS HINT
━━━━━━━━━━━━━━━━━━━━━━
If your query might return 0 rows (e.g. filter on a parent category, date range mismatch,
very specific condition), populate zero_rows_hint with a brief explanation of why results
may be empty and what the user could try instead. Leave empty if results are expected.

━━━━━━━━━━━━━━━━━━━━━━
OUTPUT — VALID JSON ONLY
━━━━━━━━━━━━━━━━━━━━━━
{{
  "sql": "<complete SQLite query>",
  "explanation": "<1-2 sentence plain English>",
  "modification_type": "<new|additive|restrictive|transformative|expansive>",
  "changes_made": ["<change 1>"],
  "context_notes": "<how pronouns/references resolved>",
  "is_ambiguous": false,
  "clarifying_question": "",
  "confidence": 0.95,
  "uses_cte": false,
  "zero_rows_hint": ""
}}

SQL RULES: SQLite only · strftime('%Y','%m') for dates · alias all aggregates
· table aliases in JOINs · LIMIT 100 unless user asks for all · never SELECT *
· use CTEs for complex multi-step logic.

CATEGORY HIERARCHY RULE: Electronics, Clothing, Sports, Home & Garden are TOP-LEVEL
parent categories. Products link ONLY to sub-categories (Laptops, Smartphones, Audio,
Men's Clothing, Women's Clothing, Furniture, Gardening, Fitness, Outdoor Sports).
NEVER filter WHERE c.name = 'Electronics' with a single join — always double-join:
  JOIN categories c ON p.category_id = c.category_id
  JOIN categories parent ON c.parent_id = parent.category_id
  WHERE parent.name = 'Electronics' OR c.name = 'Electronics'

No markdown, backticks, or text outside the JSON object."""


SUMMARIZE_PROMPT = """Summarize this SQL conversation history into a compact paragraph.
Preserve: key entities, filters applied, tables used, query progression.
Be concise but complete. Plain text only, no JSON."""


# ── Agent ───────────────────────────────────────────────────────────────────────

class ConversationalSQLAgent:

    def __init__(self, schema: str, api_key: str | None = None):
        self.schema            = schema
        self.client            = Groq(api_key=api_key or os.environ.get("GROQ_API_KEY"))
        self._history:         list[dict] = []
        self._summary:         str | None = None
        self.last_sql:         str | None = None
        self.turn_count:       int = 0
        self._compressions:    int = 0   # FIX #7: track compressions separately
        self.rate_limiter      = RateLimiter()

    # ── Public ──────────────────────────────────────────────────────────────────

    def chat(self, user_input: str) -> AgentResponse:
        ok, wait = self.rate_limiter.check()
        if not ok:
            return AgentResponse(
                sql="", explanation="", modification_type="new",
                changes_made=[], context_notes="",
                error=f"Rate limit reached — please wait {wait:.0f}s before the next query.",
            )

        self.turn_count += 1
        user_msg = self._enrich(user_input)
        messages = self._build_messages(user_msg)

        try:
            raw  = self._call_groq_with_retry(messages)
            data = json.loads(raw)

            resp = AgentResponse(
                sql                 = data.get("sql", "").strip(),
                explanation         = data.get("explanation", ""),
                modification_type   = data.get("modification_type", "new"),
                changes_made        = data.get("changes_made", []),
                context_notes       = data.get("context_notes", ""),
                is_ambiguous        = bool(data.get("is_ambiguous", False)),
                clarifying_question = data.get("clarifying_question", ""),
                confidence          = float(data.get("confidence", 1.0)),
                uses_cte            = bool(data.get("uses_cte", False)),
                zero_rows_hint      = data.get("zero_rows_hint", ""),
                raw_json            = data,
            )

            self._history.append({"role": "user",     "content": user_msg})
            self._history.append({"role": "assistant", "content": raw})
            self.last_sql = resp.sql

            # FIX #7: use len(history) to trigger compression, not turn_count
            # This prevents double-compression since turn_count never resets
            if len(self._history) >= MAX_HISTORY_TURNS * 2:
                self._compress_history()

            return resp

        except json.JSONDecodeError as exc:
            return AgentResponse(sql="", explanation="", modification_type="new",
                                 changes_made=[], context_notes="",
                                 error=f"JSON parse error: {exc}")
        except Exception as exc:
            return AgentResponse(sql="", explanation="", modification_type="new",
                                 changes_made=[], context_notes="", error=str(exc))

    def self_heal_sql(self, failed_sql: str, error_msg: str) -> AgentResponse:
        """Ask Groq to fix a broken SQL query."""
        ok, wait = self.rate_limiter.check()
        if not ok:
            return AgentResponse(sql=failed_sql, explanation="", modification_type="new",
                                 changes_made=[], context_notes="",
                                 error=f"Rate limit — wait {wait:.0f}s")

        prompt = f"""The following SQLite query produced an error. Fix it.

            FAILED SQL:
            {failed_sql}

            ERROR:
            {error_msg}

            Return corrected SQL in the same JSON format.
            Set explanation to what was wrong and how you fixed it.
            Set changes_made to a list describing the fix.
            Set zero_rows_hint if the fixed query might still return 0 rows."""

        messages = self._build_messages(prompt)
        try:
            raw  = self._call_groq_with_retry(messages, temperature=0.05)
            data = json.loads(raw)
            return AgentResponse(
                sql               = data.get("sql", failed_sql).strip(),
                explanation       = f"[Auto-fixed] {data.get('explanation', '')}",
                modification_type = data.get("modification_type", "new"),
                changes_made      = data.get("changes_made", ["Fixed SQL syntax error"]),
                context_notes     = f"Original error: {error_msg}",
                zero_rows_hint    = data.get("zero_rows_hint", ""),
                raw_json          = data,
            )
        except Exception as exc:
            return AgentResponse(sql=failed_sql, explanation="", modification_type="new",
                                 changes_made=[], context_notes="", error=str(exc))

    def reset(self) -> None:
        self._history      = []
        self._summary      = None
        self.last_sql      = None
        self.turn_count    = 0
        self._compressions = 0

    def update_schema(self, new_schema: str) -> None:
        self.schema = new_schema

    @property
    def calls_used(self) -> int:
        return self.rate_limiter.calls_used

    @property
    def history_summary(self) -> list[dict]:
        out = []
        for msg in self._history:
            if msg["role"] == "user":
                c = msg["content"]
                if "\n\n[USER QUESTION]" in c:
                    c = c.split("\n\n[USER QUESTION]")[-1].strip()
                out.append({"role": "user", "content": c})
            elif msg["role"] == "assistant":
                try:
                    d = json.loads(msg["content"])
                    out.append({"role": "assistant", "content": d.get("sql",""), "meta": d})
                except Exception:
                    out.append({"role": "assistant", "content": msg["content"]})
        return out

    # ── Private ─────────────────────────────────────────────────────────────────

    def _call_groq(self, messages: list, temperature: float = 0.1) -> str:
        c = self.client.chat.completions.create(
            model=GROQ_MODEL, messages=messages,
            temperature=temperature,
            max_tokens=2048,                        # FIX #3: was 1024, too low for CTEs
            response_format={"type": "json_object"},
        )
        return c.choices[0].message.content

    def _call_groq_with_retry(self, messages: list, temperature: float = 0.1) -> str:
        """FIX #16: Wrap _call_groq with retry logic for transient API errors."""
        last_exc = None
        for attempt in range(API_RETRY_ATTEMPTS + 1):
            try:
                return self._call_groq(messages, temperature)
            except Exception as exc:
                last_exc = exc
                err_str  = str(exc).lower()
                # Only retry on transient errors (timeout, 503, rate limit from Groq)
                is_transient = any(k in err_str for k in
                                   ["timeout", "503", "502", "connection", "overloaded"])
                if is_transient and attempt < API_RETRY_ATTEMPTS:
                    time.sleep(API_RETRY_DELAY)
                    continue
                break
        raise last_exc

    def _build_messages(self, user_msg: str) -> list:
        msgs = [{"role": "system", "content": build_system_prompt(self.schema)}]
        if self._summary:
            msgs.append({"role": "user", "content":
                         f"[EARLIER CONVERSATION SUMMARY]\n{self._summary}"})
            msgs.append({"role": "assistant", "content":
                         '{"sql":"","explanation":"Understood context.","modification_type":"new",'
                         '"changes_made":[],"context_notes":"","is_ambiguous":false,'
                         '"clarifying_question":"","confidence":1.0,"uses_cte":false,'
                         '"zero_rows_hint":""}'})
        msgs.extend(self._history)
        msgs.append({"role": "user", "content": user_msg})
        return msgs

    def _enrich(self, user_input: str) -> str:
        parts = []
        if self.last_sql:
            parts.append(f"[CONTEXT — Previous SQL]\n{self.last_sql}")
        parts.append(f"[USER QUESTION]\n{user_input}")
        return "\n\n".join(parts)

    def _compress_history(self) -> None:
        """FIX #7: Compress based on history length, not turn_count."""
        keep      = SUMMARY_KEEP_TURNS * 2
        if len(self._history) <= keep:
            return
        old       = self._history[:-keep]
        keep_msgs = self._history[-keep:]
        text      = []
        for m in old:
            role = m["role"]; c = m["content"]
            if role == "assistant":
                try:
                    d = json.loads(c)
                    c = f"SQL: {d.get('sql','')}\nExplanation: {d.get('explanation','')}"
                except Exception:
                    pass
            text.append(f"{role.upper()}: {c[:300]}")
        try:
            comp = self.client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "system", "content": SUMMARIZE_PROMPT},
                          {"role": "user",   "content": "\n\n".join(text)}],
                temperature=0.1, max_tokens=350,
            )
            new_sum = comp.choices[0].message.content
            self._summary = ((self._summary + "\n\n" + new_sum)
                             if self._summary else new_sum)
            self._compressions += 1
        except Exception:
            pass
        self._history = keep_msgs


# ── Modification badge metadata ─────────────────────────────────────────────────

MODIFICATION_META: dict[str, dict] = {
    "new":            {"label": "New Query",     "color": "#6366f1", "icon": "✦"},
    "additive":       {"label": "Additive",       "color": "#10b981", "icon": "+"},
    "restrictive":    {"label": "Restrictive",    "color": "#f59e0b", "icon": "⌀"},
    "transformative": {"label": "Transformative", "color": "#3b82f6", "icon": "⟲"},
    "expansive":      {"label": "Expansive",      "color": "#8b5cf6", "icon": "⊕"},
}










# """
# agent.py — Enhanced Groq conversational SQL agent.

# New features:
#   • Ambiguity detection + clarifying questions
#   • Confidence scoring
#   • CTE / multi-hop detection
#   • Self-healing SQL (auto-retry on DB error)
#   • Conversation summarization (compresses history after N turns)
#   • Rate limiting (max calls/minute)
# """

# from __future__ import annotations
# import json
# import os
# import time
# from collections import deque
# from dataclasses import dataclass, field

# from groq import Groq

# GROQ_MODEL         = "llama-3.3-70b-versatile"
# MAX_HISTORY_TURNS  = 20    # compress history after this many turns
# SUMMARY_KEEP_TURNS = 5     # keep last N turns after compression
# RATE_LIMIT_CALLS   = 25    # max Groq API calls per minute
# RATE_LIMIT_WINDOW  = 60    # seconds


# # ── Rate Limiter ────────────────────────────────────────────────────────────────

# class RateLimiter:
#     def __init__(self, max_calls=RATE_LIMIT_CALLS, window=RATE_LIMIT_WINDOW):
#         self.max_calls = max_calls
#         self.window    = window
#         self._ts: deque = deque()

#     def check(self) -> tuple[bool, float]:
#         now = time.time()
#         while self._ts and self._ts[0] < now - self.window:
#             self._ts.popleft()
#         if len(self._ts) >= self.max_calls:
#             return False, self.window - (now - self._ts[0])
#         self._ts.append(now)
#         return True, 0.0

#     @property
#     def calls_used(self) -> int:
#         now = time.time()
#         return sum(1 for t in self._ts if t > now - self.window)


# # ── Response dataclass ──────────────────────────────────────────────────────────

# @dataclass
# class AgentResponse:
#     sql:                  str
#     explanation:          str
#     modification_type:    str          # new/additive/restrictive/transformative/expansive
#     changes_made:         list[str]
#     context_notes:        str
#     is_ambiguous:         bool  = False
#     clarifying_question:  str   = ""
#     confidence:           float = 1.0
#     uses_cte:             bool  = False
#     error:                str | None = None
#     raw_json:             dict  = field(default_factory=dict)


# # ── System prompt ───────────────────────────────────────────────────────────────

# def build_system_prompt(schema: str) -> str:
#     return f"""You are an expert SQL assistant with perfect conversational memory and advanced reasoning.
# Translate natural language into SQLite-compatible SQL using the FULL conversation history.

# ━━━━━━━━━━━━━━━━━━━━━━
# DATABASE SCHEMA
# ━━━━━━━━━━━━━━━━━━━━━━
# {schema}

# ━━━━━━━━━━━━━━━━━━━━━━
# CONVERSATION MEMORY
# ━━━━━━━━━━━━━━━━━━━━━━
# 1. Always read full history before generating SQL.
# 2. Resolve ALL pronouns: "it/that/this" → previous entity; "those customers" → filtered customers;
#    "same data" → same metrics, different filter; "them/they" → last query's subjects.
# 3. For follow-ups, EVOLVE the previous SQL—do not start from scratch.

# ━━━━━━━━━━━━━━━━━━━━━━
# AMBIGUITY
# ━━━━━━━━━━━━━━━━━━━━━━
# If the question is too vague (e.g., "show me data", "what about sales?"), set is_ambiguous=true
# and provide a specific clarifying_question. Still generate best-guess SQL.

# ━━━━━━━━━━━━━━━━━━━━━━
# COMPLEX QUERIES
# ━━━━━━━━━━━━━━━━━━━━━━
# Use CTEs (WITH clauses) for multi-step logic. Set uses_cte=true when you do.

# ━━━━━━━━━━━━━━━━━━━━━━
# MODIFICATION TYPES
# ━━━━━━━━━━━━━━━━━━━━━━
# new | additive | restrictive | transformative | expansive

# ━━━━━━━━━━━━━━━━━━━━━━
# CONFIDENCE (0.0–1.0)
# ━━━━━━━━━━━━━━━━━━━━━━
# Low (<0.7) when: ambiguous intent, unclear schema mapping, unusual aggregation.

# ━━━━━━━━━━━━━━━━━━━━━━
# OUTPUT — VALID JSON ONLY
# ━━━━━━━━━━━━━━━━━━━━━━
# {{
#   "sql": "<complete SQLite query>",
#   "explanation": "<1-2 sentence plain English>",
#   "modification_type": "<new|additive|restrictive|transformative|expansive>",
#   "changes_made": ["<change 1>"],
#   "context_notes": "<how pronouns/references resolved>",
#   "is_ambiguous": false,
#   "clarifying_question": "",
#   "confidence": 0.95,
#   "uses_cte": false
# }}

# SQL RULES: SQLite only · strftime('%Y'/%m') for dates · alias all aggregates
# · table aliases in JOINs · LIMIT 100 unless user asks for all · never SELECT *
# · use CTEs for complex multi-step logic.
# · CATEGORY HIERARCHY: Electronics/Clothing/Sports/Home & Garden are TOP-LEVEL 
#   parent categories. Products link ONLY to sub-categories (Laptops, Smartphones, 
#   Audio, etc). NEVER filter WHERE c.name = 'Electronics' with a single join —
#   always double-join categories to reach the parent:
#   JOIN categories c ON p.category_id = c.category_id
#   JOIN categories parent ON c.parent_id = parent.category_id
#   WHERE parent.name = 'Electronics' OR c.name = 'Electronics'
# No markdown, backticks, or text outside the JSON object."""


# SUMMARIZE_PROMPT = """Summarize this SQL conversation history into a compact paragraph.
# Preserve: key entities, filters applied, tables used, query progression.
# Be concise but complete. Plain text only, no JSON."""


# # ── Agent ───────────────────────────────────────────────────────────────────────

# class ConversationalSQLAgent:

#     def __init__(self, schema: str, api_key: str | None = None):
#         self.schema       = schema
#         self.client       = Groq(api_key=api_key or os.environ.get("GROQ_API_KEY"))
#         self._history:    list[dict] = []
#         self._summary:    str | None = None
#         self.last_sql:    str | None = None
#         self.turn_count:  int = 0
#         self.rate_limiter = RateLimiter()

#     # ── Public ──────────────────────────────────────────────────────────────────

#     def chat(self, user_input: str) -> AgentResponse:
#         ok, wait = self.rate_limiter.check()
#         if not ok:
#             return AgentResponse(
#                 sql="", explanation="", modification_type="new",
#                 changes_made=[], context_notes="",
#                 error=f"Rate limit reached — please wait {wait:.0f}s before the next query.",
#             )

#         self.turn_count += 1
#         user_msg = self._enrich(user_input)
#         messages = self._build_messages(user_msg)

#         try:
#             raw  = self._call_groq(messages)
#             data = json.loads(raw)

#             resp = AgentResponse(
#                 sql                 = data.get("sql", "").strip(),
#                 explanation         = data.get("explanation", ""),
#                 modification_type   = data.get("modification_type", "new"),
#                 changes_made        = data.get("changes_made", []),
#                 context_notes       = data.get("context_notes", ""),
#                 is_ambiguous        = bool(data.get("is_ambiguous", False)),
#                 clarifying_question = data.get("clarifying_question", ""),
#                 confidence          = float(data.get("confidence", 1.0)),
#                 uses_cte            = bool(data.get("uses_cte", False)),
#                 raw_json            = data,
#             )

#             self._history.append({"role": "user",      "content": user_msg})
#             self._history.append({"role": "assistant",  "content": raw})
#             self.last_sql = resp.sql

#             if self.turn_count % MAX_HISTORY_TURNS == 0:
#                 self._compress_history()

#             return resp

#         except json.JSONDecodeError as exc:
#             return AgentResponse(sql="", explanation="", modification_type="new",
#                                  changes_made=[], context_notes="",
#                                  error=f"JSON parse error: {exc}")
#         except Exception as exc:
#             return AgentResponse(sql="", explanation="", modification_type="new",
#                                  changes_made=[], context_notes="", error=str(exc))

#     def self_heal_sql(self, failed_sql: str, error_msg: str) -> AgentResponse:
#         """Ask Groq to fix a broken SQL query."""
#         ok, wait = self.rate_limiter.check()
#         if not ok:
#             return AgentResponse(sql=failed_sql, explanation="", modification_type="new",
#                                  changes_made=[], context_notes="",
#                                  error=f"Rate limit — wait {wait:.0f}s")

#         prompt = f"""The following SQLite query produced an error. Fix it.

# FAILED SQL:
# {failed_sql}

# ERROR:
# {error_msg}

# Return corrected SQL in the same JSON format.
# Set explanation to what was wrong and how you fixed it.
# Set changes_made to a list describing the fix."""

#         messages = self._build_messages(prompt)
#         try:
#             raw  = self._call_groq(messages, temperature=0.05)
#             data = json.loads(raw)
#             return AgentResponse(
#                 sql               = data.get("sql", failed_sql).strip(),
#                 explanation       = f"[Auto-fixed] {data.get('explanation', '')}",
#                 modification_type = data.get("modification_type", "new"),
#                 changes_made      = data.get("changes_made", ["Fixed SQL syntax error"]),
#                 context_notes     = f"Original error: {error_msg}",
#                 raw_json          = data,
#             )
#         except Exception as exc:
#             return AgentResponse(sql=failed_sql, explanation="", modification_type="new",
#                                  changes_made=[], context_notes="", error=str(exc))

#     def reset(self) -> None:
#         self._history  = []
#         self._summary  = None
#         self.last_sql  = None
#         self.turn_count = 0

#     def update_schema(self, new_schema: str) -> None:
#         self.schema = new_schema

#     @property
#     def calls_used(self) -> int:
#         return self.rate_limiter.calls_used

#     @property
#     def history_summary(self) -> list[dict]:
#         out = []
#         for msg in self._history:
#             if msg["role"] == "user":
#                 c = msg["content"]
#                 if "\n\n[USER QUESTION]" in c:
#                     c = c.split("\n\n[USER QUESTION]")[-1].strip()
#                 out.append({"role": "user", "content": c})
#             elif msg["role"] == "assistant":
#                 try:
#                     d = json.loads(msg["content"])
#                     out.append({"role": "assistant", "content": d.get("sql",""), "meta": d})
#                 except Exception:
#                     out.append({"role": "assistant", "content": msg["content"]})
#         return out

#     # ── Private ─────────────────────────────────────────────────────────────────

#     def _call_groq(self, messages: list, temperature: float = 0.1) -> str:
#         c = self.client.chat.completions.create(
#             model=GROQ_MODEL, messages=messages,
#             temperature=temperature, max_tokens=1024,
#             response_format={"type": "json_object"},
#         )
#         return c.choices[0].message.content

#     def _build_messages(self, user_msg: str) -> list:
#         msgs = [{"role": "system", "content": build_system_prompt(self.schema)}]
#         if self._summary:
#             msgs.append({"role": "user", "content":
#                          f"[EARLIER CONVERSATION SUMMARY]\n{self._summary}"})
#             msgs.append({"role": "assistant", "content":
#                          '{"sql":"","explanation":"Understood context.","modification_type":"new",'
#                          '"changes_made":[],"context_notes":"","is_ambiguous":false,'
#                          '"clarifying_question":"","confidence":1.0,"uses_cte":false}'})
#         msgs.extend(self._history)
#         msgs.append({"role": "user", "content": user_msg})
#         return msgs

#     def _enrich(self, user_input: str) -> str:
#         parts = []
#         if self.last_sql:
#             parts.append(f"[CONTEXT — Previous SQL]\n{self.last_sql}")
#         parts.append(f"[USER QUESTION]\n{user_input}")
#         return "\n\n".join(parts)

#     def _compress_history(self) -> None:
#         keep = SUMMARY_KEEP_TURNS * 2
#         if len(self._history) <= keep:
#             return
#         old  = self._history[:-keep]
#         keep_msgs = self._history[-keep:]
#         text = []
#         for m in old:
#             role = m["role"]; c = m["content"]
#             if role == "assistant":
#                 try:
#                     d = json.loads(c)
#                     c = f"SQL: {d.get('sql','')}\nExplanation: {d.get('explanation','')}"
#                 except Exception:
#                     pass
#             text.append(f"{role.upper()}: {c[:300]}")
#         try:
#             comp = self.client.chat.completions.create(
#                 model=GROQ_MODEL,
#                 messages=[{"role":"system","content":SUMMARIZE_PROMPT},
#                           {"role":"user","content":"\n\n".join(text)}],
#                 temperature=0.1, max_tokens=350,
#             )
#             new_sum = comp.choices[0].message.content
#             self._summary = ((self._summary + "\n\n" + new_sum)
#                              if self._summary else new_sum)
#         except Exception:
#             pass
#         self._history = keep_msgs


# # ── Modification badge metadata ─────────────────────────────────────────────────

# MODIFICATION_META: dict[str, dict] = {
#     "new":            {"label": "New Query",     "color": "#6366f1", "icon": "✦"},
#     "additive":       {"label": "Additive",       "color": "#10b981", "icon": "+"},
#     "restrictive":    {"label": "Restrictive",    "color": "#f59e0b", "icon": "⌀"},
#     "transformative": {"label": "Transformative", "color": "#3b82f6", "icon": "⟲"},
#     "expansive":      {"label": "Expansive",      "color": "#8b5cf6", "icon": "⊕"},
# }