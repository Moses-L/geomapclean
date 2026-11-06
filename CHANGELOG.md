# Changelog

## 0.1.0

First release.

- Upload loader for zipped shapefiles, folders, loose shapefile parts, GeoJSON, GeoPackage and KML, with safe unzipping.
- Cleaning: empty and invalid geometry repaired, 3D flattened, missing CRS handled, automatic projection (UTM, Lambert Azimuthal Equal Area or Equal Earth).
- Labels: automatic name-column detection, fit-inside placement with no overlaps, two-line wrapping, leader lines for small units, halo and light/dark text by fill colour.
- Colour: quantile, equal interval, natural breaks, manual and categorical classes; colour-blind-safe categorical palette; missing-data class.
- Furniture: legend placed in the emptiest corner, scale bar, north arrow, graticule with edge labels, source and projection notes.
- Table join with forgiving name matching and a report of unmatched names.
- Streamlit app, command line (`geomapclean`) and Python API.
- PNG, SVG, PDF, JPG and TIFF output from one layout.
- App branded MosesLukeGISmapdeveloper, with How to use it, How it works and About tabs; full README walkthrough.
- `--layer` accepts a file name without its folder prefix.
