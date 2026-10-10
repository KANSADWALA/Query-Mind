# Project Statement — QueryMind: An Intelligent, Context-Aware Database Assistant

## Original idea (as written)

> "I want to make a database assistant, which help users to deal with the data, to just write the prompt which gets automatically generate to SQL/MySQL query and gives the output, an intelligent chatbot which is context aware as well as all the things that a smart chatbot must have."

## Refined statement

**QueryMind** is an intelligent, conversational database assistant that lets users explore and
manage relational data using plain English instead of SQL. A user types a request such as
*"Show the top 5 customers by revenue in 2024"*; the assistant understands the database schema,
translates the request into a correct, dialect-specific SQL query (MySQL, PostgreSQL or SQLite),
executes it safely, and returns the result as a table, an automatically chosen chart, and a short
natural-language explanation.

The assistant is **context-aware**: it remembers the conversation, so follow-up questions like
*"now only for Pune"* or *"plot that by month"* refine the previous query instead of starting over.
It behaves like a capable data analyst — it asks for clarification when a request is ambiguous,
repairs its own SQL when the database returns an error, explains queries in simple terms, suggests
useful next questions, and protects the database from accidental or destructive changes.

## Problem

Most people who need answers from a database — managers, analysts, students, support teams —
do not write SQL fluently. They depend on developers for every new question, which is slow, and
generic chatbots that "write SQL" fail because they do not know the actual tables and columns,
forget earlier messages, and may run dangerous statements.

## Objectives

1. Convert natural-language questions into accurate, executable SQL using the live database schema.
2. Maintain multi-turn conversational context so follow-up questions work naturally.
3. Execute queries safely with read-only defaults, statement validation and explicit confirmation
   for any data-modifying operation.
4. Present results clearly: tables, automatic visualisations, plain-English summaries and exports.
5. Work out of the box on real public datasets, and let users add any database dynamically — MySQL, PostgreSQL,
   SQLite, any SQLAlchemy-supported engine, uploaded CSV/Excel files, or a dataset from a URL — without code changes.

## Key features

| Area | Feature |
|---|---|
| Understanding | Schema-aware NL→SQL with relevant-table selection for large schemas; dialect-specific SQL |
| Context | Conversation memory (previous questions, SQL and result previews) for follow-ups and pronouns ("those", "that") |
| Intelligence | Intent routing — data query vs. clarification vs. general/SQL-help chat; ambiguity detection |
| Reliability | Self-healing loop: on a SQL error, the error is fed back and the query is corrected automatically (up to N retries) |
| Safety | SQL parsed with `sqlglot`; single-statement only; read-only mode by default; writes (INSERT/UPDATE/DELETE/DDL) need explicit enablement **and** a confirm click; automatic row limits |
| Output | Result table, auto-generated Plotly chart, natural-language answer, suggested follow-up questions, CSV download |
| Transparency | Generated SQL always shown with an explanation; editable SQL workbench for manual runs |
| Real datasets | Built-in catalog of real public datasets (Chinook, Northwind, Sakila, Titanic, Our World in Data COVID-19, Tips), downloaded on demand |
| Dynamic data sources | Add MySQL / PostgreSQL / SQLite / any SQLAlchemy URL / CSV-Excel uploads / dataset-from-URL at runtime; saved connections with `${ENV_VAR}` secrets; extensible `datasets.json` |
| Schema intelligence | Value profiling (date ranges, categorical values) so filters use real values; auto-generated starter questions for any new database |
| Sessions | Chat history saved and reloadable; schema explorer with row counts and sample rows |
| Flexibility | Groq `openai/gpt-oss-120b` by default (adjustable reasoning effort); any OpenAI-compatible endpoint — OpenAI, or a local model via Ollama |

## Scope

**In scope:** relational databases reachable by SQLAlchemy, real public datasets and user-supplied files,
English-language questions, a web UI built with Streamlit, single-user sessions.
**Out of scope (future work):** user authentication/role-based access, NoSQL databases, voice input,
fine-tuned text-to-SQL models, multi-user collaboration.

## Expected outcome

A working web application where a non-technical user can pick a real dataset or connect their own database (or upload a file),
ask questions in plain English across a multi-turn conversation, and reliably get correct answers,
charts and explanations — without writing SQL and without risk to the data.

## Tech stack

Python 3.10+, Streamlit, SQLAlchemy, sqlglot, pandas, Plotly, OpenAI-compatible LLM client
(Groq `openai/gpt-oss-120b` by default), PyMySQL / psycopg2.
