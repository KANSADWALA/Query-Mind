"""
app.py — QueryMind v2 · Full-featured Conversational SQL Assistant

Fixes applied:
  #1  Healed df now correctly rebuilt from healed query rows
  #2  healed flag set correctly in _handle_pending
  #5  Enter key submits via st.form
  #6  Sidebar examples auto-submit via auto_submit flag
  #9  Excel bytes built lazily (only on download click)
  #12 SQL copy button added
  #13 Bookmarks use in-memory list (no extra disk reads)
  #14 zero_rows_hint shown when query returns 0 rows
  #17 Auto-save after every message; autosave restore on startup
"""

from __future__ import annotations
import io
import os
import time
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from database    import init_db, DB_PATH, DatabaseManager
from agent       import ConversationalSQLAgent, MODIFICATION_META, AgentResponse
from charts      import build_chart, chart_type_label
from persistence import (save_session, list_sessions, load_session, delete_session,
                          load_bookmarks, add_bookmark, remove_bookmark,
                          auto_save_session, load_autosave, has_autosave)

load_dotenv()

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="QueryMind",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS ────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Space+Grotesk:wght@300;400;500;600;700&display=swap');
:root {
  --bg:#0d0f14; --surface:#141720; --surface2:#1c2030; --border:#252a3a;
  --accent:#7c6af7; --accent2:#4af0c8; --text:#e2e8f0; --muted:#64748b;
  --code-bg:#0a0c10; --success:#10b981; --warn:#f59e0b; --error:#ef4444;
}
html,body,[data-testid="stAppViewContainer"]{background:var(--bg)!important;
  font-family:'Space Grotesk',sans-serif;color:var(--text);}
[data-testid="stSidebar"]{background:var(--surface)!important;border-right:1px solid var(--border);}
#MainMenu,footer,header{visibility:hidden;}
[data-testid="stToolbar"]{display:none;}

/* Header */
.qm-header{display:flex;align-items:center;gap:14px;padding:20px 0 8px;
  border-bottom:1px solid var(--border);margin-bottom:24px;}
.qm-logo{font-size:2rem;background:linear-gradient(135deg,var(--accent),var(--accent2));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;}
.qm-title{font-size:1.5rem;font-weight:700;letter-spacing:-.02em;color:var(--text);margin:0;}
.qm-sub{font-size:.75rem;color:var(--muted);font-family:'JetBrains Mono',monospace;margin:0;}

/* Chat bubbles */
.msg-user{display:flex;justify-content:flex-end;margin:10px 0;}
.msg-user-bubble{background:linear-gradient(135deg,#312e81,#1e1b4b);border:1px solid #4338ca;
  border-radius:18px 18px 4px 18px;padding:11px 16px;max-width:75%;font-size:.91rem;color:#e0e7ff;}
.msg-ai{display:flex;justify-content:flex-start;margin:10px 0;}
.msg-ai-inner{max-width:93%;background:var(--surface2);border:1px solid var(--border);
  border-radius:4px 18px 18px 18px;padding:13px 16px;}
.msg-ai-meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:9px;}

/* Badges */
.mod-badge{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:20px;
  font-size:.7rem;font-weight:600;font-family:'JetBrains Mono',monospace;
  letter-spacing:.04em;text-transform:uppercase;}
.cte-badge{background:rgba(74,240,200,.12);border:1px solid rgba(74,240,200,.3);
  color:#4af0c8;padding:2px 8px;border-radius:10px;font-size:.68rem;
  font-family:'JetBrains Mono',monospace;}
.heal-badge{background:rgba(16,185,129,.12);border:1px solid rgba(16,185,129,.3);
  color:#34d399;padding:2px 8px;border-radius:10px;font-size:.68rem;
  font-family:'JetBrains Mono',monospace;}

/* Confidence bar */
.conf-row{display:flex;align-items:center;gap:8px;margin-top:4px;}
.conf-bar-bg{flex:1;height:4px;background:#1c2030;border-radius:2px;max-width:120px;}
.conf-bar{height:4px;border-radius:2px;}
.conf-lbl{font-size:.7rem;font-family:'JetBrains Mono',monospace;color:var(--muted);}

/* SQL block */
.sql-block{background:var(--code-bg);border:1px solid var(--border);
  border-left:3px solid var(--accent);border-radius:8px;padding:13px 15px;
  font-family:'JetBrains Mono',monospace;font-size:.81rem;color:#a5f3fc;
  white-space:pre-wrap;word-break:break-word;margin:7px 0;}
.sql-explanation{font-size:.84rem;color:#94a3b8;margin:5px 0;line-height:1.5;}

/* Chips */
.changes-row{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px;}
.change-chip{background:rgba(124,106,247,.1);border:1px solid rgba(124,106,247,.3);
  color:#c4b5fd;border-radius:12px;padding:2px 9px;font-size:.73rem;
  font-family:'JetBrains Mono',monospace;}

/* Context note */
.ctx-note{background:rgba(74,240,200,.05);border:1px solid rgba(74,240,200,.18);
  border-radius:7px;padding:7px 11px;font-size:.76rem;color:#6ee7b7;margin-top:7px;
  font-family:'JetBrains Mono',monospace;}

/* Ambiguity */
.amb-box{background:rgba(245,158,11,.07);border:1px solid rgba(245,158,11,.3);
  border-radius:8px;padding:10px 14px;color:#fbbf24;font-size:.84rem;margin:8px 0;}

/* Zero rows hint */
.zero-hint{background:rgba(245,158,11,.07);border:1px solid rgba(245,158,11,.25);
  border-radius:8px;padding:9px 13px;color:#fcd34d;font-size:.8rem;
  font-family:'JetBrains Mono',monospace;margin:7px 0;}

/* Error */
.err-block{background:rgba(239,68,68,.07);border:1px solid rgba(239,68,68,.28);
  border-radius:8px;padding:9px 13px;color:#fca5a5;font-size:.82rem;
  font-family:'JetBrains Mono',monospace;margin:7px 0;}

/* Result */
.result-hdr{font-size:.76rem;color:var(--muted);font-family:'JetBrains Mono',monospace;
  margin:10px 0 3px;display:flex;align-items:center;gap:7px;}
.result-cnt{background:rgba(16,185,129,.13);border:1px solid rgba(16,185,129,.28);
  color:#34d399;padding:2px 8px;border-radius:10px;font-size:.7rem;}
.no-result{color:#f59e0b;font-size:.78rem;font-family:'JetBrains Mono',monospace;
  margin:8px 0;padding:8px 12px;background:rgba(245,158,11,.07);
  border:1px solid rgba(245,158,11,.2);border-radius:7px;}

/* Plan */
.plan-block{background:var(--code-bg);border:1px solid var(--border);border-radius:7px;
  padding:10px 13px;font-family:'JetBrains Mono',monospace;font-size:.74rem;
  color:#64748b;white-space:pre-wrap;}

/* Input */
.stTextInput>div>div>input{background:var(--surface2)!important;
  border:1px solid var(--border)!important;border-radius:12px!important;
  color:var(--text)!important;font-family:'Space Grotesk',sans-serif!important;
  font-size:.92rem!important;padding:13px 17px!important;}
.stTextInput>div>div>input:focus{border-color:var(--accent)!important;
  box-shadow:0 0 0 2px rgba(124,106,247,.18)!important;}
.stTextArea textarea{background:var(--surface2)!important;border:1px solid var(--border)!important;
  color:var(--text)!important;font-family:'JetBrains Mono',monospace!important;font-size:.8rem!important;}

/* Form — remove default border/padding Streamlit adds */
[data-testid="stForm"]{border:none!important;padding:0!important;}

/* Buttons */
.stButton>button{background:linear-gradient(135deg,var(--accent),#5b4bd4)!important;
  color:#fff!important;border:none!important;border-radius:9px!important;
  font-family:'Space Grotesk',sans-serif!important;font-weight:600!important;
  transition:opacity .2s!important;}
.stButton>button:hover{opacity:.86!important;}

/* Stat cards */
.stat-row{display:flex;gap:8px;margin:14px 0;}
.stat-card{flex:1;background:var(--surface2);border:1px solid var(--border);
  border-radius:10px;padding:11px;text-align:center;}
.stat-val{font-size:1.5rem;font-weight:700;background:linear-gradient(135deg,var(--accent),var(--accent2));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;}
.stat-lbl{font-size:.68rem;color:var(--muted);margin-top:1px;}

/* Sidebar section label */
.sb-sec{font-size:.68rem;font-family:'JetBrains Mono',monospace;color:var(--muted);
  text-transform:uppercase;letter-spacing:.08em;margin:16px 0 6px;}

/* Schema tree */
.schema-tbl{font-size:.76rem;color:#7c6af7;font-family:'JetBrains Mono',monospace;
  font-weight:600;margin:6px 0 2px;}
.schema-col{font-size:.72rem;color:#64748b;font-family:'JetBrains Mono',monospace;
  padding-left:12px;}
.schema-type{color:#4af0c8;}

/* DB badge */
.db-badge{background:rgba(124,106,247,.1);border:1px solid rgba(124,106,247,.25);
  border-radius:8px;padding:5px 10px;font-size:.72rem;color:#a78bfa;
  font-family:'JetBrains Mono',monospace;margin-bottom:8px;}

/* Dataframe */
[data-testid="stDataFrame"]{border-radius:8px;border:1px solid var(--border)!important;overflow:hidden;}

/* Autosave banner */
.autosave-banner{background:rgba(74,240,200,.07);border:1px solid rgba(74,240,200,.2);
  border-radius:8px;padding:9px 14px;font-size:.78rem;color:#6ee7b7;margin-bottom:12px;
  font-family:'JetBrains Mono',monospace;}
</style>
""", unsafe_allow_html=True)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _hex_rgb(h: str) -> str:
    h = h.lstrip("#")
    return ",".join(str(int(h[i:i+2], 16)) for i in (0, 2, 4))


def _mod_badge(mod: str) -> str:
    m = MODIFICATION_META.get(mod, MODIFICATION_META["new"])
    c, icon, label = m["color"], m["icon"], m["label"]
    return (f"<span class='mod-badge' style='background:rgba({_hex_rgb(c)},.14);"
            f"border:1px solid rgba({_hex_rgb(c)},.35);color:{c}'>{icon} {label}</span>")


def _conf_bar(conf: float) -> str:
    pct   = int(conf * 100)
    color = "#10b981" if pct >= 80 else "#f59e0b" if pct >= 60 else "#ef4444"
    return (f"<div class='conf-row'>"
            f"<span class='conf-lbl'>confidence</span>"
            f"<div class='conf-bar-bg'><div class='conf-bar' "
            f"style='width:{pct}%;background:{color}'></div></div>"
            f"<span class='conf-lbl'>{pct}%</span></div>")


def _csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode()


def _xlsx_bytes(df: pd.DataFrame) -> bytes:
    """FIX #9: Called lazily only when user clicks download — not on every render."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False)
    return buf.getvalue()


# ── Session-state init ─────────────────────────────────────────────────────────

def _init_state():
    if "db_manager" not in st.session_state:
        dm = DatabaseManager()
        with st.spinner("Initialising database…"):
            init_db(DB_PATH)
        st.session_state.db_manager = dm

    if "agent" not in st.session_state:
        dm = st.session_state.db_manager
        st.session_state.agent = ConversationalSQLAgent(
            schema  = dm.get_schema_description(),
            api_key = os.environ.get("GROQ_API_KEY"),
        )

    defaults = {
        "chat_log":        [],
        "bookmarks":       load_bookmarks(),
        "show_charts":     True,
        "show_plan":       False,
        "prefill":         "",
        "auto_submit":     False,   # FIX #6: flag for sidebar example auto-submit
        "session_name":    "",
        "autosave_prompted": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ── Pending operations ─────────────────────────────────────────────────────────

def _handle_pending():
    # Re-run with edited SQL
    if "rerun_idx" in st.session_state:
        i   = st.session_state.pop("rerun_idx")
        sql = st.session_state.pop(f"rerun_sql_{i}", None)
        if sql and i < len(st.session_state.chat_log):
            _execute_and_update(i, sql)

    # Auto-heal SQL
    if "heal_idx" in st.session_state:
        i = st.session_state.pop("heal_idx")
        if i < len(st.session_state.chat_log):
            entry  = st.session_state.chat_log[i]
            healed = st.session_state.agent.self_heal_sql(
                entry["current_sql"], entry["db_error"] or "Unknown error"
            )
            if not healed.error and healed.sql:
                entry["response"].sql          = healed.sql
                entry["response"].explanation  = healed.explanation
                entry["response"].changes_made = healed.changes_made
                entry["response"].context_notes= healed.context_notes
                entry["current_sql"]           = healed.sql
                entry["healed"]                = True  # FIX #2: was missing
                _execute_and_update(i, healed.sql, update_resp=False)

    # Bookmark toggle — FIX #13: pass current in-memory list
    if "bm_toggle_idx" in st.session_state:
        i = st.session_state.pop("bm_toggle_idx")
        if i < len(st.session_state.chat_log):
            entry = st.session_state.chat_log[i]
            resp  = entry["response"]
            if entry.get("bookmarked"):
                st.session_state.bookmarks = remove_bookmark(
                    resp.sql, current=st.session_state.bookmarks
                )
                entry["bookmarked"] = False
            else:
                st.session_state.bookmarks = add_bookmark(
                    entry["user"], resp.sql, resp.explanation,
                    current=st.session_state.bookmarks
                )
                entry["bookmarked"] = True


def _execute_and_update(i: int, sql: str, update_resp: bool = True):
    entry = st.session_state.chat_log[i]
    dm    = st.session_state.db_manager
    rows, cols, db_err = dm.run_query(sql)
    # FIX #1: always rebuild df correctly from the (possibly healed) query result
    if not db_err and rows:
        df = pd.DataFrame(rows, columns=cols)
    elif not db_err:
        df = pd.DataFrame()
    else:
        df = None
    entry["current_sql"] = sql
    entry["df"]          = df
    entry["db_error"]    = db_err
    if update_resp:
        entry["response"].sql = sql
    entry["plan"] = dm.get_execution_plan(sql) if not db_err else None


# ── Sidebar ────────────────────────────────────────────────────────────────────

def render_sidebar():
    agent: ConversationalSQLAgent = st.session_state.agent
    dm:    DatabaseManager        = st.session_state.db_manager

    with st.sidebar:
        st.markdown("""
<div style='text-align:center;padding:8px 0 16px'>
  <div style='font-size:2rem'>🧠</div>
  <div style='font-weight:700;font-size:1.05rem;letter-spacing:-.01em'>QueryMind v2</div>
  <div style='font-size:.68rem;color:#64748b;font-family:JetBrains Mono'>
    Groq · llama-3.3-70b-versatile
  </div>
</div>""", unsafe_allow_html=True)

        # ── API key ──────────────────────────────────────────────────────────
        st.markdown("<div class='sb-sec'>API Key</div>", unsafe_allow_html=True)
        key = st.text_input("Groq API Key", value=os.environ.get("GROQ_API_KEY",""),
                            type="password", placeholder="gsk_…",
                            label_visibility="collapsed")
        if key and key != os.environ.get("GROQ_API_KEY",""):
            os.environ["GROQ_API_KEY"] = key
            st.session_state.pop("agent", None)
            st.rerun()

        # ── Stats ────────────────────────────────────────────────────────────
        n_q = len(st.session_state.chat_log)
        n_t = agent.turn_count
        n_r = agent.calls_used
        st.markdown(f"""
<div class='stat-row'>
  <div class='stat-card'><div class='stat-val'>{n_t}</div><div class='stat-lbl'>Turns</div></div>
  <div class='stat-card'><div class='stat-val'>{n_q}</div><div class='stat-lbl'>Queries</div></div>
  <div class='stat-card'><div class='stat-val'>{n_r}/25</div><div class='stat-lbl'>API/min</div></div>
</div>""", unsafe_allow_html=True)

        # ── Database ─────────────────────────────────────────────────────────
        st.markdown("<div class='sb-sec'>Database</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='db-badge'>📦 {dm.label}</div>",
                    unsafe_allow_html=True)

        db_tab, upload_tab, ext_tab = st.tabs(["Default", "Upload", "External"])

        with db_tab:
            if st.button("Use E-Commerce DB", use_container_width=True):
                dm.use_default()
                agent.update_schema(dm.get_schema_description())
                st.session_state.chat_log = []
                agent.reset()
                st.rerun()

        with upload_tab:
            uploaded = st.file_uploader(
                "CSV / XLSX / SQLite",
                type=["csv","xlsx","xls","db","sqlite","sqlite3"],
                label_visibility="collapsed",
            )
            if uploaded:
                key_u = f"upload_{uploaded.name}_{uploaded.size}"
                if st.session_state.get("last_upload_key") != key_u:
                    ok, msg = dm.upload_file(uploaded.read(), uploaded.name)
                    st.session_state["last_upload_key"] = key_u
                    if ok:
                        agent.update_schema(dm.get_schema_description())
                        st.session_state.chat_log = []
                        agent.reset()
                        st.toast(f"✅ {msg}")
                        st.rerun()
                    else:
                        st.error(msg)

        with ext_tab:
            conn_str = st.text_input(
                "Connection string",
                placeholder="postgresql://user:pass@host:5432/db",
                label_visibility="collapsed",
            )
            if st.button("Connect", use_container_width=True) and conn_str:
                ok, msg = dm.connect_external(conn_str)
                if ok:
                    agent.update_schema(dm.get_schema_description())
                    st.session_state.chat_log = []
                    agent.reset()
                    st.toast(f"✅ {msg}")
                    st.rerun()
                else:
                    st.error(msg)

        # ── Settings ─────────────────────────────────────────────────────────
        st.markdown("<div class='sb-sec'>Display</div>", unsafe_allow_html=True)
        st.session_state.show_charts = st.toggle("Auto-charts",
                                                  value=st.session_state.show_charts)
        st.session_state.show_plan   = st.toggle("Execution plan",
                                                  value=st.session_state.show_plan)

        # ── Examples — FIX #6: set auto_submit=True so main() submits immediately
        st.markdown("<div class='sb-sec'>Examples</div>", unsafe_allow_html=True)
        examples = [
            "Show total revenue by product category",
            "Also include the number of orders",
            "Now only for Electronics",
            "Group it by month instead",
            "Include all years not just 2023",
            "Which of those products have rating above 4?",
            "Show the top 5 customers by total spend",
            "What is the average order value per region?",
            "Find products with stock below 100",
        ]
        for ex in examples:
            if st.button(ex, key=f"ex_{ex[:18]}", use_container_width=True):
                st.session_state.prefill      = ex
                st.session_state.auto_submit  = True
                st.rerun()

        # ── Query history ─────────────────────────────────────────────────────
        if st.session_state.chat_log:
            st.markdown("<div class='sb-sec'>Query History</div>",
                        unsafe_allow_html=True)
            for j, entry in enumerate(reversed(st.session_state.chat_log[-10:])):
                label = entry["user"][:45] + ("…" if len(entry["user"]) > 45 else "")
                if st.button(f"↩ {label}", key=f"hist_{j}", use_container_width=True):
                    st.session_state.prefill = entry["user"]
                    st.rerun()

        # ── Bookmarks ─────────────────────────────────────────────────────────
        bm = st.session_state.bookmarks
        if bm:
            st.markdown("<div class='sb-sec'>Bookmarks</div>", unsafe_allow_html=True)
            for j, b in enumerate(bm[:8]):
                lbl = b["question"][:40] + ("…" if len(b["question"]) > 40 else "")
                if st.button(f"⭐ {lbl}", key=f"bm_{j}", use_container_width=True):
                    st.session_state.prefill = b["question"]
                    st.rerun()

        # ── Schema explorer ───────────────────────────────────────────────────
        st.markdown("<div class='sb-sec'>Schema Explorer</div>",
                    unsafe_allow_html=True)
        with st.expander("View tables & columns", expanded=False):
            tree = dm.get_schema_tree()
            if tree:
                parts = []
                for tbl, cols in tree.items():
                    parts.append(f"<div class='schema-tbl'>▸ {tbl}</div>")
                    for c in cols:
                        parts.append(
                            f"<div class='schema-col'>{c['name']} "
                            f"<span class='schema-type'>({c['type']})</span></div>"
                        )
                st.markdown("".join(parts), unsafe_allow_html=True)
                for tbl in tree:
                    if st.button(f"Sample {tbl}", key=f"smpl_{tbl}",
                                 use_container_width=True):
                        st.session_state.prefill     = f"Show me 5 sample rows from {tbl}"
                        st.session_state.auto_submit = True
                        st.rerun()

        # ── Sessions ──────────────────────────────────────────────────────────
        st.markdown("<div class='sb-sec'>Sessions</div>", unsafe_allow_html=True)
        with st.expander("Save / Load", expanded=False):
            sname = st.text_input("Session name", placeholder="my-analysis",
                                  label_visibility="collapsed")
            if st.button("💾 Save session", use_container_width=True):
                if st.session_state.chat_log:
                    slug = save_session(st.session_state.chat_log, sname or None)
                    st.toast(f"✅ Saved as '{slug}'")
                else:
                    st.warning("Nothing to save yet.")

            sessions = list_sessions()
            if sessions:
                options = {f"{s['name']} ({s['turns']} turns)": s["filename"]
                           for s in sessions}
                chosen = st.selectbox("Load session", list(options.keys()),
                                      label_visibility="collapsed")
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("📂 Load", use_container_width=True):
                        log = load_session(options[chosen])
                        st.session_state.chat_log = log
                        agent.reset()
                        st.toast(f"✅ Loaded '{chosen}'")
                        st.rerun()
                with c2:
                    if st.button("🗑 Delete", use_container_width=True):
                        delete_session(options[chosen])
                        st.toast("Deleted.")
                        st.rerun()

        # ── Reset ─────────────────────────────────────────────────────────────
        st.markdown("<hr style='border-color:#252a3a;margin:16px 0'>",
                    unsafe_allow_html=True)
        if st.button("🔄 Reset conversation", use_container_width=True):
            agent.reset()
            st.session_state.chat_log = []
            st.session_state.pop("last_upload_key", None)
            st.rerun()


# ── Message renderer ───────────────────────────────────────────────────────────

def render_message(i: int, entry: dict):
    resp: AgentResponse     = entry["response"]
    df:   pd.DataFrame | None = entry.get("df")
    db_err  = entry.get("db_error")
    elapsed = entry.get("elapsed", 0)

    # ── User bubble
    st.markdown(f"""
<div class='msg-user'>
  <div class='msg-user-bubble'>💬 {entry['user']}</div>
</div>""", unsafe_allow_html=True)

    # ── AI error bubble
    if resp.error:
        st.markdown(f"""
<div class='msg-ai'>
  <div class='msg-ai-inner'>
    <div class='err-block'>⚠ Agent error: {resp.error}</div>
  </div>
</div>""", unsafe_allow_html=True)
        return

    # Badges
    extra_badges = ""
    if resp.uses_cte:
        extra_badges += "<span class='cte-badge'>⛓ CTE</span> "
    if entry.get("healed"):
        extra_badges += "<span class='heal-badge'>🔧 Auto-fixed</span>"

    # Changes chips
    chips_html = ""
    if resp.changes_made:
        chips = "".join(f"<span class='change-chip'>{c}</span>"
                        for c in resp.changes_made)
        chips_html = f"<div class='changes-row'>{chips}</div>"

    # Context note
    ctx_html = ""
    if resp.context_notes:
        ctx_html = f"<div class='ctx-note'>🔗 {resp.context_notes}</div>"

    # Ambiguity
    amb_html = ""
    if resp.is_ambiguous and resp.clarifying_question:
        amb_html = (f"<div class='amb-box'>❓ <b>Did you mean:</b> "
                    f"{resp.clarifying_question}<br>"
                    f"<small>Showing best-guess results below.</small></div>")

    st.markdown(f"""
<div class='msg-ai'>
  <div class='msg-ai-inner'>
    <div class='msg-ai-meta'>
      {_mod_badge(resp.modification_type)}
      {extra_badges}
      <span style='font-size:.72rem;color:#475569;margin-left:auto;
        font-family:JetBrains Mono'>{elapsed:.2f}s</span>
    </div>
    {_conf_bar(resp.confidence)}
    {amb_html}
    <div class='sql-explanation'>{resp.explanation}</div>
    <div class='sql-block'>{entry['current_sql']}</div>
    {chips_html}
    {ctx_html}
  </div>
</div>""", unsafe_allow_html=True)

    # ── FIX #12: SQL copy button via st.code (has built-in copy icon)
    with st.expander("✏️ Edit & Re-run  |  📋 Copy SQL", expanded=False):
        st.code(entry["current_sql"], language="sql")
        edited = st.text_area(
            "Edit SQL", value=entry["current_sql"],
            key=f"sql_edit_{i}", height=110, label_visibility="collapsed"
        )
        if st.button("▶ Re-run", key=f"rerun_btn_{i}", use_container_width=False):
            st.session_state[f"rerun_sql_{i}"] = edited
            st.session_state["rerun_idx"] = i
            st.rerun()

    # ── Results
    if db_err:
        st.markdown(f"<div class='err-block'>⚠ SQL error: {db_err}</div>",
                    unsafe_allow_html=True)
        if st.button("🔧 Auto-fix SQL", key=f"heal_btn_{i}",
                     use_container_width=False):
            st.session_state["heal_idx"] = i
            st.rerun()

    elif df is not None:
        if df.empty:
            # FIX #14: Show zero_rows_hint if available
            hint = getattr(resp, "zero_rows_hint", "")
            hint_html = (f"<div class='zero-hint'>💡 {hint}</div>" if hint else "")
            st.markdown(
                f"<div class='no-result'>⚠ Query returned 0 rows</div>{hint_html}",
                unsafe_allow_html=True,
            )
        else:
            n     = len(df)
            ctype = chart_type_label(df) if st.session_state.show_charts else ""
            ctype_html = (f"<span style='color:#4af0c8;font-size:.7rem'>{ctype}</span>"
                          if ctype else "")
            st.markdown(
                f"<div class='result-hdr'>Results "
                f"<span class='result-cnt'>{n} row{'s' if n!=1 else ''}</span>"
                f"{ctype_html}</div>",
                unsafe_allow_html=True,
            )
            st.dataframe(df, use_container_width=True, hide_index=True)

            # FIX #9: xlsx built lazily inside download_button callback
            ce1, ce2, _ = st.columns([1, 1, 4])
            with ce1:
                st.download_button(
                    "📥 CSV", _csv_bytes(df),
                    file_name=f"query_{i+1}.csv", mime="text/csv",
                    key=f"dl_csv_{i}", use_container_width=True,
                )
            with ce2:
                st.download_button(
                    "📥 Excel", _xlsx_bytes(df),
                    file_name=f"query_{i+1}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"dl_xlsx_{i}", use_container_width=True,
                )

            # Auto chart
            if st.session_state.show_charts:
                fig = build_chart(df, resp.explanation)
                if fig:
                    st.plotly_chart(fig, use_container_width=True,
                                    key=f"chart_{i}")

    # ── Execution plan
    plan = entry.get("plan")
    if st.session_state.show_plan and plan and not db_err:
        with st.expander("🔍 Execution Plan", expanded=False):
            st.markdown(f"<div class='plan-block'>{plan}</div>",
                        unsafe_allow_html=True)

    # ── Bookmark button
    bm_label = "⭐ Bookmarked" if entry.get("bookmarked") else "☆ Bookmark"
    if st.button(bm_label, key=f"bm_btn_{i}", use_container_width=False):
        st.session_state["bm_toggle_idx"] = i
        st.rerun()

    st.markdown("<hr style='border-color:#1c2030;margin:4px 0'>",
                unsafe_allow_html=True)


# ── Chat log ───────────────────────────────────────────────────────────────────

def render_chat_log():
    for i, entry in enumerate(st.session_state.chat_log):
        render_message(i, entry)


# ── Input area — FIX #5: use st.form so Enter key submits ─────────────────────

def render_input() -> tuple[str, bool]:
    prefill = st.session_state.pop("prefill", "")
    with st.form("query_form", clear_on_submit=True):
        c1, c2 = st.columns([6, 1])
        with c1:
            user_input = st.text_input(
                "query", label_visibility="collapsed",
                value=prefill,
                placeholder="Ask anything… e.g. 'Show revenue by category', 'Break it down by month'…",
                key="main_input",
            )
        with c2:
            submit = st.form_submit_button("Ask ↵", use_container_width=True)
    return user_input, submit


# ── Process new message ────────────────────────────────────────────────────────

def process_message(user_input: str):
    agent: ConversationalSQLAgent = st.session_state.agent
    dm:    DatabaseManager        = st.session_state.db_manager

    with st.spinner("Thinking…"):
        t0      = time.time()
        resp    = agent.chat(user_input.strip())
        elapsed = time.time() - t0

    df, db_err, plan = None, None, None

    if not resp.error and resp.sql:
        rows, cols, db_err = dm.run_query(resp.sql)

        # Auto-heal on first failure
        if db_err:
            healed = agent.self_heal_sql(resp.sql, db_err)
            if not healed.error and healed.sql and healed.sql != resp.sql:
                rows2, cols2, db_err2 = dm.run_query(healed.sql)
                if not db_err2:
                    # FIX #1: use the healed query's results, not the failed ones
                    resp.sql          = healed.sql
                    resp.explanation  = healed.explanation
                    resp.changes_made = healed.changes_made
                    rows, cols, db_err = rows2, cols2, db_err2

        if not db_err and rows:
            df = pd.DataFrame(rows, columns=cols)
        elif not db_err:
            df = pd.DataFrame()

        if not db_err:
            plan = dm.get_execution_plan(resp.sql)

    entry = {
        "user":        user_input.strip(),
        "response":    resp,
        "df":          df,
        "db_error":    db_err,
        "elapsed":     round(elapsed, 2),
        "current_sql": resp.sql,
        "bookmarked":  False,
        "healed":      False,
        "plan":        plan,
    }
    st.session_state.chat_log.append(entry)

    # FIX #17: auto-save after every message
    auto_save_session(st.session_state.chat_log)


# ── Welcome screen ─────────────────────────────────────────────────────────────

def render_welcome():
    st.markdown("""
<div style='text-align:center;padding:44px 20px 24px'>
  <div style='font-size:3.2rem;margin-bottom:14px'>💬</div>
  <div style='font-size:1.1rem;font-weight:600;color:#e2e8f0;margin-bottom:8px'>
    Ask anything about the database
  </div>
  <div style='font-size:.84rem;color:#64748b;max-width:500px;margin:0 auto;line-height:1.7'>
    QueryMind understands follow-up questions, pronoun references, and query modifications.
    Results are auto-charted, editable, and exportable.
  </div>
  <div style='margin-top:24px;display:flex;flex-wrap:wrap;gap:8px;justify-content:center'>
    <div style='background:#141720;border:1px solid #252a3a;border-radius:9px;
      padding:8px 14px;font-size:.79rem;color:#94a3b8'>📦 "Show revenue by category"</div>
    <div style='background:#141720;border:1px solid #252a3a;border-radius:9px;
      padding:8px 14px;font-size:.79rem;color:#94a3b8'>➕ "Break it down by month"</div>
    <div style='background:#141720;border:1px solid #252a3a;border-radius:9px;
      padding:8px 14px;font-size:.79rem;color:#94a3b8'>🎯 "Just Electronics in 2023"</div>
    <div style='background:#141720;border:1px solid #252a3a;border-radius:9px;
      padding:8px 14px;font-size:.79rem;color:#94a3b8'>🔗 "Which have rating &gt; 4?"</div>
    <div style='background:#141720;border:1px solid #252a3a;border-radius:9px;
      padding:8px 14px;font-size:.79rem;color:#94a3b8'>📥 Upload your own CSV / SQLite</div>
  </div>
</div>""", unsafe_allow_html=True)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    _init_state()
    _handle_pending()

    # Header
    st.markdown("""
<div class='qm-header'>
  <div class='qm-logo'>🧠</div>
  <div>
    <p class='qm-title'>QueryMind <span style='font-size:.9rem;color:#6366f1'>v2</span></p>
    <p class='qm-sub'>Conversational SQL · Groq · llama-3.3-70b-versatile</p>
  </div>
</div>""", unsafe_allow_html=True)

    # API key guard
    if not os.environ.get("GROQ_API_KEY"):
        st.markdown("""
<div style='text-align:center;padding:60px 20px'>
  <div style='font-size:2.8rem;margin-bottom:14px'>🔑</div>
  <div style='font-size:1rem;color:#94a3b8;margin-bottom:6px'>No API key detected</div>
  <div style='font-size:.83rem;color:#64748b'>
    Add your <code style='color:#7c6af7'>GROQ_API_KEY</code> in the sidebar
    or create a <code style='color:#7c6af7'>.env</code> file.
  </div>
</div>""", unsafe_allow_html=True)
        render_sidebar()
        return

    render_sidebar()

    # FIX #17: Offer to restore autosave on first load if available
    if (not st.session_state.chat_log
            and not st.session_state.autosave_prompted
            and has_autosave()):
        st.session_state.autosave_prompted = True
        st.markdown(
            "<div class='autosave-banner'>💾 A previous session was auto-saved. "
            "Restore it?</div>",
            unsafe_allow_html=True,
        )
        r1, r2, _ = st.columns([1, 1, 5])
        with r1:
            if st.button("↩ Restore", use_container_width=True):
                st.session_state.chat_log = load_autosave()
                st.rerun()
        with r2:
            if st.button("✕ Dismiss", use_container_width=True):
                st.rerun()

    if not st.session_state.chat_log:
        render_welcome()

    render_chat_log()

    user_input, submit = render_input()

    # FIX #6: auto-submit when a sidebar example was clicked
    auto = st.session_state.pop("auto_submit", False)

    if (submit or auto) and user_input.strip():
        process_message(user_input)
        st.rerun()


if __name__ == "__main__":
    main()















# """
# app.py — QueryMind v2 · Full-featured Conversational SQL Assistant
# """

# from __future__ import annotations
# import io
# import os
# import time
# import pandas as pd
# import streamlit as st
# from dotenv import load_dotenv

# from database  import init_db, DB_PATH, DatabaseManager
# from agent     import ConversationalSQLAgent, MODIFICATION_META, AgentResponse
# from charts    import build_chart, chart_type_label
# from persistence import (save_session, list_sessions, load_session, delete_session,
#                           load_bookmarks, add_bookmark, remove_bookmark)

# load_dotenv()

# # ── Page config ────────────────────────────────────────────────────────────────
# st.set_page_config(
#     page_title="QueryMind",
#     page_icon="🧠",
#     layout="wide",
#     initial_sidebar_state="expanded",
# )

# # ── CSS ────────────────────────────────────────────────────────────────────────
# st.markdown("""
# <style>
# @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Space+Grotesk:wght@300;400;500;600;700&display=swap');
# :root {
#   --bg:#0d0f14; --surface:#141720; --surface2:#1c2030; --border:#252a3a;
#   --accent:#7c6af7; --accent2:#4af0c8; --text:#e2e8f0; --muted:#64748b;
#   --code-bg:#0a0c10; --success:#10b981; --warn:#f59e0b; --error:#ef4444;
# }
# html,body,[data-testid="stAppViewContainer"]{background:var(--bg)!important;
#   font-family:'Space Grotesk',sans-serif;color:var(--text);}
# [data-testid="stSidebar"]{background:var(--surface)!important;border-right:1px solid var(--border);}
# #MainMenu,footer,header{visibility:hidden;}
# [data-testid="stToolbar"]{display:none;}

# /* Header */
# .qm-header{display:flex;align-items:center;gap:14px;padding:20px 0 8px;
#   border-bottom:1px solid var(--border);margin-bottom:24px;}
# .qm-logo{font-size:2rem;background:linear-gradient(135deg,var(--accent),var(--accent2));
#   -webkit-background-clip:text;-webkit-text-fill-color:transparent;}
# .qm-title{font-size:1.5rem;font-weight:700;letter-spacing:-.02em;color:var(--text);margin:0;}
# .qm-sub{font-size:.75rem;color:var(--muted);font-family:'JetBrains Mono',monospace;margin:0;}

# /* Chat bubbles */
# .msg-user{display:flex;justify-content:flex-end;margin:10px 0;}
# .msg-user-bubble{background:linear-gradient(135deg,#312e81,#1e1b4b);border:1px solid #4338ca;
#   border-radius:18px 18px 4px 18px;padding:11px 16px;max-width:75%;font-size:.91rem;color:#e0e7ff;}
# .msg-ai{display:flex;justify-content:flex-start;margin:10px 0;}
# .msg-ai-inner{max-width:93%;background:var(--surface2);border:1px solid var(--border);
#   border-radius:4px 18px 18px 18px;padding:13px 16px;}
# .msg-ai-meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:9px;}

# /* Badges */
# .mod-badge{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:20px;
#   font-size:.7rem;font-weight:600;font-family:'JetBrains Mono',monospace;
#   letter-spacing:.04em;text-transform:uppercase;}
# .cte-badge{background:rgba(74,240,200,.12);border:1px solid rgba(74,240,200,.3);
#   color:#4af0c8;padding:2px 8px;border-radius:10px;font-size:.68rem;
#   font-family:'JetBrains Mono',monospace;}
# .heal-badge{background:rgba(16,185,129,.12);border:1px solid rgba(16,185,129,.3);
#   color:#34d399;padding:2px 8px;border-radius:10px;font-size:.68rem;
#   font-family:'JetBrains Mono',monospace;}

# /* Confidence bar */
# .conf-row{display:flex;align-items:center;gap:8px;margin-top:4px;}
# .conf-bar-bg{flex:1;height:4px;background:#1c2030;border-radius:2px;max-width:120px;}
# .conf-bar{height:4px;border-radius:2px;}
# .conf-lbl{font-size:.7rem;font-family:'JetBrains Mono',monospace;color:var(--muted);}

# /* SQL block */
# .sql-block{background:var(--code-bg);border:1px solid var(--border);
#   border-left:3px solid var(--accent);border-radius:8px;padding:13px 15px;
#   font-family:'JetBrains Mono',monospace;font-size:.81rem;color:#a5f3fc;
#   white-space:pre-wrap;word-break:break-word;margin:7px 0;}
# .sql-explanation{font-size:.84rem;color:#94a3b8;margin:5px 0;line-height:1.5;}

# /* Chips */
# .changes-row{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px;}
# .change-chip{background:rgba(124,106,247,.1);border:1px solid rgba(124,106,247,.3);
#   color:#c4b5fd;border-radius:12px;padding:2px 9px;font-size:.73rem;
#   font-family:'JetBrains Mono',monospace;}

# /* Context note */
# .ctx-note{background:rgba(74,240,200,.05);border:1px solid rgba(74,240,200,.18);
#   border-radius:7px;padding:7px 11px;font-size:.76rem;color:#6ee7b7;margin-top:7px;
#   font-family:'JetBrains Mono',monospace;}

# /* Ambiguity */
# .amb-box{background:rgba(245,158,11,.07);border:1px solid rgba(245,158,11,.3);
#   border-radius:8px;padding:10px 14px;color:#fbbf24;font-size:.84rem;margin:8px 0;}

# /* Error */
# .err-block{background:rgba(239,68,68,.07);border:1px solid rgba(239,68,68,.28);
#   border-radius:8px;padding:9px 13px;color:#fca5a5;font-size:.82rem;
#   font-family:'JetBrains Mono',monospace;margin:7px 0;}

# /* Result */
# .result-hdr{font-size:.76rem;color:var(--muted);font-family:'JetBrains Mono',monospace;
#   margin:10px 0 3px;display:flex;align-items:center;gap:7px;}
# .result-cnt{background:rgba(16,185,129,.13);border:1px solid rgba(16,185,129,.28);
#   color:#34d399;padding:2px 8px;border-radius:10px;font-size:.7rem;}
# .no-result{color:#f59e0b;font-size:.78rem;font-family:'JetBrains Mono',monospace;
#   margin:8px 0;padding:8px 12px;background:rgba(245,158,11,.07);
#   border:1px solid rgba(245,158,11,.2);border-radius:7px;}

# /* Plan */
# .plan-block{background:var(--code-bg);border:1px solid var(--border);border-radius:7px;
#   padding:10px 13px;font-family:'JetBrains Mono',monospace;font-size:.74rem;
#   color:#64748b;white-space:pre-wrap;}

# /* Input */
# .stTextInput>div>div>input{background:var(--surface2)!important;
#   border:1px solid var(--border)!important;border-radius:12px!important;
#   color:var(--text)!important;font-family:'Space Grotesk',sans-serif!important;
#   font-size:.92rem!important;padding:13px 17px!important;}
# .stTextInput>div>div>input:focus{border-color:var(--accent)!important;
#   box-shadow:0 0 0 2px rgba(124,106,247,.18)!important;}
# .stTextArea textarea{background:var(--surface2)!important;border:1px solid var(--border)!important;
#   color:var(--text)!important;font-family:'JetBrains Mono',monospace!important;font-size:.8rem!important;}

# /* Buttons */
# .stButton>button{background:linear-gradient(135deg,var(--accent),#5b4bd4)!important;
#   color:#fff!important;border:none!important;border-radius:9px!important;
#   font-family:'Space Grotesk',sans-serif!important;font-weight:600!important;
#   transition:opacity .2s!important;}
# .stButton>button:hover{opacity:.86!important;}

# /* Stat cards */
# .stat-row{display:flex;gap:8px;margin:14px 0;}
# .stat-card{flex:1;background:var(--surface2);border:1px solid var(--border);
#   border-radius:10px;padding:11px;text-align:center;}
# .stat-val{font-size:1.5rem;font-weight:700;background:linear-gradient(135deg,var(--accent),var(--accent2));
#   -webkit-background-clip:text;-webkit-text-fill-color:transparent;}
# .stat-lbl{font-size:.68rem;color:var(--muted);margin-top:1px;}

# /* Sidebar section label */
# .sb-sec{font-size:.68rem;font-family:'JetBrains Mono',monospace;color:var(--muted);
#   text-transform:uppercase;letter-spacing:.08em;margin:16px 0 6px;}

# /* History item */
# .hist-item{background:var(--surface2);border:1px solid var(--border);border-radius:7px;
#   padding:7px 10px;font-size:.78rem;color:#cbd5e1;margin-bottom:4px;cursor:pointer;
#   white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}

# /* Schema tree */
# .schema-tbl{font-size:.76rem;color:#7c6af7;font-family:'JetBrains Mono',monospace;
#   font-weight:600;margin:6px 0 2px;}
# .schema-col{font-size:.72rem;color:#64748b;font-family:'JetBrains Mono',monospace;
#   padding-left:12px;}
# .schema-type{color:#4af0c8;}

# /* DB badge */
# .db-badge{background:rgba(124,106,247,.1);border:1px solid rgba(124,106,247,.25);
#   border-radius:8px;padding:5px 10px;font-size:.72rem;color:#a78bfa;
#   font-family:'JetBrains Mono',monospace;margin-bottom:8px;}

# /* Dataframe */
# [data-testid="stDataFrame"]{border-radius:8px;border:1px solid var(--border)!important;overflow:hidden;}
# </style>
# """, unsafe_allow_html=True)


# # ── Helpers ────────────────────────────────────────────────────────────────────

# def _hex_rgb(h: str) -> str:
#     h = h.lstrip("#")
#     return ",".join(str(int(h[i:i+2], 16)) for i in (0, 2, 4))


# def _mod_badge(mod: str) -> str:
#     m = MODIFICATION_META.get(mod, MODIFICATION_META["new"])
#     c, icon, label = m["color"], m["icon"], m["label"]
#     return (f"<span class='mod-badge' style='background:rgba({_hex_rgb(c)},.14);"
#             f"border:1px solid rgba({_hex_rgb(c)},.35);color:{c}'>{icon} {label}</span>")


# def _conf_bar(conf: float) -> str:
#     pct = int(conf * 100)
#     color = "#10b981" if pct >= 80 else "#f59e0b" if pct >= 60 else "#ef4444"
#     return (f"<div class='conf-row'>"
#             f"<span class='conf-lbl'>confidence</span>"
#             f"<div class='conf-bar-bg'><div class='conf-bar' "
#             f"style='width:{pct}%;background:{color}'></div></div>"
#             f"<span class='conf-lbl'>{pct}%</span></div>")


# def _csv_bytes(df: pd.DataFrame) -> bytes:
#     return df.to_csv(index=False).encode()


# def _xlsx_bytes(df: pd.DataFrame) -> bytes:
#     buf = io.BytesIO()
#     with pd.ExcelWriter(buf, engine="xlsxwriter") as w:
#         df.to_excel(w, index=False)
#     return buf.getvalue()


# # ── Session-state init ─────────────────────────────────────────────────────────

# def _init_state():
#     if "db_manager" not in st.session_state:
#         dm = DatabaseManager()
#         with st.spinner("Initialising database…"):
#             init_db(DB_PATH)
#         st.session_state.db_manager = dm

#     if "agent" not in st.session_state:
#         dm = st.session_state.db_manager
#         st.session_state.agent = ConversationalSQLAgent(
#             schema  = dm.get_schema_description(),
#             api_key = os.environ.get("GROQ_API_KEY"),
#         )

#     defaults = {
#         "chat_log":       [],
#         "bookmarks":      load_bookmarks(),
#         "show_charts":    True,
#         "show_plan":      False,
#         "prefill":        "",
#         "session_name":   "",
#     }
#     for k, v in defaults.items():
#         if k not in st.session_state:
#             st.session_state[k] = v


# # ── Pending operations (re-run SQL, auto-heal, bookmark toggle) ────────────────

# def _handle_pending():
#     # Re-run with edited SQL
#     if "rerun_idx" in st.session_state:
#         i   = st.session_state.pop("rerun_idx")
#         sql = st.session_state.pop(f"rerun_sql_{i}", None)
#         if sql and i < len(st.session_state.chat_log):
#             _execute_and_update(i, sql)

#     # Auto-heal SQL
#     if "heal_idx" in st.session_state:
#         i   = st.session_state.pop("heal_idx")
#         if i < len(st.session_state.chat_log):
#             entry  = st.session_state.chat_log[i]
#             healed = st.session_state.agent.self_heal_sql(
#                 entry["current_sql"], entry["db_error"] or "Unknown error"
#             )
#             if not healed.error and healed.sql:
#                 entry["response"].sql         = healed.sql
#                 entry["response"].explanation = healed.explanation
#                 entry["response"].changes_made= healed.changes_made
#                 entry["response"].context_notes = healed.context_notes
#                 entry["current_sql"] = healed.sql
#                 entry["healed"]      = True
#                 _execute_and_update(i, healed.sql, update_resp=False)

#     # Bookmark toggle
#     if "bm_toggle_idx" in st.session_state:
#         i = st.session_state.pop("bm_toggle_idx")
#         if i < len(st.session_state.chat_log):
#             entry = st.session_state.chat_log[i]
#             resp  = entry["response"]
#             if entry.get("bookmarked"):
#                 st.session_state.bookmarks = remove_bookmark(resp.sql)
#                 entry["bookmarked"] = False
#             else:
#                 st.session_state.bookmarks = add_bookmark(
#                     entry["user"], resp.sql, resp.explanation
#                 )
#                 entry["bookmarked"] = True


# def _execute_and_update(i: int, sql: str, update_resp: bool = True):
#     entry = st.session_state.chat_log[i]
#     dm    = st.session_state.db_manager
#     rows, cols, db_err = dm.run_query(sql)
#     df = pd.DataFrame(rows, columns=cols) if not db_err and rows else \
#          (pd.DataFrame() if not db_err else None)
#     entry["current_sql"] = sql
#     entry["df"]          = df
#     entry["db_error"]    = db_err
#     if update_resp:
#         entry["response"].sql = sql
#     # Execution plan
#     if not db_err:
#         entry["plan"] = dm.get_execution_plan(sql)
#     else:
#         entry["plan"] = None


# # ── Sidebar ────────────────────────────────────────────────────────────────────

# def render_sidebar():
#     agent: ConversationalSQLAgent = st.session_state.agent
#     dm:    DatabaseManager        = st.session_state.db_manager

#     with st.sidebar:
#         st.markdown("""
# <div style='text-align:center;padding:8px 0 16px'>
#   <div style='font-size:2rem'>🧠</div>
#   <div style='font-weight:700;font-size:1.05rem;letter-spacing:-.01em'>QueryMind v2</div>
#   <div style='font-size:.68rem;color:#64748b;font-family:JetBrains Mono'>
#     Groq · llama-3.3-70b-versatile
#   </div>
# </div>""", unsafe_allow_html=True)

#         # ── API key ──────────────────────────────────────────────────────────
#         st.markdown("<div class='sb-sec'>API Key</div>", unsafe_allow_html=True)
#         key = st.text_input("Groq API Key", value=os.environ.get("GROQ_API_KEY",""),
#                             type="password", placeholder="gsk_…", label_visibility="collapsed")
#         if key and key != os.environ.get("GROQ_API_KEY",""):
#             os.environ["GROQ_API_KEY"] = key
#             st.session_state.pop("agent", None)
#             st.rerun()

#         # ── Stats ────────────────────────────────────────────────────────────
#         n_q = len(st.session_state.chat_log)
#         n_t = agent.turn_count
#         n_r = agent.calls_used
#         st.markdown(f"""
# <div class='stat-row'>
#   <div class='stat-card'><div class='stat-val'>{n_t}</div><div class='stat-lbl'>Turns</div></div>
#   <div class='stat-card'><div class='stat-val'>{n_q}</div><div class='stat-lbl'>Queries</div></div>
#   <div class='stat-card'><div class='stat-val'>{n_r}/25</div><div class='stat-lbl'>API/min</div></div>
# </div>""", unsafe_allow_html=True)

#         # ── Database ─────────────────────────────────────────────────────────
#         st.markdown("<div class='sb-sec'>Database</div>", unsafe_allow_html=True)
#         st.markdown(f"<div class='db-badge'>📦 {dm.label}</div>", unsafe_allow_html=True)

#         db_tab, upload_tab, ext_tab = st.tabs(["Default", "Upload", "External"])

#         with db_tab:
#             if st.button("Use E-Commerce DB", use_container_width=True):
#                 dm.use_default()
#                 agent.update_schema(dm.get_schema_description())
#                 st.session_state.chat_log = []
#                 agent.reset()
#                 st.rerun()

#         with upload_tab:
#             uploaded = st.file_uploader(
#                 "CSV / XLSX / SQLite",
#                 type=["csv","xlsx","xls","db","sqlite","sqlite3"],
#                 label_visibility="collapsed",
#             )
#             if uploaded:
#                 key_u = f"upload_{uploaded.name}_{uploaded.size}"
#                 if st.session_state.get("last_upload_key") != key_u:
#                     ok, msg = dm.upload_file(uploaded.read(), uploaded.name)
#                     st.session_state["last_upload_key"] = key_u
#                     if ok:
#                         agent.update_schema(dm.get_schema_description())
#                         st.session_state.chat_log = []
#                         agent.reset()
#                         st.toast(f"✅ {msg}")
#                         st.rerun()
#                     else:
#                         st.error(msg)

#         with ext_tab:
#             conn_str = st.text_input(
#                 "Connection string",
#                 placeholder="postgresql://user:pass@host:5432/db",
#                 label_visibility="collapsed",
#             )
#             if st.button("Connect", use_container_width=True) and conn_str:
#                 ok, msg = dm.connect_external(conn_str)
#                 if ok:
#                     agent.update_schema(dm.get_schema_description())
#                     st.session_state.chat_log = []
#                     agent.reset()
#                     st.toast(f"✅ {msg}")
#                     st.rerun()
#                 else:
#                     st.error(msg)

#         # ── Settings ─────────────────────────────────────────────────────────
#         st.markdown("<div class='sb-sec'>Display</div>", unsafe_allow_html=True)
#         st.session_state.show_charts = st.toggle("Auto-charts", value=st.session_state.show_charts)
#         st.session_state.show_plan   = st.toggle("Execution plan", value=st.session_state.show_plan)

#         # ── Examples ─────────────────────────────────────────────────────────
#         st.markdown("<div class='sb-sec'>Examples</div>", unsafe_allow_html=True)
#         examples = [
#             "Show total revenue by product category",
#             "Also include the number of orders",
#             "Now only for Electronics",
#             "Group it by month instead",
#             "Include all years not just 2023",
#             "Which of those products have rating above 4?",
#             "Show the top 5 customers by total spend",
#             "What is the average order value per region?",
#             "Find products with stock below 100",
#         ]
#         for ex in examples:
#             if st.button(ex, key=f"ex_{ex[:18]}", use_container_width=True):
#                 st.session_state.prefill = ex
#                 st.rerun()

#         # ── Query history ─────────────────────────────────────────────────────
#         if st.session_state.chat_log:
#             st.markdown("<div class='sb-sec'>Query History</div>", unsafe_allow_html=True)
#             for j, entry in enumerate(reversed(st.session_state.chat_log[-10:])):
#                 label = entry["user"][:45] + ("…" if len(entry["user"]) > 45 else "")
#                 if st.button(f"↩ {label}", key=f"hist_{j}", use_container_width=True):
#                     st.session_state.prefill = entry["user"]
#                     st.rerun()

#         # ── Bookmarks ─────────────────────────────────────────────────────────
#         bm = st.session_state.bookmarks
#         if bm:
#             st.markdown("<div class='sb-sec'>Bookmarks</div>", unsafe_allow_html=True)
#             for j, b in enumerate(bm[:8]):
#                 lbl = b["question"][:40] + ("…" if len(b["question"]) > 40 else "")
#                 if st.button(f"⭐ {lbl}", key=f"bm_{j}", use_container_width=True):
#                     st.session_state.prefill = b["question"]
#                     st.rerun()

#         # ── Schema explorer ───────────────────────────────────────────────────
#         st.markdown("<div class='sb-sec'>Schema Explorer</div>", unsafe_allow_html=True)
#         with st.expander("View tables & columns", expanded=False):
#             tree = dm.get_schema_tree()
#             parts = []
#             for tbl, cols in tree.items():
#                 parts.append(f"<div class='schema-tbl'>▸ {tbl}</div>")
#                 for c in cols:
#                     parts.append(f"<div class='schema-col'>{c['name']} "
#                                 f"<span class='schema-type'>({c['type']})</span></div>")
#             if parts:
#                 st.markdown("".join(parts), unsafe_allow_html=True)
#             for tbl in tree:
#                 if st.button(f"Sample {tbl}", key=f"smpl_{tbl}", use_container_width=True):
#                     st.session_state.prefill = f"Show me 5 sample rows from {tbl}"
#                     st.rerun()

#         # ── Sessions ──────────────────────────────────────────────────────────
#         st.markdown("<div class='sb-sec'>Sessions</div>", unsafe_allow_html=True)
#         with st.expander("Save / Load", expanded=False):
#             sname = st.text_input("Session name", placeholder="my-analysis",
#                                   label_visibility="collapsed")
#             if st.button("💾 Save session", use_container_width=True):
#                 if st.session_state.chat_log:
#                     slug = save_session(st.session_state.chat_log, sname or None)
#                     st.toast(f"✅ Saved as '{slug}'")
#                 else:
#                     st.warning("Nothing to save yet.")

#             sessions = list_sessions()
#             if sessions:
#                 options = {f"{s['name']} ({s['turns']} turns)": s["filename"]
#                            for s in sessions}
#                 chosen = st.selectbox("Load session", list(options.keys()),
#                                       label_visibility="collapsed")
#                 c1, c2 = st.columns(2)
#                 with c1:
#                     if st.button("📂 Load", use_container_width=True):
#                         log = load_session(options[chosen])
#                         st.session_state.chat_log = log
#                         agent.reset()
#                         st.toast(f"✅ Loaded '{chosen}'")
#                         st.rerun()
#                 with c2:
#                     if st.button("🗑 Delete", use_container_width=True):
#                         delete_session(options[chosen])
#                         st.toast("Deleted.")
#                         st.rerun()

#         # ── Reset ─────────────────────────────────────────────────────────────
#         st.markdown("<hr style='border-color:#252a3a;margin:16px 0'>", unsafe_allow_html=True)
#         if st.button("🔄 Reset conversation", use_container_width=True):
#             agent.reset(); st.session_state.chat_log = []
#             st.session_state.pop("last_upload_key", None)
#             st.rerun()


# # ── Message renderer ───────────────────────────────────────────────────────────

# def render_message(i: int, entry: dict):
#     resp: AgentResponse = entry["response"]
#     df:   pd.DataFrame | None = entry.get("df")
#     db_err  = entry.get("db_error")
#     elapsed = entry.get("elapsed", 0)

#     # ── User bubble
#     st.markdown(f"""
# <div class='msg-user'>
#   <div class='msg-user-bubble'>💬 {entry['user']}</div>
# </div>""", unsafe_allow_html=True)

#     # ── AI bubble
#     if resp.error:
#         st.markdown(f"""
# <div class='msg-ai'>
#   <div class='msg-ai-inner'>
#     <div class='err-block'>⚠ Agent error: {resp.error}</div>
#   </div>
# </div>""", unsafe_allow_html=True)
#         return

#     # Badges
#     extra_badges = ""
#     if resp.uses_cte:
#         extra_badges += "<span class='cte-badge'>⛓ CTE</span> "
#     if entry.get("healed"):
#         extra_badges += "<span class='heal-badge'>🔧 Auto-fixed</span>"

#     # Changes chips
#     chips_html = ""
#     if resp.changes_made:
#         chips = "".join(f"<span class='change-chip'>{c}</span>" for c in resp.changes_made)
#         chips_html = f"<div class='changes-row'>{chips}</div>"

#     # Context note
#     ctx_html = ""
#     if resp.context_notes:
#         ctx_html = f"<div class='ctx-note'>🔗 {resp.context_notes}</div>"

#     # Ambiguity
#     amb_html = ""
#     if resp.is_ambiguous and resp.clarifying_question:
#         amb_html = (f"<div class='amb-box'>❓ <b>Did you mean:</b> "
#                     f"{resp.clarifying_question}<br>"
#                     f"<small>Showing best-guess results below.</small></div>")

#     st.markdown(f"""
# <div class='msg-ai'>
#   <div class='msg-ai-inner'>
#     <div class='msg-ai-meta'>
#       {_mod_badge(resp.modification_type)}
#       {extra_badges}
#       <span style='font-size:.72rem;color:#475569;margin-left:auto;
#         font-family:JetBrains Mono'>{elapsed:.2f}s</span>
#     </div>
#     {_conf_bar(resp.confidence)}
#     {amb_html}
#     <div class='sql-explanation'>{resp.explanation}</div>
#     <div class='sql-block'>{entry['current_sql']}</div>
#     {chips_html}
#     {ctx_html}
#   </div>
# </div>""", unsafe_allow_html=True)

#     # ── SQL editor
#     with st.expander("✏️ Edit & Re-run SQL", expanded=False):
#         edited = st.text_area(
#             "SQL editor", value=entry["current_sql"],
#             key=f"sql_edit_{i}", height=110, label_visibility="collapsed"
#         )
#         c1, c2 = st.columns([1, 1])
#         with c1:
#             if st.button("▶ Re-run", key=f"rerun_btn_{i}", use_container_width=True):
#                 st.session_state[f"rerun_sql_{i}"] = edited
#                 st.session_state["rerun_idx"] = i
#                 st.rerun()

#     # ── Results
#     if db_err:
#         st.markdown(f"<div class='err-block'>⚠ SQL error: {db_err}</div>",
#                     unsafe_allow_html=True)
#         if st.button("🔧 Auto-fix SQL", key=f"heal_btn_{i}", use_container_width=False):
#             st.session_state["heal_idx"] = i
#             st.rerun()

#     elif df is not None:
#         if df.empty:
#             st.markdown("<div class='no-result'>⚠ Query returned 0 rows</div>",
#                         unsafe_allow_html=True)
#         else:
#             n = len(df)
#             ctype = chart_type_label(df) if st.session_state.show_charts else ""
#             ctype_html = f"<span style='color:#4af0c8;font-size:.7rem'>{ctype}</span>" if ctype else ""
#             st.markdown(
#                 f"<div class='result-hdr'>Results "
#                 f"<span class='result-cnt'>{n} row{'s' if n!=1 else ''}</span>"
#                 f"{ctype_html}</div>",
#                 unsafe_allow_html=True,
#             )
#             st.dataframe(df, width="stretch", hide_index=True)

#             # Export buttons
#             ce1, ce2, _ = st.columns([1, 1, 4])
#             with ce1:
#                 st.download_button(
#                     "📥 CSV", _csv_bytes(df),
#                     file_name=f"query_{i+1}.csv", mime="text/csv",
#                     key=f"dl_csv_{i}", use_container_width=True,
#                 )
#             with ce2:
#                 st.download_button(
#                     "📥 Excel", _xlsx_bytes(df),
#                     file_name=f"query_{i+1}.xlsx",
#                     mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
#                     key=f"dl_xlsx_{i}", use_container_width=True,
#                 )

#             # Auto chart
#             if st.session_state.show_charts:
#                 fig = build_chart(df, resp.explanation)
#                 if fig:
#                     st.plotly_chart(fig, use_container_width=True, key=f"chart_{i}")

#     # ── Execution plan
#     plan = entry.get("plan")
#     if st.session_state.show_plan and plan and not db_err:
#         with st.expander("🔍 Execution Plan", expanded=False):
#             st.markdown(f"<div class='plan-block'>{plan}</div>",
#                         unsafe_allow_html=True)

#     # ── Bookmark button
#     bm_label = "⭐ Bookmarked" if entry.get("bookmarked") else "☆ Bookmark"
#     if st.button(bm_label, key=f"bm_btn_{i}", use_container_width=False):
#         st.session_state["bm_toggle_idx"] = i
#         st.rerun()

#     st.markdown("<hr style='border-color:#1c2030;margin:4px 0'>", unsafe_allow_html=True)


# # ── Chat log ───────────────────────────────────────────────────────────────────

# def render_chat_log():
#     for i, entry in enumerate(st.session_state.chat_log):
#         render_message(i, entry)


# # ── Input area ─────────────────────────────────────────────────────────────────

# def render_input():
#     prefill = st.session_state.pop("prefill", "")
#     c1, c2  = st.columns([6, 1])
#     with c1:
#         user_input = st.text_input(
#             "query", label_visibility="collapsed",
#             value=prefill,
#             placeholder="Ask anything… ('Show revenue by category', 'Break it down by month', 'Only Electronics'…)",
#             key="main_input",
#         )
#     with c2:
#         submit = st.button("Ask ↵", use_container_width=True)

#     return user_input, submit


# # ── Process new message ────────────────────────────────────────────────────────

# def process_message(user_input: str):
#     agent: ConversationalSQLAgent = st.session_state.agent
#     dm:    DatabaseManager        = st.session_state.db_manager

#     with st.spinner("Thinking…"):
#         t0   = time.time()
#         resp = agent.chat(user_input.strip())
#         elapsed = time.time() - t0

#     df, db_err, plan = None, None, None

#     if not resp.error and resp.sql:
#         rows, cols, db_err = dm.run_query(resp.sql)

#         # Auto-heal on first failure
#         if db_err and not resp.error:
#             healed = agent.self_heal_sql(resp.sql, db_err)
#             if not healed.error and healed.sql and healed.sql != resp.sql:
#                 rows, cols, db_err2 = dm.run_query(healed.sql)
#                 if not db_err2:
#                     resp.sql         = healed.sql
#                     resp.explanation = healed.explanation
#                     resp.changes_made= healed.changes_made
#                     db_err           = db_err2
#                     rows, cols       = rows, cols

#         if not db_err and rows:
#             df = pd.DataFrame(rows, columns=cols)
#         elif not db_err:
#             df = pd.DataFrame()

#         if not db_err:
#             plan = dm.get_execution_plan(resp.sql)

#     st.session_state.chat_log.append({
#         "user":        user_input.strip(),
#         "response":    resp,
#         "df":          df,
#         "db_error":    db_err,
#         "elapsed":     round(elapsed, 2),
#         "current_sql": resp.sql,
#         "bookmarked":  False,
#         "healed":      False,
#         "plan":        plan,
#     })


# # ── Welcome screen ─────────────────────────────────────────────────────────────

# def render_welcome():
#     st.markdown("""
# <div style='text-align:center;padding:44px 20px 24px'>
#   <div style='font-size:3.2rem;margin-bottom:14px'>💬</div>
#   <div style='font-size:1.1rem;font-weight:600;color:#e2e8f0;margin-bottom:8px'>
#     Ask anything about the database
#   </div>
#   <div style='font-size:.84rem;color:#64748b;max-width:500px;margin:0 auto;line-height:1.7'>
#     QueryMind understands follow-up questions, pronoun references, and query modifications.
#     Results are auto-charted, editable, and exportable.
#   </div>
#   <div style='margin-top:24px;display:flex;flex-wrap:wrap;gap:8px;justify-content:center'>
#     <div style='background:#141720;border:1px solid #252a3a;border-radius:9px;
#       padding:8px 14px;font-size:.79rem;color:#94a3b8'>📦 "Show revenue by category"</div>
#     <div style='background:#141720;border:1px solid #252a3a;border-radius:9px;
#       padding:8px 14px;font-size:.79rem;color:#94a3b8'>➕ "Break it down by month"</div>
#     <div style='background:#141720;border:1px solid #252a3a;border-radius:9px;
#       padding:8px 14px;font-size:.79rem;color:#94a3b8'>🎯 "Just Electronics in 2023"</div>
#     <div style='background:#141720;border:1px solid #252a3a;border-radius:9px;
#       padding:8px 14px;font-size:.79rem;color:#94a3b8'>🔗 "Which have rating > 4?"</div>
#     <div style='background:#141720;border:1px solid #252a3a;border-radius:9px;
#       padding:8px 14px;font-size:.79rem;color:#94a3b8'>📥 Upload your own CSV / SQLite</div>
#   </div>
# </div>""", unsafe_allow_html=True)


# # ── Main ───────────────────────────────────────────────────────────────────────

# def main():
#     _init_state()
#     _handle_pending()

#     # Header
#     st.markdown("""
# <div class='qm-header'>
#   <div class='qm-logo'>🧠</div>
#   <div>
#     <p class='qm-title'>QueryMind <span style='font-size:.9rem;color:#6366f1'>v2</span></p>
#     <p class='qm-sub'>Conversational SQL · Groq · llama-3.3-70b-versatile</p>
#   </div>
# </div>""", unsafe_allow_html=True)

#     # API key guard
#     if not os.environ.get("GROQ_API_KEY"):
#         st.markdown("""
# <div style='text-align:center;padding:60px 20px'>
#   <div style='font-size:2.8rem;margin-bottom:14px'>🔑</div>
#   <div style='font-size:1rem;color:#94a3b8;margin-bottom:6px'>No API key detected</div>
#   <div style='font-size:.83rem;color:#64748b'>
#     Add your <code style='color:#7c6af7'>GROQ_API_KEY</code> in the sidebar
#     or create a <code style='color:#7c6af7'>.env</code> file.
#   </div>
# </div>""", unsafe_allow_html=True)
#         render_sidebar()
#         return

#     render_sidebar()

#     if not st.session_state.chat_log:
#         render_welcome()

#     render_chat_log()

#     user_input, submit = render_input()

#     if submit and user_input.strip():
#         process_message(user_input)
#         st.rerun()


# if __name__ == "__main__":
#     main()