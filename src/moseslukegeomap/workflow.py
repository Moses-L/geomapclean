"""One shared pipeline used by the CLI, the web app and the Python API."""
from __future__ import annotations

from dataclasses import replace
from importlib import resources

import geopandas as gpd

from .io import DataError, Source, filter_rows, join_table
from .render import MapOptions, MapResult, make_map


def demo_layer() -> gpd.GeoDataFrame:
    """The synthetic 'Null Island' demo layer (invented data, free to use)."""
    with resources.as_file(resources.files("moseslukegeomap") / "data" / "null_island.geojson") as p:
        return gpd.read_file(p)


def run(
    layer: gpd.GeoDataFrame,
    options: MapOptions | None = None,
    *,
    where: tuple[str, list[str]] | None = None,
    table=None,
    join: tuple[str, str] | None = None,
    overlay: gpd.GeoDataFrame | None = None,
    **kw,
) -> tuple[MapResult, dict]:
    """Filter, join and draw. Returns ``(result, extra_report)``."""
    extra: dict = {}
    if where:
        col, vals = where
        before = len(layer)
        layer = filter_rows(layer, col, vals)
        extra["filter"] = f"{col} in {list(vals)}: kept {len(layer)} of {before} features"
    if table is not None:
        if not join:
            raise DataError("To attach a table you also need the key columns (map column, table column).")
        layer, jr = join_table(layer, table, join[0], join[1])
        extra["join"] = jr
    res = make_map(layer, options, overlay=overlay, **kw)
    res.report.update(extra)
    return res, extra


def open_layer(path_or_parts, member: str | None = None, name: str | None = None):
    """Open an upload and read one layer. Returns ``(gdf, source)``; close the source when done."""
    src = Source(path_or_parts, name=name)
    try:
        return src.read(member), src
    except Exception:
        src.close()
        raise
