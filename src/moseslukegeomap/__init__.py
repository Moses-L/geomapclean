"""geomapclean: clean, labelled, publication-ready maps from raw GIS files."""
from .clean import prepare
from .io import DataError, Source, describe, filter_rows, join_table, read_table
from .labels import guess_label_column
from .render import THEMES, MapOptions, MapResult, make_map

__version__ = "0.1.3"
__all__ = [
    "DataError", "MapOptions", "MapResult", "Source", "THEMES", "describe", "filter_rows",
    "guess_label_column", "join_table", "make_map", "prepare", "read_table",
]
