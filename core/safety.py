"""SQL guardrails: parse, classify and sanity-check statements before they reach the database."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

# SQLAlchemy dialect name -> sqlglot dialect name
DIALECT_MAP = {
    "mysql": "mysql",
    "mariadb": "mysql",
    "postgresql": "postgres",
    "postgres": "postgres",
    "sqlite": "sqlite",
    "mssql": "tsql",
    "oracle": "oracle",
    "duckdb": "duckdb",
}

READ_PREFIXES = ("SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "VALUES", "TABLE")

# Patterns that are never allowed, even in write mode: file-system access, sleeps, privilege changes.
FORBIDDEN_PATTERNS = [
    r"\bINTO\s+(OUT|DUMP)FILE\b",
    r"\bLOAD_FILE\s*\(",
    r"\bLOAD\s+DATA\b",
    r"\bPG_READ_FILE\s*\(",
    r"\bPG_SLEEP\s*\(",
    r"\bSLEEP\s*\(",
    r"\bBENCHMARK\s*\(",
    r"\bCOPY\s+\w+.*\bFROM\s+PROGRAM\b",
    r"\b(GRANT|REVOKE)\b",
    r"\bCREATE\s+USER\b",
    r"\bALTER\s+USER\b",
    r"\bSHUTDOWN\b",
    r"\bATTACH\s+DATABASE\b",
]

WRITE_TYPES: tuple = tuple(
    t
    for t in (
        getattr(exp, "Insert", None),
        getattr(exp, "Update", None),
        getattr(exp, "Delete", None),
        getattr(exp, "Merge", None),
    )
    if t is not None
)
DDL_TYPES: tuple = tuple(
    t
    for t in (
        getattr(exp, "Create", None),
        getattr(exp, "Drop", None),
        getattr(exp, "Alter", None),
        getattr(exp, "AlterTable", None),
        getattr(exp, "TruncateTable", None),
        getattr(exp, "RenameTable", None),
    )
    if t is not None
)
READ_TYPES: tuple = tuple(
    t
    for t in (
        getattr(exp, "Select", None),
        getattr(exp, "Union", None),
        getattr(exp, "Intersect", None),
        getattr(exp, "Except", None),
        getattr(exp, "SetOperation", None),
        getattr(exp, "Show", None),
        getattr(exp, "Describe", None),
        getattr(exp, "Values", None),
    )
    if t is not None
)


@dataclass
class SafetyReport:
    sql: str
    kind: str  # "read" | "write" | "ddl" | "unknown"
    allowed: bool
    reason: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def is_read(self) -> bool:
        return self.kind == "read"


def to_sqlglot_dialect(sqlalchemy_dialect: str) -> str:
    return DIALECT_MAP.get(sqlalchemy_dialect.lower(), "")


def clean_sql(sql: str) -> str:
    """Strip markdown fences, comments-only lines and trailing semicolons from LLM output."""
    sql = sql.strip()
    fence = re.search(r"```(?:sql|mysql|postgresql|sqlite)?\s*(.*?)```", sql, re.S | re.I)
    if fence:
        sql = fence.group(1).strip()
    sql = sql.strip().rstrip(";").strip()
    return sql


def _strip_literals(sql: str) -> str:
    """Remove quoted strings so keyword checks don't trigger on data like 'drop shipping'."""
    return re.sub(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.)*\"|`[^`]*`", "''", sql)


def _first_keyword(sql: str) -> str:
    no_comments = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S).strip()
    m = re.match(r"\(*\s*([A-Za-z]+)", no_comments)
    return m.group(1).upper() if m else ""


def _classify_tree(tree: exp.Expression) -> str:
    # A write hidden anywhere in the tree (e.g. a data-modifying CTE) makes the whole statement a write.
    if DDL_TYPES and (isinstance(tree, DDL_TYPES) or tree.find(*DDL_TYPES)):
        return "ddl"
    if WRITE_TYPES and (isinstance(tree, WRITE_TYPES) or tree.find(*WRITE_TYPES)):
        return "write"
    if isinstance(tree, READ_TYPES):
        if isinstance(tree, exp.Select) and tree.args.get("into"):
            return "ddl"  # SELECT ... INTO new_table
        return "read"
    if isinstance(tree, exp.Command):
        return "read" if _first_keyword(tree.sql()) in READ_PREFIXES else "unknown"
    return "unknown"


def _danger_warnings(tree: exp.Expression | None, kind: str) -> list[str]:
    warnings: list[str] = []
    if tree is None:
        return warnings
    for node_type, label in (("Update", "UPDATE"), ("Delete", "DELETE")):
        cls = getattr(exp, node_type, None)
        if cls is not None:
            for node in tree.find_all(cls):
                if not node.args.get("where"):
                    warnings.append(f"{label} without a WHERE clause will affect every row in the table.")
    if kind == "ddl":
        warnings.append("This statement changes the database structure (DDL).")
    drop = getattr(exp, "Drop", None)
    if drop is not None and (isinstance(tree, drop) or tree.find(drop)):
        warnings.append("DROP permanently deletes the object and its data.")
    return warnings


def analyze(sql: str, dialect: str = "", read_only: bool = True) -> SafetyReport:
    """Classify *sql* and decide whether it may run under the current mode."""
    sql = clean_sql(sql)
    if not sql:
        return SafetyReport(sql, "unknown", False, "Empty query.")

    scrubbed = _strip_literals(sql).upper()
    for pattern in FORBIDDEN_PATTERNS:
        if re.search(pattern, scrubbed, re.I | re.S):
            return SafetyReport(sql, "unknown", False, "Query uses a forbidden operation (file access, sleep or privilege change).")

    tree: exp.Expression | None = None
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect or None) if s is not None]
    except Exception:  # sqlglot could not parse: fall back to keyword checks
        statements = None

    if statements is not None:
        if len(statements) != 1:
            return SafetyReport(sql, "unknown", False, "Only one SQL statement can run at a time.")
        tree = statements[0]
        kind = _classify_tree(tree)
    else:
        if ";" in _strip_literals(sql):
            return SafetyReport(sql, "unknown", False, "Only one SQL statement can run at a time.")
        first = _first_keyword(sql)
        write_kw = re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|REPLACE|UPSERT)\b", scrubbed)
        ddl_kw = re.search(r"\b(CREATE|DROP|ALTER|TRUNCATE|RENAME)\b", scrubbed)
        if ddl_kw:
            kind = "ddl"
        elif write_kw:
            kind = "write"
        elif first in READ_PREFIXES:
            kind = "read"
        else:
            kind = "unknown"

    warnings = _danger_warnings(tree, kind)

    if kind == "read":
        return SafetyReport(sql, kind, True, warnings=warnings)
    if kind == "unknown":
        return SafetyReport(sql, kind, False, "Could not verify this statement is safe to run.", warnings)
    if read_only:
        return SafetyReport(
            sql, kind, False,
            "Read-only mode is on. Enable write mode in the sidebar to run data-modifying statements.",
            warnings,
        )
    return SafetyReport(sql, kind, True, warnings=warnings)


def add_limit(sql: str, limit: int, dialect: str = "") -> str:
    """Add a LIMIT to a plain SELECT that has none. Leaves everything else untouched."""
    if dialect in ("tsql", "oracle"):
        return sql  # different row-limiting syntax; rely on fetch-side limits
    try:
        tree = sqlglot.parse_one(sql, read=dialect or None)
    except Exception:
        return sql
    if isinstance(tree, exp.Select) and not tree.args.get("limit"):
        if tree.find(exp.AggFunc) and not tree.args.get("group"):
            return sql  # single-row aggregate, no limit needed
        # Append textually rather than regenerating, so the user's SQL keeps its exact form.
        return f"{sql}\nLIMIT {int(limit)}"
    return sql
