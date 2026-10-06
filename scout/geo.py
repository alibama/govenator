"""geo — which county contains a point. Pure Python point-in-polygon over the
county GeoJSON the map uses, with a 1-degree grid index so 20k lookups are fast."""
import json
import math


def _in_ring(ring, x, y):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


class Counties:
    def __init__(self, path):
        with open(path) as f:
            gj = json.load(f)
        self.items, self.grid = [], {}
        for feat in gj["features"]:
            props = feat.get("properties") or {}
            fips = str(feat.get("id") or (props.get("STATE", "") + props.get("COUNTY", "")))
            geom = feat["geometry"]
            polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
            xs = [pt[0] for poly in polys for pt in poly[0]]
            ys = [pt[1] for poly in polys for pt in poly[0]]
            bbox = (min(xs), min(ys), max(xs), max(ys))
            idx = len(self.items)
            self.items.append((fips, props.get("NAME", ""), bbox, polys))
            for gx in range(math.floor(bbox[0]), math.floor(bbox[2]) + 1):
                for gy in range(math.floor(bbox[1]), math.floor(bbox[3]) + 1):
                    self.grid.setdefault((gx, gy), []).append(idx)

    def locate(self, lon, lat, state_fips=None):
        if lon is None or lat is None:
            return None
        for idx in self.grid.get((math.floor(lon), math.floor(lat)), []):
            fips, _, b, polys = self.items[idx]
            if b[0] <= lon <= b[2] and b[1] <= lat <= b[3]:
                for poly in polys:
                    if _in_ring(poly[0], lon, lat) and not any(_in_ring(h, lon, lat) for h in poly[1:]):
                        return fips
        # coastal points can fall just outside a polygon: nearest county in the same state
        best, bd = None, 1e9
        for fips, _, b, _ in self.items:
            if state_fips and not fips.startswith(state_fips):
                continue
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            d = (cx - lon) ** 2 + (cy - lat) ** 2
            if d < bd:
                best, bd = fips, d
        return best if bd < 1.0 else None
