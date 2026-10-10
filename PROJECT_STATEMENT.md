# Project Statement: QueryMind, an Intelligent, Context-Aware Database Assistant

## Original idea (as written)

> "I want to make a database assistant, which help users to deal with the data, to just write the prompt which gets automatically generate to SQL/MySQL query and gives the output, an intelligent chatbot which is context aware as well as all the things that a smart chatbot must have."

## Refined statement

**QueryMind** is an intelligent, conversational database assistant that lets users explore and analyse
relational data in plain English instead of SQL. A user types a request such as *"Show the top 5 customers by
revenue in 2012"*; the assistant reads the database schema, translates the request into correct,
dialect-specific SQL (MySQL, PostgreSQL, SQLite or any SQLAlchemy-supported engine), runs it safely, and replies
with a plain-English answer, a labelled interactive chart and the result table.

The assistant is **context-aware**: it remembers the conversation, so follow-ups like *"now only for the USA"*
or *"plot that by month"* refine the previous query instead of starting over, and long conversations are kept
in a running summary. It behaves like a capable data analyst: it reasons step by step through **multi-hop
questions**, combines data from **several databases** in one answer, asks for clarification when a request is
genuinely ambiguous, repairs its own SQL when the database returns an error, **learns from user feedback**,
explains its work, suggests useful next questions, and protects the data from accidental or destructive changes.

Beyond chat, QueryMind builds **Power BI-style interactive dashboards** for any connected database (or several
at once, side by side or merged on a shared field such as country) and exports them as a ready-to-use
**Power BI pack**.

## Problem

Most people who need answers from a database (managers, analysts, students, support teams) do not write SQL
fluently. They depend on developers for every new question, which is slow. Generic chatbots that "write SQL"
fail because they do not know the actual tables, columns and values, forget earlier messages, cannot chain
several lookups together, and may run dangerous statements. Building a dashboard is a separate, manual job in a
BI tool.

## Objectives

1. Convert natural-language questions into accurate, executable SQL using the live schema and real column values.
2. Maintain multi-turn conversational context so follow-up questions work naturally, including in long chats.
3. Answer multi-hop questions, either with a single query or through a step-by-step reasoning loop that reuses
   intermediate results, and answer questions that span more than one database.
4. Execute queries safely: read-only by default, statement validation, and explicit confirmation for any
   data-modifying operation.
5. Present results clearly: plain-English answers, labelled interactive charts, tables and exports.
6. Improve over time from user feedback (👍 / 👎) on each answer.
7. Work out of the box on real public datasets, and let users add any database dynamically (MySQL, PostgreSQL,
   SQLite, any SQLAlchemy engine, uploaded CSV/Excel files, or a dataset from a URL) without code changes.
8. Provide interactive dashboards for any database, single or combined, that can also be opened in Power BI.

## Key features

| Area | Feature |
|---|---|
| Understanding | Schema-aware NL→SQL with relevant-table selection for large schemas; dialect-specific SQL; value profiling (date ranges, category values) so filters use real values |
| Context and memory | Recent exchanges in full (with SQL and result previews) plus a running LLM summary of older messages; resolves follow-ups and pronouns ("those", "that") |
| Multi-hop reasoning | One query with CTEs/sub-queries when possible; otherwise a reasoning loop of up to N read-only queries, each reusing earlier results, shown as "How I worked it out" |
| Cross-database questions | Steps can query different databases; results are joined through an in-memory scratch database |
| Intelligence | Intent routing (data query, multi-step, clarification, or chat/SQL help); ambiguity detection with clickable options; honest "this data doesn't exist" answers |
| Reliability | Self-healing loop: SQL errors are fed back and the query is corrected automatically (up to N retries) |
| Learning from feedback | 👍 answers become worked examples for similar questions on the same database; 👎 answers (with a note) become mistakes to avoid |
| Safety | SQL parsed with `sqlglot`; single statement only; read-only by default; writes need write mode **and** a confirm click; file access, sleeps and privilege commands always blocked; row limits and timeouts |
| Output | Streamed plain-English answer, auto-chosen labelled and colour-blind-safe Plotly chart with a chart-type switcher, result table, CSV download, suggested follow-ups |
| Dashboards | Power BI-style tab: KPI cards with change vs previous period, date and category slicers, click-to-filter charts, pinned chat answers, customization, automatic or AI-designed layouts for any database |
| Multi-database dashboards | Side-by-side sections per database, or a merged view on a shared field found by comparing values (e.g. "USA" = "United States") |
| Power BI export | One-click pack: fact tables (CSV/Excel), DAX measures, Power Query script, linked shared-key table for multiple databases, and a step-by-step rebuild guide |
| Transparency | Generated SQL and every reasoning step always visible with an explanation; SQL workbench for manual runs and explanations |
| Real datasets | Built-in catalog of real public datasets (Chinook, Northwind, Sakila, Titanic, Our World in Data COVID-19, Tips), downloaded on demand |
| Dynamic data sources | Add MySQL / PostgreSQL / SQLite / any SQLAlchemy URL / CSV-Excel uploads / dataset-from-URL at runtime; saved connections with `${ENV_VAR}` secrets; extensible `datasets.json` |
| Sessions | One conversation per database, saved to disk and restored on restart; previous chats reopenable (switching database automatically); Markdown export; clear-all-history with confirmation |
| Flexibility | Groq `openai/gpt-oss-120b` by default (adjustable reasoning effort); any OpenAI-compatible endpoint, including OpenAI or a local model via Ollama |

## Scope

**In scope:** relational databases reachable by SQLAlchemy, real public datasets and user-supplied files,
English-language questions, interactive dashboards with Power BI export, a web UI built with Streamlit,
single-user sessions.

**Out of scope (future work):** user authentication and role-based access, NoSQL databases, voice input,
fine-tuned text-to-SQL models, multi-user collaboration, generating native Power BI `.pbix` files.

## Expected outcome

A working web application where a non-technical user can pick a real dataset or connect their own databases (or
upload a file), ask questions in plain English across a multi-turn conversation, including chained and
cross-database questions, and reliably get correct answers, charts and explanations, then explore the same data
on an interactive dashboard or in Power BI, without writing SQL and without risk to the data.

## Tech stack

Python 3.10+, Streamlit, SQLAlchemy, sqlglot, pandas, Plotly, OpenAI Python SDK (any OpenAI-compatible endpoint;
Groq `openai/gpt-oss-120b` by default), python-dotenv, openpyxl, PyMySQL, psycopg2. Power BI Desktop for the
optional export.
