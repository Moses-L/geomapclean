"""Reading raw GIS uploads safely: zip, folder, single file, or in-memory bytes."""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd

os.environ.setdefault("SHAPE_RESTORE_SHX", "YES")

VECTOR_EXT = {".shp", ".geojson", ".json", ".gpkg", ".kml", ".fgb"}
MAX_UNZIPPED_BYTES = 3 * 1024**3


class DataError(ValueError):
    """Raised for problems the user can fix (bad file, missing CRS, wrong column)."""


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    infos = zf.infolist()
    if sum(i.file_size for i in infos) > MAX_UNZIPPED_BYTES:
        raise DataError("The archive is too large once unzipped (limit 3 GB).")
    base = dest.resolve()
    for info in infos:
        target = (base / info.filename).resolve()
        if target != base and base not in target.parents:
            raise DataError("The zip file contains an unsafe path and was not opened.")
    zf.extractall(dest)


class Source:
    """A GIS upload. Use as a context manager so temporary files are cleaned up.

    ``data`` may be a path (zip, folder, .shp/.geojson/.gpkg/...), or a list of
    ``(filename, bytes)`` pairs such as the parts of a shapefile.
    """

    def __init__(self, data, name: str | None = None):
        self._tmp = Path(tempfile.mkdtemp(prefix="geomap_"))
        self.root = self._tmp / "src"
        self.root.mkdir()
        try:
            self._ingest(data, name)
        except Exception:
            self.close()
            raise
        self.members = self._discover()

    # ------------------------------------------------------------------ ingest
    def _ingest(self, data, name):
        if isinstance(data, (str, Path)):
            p = Path(data)
            if not p.exists():
                raise DataError(f"File not found: {p}")
            if p.is_dir():
                shutil.copytree(p, self.root, dirs_exist_ok=True)
            elif zipfile.is_zipfile(p):
                with zipfile.ZipFile(p) as zf:
                    _safe_extract(zf, self.root)
            else:
                # copy the file and any shapefile sidecars next to it
                for sib in p.parent.glob(p.stem + ".*"):
                    shutil.copy2(sib, self.root / sib.name)
                if not (self.root / p.name).exists():
                    shutil.copy2(p, self.root / p.name)
            return
        if isinstance(data, (bytes, bytearray)):
            data = [(name or "upload.bin", bytes(data))]
        for fname, blob in data:
            fname = Path(fname).name
            if fname.lower().endswith(".zip") or (blob[:2] == b"PK" and not fname.lower().endswith(tuple(VECTOR_EXT))):
                import io as _io

                try:
                    with zipfile.ZipFile(_io.BytesIO(blob)) as zf:
                        _safe_extract(zf, self.root)
                except zipfile.BadZipFile as exc:
                    raise DataError("That zip file could not be opened. It may be damaged.") from exc
            else:
                (self.root / fname).write_bytes(blob)

    def _discover(self) -> list[str]:
        out: list[str] = []
        for p in sorted(self.root.rglob("*")):
            rel = p.relative_to(self.root)
            if not p.is_file() or "__MACOSX" in rel.parts or p.name.startswith("."):
                continue
            ext = p.suffix.lower()
            if ext not in VECTOR_EXT:
                continue
            if ext == ".gpkg":
                try:
                    import pyogrio

                    layers = [row[0] for row in pyogrio.list_layers(p)]
                except Exception:
                    layers = []
                for lyr in layers or [None]:
                    out.append(f"{rel.as_posix()}::{lyr}" if lyr else rel.as_posix())
            else:
                out.append(rel.as_posix())
        return out

    # --------------------------------------------------------------------- API
    def _split(self, member: str):
        if "::" in member:
            f, lyr = member.split("::", 1)
            return self.root / f, lyr
        return self.root / member, None

    def info(self, member: str) -> dict:
        """Cheap metadata (feature count, geometry type, CRS, fields) without reading everything."""
        import pyogrio

        path, layer = self._split(member)
        try:
            meta = pyogrio.read_info(path, layer=layer)
        except Exception as exc:
            raise DataError(f"Could not inspect {member}: {exc}") from exc
        return {
            "member": member,
            "features": int(meta.get("features", -1)),
            "geometry_type": meta.get("geometry_type"),
            "crs": meta.get("crs"),
            "fields": list(meta.get("fields", [])),
        }

    def read(self, member: str | None = None) -> gpd.GeoDataFrame:
        if not self.members:
            raise DataError(
                "No map data found. Upload a zipped shapefile (.shp, .shx, .dbf, .prj), "
                "a GeoJSON, a GeoPackage or a KML."
            )
        if member is None:
            if len(self.members) > 1:
                raise DataError(
                    "This upload holds several layers. Choose one of: " + "; ".join(self.members)
                )
            member = self.members[0]
        if member not in self.members:
            # forgive a missing folder prefix: "states.shp" matches "data/states.shp"
            hits = [m for m in self.members if m.split("::")[0].split("/")[-1] == member or m.endswith("/" + member)]
            if len(hits) == 1:
                member = hits[0]
            else:
                raise DataError(f"Layer not found: {member}. Available: " + "; ".join(self.members))
        path, layer = self._split(member)
        if path.suffix.lower() == ".shp":
            missing = [e for e in (".shx", ".dbf") if not _has_sidecar(path, e)]
            if ".dbf" in missing:
                raise DataError(
                    f"{path.name} has no .dbf file, so there are no attribute names to use for labels. "
                    "Upload the full shapefile set."
                )
        kw = {"layer": layer} if layer else {}
        try:
            gdf = gpd.read_file(path, engine="pyogrio", **kw)
        except UnicodeDecodeError:
            gdf = gpd.read_file(path, engine="pyogrio", encoding="latin-1", **kw)
        except Exception as exc:
            raise DataError(f"Could not read {member}: {exc}") from exc
        if gdf.empty:
            raise DataError(f"{member} contains no features.")
        return gdf

    def close(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _has_sidecar(shp: Path, ext: str) -> bool:
    return any(p.suffix.lower() == ext for p in shp.parent.glob(shp.stem + ".*"))


def read_table(data, name: str | None = None) -> pd.DataFrame:
    """Read a CSV or Excel table from a path or bytes."""
    import io as _io

    if isinstance(data, (str, Path)):
        name = str(data)
        raw = Path(data).read_bytes()
    else:
        raw = bytes(data)
    lower = (name or "").lower()
    try:
        if lower.endswith((".xlsx", ".xls")):
            return pd.read_excel(_io.BytesIO(raw))
        for enc in ("utf-8-sig", "latin-1"):
            try:
                return pd.read_csv(_io.BytesIO(raw), encoding=enc, sep=None, engine="python")
            except UnicodeDecodeError:
                continue
    except Exception as exc:
        raise DataError(f"Could not read the table: {exc}") from exc
    raise DataError("Could not read the table. Use CSV or Excel.")


def _norm_key(s) -> str:
    s = "" if pd.isna(s) else str(s)
    s = re.sub(r"[‐-―]", "-", s)
    s = re.sub(r"\s+", " ", s.strip().casefold())
    return s


def join_table(gdf: gpd.GeoDataFrame, table: pd.DataFrame, left_on: str, right_on: str):
    """Left-join a table onto the map layer with forgiving key matching.

    Returns ``(joined_gdf, report)``; the report lists keys that failed to match
    on either side, with the closest suggestions, because silent mismatches are
    the usual cause of blank areas on a choropleth.
    """
    import difflib

    for col, frame, label in ((left_on, gdf, "map layer"), (right_on, table, "table")):
        if col not in frame.columns:
            raise DataError(f"Column '{col}' not found in the {label}.")
    t = table.copy()
    t["_k"] = t[right_on].map(_norm_key)
    dup = t["_k"][t["_k"].duplicated(keep=False) & (t["_k"] != "")].unique().tolist()
    if dup:
        raise DataError(
            f"The table has repeated keys in '{right_on}' (for example {dup[:3]}). Each area needs one row."
        )
    g = gdf.copy()
    g["_k"] = g[left_on].map(_norm_key)
    value_cols = [c for c in t.columns if c not in ("_k", right_on)]
    rename = {c: (c if c not in g.columns else f"{c}_table") for c in value_cols}
    t = t.drop(columns=[right_on]).rename(columns=rename)
    joined = g.merge(t, on="_k", how="left")
    joined = gpd.GeoDataFrame(joined, geometry=g.geometry.name, crs=gdf.crs)
    map_keys = set(g["_k"]) - {""}
    tab_keys = set(t["_k"]) - {""}
    unmatched_map = sorted(map_keys - tab_keys)
    unmatched_tab = sorted(tab_keys - map_keys)
    lookup_map = {_norm_key(v): v for v in gdf[left_on].dropna().unique()}
    lookup_tab = {_norm_key(v): v for v in table[right_on].dropna().unique()}
    suggestions = {}
    for k in unmatched_tab:
        close = difflib.get_close_matches(k, list(map_keys), n=1, cutoff=0.75)
        if close:
            suggestions[lookup_tab[k]] = lookup_map[close[0]]
    report = {
        "matched": len(map_keys & tab_keys),
        "map_features": int(len(gdf)),
        "unmatched_in_map": [lookup_map[k] for k in unmatched_map],
        "unmatched_in_table": [lookup_tab[k] for k in unmatched_tab],
        "suggestions": suggestions,
        "rename": rename,
    }
    return joined.drop(columns="_k"), report


def describe(gdf: gpd.GeoDataFrame) -> dict:
    cols = []
    for c in gdf.columns:
        if c == gdf.geometry.name:
            continue
        s = gdf[c]
        ex = s.dropna().iloc[0] if s.notna().any() else None
        cols.append({"name": c, "dtype": str(s.dtype), "unique": int(s.nunique()), "example": ex})
    return {
        "features": int(len(gdf)),
        "geometry_types": sorted(gdf.geom_type.dropna().unique().tolist()),
        "crs": gdf.crs.to_string() if gdf.crs else None,
        "bounds": [float(v) for v in gdf.total_bounds] if len(gdf) else None,
        "columns": cols,
    }


def filter_rows(gdf: gpd.GeoDataFrame, column: str, values) -> gpd.GeoDataFrame:
    if column not in gdf.columns:
        raise DataError(f"Column '{column}' not found.")
    vals = {_norm_key(v) for v in ([values] if isinstance(values, str) else values)}
    out = gdf[gdf[column].map(_norm_key).isin(vals)]
    if out.empty:
        raise DataError(f"No features matched {list(vals)} in '{column}'.")
    return out.copy()
