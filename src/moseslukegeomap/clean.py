"""Make raw geodata safe to map: repair geometry, settle the CRS, pick a sensible projection."""
from __future__ import annotations

import math

import geopandas as gpd
import numpy as np
import shapely
from pyproj import CRS
from shapely.geometry import MultiPolygon, Polygon

from .io import DataError

POLY, LINE, POINT = "polygon", "line", "point"


def geometry_kind(gdf: gpd.GeoDataFrame) -> str:
    types = gdf.geom_type.dropna()
    counts = {
        POLY: types.str.contains("Polygon").sum(),
        LINE: types.str.contains("Line").sum(),
        POINT: types.str.contains("Point").sum(),
    }
    kind = max(counts, key=counts.get)
    if counts[kind] == 0:
        raise DataError("The layer has no usable geometry (no points, lines or polygons).")
    return kind


def _only(geom, kind):
    """Keep just the parts of a (possibly collection) geometry that match ``kind``."""
    if geom is None or geom.is_empty:
        return None
    want = {POLY: ("Polygon",), LINE: ("LineString",), POINT: ("Point",)}[kind]
    parts = [p for p in shapely.get_parts(geom) if p.geom_type in want and not p.is_empty]
    if not parts:
        return None
    if kind == POLY:
        return parts[0] if len(parts) == 1 else MultiPolygon(parts)
    if len(parts) == 1:
        return parts[0]
    return shapely.multilinestrings(parts) if kind == LINE else shapely.multipoints(parts)


def _circular_mean_lon(lons: np.ndarray) -> float:
    r = np.radians(lons)
    return float(np.degrees(np.arctan2(np.sin(r).mean(), np.cos(r).mean())))


def auto_crs(gdf_ll: gpd.GeoDataFrame) -> tuple[CRS, str]:
    """Choose a projection from the data's extent. Input must be in lon/lat."""
    pts = gdf_ll.geometry.representative_point()
    lons = pts.x.to_numpy()
    lat0 = float((gdf_ll.total_bounds[1] + gdf_ll.total_bounds[3]) / 2)
    lon0 = _circular_mean_lon(lons)
    dev = np.abs(((lons - lon0 + 180) % 360) - 180)
    span = float(dev.max() * 2) if len(dev) else 0.0
    lat_span = float(gdf_ll.total_bounds[3] - gdf_ll.total_bounds[1])
    if span > 150 or lat_span > 100:
        crs = CRS.from_proj4(f"+proj=eqearth +lon_0={lon0:.4f} +datum=WGS84 +units=m +no_defs")
        return crs, f"Equal Earth projection, centred on {_fmt_deg(lon0, 'E', 'W')}, WGS 84"
    if span <= 6 and abs(lat0) < 80:
        zone = int((lon0 + 180) // 6) + 1
        epsg = (32600 if lat0 >= 0 else 32700) + zone
        return CRS.from_epsg(epsg), f"UTM zone {zone}{'N' if lat0 >= 0 else 'S'} (EPSG:{epsg}), WGS 84"
    crs = CRS.from_proj4(
        f"+proj=laea +lat_0={lat0:.4f} +lon_0={lon0:.4f} +datum=WGS84 +units=m +no_defs"
    )
    return crs, (
        f"Lambert Azimuthal Equal Area, centred on {_fmt_deg(lon0, 'E', 'W')} "
        f"{_fmt_deg(lat0, 'N', 'S')}, WGS 84"
    )


def _fmt_deg(v: float, pos: str, neg: str) -> str:
    return f"{abs(v):.1f}°{pos if v >= 0 else neg}"


def looks_like_lonlat(gdf: gpd.GeoDataFrame) -> bool:
    minx, miny, maxx, maxy = gdf.total_bounds
    return bool(np.isfinite([minx, miny, maxx, maxy]).all() and -181 <= minx and maxx <= 361 and -91 <= miny and maxy <= 91)


def prepare(
    gdf: gpd.GeoDataFrame,
    target_crs="auto",
    assume_crs=None,
    simplify_vertices: int = 600_000,
):
    """Return ``(clean_gdf, kind, crs_description, notes)``.

    * drops empty geometry, forces 2D, repairs invalid polygons
    * assumes WGS 84 when the CRS is missing and the numbers look like lon/lat
    * projects geographic data to an automatic, extent-appropriate CRS
    """
    notes: list[str] = []
    gdf = gdf.copy()
    gdf = gdf[~(gdf.geometry.isna() | gdf.geometry.is_empty)].copy()
    if gdf.empty:
        raise DataError("Every feature in this layer has empty geometry.")
    gdf["geometry"] = shapely.force_2d(gdf.geometry.values)

    if gdf.crs is None:
        if assume_crs is not None:
            gdf = gdf.set_crs(assume_crs)
            notes.append(f"No CRS in the file; used the one you supplied ({gdf.crs.to_string()}).")
        elif looks_like_lonlat(gdf):
            gdf = gdf.set_crs(4326)
            notes.append("No CRS in the file. The coordinates look like longitude/latitude, so WGS 84 (EPSG:4326) was assumed.")
        else:
            raise DataError(
                "The file has no coordinate system (.prj missing) and the coordinates are not "
                "longitude/latitude. Tell the tool the EPSG code, for example 32632."
            )

    kind = geometry_kind(gdf)
    other = gdf.geom_type.dropna().map(lambda t: POLY if "Polygon" in t else LINE if "Line" in t else POINT if "Point" in t else "other")
    if (other != kind).any():
        notes.append(f"Mixed geometry types; kept only the {kind} features ({int((other == kind).sum())} of {len(gdf)}).")

    bad = ~gdf.geometry.is_valid
    if bad.any():
        gdf.loc[bad, "geometry"] = shapely.make_valid(gdf.loc[bad, "geometry"].values)
        notes.append(f"Repaired {int(bad.sum())} invalid geometries.")
    gdf["geometry"] = gdf.geometry.map(lambda g: _only(g, kind))
    gdf = gdf[gdf.geometry.notna()].copy()
    if gdf.empty:
        raise DataError("No usable geometry remained after cleaning.")

    # projection
    if target_crs in (None, "auto"):
        if gdf.crs.is_geographic:
            ll = gdf if gdf.crs.equals(CRS.from_epsg(4326)) else gdf.to_crs(4326)
            crs, desc = auto_crs(ll)
            gdf = gdf.to_crs(crs)
        else:
            crs = gdf.crs
            desc = crs.name
    else:
        try:
            crs = CRS.from_user_input(target_crs)
        except Exception as exc:
            raise DataError(f"'{target_crs}' is not a recognised coordinate system.") from exc
        gdf = gdf.to_crs(crs)
        desc = crs.name
    if not np.isfinite(gdf.total_bounds).all():
        raise DataError("Projection produced invalid coordinates. Try a different CRS.")

    nverts = int(shapely.get_num_coordinates(gdf.geometry.values).sum())
    if kind != POINT and nverts > simplify_vertices:
        minx, miny, maxx, maxy = gdf.total_bounds
        tol = max(maxx - minx, maxy - miny) / 12000
        gdf["geometry"] = gdf.geometry.simplify(tol, preserve_topology=True)
        notes.append(f"Large layer ({nverts:,} vertices): lightly simplified for drawing, no visible change at map scale.")
    return gdf, kind, desc, notes


def meters_per_unit(crs: CRS, lat0: float = 0.0) -> float | None:
    """Metres represented by one CRS unit; None if unknown."""
    if crs.is_geographic:
        return 111_320.0 * math.cos(math.radians(lat0))
    try:
        return float(crs.axis_info[0].unit_conversion_factor)
    except Exception:
        return None
