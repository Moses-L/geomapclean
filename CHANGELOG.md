# Changelog

## 0.1.4

- Guided Google Colab notebook (`notebooks/geomapclean_colab.ipynb`): install, choose data, inspect, attach a table, style, draw, download. No installation needed.
- Animated walkthrough (`examples/walkthrough.gif`) in the README.

## 0.1.3

- Use the Zenodo "all versions" DOI (10.5281/zenodo.24010663) in the README, `CITATION.cff` and the About tab. It always resolves to the latest release. Version 0.1.2 had pointed to the DOI of a single earlier release.
- README explains the difference between the all-versions DOI and a version DOI.

## 0.1.2

- Zenodo DOI (10.5281/zenodo.24010664) added to the README badge, `CITATION.cff` and the About tab.
- README: step-by-step "How to cite" section with APA and BibTeX, a plain-language glossary and a help section.

## 0.1.1

- README images use absolute links so they display on PyPI.
- Link to the public web app (https://moseslukegismap.streamlit.app/) in the README, About tab and package metadata.

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
