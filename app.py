"""QueryMind — an intelligent, context-aware database assistant (Streamlit UI).

Run:  streamlit run app.py
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy.engine import URL

from core.agent import SQLAgent
from core.charts import CHART_LABELS, PLOTLY_CONFIG, build_figure, chart_options, suggest_chart
from core.config import PROVIDERS, SESSIONS_DIR, UPLOADS_DIR, Settings, key_setup_hint
from core.db import DatabaseManager
from core.examples import ExampleStore
from core.llm import LLMError, OpenAICompatibleLLM, is_reasoning_model
from core.memory import Conversation, Turn
from core.sources import (
    ConnectionStore, DatasetCatalog, SavedConnection, frames_to_sqlite, kind_from_url,
    read_table_file, slugify, sqlite_url,
)
from ui import theme
from ui.dashboard_view import dashboard_view
from core.dashboard import DashboardStore

st.set_page_config(page_title="QueryMind", page_icon=":material/database:", layout="wide")

st.markdown(theme.CSS, unsafe_allow_html=True)

KIND_LABELS = {"mysql": "MySQL", "postgresql": "PostgreSQL", "sqlite": "SQLite", "upload": "Uploaded files", "mssql": "SQL Server"}
ASSISTANT_AVATAR = ":material/database:"


# ===================================================================== state
def init_state() -> None:
    ss = st.session_state
    if "settings" not in ss:
        ss.settings = Settings()
    ss.setdefault("catalog", DatasetCatalog())
    ss.setdefault("store", ConnectionStore())
    ss.setdefault("conv", Conversation())
    ss.setdefault("convs", {})          # source_key -> Conversation, one per database
    ss.setdefault("examples", ExampleStore())
    ss.setdefault("linked_sel", {})     # source_key -> [source keys of extra databases to include]
    ss.setdefault("linked_mgrs", {})    # source key -> DatabaseManager (cache for cross-database questions)
    ss.setdefault("db", None)
    ss.setdefault("source", None)        # {"type": "dataset"|"connection"|"url", "id": ..., "label": ...}
    ss.setdefault("db_error", "")
    ss.setdefault("pending_prompt", None)
    ss.setdefault("starters", {})        # db url -> generated starter questions
    ss.setdefault("llm_cfg", {
        "provider": ss.settings.provider, "model": ss.settings.model, "api_key": ss.settings.api_key,
        "base_url": ss.settings.base_url, "reasoning_effort": ss.settings.reasoning_effort,
    })
    if ss.db is None and not ss.db_error and "booted" not in ss:
        ss.booted = True
        if ss.settings.database_url:
            connect_url(ss.settings.database_url, {"type": "url", "id": "env", "label": "DATABASE_URL"})
        elif ss.catalog.get(ss.settings.default_dataset):
            connect_dataset(ss.settings.default_dataset)


def source_key(source: dict | None) -> str:
    return f"{source['type']}:{source['id']}" if source else ""


def _activate(db: DatabaseManager, source: dict) -> None:
    """Make *db* the active database. Each database keeps its own conversation:
    the current chat is parked, and the target database's chat is restored (from this session, or the latest
    saved one on disk), so switching back and forth never loses history."""
    ss = st.session_state
    if ss.db is not None and ss.db is not db:
        ss.db.dispose()
    if ss.conv.turns:
        save_conv()
    if ss.conv.source_key and ss.conv.turns:
        ss.convs[ss.conv.source_key] = ss.conv
    ss.db, ss.source, ss.db_error = db, source, ""
    key = source_key(source)
    conv = ss.convs.get(key) or Conversation.latest_for(SESSIONS_DIR, key) or Conversation(source_key=key)
    conv.source_key = key
    conv.database = f"{source['label']} ({db.display_name})"
    ss.conv = conv
    ss.pop("wb_sql", None)


def connect_key(key: str) -> bool:
    """Reconnect to the database a saved chat belongs to."""
    kind, _, ident = key.partition(":")
    if kind == "dataset":
        return connect_dataset(ident)
    if kind == "connection":
        return connect_saved(ident)
    if kind == "url" and ident == "env" and st.session_state.settings.database_url:
        return connect_url(st.session_state.settings.database_url, {"type": "url", "id": "env", "label": "DATABASE_URL"})
    return False


def connect_url(url: str, source: dict) -> bool:
    ss = st.session_state
    try:
        db = DatabaseManager(url, ss.settings.query_timeout_s)
        db.test_connection()
        with st.spinner("Reading schema…"):
            db.get_schema(refresh=True)
        _activate(db, source)
        return True
    except ModuleNotFoundError as e:
        ss.db_error = f"Missing database driver: {e.name}. Install it with `pip install {e.name}`."
    except Exception as e:
        ss.db_error = f"Connection failed: {str(getattr(e, 'orig', e) or e).splitlines()[0][:300]}"
    return False


def connect_dataset(dataset_id: str) -> bool:
    ss = st.session_state
    ds = ss.catalog.get(dataset_id)
    if ds is None:
        ss.db_error = f"Unknown dataset {dataset_id}"
        return False
    try:
        if not ds.is_ready():
            bar = st.progress(0.0, text=f"Downloading {ds.name}…")
            url = ss.catalog.ensure(dataset_id, progress=lambda p: bar.progress(p, text=f"Downloading {ds.name}… {p:.0%}"))
            bar.empty()
        else:
            url = ss.catalog.ensure(dataset_id)
    except Exception as e:
        ss.db_error = f"Could not download {ds.name}: {e}"
        return False
    return connect_url(url, {"type": "dataset", "id": ds.id, "label": ds.name})


def connect_saved(name: str) -> bool:
    conn = st.session_state.store.get(name)
    if conn is None:
        return False
    return connect_url(conn.resolved_url(), {"type": "connection", "id": conn.name, "label": conn.name})


def get_agent() -> SQLAgent | None:
    ss = st.session_state
    cfg = ss.llm_cfg
    if ss.db is None:
        return None
    try:
        llm = OpenAICompatibleLLM(cfg["api_key"], cfg["model"], cfg["base_url"], ss.settings.temperature,
                                  reasoning_effort=cfg.get("reasoning_effort", "medium"))
    except LLMError:
        return None
    return SQLAgent(ss.db, llm, max_retries=ss.settings.max_retries, history_turns=ss.settings.history_turns,
                    row_limit=ss.settings.row_limit, max_steps=ss.settings.max_steps,
                    examples=ss.examples, source_key=ss.conv.source_key,
                    linked=linked_databases(), main_alias=alias_for(ss.conv.source_key, ss.source["label"]) if ss.source else "main")


def alias_for(key: str, label: str) -> str:
    """Short name the model uses to address a database: the dataset id, or a slug of the connection name."""
    kind, _, ident = key.partition(":")
    return (ident if kind == "dataset" else slugify(theme.short_name(label)))[:30] or "db"


def source_options() -> dict[str, str]:
    """Every database the user can pick: catalog datasets and saved connections, keyed by source key."""
    ss = st.session_state
    opts = {f"dataset:{d.id}": d.name for d in ss.catalog.all()}
    opts.update({f"connection:{c.name}": c.name for c in ss.store.all()})
    return opts


def _manager_for(key: str) -> DatabaseManager | None:
    ss = st.session_state
    if key in ss.linked_mgrs:
        return ss.linked_mgrs[key]
    kind, _, ident = key.partition(":")
    try:
        if kind == "dataset":
            url = ss.catalog.ensure(ident)
        elif kind == "connection" and ss.store.get(ident):
            url = ss.store.get(ident).resolved_url()
        else:
            return None
        mgr = DatabaseManager(url, ss.settings.query_timeout_s)
        mgr.get_schema()
    except Exception as e:
        st.toast(f"Couldn't open {ident}: {str(e)[:120]}", icon=":material/error:")
        return None
    ss.linked_mgrs[key] = mgr
    return mgr


def linked_source_list() -> list[dict]:
    """Extra databases included for this database, with labels: [{key, alias, label, db}]."""
    ss = st.session_state
    names = source_options()
    out = []
    for key in ss.linked_sel.get(ss.conv.source_key, []):
        if key == ss.conv.source_key or key not in names:
            continue
        mgr = _manager_for(key)
        if mgr is not None:
            out.append({"key": key, "alias": alias_for(key, names[key]), "label": theme.short_name(names[key]), "db": mgr})
    return out


def linked_databases() -> dict[str, DatabaseManager]:
    """Extra databases the user included for cross-database questions, as {alias: manager}."""
    ss = st.session_state
    out: dict[str, DatabaseManager] = {}
    names = source_options()
    for key in ss.linked_sel.get(ss.conv.source_key, []):
        if key == ss.conv.source_key or key not in names:
            continue
        mgr = _manager_for(key)
        if mgr is not None:
            out[alias_for(key, names[key])] = mgr
    return out


def save_conv() -> None:
    conv = st.session_state.conv
    if conv.turns:
        conv.save(SESSIONS_DIR)


def conv_to_markdown(conv: Conversation) -> str:
    out = [f"# {conv.title}\n", f"_Database: {conv.database}_\n"]
    for t in conv.turns:
        if t.role == "user":
            out.append(f"\n**You:** {t.content}\n")
        else:
            out.append(f"\n**QueryMind:** {t.content}\n")
            if t.sql:
                out.append(f"\n```sql\n{t.sql}\n```\n")
    return "\n".join(out)


# =================================================================== sidebar
def sidebar_database() -> None:
    ss = st.session_state
    catalog: DatasetCatalog = ss.catalog
    store: ConnectionStore = ss.store

    st.markdown('<div class="qm-side-label">Connected database</div>', unsafe_allow_html=True)
    if ss.source and ss.db is not None:
        st.markdown(theme.current_db_html(ss.source["label"], ss.db.display_name.replace(" · ", ", ")), unsafe_allow_html=True)
    else:
        st.markdown(theme.current_db_html("Nothing connected", "Pick a dataset below"), unsafe_allow_html=True)

    options: dict[str, str] = {}
    for ds in catalog.all():
        options[f"dataset:{ds.id}"] = ds.name + ("" if ds.is_ready() else " (download)")
    for c in store.all():
        options[f"conn:{c.name}"] = f"{c.name} ({KIND_LABELS.get(c.kind, c.kind or 'database')})"
    current = f"{'dataset' if ss.source and ss.source['type'] == 'dataset' else 'conn'}:{ss.source['id']}" if ss.source else None
    keys = list(options)
    choice = st.selectbox("Switch database", keys, format_func=options.get,
                          index=keys.index(current) if current in keys else None, placeholder="Choose a dataset or connection…")
    if choice:
        kind, ident = choice.split(":", 1)
        if kind == "dataset":
            ds = catalog.get(ident)
            st.caption(ds.description)
            if ds.source:
                st.caption(f"[Source]({ds.source}), license: {ds.license or 'see source'}")
        else:
            st.caption(f"`{store.get(ident).masked_url()}`")
        c1, c2 = st.columns([3, 1])
        if c1.button("Connect", type="primary", width="stretch", disabled=choice == current):
            ok = connect_dataset(ident) if kind == "dataset" else connect_saved(ident)
            if ok:
                st.rerun()
        removable = kind == "conn" or (catalog.get(ident) and catalog.get(ident).custom)
        if c2.button("", icon=":material/delete:", help="Remove from the list", width="stretch", disabled=not removable):
            catalog.remove(ident) if kind == "dataset" else store.remove(ident)
            st.rerun()
    if ss.db_error:
        st.error(ss.db_error)

    if ss.db is not None and ss.conv.source_key:
        names = {k: v for k, v in source_options().items() if k != ss.conv.source_key}
        current_sel = [k for k in ss.linked_sel.get(ss.conv.source_key, []) if k in names]
        with st.expander(f"Combine with other databases ({len(current_sel)})", icon=":material/join:"):
            st.caption("Chat questions can then use these databases too, and the Dashboard tab can show them side by "
                       "side or merged on a field they share (such as country).")
            picked = st.multiselect("Also include", list(names), default=current_sel, format_func=names.get,
                                    placeholder="Pick databases…", label_visibility="collapsed")
            if picked != current_sel:
                ss.linked_sel[ss.conv.source_key] = picked
                with st.spinner("Opening databases…"):
                    linked_databases()
                st.rerun()

    with st.expander("Add a database", icon=":material/add_circle:"):
        sidebar_add_database()


def sidebar_add_database() -> None:
    ss = st.session_state
    store: ConnectionStore = ss.store
    kind = st.selectbox("Type", ["MySQL / MariaDB", "PostgreSQL", "SQLite file", "Any SQLAlchemy URL",
                                 "Upload CSV / Excel", "Dataset from a URL"])

    def finish(name: str, url: str, conn_kind: str, remember: bool) -> None:
        if connect_url(os.path.expandvars(url), {"type": "connection" if remember else "url", "id": name, "label": name}):
            if remember:
                store.save(SavedConnection(name=name, url=url, kind=conn_kind))
            st.rerun()
        else:
            st.error(ss.db_error)

    if kind in ("MySQL / MariaDB", "PostgreSQL"):
        mysql = kind.startswith("MySQL")
        with st.form(f"add_{kind}"):
            name = st.text_input("Name", placeholder="e.g. Sales DB (prod replica)")
            c1, c2 = st.columns([3, 1])
            host = c1.text_input("Host", "localhost")
            port = c2.number_input("Port", value=3306 if mysql else 5432, step=1)
            user = st.text_input("User", "root" if mysql else "postgres")
            pwd = st.text_input("Password", type="password", help="Tip: type ${MY_DB_PASSWORD} to read it from an environment variable.")
            dbname = st.text_input("Database")
            remember = st.checkbox("Save this connection", value=True, help="Saved in connections.json on this computer (git-ignored).")
            if st.form_submit_button("Connect", width="stretch", type="primary"):
                driver = "mysql+pymysql" if mysql else "postgresql+psycopg2"
                if pwd.startswith("${"):
                    url = f"{driver}://{user}:{pwd}@{host}:{int(port)}/{dbname}"
                else:
                    url = URL.create(driver, username=user, password=pwd or None, host=host, port=int(port),
                                     database=dbname).render_as_string(hide_password=False)
                finish(name or f"{dbname}@{host}", url, "mysql" if mysql else "postgresql", remember)

    elif kind == "SQLite file":
        with st.form("add_sqlite"):
            path = st.text_input("Path to the .db / .sqlite file", placeholder=r"D:\data\shop.db")
            name = st.text_input("Name", placeholder="optional")
            remember = st.checkbox("Save this connection", value=True)
            if st.form_submit_button("Connect", width="stretch", type="primary") and path:
                p = Path(path.strip().strip('"'))
                if not p.exists():
                    st.error("File not found.")
                else:
                    finish(name or p.stem, sqlite_url(p), "sqlite", remember)

    elif kind == "Any SQLAlchemy URL":
        with st.form("add_url"):
            st.caption("Works with any database that has a SQLAlchemy driver installed — e.g. "
                       "`mssql+pyodbc://…`, `oracle+oracledb://…`, `duckdb:///file.duckdb`, `snowflake://…`.")
            url = st.text_input("URL", type="password", placeholder="dialect+driver://user:password@host:port/database")
            name = st.text_input("Name")
            remember = st.checkbox("Save this connection", value=True)
            if st.form_submit_button("Connect", width="stretch", type="primary") and url:
                finish(name or kind_from_url(url), url, kind_from_url(url), remember)

    elif kind == "Upload CSV / Excel":
        files = st.file_uploader("Each CSV (or Excel sheet) becomes a table", type=["csv", "tsv", "xlsx", "xls"],
                                 accept_multiple_files=True)
        name = st.text_input("Name for this database", placeholder="e.g. Marketing data")
        if st.button("Create database", width="stretch", type="primary", disabled=not files):
            frames: dict[str, pd.DataFrame] = {}
            UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
            for f in files:
                tmp = UPLOADS_DIR / f.name
                tmp.write_bytes(f.getvalue())
                fkind = "excel" if f.name.lower().endswith((".xlsx", ".xls")) else "csv"
                for sheet, df in read_table_file(tmp, fkind).items():
                    frames[sheet if fkind == "csv" or len(files) == 1 else f"{tmp.stem}_{sheet}"] = df
                tmp.unlink(missing_ok=True)
            label = name or Path(files[0].name).stem
            db_path = frames_to_sqlite(frames, UPLOADS_DIR / f"{slugify(label)}.db")
            finish(label, sqlite_url(db_path), "upload", True)

    else:  # Dataset from a URL
        with st.form("add_dataset"):
            st.caption("Any public link to a .csv, .xlsx or .db/.sqlite file (GitHub raw links, data portals…). "
                       "It is downloaded and added to datasets.json so it appears in the list for everyone using this folder.")
            url = st.text_input("File URL")
            name = st.text_input("Name")
            c1, c2 = st.columns(2)
            ftype = c1.selectbox("Type", ["auto", "csv", "excel", "sqlite"])
            table = c2.text_input("Table name", placeholder="optional")
            desc = st.text_input("Description", placeholder="optional")
            if st.form_submit_button("Download & connect", width="stretch", type="primary") and url and name:
                try:
                    with st.spinner("Downloading…"):
                        ds = ss.catalog.add(name, url.strip(), "" if ftype == "auto" else ftype, table, desc)
                    if connect_dataset(ds.id):
                        st.rerun()
                except Exception as e:
                    st.error(f"Could not add dataset: {e}")


def sidebar() -> None:
    ss = st.session_state
    s: Settings = ss.settings
    with st.sidebar:
        st.markdown(theme.brand_html(), unsafe_allow_html=True)
        sidebar_database()
        st.markdown('<div class="qm-side-label">Assistant</div>', unsafe_allow_html=True)

        # ---- model
        cfg = ss.llm_cfg
        with st.expander("Language model", icon=":material/psychology:", expanded=not cfg["api_key"]):
            providers = list(PROVIDERS) + ["custom"]
            provider = st.selectbox("Provider", providers, index=providers.index(cfg["provider"]) if cfg["provider"] in providers else 0)
            if provider != cfg["provider"]:
                preset = PROVIDERS.get(provider, {})
                key_env = preset.get("key_env", "")
                cfg.update(provider=provider, model=preset.get("model", cfg["model"]),
                           base_url=preset.get("base_url", cfg["base_url"]),
                           api_key="ollama" if provider == "ollama" else os.getenv(key_env, "") if key_env else "")
                ss.pop("api_key_input", None)
                ss.pop("llm_test", None)
            models = PROVIDERS.get(provider, {}).get("models", [])
            choices = models + ["Other…"]
            idx = choices.index(cfg["model"]) if cfg["model"] in choices else len(choices) - 1
            picked = st.selectbox("Model", choices, index=idx)
            if picked == "Other…":
                cfg["model"] = st.text_input("Model ID", cfg["model"], placeholder="e.g. qwen/qwen3-32b").strip()
            else:
                cfg["model"] = picked
            if is_reasoning_model(cfg["model"]):
                efforts = ["low", "medium", "high"]
                cfg["reasoning_effort"] = st.select_slider("Reasoning effort", efforts,
                                                           value=cfg.get("reasoning_effort", "medium") if cfg.get("reasoning_effort") in efforts else "medium",
                                                           help="Higher = more careful SQL on complex questions, but slower.")
            if provider == "custom":
                cfg["base_url"] = st.text_input("Base URL (OpenAI-compatible)", cfg["base_url"])
            if provider != "ollama":
                if "api_key_input" not in ss:
                    ss.api_key_input = cfg["api_key"]
                st.text_input("API key", key="api_key_input", type="password",
                              help="Kept only in this browser session. Put it in .env to avoid re-entering.")
                cfg["api_key"] = ss.api_key_input.strip()
                st.caption("Key loaded" if cfg["api_key"] else "No key yet. Free Groq keys: console.groq.com/keys")
            if st.button("Test model", icon=":material/bolt:", width="stretch", disabled=not cfg["api_key"]):
                try:
                    llm = OpenAICompatibleLLM(cfg["api_key"], cfg["model"], cfg["base_url"],
                                              reasoning_effort=cfg.get("reasoning_effort", "low"))
                    with st.spinner("Contacting model…"):
                        llm.chat([{"role": "user", "content": "Reply with the single word: OK"}])
                    ss.llm_test = ("ok", f"Connected to {cfg['model']}")
                except LLMError as e:
                    ss.llm_test = ("err", str(e))
            if ss.get("llm_test"):
                (st.success if ss.llm_test[0] == "ok" else st.error)(ss.llm_test[1])

        if ss.conv.source_key:
            good_n, bad_n = ss.examples.stats(ss.conv.source_key)
            if good_n or bad_n:
                st.caption(f"Learning from your feedback on this database: {good_n} 👍, {bad_n} 👎")

        # ---- settings
        with st.expander("Safety and limits", icon=":material/tune:"):
            write_mode = st.toggle("Allow data changes (write mode)", value=not s.read_only,
                                   help="When on, INSERT/UPDATE/DELETE/DDL can be generated — each still needs your confirmation.")
            s.read_only = not write_mode
            if write_mode:
                st.markdown(theme.caution_html("Write mode is on. Every change still needs your confirmation."), unsafe_allow_html=True)
            s.row_limit = st.slider("Max rows returned", 50, 5000, s.row_limit, step=50)
            s.max_retries = st.slider("Self-correction attempts", 0, 5, s.max_retries)
            s.history_turns = st.slider("Conversation memory (turns)", 2, 20, s.history_turns)
            s.max_steps = st.slider("Max reasoning steps (multi-hop questions)", 2, 8, s.max_steps)

        # ---- schema explorer
        if ss.db is not None:
            schema = ss.db.get_schema()
            with st.expander(f"Schema ({len(schema)} tables and views)", icon=":material/account_tree:"):
                if st.button("Refresh schema", icon=":material/refresh:", width="stretch"):
                    ss.db.get_schema(refresh=True)
                    st.rerun()
                for t in schema.values():
                    rows = f", {t.row_count:,} rows" if t.row_count is not None else ""
                    kind = "view" if t.is_view else "table"
                    st.markdown(f"**{t.name}** <span style='color:#9FB0CF;font-size:.8rem'>{kind}{rows}</span>", unsafe_allow_html=True)
                    st.caption(", ".join(f"{c.name} (key)" if c.primary_key else c.name for c in t.columns))

        # ---- chats
        st.divider()
        c1, c2 = st.columns(2)
        if c1.button("New chat", icon=":material/edit_square:", width="stretch"):
            save_conv()
            ss.conv = Conversation(database=ss.conv.database, source_key=ss.conv.source_key)
            st.rerun()
        c2.download_button("Export", conv_to_markdown(ss.conv), file_name=f"{ss.conv.id}.md", icon=":material/download:",
                           width="stretch", disabled=not ss.conv.turns)
        saved = [r for r in Conversation.list_saved(SESSIONS_DIR) if r["path"].stem != ss.conv.id]
        if saved:
            here = [r for r in saved if r["source_key"] == ss.conv.source_key]
            elsewhere = [r for r in saved if r["source_key"] != ss.conv.source_key]
            rows = {str(r["path"]): r for r in here + elsewhere}

            def chat_label(k: str) -> str:
                r = rows[k]
                db = theme.short_name(r["database"]) if r["database"] else "unknown database"
                return f"{r['title'][:38]}  ({db})"

            choice = st.selectbox(f"Previous chats ({len(here)} for this database)", list(rows), format_func=chat_label,
                                  index=None, placeholder="Open a previous chat…")
            if choice:
                r = rows[choice]
                other_db = r["source_key"] and r["source_key"] != ss.conv.source_key
                if other_db:
                    st.caption(f"Opening this switches to {theme.short_name(r['database'])}.")
                if st.button("Open chat", icon=":material/history:", width="stretch"):
                    save_conv()
                    if other_db and not connect_key(r["source_key"]):
                        st.error(ss.db_error or "Could not reconnect to that chat's database.")
                    else:
                        ss.conv = Conversation.load(r["path"])
                        st.rerun()

        sidebar_clear_history()


def clear_all_history() -> int:
    """Delete every saved chat (all databases) and reset the open conversation. Learned 👍/👎 examples are kept."""
    ss = st.session_state
    removed = 0
    if SESSIONS_DIR.exists():
        for path in SESSIONS_DIR.glob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    ss.convs = {}
    ss.conv = Conversation(database=ss.conv.database, source_key=ss.conv.source_key)
    for key in [k for k in ss.keys() if str(k).startswith(("fb_", "fbn_", "ct_", "opt_", "fu_"))]:
        del ss[key]
    ss.pending_prompt = None
    return removed


def sidebar_clear_history() -> None:
    ss = st.session_state
    has_history = bool(ss.conv.turns) or bool(Conversation.list_saved(SESSIONS_DIR))
    with st.container(key="danger_zone"):
        if not ss.get("confirm_clear"):
            if st.button("Clear all chat history", icon=":material/delete_forever:", width="stretch",
                         disabled=not has_history, key="clear_history"):
                ss.confirm_clear = True
                st.rerun()
        else:
            st.markdown(theme.caution_html("This permanently deletes every saved chat for all databases. "
                                           "Learned 👍/👎 examples are kept."), unsafe_allow_html=True)
            c1, c2 = st.columns(2)
            if c1.button("Delete all", icon=":material/delete_forever:", width="stretch", key="clear_confirm"):
                n = clear_all_history()
                ss.confirm_clear = False
                st.toast(f"Deleted {n} saved chat{'s' if n != 1 else ''}.", icon=":material/delete:")
                st.rerun()
            if c2.button("Cancel", width="stretch", key="clear_cancel"):
                ss.confirm_clear = False
                st.rerun()


# ==================================================================== render
def render_result(turn: Turn, key: str) -> None:
    df = turn.df
    if df is None or df.empty:
        return
    spec = turn.chart or suggest_chart(df)
    options = chart_options(df, spec)
    tabs = st.tabs(["Chart", "Table"] if options else ["Table"])
    if options:
        with tabs[0]:
            kinds = list(options)
            choice = kinds[0]
            if len(kinds) > 1:
                choice = st.segmented_control("Chart type", kinds, format_func=CHART_LABELS.get, default=kinds[0],
                                              key=f"ct_{key}", label_visibility="collapsed") or kinds[0]
            fig = build_figure(df, options[choice], turn.title)
            if fig is not None:
                st.plotly_chart(fig, width="stretch", key=f"chart_{key}_{choice}", config=PLOTLY_CONFIG)
            else:
                st.caption("This result can't be drawn as that chart type. Try another or use the table.")
    with tabs[-1]:
        st.dataframe(df, width="stretch", hide_index=True)
    can_pin = key != "workbench" and bool(turn.sql) and bool(st.session_state.conv.source_key)
    c1, c2, c3 = st.columns([4, 1.2, 1], vertical_alignment="center")
    note = f"Showing the first {len(df):,} rows (row limit)." if turn.truncated else f"{turn.row_count:,} rows"
    c1.caption(note)
    if can_pin:
        store = st.session_state.setdefault("dash_store", DashboardStore())
        source = st.session_state.conv.source_key
        if store.is_pinned(source, turn.sql):
            c2.button("Pinned", icon=":material/keep:", key=f"pin_{key}", width="stretch", disabled=True,
                      help="On the Dashboard tab")
        elif c2.button("Pin", icon=":material/push_pin:", key=f"pin_{key}", width="stretch",
                       help="Add this answer to the Dashboard tab"):
            chart = turn.chart or suggest_chart(df)
            store.pin(source, turn.title or turn.question or "Pinned answer", turn.sql, chart, turn.question)
            st.toast("Pinned to the dashboard", icon=":material/push_pin:")
            st.rerun()
    c3.download_button("CSV", df.to_csv(index=False).encode(), icon=":material/download:",
                       file_name=f"{(turn.title or 'result').replace(' ', '_')}.csv", key=f"dl_{key}", width="stretch")


def _on_feedback(turn: Turn, question: str) -> None:
    ss = st.session_state
    value = ss.get(f"fb_{turn.id}")
    turn.feedback = value
    if value is None:
        ss.examples.remove(turn.id)
    else:
        ss.examples.record(ss.conv.source_key, question, turn.sql, good=value == 1, note=turn.feedback_note, turn_id=turn.id)
    save_conv()


def _on_feedback_note(turn: Turn, question: str) -> None:
    ss = st.session_state
    turn.feedback_note = ss.get(f"fbn_{turn.id}", "")
    if turn.feedback == 0:
        ss.examples.record(ss.conv.source_key, question, turn.sql, good=False, note=turn.feedback_note, turn_id=turn.id)
    save_conv()


def render_feedback(turn: Turn, question: str) -> None:
    ss = st.session_state
    key = f"fb_{turn.id}"
    if key not in ss and turn.feedback is not None:
        ss[key] = turn.feedback
    c1, c2 = st.columns([1, 6], vertical_alignment="center")
    with c1:
        st.feedback("thumbs", key=key, on_change=_on_feedback, args=(turn, question))
    if turn.feedback == 1:
        c2.caption("Saved as a verified example. Similar questions will follow this answer.")
    elif turn.feedback == 0:
        with c2:
            st.text_input("What was wrong?", value=turn.feedback_note, key=f"fbn_{turn.id}",
                          placeholder="What was wrong? e.g. revenue should exclude refunds",
                          label_visibility="collapsed", on_change=_on_feedback_note, args=(turn, question))
    else:
        c2.caption("Was this right? Your rating teaches the assistant.")


def render_assistant(turn: Turn, is_last: bool, question: str = "") -> None:
    ss = st.session_state
    with st.chat_message("assistant", avatar=ASSISTANT_AVATAR):
        tags: list[tuple[str, str]] = []
        if turn.sql and not turn.error:
            if turn.df is not None and not turn.df.empty:
                tags.append((f"{turn.row_count:,} rows", ""))
            if turn.elapsed_ms:
                tags.append((f"{turn.elapsed_ms:.0f} ms", ""))
            if turn.attempts > 1:
                tags.append((f"fixed itself after {turn.attempts - 1} {'retry' if turn.attempts == 2 else 'retries'}", "fix"))
        if turn.used_examples:
            tags.append((f"used {turn.used_examples} saved {'example' if turn.used_examples == 1 else 'examples'}", "fix"))
        if turn.steps:
            n_steps = len(turn.steps)
            tags.insert(0, (f"{n_steps} reasoning {'step' if n_steps == 1 else 'steps'}", "sql"))
        if turn.action == "pending":
            tags.append(("needs your confirmation", "warn"))
        if tags:
            st.markdown(theme.tags_html(tags), unsafe_allow_html=True)

        if turn.content:
            st.markdown(money_safe(turn.content))

        render_result(turn, turn.id)

        if turn.steps:
            with st.expander(f"How I worked it out ({len(turn.steps)} {'step' if len(turn.steps) == 1 else 'steps'})",
                             icon=":material/route:"):
                multi_db = len({st_.get("database") for st_ in turn.steps}) > 1
                for i, step in enumerate(turn.steps, 1):
                    status = f"error: {step['error']}" if step.get("error") else f"{step.get('row_count', 0):,} rows"
                    if multi_db and step.get("database"):
                        status = f"on {step['database']}, {status}"
                    st.markdown(f"**Step {i}. {step.get('purpose', '')}**  \n<span style='color:#5D6A82;font-size:.85rem'>{status}</span>",
                                unsafe_allow_html=True)
                    st.code(step.get("sql", ""), language="sql", wrap_lines=True)
                    if step.get("rows"):
                        st.dataframe(pd.DataFrame(step["rows"], columns=step.get("columns") or None),
                                     hide_index=True, width="stretch")
        if turn.sql:
            with st.expander("SQL used" if not turn.steps else "Final SQL", icon=":material/code:",
                             expanded=turn.action == "pending" or bool(turn.error)):
                st.code(turn.sql, language="sql", wrap_lines=True)
                if turn.explanation:
                    st.caption(turn.explanation)
        for w in turn.warnings:
            st.markdown(theme.caution_html(w), unsafe_allow_html=True)

        if turn.action == "pending":
            c1, c2, _ = st.columns([1.2, 1, 3])
            agent = get_agent()
            if c1.button("Run change", icon=":material/play_arrow:", key=f"ok_{turn.id}", type="primary",
                         disabled=agent is None or ss.settings.read_only):
                agent.confirm_pending(turn)
                save_conv()
                st.rerun()
            if c2.button("Cancel", key=f"no_{turn.id}"):
                SQLAgent.cancel_pending(turn)
                save_conv()
                st.rerun()

        if turn.action == "query" and turn.sql and not turn.error and turn.df is not None:
            render_feedback(turn, question or turn.question)

        if is_last and turn.action == "clarify" and turn.options:
            pick = st.pills("Choose an answer", turn.options, key=f"opt_{turn.id}", label_visibility="collapsed")
            if pick:
                ss.pending_prompt = pick
                st.rerun()
        if is_last and turn.follow_ups:
            st.markdown('<div class="qm-hint">Ask next</div>', unsafe_allow_html=True)
            pick = st.pills("Ask next", turn.follow_ups, key=f"fu_{turn.id}", label_visibility="collapsed")
            if pick:
                ss.pending_prompt = pick
                st.rerun()


def money_safe(text: str) -> str:
    """Stop Streamlit from reading '$450 ... $480' as a LaTeX formula."""
    return text.replace("\\$", "$").replace("$", "\\$")


def render_user(text: str) -> None:
    with st.chat_message("user"):
        st.markdown(f'<span class="qm-user"></span>{theme.esc(money_safe(text))}', unsafe_allow_html=True)


def starter_questions(agent: SQLAgent | None) -> list[str]:
    """Curated examples for catalog datasets; LLM-generated from the schema for any other database."""
    ss = st.session_state
    src = ss.source or {}
    if src.get("type") == "dataset":
        ds = ss.catalog.get(src["id"])
        if ds and ds.examples:
            return ds.examples[:4]
    key = ss.db.url
    if key not in ss.starters and agent is not None:
        with st.spinner("Looking at your schema…"):
            ss.starters[key] = agent.suggest_questions()
    if ss.starters.get(key):
        return ss.starters[key]
    tables = [n for n, t in ss.db.get_schema().items() if not t.is_view]
    first = tables[0] if tables else "my_table"
    return ["What tables are in this database and what do they contain?", f"Show me 10 rows from {first}",
            "How many rows does each table have?", f"Summarise the numeric columns of {first}"]


def header(compact: bool) -> None:
    ss = st.session_state
    label = ss.source["label"] if ss.source else ss.db.display_name
    name = theme.short_name(label)
    pills = [("Read-only", "") if ss.settings.read_only else ("Write mode", "write"), (ss.llm_cfg["model"].split("/")[-1], "")]
    names = source_options()
    pills += [(f"+ {theme.short_name(names[k])}", "") for k in ss.linked_sel.get(ss.conv.source_key, [])
              if k in names and k != ss.conv.source_key]
    if compact:
        st.markdown(theme.bar_html(name, pills), unsafe_allow_html=True)
        return
    lede = ""
    if ss.source and ss.source.get("type") == "dataset":
        ds = ss.catalog.get(ss.source["id"])
        lede = ds.description if ds else ""
    schema = ss.db.get_schema()
    tables = sorted(((t.name, t.row_count, t.is_view) for t in schema.values()), key=lambda x: (x[2], x[0].lower()))
    where = f"{ss.db.display_name.replace(' · ', ', ')}. {pills[0][0]}, answering with {pills[1][0]}."
    st.markdown(theme.hero_html(name, where, lede, tables), unsafe_allow_html=True)


def chat_view() -> None:
    ss = st.session_state
    conv: Conversation = ss.conv
    agent = get_agent()

    if ss.db is None:
        st.markdown(theme.hero_html("your data", "Nothing connected yet.",
                                    "Pick one of the real datasets in the sidebar, or add your own MySQL, PostgreSQL, "
                                    "SQLite or CSV data under Add a database.", []), unsafe_allow_html=True)
        return

    header(compact=bool(conv.turns))

    if agent is None:
        st.warning("**Add an API key to start asking questions.** " + key_setup_hint() +
                   "\n\nUntil then, you can browse the schema in the sidebar and run SQL in the SQL workbench.",
                   icon=":material/key:")

    if not conv.turns:
        st.markdown('<div class="qm-section">Try one of these</div>', unsafe_allow_html=True)
        with st.container(key="starters"):
            cols = st.columns(2)
            for i, q in enumerate(starter_questions(agent)):
                if cols[i % 2].button(q, key=f"start_{i}", width="stretch"):
                    ss.pending_prompt = q
                    st.rerun()

    last_idx = len(conv.turns) - 1
    for i, t in enumerate(conv.turns):
        if t.role == "user":
            render_user(t.content)
        else:
            prev_q = conv.turns[i - 1].content if i > 0 and conv.turns[i - 1].role == "user" else ""
            render_assistant(t, is_last=i == last_idx, question=prev_q)

    prompt = st.chat_input("Ask a question about your data, e.g. which genre sells best?")
    prompt = prompt or ss.pending_prompt
    if prompt and agent is None:
        ss.pending_prompt = None
        st.error("Add an API key in the sidebar under Language model, then ask again.", icon=":material/key:")
    elif prompt:
        ss.pending_prompt = None
        render_user(prompt)
        with st.chat_message("assistant", avatar=ASSISTANT_AVATAR):
            status_box = st.status("Understanding your question…", expanded=False)
            answer_box = st.empty()

            def on_status(message: str) -> None:
                status_box.update(label=message, state="running")
                if message.startswith("Step "):
                    status_box.write(message)

            def on_token(text: str) -> None:
                answer_box.markdown(money_safe(text) + " ▌")

            turn = agent.respond(conv, prompt, read_only=ss.settings.read_only, on_status=on_status, on_token=on_token)
            status_box.update(label="Failed" if turn.error else "Done", state="error" if turn.error else "complete")
        save_conv()
        st.rerun()


def workbench_view() -> None:
    ss = st.session_state
    if ss.db is None:
        st.info("Connect a database first.")
        return
    header(compact=True)
    st.caption("Write or paste SQL and run it yourself. The same safety rules apply as in chat.")
    last = ss.conv.last_assistant()
    default = last.sql if last and last.sql else ""
    sql = st.text_area("SQL", value=default, height=180, key="wb_sql", label_visibility="collapsed")
    c1, c2, c3 = st.columns([1, 1, 3])
    run = c1.button("Run", icon=":material/play_arrow:", type="primary", width="stretch", disabled=not sql.strip())
    explain = c2.button("Explain", icon=":material/lightbulb:", width="stretch", disabled=not sql.strip())
    if not ss.settings.read_only:
        c3.checkbox("I confirm this may modify data", key="wb_confirm")

    if run:
        report = ss.db.check(sql, ss.settings.read_only)
        if not report.allowed:
            st.error(report.reason)
        elif not report.is_read and not ss.get("wb_confirm"):
            st.warning("This statement modifies data. Tick the confirmation box and run again.")
        else:
            res = ss.db.execute(sql, read_only=ss.settings.read_only, row_limit=ss.settings.row_limit)
            if not res.ok:
                st.error(res.error)
            elif res.df is not None:
                t = Turn(role="assistant", sql=res.sql, row_count=res.rowcount, truncated=res.truncated)
                t.set_df(res.df)
                st.markdown(theme.tags_html([(f"{res.rowcount:,} rows", ""), (f"{res.elapsed_ms:.0f} ms", "")]), unsafe_allow_html=True)
                render_result(t, "workbench")
            else:
                st.success(f"Done — {res.rowcount} row(s) affected.")
                if res.kind == "ddl":
                    ss.db.get_schema(refresh=True)
        for w in report.warnings:
            st.markdown(theme.caution_html(w), unsafe_allow_html=True)

    if explain:
        agent = get_agent()
        if agent is None:
            st.warning("Add an API key to use explanations.")
        else:
            with st.spinner("Explaining…"):
                try:
                    tables = ss.db.relevant_tables(sql)
                    text = agent.llm.chat([
                        {"role": "system", "content": f"You explain {agent.dialect_label} SQL to non-experts. Be concise: "
                                                      "what it returns, step by step, and one optimisation tip if relevant.\n\nSchema:\n"
                                                      + ss.db.schema_text(tables, with_samples=False)},
                        {"role": "user", "content": sql},
                    ])
                    st.markdown(text)
                except LLMError as e:
                    st.error(str(e))


def main() -> None:
    init_state()
    sidebar()
    views = {"chat": ":material/forum: Chat", "dash": ":material/dashboard: Dashboard",
             "sql": ":material/terminal: SQL workbench"}
    # Remember the tab outside the widget: if a rerun interrupts the page before this control is drawn
    # (e.g. switching database while a dashboard is loading), Streamlit drops its state and we restore it here.
    ss = st.session_state
    ss.setdefault("view_saved", "chat")
    if ss.get("view") not in views:
        ss["view"] = ss.view_saved
    view = st.segmented_control("View", list(views), format_func=views.get, key="view", label_visibility="collapsed")
    if view in views:
        ss.view_saved = view
    else:  # clicking the active tab again deselects it: stay where we are
        view = ss.view_saved
    if view == "chat":
        chat_view()
    elif view == "dash":
        dashboard_view(get_agent, header, linked_source_list, source_options)
    else:
        workbench_view()


main()
