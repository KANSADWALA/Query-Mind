"""Visual layer for QueryMind: CSS, brand mark, and HTML snippets used by app.py.

Palette
  paper  #F4F6FA   page background
  ink    #1B2335   text
  cobalt #2F5BEA   the user's voice + primary actions
  navy   #13203B   sidebar
  amber  #F2A93B   reserved for SQL (code edge, SQL tags)
  line   #D9E0EC   hairlines
"""
from __future__ import annotations

import html

CSS = """
<style>
:root {
  --qm-paper:#F4F6FA; --qm-ink:#1B2335; --qm-muted:#5D6A82; --qm-line:#D9E0EC;
  --qm-cobalt:#2F5BEA; --qm-cobalt-soft:#E7EDFD; --qm-navy:#13203B; --qm-amber:#F2A93B;
  --qm-display:'Bricolage Grotesque', 'Instrument Sans', system-ui, sans-serif;
}

/* Layout -------------------------------------------------------------- */
.block-container { padding-top: 1.4rem; padding-bottom: 7rem; max-width: 1080px; }
header[data-testid="stHeader"] { background: transparent; }

/* Brand (sidebar) ------------------------------------------------------ */
.qm-brand { display:flex; align-items:center; gap:.6rem; margin:.2rem 0 .1rem; }
.qm-brand svg { flex:none; }
.qm-brand span { font-family:var(--qm-display); font-weight:800; font-size:1.45rem; letter-spacing:-.02em; color:#fff; }
.qm-brand-sub { color:#9FB0CF; font-size:.86rem; margin:0 0 .9rem; }
.qm-side-label { color:#9FB0CF; font-size:.8rem; margin:.9rem 0 .35rem; }
.qm-current { display:flex; align-items:center; gap:.5rem; background:#1C2C4E; border:1px solid #2B3C63;
  border-radius:10px; padding:.55rem .7rem; margin-bottom:.5rem; }
.qm-current .dot { width:8px; height:8px; border-radius:50%; background:#3FD49B; flex:none;
  box-shadow:0 0 0 3px rgba(63,212,155,.18); }
.qm-current b { color:#fff; font-weight:600; font-size:.92rem; line-height:1.25; }
.qm-current small { display:block; color:#9FB0CF; font-size:.76rem; }

/* Hero: the one loud element -------------------------------------------- */
.qm-hero { padding: 1.6rem 0 1.2rem; }
.qm-hero .where { color:var(--qm-muted); font-size:.92rem; margin-bottom:.35rem; }
.qm-hero h1 { font-family:var(--qm-display); font-weight:800; font-size:clamp(2.3rem, 5.2vw, 3.7rem);
  line-height:1.02; letter-spacing:-.035em; color:var(--qm-ink); margin:0 0 .7rem; padding:0; }
.qm-hero p.lede { font-size:1.05rem; color:var(--qm-muted); max-width:62ch; margin:0 0 1.1rem; line-height:1.5; }
.qm-tables { display:flex; flex-wrap:wrap; gap:.4rem; }
.qm-table { display:inline-flex; align-items:baseline; gap:.45rem; background:#fff; border:1px solid var(--qm-line);
  border-radius:8px; padding:.28rem .6rem; font-size:.84rem; color:var(--qm-ink); }
.qm-table em { font-style:normal; color:var(--qm-muted); font-size:.76rem; font-variant-numeric:tabular-nums; }
.qm-table.view { border-style:dashed; }

/* Compact header (during a conversation) ------------------------------- */
.qm-bar { display:flex; align-items:center; justify-content:space-between; gap:1rem; flex-wrap:wrap;
  padding:.4rem 0 .9rem; border-bottom:1px solid var(--qm-line); margin-bottom:1rem; }
.qm-bar h2 { font-family:var(--qm-display); font-weight:800; font-size:1.5rem; letter-spacing:-.02em; margin:0; padding:0; }
.qm-pills { display:flex; gap:.35rem; flex-wrap:wrap; }
.qm-pill { font-size:.76rem; color:var(--qm-muted); background:#fff; border:1px solid var(--qm-line);
  border-radius:999px; padding:.18rem .6rem; white-space:nowrap; }
.qm-pill.write { color:#9A3412; border-color:#F5C9A8; background:#FFF6EE; }

/* Starter questions ----------------------------------------------------- */
.qm-section { font-family:var(--qm-display); font-weight:600; font-size:1.05rem; margin:1.4rem 0 .6rem; color:var(--qm-ink); }
.st-key-starters button { justify-content:flex-start; text-align:left; background:#fff; border:1px solid var(--qm-line);
  min-height:64px; padding:.8rem 1rem; height:100%; }
.st-key-starters button > div, .st-key-starters button [data-testid="stMarkdownContainer"] { justify-content:flex-start; width:100%; }
.st-key-starters button p { font-size:.95rem; line-height:1.35; text-align:left; white-space:normal;
  overflow:visible; text-overflow:clip; }
.st-key-starters button:hover { border-color:var(--qm-cobalt); color:var(--qm-cobalt); background:#fff; }

/* Chat ------------------------------------------------------------------ */
[data-testid="stChatMessage"] { background:#fff; border:1px solid var(--qm-line); border-radius:14px;
  padding:1rem 1.15rem; margin-bottom:.8rem; gap:.8rem; }
[data-testid="stChatMessage"]:has(.qm-user) { background:var(--qm-cobalt); border-color:var(--qm-cobalt);
  margin-left:auto; width:fit-content; max-width:78%; padding:.7rem 1rem; }
[data-testid="stChatMessage"]:has(.qm-user) p { color:#fff; margin:0; }
[data-testid="stChatMessage"]:has(.qm-user) [data-testid="stChatMessageContent"] { padding:0; margin:0; align-self:center; }
[data-testid="stChatMessage"]:has(.qm-user) [data-testid="stMarkdown"] *,
[data-testid="stChatMessage"]:has(.qm-user) [data-testid="stMarkdownContainer"] { margin-bottom:0 !important; height:auto !important; }
[data-testid="stChatMessage"]:has(.qm-user) [data-testid^="stChatMessageAvatar"] { display:none; }
.qm-user { display:none; }
[data-testid="stChatMessage"]:has(.qm-user) [data-testid="stMarkdownContainer"] { color:#fff; }

/* Result meta + SQL ---------------------------------------------------- */
.qm-meta { display:flex; gap:.35rem; flex-wrap:wrap; margin:.25rem 0 .5rem; }
.qm-tag { font-size:.75rem; border-radius:6px; padding:.12rem .5rem; background:var(--qm-cobalt-soft); color:#2346B8;
  font-variant-numeric:tabular-nums; }
.qm-tag.sql { background:#FDF1DC; color:#8A5A0B; }
.qm-tag.fix { background:#E3F6EF; color:#13684F; }
.qm-tag.warn { background:#FFF1E6; color:#9A3412; }
[data-testid="stChatMessage"] [data-testid="stExpander"] details { border-radius:10px; border-color:var(--qm-line); background:#FAFBFD; }
[data-testid="stCode"] pre, .stCode pre { border-left:3px solid var(--qm-amber); }
.qm-caution { border-left:3px solid #E8823A; background:#FFF6EE; color:#7A3410; padding:.45rem .7rem;
  border-radius:6px; font-size:.88rem; margin:.35rem 0; }
.qm-hint { color:var(--qm-muted); font-size:.82rem; margin:.6rem 0 .2rem; }

/* Chat input ------------------------------------------------------------ */
[data-testid="stChatInput"] { border:1.5px solid var(--qm-cobalt) !important; border-radius:14px !important;
  box-shadow:0 8px 28px rgba(47,91,234,.14); background:#fff !important; }
[data-testid="stChatInput"] > div, [data-testid="stChatInput"] textarea { background:#fff !important; }
[data-testid="stBottom"] > div { background:linear-gradient(to top, var(--qm-paper) 70%, rgba(244,246,250,0)); }

/* Workbench ------------------------------------------------------------- */
.st-key-wb_sql textarea { font-family:'JetBrains Mono', ui-monospace, monospace; font-size:.9rem;
  border-left:3px solid var(--qm-amber); background:#fff; }
.st-key-wb_sql [data-baseweb="textarea"] { background:#fff; }

/* Dashboard --------------------------------------------------------------- */
.qm-dash-title .t { font-family:var(--qm-display); font-weight:800; font-size:1.9rem; letter-spacing:-.025em; line-height:1.1;
  color:var(--qm-ink); margin:0 0 .2rem; }
.qm-dash-title span { color:var(--qm-muted); font-size:.82rem; }
.qm-kpis { display:grid; grid-template-columns:repeat(auto-fit, minmax(170px, 1fr)); gap:.75rem; margin:.4rem 0 1rem; }
.qm-kpi { background:#fff; border:1px solid var(--qm-line); border-radius:12px; padding:.85rem 1rem .8rem; }
.qm-kpi .lbl { color:var(--qm-muted); font-size:.82rem; margin-bottom:.2rem; }
.qm-kpi .val { font-family:var(--qm-display); font-weight:700; font-size:1.75rem; letter-spacing:-.02em; color:var(--qm-ink);
  font-variant-numeric:tabular-nums; line-height:1.15; }
.qm-delta { display:inline-block; margin-top:.25rem; font-size:.76rem; font-variant-numeric:tabular-nums; }
.qm-delta.up { color:#13684F; } .qm-delta.down { color:#B42318; } .qm-delta.flat { color:var(--qm-muted); }
.qm-chips { display:flex; flex-wrap:wrap; align-items:center; gap:.4rem; font-size:.82rem; color:var(--qm-muted); margin:.2rem 0 .6rem; }
.qm-chip { background:var(--qm-cobalt-soft); color:#2346B8; border-radius:999px; padding:.15rem .65rem; font-size:.8rem; }
.qm-hint-inline { font-size:.76rem; color:var(--qm-muted); }
.qm-db-head { display:flex; align-items:baseline; gap:.6rem; margin:1.6rem 0 .6rem; padding-top:1rem; border-top:1px solid var(--qm-line); }
.qm-db-head:first-of-type { border-top:none; }
.qm-db-head b { font-family:var(--qm-display); font-size:1.35rem; font-weight:800; letter-spacing:-.02em; color:var(--qm-ink); }
.qm-db-head span { color:var(--qm-muted); font-size:.82rem; }
.qm-db-head .dot { width:10px; height:10px; border-radius:3px; background:var(--qm-cobalt); align-self:center; }
[data-testid="stVerticalBlockBorderWrapper"] { background:#fff; border-radius:12px; }

/* Danger zone: clear all chat history ------------------------------------ */
.st-key-danger_zone { margin-top:.6rem; }
.st-key-clear_history button, .st-key-clear_confirm button {
  background:#C62828 !important; border-color:#C62828 !important; color:#fff !important; }
.st-key-clear_history button p, .st-key-clear_confirm button p,
.st-key-clear_history button span, .st-key-clear_confirm button span { color:#fff !important; }
.st-key-clear_history button:hover, .st-key-clear_confirm button:hover {
  background:#A61F1F !important; border-color:#A61F1F !important; }
.st-key-clear_history button:disabled { background:#5B2A33 !important; border-color:#5B2A33 !important; opacity:.6; }
section[data-testid="stSidebar"] .st-key-danger_zone .qm-caution { background:#3A1E28; color:#FFD7D7; border-left-color:#E05252; }

/* Sidebar details ------------------------------------------------------- */
section[data-testid="stSidebar"] [data-testid="stExpander"] details { border-color:#2B3C63; background:#172646; }
section[data-testid="stSidebar"] hr { border-color:#2B3C63; }
section[data-testid="stSidebar"] .stCaption, section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] { color:#9FB0CF; }

@media (max-width: 640px) {
  [data-testid="stChatMessage"]:has(.qm-user) { max-width:92%; }
  .block-container { padding-top:3.4rem; }
  .qm-hero { padding-top:.2rem; }
}
</style>
"""

# A database cylinder whose base turns into a speech-bubble tail: "talk to your data".
BRAND_SVG = """
<svg width="30" height="32" viewBox="0 0 30 32" aria-hidden="true">
  <ellipse cx="15" cy="6" rx="12" ry="4.5" fill="#F2A93B"/>
  <path d="M3 6v14c0 2.5 5.4 4.5 12 4.5c1.3 0 2.5-.1 3.7-.2L24 29l-.6-5.6C25.6 22.6 27 21.4 27 20V6
           c0 2.5-5.4 4.5-12 4.5S3 8.5 3 6z" fill="#F2A93B" opacity=".9"/>
  <path d="M3 13c0 2.5 5.4 4.5 12 4.5S27 15.5 27 13" stroke="#13203B" stroke-width="1.6" fill="none" opacity=".55"/>
</svg>
"""


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def brand_html() -> str:
    return (f'<div class="qm-brand">{BRAND_SVG}<span>QueryMind</span></div>'
            '<p class="qm-brand-sub">Plain English in, real SQL out.</p>')


def current_db_html(label: str, detail: str) -> str:
    return (f'<div class="qm-current"><span class="dot"></span><div><b>{esc(label)}</b>'
            f'<small>{esc(detail)}</small></div></div>')


def short_name(label: str) -> str:
    """'Chinook — digital music store' -> 'Chinook'."""
    for sep in (" — ", " - ", " (", ":"):
        if sep in label:
            return label.split(sep)[0].strip()
    return label.strip()


def hero_html(name: str, where: str, lede: str, tables: list[tuple[str, int | None, bool]], max_chips: int = 18) -> str:
    chips = []
    for t, rows, is_view in tables[:max_chips]:
        count = f"<em>{rows:,}</em>" if rows is not None else ""
        chips.append(f'<span class="qm-table{" view" if is_view else ""}">{esc(t)}{count}</span>')
    if len(tables) > max_chips:
        chips.append(f'<span class="qm-table"><em>+{len(tables) - max_chips} more</em></span>')
    return (f'<div class="qm-hero"><div class="where">{esc(where)}</div>'
            f'<h1>Ask {esc(name)} anything</h1>'
            + (f'<p class="lede">{esc(lede)}</p>' if lede else "")
            + f'<div class="qm-tables">{"".join(chips)}</div></div>')


def bar_html(name: str, pills: list[tuple[str, str]]) -> str:
    pill_html = "".join(f'<span class="qm-pill {cls}">{esc(text)}</span>' for text, cls in pills)
    return f'<div class="qm-bar"><h2>{esc(name)}</h2><div class="qm-pills">{pill_html}</div></div>'


def tags_html(tags: list[tuple[str, str]]) -> str:
    return '<div class="qm-meta">' + "".join(f'<span class="qm-tag {cls}">{esc(t)}</span>' for t, cls in tags) + "</div>"


def caution_html(text: str) -> str:
    return f'<div class="qm-caution">{esc(text)}</div>'
