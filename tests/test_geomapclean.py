import io
import json
import os
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import shapely
from shapely.geometry import GeometryCollection, MultiPolygon, Point, Polygon, box

from moseslukegeomap import DataError, MapOptions, Source, guess_label_column, join_table, make_map, prepare
from moseslukegeomap.cli import main as cli_main
from moseslukegeomap.io import describe, filter_rows
from moseslukegeomap.labels import clean_text, wrap_two_lines
from moseslukegeomap.render import _classify
from moseslukegeomap.workflow import demo_layer, run


# --------------------------------------------------------------------------- helpers
def overlapping_text_pairs(result):
    """Pairs of drawn texts whose boxes overlap (the guarantee the labeller makes)."""
    fig = result.fig
    ax = fig.axes[0]
    r = fig.canvas.get_renderer()
    boxes = [(t.get_text(), t.get_window_extent(r)) for t in ax.texts if t.get_text().strip()]
    bad = []
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i][1], boxes[j][1]
            if a.x0 < b.x1 - 0.5 and a.x1 > b.x0 + 0.5 and a.y0 < b.y1 - 0.5 and a.y1 > b.y0 + 0.5:
                bad.append((boxes[i][0], boxes[j][0]))
    return bad


def write_shp_zip(gdf, tmp_path, name="layer"):
    d = tmp_path / "shp"
    d.mkdir(exist_ok=True)
    gdf.to_file(d / f"{name}.shp")
    z = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for p in d.iterdir():
            zf.write(p, p.name)
    return z


# ------------------------------------------------------------------------------- io
def test_zip_shapefile_roundtrip(grid, tmp_path):
    z = write_shp_zip(grid, tmp_path)
    with Source(z) as src:
        assert src.members == ["layer.shp"]
        g = src.read()
    assert len(g) == 16 and g.crs.to_epsg() == 4326 and "ZONE_NAME" in g.columns


def test_bytes_parts_and_zip_bytes(grid, tmp_path):
    z = write_shp_zip(grid, tmp_path)
    with Source(z.read_bytes(), name="up.zip") as src:
        assert len(src.read()) == 16
    d = tmp_path / "shp"
    parts = [(p.name, p.read_bytes()) for p in d.iterdir()]
    with Source(parts) as src:
        assert len(src.read()) == 16


def test_geojson_and_folder(grid, tmp_path):
    p = tmp_path / "a.geojson"
    grid.to_file(p, driver="GeoJSON")
    with Source(p) as src:
        assert len(src.read()) == 16
    with Source(tmp_path) as src:
        assert "a.geojson" in src.members


def test_multiple_layers_need_a_choice(grid, tmp_path):
    grid.to_file(tmp_path / "a.geojson", driver="GeoJSON")
    grid.head(3).to_file(tmp_path / "b.geojson", driver="GeoJSON")
    with Source(tmp_path) as src:
        with pytest.raises(DataError, match="several layers"):
            src.read()
        assert len(src.read("b.geojson")) == 3
        assert src.info("a.geojson")["features"] == 16


def test_layer_name_without_folder_prefix(grid, tmp_path):
    (tmp_path / "inner").mkdir()
    grid.to_file(tmp_path / "inner" / "a.geojson", driver="GeoJSON")
    grid.head(2).to_file(tmp_path / "inner" / "b.geojson", driver="GeoJSON")
    with Source(tmp_path) as src:
        assert len(src.read("b.geojson")) == 2
        with pytest.raises(DataError, match="Available"):
            src.read("c.geojson")


def test_geopackage_layers(grid, tmp_path):
    p = tmp_path / "multi.gpkg"
    grid.to_file(p, layer="zones", driver="GPKG")
    grid.head(4).to_file(p, layer="few", driver="GPKG")
    with Source(p) as src:
        assert sorted(src.members) == ["multi.gpkg::few", "multi.gpkg::zones"]
        assert len(src.read("multi.gpkg::few")) == 4


def test_zip_slip_is_refused(tmp_path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../escape.txt", "x")
    with pytest.raises(DataError, match="unsafe"):
        Source(z)
    assert not (tmp_path.parent / "escape.txt").exists()


def test_bad_inputs_give_friendly_errors(tmp_path):
    with pytest.raises(DataError, match="not found"):
        Source(tmp_path / "nope.zip")
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"PK\x03\x04garbage")
    with pytest.raises(DataError):
        Source(bad.read_bytes(), name="bad.zip")
    (tmp_path / "empty").mkdir()
    with Source(tmp_path / "empty") as src:
        with pytest.raises(DataError, match="No map data"):
            src.read()


def test_shapefile_without_dbf_is_explained(grid, tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    grid.to_file(d / "x.shp")
    (d / "x.dbf").unlink()
    with Source(d) as src:
        with pytest.raises(DataError, match="dbf"):
            src.read()


def test_shapefile_without_cpg_still_reads(tmp_path):
    g = gpd.GeoDataFrame({"name": ["Ibadan", "Yaoundé"], "geometry": [box(0, 0, 1, 1), box(1, 0, 2, 1)]}, crs=4326)
    d = tmp_path / "l"
    d.mkdir()
    g.to_file(d / "x.shp", encoding="utf-8")
    (d / "x.cpg").unlink(missing_ok=True)
    with Source(d) as src:
        out = src.read()
    assert len(out) == 2


# ------------------------------------------------------------------------------ clean
def test_invalid_and_empty_geometry_repaired(grid):
    bow = Polygon([(8, 6), (9, 7), (9, 6), (8, 7)])  # self-intersecting bow-tie
    g = grid.copy()
    g.loc[0, "geometry"] = bow
    g.loc[1, "geometry"] = None
    g.loc[2, "geometry"] = Polygon()
    out, kind, desc, notes = prepare(g)
    assert kind == "polygon" and len(out) == 14
    assert out.geometry.is_valid.all()
    assert any("Repaired" in n for n in notes)


def test_geometry_collection_reduced_to_polygons(grid):
    g = grid.copy()
    g.loc[0, "geometry"] = GeometryCollection([box(0, 0, 1, 1), Point(5, 5)])
    out, kind, _, _ = prepare(g)
    assert out.geometry.geom_type.isin(["Polygon", "MultiPolygon"]).all()


def test_z_coordinates_dropped(grid):
    g = grid.copy()
    g["geometry"] = shapely.force_3d(g.geometry.values, 5.0)
    out, *_ = prepare(g)
    assert not out.geometry.has_z.any()


def test_missing_crs_assumed_wgs84_or_rejected(grid):
    g = grid.set_crs(None, allow_override=True)
    out, _, _, notes = prepare(g)
    assert any("WGS 84" in n for n in notes) and out.crs is not None
    proj = grid.to_crs(32632).set_crs(None, allow_override=True)
    with pytest.raises(DataError, match="no coordinate system"):
        prepare(proj)
    out, *_ = prepare(proj, assume_crs=32632)
    assert out.crs.to_epsg() == 32632


def test_projection_choice_by_extent(grid):
    small, _, desc, _ = prepare(grid)
    assert "UTM" in desc and small.crs.is_projected
    wide = gpd.GeoDataFrame({"n": ["a", "b"], "geometry": [box(-10, -5, 0, 5), box(20, 0, 30, 10)]}, crs=4326)
    _, _, desc, _ = prepare(wide)
    assert "Lambert Azimuthal" in desc
    world = gpd.GeoDataFrame({"n": ["w"], "geometry": [box(-179, -80, 179, 80)]}, crs=4326)
    _, _, desc, _ = prepare(world)
    assert "Equal Earth" in desc


def test_antimeridian_data_stays_contiguous():
    east = box(177, -18, 180, -16)
    west = box(-180, -18, -178, -16)
    g = gpd.GeoDataFrame({"n": ["east", "west"], "geometry": [east, west]}, crs=4326)
    out, *_ = prepare(g)
    minx, _, maxx, _ = out.total_bounds
    assert (maxx - minx) < 1_000_000  # ~ 500 km, not a world-wide smear


def test_user_crs_and_bad_crs(grid):
    out, *_ = prepare(grid, 32632)
    assert out.crs.to_epsg() == 32632
    with pytest.raises(DataError, match="not a recognised"):
        prepare(grid, "EPSG:not-a-thing")


def test_mixed_geometry_keeps_dominant(grid):
    g = pd.concat([grid, gpd.GeoDataFrame({"ZONE_NAME": ["pt"], "geometry": [Point(8, 6)]}, crs=4326)], ignore_index=True)
    _, kind, _, notes = prepare(g)
    assert kind == "polygon" and any("Mixed" in n for n in notes)


# ----------------------------------------------------------------------------- labels
def test_guess_label_column_prefers_names():
    g = gpd.GeoDataFrame({
        "Shape_Area": np.arange(6.0), "ADM1_PCODE": [f"NG{i}" for i in range(6)],
        "ADM1_EN": ["A", "A", "B", "B", "C", "C"],
        "ADM2_EN": list("pqrstu"), "ADM2_PCODE": [f"NG0{i}" for i in range(6)],
        "date": ["2019"] * 6, "geometry": [Point(i, i) for i in range(6)]}, crs=4326)
    assert guess_label_column(g) == "ADM2_EN"
    assert guess_label_column(g.drop(columns=["ADM2_EN"])) == "ADM1_EN"
    g2 = gpd.GeoDataFrame({"a": [1, 2], "geometry": [Point(0, 0), Point(1, 1)]}, crs=4326)
    assert guess_label_column(g2) is None


def test_clean_text_and_wrap():
    assert clean_text(None) == "" and clean_text(float("nan")) == "" and clean_text("  Ife   North ") == "Ife North"
    assert clean_text(12.0) == "12" and clean_text("nan") == ""
    assert clean_text("ABUJA MUNICIPAL", "title") == "Abuja Municipal"
    assert clean_text("ife north", "upper") == "IFE NORTH"
    assert wrap_two_lines("Federal Capital Territory") in ("Federal Capital\nTerritory", "Federal\nCapital Territory")
    assert wrap_two_lines("Kano") is None


# ------------------------------------------------------------------------ classification
def test_classification_methods_cover_all_values(grid):
    s = grid["value"]
    for mode in ("quantiles", "equal", "jenks"):
        cls, labels, bins = _classify(s, MapOptions(classification=mode, k=5, color_by="value"))
        assert (cls >= 0).all() and cls.max() == len(bins) - 1 and len(labels) == len(bins)
    cls, labels, bins = _classify(s, MapOptions(classification="manual", bins=[10, 30], color_by="value"))
    assert bins[-1] >= s.max() and len(labels) == 3


def test_integer_labels_do_not_overlap(grid):
    _, labels, _ = _classify(grid["count"].astype(float), MapOptions(classification="equal", k=4, color_by="count"))
    assert all("." not in l for l in labels)  # whole numbers stay whole


def test_few_distinct_values_reduce_classes():
    s = pd.Series([1.0, 1.0, 2.0, 2.0, 2.0])
    _, labels, bins = _classify(s, MapOptions(classification="quantiles", k=7, color_by="v"))
    assert len(bins) <= 2


# ------------------------------------------------------------------------------ render
def test_overlap_checker_actually_detects_overlap(grid):
    r = make_map(grid, labels=False, north_arrow=False, scale_bar=False)
    ax = r.fig.axes[0]
    x, y = np.mean(ax.get_xlim()), np.mean(ax.get_ylim())
    ax.text(x, y, "Collision one", fontsize=10)
    ax.text(x, y, "Collision two", fontsize=10)
    assert overlapping_text_pairs(r) == [("Collision one", "Collision two")]


def test_plain_map_labels_every_unit_without_collisions(grid):
    r = make_map(grid, title="Test", source="Synthetic")
    assert r.report["labels"]["column"] == "ZONE_NAME" and r.report["labels"]["skipped"] == 0
    assert overlapping_text_pairs(r) == []


@pytest.mark.parametrize("mode", ["quantiles", "equal", "jenks", "categorical"])
def test_choropleth_modes(grid, mode):
    col = "kind" if mode == "categorical" else "value"
    r = make_map(grid, color_by=col, classification=mode, k=4, legend_title="X")
    assert r.report["colour"]["mode"] == mode
    assert overlapping_text_pairs(r) == []


def test_missing_values_get_a_legend_entry(grid):
    g = grid.copy()
    g.loc[:2, "value"] = np.nan
    r = make_map(g, color_by="value")
    assert r.report["colour"]["missing"] == 3
    legend = r.fig.axes[0].get_legend()
    assert legend is not None and "No data" in [t.get_text() for t in legend.get_texts()]


def test_numbers_stored_as_text_are_understood(grid):
    g = grid.copy()
    g["pct"] = [f"{v:,.1f}%" for v in g["value"]]
    r = make_map(g, color_by="pct", classification="quantiles")
    assert r.report["colour"]["mode"] == "quantiles"


def test_points_lines_and_overlay(grid, points, lines):
    rp = make_map(points, title="Clinics")
    assert rp.report["geometry"] == "point" and overlapping_text_pairs(rp) == []
    rl = make_map(lines)
    assert rl.report["geometry"] == "line" and rl.report["labels"]["placed"] >= 1
    ro = make_map(grid, overlay=grid.dissolve().reset_index(drop=True))
    assert ro.report["features"] == 16


def test_overlay_must_be_polygons(grid, points):
    with pytest.raises(DataError, match="overlay"):
        make_map(grid, overlay=points)


@pytest.mark.parametrize("theme", ["light", "paper", "dark"])
def test_themes_render(grid, theme):
    assert make_map(grid, theme=theme, color_by="value").fig is not None


def test_outputs_are_valid_files(grid, tmp_path):
    r = make_map(grid, title="T", color_by="value")
    png = r.save(tmp_path / "m.png", dpi=80)
    assert Path(png).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    r.save(tmp_path / "m.svg")
    import xml.dom.minidom as md
    md.parse(str(tmp_path / "m.svg"))
    r.save(tmp_path / "m.pdf")
    assert (tmp_path / "m.pdf").read_bytes()[:5] == b"%PDF-"
    with pytest.raises(DataError, match="Unsupported"):
        r.save(tmp_path / "m.exe")


def test_layout_is_resolution_independent(grid):
    r = make_map(grid, color_by="value")
    a = r.to_bytes("png", 60)
    b = r.to_bytes("png", 200)
    assert len(b) > len(a)  # same figure, just more pixels


def test_error_messages(grid):
    with pytest.raises(DataError, match="Unknown theme"):
        make_map(grid, theme="neon")
    with pytest.raises(DataError, match="not found"):
        make_map(grid, color_by="nope")
    with pytest.raises(DataError, match="not found"):
        make_map(grid, label_column="nope")
    with pytest.raises(DataError, match="not numeric"):
        make_map(grid, color_by="ZONE_NAME", classification="quantiles")
    with pytest.raises(DataError, match="Unknown colour map"):
        make_map(grid, color_by="value", cmap="notacmap")
    with pytest.raises(DataError, match="too many"):
        g = grid.copy()
        g["uniq"] = [f"c{i}" for i in range(16)] * 1
        big = pd.concat([g] * 2).assign(uniq=[f"c{i}" for i in range(32)])
        make_map(gpd.GeoDataFrame(big, crs=4326), color_by="uniq", classification="categorical")


def test_no_name_column_still_draws(grid):
    g = grid[["value", "geometry"]]
    r = make_map(g, color_by="value")
    assert r.report["labels"]["column"] is None and any("No obvious name" in n for n in r.report["notes"])


def test_dense_map_never_overlaps_and_reports_skips():
    rows = []
    for i in range(30):
        for j in range(30):
            rows.append({"NAME": f"Unit number {i}-{j}", "geometry": box(i, j, i + 1, j + 1)})
    g = gpd.GeoDataFrame(rows, crs=3857)
    r = make_map(g, graticule=False)
    assert r.report["labels"]["skipped"] > 0
    assert overlapping_text_pairs(r) == []


def test_leader_labels_for_tiny_units(grid):
    tiny = gpd.GeoDataFrame(
        {"NAME": ["Big", "Small One", "Small Two"],
         "geometry": [box(0, 0, 10, 10), box(10, 4.8, 10.15, 5.0), box(10, 5.1, 10.15, 5.3)]}, crs=3857)
    r = make_map(tiny, graticule=False, label_overflow=True)
    assert r.report["labels"]["skipped"] == 0 and overlapping_text_pairs(r) == []
    off = make_map(tiny, graticule=False, label_overflow=False)
    assert off.report["labels"]["skipped"] == 2


def test_furniture_can_be_switched_off(grid):
    r = make_map(grid, north_arrow=False, scale_bar=False, graticule=False, legend=False, labels=False, crs_note=False)
    assert r.report["labels"]["placed"] == 0


def test_single_feature_and_tiny_extent():
    g = gpd.GeoDataFrame({"name": ["Only"], "geometry": [box(3, 6, 3.0005, 6.0005)]}, crs=4326)
    assert make_map(g).fig is not None
    p = gpd.GeoDataFrame({"name": ["Pt"], "geometry": [Point(3, 6)]}, crs=4326)
    assert make_map(p).fig is not None


def test_geographic_output_crs_still_draws(grid):
    r = make_map(grid, crs=4326)
    assert r.fig is not None  # geographic output still draws


def test_large_layer_is_simplified():
    ang = np.linspace(0, 2 * np.pi, 700_000)
    ring = Polygon(np.c_[np.cos(ang), np.sin(ang)] * 100)
    g = gpd.GeoDataFrame({"name": ["Big"], "geometry": [ring]}, crs=3857)
    _, _, _, notes = prepare(g)
    assert any("simplified" in n for n in notes)


# -------------------------------------------------------------------------------- join
def test_join_matches_loosely_and_reports_gaps(grid):
    table = pd.DataFrame({"area": ["zone a1", " ZONE A2 ", "Zone A3", "Zone B9", "Zoen A4"], "rate": [1, 2, 3, 4, 5]})
    out, rep = join_table(grid, table, "ZONE_NAME", "area")
    assert rep["matched"] == 3
    assert out.loc[out.ZONE_NAME == "Zone A2", "rate"].iloc[0] == 2
    assert "Zone B9" in rep["unmatched_in_table"]
    assert rep["suggestions"].get("Zoen A4") == "Zone A4"
    assert len(rep["unmatched_in_map"]) == 13
    assert isinstance(out, gpd.GeoDataFrame)


def test_join_errors(grid):
    with pytest.raises(DataError, match="repeated keys"):
        join_table(grid, pd.DataFrame({"area": ["Zone A1", "zone a1"], "v": [1, 2]}), "ZONE_NAME", "area")
    with pytest.raises(DataError, match="not found"):
        join_table(grid, pd.DataFrame({"area": ["x"], "v": [1]}), "NOPE", "area")


def test_join_column_name_clash_is_renamed(grid):
    table = pd.DataFrame({"area": ["Zone A1"], "value": [999.0]})
    out, rep = join_table(grid, table, "ZONE_NAME", "area")
    assert "value_table" in out.columns and out.loc[0, "value"] != 999.0


def test_filter_rows(grid):
    assert len(filter_rows(grid, "kind", ["URBAN"])) == len(grid[grid.kind == "urban"])
    with pytest.raises(DataError):
        filter_rows(grid, "kind", ["nothing"])


def test_describe(grid):
    d = describe(grid)
    assert d["features"] == 16 and d["crs"].endswith("4326")


# ------------------------------------------------------------------------- workflow/CLI
def test_demo_layer_and_run():
    res, _ = run(demo_layer(), MapOptions(color_by="coverage_pct"), where=("region", ["North"]))
    assert 0 < res.report["features"] < 16 and "filter" in res.report


def test_run_join_requires_keys(grid):
    with pytest.raises(DataError, match="key columns"):
        run(grid, table=pd.DataFrame({"a": [1]}))


def test_cli_demo_inspect_draw(grid, tmp_path, capsys):
    out = tmp_path / "d.png"
    assert cli_main(["demo", "-o", str(out)]) == 0 and out.exists()
    z = write_shp_zip(grid, tmp_path)
    assert cli_main(["inspect", str(z)]) == 0
    out2 = tmp_path / "m.pdf"
    assert cli_main(["draw", str(z), "-o", str(out2), "--color-by", "value", "-k", "4", "--where", "kind=urban,rural",
                     "--title", "CLI", "--dpi", "80"]) == 0
    assert out2.exists()
    csv = tmp_path / "t.csv"
    pd.DataFrame({"z": grid["ZONE_NAME"], "v": range(16)}).to_csv(csv, index=False)
    out3 = tmp_path / "j.png"
    assert cli_main(["draw", str(z), "-o", str(out3), "--join", str(csv), "--join-key", "ZONE_NAME=z", "--color-by", "v", "--dpi", "60"]) == 0
    capsys.readouterr()
    assert cli_main(["draw", str(tmp_path / "missing.zip")]) == 1
    assert "Error" in capsys.readouterr().err
    assert cli_main(["draw", str(z), "--join", str(csv)]) == 1  # no key given


def test_app_smoke():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(Path(__file__).parents[1] / "src/moseslukegeomap/app.py"), default_timeout=180)
    at.run()
    assert not at.exception
    assert at.title[0].value == "MosesLukeGISmapdeveloper"
    assert [tb.label for tb in at.tabs] == ["Make a map", "How to use it", "How it works", "About"]
    at.toggle[0].set_value(True).run()
    assert not at.exception and not at.error
    for sb in at.selectbox:
        if sb.label == "Colour areas by":
            sb.set_value("coverage_pct")
    at.run()
    assert not at.exception and not at.error
