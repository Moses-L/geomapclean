"""Generate the small synthetic 'Null Island' demo layer shipped with the package.

It is invented data (no real place, no licence issues): 16 districts made by
clipping a Voronoi diagram to an irregular island outline.
"""
import json
import numpy as np
import shapely
from shapely.geometry import Point, MultiPoint, mapping
from shapely.ops import voronoi_diagram

rng = np.random.default_rng(2026)
ang = np.linspace(0, 2 * np.pi, 120, endpoint=False)
rad = 1.0 + 0.18 * np.sin(3 * ang + 0.7) + 0.10 * np.sin(5 * ang + 2.0) + 0.05 * np.sin(9 * ang)
island = shapely.Polygon(np.c_[1.25 * rad * np.cos(ang), 0.85 * rad * np.sin(ang)]).buffer(0)
pts = []
while len(pts) < 16:
    p = Point(rng.uniform(-1.4, 1.4), rng.uniform(-1.0, 1.0))
    if island.contains(p) and all(p.distance(q) > 0.28 for q in pts):
        pts.append(p)
cells = voronoi_diagram(MultiPoint(pts), envelope=island.buffer(1).envelope)
names = ["Northvale", "Eastmoor", "Saltmarsh", "Highcliff", "Rivermouth", "Greenhollow", "Port Amberley",
         "Stonebridge", "Westerly Heath", "Lakeside", "Ashford Reach", "Cape Meridian", "Old Harbour",
         "Fernwood", "Kingsway", "Marlow Bay"]
regions = ["North", "East", "South", "North", "East", "South", "East", "North", "South", "North", "East", "South", "East", "South", "North", "East"]
feats = []
polys = [c.intersection(island) for c in cells.geoms]
polys = [p for p in polys if not p.is_empty]
order = sorted(range(len(polys)), key=lambda i: (-polys[i].centroid.y, polys[i].centroid.x))
for n, i in enumerate(order[:16]):
    pop = int(rng.integers(8_000, 240_000))
    feats.append({
        "type": "Feature",
        "properties": {"district": names[n], "region": regions[n], "population": pop,
                       "coverage_pct": round(float(rng.uniform(38, 97)), 1)},
        "geometry": mapping(shapely.set_precision(polys[i], 1e-5)),
    })
with open("src/moseslukegeomap/data/null_island.geojson", "w") as fh:
    json.dump({"type": "FeatureCollection", "name": "null_island", "features": feats}, fh)
print(len(feats), "districts")
