"""Power BI-style dashboards: a flat fact table, KPI cards, slicers, cross-filtering charts and pinned chat answers.

A dashboard spec is plain JSON:
  {"title", "fact_sql", "date": <date column or null>,
   "kpis":   [{"label", "col", "agg", "format"} | {"label", "ratio": [part, part], "scale", "format"}],
   "slicers": [<dimension columns>],
   "charts": [{"title", "type": bar|line|pie|scatter|histogram|table, "x", "y", "agg", "color", "top", "grain",
               "order", "label", "y_label"}]}
agg = sum | mean | count | nunique | max | min | mean_pct (mean x 100, for 0/1 flags).

Specs come from dashboards.json (bundled datasets), are generated from the schema (any database), or are designed
by the LLM. User edits and pinned chat answers are stored per database in data/dashboards.json.
"""
from __future__ import annotations

import io
import json
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from .config import BASE_DIR, DATA_DIR
from .safety import clean_sql

BUILTIN_FILE = BASE_DIR / "dashboards.json"
USER_FILE = DATA_DIR / "dashboards.json"
FACT_ROW_CAP = 1_000_000
AGGS = ("sum", "mean", "count", "nunique", "max", "min", "mean_pct")
CHART_TYPES = ("bar", "line", "pie", "scatter", "histogram", "table")
GRAINS = {"day": "D", "week": "W", "month": "M", "quarter": "Q", "year": "Y"}


# =================================================================== storage
def builtin_spec(dataset_id: str) -> dict | None:
    if not BUILTIN_FILE.exists():
        return None
    spec = json.loads(BUILTIN_FILE.read_text(encoding="utf-8")).get("dashboards", {}).get(dataset_id)
    return json.loads(json.dumps(spec)) if spec else None  # deep copy


class DashboardStore:
    """Per-database user state: an edited/generated spec (optional) and pinned chat answers."""

    def __init__(self, path: Path = USER_FILE):
        self.path = path

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def get(self, key: str) -> dict:
        return self._read().get(key, {})

    def _update(self, key: str, **fields) -> None:
        data = self._read()
        entry = data.get(key, {})
        for k, v in fields.items():
            if v is None:
                entry.pop(k, None)
            else:
                entry[k] = v
        data[key] = entry
        self._write(data)

    def save_spec(self, key: str, spec: dict | None) -> None:
        self._update(key, spec=spec)

    def pins(self, key: str) -> list[dict]:
        return self.get(key).get("pins", [])

    def pin(self, key: str, title: str, sql: str, chart: dict | None, question: str = "") -> dict:
        pins = [p for p in self.pins(key) if p.get("sql") != sql]
        item = {"id": uuid.uuid4().hex[:8], "title": title or question[:60] or "Pinned answer", "sql": sql,
                "chart": chart, "question": question, "pinned_at": datetime.now().isoformat(timespec="seconds")}
        self._update(key, pins=pins + [item])
        return item

    def unpin(self, key: str, pin_id: str) -> None:
        self._update(key, pins=[p for p in self.pins(key) if p.get("id") != pin_id])

    def is_pinned(self, key: str, sql: str) -> bool:
        return any(p.get("sql") == sql for p in self.pins(key))


# =================================================================== fact data
def load_fact(db, sql: str, date_col: str | None = None, cap: int = FACT_ROW_CAP) -> pd.DataFrame:
    """Run the fact query (read-only, capped) and tidy types: dates parsed, text as compact categories."""
    sql = clean_sql(sql)
    report = db.check(sql, read_only=True)
    if not report.is_read:
        raise ValueError(f"The dashboard query must be a read-only SELECT ({report.reason or report.kind}).")
    with db.engine.connect() as conn:
        res = conn.execution_options(no_parameters=True).exec_driver_sql(sql)
        rows = res.fetchmany(cap)
        df = pd.DataFrame(rows, columns=list(res.keys()))
    for c in df.columns:
        if df[c].dtype == object:
            num = pd.to_numeric(df[c], errors="coerce")
            if len(df) and num.notna().sum() == df[c].notna().sum() and df[c].notna().any():
                df[c] = num  # Decimals / numeric text
    if date_col and date_col in df:
        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    for c in df.columns:
        if (df[c].dtype == object or pd.api.types.is_string_dtype(df[c])) and c != date_col:
            if df[c].nunique(dropna=True) < max(50, len(df) // 2):
                df[c] = df[c].astype("category")
    return df


def column_roles(df: pd.DataFrame, date_col: str | None = None) -> dict[str, list[str]]:
    dates = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    if date_col and date_col in df and date_col not in dates:
        dates.insert(0, date_col)
    numeric = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c])
               and c not in dates]
    measures = [c for c in numeric if not re.search(r"(^|_)id$|Id$|ID$", str(c))]
    dims = [c for c in df.columns if c not in dates and c not in numeric and 1 < df[c].nunique(dropna=True) <= 200]
    small_numeric_dims = [c for c in numeric if df[c].nunique(dropna=True) <= 12 and c not in measures]
    return {"dates": dates, "measures": measures, "dimensions": dims + small_numeric_dims, "numeric": numeric}


# =================================================================== filtering
@dataclass
class Filters:
    date_range: tuple[date, date] | None = None
    slicers: dict[str, list] | None = None   # column -> allowed values (empty = all)

    def apply(self, df: pd.DataFrame, date_col: str | None, skip: str | None = None) -> pd.DataFrame:
        mask = pd.Series(True, index=df.index)
        if self.date_range and date_col and date_col in df:
            lo, hi = pd.Timestamp(self.date_range[0]), pd.Timestamp(self.date_range[1]) + pd.Timedelta(days=1)
            mask &= (df[date_col] >= lo) & (df[date_col] < hi)
        for col, values in (self.slicers or {}).items():
            if values and col in df and col != skip:
                mask &= df[col].astype(str).isin([str(v) for v in values])
        return df[mask]


def previous_period(date_range: tuple[date, date] | None, full: tuple[date, date] | None) -> tuple[date, date] | None:
    """Same-length window just before the selected range (for KPI deltas). None when the full range is selected."""
    if not date_range or not full or (date_range[0] <= full[0] and date_range[1] >= full[1]):
        return None
    span = (date_range[1] - date_range[0]).days + 1
    end = date_range[0] - timedelta(days=1)
    start = end - timedelta(days=span - 1)
    return (start, end) if end >= full[0] else None


# =================================================================== aggregation
def _agg(series: pd.Series, agg: str) -> float:
    if series is None:
        return float("nan")
    if agg == "count":
        return float(len(series))
    if agg == "nunique":
        return float(series.nunique(dropna=True))
    s = pd.to_numeric(series, errors="coerce")
    if agg == "mean_pct":
        return float(s.mean() * 100) if s.notna().any() else float("nan")
    fn = {"sum": s.sum, "mean": s.mean, "max": s.max, "min": s.min}.get(agg, s.sum)
    return float(fn()) if s.notna().any() else float("nan")


def kpi_value(df: pd.DataFrame, kpi: dict) -> float:
    if "ratio" in kpi:
        num, den = (_agg(df.get(p["col"]), p.get("agg", "sum")) for p in kpi["ratio"])
        return (num / den * kpi.get("scale", 1)) if den else float("nan")
    return _agg(df.get(kpi["col"]), kpi.get("agg", "sum"))


def format_value(v: float, fmt: str = "num") -> str:
    if v is None or pd.isna(v):
        return "–"
    sign = "-" if v < 0 else ""
    a = abs(v)
    if fmt == "pct":
        return f"{v:.1f}%"

    def compact(x: float, money: bool) -> str:
        prefix = "$" if money else ""
        for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
            if x >= div * (10 if div == 1e3 else 1):
                return f"{prefix}{x / div:.2f}{suf}" if x / div < 100 else f"{prefix}{x / div:.1f}{suf}"
        return f"{prefix}{x:,.2f}" if money else (f"{x:,.0f}" if x == int(x) else f"{x:,.1f}")

    if fmt == "money":
        return sign + compact(a, True)
    if fmt == "int":
        return sign + (compact(a, False) if a >= 10_000 else f"{a:,.0f}")
    return sign + compact(a, False)


def chart_frame(df: pd.DataFrame, chart: dict) -> pd.DataFrame:
    """Aggregate the (filtered) fact table into the small table a chart draws."""
    kind, x, y, agg, color = chart["type"], chart.get("x"), chart.get("y"), chart.get("agg", "sum"), chart.get("color")
    if kind in ("scatter", "histogram", "table"):
        cols = [c for c in dict.fromkeys([chart.get("label"), x, y, color] + (chart.get("columns") or [])) if c and c in df]
        if kind == "table":
            out = df[cols or list(df.columns)].head(chart.get("top") or 200)
        else:
            out = df[cols].dropna(subset=[c for c in (x, y) if c]).head(5000)
        return out.reset_index(drop=True)

    if x not in df:
        return pd.DataFrame()
    value = chart.get("y_label") or (f"{agg} of {y}" if y else "count")
    keys = [x] + ([color] if color and color in df and color != x else [])
    data = df
    if chart.get("grain") and pd.api.types.is_datetime64_any_dtype(df[x]):
        grain = chart["grain"]
        period = df[x].dt.to_period(GRAINS.get(grain, "M"))
        label = {"year": "year", "quarter": "quarter", "month": "month", "week": "week", "day": "date"}[grain]
        data = df.assign(**{label: period.astype(str) if grain != "day" else period.dt.start_time.dt.date.astype(str)})
        keys = [label] + keys[1:]
    grouped = data.groupby(keys, observed=True, dropna=True)
    if agg == "count" or not y:
        out = grouped.size().reset_index(name=value)
    elif agg == "nunique":
        out = grouped[y].nunique().reset_index(name=value)
    else:
        series = pd.to_numeric(data[y], errors="coerce")
        g = series.groupby([data[k] for k in keys], observed=True, dropna=True)
        res = {"sum": g.sum, "mean": g.mean, "max": g.max, "min": g.min, "mean_pct": lambda: g.mean() * 100}[agg]()
        out = res.reset_index(name=value)
    for k in keys:
        if isinstance(out[k].dtype, pd.CategoricalDtype):
            out[k] = out[k].astype(str)
    out[value] = out[value].round(2)
    first = keys[0]
    if chart.get("grain"):
        out = out.sort_values(first)
    elif chart.get("order"):
        order = {str(v): i for i, v in enumerate(chart["order"])}
        out = out.assign(_o=out[first].astype(str).map(order).fillna(len(order))).sort_values(["_o", first]).drop(columns="_o")
    else:
        totals = out.groupby(first)[value].sum().sort_values(ascending=False)
        top = chart.get("top")
        keep = list(totals.index[:top]) if top else list(totals.index)
        out = out[out[first].isin(keep)]
        out = out.assign(_o=out[first].map({k: i for i, k in enumerate(keep)})).sort_values("_o").drop(columns="_o")
    return out.reset_index(drop=True)


def chart_spec_for_figure(chart: dict, frame: pd.DataFrame) -> dict | None:
    """Translate a dashboard chart into the spec charts.build_figure understands."""
    if frame is None or frame.empty:
        return None
    kind = chart["type"]
    if kind in ("scatter", "histogram"):
        return {"type": kind, "x": chart.get("x"), "y": chart.get("y"),
                "color": chart.get("color") if chart.get("color") in frame else None}
    if kind == "table":
        return None
    x = frame.columns[0]
    value = frame.columns[-1]
    color = chart.get("color") if chart.get("color") in frame.columns and chart.get("color") != x else None
    return {"type": kind, "x": x, "y": value, "color": color}


# =================================================================== automatic specs
def auto_spec(db, title: str = "") -> dict:
    """Build a sensible dashboard for any database without an LLM: biggest table, its dates, measures, dimensions."""
    schema = db.get_schema()
    tables = [t for t in schema.values() if not t.is_view] or list(schema.values())
    if not tables:
        raise ValueError("This database has no tables.")
    table = max(tables, key=lambda t: t.row_count or 0)
    q = db._quote(table.name)
    sql = f"SELECT * FROM {q}"
    sample = load_fact(db, sql + " LIMIT 5000")
    date_col = next((c.name for c in table.columns if c.value_range), None)
    if date_col:
        sample[date_col] = pd.to_datetime(sample[date_col], errors="coerce")
        if sample[date_col].notna().mean() < 0.8:
            date_col = None
    roles = column_roles(sample, date_col)
    measures, dims = roles["measures"][:4], [d for d in roles["dimensions"] if sample[d].nunique() <= 60][:4]
    additive = re.compile(r"amount|price|revenue|sales|total|qty|quantity|count|cost|spend|paid|payment|fee|units|"
                          r"profit|income|value|volume|cases|deaths|population", re.I)

    def how(m: str) -> tuple[str, str]:
        """Sum things that add up (money, quantities, counts); average measurements (lengths, ages, ratings…)."""
        return ("sum", "Total") if additive.search(m) else ("mean", "Average")

    kpis = [{"label": f"{pretty_label(table.name)} rows", "col": sample.columns[0], "agg": "count", "format": "int"}]
    for m in measures[:4]:
        agg_m, word = how(m)
        kpis.append({"label": f"{word} {pretty_label(m).lower()}", "col": m, "agg": agg_m, "format": "num"})
    charts = []
    main_measure = measures[0] if measures else None
    agg, word = how(main_measure) if main_measure else ("count", "")
    y_label = f"{word} {pretty_label(main_measure).lower()}" if main_measure else "Rows"
    if date_col:
        charts.append({"title": f"{y_label} over time", "type": "line", "x": date_col, "grain": "month",
                       "y": main_measure, "agg": agg, "y_label": y_label})
    for i, d in enumerate(dims[:3]):
        charts.append({"title": f"{y_label} by {pretty_label(d).lower()}", "type": "pie" if i == 1 and agg == "sum" and sample[d].nunique() <= 6 else "bar",
                       "x": d, "y": main_measure, "agg": agg, "top": 10, "y_label": y_label})
    if len(measures) >= 2:
        charts.append({"title": f"{pretty_label(measures[0])} vs {pretty_label(measures[1]).lower()}", "type": "scatter",
                       "x": measures[0], "y": measures[1], "color": dims[0] if dims else None})
    elif main_measure:
        charts.append({"title": f"{pretty_label(main_measure)} distribution", "type": "histogram", "x": main_measure})
    charts.append({"title": f"{pretty_label(table.name)} records", "type": "table", "top": 100})
    return {"title": title or f"{pretty_label(table.name)} overview", "fact_sql": sql + f" LIMIT {FACT_ROW_CAP}",
            "date": date_col, "kpis": kpis[:5], "slicers": dims[:3], "charts": charts, "generated": "auto"}


DESIGN_SYSTEM = """You are a BI developer designing a Power BI-style dashboard for a {dialect} database.
SCHEMA:
{schema}

Design ONE dashboard as JSON:
{{"title": "<short title>",
  "fact_sql": "<ONE read-only {dialect} SELECT that joins the main fact table to its dimensions and returns one row per
               business event, with readable snake_case column aliases: a date column, numeric measures, ids, and
               text dimensions. No LIMIT, no aggregation.>",
  "date": "<date column alias from fact_sql, or null>",
  "kpis": [{{"label": "...", "col": "<alias>", "agg": "sum|mean|count|nunique|max|min|mean_pct", "format": "money|int|num|pct"}}],
  "slicers": ["<2-4 text dimension aliases good for filtering>"],
  "charts": [{{"title": "...", "type": "line|bar|pie|scatter|histogram", "x": "<alias>", "y": "<measure alias>",
              "agg": "sum|mean|count|nunique|max|mean_pct", "grain": "month (only for line over the date)",
              "top": 10, "color": "<optional dimension alias>", "y_label": "<axis label>"}}]}}
Use 4-5 KPIs and 6 charts: one trend over time if a date exists, rankings (bar, top 10), one part-to-whole (pie, <=6
parts) and one relationship or distribution. Use ONLY tables/columns from the schema. Return only the JSON."""


def ai_spec(db, llm, dialect_label: str) -> dict:
    """Let the LLM design the dashboard from the schema; validated against real data before use."""
    from .llm import parse_json

    schema = db.schema_text(db.relevant_tables("", max_tables=15), with_samples=True)
    spec = parse_json(llm.chat([{"role": "system", "content": DESIGN_SYSTEM.format(dialect=dialect_label, schema=schema)},
                                {"role": "user", "content": "Design the dashboard."}], json_mode=True))
    return validate_spec(db, spec)


def validate_spec(db, spec: dict) -> dict:
    """Run the fact query on a sample and drop anything that refers to missing columns."""
    if not spec.get("fact_sql"):
        raise ValueError("The design has no fact query.")
    sample = load_fact(db, clean_sql(spec["fact_sql"]).rstrip(";") + " LIMIT 2000", spec.get("date"))
    cols = set(sample.columns)
    spec["date"] = spec.get("date") if spec.get("date") in cols else None
    ok = lambda c: c is None or c in cols  # noqa: E731
    spec["kpis"] = [k for k in spec.get("kpis", []) if (all(ok(p.get("col")) for p in k["ratio"]) if "ratio" in k
                                                     else ok(k.get("col"))) and k.get("agg", "sum") in AGGS][:6]
    spec["slicers"] = [s for s in spec.get("slicers", []) if s in cols][:4]
    charts = []
    for c in spec.get("charts", []):
        if c.get("type") not in CHART_TYPES or not ok(c.get("x")) or not ok(c.get("y")) or not ok(c.get("color")):
            continue
        if c.get("agg", "sum") not in AGGS:
            c["agg"] = "sum"
        if c.get("grain") and c.get("x") != spec["date"]:
            c.pop("grain")
        charts.append(c)
    spec["charts"] = charts[:8]
    if not spec["kpis"] or not spec["charts"]:
        raise ValueError("The design didn't produce usable KPIs and charts.")
    spec["generated"] = spec.get("generated", "ai")
    return spec


def pretty_label(name) -> str:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(name)).replace("_", " ").strip()
    return text[:1].upper() + text[1:].lower() if text else str(name)


# =================================================================== Power BI export
DAX_AGG = {"sum": "SUM", "mean": "AVERAGE", "max": "MAX", "min": "MIN"}


def dax_measures(spec: dict, table: str = "Fact") -> str:
    lines = [f"// DAX measures for '{spec.get('title', 'Dashboard')}'.",
             "// In Power BI Desktop: Modeling > New measure, then paste one line at a time.", ""]
    names = []

    def expr(part: dict) -> str:
        col, agg = part.get("col"), part.get("agg", "sum")
        if agg == "count":
            return f"COUNTROWS('{table}')"
        if agg == "nunique":
            return f"DISTINCTCOUNT('{table}'[{col}])"
        if agg == "mean_pct":
            return f"AVERAGE('{table}'[{col}]) * 100"
        return f"{DAX_AGG.get(agg, 'SUM')}('{table}'[{col}])"

    for k in spec.get("kpis", []):
        name = k["label"].replace("[", "(").replace("]", ")")
        if "ratio" in k:
            num, den = (expr(p) for p in k["ratio"])
            scale = k.get("scale", 1)
            body = f"DIVIDE({num}, {den})" + (f" * {scale}" if scale != 1 else "")
        else:
            body = expr(k)
        names.append(name)
        lines.append(f"{name} = {body}")
    return "\n".join(lines) + "\n"


def power_query_m(df: pd.DataFrame, filename: str = "fact.csv") -> str:
    def m_type(s: pd.Series) -> str:
        if pd.api.types.is_datetime64_any_dtype(s):
            return "type date"
        if pd.api.types.is_integer_dtype(s):
            return "Int64.Type"
        if pd.api.types.is_numeric_dtype(s):
            return "type number"
        return "type text"

    types = ", ".join(f'{{"{c}", {m_type(df[c])}}}' for c in df.columns)
    return f"""// Power Query (M) to load the fact table. In Power BI Desktop: Home > Transform data > New Source >
// Blank Query > Advanced Editor, paste this, and change the file path to where you saved {filename}.
let
    Source = Csv.Document(File.Contents("C:\\path\\to\\{filename}"), [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),
    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),
    Typed = Table.TransformColumnTypes(Promoted, {{{types}}})
in
    Typed
"""


def visual_guide(spec: dict) -> list[str]:
    names = {"bar": "Clustered bar/column chart", "line": "Line chart", "pie": "Donut chart",
             "scatter": "Scatter chart", "histogram": "Column chart (bin the field first)", "table": "Table"}
    out = []
    for c in spec.get("charts", []):
        agg = c.get("agg", "sum")
        value = (f"{'Count' if agg == 'count' else 'Count (Distinct)' if agg == 'nunique' else 'Average' if agg in ('mean', 'mean_pct') else agg.title()}"
                 f" of {c.get('y') or c.get('x')}")
        axis = c.get("x") + (f" (by {c['grain']})" if c.get("grain") else "") if c.get("x") else "—"
        extra = f"; Legend = {c['color']}" if c.get("color") else ""
        extra += f"; Filter = top {c['top']} by value" if c.get("top") else ""
        if c["type"] == "scatter":
            out.append(f"- **{c['title']}**: {names['scatter']}. X = {c.get('x')}, Y = {c.get('y')}" +
                       (f", Legend = {c['color']}" if c.get("color") else "") + (f", Details = {c['label']}" if c.get("label") else ""))
        elif c["type"] == "table":
            out.append(f"- **{c['title']}**: Table with the columns you need.")
        else:
            out.append(f"- **{c['title']}**: {names.get(c['type'], 'Chart')}. Axis = {axis}; Values = {value}{extra}")
    return out


def powerbi_readme(spec: dict, db_label: str, rows: int, filtered: bool, excel_has_fact: bool) -> str:
    kpis = "\n".join(f"- **{k['label']}** (card visual)" for k in spec.get("kpis", []))
    slicers = ", ".join(spec.get("slicers", [])) or "none"
    date_line = f"- Add a **Slicer** on `{spec['date']}` (style: Between) for the date range.\n" if spec.get("date") else ""
    excel_line = ("   (Or **Get data → Excel workbook** → `dashboard.xlsx` → tick the **Fact** sheet.)\n" if excel_has_fact else "")
    return f"""# Open this dashboard in Power BI

Dashboard: **{spec.get('title', 'Dashboard')}**  |  Source: {db_label}  |  Exported {datetime.now():%Y-%m-%d %H:%M}
Fact table: {rows:,} rows{' (with the filters that were active in QueryMind)' if filtered else ''}

## Files in this pack

| File | What it is |
|---|---|
| `fact.csv` | The flat fact table every visual is built from (one row per event) |
| `dashboard.xlsx` | {'Sheet "Fact" plus' if excel_has_fact else 'Too many rows for Excel, so the fact table is only in fact.csv;'} one sheet per KPI/chart with the numbers QueryMind showed |
| `measures.dax` | Ready-made DAX measures for the KPI cards |
| `load_fact.pq` | Power Query (M) script that loads fact.csv with the right column types |
| `fact_query.sql` | The SQL behind the fact table, for a live connection to the database |

## Steps (about 10 minutes)

1. Open **Power BI Desktop** → **Get data** → **Text/CSV** → choose `fact.csv` → **Load**.
   Rename the table to **Fact** (right-click it in the Data pane → Rename).
{excel_line}2. Check column types in **Transform data**: dates as *Date*, measures as *Decimal number*.
   `load_fact.pq` does this for you if you prefer pasting a script.
3. Create the measures: **Modeling → New measure**, paste each line from `measures.dax`.
4. Build the report page:
{kpis}
{date_line}- Add a **Slicer** for each of: {slicers}.
{chr(10).join(visual_guide(spec))}
5. Cross-filtering works automatically: clicking a bar or slice filters the other visuals, as in QueryMind.
6. Save as `.pbix`. To refresh with new data later, export again from QueryMind and click **Refresh**,
   or connect live (below).

## Live connection instead of a CSV

- **MySQL / PostgreSQL / SQL Server**: Get data → choose the database → Advanced options → paste `fact_query.sql`.
- **SQLite**: install the SQLite ODBC driver, then Get data → ODBC → your DSN → paste `fact_query.sql`.
"""


def export_powerbi(spec: dict, fact: pd.DataFrame, chart_frames: dict[str, pd.DataFrame],
                   kpi_rows: list[dict], db_label: str, filtered: bool, excel_fact_limit: int = 100_000) -> bytes:
    """Zip with fact.csv, dashboard.xlsx, measures.dax, load_fact.pq, fact_query.sql and a step-by-step README."""
    fact_out = fact.copy()
    for c in fact_out.columns:
        if isinstance(fact_out[c].dtype, pd.CategoricalDtype):
            fact_out[c] = fact_out[c].astype(str).replace("nan", "")
        if pd.api.types.is_datetime64_any_dtype(fact_out[c]):
            fact_out[c] = fact_out[c].dt.date
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("fact.csv", fact_out.to_csv(index=False))
        xbuf = io.BytesIO()
        excel_has_fact = len(fact_out) <= excel_fact_limit
        with pd.ExcelWriter(xbuf, engine="openpyxl") as xw:
            if excel_has_fact:
                fact_out.to_excel(xw, sheet_name="Fact", index=False)
            pd.DataFrame(kpi_rows).to_excel(xw, sheet_name="KPIs", index=False)
            used = {"Fact", "KPIs"}
            for title, frame in chart_frames.items():
                name = re.sub(r"[\[\]\*\?/\\:]", "", title)[:28] or "Chart"
                base, i = name, 2
                while name in used:
                    name, i = f"{base[:25]}_{i}", i + 1
                used.add(name)
                frame.to_excel(xw, sheet_name=name, index=False)
        z.writestr("dashboard.xlsx", xbuf.getvalue())
        z.writestr("measures.dax", dax_measures(spec))
        z.writestr("load_fact.pq", power_query_m(fact))
        z.writestr("fact_query.sql", clean_sql(spec.get("fact_sql", "")) + "\n")
        z.writestr("POWERBI_README.md", powerbi_readme(spec, db_label, len(fact_out), filtered, excel_has_fact))
    return buf.getvalue()


# =================================================================== several databases
KEY_ALIASES = {
    "usa": "united states", "us": "united states", "u s": "united states", "u s a": "united states",
    "united states of america": "united states", "america": "united states",
    "uk": "united kingdom", "u k": "united kingdom", "great britain": "united kingdom", "britain": "united kingdom",
    "england": "united kingdom", "czech republic": "czechia", "russian federation": "russia",
    "korea republic of": "south korea", "republic of korea": "south korea", "korea south": "south korea",
    "congo the democratic republic of the": "democratic republic of congo", "dr congo": "democratic republic of congo",
    "viet nam": "vietnam", "holy see vatican city state": "vatican", "virgin islands u s": "united states virgin islands",
    "iran islamic republic of": "iran", "turkiye": "turkey", "ivory coast": "cote divoire", "cote d ivoire": "cote divoire",
    "uae": "united arab emirates", "the netherlands": "netherlands", "holland": "netherlands",
}


def normalize_key(value) -> str:
    """Canonical form for matching values across databases ('USA' == 'United States', 'Côte d'Ivoire' == 'Cote d'Ivoire')."""
    import unicodedata

    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", " ", text.lower().replace("&", " and ")).strip()
    text = KEY_ALIASES.get(text, text)
    return KEY_ALIASES.get(text.replace(" ", ""), text) if text.replace(" ", "") in KEY_ALIASES else text


def _key_set(series: pd.Series) -> set[str]:
    return {k for k in (normalize_key(v) for v in series.dropna().astype(str).unique()) if k}


def key_candidates(df: pd.DataFrame) -> list[str]:
    """Columns that could link databases: any text column (any number of distinct values) plus small integer codes."""
    out = []
    for c in df.columns:
        s = df[c]
        if pd.api.types.is_datetime64_any_dtype(s):
            continue
        n = s.nunique(dropna=True)
        if n < 2:
            continue
        if s.dtype == object or pd.api.types.is_string_dtype(s) or isinstance(s.dtype, pd.CategoricalDtype):
            out.append(c)
    return out


def find_shared_fields(facts: dict[str, pd.DataFrame], main: str, dims: dict[str, list[str]] | None = None) -> dict:
    """Find the field the databases have in common by comparing actual values (not just column names).

    Returns {"fields": {alias: column}, "overlap": {alias: share of the main values also found there}, "score": float}
    or {} when nothing is shared."""
    others = [a for a in facts if a != main]
    if not others:
        return {}
    dims = dims or {a: key_candidates(df) for a, df in facts.items()}
    sets = {a: {c: _key_set(facts[a][c]) for c in dims.get(a, []) if c in facts[a]} for a in facts}

    def name_bonus(c1: str, c2: str) -> float:
        t1, t2 = set(re.split(r"[_\W]+", c1.lower())), set(re.split(r"[_\W]+", c2.lower()))
        return 0.1 if t1 & t2 - {"", "id", "name"} else 0.0

    best: dict = {}
    for mc, mset in sets[main].items():
        if len(mset) < 2:
            continue
        fields, overlaps, total = {main: mc}, {}, 0.0
        for a in others:
            cand = []
            for oc, oset in sets[a].items():
                if len(oset) < 2:
                    continue
                inter = len(mset & oset)
                if inter < 2:
                    continue
                share = inter / len(mset)                       # how much of the main field is matched
                jacc = inter / len(mset | oset)
                cand.append((0.6 * share + 0.4 * jacc + name_bonus(mc, oc), oc, share))
            if not cand:
                break
            score, oc, share = max(cand)
            fields[a], overlaps[a] = oc, round(share, 3)
            total += score
        else:
            if total > best.get("score", 0) and min(overlaps.values()) >= 0.15:
                best = {"fields": fields, "overlap": overlaps, "score": round(total / len(others), 3)}
    return best


def merged_frame(facts: dict[str, pd.DataFrame], fields: dict[str, str], measures: dict[str, dict],
                 labels: dict[str, str]) -> pd.DataFrame:
    """One row per shared value, one column per database measure (outer join on the normalised key)."""
    parts = []
    display: dict[str, str] = {}
    for alias, df in facts.items():
        col, m = fields[alias], measures[alias]
        keys = df[col].astype(str).map(normalize_key)
        frame = df.assign(_key=keys)
        frame = frame[frame["_key"] != ""]
        name = f"{labels[alias]}: {m.get('label') or pretty_label(m.get('col') or 'rows')}"
        agg = m.get("agg", "sum")
        g = frame.groupby("_key", observed=True)
        if agg == "count":
            s = g.size()
        elif agg == "nunique":
            s = g[m["col"]].nunique()
        else:
            vals = pd.to_numeric(frame[m["col"]], errors="coerce")
            gg = vals.groupby(frame["_key"])
            s = {"sum": lambda: gg.sum(min_count=1),  # all-blank stays blank (no data), not 0
                 "mean": gg.mean, "max": gg.max, "min": gg.min, "mean_pct": lambda: gg.mean() * 100}[agg]()
        parts.append(s.rename(name).round(2))
        for k, original in zip(frame["_key"], frame[col].astype(str)):
            display.setdefault(k, original)
    out = pd.concat(parts, axis=1).reset_index().rename(columns={"index": "_key", "_key": "_key"})
    out.insert(0, "shared_value", out["_key"].map(display))
    return out.drop(columns="_key")


def comparison_view(merged: pd.DataFrame, mode: str = "auto", top: int = 15) -> tuple[pd.DataFrame, str]:
    """Rows present in every database, top N by the first measure. Values become each database's % share of its
    own matched total when scales differ a lot (one axis, honest comparison) or when asked."""
    value_cols = [c for c in merged.columns if c != "shared_value"]
    both = merged.dropna(subset=value_cols).copy()
    if both.empty:
        return both, "raw"
    totals = both[value_cols].abs().sum()
    spread = (totals.max() / totals[totals > 0].min()) if (totals > 0).any() else 1
    use_share = mode == "share" or (mode == "auto" and spread > 10)
    both = both.sort_values(value_cols[0], ascending=False).head(top)
    if use_share:
        for c in value_cols:
            denom = merged[c].sum()
            both[c] = (both[c] / denom * 100).round(2) if denom else 0
        return both, "share"
    return both, "raw"


def export_multi_powerbi(sections: list[dict], shared: dict | None, merged: pd.DataFrame | None) -> bytes:
    """Zip with one fact CSV per database (+ a shared_key column), a SharedKeys table and merged comparison,
    DAX measures per table and a guide that wires the relationships."""
    buf = io.BytesIO()
    lines_dax = ["// DAX measures, one block per database table. Modeling > New measure > paste.", ""]
    rel_lines, files = [], []
    keys_all: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for sec in sections:
            alias, spec, fact = sec["alias"], sec["spec"], sec["fact"].copy()
            for c in fact.columns:
                if isinstance(fact[c].dtype, pd.CategoricalDtype):
                    fact[c] = fact[c].astype(str).replace("nan", "")
                if pd.api.types.is_datetime64_any_dtype(fact[c]):
                    fact[c] = fact[c].dt.date
            table = f"Fact_{alias}"
            if shared and alias in shared.get("fields", {}):
                fact["shared_key"] = fact[shared["fields"][alias]].astype(str).map(normalize_key)
                keys_all |= set(fact["shared_key"]) - {""}
                rel_lines.append(f"- `SharedKeys[shared_key]` (one) → `{table}[shared_key]` (many)")
            name = f"fact_{alias}.csv"
            z.writestr(name, fact.to_csv(index=False))
            files.append(f"| `{name}` | Fact table for {sec['label']} — load as **{table}** |")
            lines_dax.append(f"// ---- {sec['label']} ({table})")
            lines_dax += [ln for ln in dax_measures(spec, table).splitlines()[3:] if ln.strip()]
            lines_dax.append("")
        if shared:
            z.writestr("shared_keys.csv", pd.DataFrame({"shared_key": sorted(keys_all)}).to_csv(index=False))
            files.append("| `shared_keys.csv` | One row per shared value — load as **SharedKeys** |")
        if merged is not None and not merged.empty:
            z.writestr("merged_comparison.csv", merged.to_csv(index=False))
            files.append("| `merged_comparison.csv` | The side-by-side numbers QueryMind showed in Merged mode |")
        z.writestr("measures.dax", "\n".join(lines_dax) + "\n")
        field_txt = (", ".join(f"{s['label']}: `{shared['fields'][s['alias']]}`" for s in sections if s["alias"] in shared["fields"])
                     if shared else "none found")
        z.writestr("POWERBI_README.md", f"""# Multi-database dashboard for Power BI

Exported {datetime.now():%Y-%m-%d %H:%M} from QueryMind. Databases: {", ".join(s['label'] for s in sections)}.
Shared field: {field_txt}

## Files

| File | What it is |
|---|---|
{chr(10).join(files)}
| `measures.dax` | DAX measures for every table's KPI cards |

## Steps

1. Power BI Desktop → **Get data → Text/CSV**: load each `fact_*.csv` and rename it as shown above
   {"and load `shared_keys.csv` as **SharedKeys**" if shared else ""}.
2. **Model view** → create the relationships (single direction, from SharedKeys to each fact table):
{chr(10).join(rel_lines) or "- (no shared field — keep the tables separate, one report page each)"}
3. **Modeling → New measure**: paste the measures from `measures.dax`.
4. Build a page per database with its cards and charts (see each database's single-database guide in QueryMind),
   then a comparison page: a **slicer on SharedKeys[shared_key]** filters every database at once, and a
   **table or clustered bar** with SharedKeys[shared_key] on the axis and one measure per database compares them.
   When the measures have very different scales, use one chart per measure or show each as % of its total.
""")
    return buf.getvalue()
