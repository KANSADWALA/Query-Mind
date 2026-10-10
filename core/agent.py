"""The conversational SQL agent: plan → validate → execute → self-heal → summarise.

Also handles multi-hop reasoning (a chain of queries, optionally across several databases joined through an
in-memory "scratch" database), learning from 👍/👎 feedback, a running summary memory for long chats, and
streaming the final answer.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Callable

import pandas as pd

from .charts import suggest_chart
from .db import DatabaseManager, QueryResult
from .examples import ExampleStore, examples_block
from .llm import ChatModel, LLMError, parse_json
from .memory import Conversation, Turn
from .prompts import (
    FIX_SYSTEM, FIX_USER, MEMORY_SUMMARY_SYSTEM, MEMORY_SUMMARY_USER, MULTISTEP_SYSTEM, PLANNER_SYSTEM, STEP_RESULT,
    SUGGEST_SYSTEM, SUMMARY_STREAM_SYSTEM, SUMMARY_SYSTEM, SUMMARY_USER,
)
from .safety import clean_sql

DIALECT_LABELS = {"mysql": "MySQL", "mariadb": "MariaDB", "postgresql": "PostgreSQL", "sqlite": "SQLite", "mssql": "SQL Server"}
FOLLOW_MARK = "---FOLLOW-UPS---"
Status = Callable[[str], None]
Token = Callable[[str], None]


def _noop(_: str) -> None:
    pass


def _sql_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Make a result storable in SQLite (Decimals, dates and odd objects become numbers or text)."""
    out = df.copy()
    out.columns = [str(c) for c in out.columns]
    for c in out.columns:
        if out[c].dtype == object:
            num = pd.to_numeric(out[c], errors="coerce")
            out[c] = num if num.notna().sum() == out[c].notna().sum() else out[c].astype(str)
    return out


class SQLAgent:
    def __init__(self, db: DatabaseManager, llm: ChatModel, max_retries: int = 3, history_turns: int = 8,
                 row_limit: int = 500, max_steps: int = 5, examples: ExampleStore | None = None, source_key: str = "",
                 linked: dict[str, DatabaseManager] | None = None, main_alias: str = "main"):
        self.db = db
        self.llm = llm
        self.max_retries = max_retries
        self.history_turns = history_turns
        self.row_limit = row_limit
        self.max_steps = max_steps
        self.examples = examples
        self.source_key = source_key
        self.linked = {a: m for a, m in (linked or {}).items() if m is not db}
        self.main_alias = main_alias

    # ---------------------------------------------------------------- utils
    @staticmethod
    def label_for(db: DatabaseManager) -> str:
        return DIALECT_LABELS.get(db.dialect, db.dialect)

    @property
    def dialect_label(self) -> str:
        return self.label_for(self.db)

    def _json(self, messages: list[dict]) -> dict:
        return parse_json(self.llm.chat(messages, json_mode=True))

    def _context_text(self, conv: Conversation, question: str) -> str:
        recent = [t.content for t in conv.turns[-6:] if t.role == "user"]
        last = conv.last_assistant()
        return " ".join(recent + [question, last.sql if last else ""])

    def _schema_bundle(self, text: str) -> tuple[str, list[str]]:
        """Schema text for the main database, plus linked databases when cross-database mode is on."""
        tables = self.db.relevant_tables(text)
        main = self.db.schema_text(tables)
        if not self.linked:
            return main, tables
        parts = [f"DATABASE {self.main_alias} ({self.dialect_label}) - the currently connected database:\n{main}"]
        for alias, mgr in self.linked.items():
            sub = mgr.schema_text(mgr.relevant_tables(text, max_tables=8), with_samples=False)
            parts.append(f"LINKED DATABASE {alias} ({self.label_for(mgr)}):\n{sub}")
        return "\n\n".join(parts), tables

    # --------------------------------------------------------------- memory
    def update_memory(self, conv: Conversation) -> None:
        """Fold turns that left the recent window into the running summary (batched, best effort)."""
        pending = conv.turns_to_summarize(self.history_turns)
        if not pending:
            return
        exchanges = "\n".join(f"{'User' if t.role == 'user' else 'Assistant'}: {t.history_text()[:600]}" for t in pending)
        try:
            text = self.llm.chat([
                {"role": "system", "content": MEMORY_SUMMARY_SYSTEM},
                {"role": "user", "content": MEMORY_SUMMARY_USER.format(summary=conv.summary or "(empty)", exchanges=exchanges)},
            ])
        except LLMError:
            return
        if text and text.strip():
            conv.summary = text.strip()[:2000]
            conv.summary_upto += len(pending)

    # ------------------------------------------------------------------ main
    def respond(self, conv: Conversation, question: str, read_only: bool = True,
                on_status: Status | None = None, on_token: Token | None = None) -> Turn:
        """Handle one user message; appends the user turn and the assistant turn to *conv*."""
        status = on_status or _noop
        self.update_memory(conv)
        history = conv.llm_history(self.history_turns)
        conv.add(Turn(role="user", content=question))

        status("Reading the schema and your question…")
        schema, tables = self._schema_bundle(self._context_text(conv, question))
        good, bad = self.examples.similar(self.source_key, question) if self.examples and self.source_key else ([], [])
        system = PLANNER_SYSTEM.format(dialect=self.dialect_label, today=date.today().isoformat(), schema=schema)
        system += examples_block(good, bad)
        messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": question}]

        try:
            plan = self._json(messages)
        except LLMError as e:
            return conv.add(Turn(role="assistant", action="error", content=f"⚠️ {e}", error=str(e), question=question))

        action = (plan.get("action") or "answer").lower()
        if action == "clarify":
            return conv.add(Turn(
                role="assistant", action="clarify", question=question,
                content=plan.get("clarification") or "Could you clarify what you mean?",
                options=[str(o) for o in (plan.get("options") or [])][:4],
            ))
        if action == "multi_step":
            turn = self._multi_step(question, plan, history, tables, status, on_token)
            turn.used_examples = len(good)
            return conv.add(turn)
        if action != "query" or not plan.get("sql"):
            return conv.add(Turn(role="assistant", action="answer", question=question,
                                 content=plan.get("answer") or plan.get("explanation") or "…"))

        sql = clean_sql(plan["sql"])
        turn = Turn(
            role="assistant", action="query", sql=sql, question=question, used_examples=len(good),
            explanation=plan.get("explanation", ""), title=plan.get("title", ""),
            chart=plan.get("chart") if isinstance(plan.get("chart"), dict) else None,
        )

        report = self.db.check(sql, read_only)
        turn.warnings = report.warnings
        if not report.is_read:
            if report.allowed:  # write mode on: never auto-run writes, ask for confirmation
                turn.action = "pending"
                turn.content = "This statement will **modify the database**. Review it and confirm to run."
            else:
                turn.action = "answer"
                turn.error = report.reason
                turn.content = f"🔒 I generated the statement below but did not run it. {report.reason}"
            return conv.add(turn)

        status("Running the query…")
        result = self._execute_with_healing(question, turn, tables, status=status)
        self._finish(turn, question, result, status=status, on_token=on_token)
        return conv.add(turn)

    # ------------------------------------------------------------ internals
    def _execute_with_healing(self, question: str, turn: Turn, tables: list[str], mgr: DatabaseManager | None = None,
                              status: Status = _noop) -> QueryResult:
        mgr = mgr or self.db
        sql = turn.sql
        result = mgr.execute(sql, read_only=True, row_limit=self.row_limit)
        attempts = 1
        fix_schema = None
        while not result.ok and not result.error.startswith("Blocked") and attempts <= self.max_retries:
            status(f"Fixing an error in the SQL (attempt {attempts + 1})…")
            if fix_schema is None:  # give the fixer a wider view of the schema
                if mgr is self.db:
                    wide = mgr.relevant_tables(question + " " + sql, max_tables=25)
                    fix_schema = mgr.schema_text(list(dict.fromkeys(tables + wide)))
                else:
                    fix_schema = mgr.schema_text(mgr.relevant_tables(question + " " + sql, max_tables=25))
            try:
                fix = self._json([
                    {"role": "system", "content": FIX_SYSTEM.format(dialect=self.label_for(mgr), schema=fix_schema)},
                    {"role": "user", "content": FIX_USER.format(question=question, sql=sql, error=result.error)},
                ])
            except LLMError:
                break
            new_sql = clean_sql(fix.get("sql", ""))
            if not new_sql or new_sql == sql:
                break
            sql = new_sql
            report = mgr.check(sql, read_only=True)
            if not report.is_read:  # a "fix" must never turn a read into a write
                break
            result = mgr.execute(sql, read_only=True, row_limit=self.row_limit)
            attempts += 1
            if fix.get("explanation"):
                turn.explanation = (turn.explanation + f" (Auto-fixed: {fix['explanation']})").strip()
        turn.attempts = attempts
        turn.sql = result.sql or sql
        return result

    def _multi_step(self, question: str, plan: dict, history: list[dict], tables: list[str],
                    status: Status = _noop, on_token: Token | None = None) -> Turn:
        """Multi-hop reasoning: a chain of read-only queries, each able to use earlier results. Steps can target
        different databases; every result is copied into an in-memory 'scratch' SQLite DB as step_<n> for joining."""
        scratch = DatabaseManager("sqlite://")
        dbs: dict[str, DatabaseManager] = {self.main_alias: self.db, **self.linked, "scratch": scratch}
        aliases = ", ".join(dbs)

        hint = question + " " + " ".join(map(str, plan.get("plan") or []))
        main_schema = self.db.schema_text(list(dict.fromkeys(tables + self.db.relevant_tables(hint, max_tables=20))))
        if self.linked:
            parts = [f"DATABASE {self.main_alias} ({self.dialect_label}):\n{main_schema}"]
            for alias, mgr in self.linked.items():
                parts.append(f"DATABASE {alias} ({self.label_for(mgr)}):\n"
                             f"{mgr.schema_text(mgr.relevant_tables(hint, max_tables=12))}")
            schema = "\n\n".join(parts)
        else:
            schema = f"DATABASE {self.main_alias} ({self.dialect_label}):\n{main_schema}"
        system = MULTISTEP_SYSTEM.format(dialect=self.dialect_label, today=date.today().isoformat(),
                                         schema=schema, max_steps=self.max_steps, aliases=aliases)
        plan_text = "\n".join(f"- {p}" for p in (plan.get("plan") or []))
        messages = [{"role": "system", "content": system}, *history,
                    {"role": "user", "content": question + (f"\n\nSuggested plan:\n{plan_text}" if plan_text else "")}]
        turn = Turn(role="assistant", action="query", title=plan.get("title", ""), question=question)
        last_ok: QueryResult | None = None
        final: dict = {}

        for n in range(1, self.max_steps + 1):
            status(f"Planning step {n}…")
            try:
                out = self._json(messages)
            except LLMError as e:
                turn.error = str(e)
                break
            if (out.get("action") or "").lower() == "final" or not out.get("sql"):
                final = out
                break
            alias = out.get("database") if out.get("database") in dbs else self.main_alias
            mgr = dbs[alias]
            sql = clean_sql(out["sql"])
            purpose = str(out.get("purpose") or f"Step {n}")
            status(f"Step {n}: {purpose}" + (f" (on {alias})" if len(dbs) > 2 else ""))
            report = mgr.check(sql, read_only=True)
            res = (mgr.execute(sql, read_only=True, row_limit=self.row_limit) if report.is_read
                   else QueryResult(sql, report.kind, error="Only read queries are allowed in reasoning steps."))
            step = {"purpose": purpose, "database": alias, "sql": res.sql or sql, "row_count": res.rowcount,
                    "error": res.error or "", "columns": [], "rows": []}
            if res.ok and res.df is not None:
                last_ok = res
                step["columns"] = [str(c) for c in res.df.columns]
                step["rows"] = json.loads(res.df.head(5).astype(str).to_json(orient="records"))
                try:
                    _sql_safe(res.df).to_sql(f"step_{n}", scratch.engine, if_exists="replace", index=False)
                    scratch._schema = None
                except Exception:
                    pass
                observation = f"{res.rowcount} rows" + (" (truncated)" if res.truncated else "") + ":\n" + \
                    res.df.head(15).to_csv(index=False)[:3000]
            else:
                observation = f"ERROR: {res.error}"
            turn.steps.append(step)
            messages += [{"role": "assistant", "content": json.dumps(out)},
                         {"role": "user", "content": STEP_RESULT.format(n=n, purpose=purpose, database=alias,
                                                                         observation=observation)}]
        else:  # step budget used up: ask for the final answer
            messages.append({"role": "user", "content": 'Step limit reached. Reply with action "final" now.'})
            try:
                final = self._json(messages)
            except LLMError:
                final = {}

        turn.title = final.get("title") or turn.title
        turn.explanation = final.get("explanation", "")
        turn.chart = final.get("chart") if isinstance(final.get("chart"), dict) else None
        findings = "".join(
            f"Step {i} [{s['database']}]: {s['purpose']} -> " + (f"ERROR {s['error']}" if s["error"] else
                                                               f"{s['row_count']} rows, first rows: {json.dumps(s['rows'][:3])}") + "\n"
            for i, s in enumerate(turn.steps, 1))

        if final.get("sql"):
            alias = final.get("database") if final.get("database") in dbs else self.main_alias
            turn.sql = clean_sql(final["sql"])
            status("Running the final query…")
            result = self._execute_with_healing(question, turn, tables if alias == self.main_alias else [],
                                                mgr=dbs[alias], status=status)
            if alias != self.main_alias:
                turn.explanation = (turn.explanation + f" Final table computed on the {alias} database.").strip()
        elif last_ok is not None:
            turn.sql, turn.attempts, result = last_ok.sql, 1, last_ok
        else:
            turn.error = turn.error or "None of the reasoning steps returned data."
            turn.content = f"❌ I couldn't work this out step by step: {turn.error}"
            return turn
        self._finish(turn, question, result, findings=f"Findings from the reasoning steps:\n{findings}",
                     status=status, on_token=on_token)
        return turn

    def _finish(self, turn: Turn, question: str, result: QueryResult, findings: str = "",
                status: Status = _noop, on_token: Token | None = None) -> None:
        turn.elapsed_ms = result.elapsed_ms
        if not result.ok:
            turn.error = result.error
            turn.content = f"❌ The query failed after {turn.attempts} attempt(s): `{result.error}`"
            return
        turn.row_count = result.rowcount
        turn.truncated = result.truncated
        turn.set_df(result.df)
        df = result.df

        if df is None or df.empty:
            turn.content = "No rows matched this query. Try widening the filters."
            turn.chart = None
            return

        turn.chart = suggest_chart(df, turn.chart)
        status("Writing the answer…")
        user_msg = SUMMARY_USER.format(
            question=question, findings=findings, sql=turn.sql, rows=result.rowcount,
            truncated=" (truncated at the row limit)" if result.truncated else "",
            preview=df.head(25).to_csv(index=False)[:6000],
        )
        if on_token is not None and hasattr(self.llm, "stream"):
            try:
                self._stream_summary(turn, user_msg, on_token)
                return
            except LLMError:
                pass  # fall back to the JSON summary below
        try:
            summary = self._json([{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": user_msg}])
            turn.content = summary.get("answer") or turn.explanation
            turn.follow_ups = [str(q) for q in (summary.get("follow_ups") or [])][:3]
        except LLMError:
            turn.content = turn.explanation or f"Returned {result.rowcount} rows."

    def _stream_summary(self, turn: Turn, user_msg: str, on_token: Token) -> None:
        """Stream the answer text to the UI as it is generated; follow-up questions come after a marker line."""
        text = ""
        for piece in self.llm.stream([{"role": "system", "content": SUMMARY_STREAM_SYSTEM},
                                      {"role": "user", "content": user_msg}]):
            text += piece
            visible = text.split(FOLLOW_MARK)[0]
            cut = visible.rfind("---")
            if cut != -1 and len(visible) - cut < len(FOLLOW_MARK):  # don't flash a half-written marker
                visible = visible[:cut]
            on_token(visible)
        answer, _, tail = text.partition(FOLLOW_MARK)
        if not answer.strip():
            raise LLMError("empty streamed answer")
        turn.content = answer.strip()
        turn.follow_ups = [q.strip(" -•*0123456789.").strip() for q in tail.splitlines() if q.strip(" -•*")][:3]
        on_token(turn.content)

    # ------------------------------------------------------------ actions
    def suggest_questions(self) -> list[str]:
        """Starter questions generated from the connected schema (works for any database)."""
        tables = self.db.relevant_tables("", max_tables=15)
        try:
            data = self._json([
                {"role": "system", "content": SUGGEST_SYSTEM.format(schema=self.db.schema_text(tables, with_samples=False))},
                {"role": "user", "content": "Suggest the questions."},
            ])
            return [str(q) for q in data.get("questions", [])][:4]
        except LLMError:
            return []

    def confirm_pending(self, turn: Turn) -> Turn:
        """Run a write statement the user has explicitly approved."""
        result = self.db.execute(turn.sql, read_only=False, row_limit=self.row_limit)
        turn.elapsed_ms = result.elapsed_ms
        if result.ok:
            turn.action = "query"
            turn.row_count = result.rowcount
            turn.content = f"✅ Done — {result.rowcount} row(s) affected." if result.kind == "write" else "✅ Schema updated."
            if result.kind == "ddl":
                self.db.get_schema(refresh=True)
        else:
            turn.action = "query"
            turn.error = result.error
            turn.content = f"❌ Failed: `{result.error}`"
        return turn

    @staticmethod
    def cancel_pending(turn: Turn) -> Turn:
        turn.action = "answer"
        turn.content = "Cancelled — nothing was changed."
        return turn
