"""Command line: geomapclean inspect | draw | demo | app."""
from __future__ import annotations

import argparse
import sys
from dataclasses import fields

from . import __version__
from .io import DataError, Source, describe, read_table
from .render import THEMES, MapOptions
from .workflow import demo_layer, run


def _csv_list(s):
    return [x.strip() for x in s.split(",") if x.strip()]


def build_parser():
    p = argparse.ArgumentParser(prog="geomapclean", description="Clean, labelled maps from raw GIS files.")
    p.add_argument("--version", action="version", version=f"geomapclean {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("inspect", help="List the layers and columns in a file")
    i.add_argument("input")

    d = sub.add_parser("draw", help="Draw a map")
    d.add_argument("input", help="zip, folder, .shp, .geojson, .gpkg or .kml")
    d.add_argument("-o", "--output", default="map.png", help="png, svg, pdf, jpg or tif (default map.png)")
    d.add_argument("--layer", help="Layer to draw when the file has several (see 'inspect')")
    d.add_argument("--overlay-layer", help="Polygon layer drawn as bold outlines on top (e.g. states over LGAs)")
    d.add_argument("--where", help="Keep only some rows, e.g. ADM1_EN=Lagos or ADM1_EN=Lagos,Ogun")
    d.add_argument("--join", help="CSV/Excel to attach to the map")
    d.add_argument("--join-key", help="MAPCOL=TABLECOL, e.g. ADM2_EN=lga")
    d.add_argument("--title"); d.add_argument("--subtitle"); d.add_argument("--source")
    d.add_argument("--label-column", default="auto")
    d.add_argument("--label-case", choices=["as-is", "title", "upper"], default="as-is")
    d.add_argument("--overflow", dest="overflow", action="store_true", default=None, help="Label small units with leader lines")
    d.add_argument("--no-overflow", dest="overflow", action="store_false")
    d.add_argument("--label-max", type=int)
    d.add_argument("--color-by")
    d.add_argument("--classification", default="auto", choices=["auto", "quantiles", "equal", "jenks", "manual", "categorical"])
    d.add_argument("-k", type=int, default=5, help="Number of classes")
    d.add_argument("--bins", type=lambda s: [float(x) for x in _csv_list(s)], help="Manual break values, e.g. 10,20,50")
    d.add_argument("--cmap"); d.add_argument("--reverse-cmap", action="store_true")
    d.add_argument("--legend-title"); d.add_argument("--legend-loc", default="auto", choices=["auto", "upper left", "upper right", "lower left", "lower right"])
    d.add_argument("--decimals", type=int)
    d.add_argument("--theme", default="light", choices=list(THEMES))
    d.add_argument("--font")
    d.add_argument("--width", type=float, default=10.0, help="Figure width in inches")
    d.add_argument("--dpi", type=int, default=300)
    d.add_argument("--crs", default="auto", help="'auto' or an EPSG code such as 32632")
    d.add_argument("--assume-crs", help="CRS to assume when the file has none")
    for flag in ("labels", "legend", "scale-bar", "north-arrow", "graticule", "frame", "crs-note"):
        d.add_argument(f"--no-{flag}", dest=flag.replace("-", "_"), action="store_false", default=True)
    d.add_argument("--credit", action="store_true", help="Add a small 'made with' line")

    m = sub.add_parser("demo", help="Draw the built-in demo map")
    m.add_argument("-o", "--output", default="demo_map.png")
    sub.add_parser("app", help="Launch the upload web app (needs: pip install 'moseslukegeomap[app]')")
    return p


def _options(a) -> MapOptions:
    return MapOptions(
        title=a.title, subtitle=a.subtitle, source=a.source, credit=a.credit, labels=a.labels,
        label_column=a.label_column, label_case=a.label_case, label_overflow=a.overflow, label_max=a.label_max,
        color_by=a.color_by, classification=a.classification, k=a.k, bins=a.bins, cmap=a.cmap,
        reverse_cmap=a.reverse_cmap, legend=a.legend, legend_title=a.legend_title, legend_loc=a.legend_loc,
        decimals=a.decimals, north_arrow=a.north_arrow, scale_bar=a.scale_bar, graticule=a.graticule,
        frame=a.frame, crs_note=a.crs_note, theme=a.theme, font=a.font, width_in=a.width, crs=a.crs,
        assume_crs=a.assume_crs,
    )


def _print_report(rep: dict):
    for n in rep.get("notes", []):
        print(f"  note: {n}")
    if "filter" in rep:
        print(f"  filter: {rep['filter']}")
    j = rep.get("join")
    if j:
        print(f"  join: {j['matched']} of {j['map_features']} map areas matched the table")
        if j["unmatched_in_map"]:
            print(f"    map areas with no table row ({len(j['unmatched_in_map'])}): {', '.join(j['unmatched_in_map'][:10])}")
        for t, m in j["suggestions"].items():
            print(f"    did you mean: '{t}' -> '{m}'?")
        extra = [t for t in j["unmatched_in_table"] if t not in j["suggestions"]]
        if extra:
            print(f"    table rows with no map area ({len(extra)}): {', '.join(map(str, extra[:10]))}")
    lab = rep.get("labels", {})
    if lab.get("column"):
        print(f"  labels: {lab['placed']} placed from '{lab['column']}', {lab['skipped']} too small to fit at this scale")
        if lab["skipped"]:
            print("    tip: map one region at a time with --where, make the figure larger with --width, or add --overflow")
    print(f"  projection: {rep.get('crs')}")


def main(argv=None) -> int:
    parser = build_parser()
    a, extra = parser.parse_known_args(argv)
    if extra and a.cmd != "app":
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    try:
        if a.cmd == "app":
            try:
                from streamlit.web import cli as stcli
            except ImportError:
                print("Install the app extra first: pip install 'moseslukegeomap[app]'", file=sys.stderr)
                return 1
            from importlib import resources

            with resources.as_file(resources.files("moseslukegeomap") / "app.py") as p:
                sys.argv = ["streamlit", "run", str(p), *extra]
                return stcli.main()
        if a.cmd == "demo":
            res, _ = run(demo_layer(), MapOptions(
                title="Null Island: districts by coverage", subtitle="Synthetic demonstration data",
                color_by="coverage_pct", legend_title="Coverage (%)", k=4,
                source="Invented data supplied with geomapclean for demonstration."))
            res.save(a.output, 200)
            print(f"Wrote {a.output}")
            _print_report(res.report)
            return 0
        if a.cmd == "inspect":
            with Source(a.input) as src:
                if not src.members:
                    raise DataError("No map layers found in that file.")
                for m in src.members:
                    info = src.info(m)
                    print(f"{m}\n  {info['features']} features, {info['geometry_type']}, CRS {info['crs']}")
                    print(f"  columns: {', '.join(map(str, info['fields']))}")
            return 0
        # draw
        with Source(a.input) as src:
            layer = src.read(a.layer)
            overlay = src.read(a.overlay_layer) if a.overlay_layer else None
            where = None
            if a.where:
                col, _, vals = a.where.partition("=")
                where = (col.strip(), _csv_list(vals))
            table = join = None
            if a.join:
                table = read_table(a.join)
                if not a.join_key or "=" not in a.join_key:
                    raise DataError("--join needs --join-key MAPCOL=TABLECOL")
                l, _, r = a.join_key.partition("=")
                join = (l.strip(), r.strip())
            res, _ = run(layer, _options(a), where=where, table=table, join=join, overlay=overlay)
            res.save(a.output, a.dpi)
        print(f"Wrote {a.output}")
        _print_report(res.report)
        return 0
    except DataError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
