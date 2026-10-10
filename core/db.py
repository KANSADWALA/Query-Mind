"""Database access: connections, schema introspection, relevant-table selection and safe execution."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine

from .safety import SafetyReport, add_limit, analyze, to_sqlglot_dialect


@dataclass
class Column:
    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False
    values: list | None = None              # distinct values, for low-cardinality text columns
    value_range: tuple | None = None        # (min, max), for date/time columns


@dataclass
class Table:
    name: str
    columns: list[Column]
    foreign_keys: list[tuple[str, str, str]] = field(default_factory=list)  # (column, ref_table, ref_column)
    row_count: int | None = None
    sample: pd.DataFrame | None = None
    is_view: bool = False


@dataclass
class QueryResult:
    sql: str
    kind: str
    df: pd.DataFrame | None = None
    rowcount: int = 0
    truncated: bool = False
    elapsed_ms: float = 0.0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None


def _cell_str(v, width: int = 40) -> str:
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<binary {len(v)} bytes>"
    return str(v)[:width]


def _clean_binary(df: pd.DataFrame) -> pd.DataFrame:
    """Replace BLOB values (images, files) with a short placeholder so results stay displayable."""
    for c in df.columns:
        if df[c].dtype == object and df[c].map(lambda v: isinstance(v, (bytes, bytearray, memoryview))).any():
            df[c] = df[c].map(lambda v: _cell_str(v) if isinstance(v, (bytes, bytearray, memoryview)) else v)
    return df


def _tokens(text: str) -> set[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)  # split camelCase / PascalCase (InvoiceLine)
    words = re.findall(r"[a-zA-Z]+", text.lower())
    out = set()
    for w in words:
        if len(w) < 3:
            continue
        out.add(w)
        if w.endswith("ies"):
            out.add(w[:-3] + "y")
        elif w.endswith("s"):
            out.add(w[:-1])
    return out


class DatabaseManager:
    def __init__(self, url: str, query_timeout_s: int = 30):
        self.url = url
        self.query_timeout_s = query_timeout_s
        kwargs: dict = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):  # in-memory scratch DB: one shared connection
            from sqlalchemy.pool import StaticPool

            kwargs = {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
        self.engine: Engine = create_engine(url, **kwargs)
        self._schema: dict[str, Table] | None = None

    # ------------------------------------------------------------------ info
    @property
    def dialect(self) -> str:
        return self.engine.dialect.name

    @property
    def sqlglot_dialect(self) -> str:
        return to_sqlglot_dialect(self.dialect)

    @property
    def display_name(self) -> str:
        u = self.engine.url
        if self.dialect == "sqlite":
            return f"SQLite · {Path(u.database).name if u.database else 'in-memory'}"
        return f"{self.dialect} · {u.host}/{u.database}"

    def test_connection(self) -> None:
        with self.engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")

    # ---------------------------------------------------------------- schema
    def get_schema(self, refresh: bool = False, sample_rows: int = 3) -> dict[str, Table]:
        if self._schema is not None and not refresh:
            return self._schema
        insp = inspect(self.engine)
        names = [(n, False) for n in insp.get_table_names()]
        try:
            names += [(n, True) for n in insp.get_view_names()]
        except Exception:
            pass

        schema: dict[str, Table] = {}
        for name, is_view in names:
            try:
                pk = set(insp.get_pk_constraint(name).get("constrained_columns") or [])
            except Exception:
                pk = set()
            cols = [
                Column(c["name"], str(c["type"]), c.get("nullable", True), c["name"] in pk)
                for c in insp.get_columns(name)
            ]
            fks = []
            try:
                for fk in insp.get_foreign_keys(name):
                    for col, ref in zip(fk["constrained_columns"], fk["referred_columns"]):
                        fks.append((col, fk["referred_table"], ref))
            except Exception:
                pass
            schema[name] = Table(name, cols, fks, is_view=is_view)

        self._fill_stats(schema, sample_rows, many=len(schema) > 60)
        self._schema = schema
        return schema

    def _quote(self, name: str) -> str:
        return self.engine.dialect.identifier_preparer.quote(name)

    def _fill_stats(self, schema: dict[str, Table], sample_rows: int, many: bool) -> None:
        with self.engine.connect() as conn:
            for t in schema.values():
                q = self._quote(t.name)
                if not many:
                    try:
                        t.row_count = conn.exec_driver_sql(f"SELECT COUNT(*) FROM {q}").scalar()
                    except Exception:
                        t.row_count = None
                if sample_rows:
                    try:
                        res = conn.exec_driver_sql(f"SELECT * FROM {q} LIMIT {int(sample_rows)}")
                        t.sample = pd.DataFrame(res.fetchall(), columns=list(res.keys()))
                    except Exception:
                        t.sample = None
            if not many:
                self._profile_columns(conn, schema)

    def _profile_columns(self, conn, schema: dict[str, Table], budget: int = 200) -> None:
        """Collect date ranges and categorical values: big accuracy boost for filters like status = 'Shipped'."""
        for t in schema.values():
            if t.is_view or (t.row_count or 0) > 2_000_000:
                continue
            q = self._quote(t.name)
            for c in t.columns:
                if budget <= 0:
                    return
                typ, name = c.type.upper(), c.name.lower()
                qc = self._quote(c.name)
                try:
                    if any(k in typ for k in ("DATE", "TIME")) or name.endswith(("date", "_at")) or name == "date":
                        budget -= 1
                        lo, hi = conn.exec_driver_sql(f"SELECT MIN({qc}), MAX({qc}) FROM {q}").fetchone()
                        if lo is not None:
                            c.value_range = (str(lo)[:19], str(hi)[:19])
                    elif any(k in typ for k in ("CHAR", "TEXT", "STRING", "ENUM")) and not c.primary_key:
                        budget -= 1
                        rows = conn.exec_driver_sql(
                            f"SELECT {qc} FROM {q} WHERE {qc} IS NOT NULL GROUP BY {qc} ORDER BY COUNT(*) DESC LIMIT 16"
                        ).fetchall()
                        if 0 < len(rows) <= 15 and (t.row_count or 0) > len(rows):
                            c.values = [str(r[0])[:40] for r in rows]
                except Exception:
                    continue

    def schema_text(self, tables: list[str] | None = None, with_samples: bool = True) -> str:
        """Compact, LLM-friendly description of the schema."""
        schema = self.get_schema()
        lines = []
        for name in tables or list(schema):
            t = schema.get(name)
            if t is None:
                continue
            kind = "VIEW" if t.is_view else "TABLE"
            rows = f"  -- ~{t.row_count} rows" if t.row_count is not None else ""
            lines.append(f"{kind} {self._quote(t.name)} ({rows}")
            for c in t.columns:
                flags = " PRIMARY KEY" if c.primary_key else ""
                flags += "" if c.nullable else " NOT NULL"
                note = ""
                if c.value_range:
                    note = f"  -- range {c.value_range[0]} .. {c.value_range[1]}"
                elif c.values:
                    note = "  -- values: " + ", ".join(repr(v) for v in c.values)
                lines.append(f"  {self._quote(c.name)} {c.type}{flags},{note}")
            for col, rt, rc in t.foreign_keys:
                lines.append(f"  FOREIGN KEY ({self._quote(col)}) REFERENCES {self._quote(rt)}({self._quote(rc)}),")
            lines.append(")")
            if with_samples and t.sample is not None and not t.sample.empty:
                sample = t.sample.copy()
                for c in sample.columns:
                    sample[c] = sample[c].map(_cell_str)
                lines.append(f"/* sample rows from {t.name}:\n{sample.to_csv(index=False).strip()}\n*/")
            lines.append("")
        return "\n".join(lines).strip()

    def relevant_tables(self, text: str, max_tables: int = 12) -> list[str]:
        """Pick the tables most related to the question (plus FK join partners). All tables if the schema is small."""
        schema = self.get_schema()
        if len(schema) <= max_tables:
            return list(schema)
        q, low = _tokens(text), text.lower()
        mentioned, scores = [], {}
        for t in schema.values():
            if t.name.lower() in low:
                mentioned.append(t.name)
            name_tokens = _tokens(t.name.replace("_", " "))
            col_tokens = set().union(*[_tokens(c.name.replace("_", " ")) for c in t.columns]) if t.columns else set()
            scores[t.name] = 3 * len(q & name_tokens) + len(q & col_tokens)

        def ranked(views: bool) -> list[str]:
            items = [n for n, s in scores.items() if s > 0 and schema[n].is_view == views]
            return sorted(items, key=lambda n: -scores[n])

        chosen: list[str] = list(dict.fromkeys(mentioned))[:max_tables]
        for n in ranked(views=False)[: max(1, max_tables // 2)]:
            if n not in chosen:
                chosen.append(n)
        for n in list(chosen):  # bring in join partners
            for _, rt, _ in schema[n].foreign_keys:
                if rt in schema and rt not in chosen and len(chosen) < max_tables:
                    chosen.append(rt)
        for n in ranked(views=False) + ranked(views=True):  # fill remaining slots, base tables first
            if len(chosen) >= max_tables:
                break
            if n not in chosen:
                chosen.append(n)
        if not chosen:
            chosen = [n for n, t in schema.items() if not t.is_view][:max_tables] or list(schema)[:max_tables]
        return chosen

    # ------------------------------------------------------------- execution
    def check(self, sql: str, read_only: bool) -> SafetyReport:
        return analyze(sql, self.sqlglot_dialect, read_only)

    def _set_timeout(self, conn) -> None:
        ms = int(self.query_timeout_s * 1000)
        try:
            if self.dialect == "postgresql":
                conn.exec_driver_sql(f"SET statement_timeout = {ms}")
            elif self.dialect in ("mysql", "mariadb"):
                conn.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME = {ms}")
        except Exception:
            pass

    def execute(self, sql: str, read_only: bool = True, row_limit: int = 500) -> QueryResult:
        report = self.check(sql, read_only)
        if not report.allowed:
            return QueryResult(report.sql, report.kind, error=f"Blocked: {report.reason}", warnings=report.warnings)

        sql = report.sql
        # Fetch one extra row so truncation can be detected; show the user-facing limit in the SQL.
        run_sql = add_limit(sql, row_limit + 1, self.sqlglot_dialect) if report.is_read else sql
        shown_sql = add_limit(sql, row_limit, self.sqlglot_dialect) if report.is_read else sql
        start = time.perf_counter()
        try:
            with self.engine.connect() as conn:
                self._set_timeout(conn)
                conn = conn.execution_options(no_parameters=True)
                if report.is_read:
                    res = conn.exec_driver_sql(run_sql)
                    if res.returns_rows:
                        rows = res.fetchmany(row_limit + 1)
                        truncated = len(rows) > row_limit
                        df = _clean_binary(pd.DataFrame(rows[:row_limit], columns=list(res.keys())))
                        elapsed = (time.perf_counter() - start) * 1000
                        return QueryResult(shown_sql, report.kind, df, len(df), truncated, elapsed, warnings=report.warnings)
                    elapsed = (time.perf_counter() - start) * 1000
                    return QueryResult(shown_sql, report.kind, None, 0, False, elapsed, warnings=report.warnings)
                with conn.begin():
                    res = conn.exec_driver_sql(run_sql)
                    count = res.rowcount if res.rowcount is not None else 0
                self._schema = None if report.kind == "ddl" else self._schema
                elapsed = (time.perf_counter() - start) * 1000
                return QueryResult(run_sql, report.kind, None, count, False, elapsed, warnings=report.warnings)
        except Exception as e:  # surface the DB error text so the agent can self-correct
            msg = str(getattr(e, "orig", e) or e).strip().splitlines()[0][:500]
            return QueryResult(shown_sql, report.kind, error=msg, warnings=report.warnings)

    # ------------------------------------------------------------- uploads
    @classmethod
    def from_dataframes(cls, frames: dict[str, pd.DataFrame], db_path: Path) -> "DatabaseManager":
        """Load uploaded CSV/Excel data into a local SQLite file and connect to it."""
        from .sources import frames_to_sqlite, sqlite_url

        frames_to_sqlite(frames, db_path)
        return cls(sqlite_url(db_path))

    def dispose(self) -> None:
        self.engine.dispose()
