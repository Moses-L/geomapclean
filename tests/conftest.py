import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import LineString, Point, box


@pytest.fixture
def grid():
    """4 x 4 grid of named zones in lon/lat (WGS 84), with numeric and categorical columns."""
    rows = []
    for i in range(4):
        for j in range(4):
            rows.append({
                "ZONE_NAME": f"Zone {chr(65 + i)}{j + 1}",
                "ZONE_PCODE": f"Z{i}{j}",
                "value": float(i * 4 + j + 1) * 3.7,
                "count": i * 4 + j,
                "kind": ["urban", "rural", "peri"][(i + j) % 3],
                "geometry": box(8 + j * 0.5, 6 + i * 0.5, 8.5 + j * 0.5, 6.5 + i * 0.5),
            })
    return gpd.GeoDataFrame(rows, crs=4326)


@pytest.fixture
def points():
    rng = np.random.default_rng(1)
    return gpd.GeoDataFrame(
        {"name": [f"Clinic {i}" for i in range(15)],
         "geometry": [Point(8 + rng.random() * 2, 6 + rng.random() * 2) for _ in range(15)]},
        crs=4326,
    )


@pytest.fixture
def lines():
    return gpd.GeoDataFrame(
        {"road": ["Alpha Road", "Beta Road", "Gamma Road"],
         "geometry": [LineString([(8, 6), (10, 6.5)]), LineString([(8, 7), (10, 7.2)]), LineString([(9, 6), (9.1, 8)])]},
        crs=4326,
    )
