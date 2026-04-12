"""
charts.py — Auto chart-type detection and Plotly figure builder.
Detects the best chart type from DataFrame shape and column semantics,
then applies a polished dark theme with smooth gradients and refined styling.
"""

from __future__ import annotations
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from typing import Optional
import numpy as np

# ── Palette ─────────────────────────────────────────────────────────────────────

PRIMARY   = "#7c6af7"
SECONDARY = "#4af0c8"

# Main sequence — violet → teal → amber → rose → blue → emerald
COLORS = [
    "#7c6af7", "#4af0c8", "#f59e0b", "#f43f5e",
    "#3b82f6", "#10b981", "#f97316", "#a78bfa",
    "#06b6d4", "#84cc16",
]

# Gradient pairs for single-series charts (start, end)
GRAD_PAIRS = [
    ("#7c6af7", "#4af0c8"),
    ("#f59e0b", "#f43f5e"),
    ("#3b82f6", "#10b981"),
]

BG_MAIN    = "rgba(13,15,20,0)"          # transparent so Streamlit bg shows
BG_PLOT    = "rgba(20,23,32,0.85)"
GRID_COLOR = "rgba(37,42,58,0.8)"
TICK_COLOR = "#4a5568"
LABEL_COLOR= "#64748b"
TEXT_COLOR = "#e2e8f0"
BORDER     = "rgba(37,42,58,1)"

# ── Keyword sets ────────────────────────────────────────────────────────────────

TIME_KEYWORDS  = {"date","month","year","week","day","time",
                  "period","quarter","hour","created","updated","timestamp"}
RATIO_KEYWORDS = {"pct","percent","%","share","ratio","rate","margin","proportion"}
RANK_KEYWORDS  = {"rank","top","bottom","best","worst","most","least"}
GEO_KEYWORDS   = {"state","city","country","region","location","territory","zone","area"}


# ── Base layout ─────────────────────────────────────────────────────────────────

def _base_layout(title: str = "", height: int = 380) -> dict:
    return dict(
        paper_bgcolor = BG_MAIN,
        plot_bgcolor  = BG_PLOT,
        height        = height,
        font          = dict(family="'Space Grotesk', 'JetBrains Mono', sans-serif",
                             color=TEXT_COLOR, size=12),
        title         = dict(
            text      = f"<b>{title}</b>" if title else "",
            font      = dict(size=13, color="#94a3b8",
                             family="'Space Grotesk', sans-serif"),
            x=0.01, xanchor="left", y=0.98,
        ),
        xaxis = dict(
            gridcolor     = GRID_COLOR,
            linecolor     = BORDER,
            tickfont      = dict(color=TICK_COLOR, size=11),
            title_font    = dict(color=LABEL_COLOR, size=11),
            showgrid      = True,
            zeroline      = False,
            tickangle     = -30,
        ),
        yaxis = dict(
            gridcolor     = GRID_COLOR,
            linecolor     = BORDER,
            tickfont      = dict(color=TICK_COLOR, size=11),
            title_font    = dict(color=LABEL_COLOR, size=11),
            showgrid      = True,
            zeroline      = False,
            tickformat    = ",.0f",
        ),
        legend = dict(
            bgcolor      = "rgba(20,23,32,0.9)",
            bordercolor  = BORDER,
            borderwidth  = 1,
            font         = dict(color="#94a3b8", size=11),
            orientation  = "h",
            yanchor      = "bottom", y=1.02,
            xanchor      = "right",  x=1,
        ),
        margin      = dict(l=12, r=12, t=52, b=40),
        hoverlabel  = dict(
            bgcolor    = "#1c2030",
            bordercolor= "#7c6af7",
            font       = dict(color="#e2e8f0", size=12,
                              family="'Space Grotesk', sans-serif"),
        ),
        bargap      = 0.25,
        bargroupgap = 0.08,
    )


# ── Gradient bar color scale ─────────────────────────────────────────────────────

def _gradient_colors(n: int, pair_idx: int = 0) -> list[str]:
    """Generate n colors interpolated between a gradient pair."""
    if n <= 1:
        return [GRAD_PAIRS[pair_idx][0]]
    c1 = _hex_to_rgb(GRAD_PAIRS[pair_idx][0])
    c2 = _hex_to_rgb(GRAD_PAIRS[pair_idx][1])
    result = []
    for i in range(n):
        t = i / (n - 1)
        r = int(c1[0] + (c2[0] - c1[0]) * t)
        g = int(c1[1] + (c2[1] - c1[1]) * t)
        b = int(c1[2] + (c2[2] - c1[2]) * t)
        result.append(f"rgb({r},{g},{b})")
    return result


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgba(hex_color: str, alpha: float) -> str:
    r, g, b = _hex_to_rgb(hex_color)
    return f"rgba({r},{g},{b},{alpha})"


# ── Detection ───────────────────────────────────────────────────────────────────

def _col_has(col: str, keywords: set) -> bool:
    return any(kw in col.lower() for kw in keywords)


def _detect(df: pd.DataFrame) -> str:
    cols    = list(df.columns)
    numeric = df.select_dtypes(include="number").columns.tolist()
    categ   = [c for c in cols if c not in numeric]
    n_num   = len(numeric)
    n_cat   = len(categ)
    n_rows  = len(df)

    if n_num == 0 or n_rows < 2:
        return "none"

    has_time  = any(_col_has(c, TIME_KEYWORDS)  for c in categ)
    has_ratio = any(_col_has(c, RATIO_KEYWORDS) for c in numeric)
    has_rank  = any(_col_has(c, RANK_KEYWORDS)  for c in cols)
    has_geo   = any(_col_has(c, GEO_KEYWORDS)   for c in categ)

    # Time series → line
    if has_time and n_num >= 1:
        if n_num >= 2:
            return "multi_line"
        return "line"

    # Two numeric, no category → scatter
    if n_cat == 0 and n_num == 2:
        return "scatter"

    # Many numeric → scatter matrix
    if n_cat == 0 and n_num >= 3:
        return "scatter_matrix"

    # Ratio/percentage with few rows → donut
    if n_cat == 1 and n_num == 1 and has_ratio and n_rows <= 10:
        return "donut"

    # Small categorical set (≤6) → horizontal bar (easier to read labels)
    if n_cat == 1 and n_num == 1 and n_rows <= 6:
        return "bar_h"

    # Medium categorical → vertical bar with gradient
    if n_cat == 1 and n_num == 1 and n_rows <= 20:
        return "bar"

    # Multiple metrics → grouped bar
    if n_cat == 1 and n_num >= 2 and n_rows <= 15:
        return "bar_grouped"

    # Multiple metrics, many rows → line
    if n_cat == 1 and n_num >= 2:
        return "multi_line"

    # Large categorical → horizontal bar
    if n_cat >= 1 and n_num == 1 and n_rows > 20:
        return "bar_h"

    if n_num >= 1:
        return "bar"

    return "none"


# ── Public API ──────────────────────────────────────────────────────────────────

def build_chart(df: pd.DataFrame, title: str = "") -> Optional[go.Figure]:
    if df is None or df.empty or len(df.columns) < 2:
        return None

    chart_type = _detect(df)
    if chart_type == "none":
        return None

    numeric = df.select_dtypes(include="number").columns.tolist()
    categ   = [c for c in df.columns if c not in numeric]
    short   = (title[:60] + "…") if len(title) > 60 else title

    try:
        fig = _build(df, chart_type, numeric, categ, short)
        if fig is None:
            return None
        fig.update_layout(**_base_layout(short))
        _polish(fig, chart_type)
        return fig
    except Exception:
        return None


def chart_type_label(df: pd.DataFrame) -> str:
    mapping = {
        "line":           "📈 Line",
        "multi_line":     "📈 Multi-line",
        "bar":            "📊 Bar",
        "bar_h":          "📊 Horizontal bar",
        "bar_grouped":    "📊 Grouped bar",
        "donut":          "🍩 Donut",
        "scatter":        "🔵 Scatter",
        "scatter_matrix": "🔵 Scatter matrix",
        "none":           "",
    }
    return mapping.get(_detect(df), "")


# ── Builders ────────────────────────────────────────────────────────────────────

def _build(df, chart_type, numeric, categ, title) -> Optional[go.Figure]:
    x_cat = categ[0] if categ else df.columns[0]
    y_num = numeric[0] if numeric else None

    # ── Line ──────────────────────────────────────────────────────────────────
    if chart_type == "line":
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x    = df[x_cat],
            y    = df[y_num],
            mode = "lines+markers",
            name = y_num,
            line = dict(color=PRIMARY, width=2.5, shape="spline", smoothing=0.8),
            marker= dict(size=6, color=PRIMARY, line=dict(width=1.5, color=BG_PLOT)),
            fill = "tozeroy",
            fillcolor = _rgba(PRIMARY, 0.08),
            hovertemplate = f"<b>%{{x}}</b><br>{y_num}: %{{y:,.2f}}<extra></extra>",
        ))
        return fig

    # ── Multi-line ────────────────────────────────────────────────────────────
    if chart_type == "multi_line":
        fig = go.Figure()
        for idx, col in enumerate(numeric):
            color = COLORS[idx % len(COLORS)]
            fig.add_trace(go.Scatter(
                x    = df[x_cat],
                y    = df[col],
                mode = "lines+markers",
                name = col,
                line = dict(color=color, width=2.2, shape="spline", smoothing=0.7),
                marker= dict(size=5, color=color, line=dict(width=1, color=BG_PLOT)),
                hovertemplate = f"<b>%{{x}}</b><br>{col}: %{{y:,.2f}}<extra></extra>",
            ))
        return fig

    # ── Vertical bar with gradient ────────────────────────────────────────────
    if chart_type == "bar":
        grad = _gradient_colors(len(df))
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x             = df[x_cat],
            y             = df[y_num],
            name          = y_num,
            marker        = dict(
                color     = grad,
                line      = dict(width=0),
                cornerradius = 4,
            ),
            hovertemplate = f"<b>%{{x}}</b><br>{y_num}: %{{y:,.2f}}<extra></extra>",
        ))
        return fig

    # ── Horizontal bar ────────────────────────────────────────────────────────
    if chart_type == "bar_h":
        df_s = df.sort_values(y_num, ascending=True)
        grad = _gradient_colors(len(df_s))
        fig = go.Figure()
        fig.add_trace(go.Bar(
            y             = df_s[x_cat],
            x             = df_s[y_num],
            orientation   = "h",
            name          = y_num,
            marker        = dict(
                color     = grad,
                line      = dict(width=0),
                cornerradius = 4,
            ),
            hovertemplate = f"<b>%{{y}}</b><br>{y_num}: %{{x:,.2f}}<extra></extra>",
        ))
        fig.update_layout(
            xaxis = dict(tickformat=",.0f"),
            yaxis = dict(tickfont=dict(size=11)),
            height = max(320, len(df_s) * 38 + 80),
        )
        return fig

    # ── Grouped bar ───────────────────────────────────────────────────────────
    if chart_type == "bar_grouped":
        fig = go.Figure()
        for idx, col in enumerate(numeric):
            color = COLORS[idx % len(COLORS)]
            fig.add_trace(go.Bar(
                x             = df[x_cat],
                y             = df[col],
                name          = col,
                marker        = dict(
                    color     = color,
                    line      = dict(width=0),
                    cornerradius = 3,
                ),
                hovertemplate = f"<b>%{{x}}</b><br>{col}: %{{y:,.2f}}<extra></extra>",
            ))
        fig.update_layout(barmode="group")
        return fig

    # ── Donut ─────────────────────────────────────────────────────────────────
    if chart_type == "donut":
        fig = go.Figure()
        fig.add_trace(go.Pie(
            labels            = df[x_cat],
            values            = df[y_num],
            hole              = 0.52,
            marker            = dict(
                colors        = COLORS[:len(df)],
                line          = dict(color=BG_PLOT, width=2),
            ),
            textfont          = dict(color=TEXT_COLOR, size=11),
            hovertemplate     = "<b>%{label}</b><br>%{value:,.2f} (%{percent})<extra></extra>",
            textposition      = "outside",
            textinfo          = "label+percent",
            insidetextorientation = "radial",
        ))
        # Center annotation
        total = df[y_num].sum()
        fig.add_annotation(
            text      = f"<b>{_fmt_number(total)}</b><br><span style='font-size:10px'>{y_num}</span>",
            x=0.5, y=0.5, showarrow=False,
            font      = dict(color=TEXT_COLOR, size=13),
            xanchor   = "center", yanchor = "middle",
        )
        fig.update_layout(showlegend=True, height=400)
        return fig

    # ── Scatter ───────────────────────────────────────────────────────────────
    if chart_type == "scatter":
        x_col, y_col = numeric[0], numeric[1]
        label_col = categ[0] if categ else None
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x             = df[x_col],
            y             = df[y_col],
            mode          = "markers",
            text          = df[label_col] if label_col else None,
            marker        = dict(
                size      = 9,
                color     = PRIMARY,
                opacity   = 0.8,
                line      = dict(width=1, color=_rgba(PRIMARY, 0.3)),
            ),
            hovertemplate = (
                (f"<b>%{{text}}</b><br>" if label_col else "") +
                f"{x_col}: %{{x:,.2f}}<br>{y_col}: %{{y:,.2f}}<extra></extra>"
            ),
        ))
        # Trendline
        if len(df) >= 5:
            try:
                z   = np.polyfit(df[x_col].dropna(), df[y_col].dropna(), 1)
                p   = np.poly1d(z)
                xs  = np.linspace(df[x_col].min(), df[x_col].max(), 100)
                fig.add_trace(go.Scatter(
                    x    = xs, y = p(xs),
                    mode = "lines",
                    name = "trend",
                    line = dict(color=SECONDARY, width=1.5, dash="dot"),
                    hoverinfo = "skip",
                ))
            except Exception:
                pass
        fig.update_layout(
            xaxis_title = x_col,
            yaxis_title = y_col,
        )
        return fig

    # ── Scatter matrix ────────────────────────────────────────────────────────
    if chart_type == "scatter_matrix":
        dims = numeric[:4]
        fig  = px.scatter_matrix(
            df, dimensions=dims,
            color_discrete_sequence=[PRIMARY],
        )
        fig.update_traces(
            marker=dict(size=4, opacity=0.65,
                        line=dict(width=0.5, color=_rgba(PRIMARY, 0.2))),
            diagonal_visible=False,
        )
        fig.update_layout(height=480)
        return fig

    return None


# ── Polish pass ──────────────────────────────────────────────────────────────────

def _polish(fig: go.Figure, chart_type: str):
    """Apply finishing touches: axis formatting, hover mode, spikes."""
    fig.update_layout(
        hovermode    = "x unified" if chart_type in ("line", "multi_line") else "closest",
        dragmode     = "pan",
    )
    # Spike lines for line charts
    if chart_type in ("line", "multi_line"):
        fig.update_xaxes(
            showspikes     = True,
            spikecolor     = _rgba(PRIMARY, 0.4),
            spikethickness = 1,
            spikedash      = "dot",
            spikemode      = "across",
        )
    # Smart Y-axis tick formatting
    for trace in fig.data:
        if hasattr(trace, "y") and trace.y is not None and len(trace.y) > 0:
            try:
                max_val = max(abs(v) for v in trace.y if v is not None)
                if max_val >= 1_000_000:
                    fig.update_yaxes(tickformat="$.3s")
                elif max_val >= 1_000:
                    fig.update_yaxes(tickformat=",.0f")
            except Exception:
                pass
            break


# ── Number formatter ─────────────────────────────────────────────────────────────

def _fmt_number(n: float) -> str:
    if abs(n) >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n/1_000:.1f}K"
    return f"{n:,.0f}"









# """
# charts.py — Auto chart-type detection and Plotly figure builder.
# Detects the best chart type from DataFrame shape and column semantics,
# then applies a consistent dark theme.
# """

# from __future__ import annotations
# import pandas as pd
# import plotly.express as px
# import plotly.graph_objects as go
# from typing import Optional

# # ── Theme ───────────────────────────────────────────────────────────────────────

# COLORS = ["#7c6af7", "#4af0c8", "#f59e0b", "#ef4444", "#3b82f6",
#           "#10b981", "#f97316", "#ec4899", "#06b6d4", "#84cc16"]

# DARK_LAYOUT = dict(
#     paper_bgcolor="rgba(0,0,0,0)",
#     plot_bgcolor="rgba(20,23,32,0.9)",
#     font=dict(color="#e2e8f0", family="Space Grotesk, sans-serif", size=12),
#     title_font=dict(size=13, color="#94a3b8"),
#     xaxis=dict(
#         gridcolor="#252a3a", linecolor="#252a3a",
#         tickfont=dict(color="#94a3b8", size=11),
#         title_font=dict(color="#64748b"),
#     ),
#     yaxis=dict(
#         gridcolor="#252a3a", linecolor="#252a3a",
#         tickfont=dict(color="#94a3b8", size=11),
#         title_font=dict(color="#64748b"),
#     ),
#     legend=dict(
#         bgcolor="rgba(20,23,32,0.8)", bordercolor="#252a3a",
#         borderwidth=1, font=dict(color="#94a3b8"),
#     ),
#     margin=dict(l=10, r=10, t=45, b=30),
#     hoverlabel=dict(bgcolor="#1c2030", bordercolor="#252a3a", font_color="#e2e8f0"),
# )

# TIME_KEYWORDS = {"date", "month", "year", "week", "day", "time",
#                  "period", "quarter", "hour", "created", "updated"}


# # ── Chart type detection ────────────────────────────────────────────────────────

# def _detect(df: pd.DataFrame) -> str:
#     cols     = list(df.columns)
#     numeric  = df.select_dtypes(include="number").columns.tolist()
#     categ    = [c for c in cols if c not in numeric]
#     n_num    = len(numeric)
#     n_cat    = len(categ)
#     n_rows   = len(df)

#     has_time = any(
#         any(kw in c.lower() for kw in TIME_KEYWORDS)
#         for c in categ
#     )
#     # Percentage / ratio columns → pie makes sense for small sets
#     pct_like = any("pct" in c.lower() or "%" in c or "share" in c.lower()
#                    for c in numeric)

#     if n_num == 0:
#         return "none"
#     if has_time and n_num >= 1:
#         return "line"
#     if n_cat == 1 and n_num == 1 and n_rows <= 8 and not pct_like:
#         return "bar"
#     if n_cat == 1 and n_num == 1 and n_rows <= 8 and pct_like:
#         return "pie"
#     if n_cat == 1 and n_num >= 2:
#         return "bar_grouped"
#     if n_cat >= 1 and n_num == 1:
#         return "bar"
#     if n_cat == 0 and n_num == 2:
#         return "scatter"
#     if n_cat == 0 and n_num >= 3:
#         return "scatter_matrix"
#     if n_num >= 1:
#         return "bar"
#     return "none"


# # ── Public API ──────────────────────────────────────────────────────────────────

# def build_chart(df: pd.DataFrame, title: str = "") -> Optional[go.Figure]:
#     """
#     Build a Plotly figure automatically from df.
#     Returns None if the data isn't suitable for a chart.
#     """
#     if df is None or df.empty or len(df.columns) < 2:
#         return None

#     chart_type = _detect(df)
#     if chart_type == "none":
#         return None

#     numeric = df.select_dtypes(include="number").columns.tolist()
#     categ   = [c for c in df.columns if c not in numeric]
#     short   = (title[:55] + "…") if len(title) > 55 else title

#     try:
#         fig = _build(df, chart_type, numeric, categ, short)
#         if fig is None:
#             return None
#         fig.update_layout(**DARK_LAYOUT)
#         return fig
#     except Exception:
#         return None


# def chart_type_label(df: pd.DataFrame) -> str:
#     """Return human-readable chart type name for display."""
#     mapping = {
#         "line":           "📈 Line chart",
#         "bar":            "📊 Bar chart",
#         "bar_grouped":    "📊 Grouped bar",
#         "pie":            "🥧 Donut chart",
#         "scatter":        "🔵 Scatter plot",
#         "scatter_matrix": "🔵 Scatter matrix",
#         "none":           "",
#     }
#     return mapping.get(_detect(df), "")


# # ── Private builders ────────────────────────────────────────────────────────────

# def _build(df, chart_type, numeric, categ, title) -> Optional[go.Figure]:
#     x_cat = categ[0] if categ else df.columns[0]

#     if chart_type == "line":
#         return px.line(
#             df, x=x_cat, y=numeric, title=title,
#             color_discrete_sequence=COLORS, markers=True,
#         )

#     if chart_type == "bar":
#         fig = px.bar(
#             df, x=x_cat, y=numeric[0], title=title,
#             color_discrete_sequence=COLORS,
#         )
#         fig.update_traces(marker_line_width=0)
#         return fig

#     if chart_type == "bar_grouped":
#         return px.bar(
#             df, x=x_cat, y=numeric, title=title,
#             barmode="group", color_discrete_sequence=COLORS,
#         )

#     if chart_type == "pie":
#         return px.pie(
#             df, names=x_cat, values=numeric[0], title=title,
#             color_discrete_sequence=COLORS, hole=0.38,
#         )

#     if chart_type == "scatter":
#         return px.scatter(
#             df, x=numeric[0], y=numeric[1], title=title,
#             color_discrete_sequence=COLORS,
#             trendline="ols" if len(df) >= 5 else None,
#             trendline_color_override="#4af0c8",
#         )

#     if chart_type == "scatter_matrix":
#         return px.scatter_matrix(
#             df, dimensions=numeric[:4], title=title,
#             color_discrete_sequence=COLORS,
#         )

#     return None