"""Automatic chart selection and styled, labelled, interactive Plotly figures for query results.

Colour rules
  * Categorical (identity): a fixed 8-slot palette, validated for colour-blind separation. Each category / series
    gets its own hue in a fixed order. More than 8 categories never get generated hues.
  * Sequential (magnitude): one hue, light -> dark (used when a bar chart has more than 8 bars).
  * Text (labels, values, axes) stays in ink colours; the coloured mark beside it carries identity.
"""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

DATE_HINTS = ("date", "month", "year", "day", "week", "time", "period", "quarter")

# Categorical palette (fixed order; passes adjacent-pair CVD and normal-vision checks on a light surface).
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
# Sequential blue ramp (ordinal-safe: lightest step still visible on white).
SEQUENTIAL = ["#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281"]
INK, MUTED, GRID, SURFACE = "#1B2335", "#5D6A82", "#E6EBF3", "#FFFFFF"
DISPLAY_FONT = "Bricolage Grotesque, Instrument Sans, system-ui, sans-serif"
BODY_FONT = "Instrument Sans, system-ui, sans-serif"

CHART_LABELS = {"bar": "Bar", "line": "Line", "pie": "Donut", "scatter": "Scatter", "histogram": "Distribution"}


# ------------------------------------------------------------------ helpers
def _is_text(s: pd.Series) -> bool:
    return s.dtype == object or pd.api.types.is_string_dtype(s)


def _is_datelike(s: pd.Series) -> bool:
    if pd.api.types.is_datetime64_any_dtype(s):
        return True
    if _is_text(s) and any(h in str(s.name).lower() for h in DATE_HINTS):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            parsed = pd.to_datetime(s, errors="coerce")
        return parsed.notna().mean() > 0.8
    if pd.api.types.is_integer_dtype(s) and "year" in str(s.name).lower():
        return True
    return False


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in df.columns:
        if any(h in str(c).lower() for h in DATE_HINTS):
            continue  # keep '2009', '2010-03' as labels, not numbers
        if _is_text(df[c]):  # numeric values that came back as strings / Decimals
            converted = pd.to_numeric(df[c], errors="coerce")
            if converted.notna().all() and len(df):
                df[c] = converted
    return df


def _roles(df: pd.DataFrame) -> tuple[list, list, list]:
    """(measures, dates, categories) for a prepared dataframe."""
    cols = list(df.columns)
    numeric = [c for c in cols if pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c])]
    dates = [c for c in cols if _is_datelike(df[c])]
    categories = [c for c in cols if c not in numeric and c not in dates]
    measures = [c for c in numeric if c not in dates and not str(c).lower().endswith("id")]
    return measures, dates, categories


def pretty(name) -> str:
    """'total_revenue' -> 'Total revenue', 'InvoiceDate' -> 'Invoice date'."""
    import re

    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(name)).replace("_", " ").strip()
    return text[:1].upper() + text[1:].lower() if text else str(name)


def _num_format(values: pd.Series) -> str:
    """A d3 format that keeps labels short: 1.2M / 48.3k for big numbers, 2 decimals for small ones."""
    v = pd.to_numeric(values, errors="coerce").dropna()
    if v.empty:
        return ","
    biggest = v.abs().max()
    if biggest >= 100_000:
        return ".3~s"
    if (v % 1 == 0).all():
        return ",d"
    return ",.2f" if biggest < 1000 else ",.0f"


def _human(v: float, fmt: str) -> str:
    """Python-side twin of the d3 label format (for annotations)."""
    if fmt == ".3~s":
        for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
            if abs(v) >= div:
                return f"{v / div:.3g}{suf}"
    if fmt in (",d", ",.0f"):
        return f"{v:,.0f}"
    return f"{v:,.2f}"


def _fold_other(df: pd.DataFrame, x: str, y: str, keep: int) -> pd.DataFrame:
    """Keep the largest `keep - 1` slices and fold the rest into 'Other' (no generated hues past the palette)."""
    if len(df) <= keep:
        return df
    top = df.nlargest(keep - 1, y)
    other = pd.DataFrame({x: ["Other"], y: [df.drop(top.index)[y].sum()]})
    return pd.concat([top[[x, y]], other], ignore_index=True)


# --------------------------------------------------------------- selection
def suggest_chart(df: pd.DataFrame, hint: dict | None = None) -> dict | None:
    """Return a chart spec {type, x, y, color} or None when a chart wouldn't help."""
    if df is None or df.empty or len(df.columns) < 1 or len(df) < 2:
        return None
    df = _prepare(df)
    cols = list(df.columns)
    measures, dates, categories = _roles(df)

    if hint and hint.get("type") and hint["type"] != "none":
        x, y = hint.get("x"), hint.get("y")
        if x in cols and (y in cols or hint["type"] == "histogram"):
            color = hint.get("color") if hint.get("color") in cols else None
            return {"type": hint["type"], "x": x, "y": y, "color": color}
    if hint and hint.get("type") == "none":
        return None

    if dates and measures:
        color = categories[0] if categories and df[categories[0]].nunique() <= 8 else None
        return {"type": "line", "x": dates[0], "y": measures[0], "color": color}
    if categories and measures:
        return {"type": "bar", "x": categories[0], "y": measures[0], "color": None}
    if len(measures) >= 2:
        return {"type": "scatter", "x": measures[0], "y": measures[1], "color": categories[0] if categories else None}
    if len(measures) == 1 and len(df) >= 5:
        return {"type": "histogram", "x": measures[0], "y": None, "color": None}
    return None


def chart_options(df: pd.DataFrame, spec: dict | None) -> dict[str, dict]:
    """Every chart type that makes sense for this result, as {type: spec}. Powers the chart-type switcher."""
    if df is None or df.empty or len(df) < 2:
        return {}
    df = _prepare(df)
    measures, dates, categories = _roles(df)
    base = spec or {}
    opts: dict[str, dict] = {}
    y = base.get("y") if base.get("y") in measures else (measures[0] if measures else None)
    label_col = base.get("x") if base.get("x") in categories + dates else ((dates or categories or [None])[0])
    series = base.get("color") if base.get("color") in categories else None

    if y and label_col:
        opts["bar"] = {"type": "bar", "x": label_col, "y": y, "color": series}
        if len(df) >= 3 and label_col in dates:
            opts["line"] = {"type": "line", "x": label_col, "y": y, "color": series}
        if not series and (df[y] >= 0).all() and df[label_col].nunique() <= 12:
            opts["pie"] = {"type": "pie", "x": label_col, "y": y, "color": None}
    if len(measures) >= 2:
        sx, sy = (base["x"], base["y"]) if base.get("type") == "scatter" and base.get("x") in measures else (measures[0], measures[1])
        opts["scatter"] = {"type": "scatter", "x": sx, "y": sy, "color": categories[0] if categories else None}
    if measures and len(df) >= 10:
        opts["histogram"] = {"type": "histogram", "x": y or measures[0], "y": None, "color": None}
    if base.get("type") in opts:  # keep the original choice first
        opts = {base["type"]: opts[base["type"]], **{k: v for k, v in opts.items() if k != base["type"]}}
    return opts


# ----------------------------------------------------------------- figures
def _bar(df: pd.DataFrame, x: str, y: str, color: str | None) -> go.Figure:
    data = df.head(30).copy()
    data[x] = data[x].astype(str)
    fmt = _num_format(data[y])
    horizontal = data[x].nunique() > 8 or data[x].str.len().mean() > 14
    order = list(dict.fromkeys(data[x]))
    xy = dict(x=y, y=x, orientation="h") if horizontal else dict(x=x, y=y)
    labels = {x: pretty(x), y: pretty(y)}

    if color and color in data and color != x:  # grouped bars: one hue per series
        data[color] = data[color].astype(str)
        fig = px.bar(data, **xy, color=color, barmode="group", labels={**labels, color: pretty(color)},
                     color_discrete_sequence=PALETTE, category_orders={x: order})
    elif data[x].nunique() <= len(PALETTE):  # few categories: each bar gets its own hue
        fig = px.bar(data, **xy, color=x, labels=labels, color_discrete_sequence=PALETTE, category_orders={x: order})
        fig.update_layout(showlegend=False)
    else:  # many categories: one hue, shade by value (magnitude, not identity)
        fig = px.bar(data, **xy, color=y, labels=labels, color_continuous_scale=SEQUENTIAL, category_orders={x: order})
        fig.update_layout(coloraxis_showscale=False)

    value_axis = "x" if horizontal else "y"
    show_labels = len(data) <= 30
    fig.update_traces(
        texttemplate=f"%{{{value_axis}:{fmt}}}" if show_labels else None,
        textposition="outside", cliponaxis=False, textfont=dict(color=INK, size=12),
        marker_line_width=0,
        hovertemplate=f"<b>%{{{'y' if horizontal else 'x'}}}</b><br>{pretty(y)}: %{{{value_axis}:,.2f}}<extra></extra>",
    )
    try:
        fig.update_traces(marker_cornerradius=4)
    except Exception:  # older plotly
        pass
    if horizontal:
        fig.update_yaxes(title=None)  # px already lists the first category at the top
        fig.update_xaxes(showgrid=True)
        fig.update_layout(height=max(360, 34 * len(data) + 110), bargap=0.25)
    else:
        fig.update_layout(bargap=0.3)
    return fig


def _line(df: pd.DataFrame, x: str, y: str, color: str | None) -> go.Figure:
    df = df.copy()
    yearly = "year" in str(x).lower() and df[x].astype(str).str.fullmatch(r"\d{4}").all()
    if yearly:
        df[x] = df[x].astype(str)  # 2009, 2010 ... as labels, not dates or decimals
    elif _is_text(df[x]):
        parsed = pd.to_datetime(df[x], errors="coerce")
        if parsed.notna().all():
            df[x] = parsed
    if color and color in df:
        df[color] = df[color].astype(str)
    else:
        color = None
    df = df.sort_values(x)
    fmt = _num_format(df[y])
    fig = px.line(df, x=x, y=y, color=color, markers=True, color_discrete_sequence=PALETTE,
                  labels={x: pretty(x), y: pretty(y), **({color: pretty(color)} if color else {})})
    many = (df.groupby(color).size().max() if color else len(df)) > 60
    fig.update_traces(line=dict(width=2.5), marker=dict(size=5 if many else 8, line=dict(width=1 if many else 2, color=SURFACE)),
                      hovertemplate=f"{pretty(y)}: %{{y:,.2f}}<extra>%{{fullData.name}}</extra>" if color
                      else f"{pretty(y)}: %{{y:,.2f}}<extra></extra>")

    points = df.groupby(color).size().max() if color else len(df)
    if points <= 15 and not (color and df[color].nunique() > 3):
        # few points: label every point
        fig.update_traces(mode="lines+markers+text", texttemplate=f"%{{y:{fmt}}}", textposition="top center",
                          textfont=dict(color=INK, size=11), cliponaxis=False)
    elif not color:
        # many points, one series: call out the peak and the latest value only (multi-series rely on legend + hover)
        for _, part in [(None, df)]:
            peak, last = part.loc[part[y].idxmax()], part.iloc[-1]
            for row, label in ((peak, "Peak"), (last, "Latest")):
                fig.add_annotation(x=row[x], y=row[y], text=f"{label}: {_human(row[y], fmt)}",
                                   showarrow=True, arrowhead=0, arrowcolor=MUTED, ax=0, ay=-28,
                                   font=dict(color=INK, size=11), bgcolor="rgba(255,255,255,.85)")
                if peak.equals(last):
                    break

    if not color:  # single series: soft area under the line
        lo, hi = df[y].min(), df[y].max()
        if lo >= 0 and lo <= 0.35 * hi:
            fig.update_traces(fill="tozeroy", fillcolor="rgba(42,120,214,0.10)")
    if yearly:
        fig.update_xaxes(type="category")
    elif pd.api.types.is_datetime64_any_dtype(df[x]) and len(df) > 24:
        fig.update_xaxes(rangeslider=dict(visible=True, thickness=0.07, bgcolor="#F4F6FA"),
                         rangeselector=dict(buttons=[dict(count=3, label="3M", step="month", stepmode="backward"),
                                                     dict(count=1, label="1Y", step="year", stepmode="backward"),
                                                     dict(step="all", label="All")],
                                            bgcolor="#EEF2FA", activecolor="#C9D7F8", font=dict(size=11),
                                            x=1, xanchor="right", y=1.02, yanchor="bottom"))
    fig.update_layout(hovermode="x unified")
    return fig


def _pie(df: pd.DataFrame, x: str, y: str) -> go.Figure:
    data = df[[x, y]].copy()
    data[x] = data[x].astype(str)
    data = data.groupby(x, as_index=False)[y].sum().sort_values(y, ascending=False)
    total = data[y].sum()
    tiny = data[y] < 0.02 * total  # slivers fold into Other too
    if tiny.sum() > 1:
        data = pd.concat([data[~tiny], pd.DataFrame({x: ["Other"], y: [data.loc[tiny, y].sum()]})], ignore_index=True)
    data = _fold_other(data, x, y, keep=len(PALETTE))
    data = pd.concat([data[data[x] != "Other"].sort_values(y, ascending=False), data[data[x] == "Other"]], ignore_index=True)
    fmt = _num_format(data[y])
    colors = [("#B8C0CE" if name == "Other" else PALETTE[i]) for i, name in enumerate(data[x])]  # 'Other' stays neutral
    share = data[y] / total
    many = len(data) > 5
    if many:  # names go in the legend; slices show only their percentage (when big enough to read)
        text = [f"{p:.0%}" if p >= 0.04 else "" for p in share]
    else:
        text = [f"{n}<br>{p:.1%}" if p >= 0.03 else "" for n, p in zip(data[x], share)]
    fig = go.Figure(go.Pie(labels=data[x], values=data[y], hole=0.55, sort=False, direction="clockwise", rotation=90,
                           marker=dict(colors=colors, line=dict(color=SURFACE, width=2)),
                           text=text, textinfo="text", textposition="inside" if many else "outside",
                           insidetextorientation="horizontal",
                           textfont=dict(color="#FFFFFF" if many else INK, size=12),
                           hovertemplate=f"<b>%{{label}}</b><br>{pretty(y)}: %{{value:,.2f}}<br>%{{percent}} of total<extra></extra>"))
    label = pretty(y).lower()
    label = label if len(label) <= 14 else label[:13] + "…"
    fig.add_annotation(text=f"<b>{_human(total, fmt)}</b><br><span style='color:{MUTED};font-size:11px'>Total {label}</span>",
                       showarrow=False, font=dict(size=18, color=INK, family=DISPLAY_FONT), x=0.5, y=0.5)
    fig.update_layout(showlegend=len(data) > 5, legend=dict(orientation="v", x=1.02, y=0.5, yanchor="middle"))
    return fig


def _scatter(df: pd.DataFrame, x: str, y: str, color: str | None) -> go.Figure:
    df = df.copy()
    _, _, categories = _roles(df)
    if color and color in df and df[color].nunique() <= 3:  # all-pairs separation holds for 3 hues
        df[color] = df[color].astype(str)
    else:
        color = None
    label_col = categories[0] if categories else None
    text = None
    if label_col:  # selective direct labels: name only the 5 highest points
        span_x = (df[x].max() - df[x].min()) or 1
        span_y = (df[y].max() - df[y].min()) or 1
        top, placed = set(), []
        for i, row in df.nlargest(8, y).iterrows():  # skip labels that would sit on top of one another
            if all(abs(row[x] - px_) / span_x > 0.08 or abs(row[y] - py_) / span_y > 0.06 for px_, py_ in placed):
                top.add(i)
                placed.append((row[x], row[y]))
            if len(top) == 5:
                break
        df["_label"] = [str(v) if i in top else "" for i, v in zip(df.index, df[label_col])]
        text = "_label"
    hover = {c: True for c in df.columns[:8] if c != "_label"}
    hover["_label"] = False
    fig = px.scatter(df, x=x, y=y, color=color, text=text, color_discrete_sequence=PALETTE,
                     hover_data=hover,
                     labels={c: pretty(c) for c in df.columns})
    fig.update_traces(marker=dict(size=11, line=dict(width=1.5, color=SURFACE), opacity=0.9),
                      textposition="top center", textfont=dict(color=MUTED, size=11), cliponaxis=False)
    return fig


def _histogram(df: pd.DataFrame, x: str) -> go.Figure:
    fig = px.histogram(df, x=x, color_discrete_sequence=[PALETTE[0]], labels={x: pretty(x)})
    fig.update_traces(texttemplate="%{y}", textposition="outside", textfont=dict(color=INK, size=11),
                      marker_line_color=SURFACE, marker_line_width=2, cliponaxis=False,
                      hovertemplate=f"{pretty(x)}: %{{x}}<br>Count: %{{y}}<extra></extra>")
    fig.update_layout(bargap=0.04, yaxis_title="Count")
    return fig


def _has_legend(fig: go.Figure) -> bool:
    if fig.layout.showlegend is False:
        return False
    if fig.layout.legend.orientation == "v":
        return False
    return sum(1 for t in fig.data if t.showlegend is not False and t.name) > 1


def _style(fig: go.Figure, title: str) -> go.Figure:
    fig.update_layout(
        template="plotly_white",
        title=dict(text=title or None, x=0, xanchor="left", font=dict(family=DISPLAY_FONT, size=17, color=INK)),
        font=dict(family=BODY_FONT, color=INK, size=12),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=8, r=28, t=(92 if title else 56) if _has_legend(fig) else (56 if title else 24), b=8),
        hoverlabel=dict(bgcolor=SURFACE, bordercolor=GRID, font=dict(family=BODY_FONT, color=INK, size=13)),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title_text="", font=dict(color=INK))
        if fig.layout.legend.orientation != "v" else dict(font=dict(color=INK)),
        uniformtext=dict(minsize=9, mode="hide"),
        transition=dict(duration=350, easing="cubic-in-out"),
    )
    if fig.layout.height is None:
        fig.update_layout(height=420)
    fig.update_xaxes(showgrid=False, linecolor=GRID, ticks="", tickfont=dict(color=MUTED), title_font=dict(color=MUTED))
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor=GRID, tickfont=dict(color=MUTED), title_font=dict(color=MUTED))
    return fig


def build_figure(df: pd.DataFrame, spec: dict | None, title: str = ""):
    if not spec:
        return None
    df = _prepare(df)
    kind, x, y, color = spec["type"], spec.get("x"), spec.get("y"), spec.get("color")
    try:
        if kind == "line":
            fig = _line(df, x, y, color)
        elif kind == "bar":
            fig = _bar(df, x, y, color)
        elif kind == "pie":
            fig = _pie(df, x, y)
        elif kind == "scatter":
            fig = _scatter(df, x, y, color)
        elif kind == "histogram":
            fig = _histogram(df, x)
        else:
            return None
    except Exception:
        return None
    return _style(fig, title)


PLOTLY_CONFIG = {
    "displaylogo": False,
    "modeBarButtonsToRemove": ["lasso2d", "select2d", "autoScale2d"],
    "toImageButtonOptions": {"format": "png", "scale": 2, "filename": "querymind_chart"},
}
