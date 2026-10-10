# 🧠 QueryMind: Intelligent Database Assistant

Ask your database questions in plain English and get real answers. QueryMind reads your schema, writes correct
SQL for MySQL, PostgreSQL, SQLite (or any SQLAlchemy-supported database), runs it safely, and replies with a
plain-English answer, a labelled interactive chart and the result table. It remembers the conversation, reasons
through multi-hop questions step by step, can combine several databases in one answer, and learns from your
feedback.

Powered by **Groq `openai/gpt-oss-120b`** by default; any OpenAI-compatible model (OpenAI, a local Ollama model,
or another provider) can be selected from the sidebar.

See [PROJECT_STATEMENT.md](PROJECT_STATEMENT.md) for the problem statement and objectives, and
[test.md](test.md) for a 162-point test plan with expected answers.

---

## Contents

- [Feature overview](#feature-overview)
- [Features in detail](#features-in-detail)
- [Dashboard and Power BI](#dashboard-and-power-bi)
- [Example questions](#example-questions)
- [Real datasets included](#real-datasets-included)
- [Adding your own databases](#adding-your-own-databases)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [How it works](#how-it-works)
- [Project structure](#project-structure)
- [Testing](#testing)
- [Tech stack](#tech-stack)
- [Limitations](#limitations)

---

## Feature overview

| Area | What QueryMind does |
|---|---|
| Natural language → SQL | Writes dialect-correct SQL from your real schema, including date ranges and actual category values |
| Multi-hop reasoning | Answers chained questions with one CTE/sub-query, or step by step, feeding each result into the next query |
| Cross-database questions | Combines data from several databases in one answer through a temporary in-memory table |
| Context awareness | Follow-ups such as "now only for the USA" or "plot that by month" build on the previous query |
| Long-chat memory | Older messages are folded into a running summary, so context survives long conversations |
| Clarification | Asks when a question is genuinely ambiguous, with one-click answers; otherwise states its assumption |
| Self-correction | Reads database errors and rewrites the SQL automatically (up to 3 attempts) |
| Learning from feedback | 👍 answers become worked examples for similar questions; 👎 answers (with your note) become mistakes to avoid |
| Streaming | Live status line while it works; the answer types out word by word |
| Safety | Read-only by default, SQL parsing guardrails, confirm-before-write, row limits and timeouts |
| Charts | Auto-chosen, labelled, multi-colour, interactive Plotly charts with a chart-type switcher |
| Data sources | Six real datasets built in; add MySQL, PostgreSQL, SQLite, any SQLAlchemy URL, CSV/Excel uploads or a dataset URL |
| Chats | One conversation per database, saved to disk, reopenable, exportable, with a "clear all history" button |
| Transparency | The SQL, each reasoning step and an explanation are always one click away |
| Dashboard (Power BI style) | KPI cards, date and category slicers, click-to-filter charts, pinned chat answers, customizable layout |
| Multi-database dashboards | Side-by-side sections per database, or a merged view on a shared field found automatically (e.g. country, with "USA" = "United States") |
| Power BI export | One-click pack: fact.csv, Excel workbook, DAX measures, Power Query script and a step-by-step rebuild guide |

---

## Features in detail

### Understanding your data

- **Schema-aware SQL.** The model sees the tables, columns, keys and sample rows of the connected database, and
  writes SQL in the right dialect (SQLite, MySQL, PostgreSQL…).
- **Value profiling.** For each table QueryMind records real date ranges and the actual values of category columns
  (for example `'male'`/`'female'`, or `'S'`, `'C'`, `'Q'` for ports), so filters use values that exist.
- **Large schemas.** Only the tables relevant to the question, plus their join partners, are sent to the model.
  Base tables are preferred over views.
- **Relative dates.** "Last year" or "recent" is interpreted against the data's own latest date when the data
  ends before today, and the answer says so.

### Multi-hop reasoning

Many real questions chain facts together: "Where are the customers of the top-earning support agent from?"
first needs the top agent, then their customers, then a breakdown by country.

- **One query when possible.** The assistant first tries to answer with a single query using CTEs, sub-queries
  and joins.
- **Step by step when needed.** When a later step depends on a value that must be looked up first, it switches
  to a reasoning loop: run a query, read the result, and reuse the exact values (names, IDs, dates) in the next
  query. It uses up to 5 steps by default (adjustable).
- **Recovers from errors.** If a step fails, the next step corrects it.
- **Shows its work.** Answers include a **"How I worked it out"** section listing each step's purpose, its SQL
  and its first rows.
- **Remembered for follow-ups.** The steps are part of the conversation history, so "what about the second-best
  agent?" works afterwards.

### Questions across databases

Open **Combine with other databases** in the sidebar and pick extra databases (for example, add Titanic while
Chinook is connected). The header then shows "+ Titanic".

- Each reasoning step queries one database, in that database's own dialect.
- Every step result is copied into a temporary in-memory SQLite database as `step_1`, `step_2`, …
- A final query joins or compares those step tables, so data from different engines can be combined.
- "How I worked it out" labels each step with the database it used.

Example: *"Are there more Titanic passengers than Chinook customers?"* counts each database separately, then
compares them: 891 vs 59.

### Conversation and memory

- **Context-aware follow-ups.** "Show total sales by year" → "now only for the USA" → "which year was best?"
  Each follow-up modifies the previous SQL and keeps earlier filters unless you change them.
- **Recent history in full.** The last 8 exchanges (adjustable) are sent to the model with their SQL and result
  previews.
- **Running summary for long chats.** Older messages are folded into a ~180-word summary of topics, filters you
  settled on, key numbers and your stated preferences (such as "revenue means net of discounts"). It is updated
  every few messages, not on every one, and saved with the chat.
- **Clarifying questions.** Genuinely ambiguous requests ("show the best customers") get a question with
  clickable options. When a sensible default exists, it uses it and states the assumption.
- **Honest about gaps.** If the data can't answer the question (for example, profit when there is no cost data),
  it says what is missing and suggests the closest question it can answer.
- **Small talk and SQL help.** Greetings, "what tables are there?", "explain the last query" and "how could it
  be faster?" are answered without running anything.

### Learning from feedback

- Every answer with a result has 👍 / 👎 buttons.
- **👍** saves the question and its SQL as a verified example. Similar future questions on the same database
  are shown it as a worked example, and the answer is tagged "used 1 saved example".
- **👎** opens a box for an optional note ("revenue should exclude refunds"). The rejected SQL and your note are
  shown to the model as mistakes not to repeat.
- Examples are matched by word overlap, kept **per database** (never shared across databases), and stored in
  `data/feedback.json`. The sidebar shows how many 👍/👎 the current database has.

### Reliability and safety

- **Self-correcting SQL.** When the database returns an error, the error is sent back to the model with a wider
  view of the schema and the query is rewritten, up to 3 times. Answers that needed a fix are tagged
  "fixed itself after N retries".
- **Read-only by default.** SQL is parsed and classified with `sqlglot`. In read-only mode, INSERT, UPDATE,
  DELETE and schema changes are shown but never run.
- **Confirm before writing.** In write mode, data changes still need a **Run change** click, with warnings for
  risky statements (UPDATE/DELETE without WHERE, DROP).
- **Always blocked.** Multiple statements in one query, file access (`INTO OUTFILE`, `LOAD_FILE`), `SLEEP`, and
  privilege commands (GRANT, CREATE USER).
- **Limits.** A row limit (500 by default) and a per-query timeout on MySQL and PostgreSQL.
- **Secrets.** API keys live in `.env` and saved-connection passwords can be `${ENV_VAR}` placeholders. Both
  `.env` and `connections.json` are git-ignored.

### Results and charts

- **Plain-English answers** that quote the real numbers, plus the result table and a CSV download.
- **Automatic chart choice:** line for trends, bar for comparisons, donut for parts of a whole, scatter for two
  measures, histogram for distributions.
- **Labels on every chart:** values on bars, labels on line points (peak and latest value on long series),
  name and percentage on donut slices, counts on histograms, names on the top scatter points.
- **Colour:** a colour-blind-checked 8-colour palette. Each category or series gets its own colour; large bar
  charts shade light-to-dark blue by value; small donut slices are grouped into a grey "Other".
- **Interactive:** hover cards with exact values, click legend items to hide series, zoom and pan, a range
  slider with 3M / 1Y / All buttons on long time series, and PNG export.
- **Chart-type switcher:** turn any result into a bar, line, donut, scatter or distribution chart, offering only
  the types that suit the data.
- **Suggested follow-ups:** three clickable next questions after each answer.

### Streaming

- A live status line shows what the assistant is doing: "Reading the schema…", "Running the query…",
  "Fixing an error in the SQL (attempt 2)…", "Step 2: Count passengers (on titanic)", "Writing the answer…".
- The answer types out word by word; the follow-up suggestions appear when it finishes.

### Chats and sessions

- **One conversation per database.** Switching from Chinook to Titanic and back restores the Chinook chat
  exactly as you left it.
- **Saved automatically.** Chats are written to `sessions/` and the last chat for each database is restored when
  you restart the app.
- **Previous chats.** The list shows which database each chat belongs to. Opening a chat from another database
  switches to that database automatically.
- **New chat, Export** (Markdown with questions, answers and SQL), and a red **Clear all chat history** button
  that asks for confirmation. Clearing keeps the learned 👍/👎 examples.

### Interface

- Three tabs: **Chat**, **Dashboard** and **SQL workbench**. The selected tab stays put when you switch databases
  or click it again.
- A clean light theme with a navy sidebar and a large "Ask *Database* anything" header listing the tables and
  their row counts.
- Starter questions: curated for the bundled datasets, generated from the schema for any database you add.
- **Dashboard tab** (see [Dashboard and Power BI](#dashboard-and-power-bi)).
- **SQL workbench** to write and run SQL yourself (same safety rules) or have any query explained.
- Schema explorer, provider/model/reasoning-effort controls, a **Test model** button, and settings for write
  mode, row limit, retries, memory length and reasoning steps.
- Works on phone-sized screens.

---

## Dashboard and Power BI

The **Dashboard** tab turns the connected database into an interactive, Power BI-style report page.

**What's on it**

- **KPI cards** across the top (revenue, orders, customers, averages…). When a date range is selected, each card
  shows the change versus the previous period of the same length (▲ / ▼ %).
- **One filter row above everything:** a date range (all dates, latest 12 months, latest 3 months, or custom)
  followed by slicers for key categories. Every KPI and chart re-calculates from the same filtered data.
- **Click-to-filter charts:** click a bar or donut slice and every other visual filters to it, like Power BI
  cross-filtering. The clicked chart keeps its other categories faded so you can switch selection; click again
  or use **Clear filters** to undo. Active click filters are shown as chips.
- **Six charts per dashboard** using the same labelled, colour-blind-safe chart styles as the chat.
- **Pinned from chat:** any chat answer has a **Pin** button; pinned answers appear on the dashboard and re-run
  their SQL each time.

**Where the dashboard comes from**

| Database | Dashboard |
|---|---|
| Bundled datasets | Hand-built dashboards (in [`dashboards.json`](dashboards.json)), e.g. *Music store sales* for Chinook, *Titanic passenger survival* for Titanic |
| Any other database | Built automatically from the largest table's dates, numbers and categories |
| Any database, with a key | **Design with AI**: the model designs the fact query, KPIs, slicers and charts from your schema; every part is checked against real data before it's used |

**Customize** (expander under the dashboard): rename it, choose slicers, remove charts, add charts (type, axis,
value, aggregation, top N, split by), or reset to the default. Changes are saved per database in
`data/dashboards.json`.

### Several databases on one dashboard

The Dashboard tab has a mode switch at the top. Pick databases in **Compare with other databases** (next to the
switch, or under **Combine with other databases** in the sidebar; both stay in sync):

| Mode | What you see |
|---|---|
| **This database** | The normal single-database dashboard |
| **Side by side** (default) | One section per database, each with its own KPI cards, slicers, click-to-filter charts and change vs previous period. A shared date-range control applies to all of them, relative to each database's own latest date. |
| **Merged by a shared field** | QueryMind compares the actual values in every text column to find the field the databases share (for example `country` in Chinook, `customer_country` in Northwind and `country` in the COVID data), normalising names so "USA" = "United States", "UK" = "United Kingdom", "Czech Republic" = "Czechia". You then get KPI cards for the matched values, a grouped bar per shared value with one bar per database, a scatter of two databases' measures with their correlation, a coverage table (which values exist where), and a filter that applies to every database at once. |

In Merged mode you can change the matching field and the measure for each database. When the measures have very
different scales (dollars vs deaths, thousands vs millions), the chart automatically shows each database's share
of its own total, so everything fits on one honest axis; **Raw values** switches back to the real numbers.
Databases with nothing in common (for example Titanic and Chinook) are reported as such instead of being forced
together.

**Power BI pack (all databases):** one `fact_<database>.csv` per database with an added `shared_key` column, a
`shared_keys.csv` table, the merged comparison, DAX measures for every table, and a guide to link SharedKeys to each
fact table so one slicer filters every database in Power BI too.

**Under the hood:** each dashboard is a flat *fact query* (one row per event, joined to its dimensions) plus
KPI, slicer and chart definitions that aggregate it, exactly the shape Power BI works best with.

### Open it in Power BI

Click **Prepare Power BI pack** → **Download** (optionally only the filtered rows). The zip contains:

| File | Use in Power BI |
|---|---|
| `fact.csv` | Get data → Text/CSV. The flat table every visual is built from |
| `dashboard.xlsx` | The fact table (up to 100k rows) plus one sheet per KPI and chart with the numbers shown in QueryMind |
| `measures.dax` | Ready-made DAX measures for the KPI cards: Modeling → New measure → paste |
| `load_fact.pq` | Power Query (M) script that loads fact.csv with correct column types |
| `fact_query.sql` | The SQL behind the fact table, for a live connection (MySQL, PostgreSQL, SQL Server, or SQLite via ODBC) |
| `POWERBI_README.md` | Step-by-step instructions to rebuild this exact dashboard: which visual, axis, values, legend and slicers |

Rebuilding the page in Power BI Desktop takes about 10 minutes, and cross-filtering then works there too.

---

## Example questions

Answers below come from the bundled data; the full list with expected results is in [test.md](test.md).

| Dataset | Question | Kind | Answer |
|---|---|---|---|
| Chinook | Which 5 artists have generated the most revenue? | Join (4 tables) | Iron Maiden $138.60, U2 $105.93, Metallica $90.09, … |
| Chinook | Where are the customers of the top-earning support agent from? | Multi-hop | Jane Peacock's 21 customers: Canada 5, USA 3, … |
| Chinook | What is the best-selling genre, and who is its top artist? | Multi-hop | Rock; U2 ($90.09), not Iron Maiden, whose sales are split between Rock and Metal |
| Chinook | "Show total sales by year" → "now only for the USA" | Follow-up | 2012 is the best USA year at $127.98 |
| Northwind | Which customer placed the most orders, and what do they buy most? | Multi-hop | B's Beverages (210 orders), Schoggi Schokolade |
| Sakila | Who is the top-paying customer, and which category do they rent most? | Multi-hop | Karl Seal ($221.55), Animation |
| Titanic | In the class with the lowest survival rate, how many women survived? | Multi-hop | 3rd class: 72 of 144 |
| Tips | On the busiest-revenue day, compare lunch and dinner tips | Multi-hop, data quality | Saturday has no lunch service; dinner averages $2.99 |
| Chinook + Titanic | Are there more Titanic passengers than Chinook customers? | Cross-database | 891 vs 59 |
| Chinook + Northwind | Which countries have customers in both? | Cross-database | 17 countries |

---

## Real datasets included

Listed in [`datasets.json`](datasets.json) and downloaded automatically the first time you pick them (or all at
once with `python scripts/download_datasets.py`).

| id | Dataset | Type | What's inside |
|---|---|---|---|
| `chinook` *(default)* | [Chinook](https://github.com/lerocha/chinook-database): digital music store | SQLite, 11 tables | artists, albums, tracks, genres, customers, employees, invoices (2009–2013) |
| `northwind` | [Northwind](https://github.com/jpwhite3/northwind-SQLite3): trading company | SQLite, 13 tables + views | customers, orders, 600k+ order lines, products, suppliers, shippers |
| `sakila` | [Sakila](https://github.com/bradleygrant/sakila-sqlite3): DVD rental chain (MySQL's sample database) | SQLite, 16 tables + views | films, actors, inventory, stores, rentals, payments |
| `titanic` | [Titanic passengers](https://github.com/datasciencedojo/datasets) | CSV → table | 891 passengers: class, age, fare, survival |
| `covid_latest` | [Our World in Data COVID-19](https://github.com/owid/covid-19-data) | CSV → table | cases, deaths, vaccinations, GDP and population per country |
| `tips` | [Restaurant tips](https://github.com/mwaskom/seaborn-data) | CSV → table | bills, tips, party size, day, time |

---

## Adding your own databases

**1. From the app:** Sidebar → **Add a database**

| Option | What it does |
|---|---|
| MySQL / MariaDB, PostgreSQL | Host/port/user/password form; tested, then saved to `connections.json` |
| SQLite file | Any `.db` / `.sqlite` file on your computer |
| Any SQLAlchemy URL | SQL Server, Oracle, DuckDB, Snowflake, BigQuery… (install that driver with pip) |
| Upload CSV / Excel | Each CSV or Excel sheet becomes a table in a new SQLite database |
| Dataset from a URL | Any public `.csv`, `.xlsx` or `.db` link: downloaded and added to `datasets.json` |

**2. Edit `datasets.json`:** add an entry and it appears in the dataset list.

```json
{"id": "my_data", "name": "My data", "kind": "csv",
 "files": [{"url": "https://example.org/sales.csv", "table": "sales"}],
 "description": "…", "examples": ["Total sales by region?"]}
```

For `"kind": "sqlite"`, use `"url"` and `"file"` instead of `"files"`.

**3. Edit `connections.json`** (created when you save your first connection; git-ignored):

```json
{"connections": [{"name": "Prod replica", "kind": "mysql",
  "url": "mysql+pymysql://readonly:${PROD_DB_PASSWORD}@10.0.0.5:3306/shop"}]}
```

`${ENV_VAR}` placeholders are filled from environment variables, so passwords don't have to sit in the file.

> Tip: connect your own databases with a user that only has `SELECT` rights, for an extra layer of protection.

---

## Quick start

Requires Python 3.10+ and a free Groq API key from https://console.groq.com/keys.

**Windows (PowerShell or Command Prompt)**

```bash
git clone https://github.com/KANSADWALA/Query-Mind.git
cd Query-Mind
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Open `.env`, replace `your_groq_api_key_here` with your key, then:

```bash
streamlit run app.py
```

**macOS / Linux**

```bash
git clone https://github.com/KANSADWALA/Query-Mind.git
cd Query-Mind
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then add your key to .env
streamlit run app.py
```

The app opens at http://localhost:8501 on the Chinook dataset (downloaded automatically the first time).
Click **Test model** in the sidebar to confirm the key works, then try a starter question.

---

## Configuration

All settings live in `.env` (copy `.env.example`). Only the API key is required.

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `groq` | `groq`, `openai`, `ollama` or `custom` |
| `GROQ_API_KEY` | — | Your Groq key (or `OPENAI_API_KEY` / `LLM_API_KEY` for other providers) |
| `LLM_MODEL` | `openai/gpt-oss-120b` | Model ID, e.g. `openai/gpt-oss-20b` for faster answers |
| `LLM_REASONING_EFFORT` | `medium` | `low`, `medium` or `high` for reasoning models such as gpt-oss |
| `LLM_BASE_URL` | — | Endpoint for `custom` OpenAI-compatible providers |
| `DEFAULT_DATASET` | `chinook` | Dataset opened at start-up |
| `DATABASE_URL` | — | Connect straight to your own database (takes priority over `DEFAULT_DATASET`) |
| `READ_ONLY` | `true` | `false` allows data changes, each still needing a confirm click |
| `ROW_LIMIT` | `500` | Maximum rows returned per query |
| `MAX_SQL_RETRIES` | `3` | Self-correction attempts when a query fails |
| `MAX_REASONING_STEPS` | `5` | Step budget for multi-hop questions |
| `HISTORY_TURNS` | `8` | Recent exchanges kept in full; older ones are summarised |
| `QUERY_TIMEOUT_S` | `30` | Per-query timeout (MySQL / PostgreSQL) |
| `LLM_TEMPERATURE` | `0.1` | Sampling temperature |

Most of these can also be changed live in the sidebar (Language model, Safety and limits).

---

## How it works

```
User message
   │
   ▼
Update running summary (long chats) ─► pick relevant tables ─► find similar 👍/👎 examples
   │
   ▼
Planner LLM call: schema + value profiles + linked databases + examples + conversation history
   │
   ├── answer ────────► chat reply / schema info / SQL explanation
   ├── clarify ───────► question with clickable options
   ├── query ─────────► sqlglot safety check
   │                      ├── write/DDL → shown, runs only after "Run change" (write mode)
   │                      └── read → execute (row limit, timeout)
   │                                   └── error? → fix-SQL LLM call, retry (up to 3)
   └── multi_step ────► reasoning loop (up to 5 steps)
                          each step: choose database → SQL → run → result saved as scratch table step_n
                          final: query on a database or on the scratch tables to combine results
   │
   ▼
Streamed answer + follow-ups ─► labelled chart, table, SQL, reasoning steps ─► 👍/👎 feedback
```

---

## Project structure

```
app.py                        Streamlit UI: chat, workbench, sidebar, add-database forms, feedback, history
datasets.json                 Catalog of real datasets (extensible)
dashboards.json               Ready-made Power BI-style dashboards for the bundled datasets
.env.example                  Settings template; copy to .env and add your key
.streamlit/config.toml        Theme (colours, fonts)
core/
  agent.py                    Agent: plan → validate → execute → self-heal → summarise; multi-step and
                              cross-database reasoning; streaming; running-summary memory
  prompts.py                  Prompt templates
  db.py                       Connections, schema and value profiling, relevant-table selection, safe execution
  safety.py                   SQL parsing and classification guardrails (sqlglot)
  llm.py                      OpenAI-compatible client: JSON mode, streaming, reasoning effort, retries
  memory.py                   Conversation turns, history, running summary, session save/load
  examples.py                 Feedback store and similar-example retrieval (learning from 👍/👎)
  sources.py                  Dataset catalog (download/convert) and saved-connection registry
  charts.py                   Chart selection and labelled, colour-coded Plotly figures
  dashboard.py                Dashboard engine: fact data, filters, KPIs, aggregation, auto/AI design, Power BI export
  config.py                   Settings from .env
ui/theme.py                   CSS, brand mark and HTML snippets
ui/dashboard_view.py          The Dashboard tab: KPI cards, slicers, cross-filtering charts, pins, customize, export
scripts/download_datasets.py  Download all (or selected) catalog datasets
tests/test_core.py            43 automated tests on real data with a scripted fake LLM (no API key needed)
test.md                       Manual test plan: 162 checks with expected answers
PROJECT_STATEMENT.md          Problem statement and objectives
```

Created while you use the app (all git-ignored): `.env`, `connections.json`, `sessions/`, `data/datasets/`,
`data/uploads/`, `data/feedback.json`, `data/dashboards.json`.

---

## Testing

**Automated tests** (run offline with a scripted model, no API key needed):

```bash
python -m pytest -q
```

They cover SQL safety rules, schema profiling, follow-up context, self-correction, multi-hop reasoning,
cross-database questions, streaming, feedback learning, running-summary memory, per-database chats, chart labels
and colours, dashboards (KPIs, filters, auto and AI designs, pins, multi-database matching and merging) and the Power BI export
packs, and
dataset/connection management.

**Manual test plan:** [test.md](test.md) has 162 checks, including 10 questions per dataset with expected
answers computed from the data, plus tests for every feature above.

---

## Tech stack

Python 3.10+, Streamlit, SQLAlchemy, sqlglot, pandas, Plotly, OpenAI Python SDK (any OpenAI-compatible endpoint),
python-dotenv, openpyxl (Excel export), PyMySQL, psycopg2. Default model: Groq `openai/gpt-oss-120b`.
Power BI Desktop is only needed to open the optional export pack.

---

## Limitations

- Answers are only as good as the model; check the SQL for important decisions (it is always shown).
- Cross-database and multi-step answers make several model calls, so they are slower.
- Feedback examples are matched by word overlap, not meaning, so very differently worded questions may not
  pick up an example.
- Query timeouts apply to MySQL and PostgreSQL; SQLite queries rely on the row limit.
- Single-user app: there is no login or per-user permission system.
- Dashboards load their fact table into memory (capped at 1 million rows). Large databases take a few seconds the
  first time (Northwind, 609k rows: about 5 seconds), then stay cached for 15 minutes.
- Power BI can't import a ready-made report file from outside Power BI, so the export pack gives you the data,
  DAX measures, Power Query script and a step-by-step guide; rebuilding the visuals takes about 10 minutes.
- The automatic dashboard looks at the largest table only; use **Design with AI** for databases spread across
  many related tables.
- Merged dashboards match values exactly after normalising case, accents and common country-name variants. Other
  naming differences (e.g. product codes vs product names) need the matching field picked by hand.
