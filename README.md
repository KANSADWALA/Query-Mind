# 🧠 QueryMind — Intelligent Database Assistant

Ask your database questions in plain English. QueryMind turns them into correct MySQL / PostgreSQL / SQLite
(or any SQLAlchemy-supported) queries, runs them safely, and answers with a table, an automatic chart and a
short explanation — while remembering the conversation so follow-ups just work.

Powered by **Groq `openai/gpt-oss-120b`** by default (swap to any OpenAI-compatible model from the sidebar).
See [PROJECT_STATEMENT.md](PROJECT_STATEMENT.md) for the full problem statement and objectives.

## Features

- **Natural language → SQL**, aware of your real schema: tables, keys, sample rows, **date ranges and category
  values** (so filters like `Country = 'USA'` or `status = 'Shipped'` use real values)
- **Context-aware chat** — "now only for 2012", "break that down by genre", "plot it by month" refine the previous query
- **Asks before guessing** when a request is genuinely ambiguous (with one-click answers)
- **Multi-hop reasoning** — chained questions are answered with one CTE/sub-query where possible, or step by step:
  the assistant runs a query, reads the result, and uses those values in the next query (up to 5 steps, shown
  under "How I worked it out")
- **Cross-database questions** — pick extra databases under "Combine with other databases"; each reasoning step
  queries one database, results land in a temporary in-memory table (`step_1`, `step_2`…) and a final query joins them
- **Learns from feedback** — rate answers 👍/👎. Liked question→SQL pairs become worked examples for similar future
  questions on the same database; disliked ones (with your note) become "don't repeat this" hints (`data/feedback.json`)
- **Streaming answers** — a live status line (reading schema, running query, step 2 of 3…) and the answer types out
  word by word
- **Long-chat memory** — older messages are folded into a running summary (topics, filters, findings, your
  preferences) so follow-ups still work after dozens of turns
- **Self-healing SQL** — database errors are fed back to the model and the query is fixed automatically
- **Safety first** — read-only by default; SQL parsed with `sqlglot`; one statement at a time; file-access /
  sleep / GRANT always blocked; writes need write mode **and** a confirm click; row limits and timeouts
- **Results you can use** — table, auto-chosen Plotly chart, plain-English summary, CSV download, follow-up suggestions
- **Real datasets built in** and **any database you add** (see below), switchable from the sidebar
- **Large schemas** — only relevant tables (plus join partners) go to the model
- **Starter questions** — curated for bundled datasets, auto-generated from the schema for anything you add
- **One conversation per database** — switching databases parks the current chat and restores the other
  database's chat; chats auto-save, survive restarts, and opening an old chat reconnects its database; a red
  "Clear all chat history" button (with confirmation) deletes every saved chat
- **SQL Workbench** to run or explain SQL yourself; export any chat as Markdown

## Real datasets included

Listed in [`datasets.json`](datasets.json); downloaded automatically the first time you pick them
(or all at once with `python scripts/download_datasets.py`).

| id | Dataset | Type | What's inside |
|---|---|---|---|
| `chinook` *(default)* | [Chinook](https://github.com/lerocha/chinook-database) — digital music store | SQLite, 11 tables | artists, albums, tracks, genres, customers, employees, invoices |
| `northwind` | [Northwind](https://github.com/jpwhite3/northwind-SQLite3) — trading company | SQLite, 13 tables + views | customers, orders, 600k+ order lines, products, suppliers, shippers |
| `sakila` | [Sakila](https://github.com/bradleygrant/sakila-sqlite3) — DVD rental chain (MySQL's sample DB) | SQLite, 16 tables + views | films, actors, inventory, stores, rentals, payments |
| `titanic` | [Titanic passengers](https://github.com/datasciencedojo/datasets) | CSV → table | 891 passengers, class, age, fare, survival |
| `covid_latest` | [Our World in Data COVID-19](https://github.com/owid/covid-19-data) | CSV → table | cases, deaths, vaccinations, GDP, population per country |
| `tips` | [Restaurant tips](https://github.com/mwaskom/seaborn-data) | CSV → table | bills, tips, party size, day, time |

## Add any database — three ways

**1. From the app** — Sidebar → **➕ Add a database**:

| Option | What it does |
|---|---|
| MySQL / MariaDB, PostgreSQL | Host/port/user/password form; tested, then saved to `connections.json` |
| SQLite file | Any `.db` / `.sqlite` file on your computer |
| Any SQLAlchemy URL | SQL Server, Oracle, DuckDB, Snowflake, BigQuery… (install that driver with pip) |
| Upload CSV / Excel | Each CSV or Excel sheet becomes a table in a new SQLite database |
| Dataset from a URL | Any public `.csv`, `.xlsx` or `.db` link — downloaded and added to `datasets.json` |

**2. Edit `datasets.json`** — add an entry and it shows up in the dataset list:
```json
{"id": "my_data", "name": "My data", "kind": "csv",
 "files": [{"url": "https://example.org/sales.csv", "table": "sales"}],
 "description": "…", "examples": ["Total sales by region?"]}
```
(`kind` = `sqlite` uses `"url"` + `"file"` instead of `"files"`.)

**3. Edit `connections.json`** (created after you save your first connection; git-ignored):
```json
{"connections": [{"name": "Prod replica", "kind": "mysql",
  "url": "mysql+pymysql://readonly:${PROD_DB_PASSWORD}@10.0.0.5:3306/shop"}]}
```
`${ENV_VAR}` placeholders are filled from environment variables, so passwords don't have to sit in the file.

## Quick start (Windows)

```powershell
cd "D:\My Projects\Database-assistant"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
# create a file named .env containing:  GROQ_API_KEY=your_key   (free: console.groq.com/keys)
streamlit run app.py
```

macOS / Linux: same steps with `source .venv/bin/activate`. Open http://localhost:8501.

> Tip: connect your own MySQL with a user that only has `SELECT` rights for an extra layer of protection.

## How it works

```
User message
   │
   ▼
Relevant-table selection ──► Planner LLM call (schema + value profiles + conversation history)
                                   │
          ┌────────────────────────┼─────────────────────────┐
       clarify                   answer                     query
   (ask + options)        (chat / SQL help)                   │
                                                   sqlglot safety check
                                                 ┌────────────┴───────────┐
                                               read                 write / DDL
                                                 │                 confirm in UI
                                          execute (limit, timeout)
                                                 │ error?
                                          fix-SQL LLM call ◄─┐ (up to N retries)
                                                 │───────────┘
                                          summary LLM call + auto chart
                                                 │
                                          table · chart · answer · follow-ups
```

## Project structure

```
app.py                        Streamlit UI (chat, workbench, sidebar, add-database forms)
datasets.json                 Catalog of real datasets (extensible)
core/
  agent.py                    Conversational agent: plan → validate → execute → self-heal → summarise
  prompts.py                  Prompt templates
  db.py                       Connections, schema + value profiling, relevant tables, safe execution
  sources.py                  Dataset catalog (download/convert) and saved-connection registry
  safety.py                   SQL parsing/classification guardrails (sqlglot)
  llm.py                      OpenAI-compatible client (gpt-oss reasoning effort, JSON mode, retries)
  memory.py                   Conversation turns, LLM history, session save/load
  charts.py                   Automatic chart selection (Plotly)
  config.py                   Settings from environment / .env
scripts/download_datasets.py  Download all (or selected) catalog datasets
tests/test_core.py            Tests on the real Chinook data with a scripted fake LLM (no API key needed)
```

### `.env` settings (all optional except the key)

```
GROQ_API_KEY=your_key
LLM_MODEL=openai/gpt-oss-120b      # or openai/gpt-oss-20b
LLM_REASONING_EFFORT=medium        # low | medium | high
DEFAULT_DATASET=chinook            # chinook | northwind | sakila | titanic | covid_latest | tips
# DATABASE_URL=mysql+pymysql://user:password@localhost:3306/mydb
READ_ONLY=true
ROW_LIMIT=500
MAX_REASONING_STEPS=5              # multi-hop step budget
```

## Tests

```bash
python -m pytest -q
```
