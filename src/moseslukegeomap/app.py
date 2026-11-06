"""Streamlit web app: upload raw GIS data, get a clean labelled map."""
from __future__ import annotations

import streamlit as st

from moseslukegeomap import __version__
from moseslukegeomap.io import DataError, Source, describe, read_table
from moseslukegeomap.labels import guess_label_column
from moseslukegeomap.render import THEMES, MapOptions
from moseslukegeomap.workflow import demo_layer, run


APP_NAME = "MosesLukeGISmapdeveloper"

HOW_TO_USE = """
### What this tool does

You give it a file of boundaries (for example states, districts or health facilities). It gives you back a finished map: areas coloured by your numbers, names written on the map without overlapping, a legend, a scale bar, a north arrow and grid labels. You can download it as a PNG for slides, an SVG you can edit, or a PDF for a journal.

You do not need GIS software or any coding.

### Before you start: get your file ready

| You have | What to upload |
|---|---|
| A shapefile | Zip the whole set into one `.zip`. It must include `.shp`, `.shx` and `.dbf`. Add `.prj` so the map projection is known. |
| A GeoJSON, GeoPackage or KML | Upload the file as it is. |
| Numbers to show on the map | A CSV or Excel file with one row per area and a column holding the area name. |

If you only want to try the tool, switch on **Use the built-in demo** in the sidebar. It is an invented island, so nothing real is shown.

### Step by step

1. **Upload your file** in the sidebar (section 1). If the file holds several layers, for example states and districts, choose the one to map. You can also pick a second polygon layer to draw as bold outlines on top, such as state borders over district boundaries.
2. **Focus on part of the map** (section 2, optional). Choose a column and one or more values, for example `State = Lagos`. This is the best way to get every name onto a map with many small areas.
3. **Attach your numbers** (section 3, optional). Upload your CSV or Excel file, then say which column in the map and which column in your table hold the area names. The tool matches them even if the capital letters or spaces differ, and tells you which names did not match.
4. **Choose the style** (section 4). Add a title, subtitle and source line. Pick the column to colour by, how to group the values, the colours and what to label with.
5. **Check the report** next to the map. It tells you how many names were placed, which areas could not be named, whether any table rows failed to match, and which projection was used.
6. **Download.** Switch on *Prepare high-resolution files* to get the PNG (300 dpi), SVG and PDF.

### Choosing how to group your numbers

| Method | Use it when | What it does |
|---|---|---|
| Quantiles | You want to compare areas by rank, for example top fifth against bottom fifth | Puts the same number of areas in each group |
| Equal interval | Your values are fairly evenly spread and you want a plain scale | Splits the range of values into equal steps |
| Natural breaks (jenks) | Your values form clusters | Finds the gaps in the data and cuts there |
| Categorical | The column holds categories such as zone or type, not numbers | Gives each category its own colour |
| Manual (command line and Python) | You need set cut-offs such as 50, 80 and 95 per cent | Uses the break values you supply |

Whatever you choose, say how you grouped the data in your figure caption.

### If some names are missing from the map

On a map with hundreds of small areas, not every name can fit without crowding. The tool leaves out the ones that do not fit and reports how many. To name more of them you can:

- focus on one region (section 2),
- make the figure wider with the width slider,
- choose **label with a leader line**, which writes small names beside the area with a thin line pointing to it.

### Common problems

| What you see | Likely cause and fix |
|---|---|
| "No map layer found" | The zip is missing `.shp`, `.shx` or `.dbf`, or the files are inside a password-protected archive. |
| Areas shown as "No data" | Their names did not match your table. Read the join warnings in the report, then correct the spelling in your table. |
| "The file has no coordinate system" | The `.prj` file is missing and the numbers are not longitude and latitude. Add the `.prj`, or ask the person who made the file for the EPSG code. |
| No labels at all | There is no obvious name column. Choose one under **Label with**. |
| The map looks stretched | It should not. If it does, check that the file really covers the area you expect. |
"""

HOW_IT_WORKS = """
### The short version

Every upload goes through the same five stages. Nothing is changed in your original file.

**1. Read.** Zipped shapefiles, GeoJSON, GeoPackage and KML are opened. Zip files are unpacked safely, and files that try to write outside their folder are refused.

**2. Clean.** Empty features are removed. Broken shapes, such as a boundary that crosses itself, are repaired. Height values are dropped because a flat map does not need them. If the file does not state its coordinate system, the tool assumes WGS 84 only when the numbers look like longitude and latitude. Otherwise it stops and asks, because guessing would put your map in the wrong place.

**3. Project.** A map of latitude and longitude drawn flat looks stretched away from the equator. The tool chooses a projection from the size and position of your data. A small area gets a UTM zone. A country or region gets a Lambert azimuthal equal-area projection centred on your data, which keeps areas fair. A world map gets Equal Earth. The projection used is written under the map.

**4. Colour.** Your column is grouped into classes and given a colour scale. Categories use a palette that stays distinguishable for people with the most common forms of colour blindness. Areas without a value get their own "No data" class.

**5. Lay out and label.** This is where most of the care goes.

### How the names are placed

The tool measures the real width and height of every name in the chosen font, then works through the areas from largest to smallest.

- For each area it finds the point furthest from the edges, which is the best centre for a name.
- It tries the largest text size that fits entirely inside the area, down to a minimum readable size. Long names can break onto two lines.
- A name is only placed if it does not touch any name already placed, or the legend, scale bar or north arrow.
- Text turns white with a dark outline on dark fills, and dark with a white outline on light fills.
- Areas too small for a name can be named beside the area with a leader line, if you ask for it. The line is kept away from other text.

This is checked automatically: the test suite confirms that no two pieces of text overlap, and the same check was run on every layer of a national boundary dataset during development.

### Where the legend, scale bar and north arrow go

The tool tests all four corners of the map, measures how much of the map each corner would cover, and uses the emptiest ones. If no corner is clear enough, it adds margin around the map until one is.

### What it cannot do

- It does not draw basemap tiles, satellite images or rasters.
- Scale bar and north arrow are approximate. They are accurate near the centre of the map and drift slightly at the edges.
- It cannot name every area on a very dense map at a normal page size. It tells you how many it left out.
- It draws one thematic layer, with an optional outline layer on top.
- It cannot check whether your boundaries are correct or current. That depends on your source.

### Your data and privacy

Files are read into memory for your session. The app does not save them or send them anywhere. If you run a public copy for others, you are responsible for what is uploaded to it, and sensitive data should not be uploaded to any shared server.
"""

ABOUT = f"""
### {APP_NAME}

Built by **Moses Luke**, an epidemiologist and health information specialist based in Abuja, Nigeria, so that researchers and programme teams can turn boundary files into clear maps without desktop GIS software.

| | |
|---|---|
| Version | {__version__} |
| Python package | `moseslukegeomap` (command: `geomapclean`) |
| Web app | https://moseslukegismap.streamlit.app/ |
| Source code | https://github.com/Moses-L/geomapclean |
| Author | https://github.com/Moses-L |
| ORCID | https://orcid.org/0000-0002-0600-6660 |
| DOI | https://doi.org/10.5281/zenodo.24010664 |
| Licence | MIT: free to use, copy and adapt, with the copyright notice kept |

### Using maps from this tool in your work

- Credit the source of your boundary data in the source line. Boundary files have their own licences.
- You may mention the tool in your methods, for example: "Maps were produced with {APP_NAME} (geomapclean {__version__}, Luke M.)."
- Cite the software so readers can find it. APA 7th edition: Luke, M. (2026). *geomapclean: Clean, labelled maps from raw GIS files* (Version {__version__}) [Computer software]. Zenodo. https://doi.org/10.5281/zenodo.24010664
- The README on GitHub also gives a BibTeX entry and a step-by-step guide to citing.
- Say in your figure caption how you grouped the values, because that decides what the colours mean.
- Report problems or suggest improvements on the GitHub Issues page.

### Built on

GeoPandas, Shapely, PyProj, Matplotlib, mapclassify and Streamlit, all open-source.
"""

UPLOAD_TYPES = ["zip", "shp", "shx", "dbf", "prj", "cpg", "geojson", "json", "gpkg", "kml"]
CMAPS = ["YlOrRd", "Blues", "Greens", "Purples", "Oranges", "YlGnBu", "viridis", "magma", "RdBu", "RdYlGn", "Spectral"]


@st.cache_data(show_spinner="Reading your file...")
def _members(parts):
    with Source(list(parts)) as src:
        return {m: src.info(m) for m in src.members}


@st.cache_data(show_spinner="Loading the layer...")
def _read(parts, member):
    with Source(list(parts)) as src:
        return src.read(member)


def _fmt_member(infos):
    def f(m):
        i = infos[m]
        return f"{m}  ({i['features']} features, {i['geometry_type']})"
    return f


def main():
    st.set_page_config(page_title=APP_NAME, page_icon="🗺️", layout="wide")
    st.title(APP_NAME)
    st.markdown("##### Clean, labelled, publication-ready maps from raw GIS data")
    st.caption(
        f"Version {__version__}. Upload a zipped shapefile, GeoJSON, GeoPackage or KML. "
        "Files are processed in memory for your session only and are not stored."
    )
    tab_map, tab_how, tab_works, tab_about = st.tabs(["Make a map", "How to use it", "How it works", "About"])
    with tab_how:
        st.markdown(HOW_TO_USE)
    with tab_works:
        st.markdown(HOW_IT_WORKS)
    with tab_about:
        st.markdown(ABOUT)

    layer = overlay = table = None
    infos = {}
    with st.sidebar:
        st.header("1. Your data")
        use_demo = st.toggle("Use the built-in demo instead", value=False)
        up = None if use_demo else st.file_uploader(
            "Zip (recommended), or all the parts of a shapefile together",
            type=UPLOAD_TYPES, accept_multiple_files=True)
        member = overlay_member = None
        parts = None
        if use_demo:
            layer = demo_layer()
        elif up:
            parts = tuple((f.name, f.getvalue()) for f in up)
            try:
                infos = _members(parts)
            except DataError as exc:
                st.error(str(exc))
            if infos:
                member = st.selectbox("Layer to map", list(infos), format_func=_fmt_member(infos))
                polys = [m for m in infos if m != member and "Polygon" in str(infos[m]["geometry_type"])]
                if polys:
                    ov = st.selectbox("Outline layer on top (optional)", ["None"] + polys)
                    overlay_member = None if ov == "None" else ov
                try:
                    layer = _read(parts, member)
                    if overlay_member:
                        overlay = _read(parts, overlay_member)
                except DataError as exc:
                    st.error(str(exc))
            elif not infos:
                st.info("No map layer found yet. A shapefile needs at least .shp, .shx and .dbf (and .prj for the projection).")

    with tab_map:
        if layer is None:
            st.info("Upload a file in the sidebar, or switch on the demo to see what the tool produces.")
            st.markdown(
                "**What you get:** automatic projection, repaired geometry, collision-free labels, "
                "a classed legend placed where it does not cover the map, scale bar, north arrow, "
                "graticule, and PNG, SVG and PDF downloads."
            )
            return

        cols = [c for c in layer.columns if c != layer.geometry.name]
        text_cols = [c for c in cols if layer[c].dtype == object or str(layer[c].dtype).startswith(("str", "string"))]

        with st.sidebar:
            st.header("2. Focus (optional)")
            where = None
            fcol = st.selectbox("Show only rows where", ["(all)"] + [c for c in text_cols if layer[c].nunique() <= 500])
            if fcol != "(all)":
                vals = st.multiselect("is one of", sorted(layer[fcol].dropna().astype(str).unique()))
                if vals:
                    where = (fcol, vals)

            st.header("3. Attach your numbers (optional)")
            tbl_file = st.file_uploader("CSV or Excel with one row per area", type=["csv", "xlsx", "xls"])
            join = None
            if tbl_file:
                try:
                    table = read_table(tbl_file.getvalue(), tbl_file.name)
                    lk = st.selectbox("Map column holding the area names", text_cols or cols)
                    rk = st.selectbox("Table column holding the area names", list(table.columns))
                    join = (lk, rk)
                except DataError as exc:
                    st.error(str(exc))
                    table = None

            st.header("4. Style")
            title = st.text_input("Title", "")
            subtitle = st.text_input("Subtitle", "")
            source = st.text_input("Source line", "")
            all_cols = cols + ([c for c in table.columns if c not in cols] if table is not None else [])
            color_by = st.selectbox("Colour areas by", ["(none)"] + all_cols)
            color_by = None if color_by == "(none)" else color_by
            c1, c2 = st.columns(2)
            classification = c1.selectbox("Method", ["auto", "quantiles", "equal", "jenks", "categorical"])
            k = c2.slider("Classes", 2, 9, 5)
            cmap = st.selectbox("Colours", CMAPS)
            legend_title = st.text_input("Legend title", color_by or "")
            guess = guess_label_column(layer)
            label_options = ["(auto)", "(no labels)"] + cols
            label_choice = st.selectbox("Label with", label_options, help=f"Auto picks: {guess}")
            label_case = st.selectbox("Label case", ["as-is", "title", "upper"])
            overflow = st.selectbox("Small areas", ["automatic", "label with a leader line", "leave unlabelled"])
            theme = st.selectbox("Theme", list(THEMES))
            width = st.slider("Figure width (inches)", 6, 30, 10, help="Wider figures fit more labels.")
            furniture = st.multiselect("Show", ["Legend", "Scale bar", "North arrow", "Graticule"],
                                       default=["Legend", "Scale bar", "North arrow", "Graticule"])

        opts = MapOptions(
            title=title or None, subtitle=subtitle or None, source=source or None,
            labels=label_choice != "(no labels)",
            label_column="auto" if label_choice in ("(auto)", "(no labels)") else label_choice,
            label_case=label_case,
            label_overflow={"automatic": None, "label with a leader line": True, "leave unlabelled": False}[overflow],
            color_by=color_by, classification=classification, k=k, cmap=cmap,
            legend_title=legend_title, theme=theme, width_in=float(width),
            legend="Legend" in furniture, scale_bar="Scale bar" in furniture,
            north_arrow="North arrow" in furniture, graticule="Graticule" in furniture,
        )
        try:
            res, extra = run(layer, opts, where=where, table=table, join=join, overlay=overlay)
        except DataError as exc:
            st.error(str(exc))
            return
        except Exception as exc:  # keep the app alive on unexpected input
            st.error(f"Something went wrong while drawing the map: {exc}")
            return

        rep = res.report
        left, right = st.columns([3, 1])
        with left:
            png = res.to_bytes("png", 110)
            try:
                st.image(png, width="stretch")
            except TypeError:  # older Streamlit
                st.image(png, use_container_width=True)
        with right:
            st.subheader("Report")
            st.write(f"**Features:** {rep['features']} ({rep['geometry']})")
            st.write(f"**Projection:** {rep['crs']}")
            lab = rep["labels"]
            if lab.get("column"):
                st.write(f"**Labels:** {lab['placed']} placed from `{lab['column']}`")
                if lab["skipped"]:
                    st.warning(
                        f"{lab['skipped']} areas are too small to label at this size. Focus on one region, "
                        "widen the figure, or allow leader lines."
                    )
            for n in rep["notes"]:
                st.info(n)
            j = rep.get("join")
            if j:
                st.write(f"**Join:** {j['matched']} of {j['map_features']} areas matched")
                if j["unmatched_in_map"]:
                    st.warning("Areas with no table row: " + ", ".join(j["unmatched_in_map"][:15]))
                for t, m in j["suggestions"].items():
                    st.warning(f"Did you mean '{t}' = '{m}'?")
                rest = [t for t in j["unmatched_in_table"] if t not in j["suggestions"]]
                if rest:
                    st.warning("Table rows with no map area: " + ", ".join(map(str, rest[:15])))
            if rep["colour"].get("missing"):
                st.warning(f"{rep['colour']['missing']} areas have no value and are shown as '{opts.nodata_label}'.")

        st.divider()
        st.subheader("Download")
        if st.toggle("Prepare high-resolution files (PNG 300 dpi, SVG, PDF)"):
            d1, d2, d3 = st.columns(3)
            d1.download_button("PNG, 300 dpi", res.to_bytes("png", 300), "map.png", "image/png")
            d2.download_button("SVG (editable)", res.to_bytes("svg"), "map.svg", "image/svg+xml")
            d3.download_button("PDF", res.to_bytes("pdf"), "map.pdf", "application/pdf")
        with st.expander("Data preview"):
            st.json(describe(layer)["columns"][:30])
        st.caption(f"{APP_NAME}. Boundary data remain the property of their publishers, so cite the source of your data on the map.")


if __name__ == "__main__":
    main()
