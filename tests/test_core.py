"""Tests: safety rules, data sources, DB layer, memory, and the agent loop with a scripted fake LLM.

DB/agent tests run on the real Chinook dataset (downloaded once via datasets.json if it is not already present).
"""
import json
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.agent import SQLAgent  # noqa: E402
from core.charts import suggest_chart  # noqa: E402
from core.db import DatabaseManager  # noqa: E402
from core.llm import is_reasoning_model, parse_json  # noqa: E402
from core.memory import Conversation, Turn  # noqa: E402
from core.safety import add_limit, analyze  # noqa: E402
from core.sources import ConnectionStore, DatasetCatalog, SavedConnection, detect_kind, sqlite_url  # noqa: E402


class FakeLLM:
    """Returns queued responses in order and records every prompt it receives."""

    def __init__(self, *responses):
        self.responses = [json.dumps(r) if isinstance(r, dict) else r for r in responses]
        self.calls = []

    def chat(self, messages, json_mode=False, temperature=None):
        self.calls.append([dict(m) for m in messages])  # snapshot: the agent extends the same list
        return self.responses.pop(0)


@pytest.fixture(scope="session")
def chinook_path():
    try:
        url = DatasetCatalog().ensure("chinook")
    except Exception as e:  # offline and not downloaded yet
        pytest.skip(f"Chinook dataset unavailable: {e}")
    return Path(url.replace("sqlite:///", ""))


@pytest.fixture(scope="module")
def db(chinook_path):
    return DatabaseManager(sqlite_url(chinook_path))


# ---------------------------------------------------------------- safety
@pytest.mark.parametrize("sql", [
    "SELECT * FROM Invoice",
    "WITH t AS (SELECT 1 AS x) SELECT x FROM t",
    "SELECT Name FROM Artist UNION SELECT Name FROM Genre",
    "```sql\nSELECT 1;\n```",
    "SELECT * FROM Track WHERE Name = 'drop table'",
    'SELECT * FROM "Order Details"',
])
def test_reads_allowed(sql):
    r = analyze(sql, "sqlite", read_only=True)
    assert r.allowed and r.kind == "read", r.reason


@pytest.mark.parametrize("sql,kind", [
    ("DELETE FROM Invoice WHERE InvoiceId = 1", "write"),
    ("UPDATE Track SET UnitPrice = 1", "write"),
    ("DROP TABLE Invoice", "ddl"),
    ("INSERT INTO Genre VALUES (99, 'x')", "write"),
    ("ALTER TABLE Invoice ADD COLUMN x INT", "ddl"),
])
def test_writes_blocked_in_read_only(sql, kind):
    r = analyze(sql, "sqlite", read_only=True)
    assert not r.allowed and r.kind == kind


def test_write_mode_allows_but_warns():
    r = analyze("UPDATE Track SET UnitPrice = 1", "sqlite", read_only=False)
    assert r.allowed and any("WHERE" in w for w in r.warnings)


@pytest.mark.parametrize("sql", [
    "SELECT 1; DROP TABLE Invoice",
    "SELECT * FROM Invoice INTO OUTFILE '/tmp/x'",
    "SELECT SLEEP(10)",
    "GRANT ALL ON *.* TO 'x'",
])
def test_always_forbidden(sql):
    assert not analyze(sql, "mysql", read_only=False).allowed


def test_add_limit():
    assert add_limit("SELECT * FROM Invoice", 10, "sqlite").endswith("LIMIT 10")
    assert add_limit("SELECT * FROM Invoice LIMIT 5", 10, "sqlite") == "SELECT * FROM Invoice LIMIT 5"
    assert add_limit("SELECT COUNT(*) FROM Invoice", 10, "sqlite") == "SELECT COUNT(*) FROM Invoice"


# ------------------------------------------------------------------- llm
def test_llm_helpers():
    assert is_reasoning_model("openai/gpt-oss-120b") and not is_reasoning_model("llama3.1:8b")
    assert parse_json('Sure:\n```json\n{"a": 1}\n```') == {"a": 1}


# --------------------------------------------------------------- sources
def test_catalog_has_real_datasets():
    ids = {d.id for d in DatasetCatalog().all()}
    assert {"chinook", "northwind", "sakila", "titanic"} <= ids


def test_add_dataset_from_local_csv(tmp_path):
    csv = tmp_path / "sales data.csv"
    pd.DataFrame({"Region Name": ["West", "East", "West"], "Amount ($)": [10, 20, 30]}).to_csv(csv, index=False)
    catalog_file = tmp_path / "datasets.json"
    catalog_file.write_text('{"datasets": []}')
    cat = DatasetCatalog(catalog_file, tmp_path / "ds")
    ds = cat.add("My Sales", str(csv), table="sales")
    assert ds.id == "my_sales" and cat.get("my_sales").custom
    db = DatabaseManager(cat.ensure("my_sales"))
    assert list(db.get_schema()) == ["sales"]
    assert [c.name for c in db.get_schema()["sales"].columns] == ["region_name", "amount"]
    cat.remove("my_sales")
    assert cat.get("my_sales") is None


def test_connection_store(tmp_path, monkeypatch):
    store = ConnectionStore(tmp_path / "connections.json")
    store.save(SavedConnection("Prod", "mysql+pymysql://app:${DB_PW}@db:3306/shop", "mysql"))
    store.save(SavedConnection("Local", "mysql+pymysql://root:secret@localhost/shop", "mysql"))
    monkeypatch.setenv("DB_PW", "s3cr3t")
    assert store.get("Prod").resolved_url() == "mysql+pymysql://app:s3cr3t@db:3306/shop"
    assert "secret" not in store.get("Local").masked_url()
    store.remove("Local")
    assert [c.name for c in store.all()] == ["Prod"]
    assert detect_kind("https://x.org/a.csv?raw=1") == "csv" and detect_kind("b.sqlite") == "sqlite"


# -------------------------------------------------------------------- db
def test_schema_profile_and_execute(db):
    schema = db.get_schema()
    assert {"Invoice", "Customer", "Track", "InvoiceLine"} <= set(schema)
    assert ("CustomerId", "Customer", "CustomerId") in schema["Invoice"].foreign_keys
    inv_date = next(c for c in schema["Invoice"].columns if c.name == "InvoiceDate")
    assert inv_date.value_range[0].startswith("2009")
    text = db.schema_text()
    assert 'TABLE "Invoice"' in text and "FOREIGN KEY" in text and "-- range" in text
    res = db.execute("SELECT Country, COUNT(*) AS n FROM Customer GROUP BY Country", row_limit=100)
    assert res.ok and len(res.df) == 24
    assert not db.execute("DELETE FROM Invoice").ok  # read-only by default


def test_relevant_tables_camelcase(db):
    picked = db.relevant_tables("revenue per invoice line by genre", max_tables=5)
    assert "InvoiceLine" in picked and "Genre" in picked


def test_row_limit_truncates(db):
    res = db.execute("SELECT * FROM Track", row_limit=50)
    assert res.ok and len(res.df) == 50 and res.truncated


def test_literal_with_colon(db):
    res = db.execute("SELECT '10:30' AS t, 'a%b' AS p")
    assert res.ok and res.df.iloc[0]["t"] == "10:30"


# ---------------------------------------------------------------- agent
def test_agent_query_followup_and_memory(db):
    llm = FakeLLM(
        {"action": "query", "sql": "SELECT Country, COUNT(*) AS customers FROM Customer GROUP BY Country ORDER BY customers DESC",
         "explanation": "Counts customers per country.", "title": "Customers by country", "chart": {"type": "bar", "x": "Country", "y": "customers"}},
        {"answer": "The USA has the most customers (13).", "follow_ups": ["Which country spends the most?"]},
        {"action": "query", "sql": "SELECT State, COUNT(*) AS customers FROM Customer WHERE Country = 'USA' GROUP BY State",
         "explanation": "Same, for US states.", "title": "US states", "chart": {"type": "bar", "x": "State", "y": "customers"}},
        {"answer": "11 states.", "follow_ups": []},
    )
    agent = SQLAgent(db, llm)
    conv = Conversation()
    t1 = agent.respond(conv, "How many customers per country?")
    assert t1.action == "query" and t1.row_count == 24 and t1.chart["type"] == "bar" and t1.follow_ups

    t2 = agent.respond(conv, "break the USA down by state")
    assert t2.row_count == 11
    second_prompt = "\n".join(m["content"] for m in llm.calls[2])  # history carries the previous SQL
    assert "GROUP BY Country ORDER BY customers DESC" in second_prompt
    assert len(conv.turns) == 4


def test_agent_self_heals(db):
    llm = FakeLLM(
        {"action": "query", "sql": "SELECT Nme FROM Artist", "explanation": "x", "title": "t"},
        {"sql": "SELECT Name FROM Artist ORDER BY Name", "explanation": "Column is `Name`."},
        {"answer": "Here are the artists.", "follow_ups": []},
    )
    turn = SQLAgent(db, llm).respond(Conversation(), "list artists")
    assert turn.error == "" and turn.attempts == 2 and turn.row_count == 275
    assert "Auto-fixed" in turn.explanation


def test_agent_clarify_answer_and_suggest(db):
    llm = FakeLLM({"action": "clarify", "clarification": "Revenue or number of sales?", "options": ["Revenue", "Sales count"]},
                  {"action": "answer", "answer": "Hi! Ask me anything about your data."},
                  {"questions": ["Top genres by revenue?", "Sales by year?"]})
    agent, conv = SQLAgent(db, llm), Conversation()
    t = agent.respond(conv, "best genres")
    assert t.action == "clarify" and t.options == ["Revenue", "Sales count"]
    assert agent.respond(conv, "hello").action == "answer"
    assert agent.suggest_questions() == ["Top genres by revenue?", "Sales by year?"]


def test_agent_write_requires_confirmation(chinook_path, tmp_path):
    copy = tmp_path / "chinook_copy.sqlite"
    shutil.copyfile(chinook_path, copy)
    wdb = DatabaseManager(sqlite_url(copy))
    llm = FakeLLM({"action": "query", "sql": "UPDATE Track SET UnitPrice = UnitPrice * 1.1 WHERE GenreId = 1",
                   "explanation": "Raise rock prices 10%."},
                  {"action": "query", "sql": "UPDATE Track SET UnitPrice = 0 WHERE TrackId = 1", "explanation": "x"})
    agent = SQLAgent(wdb, llm)
    t = agent.respond(Conversation(), "raise rock track prices by 10%", read_only=False)
    assert t.action == "pending"
    agent.confirm_pending(t)
    assert t.row_count == 1297 and not t.error
    blocked = agent.respond(Conversation(), "set price 0", read_only=True)
    assert blocked.action == "answer" and "Read-only" in blocked.error


def test_conversation_persistence(tmp_path):
    conv = Conversation()
    conv.add(Turn(role="user", content="q"))
    t = conv.add(Turn(role="assistant", action="query", sql="SELECT 1"))
    t.set_df(pd.DataFrame({"a": [1, 2], "d": pd.to_datetime(["2024-01-01", "2024-02-01"])}))
    loaded = Conversation.load(conv.save(tmp_path))
    assert loaded.title == "q" and loaded.turns[1].df.shape == (2, 2)


def test_chart_heuristics():
    ts = pd.DataFrame({"month": ["2024-01", "2024-02", "2024-03"], "revenue": [1, 2, 3]})
    assert suggest_chart(ts)["type"] == "line"
    assert suggest_chart(pd.DataFrame({"city": ["A", "B"], "n": [1, 2]}))["type"] == "bar"
    assert suggest_chart(pd.DataFrame({"total": [5]})) is None


def test_charts_have_labels_and_palette():
    from core.charts import PALETTE, build_figure, chart_options

    bars = pd.DataFrame({"artist": ["A", "B", "C"], "revenue": [30.5, 20.25, 10.0]})
    fig = build_figure(bars, {"type": "bar", "x": "artist", "y": "revenue"}, "Top")
    assert all(t.texttemplate for t in fig.data)                      # value labels on bars
    assert [t.marker.color for t in fig.data] == PALETTE[:3]           # one hue per category, fixed order

    many = pd.DataFrame({"genre": [f"g{i}" for i in range(20)], "tracks": range(20, 0, -1)})
    assert build_figure(many, {"type": "bar", "x": "genre", "y": "tracks"}).data[0].orientation == "h"

    pie_df = pd.DataFrame({"country": [f"c{i}" for i in range(12)], "n": [30] + [5] * 11})
    pie = build_figure(pie_df, {"type": "pie", "x": "country", "y": "n"})
    assert "Other" in list(pie.data[0].labels) and len(pie.data[0].labels) <= len(PALETTE)

    ts = pd.DataFrame({"month": ["2024-01-01", "2024-02-01", "2024-03-01"], "revenue": [1.5, 2.5, 3.5]})
    line = build_figure(ts, {"type": "line", "x": "month", "y": "revenue"})
    assert "text" in line.data[0].mode                                  # few points -> every point labelled
    assert set(chart_options(bars, None)) >= {"bar", "pie"}


def test_agent_multi_hop_reasoning(db):
    llm = FakeLLM(
        {"action": "multi_step", "plan": ["Find the support rep with the highest sales", "List that rep's customers"]},
        {"action": "query", "purpose": "Top support rep by sales",
         "sql": "SELECT e.EmployeeId, e.FirstName, ROUND(SUM(i.Total),2) AS sales FROM Employee e JOIN Customer c "
                "ON c.SupportRepId = e.EmployeeId JOIN Invoice i ON i.CustomerId = c.CustomerId "
                "GROUP BY e.EmployeeId ORDER BY sales DESC LIMIT 1"},
        {"action": "query", "purpose": "Customers of employee 3", "sql": "SELECT Nme FROM Customer WHERE SupportRepId = 3"},
        {"action": "query", "purpose": "Customers of employee 3 (fixed)",
         "sql": "SELECT FirstName, LastName, Country FROM Customer WHERE SupportRepId = 3"},
        {"action": "final", "sql": "SELECT Country, COUNT(*) AS customers FROM Customer WHERE SupportRepId = 3 GROUP BY Country",
         "title": "Jane's customers by country", "explanation": "Jane Peacock had the most sales; these are her customers."},
        {"answer": "Jane Peacock sold the most ($833.04) and supports 21 customers.", "follow_ups": []},
    )
    conv = Conversation()
    turn = SQLAgent(db, llm).respond(conv, "Where are the customers of the top-selling support rep from?")
    assert [s["row_count"] for s in turn.steps] == [1, 0, 21]
    assert turn.steps[1]["error"]                                  # a failed step is recorded and corrected next step
    assert turn.steps[0]["rows"][0]["FirstName"] == "Jane"
    assert turn.row_count > 0 and "Jane" in turn.content and not turn.error
    step2_prompt = llm.calls[2][-1]["content"]                     # step 2 saw step 1's result
    assert "Jane" in step2_prompt and "833.04" in step2_prompt
    assert "[Reasoning steps]" in turn.history_text()              # follow-ups can refer to the steps


def test_conversation_per_database(tmp_path):
    a = Conversation(source_key="dataset:chinook", database="Chinook")
    a.add(Turn(role="user", content="top artists"))
    a.save(tmp_path)
    b = Conversation(source_key="dataset:titanic", database="Titanic")
    b.add(Turn(role="user", content="survival by class"))
    b.save(tmp_path)
    assert Conversation.latest_for(tmp_path, "dataset:chinook").title == "top artists"
    assert Conversation.latest_for(tmp_path, "dataset:sakila") is None
    assert {r["source_key"] for r in Conversation.list_saved(tmp_path)} == {"dataset:chinook", "dataset:titanic"}


class FakeStreamLLM(FakeLLM):
    def __init__(self, *responses, stream_text=""):
        super().__init__(*responses)
        self.stream_text = stream_text

    def stream(self, messages, temperature=None):
        for i in range(0, len(self.stream_text), 7):
            yield self.stream_text[i:i + 7]


def test_streaming_answer_and_status(db):
    llm = FakeStreamLLM(
        {"action": "query", "sql": "SELECT Name FROM Genre ORDER BY Name", "title": "Genres"},
        stream_text="There are **25 genres**, from Alternative to World.\n---FOLLOW-UPS---\nWhich genre sells most?\n- Tracks per genre?\n3. Genre revenue by year?",
    )
    tokens, statuses = [], []
    turn = SQLAgent(db, llm).respond(Conversation(), "list genres", on_status=statuses.append, on_token=tokens.append)
    assert len(tokens) > 3 and tokens[-1] == turn.content            # streamed progressively, final text matches
    assert all("FOLLOW" not in t for t in tokens)                     # the marker never flashes on screen
    assert turn.content.startswith("There are **25 genres**")
    assert turn.follow_ups == ["Which genre sells most?", "Tracks per genre?", "Genre revenue by year?"]
    assert "Running the query…" in statuses and "Writing the answer…" in statuses


def test_feedback_examples_are_used(db, tmp_path):
    from core.examples import ExampleStore

    store = ExampleStore(tmp_path / "feedback.json")
    store.record("dataset:chinook", "top 5 artists by revenue", "SELECT ar.Name ... LIMIT 5", good=True, turn_id="t1")
    store.record("dataset:chinook", "top artists by number of albums", "SELECT bad", good=False,
                 note="count albums, not tracks", turn_id="t2")
    store.record("dataset:titanic", "top artists by revenue", "SELECT other_db", good=True, turn_id="t3")
    assert store.stats("dataset:chinook") == (1, 1)
    llm = FakeLLM({"action": "answer", "answer": "ok"})
    turn = SQLAgent(db, llm, examples=store, source_key="dataset:chinook").respond(Conversation(), "top 10 artists by revenue")
    system = llm.calls[0][0]["content"]
    assert "SELECT ar.Name ... LIMIT 5" in system and "count albums, not tracks" in system
    assert "other_db" not in system                                     # examples never leak across databases
    store.record("dataset:chinook", "top 5 artists by revenue", "SELECT better", good=True, turn_id="t1")  # re-rate
    assert [e["sql"] for e in store.for_source("dataset:chinook") if e["good"]] == ["SELECT better"]


def test_cross_database_question(db):
    titanic = DatabaseManager(DatasetCatalog().ensure("titanic"))
    llm = FakeLLM(
        {"action": "multi_step", "plan": ["count Chinook customers", "count Titanic passengers", "compare"]},
        {"action": "query", "database": "chinook", "purpose": "Chinook customers", "sql": "SELECT COUNT(*) AS n FROM Customer"},
        {"action": "query", "database": "titanic", "purpose": "Titanic passengers", "sql": "SELECT COUNT(*) AS n FROM passengers"},
        {"action": "final", "database": "scratch", "title": "People in each database",
         "sql": "SELECT 'Chinook customers' AS source, n FROM step_1 UNION ALL SELECT 'Titanic passengers', n FROM step_2"},
        {"answer": "Titanic has 891 passengers vs 59 Chinook customers.", "follow_ups": []},
    )
    agent = SQLAgent(db, llm, linked={"titanic": titanic}, main_alias="chinook")
    turn = agent.respond(Conversation(), "Are there more Titanic passengers than Chinook customers?")
    assert "LINKED DATABASE titanic" in llm.calls[0][0]["content"]
    assert [s["database"] for s in turn.steps] == ["chinook", "titanic"]
    assert turn.row_count == 2 and not turn.error
    assert sorted(turn.df["n"].tolist()) == [59, 891]


def test_running_summary_memory(db):
    conv = Conversation()
    for i in range(10):
        conv.add(Turn(role="user", content=f"question {i} about genre sales"))
        conv.add(Turn(role="assistant", action="answer", content=f"answer {i}"))
    llm = FakeLLM("- Asked about genre sales 0-5; user prefers bar charts.", {"action": "answer", "answer": "ok"})
    agent = SQLAgent(db, llm, history_turns=2)
    agent.respond(conv, "and now by year?")
    assert conv.summary.startswith("- Asked about genre sales") and conv.summary_upto == 16
    planner_history = llm.calls[1][1]["content"]
    assert "Summary of our earlier conversation" in planner_history and "bar charts" in planner_history
    assert Conversation.load(conv.save(Path(__import__("tempfile").mkdtemp()))).summary == conv.summary


# ------------------------------------------------------------ dashboards
def test_builtin_dashboards_compute(chinook_path):
    from datetime import date
    from core.dashboard import Filters, builtin_spec, chart_frame, kpi_value, load_fact, previous_period

    db = DatabaseManager(sqlite_url(chinook_path))
    spec = builtin_spec("chinook")
    fact = load_fact(db, spec["fact_sql"], spec["date"])
    assert len(fact) == 2240
    kpis = {k["label"]: round(kpi_value(fact, k), 2) for k in spec["kpis"]}
    assert kpis["Revenue"] == 2328.6 and kpis["Invoices"] == 412 and kpis["Customers"] == 59
    usa_2012 = Filters(date_range=(date(2012, 1, 1), date(2012, 12, 31)), slicers={"country": ["USA"]}).apply(fact, "invoice_date")
    assert round(kpi_value(usa_2012, spec["kpis"][0]), 2) == 127.98         # matches test.md 7.1
    by_country = chart_frame(fact, spec["charts"][1])
    assert by_country.iloc[0].tolist() == ["USA", 523.06] and len(by_country) == 10
    assert previous_period((date(2013, 1, 1), date(2013, 12, 22)), (date(2009, 1, 1), date(2013, 12, 22)))[1] == date(2012, 12, 31)


def test_every_builtin_dashboard_is_valid():
    from core.charts import build_figure
    from core.dashboard import builtin_spec, chart_frame, chart_spec_for_figure, load_fact

    catalog = DatasetCatalog()
    for ds in ["titanic", "tips", "covid_latest"]:
        db = DatabaseManager(catalog.ensure(ds))
        spec = builtin_spec(ds)
        fact = load_fact(db, spec["fact_sql"], spec["date"])
        for chart in spec["charts"]:
            frame = chart_frame(fact, chart)
            assert not frame.empty, (ds, chart["title"])
            assert build_figure(frame, chart_spec_for_figure(chart, frame), chart["title"]) is not None


def test_auto_and_ai_dashboard_specs(db, tmp_path):
    from core.dashboard import DashboardStore, ai_spec, auto_spec

    auto = auto_spec(db)
    assert auto["kpis"] and auto["charts"] and auto["fact_sql"].startswith("SELECT")
    llm = FakeLLM({"title": "AI", "fact_sql": "SELECT InvoiceId AS invoice_id, BillingCountry AS country, Total AS total FROM Invoice",
                   "date": "nope", "kpis": [{"label": "Sales", "col": "total", "agg": "sum"}, {"label": "Bad", "col": "missing"}],
                   "slicers": ["country", "ghost"], "charts": [{"title": "By country", "type": "bar", "x": "country", "y": "total"},
                                                               {"title": "Broken", "type": "bar", "x": "nothing"}]})
    spec = ai_spec(db, llm, "SQLite")
    assert [k["label"] for k in spec["kpis"]] == ["Sales"] and spec["slicers"] == ["country"]
    assert [c["title"] for c in spec["charts"]] == ["By country"] and spec["date"] is None
    store = DashboardStore(tmp_path / "dash.json")
    store.save_spec("dataset:chinook", spec)
    store.pin("dataset:chinook", "Top", "SELECT 1", None, "q")
    assert store.get("dataset:chinook")["spec"]["title"] == "AI" and store.is_pinned("dataset:chinook", "SELECT 1")


def test_powerbi_export_pack(chinook_path):
    import io
    import zipfile

    from core.dashboard import builtin_spec, chart_frame, dax_measures, export_powerbi, load_fact

    spec = builtin_spec("chinook")
    fact = load_fact(DatabaseManager(sqlite_url(chinook_path)), spec["fact_sql"], spec["date"])
    pack = export_powerbi(spec, fact, {c["title"]: chart_frame(fact, c) for c in spec["charts"]},
                          [{"KPI": "Revenue", "Value": 2328.6}], "Chinook", filtered=False)
    names = set(zipfile.ZipFile(io.BytesIO(pack)).namelist())
    assert names == {"fact.csv", "dashboard.xlsx", "measures.dax", "load_fact.pq", "fact_query.sql", "POWERBI_README.md"}
    dax = dax_measures(spec)
    assert "Invoices = DISTINCTCOUNT('Fact'[invoice_id])" in dax and "DIVIDE(" in dax
    fact_csv = zipfile.ZipFile(io.BytesIO(pack)).read("fact.csv").decode()
    assert fact_csv.count("\n") == 2241


def test_multi_database_matching_and_merge():
    import io
    import zipfile

    from core.dashboard import (builtin_spec, comparison_view, export_multi_powerbi, find_shared_fields, load_fact,
                                merged_frame, normalize_key)

    assert normalize_key("USA") == normalize_key("United States") == "united states"
    assert normalize_key("Czech Republic") == normalize_key("Czechia") and normalize_key("UK") == "united kingdom"
    cat = DatasetCatalog()
    facts, specs = {}, {}
    for ds in ["chinook", "covid_latest", "titanic"]:
        specs[ds] = builtin_spec(ds)
        facts[ds] = load_fact(DatabaseManager(cat.ensure(ds)), specs[ds]["fact_sql"], specs[ds]["date"])
    found = find_shared_fields({k: facts[k] for k in ("chinook", "covid_latest")}, "chinook")
    assert found["fields"] == {"chinook": "country", "covid_latest": "country"} and found["overlap"]["covid_latest"] == 1.0
    assert find_shared_fields({k: facts[k] for k in ("chinook", "titanic")}, "chinook") == {}   # nothing in common
    merged = merged_frame({k: facts[k] for k in ("chinook", "covid_latest")}, found["fields"],
                          {"chinook": specs["chinook"]["kpis"][0], "covid_latest": specs["covid_latest"]["kpis"][2]},
                          {"chinook": "Chinook", "covid_latest": "COVID"})
    usa = merged[merged["shared_value"] == "USA"].iloc[0]
    assert usa["Chinook: Revenue"] == 523.06 and usa["COVID: Total deaths"] > 1_000_000
    view, mode = comparison_view(merged)
    assert mode == "share" and view.iloc[0]["shared_value"] == "USA"          # $ vs deaths: scales differ -> % share
    sections = [{"alias": k, "label": k.title(), "spec": specs[k], "fact": facts[k]} for k in ("chinook", "covid_latest")]
    names = zipfile.ZipFile(io.BytesIO(export_multi_powerbi(sections, found, merged))).namelist()
    assert {"fact_chinook.csv", "fact_covid_latest.csv", "shared_keys.csv", "merged_comparison.csv", "measures.dax"} <= set(names)
