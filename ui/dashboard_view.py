"""The Dashboard tab: KPI cards, a filter row, cross-filtering charts, pinned chat answers and Power BI export."""
from __future__ import annotations

import re
from datetime import datetime

import pandas as pd
import streamlit as st

from core.charts import PLOTLY_CONFIG, build_figure
from core.dashboard import (
    AGGS, CHART_TYPES, DashboardStore, Filters, ai_spec, auto_spec, builtin_spec, chart_frame, chart_spec_for_figure,
    column_roles, comparison_view, export_multi_powerbi, export_powerbi, find_shared_fields, format_value,
    key_candidates, kpi_value, load_fact, merged_frame, normalize_key, pretty_label, previous_period,
)
from core.db import DatabaseManager
from ui import theme

CLICKABLE = ("bar", "pie")


@st.cache_data(ttl=900, show_spinner=False, max_entries=8)
def _cached_fact(url: str, sql: str, date_col: str | None, timeout: int) -> pd.DataFrame:
    return load_fact(DatabaseManager(url, timeout), sql, date_col)


def _store() -> DashboardStore:
    ss = st.session_state
    if "dash_store" not in ss:
        ss.dash_store = DashboardStore()
    return ss.dash_store


def resolve_spec(key: str, db=None, label: str = "") -> tuple[dict, str]:
    """User-saved spec → bundled dashboard for this dataset → automatic design. Returns (spec, origin)."""
    ss = st.session_state
    db = db or ss.db
    label = label or (ss.source["label"] if ss.source else "")
    saved = _store().get(key).get("spec")
    if saved:
        return saved, "custom"
    kind, _, ident = key.partition(":")
    if kind == "dataset":
        spec = builtin_spec(ident)
        if spec:
            return spec, "built-in"
    auto = ss.setdefault("dash_auto", {})
    if key not in auto:
        auto[key] = auto_spec(db, title=(theme.short_name(label) + " overview") if label else "")
    return auto[key], "automatic"


# ------------------------------------------------------------------ pieces
def _kpi_cards(spec: dict, current: pd.DataFrame, previous: pd.DataFrame | None, period_label: str) -> None:
    cards = []
    for k in spec.get("kpis", []):
        v = kpi_value(current, k)
        delta_html = ""
        if previous is not None and len(previous):
            p = kpi_value(previous, k)
            if p and not pd.isna(p) and not pd.isna(v):
                change = (v - p) / abs(p) * 100
                cls = "up" if change > 0.05 else "down" if change < -0.05 else "flat"
                arrow = "▲" if cls == "up" else "▼" if cls == "down" else "■"
                delta_html = f'<span class="qm-delta {cls}">{arrow} {abs(change):.1f}% vs {period_label}</span>'
        cards.append(f'<div class="qm-kpi"><div class="lbl">{theme.esc(k["label"])}</div>'
                     f'<div class="val">{format_value(v, k.get("format", "num"))}</div>{delta_html}</div>')
    st.markdown(f'<div class="qm-kpis">{"".join(cards)}</div>', unsafe_allow_html=True)


def _highlight(fig, selected: list[str]) -> None:
    """Power BI-style highlight: keep the selected categories bright, fade the rest."""
    sel = {str(s) for s in selected}
    for trace in fig.data:
        if trace.type == "pie":
            labels = [str(lbl) for lbl in trace.labels]
            trace.pull = [0.06 if lbl in sel else 0 for lbl in labels]
            trace.opacity = 1
            colors = list(trace.marker.colors or [])
            if colors:
                trace.marker.colors = [c if lbl in sel else "#D5DBE6" for c, lbl in zip(colors, labels)]
        elif trace.type == "bar":
            raw = trace.y if trace.orientation == "h" else trace.x
            cats = [str(c) for c in (raw if raw is not None else [])]
            if len(cats) == 1:
                trace.opacity = 1 if cats[0] in sel else 0.28
            elif cats:
                trace.marker.opacity = [1 if c in sel else 0.28 for c in cats]


def _clicked_value(event, fig) -> str | None:
    try:
        points = event.selection.points if event else []
    except AttributeError:
        points = (event or {}).get("selection", {}).get("points", [])
    if not points:
        return None
    pt = points[0]
    if pt.get("label") is not None:
        return str(pt["label"])
    horizontal = any(getattr(t, "orientation", None) == "h" for t in fig.data)
    value = pt.get("y") if horizontal else pt.get("x")
    return None if value is None else str(value)


def _render_chart(chart: dict, frame: pd.DataFrame, key: str, idx: int, clicked: dict[str, list]) -> None:
    ss = st.session_state
    with st.container(border=True):
        if chart["type"] == "table":
            st.markdown(f"**{theme.esc(chart.get('title', 'Table'))}**")
            st.dataframe(frame, hide_index=True, width="stretch", height=340)
            return
        spec = chart_spec_for_figure(chart, frame)
        fig = build_figure(frame, spec, chart.get("title", "")) if spec else None
        if fig is None:
            st.markdown(f"**{theme.esc(chart.get('title', 'Chart'))}**")
            st.caption("No data for the current filters.")
            return
        fig.update_layout(height=360, margin=dict(l=8, r=24, t=48, b=8))
        dim = chart.get("x")
        clickable = chart["type"] in CLICKABLE and not chart.get("grain")
        if clickable and clicked.get(dim):
            _highlight(fig, clicked[dim])
        wkey = f"dash_{key}_{idx}_{ss.get('dash_ver', 0)}"
        if clickable:
            event = st.plotly_chart(fig, width="stretch", key=wkey, config=PLOTLY_CONFIG,
                                    on_select="rerun", selection_mode="points")
            value = _clicked_value(event, fig)
            seen_key = f"seen_{wkey}"
            if value is not None and ss.get(seen_key) != value:
                ss[seen_key] = value
                current = clicked.get(dim, [])
                clicked[dim] = [] if current == [value] else [value]  # click again to clear
                if not clicked[dim]:
                    clicked.pop(dim)
                st.rerun()
        else:
            st.plotly_chart(fig, width="stretch", key=wkey, config=PLOTLY_CONFIG)


def _render_pins(db, key: str) -> None:
    pins = _store().pins(key)
    if not pins:
        return
    st.markdown('<div class="qm-section">Pinned from chat</div>', unsafe_allow_html=True)
    st.caption("These tiles re-run their saved SQL each time. Slicers above don't apply to them.")
    cols = st.columns(2)
    for i, pin in enumerate(pins):
        with cols[i % 2], st.container(border=True):
            res = db.execute(pin["sql"], read_only=True, row_limit=500)
            if not res.ok or res.df is None or res.df.empty:
                st.markdown(f"**{theme.esc(pin['title'])}**")
                st.caption(f"Couldn't refresh: {res.error or 'no rows'}")
            else:
                fig = build_figure(res.df, pin.get("chart"), pin["title"]) if pin.get("chart") else None
                if fig is not None:
                    fig.update_layout(height=340)
                    st.plotly_chart(fig, width="stretch", key=f"pin_{pin['id']}", config=PLOTLY_CONFIG)
                else:
                    st.markdown(f"**{theme.esc(pin['title'])}**")
                    st.dataframe(res.df, hide_index=True, width="stretch", height=300)
            if pin.get("question"):
                st.caption(f"Asked: {pin['question']}")
            if st.button("Unpin", icon=":material/keep_off:", key=f"unpin_{pin['id']}"):
                _store().unpin(key, pin["id"])
                st.rerun()


def _customize(spec: dict, origin: str, fact: pd.DataFrame, key: str, get_agent) -> None:
    ss = st.session_state
    roles = column_roles(fact, spec.get("date"))
    with st.expander("Customize this dashboard", icon=":material/tune:"):
        st.caption(f"Current design: {origin}. Changes are saved for this database only.")
        title = st.text_input("Title", spec.get("title", ""), key=f"cz_title_{key}")
        slicers = st.multiselect("Slicers (filters)", roles["dimensions"],
                                 default=[s for s in spec.get("slicers", []) if s in roles["dimensions"]], key=f"cz_sl_{key}")
        titles = [c.get("title", f"Chart {i + 1}") for i, c in enumerate(spec.get("charts", []))]
        hide = st.multiselect("Remove charts", titles, key=f"cz_hide_{key}")

        st.markdown("**Add a chart**")
        c1, c2, c3 = st.columns(3)
        ctype = c1.selectbox("Type", [t for t in CHART_TYPES if t != "table"], key=f"cz_type_{key}")
        x_opts = roles["dates"] + roles["dimensions"] + (roles["measures"] if ctype in ("scatter", "histogram") else [])
        x = c2.selectbox("X axis / category", x_opts, key=f"cz_x_{key}") if x_opts else None
        y_opts = ["(count rows)"] + roles["measures"] + [c for c in fact.columns if c not in roles["measures"]]
        y = c3.selectbox("Value", y_opts, key=f"cz_y_{key}") if ctype != "histogram" else None
        c4, c5, c6 = st.columns(3)
        agg = c4.selectbox("Aggregation", AGGS, key=f"cz_agg_{key}") if ctype not in ("scatter", "histogram") else None
        top = c5.number_input("Top N (0 = all)", 0, 50, 10, key=f"cz_top_{key}") if ctype in ("bar", "pie") else 0
        color = c6.selectbox("Split by (optional)", ["(none)"] + roles["dimensions"], key=f"cz_col_{key}") \
            if ctype in ("bar", "line", "scatter") else "(none)"
        new_title = st.text_input("Chart title", key=f"cz_ctitle_{key}", placeholder="e.g. Revenue by city")
        add = st.checkbox("Add this chart when saving", key=f"cz_add_{key}")

        b1, b2, b3, b4 = st.columns(4)
        if b1.button("Save changes", type="primary", width="stretch", key=f"cz_save_{key}"):
            new = dict(spec)
            new["title"] = title or spec.get("title", "")
            new["slicers"] = slicers
            new["charts"] = [c for c, t in zip(spec.get("charts", []), titles) if t not in hide]
            if add and x:
                chart = {"title": new_title or f"{pretty_label(y if y and y != '(count rows)' else 'rows')} by {pretty_label(x).lower()}",
                         "type": ctype, "x": x}
                if ctype not in ("scatter", "histogram"):
                    chart.update(agg="count" if y == "(count rows)" else agg, y=None if y == "(count rows)" else y,
                                 y_label="Rows" if y == "(count rows)" else f"{pretty_label(agg)} of {pretty_label(y).lower()}")
                elif ctype == "scatter":
                    chart["y"] = y if y != "(count rows)" else None
                if x in roles["dates"] and ctype in ("line", "bar"):
                    chart["grain"] = "month"
                if top:
                    chart["top"] = int(top)
                if color and color != "(none)":
                    chart["color"] = color
                new["charts"].append(chart)
            _store().save_spec(key, new)
            ss.pop("powerbi_pack", None)
            st.rerun()
        if b2.button("Reset to default", width="stretch", key=f"cz_reset_{key}"):
            _store().save_spec(key, None)
            ss.get("dash_auto", {}).pop(key, None)
            st.rerun()
        if b3.button("Auto-design", width="stretch", key=f"cz_auto_{key}",
                     help="Build a dashboard from the largest table's dates, numbers and categories"):
            _store().save_spec(key, auto_spec(ss.db, title=spec.get("title", "")))
            st.rerun()
        agent = get_agent()
        if b4.button("Design with AI", icon=":material/auto_awesome:", width="stretch", key=f"cz_ai_{key}",
                     disabled=agent is None, help="Let the model design KPIs and charts from your schema"):
            with st.spinner("Designing a dashboard from your schema…"):
                try:
                    _store().save_spec(key, ai_spec(ss.db, agent.llm, agent.dialect_label))
                    st.rerun()
                except Exception as e:  # noqa: BLE001
                    st.error(f"The AI design didn't work out: {e}")


# ------------------------------------------------------------------ main
def dashboard_view(get_agent, header, linked_sources=None, source_options=None) -> None:
    ss = st.session_state
    if ss.db is None:
        st.info("Connect a database first.")
        return
    key = ss.conv.source_key or ss.db.url
    db = ss.db
    linked = linked_sources() if linked_sources else []

    # ---- mode switch + "compare with" picker (always visible)
    modes = {"single": ":material/dashboard: This database", "side": ":material/view_column: Side by side",
             "merged": ":material/join: Merged by a shared field"}
    saved_modes = ss.setdefault("dmode_saved", {})
    if ss.pop("dmode_force", None) == key:  # set before the widget exists (Streamlit rule)
        saved_modes[key] = "side"
        ss[f"dmode_{key}"] = "side"
    m_col, c_col = st.columns([3, 2], vertical_alignment="bottom")
    with m_col:
        if ss.get(f"dmode_{key}") not in modes:  # first visit, or state dropped by an interrupted rerun
            ss[f"dmode_{key}"] = saved_modes.get(key, "side" if linked else "single")
        mode = st.segmented_control("Dashboard mode", list(modes), format_func=modes.get, key=f"dmode_{key}",
                                    label_visibility="collapsed")
        if mode in modes:
            saved_modes[key] = mode
        else:
            mode = saved_modes.get(key, "side" if linked else "single")
    if source_options is not None:
        names = {k: v for k, v in source_options().items() if k != key}
        current = [k for k in ss.get("linked_sel", {}).get(key, []) if k in names]
        with c_col:
            picked = st.multiselect("Compare with other databases", list(names), default=current,
                                    format_func=names.get, placeholder="Add databases to compare…",
                                    key=f"cmp_{key}_{'-'.join(current)}")
        if picked != current:
            ss.setdefault("linked_sel", {})[key] = picked
            if picked and mode == "single":
                ss.dmode_force = key
            st.rerun()

    if mode != "single":
        if not linked:
            st.info("Pick one or more databases in **Compare with other databases** (top right) to see them "
                    "side by side or merged on a shared field such as country.", icon=":material/join:")
            return
        main = {"key": key, "alias": _alias(key, ss.source["label"] if ss.source else "main"),
                "label": theme.short_name(ss.source["label"]) if ss.source else "Main", "db": db}
        multi_view([main] + linked, mode)
        return
    try:
        spec, origin = resolve_spec(key)
    except Exception as e:  # noqa: BLE001
        st.error(f"Couldn't build a dashboard for this database: {e}")
        return

    try:
        with st.spinner("Loading dashboard data…"):
            fact = _cached_fact(db.url, spec["fact_sql"], spec.get("date"), ss.settings.query_timeout_s)
    except Exception as e:  # noqa: BLE001
        st.error(f"The dashboard query failed: {e}")
        if origin != "built-in" and st.button("Reset to default"):
            _store().save_spec(key, None)
            st.rerun()
        return

    date_col = spec.get("date") if spec.get("date") in fact else None
    clicked: dict[str, list] = ss.setdefault("dash_click", {}).setdefault(key, {})

    # ---- title bar
    left, right = st.columns([3, 2], vertical_alignment="center")
    with left:
        st.markdown(f'<div class="qm-dash-title"><div class="t">{theme.esc(spec.get("title", "Dashboard"))}</div>'
                    f'<span>{theme.esc(ss.source["label"] if ss.source else db.display_name)} · {len(fact):,} rows · '
                    f'{origin} design · refreshed {datetime.now():%H:%M}</span></div>', unsafe_allow_html=True)
    with right:
        r1, r2 = st.columns(2)
        if r1.button("Refresh data", icon=":material/refresh:", width="stretch"):
            _cached_fact.clear()
            ss.pop("powerbi_pack", None)
            st.rerun()
        if r2.button("Clear filters", icon=":material/filter_alt_off:", width="stretch"):
            ss.dash_click[key] = {}
            for k in [k for k in ss.keys() if str(k).startswith((f"sl_{key}_", f"date_{key}", f"preset_{key}"))]:
                del ss[k]
            ss.dash_ver = ss.get("dash_ver", 0) + 1
            st.rerun()

    # ---- filter row: date first, then slicers
    slicers = [s for s in spec.get("slicers", []) if s in fact.columns]
    n = (1 if date_col else 0) + len(slicers)
    filter_cols = st.columns(max(n, 1)) if n else []
    date_range = full_range = None
    i = 0
    if date_col:
        dmin, dmax = fact[date_col].min(), fact[date_col].max()
        if pd.notna(dmin) and pd.notna(dmax):
            full_range = (dmin.date(), dmax.date())
            with filter_cols[0]:
                preset = st.selectbox("Date range", ["All dates", "Latest 12 months", "Latest 3 months", "Custom"],
                                      key=f"preset_{key}")
                if preset == "Latest 12 months":
                    date_range = ((dmax - pd.DateOffset(months=12) + pd.Timedelta(days=1)).date(), dmax.date())
                elif preset == "Latest 3 months":
                    date_range = ((dmax - pd.DateOffset(months=3) + pd.Timedelta(days=1)).date(), dmax.date())
                elif preset == "Custom":
                    picked = st.date_input("From – to", value=full_range, min_value=full_range[0],
                                           max_value=full_range[1], key=f"date_{key}", label_visibility="collapsed")
                    if isinstance(picked, (list, tuple)) and len(picked) == 2:
                        date_range = (picked[0], picked[1])
                if date_range and date_range[0] < full_range[0]:
                    date_range = (full_range[0], date_range[1])
            i = 1
    chosen: dict[str, list] = {}
    for s in slicers:
        with filter_cols[i]:
            values = sorted(fact[s].dropna().astype(str).unique().tolist())
            chosen[s] = st.multiselect(pretty_label(s), values, key=f"sl_{key}_{s}", placeholder="All")
        i += 1

    if clicked:
        chips = "".join(f'<span class="qm-chip">{theme.esc(pretty_label(d))}: {theme.esc(", ".join(v))}</span>'
                        for d, v in clicked.items())
        st.markdown(f'<div class="qm-chips">Clicked filters {chips}<span class="qm-hint-inline">click the same bar again '
                    f'or use Clear filters to remove</span></div>', unsafe_allow_html=True)

    base_filters = Filters(date_range=date_range, slicers=chosen)
    all_filters = Filters(date_range=date_range, slicers={**chosen, **{d: v for d, v in clicked.items()}})
    current = all_filters.apply(fact, date_col)
    prev_range = previous_period(date_range, full_range)
    previous = None
    if prev_range:
        previous = Filters(date_range=prev_range, slicers=all_filters.slicers).apply(fact, date_col)
    period_label = "previous period" if prev_range else ""

    # ---- KPI row
    _kpi_cards(spec, current, previous, period_label)
    if current.empty:
        st.warning("No rows match these filters. Widen the date range or clear some filters.", icon=":material/filter_alt:")

    # ---- chart grid (cross-filtered)
    charts = spec.get("charts", [])
    frames: dict[str, pd.DataFrame] = {}
    for row_start in range(0, len(charts), 2):
        cols = st.columns(2)
        for j, chart in enumerate(charts[row_start:row_start + 2]):
            idx = row_start + j
            dim = chart.get("x")
            # the chart you clicked keeps all its categories (highlighted); every other visual filters
            own = Filters(date_range=date_range, slicers={**chosen, **{d: v for d, v in clicked.items() if d != dim}}) \
                if chart["type"] in CLICKABLE and not chart.get("grain") else all_filters
            frame = chart_frame(own.apply(fact, date_col), chart)
            frames[chart.get("title", f"Chart {idx + 1}")] = frame
            with cols[j]:
                _render_chart(chart, frame, key, idx, clicked)

    _render_pins(db, key)

    # ---- Power BI export + customize
    st.markdown('<div class="qm-section">Open in Power BI</div>', unsafe_allow_html=True)
    e1, e2 = st.columns([2, 3], vertical_alignment="center")
    with_filters = e2.checkbox("Export only the filtered rows", value=bool(date_range or any(chosen.values()) or clicked))
    if e1.button("Prepare Power BI pack", icon=":material/package_2:", width="stretch"):
        with st.spinner("Building the export (CSV, Excel, DAX measures, Power Query script, guide)…"):
            data = current if with_filters else fact
            kpi_rows = [{"KPI": k["label"], "Value": kpi_value(data, k),
                         "Formatted": format_value(kpi_value(data, k), k.get("format", "num"))} for k in spec.get("kpis", [])]
            ss.powerbi_pack = (key, export_powerbi(spec, data, frames, kpi_rows,
                                                   ss.source["label"] if ss.source else db.display_name, with_filters))
    pack = ss.get("powerbi_pack")
    if pack and pack[0] == key:
        st.download_button("Download Power BI pack (.zip)", pack[1], icon=":material/download:", type="primary",
                           file_name=f"{theme.short_name(ss.source['label'] if ss.source else 'dashboard').lower().replace(' ', '_')}_powerbi.zip",
                           mime="application/zip")
        st.caption("Unzip it and follow POWERBI_README.md: load fact.csv (or dashboard.xlsx), paste the DAX measures, "
                   "and rebuild the same visuals. About 10 minutes in Power BI Desktop.")

    _customize(spec, origin, fact, key, get_agent)


# ================================================================== several databases
def _alias(key: str, label: str) -> str:
    kind, _, ident = key.partition(":")
    return re.sub(r"[^a-z0-9]+", "_", (ident if kind == "dataset" else label).lower()).strip("_")[:30] or "db"


def _date_window(fact: pd.DataFrame, date_col: str | None, preset: str):
    if not date_col or date_col not in fact or preset == "All dates":
        return None
    dmax = fact[date_col].max()
    if pd.isna(dmax):
        return None
    months = 12 if preset == "Latest 12 months" else 3
    return ((dmax - pd.DateOffset(months=months) + pd.Timedelta(days=1)).date(), dmax.date())


def _load_sections(sources: list[dict]) -> list[dict]:
    ss = st.session_state
    out = []
    for src in sources:
        try:
            spec, origin = resolve_spec(src["key"], src["db"], src["label"])
            fact = _cached_fact(src["db"].url, spec["fact_sql"], spec.get("date"), ss.settings.query_timeout_s)
        except Exception as e:  # noqa: BLE001
            st.warning(f"Skipped {src['label']}: {e}")
            continue
        out.append({**src, "spec": spec, "origin": origin, "fact": fact,
                    "date": spec.get("date") if spec.get("date") in fact else None})
    return out


def multi_view(sources: list[dict], mode: str) -> None:
    ss = st.session_state
    with st.spinner("Loading every database's dashboard data…"):
        sections = _load_sections(sources)
    if len(sections) < 2:
        st.info("Add at least one more database under **Combine with other databases** in the sidebar.")
        return
    names = ", ".join(sec["label"] for sec in sections)
    left, right = st.columns([3, 1], vertical_alignment="bottom")
    left.markdown(f'<div class="qm-dash-title"><div class="t">{"Side by side" if mode == "side" else "Merged view"}</div>'
                  f'<span>{theme.esc(names)} · refreshed {datetime.now():%H:%M}</span></div>', unsafe_allow_html=True)
    group_key = "|".join(sec["key"] for sec in sections)
    preset = right.selectbox("Date range", ["All dates", "Latest 12 months", "Latest 3 months"], key=f"mpreset_{group_key}",
                             help="Applied to every database, relative to each one's own latest date")
    for sec in sections:
        sec["window"] = _date_window(sec["fact"], sec["date"], preset)
    if mode == "side":
        _side_by_side(sections)
    else:
        _merged(sections, group_key)


def _side_by_side(sections: list[dict]) -> None:
    ss = st.session_state
    for sec in sections:
        key, spec, fact = sec["key"], sec["spec"], sec["fact"]
        st.markdown(f'<div class="qm-db-head"><span class="dot"></span><b>{theme.esc(sec["label"])}</b>'
                    f'<span>{theme.esc(spec.get("title", ""))} · {len(fact):,} rows · {sec["origin"]} design</span></div>',
                    unsafe_allow_html=True)
        slicers = [s_ for s_ in spec.get("slicers", []) if s_ in fact.columns][:4]
        chosen = {}
        if slicers:
            cols = st.columns(len(slicers))
            for col, s_ in zip(cols, slicers):
                with col:
                    values = sorted(fact[s_].dropna().astype(str).unique().tolist())
                    chosen[s_] = st.multiselect(pretty_label(s_), values, key=f"msl_{key}_{s_}", placeholder="All")
        clicked = ss.setdefault("dash_click", {}).setdefault(key, {})
        flt = Filters(date_range=sec["window"], slicers={**chosen, **clicked})
        current = flt.apply(fact, sec["date"])
        full = (fact[sec["date"]].min().date(), fact[sec["date"]].max().date()) if sec["date"] else None
        prev_range = previous_period(sec["window"], full)
        previous = Filters(date_range=prev_range, slicers=flt.slicers).apply(fact, sec["date"]) if prev_range else None
        _kpi_cards(spec, current, previous, "previous period" if prev_range else "")
        charts = spec.get("charts", [])[:4]
        for row in range(0, len(charts), 2):
            cols = st.columns(2)
            for j, chart in enumerate(charts[row:row + 2]):
                dim = chart.get("x")
                own = Filters(date_range=sec["window"], slicers={**chosen, **{d: v for d, v in clicked.items() if d != dim}}) \
                    if chart["type"] in CLICKABLE and not chart.get("grain") else flt
                with cols[j]:
                    _render_chart(chart, chart_frame(own.apply(fact, sec["date"]), chart), key, row + j, clicked)
        sec["current"] = current
    st.caption("Each database keeps its own filters and click-filters. Switch to **Merged by a shared field** to compare "
               "them on one chart.")
    _multi_export(sections, None, None)


def _measure_options(sec: dict) -> list[dict]:
    opts = [k for k in sec["spec"].get("kpis", []) if "ratio" not in k]
    return opts or [{"label": "Rows", "col": sec["fact"].columns[0], "agg": "count"}]


def _merged(sections: list[dict], group_key: str) -> None:
    ss = st.session_state
    main = sections[0]
    facts = {sec["alias"]: Filters(date_range=sec["window"]).apply(sec["fact"], sec["date"]) for sec in sections}
    cache = ss.setdefault("shared_fields", {})
    if group_key not in cache:
        with st.spinner("Looking for a field the databases share…"):
            cache[group_key] = find_shared_fields({s["alias"]: s["fact"] for s in sections}, main["alias"])
    detected = cache[group_key]

    with st.expander("Matching and measures", icon=":material/link:", expanded=not detected):
        if detected:
            cov = ", ".join(f"{s['label']} {detected['overlap'][s['alias']]:.0%}" for s in sections[1:])
            st.caption(f"Detected automatically by comparing values (names like 'USA' and 'United States' count as the same). "
                       f"Share of {main['label']}'s values found in: {cov}.")
        else:
            st.warning("No shared field was found automatically. Pick the matching columns yourself below.")
        fields, measures = {}, {}
        cols = st.columns(len(sections))
        for col, sec in zip(cols, sections):
            with col:
                st.markdown(f"**{theme.esc(sec['label'])}**")
                cands = key_candidates(sec["fact"]) or list(sec["fact"].columns)
                default = detected.get("fields", {}).get(sec["alias"]) if detected else None
                fields[sec["alias"]] = st.selectbox("Shared field", cands, index=cands.index(default) if default in cands else 0,
                                                    key=f"mf_{group_key}_{sec['alias']}")
                opts = _measure_options(sec)
                # default: the first measure that isn't just counting the shared field itself (that would be 1 per value)
                default_i = next((i for i, o in enumerate(opts) if o.get("col") != fields[sec["alias"]]), 0)
                pick = st.selectbox("Measure", [o["label"] for o in opts], index=default_i,
                                    key=f"mm_{group_key}_{sec['alias']}")
                measures[sec["alias"]] = next(o for o in opts if o["label"] == pick)
        c1, c2 = st.columns(2)
        scale = c1.radio("Compare as", ["Auto", "Raw values", "% of each database's total"], horizontal=True,
                         key=f"mscale_{group_key}")
        top = c2.slider("Top N shared values", 5, 30, 12, key=f"mtop_{group_key}")

    labels = {s["alias"]: s["label"] for s in sections}
    merged = merged_frame(facts, fields, measures, labels)
    value_cols = [c for c in merged.columns if c != "shared_value"]
    in_all = merged.dropna(subset=value_cols)
    pick_vals = st.multiselect("Filter shared values (applies to every database)", sorted(in_all["shared_value"].astype(str)),
                               key=f"mvals_{group_key}", placeholder="All shared values")
    view_src = merged[merged["shared_value"].astype(str).isin(pick_vals)] if pick_vals else merged
    mode_key = {"Auto": "auto", "Raw values": "raw", "% of each database's total": "share"}[scale]
    view, used = comparison_view(view_src if not pick_vals else view_src, mode_key, top)

    # KPI cards: matched values + each database's total for its measure on matched values
    cards = [f'<div class="qm-kpi"><div class="lbl">Values in every database</div><div class="val">{len(in_all):,}</div>'
             f'<span class="qm-delta flat">of {len(merged):,} distinct {theme.esc(pretty_label(fields[main["alias"]]).lower())} values</span></div>']
    for c in value_cols:
        total = in_all[c].sum()
        alias = next(a for a in labels if c.startswith(labels[a] + ":"))
        fmt = measures[alias].get("format", "num")
        cards.append(f'<div class="qm-kpi"><div class="lbl">{theme.esc(c)}</div><div class="val">{format_value(total, fmt)}</div>'
                     f'<span class="qm-delta flat">on shared values</span></div>')
    st.markdown(f'<div class="qm-kpis">{"".join(cards)}</div>', unsafe_allow_html=True)

    if view.empty:
        st.warning("None of the selected values appear in every database.")
        return
    unit = "% of its own total" if used == "share" else "value"
    long = view.melt(id_vars="shared_value", var_name="Measure", value_name=("Share of total (%)" if used == "share" else "Value"))
    long["shared_value"] = long["shared_value"].astype(str)
    ycol = long.columns[-1]
    with st.container(border=True):
        fig = build_figure(long, {"type": "bar", "x": "shared_value", "y": ycol, "color": "Measure"},
                           f"{pretty_label(fields[main['alias']])}: each database side by side ({unit})")
        if fig is not None:
            fig.update_layout(height=max(420, 26 * len(view) * len(value_cols) + 140))
            st.plotly_chart(fig, width="stretch", key=f"mbar_{group_key}", config=PLOTLY_CONFIG)
        if used == "share":
            st.caption("Scales differ a lot between databases, so each bar is that database's share of its own total. "
                       "Switch to **Raw values** under Matching and measures to see the real numbers (also in the table below).")
    c1, c2 = st.columns(2)
    if len(value_cols) >= 2:
        with c1, st.container(border=True):
            pair = in_all[["shared_value"] + value_cols[:2]].copy()
            pair["shared_value"] = pair["shared_value"].astype(str)
            sc = build_figure(pair, {"type": "scatter", "x": value_cols[0], "y": value_cols[1], "color": None},
                              f"{value_cols[0]} vs {value_cols[1]}")
            if sc is not None:
                sc.update_layout(height=380)
                st.plotly_chart(sc, width="stretch", key=f"msc_{group_key}", config=PLOTLY_CONFIG)
            if len(pair) >= 3:
                corr = pair[value_cols[0]].corr(pair[value_cols[1]])
                st.caption(f"Correlation across {len(pair)} shared values: {corr:.2f} "
                           f"({'strong' if abs(corr) >= .7 else 'moderate' if abs(corr) >= .4 else 'weak'}"
                           f"{', positive' if corr > 0 else ', negative' if corr < 0 else ''}). Correlation is not causation.")
    with c2, st.container(border=True):
        st.markdown("**Coverage**")
        cover = pd.DataFrame({"Database": [labels[a] for a in labels],
                              "Distinct values": [merged[c].notna().sum() for c in value_cols],
                              "Only in this database": [(merged[c].notna() & merged[value_cols].notna().sum(axis=1).eq(1)).sum()
                                                        for c in value_cols]})
        st.dataframe(cover, hide_index=True, width="stretch")
        missing = merged[merged[value_cols].isna().any(axis=1)]["shared_value"].astype(str).tolist()
        if missing:
            st.caption("Not in every database: " + ", ".join(missing[:25]) + (" …" if len(missing) > 25 else ""))
    with st.expander("All numbers (raw values)", icon=":material/table:"):
        st.dataframe(merged.sort_values(value_cols[0], ascending=False), hide_index=True, width="stretch")
    shared = {"fields": fields, "overlap": detected.get("overlap", {}) if detected else {}}
    for sec in sections:
        sec["current"] = facts[sec["alias"]]
    _multi_export(sections, shared, merged)


def _multi_export(sections: list[dict], shared: dict | None, merged: pd.DataFrame | None) -> None:
    ss = st.session_state
    st.markdown('<div class="qm-section">Open in Power BI</div>', unsafe_allow_html=True)
    group = "|".join(s["key"] for s in sections)
    if st.button("Prepare Power BI pack (all databases)", icon=":material/package_2:", key=f"mexp_{group}"):
        with st.spinner("Building one fact table per database, the shared key table and DAX measures…"):
            packs = [{"alias": s["alias"], "label": s["label"], "spec": s["spec"], "fact": s.get("current", s["fact"])}
                     for s in sections]
            if shared is None:
                found = find_shared_fields({s["alias"]: s["fact"] for s in sections}, sections[0]["alias"])
                shared = {"fields": found["fields"], "overlap": found["overlap"]} if found else None
            ss.multi_pack = (group, export_multi_powerbi(packs, shared, merged))
    pack = ss.get("multi_pack")
    if pack and pack[0] == group:
        st.download_button("Download multi-database Power BI pack (.zip)", pack[1], icon=":material/download:",
                           type="primary", file_name="querymind_multi_db_powerbi.zip", mime="application/zip",
                           key=f"mdl_{group}")
        st.caption("One fact table per database plus a SharedKeys table: in Power BI, link SharedKeys to each fact table "
                   "and one slicer filters every database. POWERBI_README.md has the steps.")
