"""The map renderer: classification, colour, legend, scale bar, north arrow, graticule, labels."""
from __future__ import annotations

import io
import math
import textwrap
import warnings
from dataclasses import dataclass, field, replace

import matplotlib
import numpy as np
import pandas as pd
import shapely
from matplotlib import font_manager
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.colors import to_rgba
from matplotlib.figure import Figure
from matplotlib.patches import Polygon as MplPolygon
from matplotlib.patches import Rectangle
from pyproj import CRS, Transformer
from shapely.geometry import box as sbox
from shapely.ops import unary_union

from .clean import LINE, POINT, POLY, meters_per_unit, prepare
from .io import DataError
from .labels import LabelPlacer, LabelStyle, clean_text, guess_label_column, luminance

THEMES = {
    "light": dict(bg="#ffffff", land="#e8ebef", edge="#7b8494", edge_w=0.45, frame="#222222", text="#1b1b1b",
                  sub="#5a6270", grid="#9aa3b0", overlay="#2b2f36", nodata="#d9d9d9"),
    "paper": dict(bg="#fbfaf7", land="#ece9e1", edge="#8a857a", edge_w=0.45, frame="#2a2a2a", text="#1f1d1a",
                  sub="#6b665c", grid="#a39e92", overlay="#2a2a2a", nodata="#dcd8cd"),
    "dark": dict(bg="#13161b", land="#2a303b", edge="#8791a1", edge_w=0.45, frame="#c8ced8", text="#f2f4f7",
                 sub="#a9b1bd", grid="#5b6574", overlay="#e6eaf0", nodata="#3a414d"),
}
OKABE_ITO = ["#E69F00", "#56B4E9", "#009E73", "#F0E442", "#0072B2", "#D55E00", "#CC79A7", "#999999"]
DIVERGING = {"RdBu", "RdYlBu", "RdYlGn", "PuOr", "PRGn", "BrBG", "PiYG", "Spectral", "coolwarm", "bwr", "seismic", "RdGy"}
CORNERS = ["upper left", "upper right", "lower left", "lower right"]


@dataclass
class MapOptions:
    # text
    title: str | None = None
    subtitle: str | None = None
    source: str | None = None
    credit: bool = False
    # labels
    labels: bool = True
    label_column: str | None = "auto"
    label_case: str = "as-is"            # as-is | title | upper
    label_font_max: float = 11.0
    label_font_min: float = 5.5
    label_overflow: bool | None = None   # None = automatic (on for sparse maps); small units get a leader line
    label_max: int | None = None
    # colour
    color_by: str | None = None
    classification: str = "auto"         # auto | quantiles | equal | jenks | manual | categorical
    k: int = 5
    bins: list | None = None
    cmap: str | None = None
    reverse_cmap: bool = False
    legend: bool = True
    legend_title: str | None = None
    legend_loc: str = "auto"
    decimals: int | None = None
    nodata_label: str = "No data"
    # map furniture
    north_arrow: bool = True
    scale_bar: bool = True
    graticule: bool = True
    frame: bool = True
    crs_note: bool = True
    # overlay (e.g. state borders on top of LGAs)
    overlay_width: float = 1.1
    overlay_label_column: str | None = None
    # look
    theme: str = "light"
    font: str | None = None
    width_in: float = 10.0
    point_size: float = 5.0
    line_width: float = 1.0
    # data
    crs: str | int = "auto"
    assume_crs: str | int | None = None


@dataclass
class MapResult:
    fig: Figure
    report: dict = field(default_factory=dict)

    def to_bytes(self, fmt: str = "png", dpi: int = 300) -> bytes:
        fmt = fmt.lower().lstrip(".")
        buf = io.BytesIO()
        with matplotlib.rc_context({"pdf.fonttype": 42, "svg.fonttype": "path", "ps.fonttype": 42}):
            self.fig.savefig(buf, format=fmt, dpi=dpi, facecolor=self.fig.get_facecolor())
        return buf.getvalue()

    def save(self, path, dpi: int = 300) -> str:
        path = str(path)
        fmt = path.rsplit(".", 1)[-1] if "." in path else "png"
        if fmt.lower() not in {"png", "svg", "pdf", "jpg", "jpeg", "tif", "tiff"}:
            raise DataError(f"Unsupported output format '{fmt}'. Use png, svg, pdf, jpg or tif.")
        with open(path, "wb") as fh:
            fh.write(self.to_bytes(fmt, dpi))
        return path


# ----------------------------------------------------------------------- helpers
def pick_font(preferred: str | None = None) -> str:
    for name in ([preferred] if preferred else []) + ["Arial", "Helvetica", "Liberation Sans", "Noto Sans", "DejaVu Sans"]:
        try:
            font_manager.findfont(name, fallback_to_default=False)
            return name
        except Exception:
            continue
    return "DejaVu Sans"


def _numeric(series: pd.Series) -> pd.Series | None:
    if pd.api.types.is_bool_dtype(series):
        return None
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce")
    s = series.astype(str).str.replace(",", "", regex=False).str.replace("%", "", regex=False).str.strip()
    s = s.where(series.notna(), None)
    num = pd.to_numeric(s, errors="coerce")
    ok = num.notna().sum()
    return num if series.notna().sum() and ok >= 0.9 * series.notna().sum() else None


def fmt_num(x: float, decimals: int) -> str:
    return f"{x:,.{decimals}f}"


def _auto_decimals(vals: np.ndarray) -> int:
    if np.allclose(vals, np.round(vals)):
        return 0
    span = float(np.nanmax(vals) - np.nanmin(vals))
    return 0 if span >= 200 else 1 if span >= 20 else 2 if span >= 2 else 3


def _nice(x: float) -> float:
    if x <= 0:
        return 1.0
    p = 10 ** math.floor(math.log10(x))
    for m in (1, 2, 5, 10):
        if m * p >= x * 0.999:
            return m * p
    return 10 * p


def _nice_floor(x: float) -> float:
    p = 10 ** math.floor(math.log10(x))
    best = p
    for m in (1, 2, 5):
        if m * p <= x:
            best = m * p
    return best


def _classify(values: pd.Series, opts: MapOptions):
    import mapclassify as mc

    vals = values.dropna().to_numpy(float)
    if not len(vals):
        raise DataError(f"Column '{opts.color_by}' has no numeric values to map.")
    uniq = np.unique(vals)
    k = max(1, min(opts.k, len(uniq)))
    mode = opts.classification
    if mode == "manual":
        if not opts.bins:
            raise DataError("Manual classification needs bin edges, for example 10,20,50.")
        edges = sorted(float(b) for b in opts.bins)
        if edges[-1] < vals.max():
            edges.append(float(vals.max()))
        bins = np.array(edges)
    elif k == 1:
        bins = np.array([vals.max()])
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if mode == "equal":
                bins = mc.EqualInterval(vals, k=k).bins
            elif mode == "jenks":
                bins = (mc.FisherJenks(vals, k=k) if len(vals) <= 1500 else mc.NaturalBreaks(vals, k=k)).bins
            else:
                bins = mc.Quantiles(vals, k=k).bins
    bins = np.unique(np.asarray(bins, float))
    cls = np.searchsorted(bins, values.to_numpy(float), side="left")
    cls = np.where(values.isna(), -1, np.clip(cls, 0, len(bins) - 1))
    lows = np.concatenate([[vals.min()], bins[:-1]])
    is_int = bool(np.allclose(vals, np.round(vals)))
    dec = opts.decimals if opts.decimals is not None else _auto_decimals(vals)
    labels = []
    for i, (lo, hi) in enumerate(zip(lows, bins)):
        if is_int and i > 0:
            lo = bins[i - 1] + 1
        if is_int and lo >= hi:
            labels.append(fmt_num(hi, dec))
        else:
            labels.append(f"{fmt_num(lo, dec)} – {fmt_num(hi, dec)}")
    return cls, labels, bins


def _class_colors(n: int, cmap_name: str | None, reverse: bool):
    import matplotlib as mpl

    name = cmap_name or "YlOrRd"
    try:
        cm = mpl.colormaps[name]
    except KeyError as exc:
        raise DataError(f"Unknown colour map '{name}'. Try YlOrRd, Blues, viridis, RdBu.") from exc
    base = name.removesuffix("_r")
    lo, hi = (0.05, 0.95) if base in DIVERGING else (0.12, 0.95)
    pts = np.linspace(lo, hi, n) if n > 1 else np.array([0.6])
    cols = [tuple(cm(p)) for p in pts]
    return cols[::-1] if reverse else cols


def _colorize(gdf, opts: MapOptions, theme):
    """Return (facecolors list, legend items [(label, rgba)], info dict)."""
    n = len(gdf)
    land = to_rgba(theme["land"])
    if not opts.color_by:
        return [land] * n, [], {"mode": "plain"}
    if opts.color_by not in gdf.columns:
        raise DataError(f"Column '{opts.color_by}' not found. Available: {', '.join(map(str, gdf.columns[:15]))}")
    col = gdf[opts.color_by]
    mode = opts.classification
    num = _numeric(col)
    if mode == "auto":
        mode = "quantiles" if num is not None and num.nunique() > 8 else ("categorical" if num is None or num.nunique() <= 8 else "quantiles")
    nodata = to_rgba(theme["nodata"])
    if mode == "categorical":
        cats = [c for c in pd.unique(col.dropna())]
        try:
            cats = sorted(cats)
        except TypeError:
            cats = sorted(cats, key=str)
        if len(cats) > 20:
            raise DataError(f"'{opts.color_by}' has {len(cats)} distinct values, too many for a categorical map. Use a numeric classification.")
        if len(cats) <= 8:
            palette = [to_rgba(c) for c in OKABE_ITO]
        else:
            import matplotlib as mpl
            palette = [tuple(mpl.colormaps["tab20"](i / 19)) for i in range(20)]
        cmap = {c: palette[i] for i, c in enumerate(cats)}
        faces = [cmap.get(v, nodata) if pd.notna(v) else nodata for v in col]
        items = [(clean_text(c), cmap[c]) for c in cats]
        info = {"mode": "categorical", "classes": [i[0] for i in items]}
    else:
        if num is None:
            raise DataError(f"'{opts.color_by}' is not numeric, so it cannot be classified into ranges. Use categorical.")
        opts2 = replace(opts, classification=mode)
        cls, labels, bins = _classify(num, opts2)
        colors = _class_colors(len(bins), opts.cmap, opts.reverse_cmap)
        faces = [colors[c] if c >= 0 else nodata for c in cls]
        items = list(zip(labels, colors))
        info = {"mode": mode, "classes": labels, "breaks": [float(b) for b in bins]}
    if (col.isna()).any():
        items.append((opts.nodata_label, nodata))
        info["missing"] = int(col.isna().sum())
    return faces, items, info


# ----------------------------------------------------------------- map furniture
def _graticule(ax, crs: CRS, xlim, ylim, theme, size):
    try:
        to_ll = Transformer.from_crs(crs, 4326, always_xy=True)
        to_xy = Transformer.from_crs(4326, crs, always_xy=True)
        xs, ys = np.meshgrid(np.linspace(*xlim, 25), np.linspace(*ylim, 25))
        lon, lat = to_ll.transform(xs.ravel(), ys.ravel())
        lon, lat = np.asarray(lon, float), np.asarray(lat, float)
        ok = np.isfinite(lon) & np.isfinite(lat)
        if ok.sum() < 20:
            return
        r = np.radians(lon[ok])
        lon0 = float(np.degrees(np.arctan2(np.sin(r).mean(), np.cos(r).mean())))
        lon = ((lon - lon0 + 180) % 360) - 180 + lon0
        lo0, lo1, la0, la1 = lon[ok].min(), lon[ok].max(), lat[ok].min(), lat[ok].max()
        step = _nice_floor(min(lo1 - lo0, la1 - la0) / 3.2) if min(lo1 - lo0, la1 - la0) > 0 else 1.0
        step = max(step, 0.05)
        merids = np.arange(math.ceil(lo0 / step) * step, lo1 + 1e-9, step)
        parals = np.arange(math.ceil(la0 / step) * step, la1 + 1e-9, step)
        parals = parals[(parals > -90) & (parals < 90)]
        xt, xl, yt, yl = [], [], [], []
        fmt = lambda v, p, n: f"{abs(v):.10g}°{p if v >= 0 else n}"
        span_lat = np.linspace(max(la0 - step, -89.9), min(la1 + step, 89.9), 240)
        for m in merids:
            X, Y = to_xy.transform(np.full_like(span_lat, ((m + 180) % 360) - 180), span_lat)
            X, Y = np.asarray(X), np.asarray(Y)
            ax.plot(X, Y, color=theme["grid"], lw=0.45, ls=(0, (4, 3)), alpha=0.75, zorder=2)
            if np.all(np.isfinite(Y)) and Y.min() <= ylim[0] <= Y.max():
                o = np.argsort(Y)
                x = float(np.interp(ylim[0], Y[o], X[o]))
                if xlim[0] < x < xlim[1]:
                    xt.append(x)
                    xl.append(fmt(((m + 180) % 360) - 180, "E", "W"))
        span_lon = np.linspace(lo0 - step, lo1 + step, 240)
        for p in parals:
            X, Y = to_xy.transform(((span_lon + 180) % 360) - 180, np.full_like(span_lon, p))
            X, Y = np.asarray(X), np.asarray(Y)
            ax.plot(X, Y, color=theme["grid"], lw=0.45, ls=(0, (4, 3)), alpha=0.75, zorder=2)
            if np.all(np.isfinite(X)) and X.min() <= xlim[0] <= X.max():
                o = np.argsort(X)
                y = float(np.interp(xlim[0], X[o], Y[o]))
                if ylim[0] < y < ylim[1]:
                    yt.append(y)
                    yl.append(fmt(p, "N", "S"))
        ax.set_xticks(xt)
        ax.set_xticklabels(xl, fontsize=size, color=theme["sub"])
        ax.set_yticks(yt)
        ax.set_yticklabels(yl, fontsize=size, color=theme["sub"], rotation=90, va="center")
    except Exception:
        ax.set_xticks([])
        ax.set_yticks([])


def _north_arrow(ax, cx, cy, k, theme, family):
    w, h = 9 * k, 28 * k  # half width, height in data units
    base = cy - h / 2
    tip = (cx, base + h)
    left = [tip, (cx - w, base), (cx, base + h * 0.28)]
    right = [tip, (cx + w, base), (cx, base + h * 0.28)]
    ax.add_patch(MplPolygon(left, closed=True, fc=theme["text"], ec=theme["text"], lw=0.6, zorder=8))
    ax.add_patch(MplPolygon(right, closed=True, fc=theme["bg"], ec=theme["text"], lw=0.6, zorder=8))
    ax.text(cx, base + h + 2 * k, "N", ha="center", va="bottom", fontsize=10, fontweight="bold",
            color=theme["text"], fontfamily=family, zorder=8)


def _scale_bar(ax, x0, y0, length_data, label_total, k, theme, family, size=7.5):
    h = 4.5 * k
    half = length_data / 2
    ax.add_patch(Rectangle((x0, y0), half, h, fc=theme["text"], ec=theme["text"], lw=0.6, zorder=8))
    ax.add_patch(Rectangle((x0 + half, y0), half, h, fc=theme["bg"], ec=theme["text"], lw=0.6, zorder=8))
    ty = y0 - 2 * k
    for x, s in ((x0, "0"), (x0 + half, label_total[0]), (x0 + length_data, label_total[1])):
        ax.text(x, ty, s, ha="center", va="top", fontsize=size, color=theme["text"], fontfamily=family, zorder=8)


def _legend_size(items, title, family, theme, fs=8.5):
    """Measure the legend in points using a scratch figure."""
    f = Figure(figsize=(4, 4), dpi=72)
    FigureCanvasAgg(f)
    a = f.add_axes([0, 0, 1, 1])
    leg = _make_legend(a, items, title, "lower left", family, theme, fs, pad=0)
    f.canvas.draw()
    bb = leg.get_window_extent()
    return bb.width, bb.height


def _make_legend(ax, items, title, loc, family, theme, fs, pad=8):
    handles = [Rectangle((0, 0), 1, 1, fc=c, ec=theme["edge"], lw=0.5) for _, c in items]
    leg = ax.legend(
        handles, [l for l, _ in items], title=title, loc=loc, fontsize=fs, title_fontsize=fs + 1,
        frameon=True, fancybox=False, framealpha=0.94, facecolor=theme["bg"], edgecolor=theme["frame"],
        handlelength=1.5, handleheight=1.0, labelspacing=0.45, borderpad=0.7,
        borderaxespad=pad / fs, prop={"family": family, "size": fs}, alignment="left",
    )
    leg.get_frame().set_linewidth(0.6)
    leg.get_title().set_fontfamily(family)
    leg.get_title().set_fontweight("bold")
    leg.get_title().set_color(theme["text"])
    for t in leg.get_texts():
        t.set_color(theme["text"])
    leg.set_zorder(9)
    return leg


# ----------------------------------------------------------------------- main
def make_map(gdf, options: MapOptions | None = None, overlay=None, **kw) -> MapResult:
    """Build a finished map from a GeoDataFrame. Keyword arguments override ``options``."""
    opts = replace(options or MapOptions(), **kw)
    theme = THEMES.get(opts.theme)
    if theme is None:
        raise DataError(f"Unknown theme '{opts.theme}'. Use: {', '.join(THEMES)}")
    family = pick_font(opts.font)
    report: dict = {"notes": [], "font": family}

    gdf, kind, crs_desc, notes = prepare(gdf, opts.crs, opts.assume_crs)
    report["notes"] += notes
    report["crs"] = crs_desc
    if overlay is not None:
        overlay, okind, _, onotes = prepare(overlay, gdf.crs)
        if okind != POLY:
            raise DataError("The overlay layer must be polygons (for example state boundaries).")
        report["notes"] += [f"Overlay: {n}" for n in onotes]

    faces, items, cinfo = _colorize(gdf, opts, theme)
    report["colour"] = cinfo
    lums = np.array([luminance(c) for c in faces])

    # ---- extent, element sizes, layout --------------------------------------------------
    minx, miny, maxx, maxy = gdf.total_bounds
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    dx, dy = max(maxx - minx, 1e-9), max(maxy - miny, 1e-9)
    ext = max(dx, dy)
    mpu = meters_per_unit(gdf.crs, 0.0)
    if gdf.crs.is_geographic:
        try:
            mpu = meters_per_unit(gdf.crs, cy)
        except Exception:
            mpu = None

    geoms = list(gdf.geometry.values)
    cover_src = geoms if kind == POLY else [g.buffer(ext * 0.008) for g in geoms]
    if kind == POLY:
        tol = ext * 0.003
        cover_src = [g.simplify(tol) for g in geoms]
    cover = unary_union(cover_src)

    legend_items = items if (opts.legend and items) else []
    legend_title = opts.legend_title if opts.legend_title is not None else (opts.color_by or "")
    if legend_items:
        leg_w, leg_h = _legend_size(legend_items, legend_title, family, theme)
    else:
        leg_w = leg_h = 0.0
    M = 9.0  # outer margin in points
    margins_in = dict(left=0.62 if opts.graticule else 0.3, right=0.3)

    def layout(pad):
        px, py = dx * (1 + 2 * pad), dy * (1 + 2 * pad)
        a = py / px
        map_w = opts.width_in - margins_in["left"] - margins_in["right"]
        map_h = map_w * a
        if map_h > 11.0:
            map_h, map_w = 11.0, 11.0 / a
        map_h = max(map_h, 2.4)
        k = px / (map_w * 72.0)
        return dict(px=px, py=py, map_w=map_w, map_h=map_h, k=k,
                    xlim=(cx - px / 2, cx + px / 2), ylim=(cy - py / 2, cy + py / 2))

    def element_rects(L):
        k = L["k"]
        els = {}
        if legend_items:
            els["legend"] = (leg_w + 2, leg_h + 2)
        if opts.north_arrow:
            els["north"] = (24, 48)
        bar = None
        if opts.scale_bar and mpu:
            target_m = L["px"] * mpu * 0.2
            nice_m = _nice_floor(max(target_m, 1.0))
            bar = (nice_m, nice_m / mpu)
            els["scale"] = (bar[1] / k + 30, 30)
        return els, bar

    def corner_rect(corner, size_pt, L):
        k = L["k"]
        w, h = size_pt[0] * k, size_pt[1] * k
        (x0, x1), (y0, y1) = L["xlim"], L["ylim"]
        m = M * k
        x = x0 + m if "left" in corner else x1 - m - w
        y = y0 + m if "lower" in corner else y1 - m - h
        return (x, y, x + w, y + h)

    prefs = {"legend": ["lower left", "upper left", "lower right", "upper right"],
             "north": ["upper right", "upper left", "lower right", "lower left"],
             "scale": ["lower right", "lower left", "upper right", "upper left"]}
    if opts.legend_loc in CORNERS:
        prefs["legend"] = [opts.legend_loc]

    from itertools import permutations

    best = None
    for pad in (0.05, 0.1, 0.16, 0.24, 0.34):
        L = layout(pad)
        els, bar = element_rects(L)
        names = list(els)
        score_cache = {}

        def overlap(name, corner):
            key = (name, corner)
            if key not in score_cache:
                r = corner_rect(corner, els[name], L)
                bx = sbox(*r)
                score_cache[key] = bx.intersection(cover).area / bx.area if bx.area else 0.0
            return score_cache[key]

        cand = None
        for combo in permutations(CORNERS, len(names)):
            if any(c not in prefs[n] for n, c in zip(names, combo)):
                continue
            ov = [overlap(n, c) for n, c in zip(names, combo)]
            cost = max(ov, default=0) * 10 + sum(ov) + sum(prefs[n].index(c) for n, c in zip(names, combo)) * 0.002
            if cand is None or cost < cand[0]:
                cand = (cost, dict(zip(names, combo)), max(ov, default=0))
        if cand is None:  # more elements than allowed corners: let them share by preference order
            cand = (0, {n: prefs[n][0] for n in names}, 0)
        if best is None or cand[2] < best[0][2] - 1e-6:
            best = (cand, L, els, bar, pad)
        if cand[2] < 0.02:
            break
    (_, place, worst), L, els, bar, pad = best
    report["padding"] = pad
    report["element_overlap"] = round(worst, 3)
    k = L["k"]

    # header/footer geometry (inches)
    W = L["map_w"] + margins_in["left"] + margins_in["right"]
    W = max(W, 6.2)
    title_fs, sub_fs, foot_fs = 16.0, 10.5, 7.5

    def wrap(s, fs):
        cap = max(20, int((W - 0.7) * 72 / (fs * 0.52)))
        return textwrap.fill(s, cap)

    t_txt = wrap(opts.title, title_fs) if opts.title else ""
    s_txt = wrap(opts.subtitle, sub_fs) if opts.subtitle else ""
    foot_lines = []
    if opts.source:
        foot_lines.append(wrap(opts.source, foot_fs))
    if opts.crs_note:
        foot_lines.append(wrap(f"Projection: {crs_desc}. Scale and north arrow are approximate.", foot_fs))
    if opts.credit:
        foot_lines.append("Made with geomapclean (github.com/Moses-L/geomapclean)")
    foot_txt = "\n".join(foot_lines)
    header_in = 0.18
    if t_txt:
        header_in += (t_txt.count("\n") + 1) * title_fs * 1.25 / 72 + 0.06
    if s_txt:
        header_in += (s_txt.count("\n") + 1) * sub_fs * 1.3 / 72 + 0.06
    header_in += 0.1
    tick_in = 0.3 if opts.graticule else 0.12
    footer_in = tick_in + (foot_txt.count("\n") + 1) * foot_fs * 1.35 / 72 + 0.14 if foot_txt else tick_in + 0.1
    H = header_in + L["map_h"] + footer_in

    fig = Figure(figsize=(W, H), dpi=72, facecolor=theme["bg"])
    FigureCanvasAgg(fig)
    left_in = margins_in["left"] + (W - margins_in["left"] - margins_in["right"] - L["map_w"]) / 2
    ax = fig.add_axes([left_in / W, footer_in / H, L["map_w"] / W, L["map_h"] / H])
    ax.set_facecolor(theme["bg"])
    ax.set_xlim(*L["xlim"])
    ax.set_ylim(*L["ylim"])
    ax.set_aspect("equal", adjustable="box")
    for s in ax.spines.values():
        s.set_visible(opts.frame)
        s.set_linewidth(0.9)
        s.set_edgecolor(theme["frame"])
    ax.tick_params(length=3, width=0.6, color=theme["frame"], pad=2)
    ax.set_xlabel("")
    ax.set_ylabel("")

    # ---- layers -------------------------------------------------------------------
    if kind == POLY:
        gdf.plot(ax=ax, facecolor=faces, edgecolor=theme["edge"], linewidth=theme["edge_w"], zorder=1)
    elif kind == LINE:
        gdf.plot(ax=ax, color=faces if cinfo["mode"] != "plain" else theme["text"], linewidth=opts.line_width, zorder=3)
    else:
        xs = [g.x if g.geom_type == "Point" else g.representative_point().x for g in geoms]
        ys = [g.y if g.geom_type == "Point" else g.representative_point().y for g in geoms]
        pc = faces if cinfo["mode"] != "plain" else [to_rgba("#c0392b")] * len(xs)
        ax.scatter(xs, ys, s=opts.point_size**2, c=pc, edgecolors=theme["bg"], linewidths=0.6, zorder=4)
    if overlay is not None:
        overlay.boundary.plot(ax=ax, color=theme["overlay"], linewidth=opts.overlay_width, zorder=3)
    ax.set_xlabel("")
    ax.set_ylabel("")
    ax.set_xlim(*L["xlim"])
    ax.set_ylim(*L["ylim"])
    ax.set_aspect("equal", adjustable="box")
    if opts.graticule:
        _graticule(ax, gdf.crs, L["xlim"], L["ylim"], theme, 7.5)
    else:
        ax.set_xticks([])
        ax.set_yticks([])

    # ---- furniture ----------------------------------------------------------------
    reserved = []
    if "legend" in place:
        loc = place["legend"]
        _make_legend(ax, legend_items, legend_title, loc, family, theme, 8.5, pad=M)
        reserved.append(corner_rect(loc, els["legend"], L))
    if "north" in place:
        r = corner_rect(place["north"], els["north"], L)
        _north_arrow(ax, (r[0] + r[2]) / 2, (r[1] + r[3]) / 2 - 3 * k, k, theme, family)
        reserved.append(r)
    if "scale" in place and bar:
        r = corner_rect(place["scale"], els["scale"], L)
        n_m, n_u = bar
        km = n_m >= 1000
        tot = f"{n_m / 1000:g} km" if km else f"{n_m:g} m"
        half = f"{n_m / 2000:g}" if km else f"{n_m / 2:g}"
        _scale_bar(ax, r[0] + 6 * k, r[1] + 17 * k, n_u, (half, tot), k, theme, family)
        reserved.append(r)

    # ---- labels -------------------------------------------------------------------
    report["labels"] = {"placed": 0, "skipped": 0, "column": None}
    if opts.labels:
        placer = LabelPlacer(fig, ax, k, family)
        for r in reserved:
            placer.reserve(r)
        style = LabelStyle(font_max=opts.label_font_max, font_min=opts.label_font_min,
                           case=opts.label_case,
                           overflow=(len(gdf) <= 80) if opts.label_overflow is None else opts.label_overflow,
                           max_labels=opts.label_max)
        if overlay is not None:
            ocol = opts.overlay_label_column
            if ocol in (None, "auto"):
                ocol = guess_label_column(overlay)
            if ocol:
                ostyle = replace(style, weight="bold", case="upper", font_max=max(style.font_max + 1, 12),
                                 overflow=True, max_labels=None)
                otexts = [clean_text(v, "upper") for v in overlay[ocol]]
                placer.place_polygons(list(overlay.geometry.values), otexts, None, ostyle, weight="bold", color_mode="plain")
        col = opts.label_column
        if col in (None, "auto", ""):
            col = guess_label_column(gdf)
        if col and col not in gdf.columns:
            raise DataError(f"Label column '{col}' not found. Available: {', '.join(map(str, gdf.columns[:15]))}")
        if col:
            texts = [clean_text(v, opts.label_case) for v in gdf[col]]
            if kind == POLY:
                placer.place_polygons(geoms, texts, lums, style)
            elif kind == POINT:
                placer.place_points(geoms, texts, style, marker_pt=opts.point_size)
            else:
                placer.place_lines(geoms, texts, style)
            report["labels"] = {"placed": placer.report["placed"], "skipped": len(placer.report["skipped"]),
                                "skipped_names": placer.report["skipped"][:200], "column": col}
        else:
            report["notes"].append("No obvious name column found, so no labels were drawn. Choose a label column.")
    report["features"] = int(len(gdf))
    report["geometry"] = kind

    # ---- header / footer ----------------------------------------------------------
    y = 1 - 0.18 / H
    if t_txt:
        fig.text(left_in / W - 0.0, y, t_txt, ha="left", va="top", fontsize=title_fs, fontweight="bold",
                 color=theme["text"], fontfamily=family, linespacing=1.15)
        y -= ((t_txt.count("\n") + 1) * title_fs * 1.25 / 72 + 0.06) / H
    if s_txt:
        fig.text(left_in / W, y, s_txt, ha="left", va="top", fontsize=sub_fs, color=theme["sub"], fontfamily=family)
    if foot_txt:
        fig.text(left_in / W, 0.08 / H, foot_txt, ha="left", va="bottom", fontsize=foot_fs, color=theme["sub"],
                 fontfamily=family, linespacing=1.3)
    return MapResult(fig=fig, report=report)
