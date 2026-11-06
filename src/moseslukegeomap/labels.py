"""Automatic label-column detection and collision-free label placement."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

import numpy as np
import pandas as pd
import shapely
from matplotlib import patheffects
from matplotlib.font_manager import FontProperties
from shapely.ops import polylabel

_NOT_NAMES = re.compile(
    r"pcode|(^|_)code($|_)|ref$|_ref|alt\d|date|valid|shape|objectid|^gid$|"
    r"(^|_)(id|fid|x|y|lat|lon|lng|area|leng\w*|type|level|iso\w*)($|_)",
    re.I,
)
_NAMEISH = re.compile(r"name|nom|nombre|_en$|label|title|lga|ward|state|district|county|region", re.I)


def guess_label_column(gdf) -> str | None:
    """Pick the column most likely to hold place names (None if nothing fits)."""
    best, best_score = None, 0.0
    n = len(gdf)
    for c in gdf.columns:
        if c == gdf.geometry.name or _NOT_NAMES.search(str(c)):
            continue
        s = gdf[c]
        if not (s.dtype == object or pd.api.types.is_string_dtype(s) or str(s.dtype) == "category"):
            continue
        nn = s.dropna().astype(str).str.strip()
        nn = nn[nn != ""]
        if len(nn) < 0.5 * n or nn.str.len().median() > 40:
            continue
        uniq = nn.nunique() / len(nn)
        score = uniq
        if _NAMEISH.search(str(c)):
            score += 1.0
        m = re.search(r"adm(\d)", str(c).lower())
        if m:
            score += 0.1 * int(m.group(1))
        if uniq < 0.05 and n > 20:
            score -= 1.0
        if score > best_score:
            best, best_score = c, score
    return best


def clean_text(v, case: str = "as-is") -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)) or (v is pd.NA):
        return ""
    if isinstance(v, (float, np.floating)) and float(v).is_integer():
        v = int(v)
    s = re.sub(r"\s+", " ", str(v)).strip()
    if s.lower() in {"nan", "none", "null", "<na>"}:
        return ""
    if case == "upper":
        return s.upper()
    if case == "title":
        return "-".join(" ".join(w[:1].upper() + w[1:].lower() for w in part.split(" ")) for part in s.split("-"))
    return s


def wrap_two_lines(s: str) -> str | None:
    words = s.split(" ")
    if len(words) < 2:
        return None
    best, best_gap = None, 10**9
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        gap = abs(len(a) - len(b))
        if gap < best_gap:
            best, best_gap = f"{a}\n{b}", gap
    return best


def luminance(rgba) -> float:
    r, g, b = rgba[:3]
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


@dataclass
class LabelStyle:
    font_max: float = 11.0
    font_min: float = 5.5
    point_font: float = 8.0
    weight: str = "normal"
    case: str = "as-is"
    wrap: bool = True
    overflow: bool = False
    max_labels: int | None = None
    dark_text: str = "#1b1b1b"
    light_text: str = "#ffffff"
    halo_light: str = "#ffffff"
    halo_dark: str = "#1b1b1b"
    z: int = 6


class LabelPlacer:
    """Greedy, largest-first placement that never lets two labels touch.

    Everything is measured in points on a 72-dpi figure and converted to data
    units with ``k`` (data units per point), so the layout is identical at
    any export resolution.
    """

    PAD = 2.0  # points of breathing room around each label (covers the halo)

    def __init__(self, fig, ax, k: float, family: str):
        self.fig, self.ax, self.k, self.family = fig, ax, k, family
        self.renderer = fig.canvas.get_renderer()
        self.boxes = np.zeros((0, 4))
        self.lines = np.zeros((0, 4))  # thin boxes along leader lines
        self._cache: dict = {}
        x0, x1 = ax.get_xlim()
        y0, y1 = ax.get_ylim()
        self.view = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        self.report = {"placed": 0, "skipped": []}

    # -- geometry helpers --------------------------------------------------
    LINESPACING = 1.05

    def measure(self, text: str, weight: str):
        """Width and height in points at 10 pt, replicating matplotlib's own text layout."""
        key = (text, weight)
        if key not in self._cache:
            fp = FontProperties(family=self.family, weight=weight, size=10)
            get = lambda s: self.renderer.get_text_width_height_descent(s, fp, ismath=False)
            _, lp_h, lp_d = get("lp")
            min_dy = (lp_h - lp_d) * self.LINESPACING
            ws, y, last_d = [], 0.0, 0.0
            for i, line in enumerate(text.split("\n")):
                w, h, d = get(line)
                h, d = max(h, lp_h), max(d, lp_d)
                ws.append(w)
                y = -(h - d) if i == 0 else y - max(min_dy, (h - d) * self.LINESPACING)
                y -= d
                last_d = d
            self._cache[key] = (max(ws), -(y))
        return self._cache[key]

    def collides(self, box) -> bool:
        b = np.vstack([self.boxes, self.lines]) if len(self.lines) else self.boxes
        if not len(b):
            return False
        return bool(np.any((box[0] < b[:, 2]) & (box[2] > b[:, 0]) & (box[1] < b[:, 3]) & (box[3] > b[:, 1])))

    def _seg_hits(self, x0, y0, x1, y1) -> bool:
        """Does the segment pass through any reserved box? (Liang-Barsky, vectorised.)"""
        b = self.boxes
        if not len(b):
            return False
        dx, dy = x1 - x0, y1 - y0
        t0, t1 = np.zeros(len(b)), np.ones(len(b))
        for p, q in ((-dx, x0 - b[:, 0]), (dx, b[:, 2] - x0), (-dy, y0 - b[:, 1]), (dy, b[:, 3] - y0)):
            if p == 0:
                t1 = np.where(q < 0, -1.0, t1)
            elif p < 0:
                t0 = np.maximum(t0, q / p)
            else:
                t1 = np.minimum(t1, q / p)
        return bool(np.any(t0 <= t1))

    def _reserve_segment(self, x0, y0, x1, y1) -> None:
        n = max(2, int(math.hypot(x1 - x0, y1 - y0) / (2.0 * self.k)))
        h = 0.8 * self.k
        pts = [(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t) for t in np.linspace(0, 1, n)]
        new = np.array([(x - h, y - h, x + h, y + h) for x, y in pts])
        self.lines = np.vstack([self.lines, new])

    def reserve(self, box) -> None:
        self.boxes = np.vstack([self.boxes, np.asarray(box, float)[None, :]])

    def _inside_view(self, box) -> bool:
        return box[0] >= self.view[0] and box[1] >= self.view[1] and box[2] <= self.view[2] and box[3] <= self.view[3]

    def _box(self, x, y, w_pt, h_pt):
        w, h = (w_pt + self.PAD) * self.k, (h_pt + self.PAD) * self.k
        return (x - w / 2, y - h / 2, x + w / 2, y + h / 2)

    def _draw(self, x, y, text, size, color_mode, style, weight, ha="center", va="center", rotation=0.0, fill_lum=None):
        dark = fill_lum is not None and fill_lum < 0.22 if color_mode == "auto" else False
        color = style.light_text if dark else style.dark_text
        halo = style.halo_dark if dark else style.halo_light
        t = self.ax.text(
            x, y, text, fontsize=size, color=color, ha=ha, va=va, rotation=rotation,
            fontweight=weight, fontfamily=self.family, zorder=style.z,
            multialignment="center", linespacing=1.05, clip_on=True, rotation_mode="anchor",
        )
        t.set_path_effects([patheffects.withStroke(linewidth=max(1.3, size * 0.28), foreground=halo)])
        self.report["placed"] += 1
        return t

    # -- polygons ----------------------------------------------------------
    def place_polygons(self, geoms, texts, lums, style: LabelStyle, weight: str | None = None, color_mode="auto"):
        weight = weight or style.weight
        sizes = np.arange(style.font_min, style.font_max + 1e-9, 0.5)
        areas = np.array([g.area for g in geoms])
        order = np.argsort(-areas)
        done = 0
        leftovers = []
        for i in order:
            text = texts[i]
            if not text:
                continue
            if style.max_labels is not None and done >= style.max_labels:
                self.report["skipped"].append(text)
                continue
            part = _largest_part(geoms[i])
            if part is None:
                continue
            bx0, by0, bx1, by1 = part.bounds
            if (bx1 - bx0) / self.k < 6 or (by1 - by0) / self.k < 6:
                leftovers.append(i)
                continue
            shapely.prepare(part)
            pt = polylabel(part, tolerance=max(self.k * 0.4, 1e-9))
            r = part.boundary.distance(pt)
            best = None  # (size, text, w10, h10)
            variants = [text]
            if style.wrap:
                two = wrap_two_lines(text)
                if two:
                    variants.append(two)
            for v in variants:
                w10, h10 = self.measure(v, weight)
                idx = self._fit(part, pt, r, w10, h10, sizes, (bx1 - bx0, by1 - by0))
                if idx >= 0 and (best is None or sizes[idx] > best[0] + 0.99):
                    best = (sizes[idx], v, w10, h10)
            if best is None:
                leftovers.append(i)
                continue
            size, v, _, _ = best
            self._draw(pt.x, pt.y, v, size, color_mode, style, weight, fill_lum=lums[i] if lums is not None else None)
            w10, h10 = self.measure(v, weight)
            self.reserve(self._box(pt.x, pt.y, w10 * size / 10, h10 * size / 10))
            done += 1
        # second pass: small units, label only where there is genuinely free space
        for i in leftovers:
            text = texts[i]
            if not style.overflow:
                self.report["skipped"].append(text)
                continue
            part = _largest_part(geoms[i])
            if part is None:
                self.report["skipped"].append(text)
                continue
            anchors = [polylabel(part, tolerance=max(self.k * 0.4, 1e-9)), part.representative_point()]
            if part.contains(part.centroid):
                anchors.append(part.centroid)
            anchors = [p for p in anchors if not self._point_in_boxes(p.x, p.y)]
            if not anchors:  # centre is covered by a neighbour's label: sample other interior points
                x0, y0, x1, y1 = part.bounds
                grid = [shapely.Point(x, y) for x in np.linspace(x0, x1, 9)[1:-1] for y in np.linspace(y0, y1, 9)[1:-1]]
                grid = [p for p in grid if part.contains(p) and not self._point_in_boxes(p.x, p.y)]
                anchors = sorted(grid, key=lambda p: -part.boundary.distance(p))[:4]
            if not anchors:
                self.report["skipped"].append(text)
                continue
            size = max(style.font_min, 6.0)
            variants = [text]
            two = wrap_two_lines(text) if style.wrap else None
            if two:
                variants.append(two)
            if not self._try_leader(anchors, variants, size, weight, style):
                self.report["skipped"].append(text)

    def _point_in_boxes(self, x, y) -> bool:
        b = self.boxes
        return bool(len(b) and np.any((x > b[:, 0]) & (x < b[:, 2]) & (y > b[:, 1]) & (y < b[:, 3])))

    def _try_leader(self, anchors, variants, size, weight, style) -> bool:
        """Put a label beside its (too small) unit and join them with a thin leader line."""
        for pt in anchors:
            for d_pt in (10.0, 20.0, 34.0, 50.0, 70.0, 95.0):
                for ux, uy in _DIRS:
                    for v in variants:
                        w10, h10 = self.measure(v, weight)
                        w, h = (w10 * size / 10 + self.PAD) * self.k, (h10 * size / 10 + self.PAD) * self.k
                        d = d_pt * self.k
                        cx, cy = pt.x + ux * (w / 2 + d), pt.y + uy * (h / 2 + d)
                        box = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
                        if not self._inside_view(box) or self.collides(box):
                            continue
                        ex, ey = min(max(pt.x, box[0]), box[2]), min(max(pt.y, box[1]), box[3])
                        if self._seg_hits(pt.x, pt.y, ex, ey):
                            continue
                        self._draw(cx, cy, v, size, "plain", style, weight)
                        self._reserve_segment(pt.x, pt.y, ex, ey)
                        self.ax.plot([pt.x, ex], [pt.y, ey], color=style.dark_text, lw=0.55,
                                     zorder=style.z - 1, solid_capstyle="round")
                        self.ax.plot([pt.x], [pt.y], marker="o", ms=2.0, color=style.dark_text, zorder=style.z - 1)
                        self.reserve(box)
                        return True
        return False

    def _fit(self, part, pt, r, w10, h10, sizes, bsize) -> int:
        lo, hi, best = 0, len(sizes) - 1, -1
        while lo <= hi:
            mid = (lo + hi) // 2
            s = sizes[mid]
            box = self._box(pt.x, pt.y, w10 * s / 10, h10 * s / 10)
            w, h = box[2] - box[0], box[3] - box[1]
            if (w / 2) ** 2 + (h / 2) ** 2 <= (r * 0.98) ** 2:
                inside = True
            elif w > bsize[0] or h > bsize[1]:
                inside = False
            else:
                inside = part.contains(shapely.box(*box))
            if inside and self._inside_view(box) and not self.collides(box):
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        return best

    # -- points ------------------------------------------------------------
    def place_points(self, geoms, texts, style: LabelStyle, marker_pt: float = 4.0, weight: str | None = None):
        weight = weight or style.weight
        size = style.point_font
        off = (marker_pt / 2 + 2.0) * self.k
        for g, text in zip(geoms, texts):
            if not text:
                continue
            pt = g if g.geom_type == "Point" else g.representative_point()
            w10, h10 = self.measure(text, weight)
            w, h = (w10 * size / 10 + self.PAD) * self.k, (h10 * size / 10 + self.PAD) * self.k
            cands = [  # (box, ha, va, text x, text y)
                ((pt.x + off, pt.y - h / 2, pt.x + off + w, pt.y + h / 2), "left", "center", pt.x + off + self.PAD * self.k / 2, pt.y),
                ((pt.x - off - w, pt.y - h / 2, pt.x - off, pt.y + h / 2), "right", "center", pt.x - off - self.PAD * self.k / 2, pt.y),
                ((pt.x - w / 2, pt.y + off, pt.x + w / 2, pt.y + off + h), "center", "bottom", pt.x, pt.y + off + self.PAD * self.k / 2),
                ((pt.x - w / 2, pt.y - off - h, pt.x + w / 2, pt.y - off), "center", "top", pt.x, pt.y - off - self.PAD * self.k / 2),
                ((pt.x + off, pt.y + off, pt.x + off + w, pt.y + off + h), "left", "bottom", pt.x + off, pt.y + off),
                ((pt.x + off, pt.y - off - h, pt.x + off + w, pt.y - off), "left", "top", pt.x + off, pt.y - off),
                ((pt.x - off - w, pt.y + off, pt.x - off, pt.y + off + h), "right", "bottom", pt.x - off, pt.y + off),
                ((pt.x - off - w, pt.y - off - h, pt.x - off, pt.y - off), "right", "top", pt.x - off, pt.y - off),
            ]
            for box, ha, va, tx, ty in cands:
                if self._inside_view(box) and not self.collides(box):
                    self._draw(tx, ty, text, size, "plain", style, weight, ha=ha, va=va)
                    self.reserve(box)
                    break
            else:
                self.report["skipped"].append(text)

    # -- lines -------------------------------------------------------------
    def place_lines(self, geoms, texts, style: LabelStyle, weight: str | None = None):
        weight = weight or style.weight
        size = style.point_font
        order = np.argsort([-g.length for g in geoms])
        for i in order:
            text, g = texts[i], geoms[i]
            if not text:
                continue
            if g.geom_type.startswith("Multi"):
                g = max(shapely.get_parts(g), key=lambda p: p.length)
            w10, h10 = self.measure(text, weight)
            w, h = w10 * size / 10, h10 * size / 10
            if g.length < 1.4 * w * self.k:
                self.report["skipped"].append(text)
                continue
            mid = g.interpolate(0.5, normalized=True)
            a = g.interpolate(max(0.0, g.project(mid) - w * self.k / 2))
            b = g.interpolate(min(g.length, g.project(mid) + w * self.k / 2))
            ang = np.degrees(np.arctan2(b.y - a.y, b.x - a.x))
            if ang > 90:
                ang -= 180
            elif ang < -90:
                ang += 180
            rad = np.radians(ang)
            hw = (abs(np.cos(rad)) * w + abs(np.sin(rad)) * h) / 2 + self.PAD
            hh = (abs(np.sin(rad)) * w + abs(np.cos(rad)) * h) / 2 + self.PAD
            box = (mid.x - hw * self.k, mid.y - hh * self.k, mid.x + hw * self.k, mid.y + hh * self.k)
            if self._inside_view(box) and not self.collides(box):
                self._draw(mid.x, mid.y, text, size, "plain", style, weight, rotation=ang)
                self.reserve(box)
            else:
                self.report["skipped"].append(text)


_DIRS = [(math.cos(a), math.sin(a)) for a in np.radians(np.arange(0, 360, 22.5))]
_DIRS = [_DIRS[i] for i in (0, 8, 4, 12, 2, 6, 10, 14, 1, 7, 9, 15, 3, 5, 11, 13)]  # axis directions first


def _largest_part(geom):
    if geom is None or geom.is_empty:
        return None
    parts = [p for p in shapely.get_parts(geom) if p.geom_type == "Polygon" and not p.is_empty]
    return max(parts, key=lambda p: p.area) if parts else None
