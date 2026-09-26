## Summary

`import_from_geojson` passes the caller's path straight to
`arcpy.conversion.JSONToFeatures`. That tool parses a file with a **`.geojson`**
extension as **polygons only**: a point or polyline layer comes back as an
**empty feature class with the default geometry type**, and nothing raises.

Renaming the identical bytes to `.json` imports all three geometry types
correctly.

## Reproduction

Controlled experiment — same bytes, only the extension differs:

```
facilities.geojson  ->  n=0   shapeType=Polygon (default)  spatialReference=Unknown
facilities.json     ->  n=10  shapeType=Point              spatialReference=4326
roads.geojson       ->  n=0   shapeType=Polygon (default)  spatialReference=Unknown
roads.json          ->  n=6   shapeType=Polyline           spatialReference=4326
parcels.geojson     ->  n=30  shapeType=Polygon            spatialReference=4326
```

Minimal reproducer — a one-feature FeatureCollection:

```json
{"type":"FeatureCollection","features":[{"type":"Feature",
 "geometry":{"type":"Point","coordinates":[115.97,24.39]},
 "properties":{"id":1}}]}
```

as `probe.geojson` → 0 features; as `probe.json` → 1 feature.

`parcels` succeeds because it *is* polygons — which is exactly what makes this
hard to notice: a polygon-only test scene works perfectly.

Byte-identity was verified (2520 bytes, identical content, `json.loads` equal),
so this is not a content or encoding issue. GeoJSON *is* JSON, and RFC 7946
defines no extension, so `.geojson` is a naming convention rather than a format
difference.

## Why it matters

The failure is **silent and displaced**. Nothing in the chain raises:

* `JSONToFeatures` does not error;
* `GetCount` faithfully returns 0;
* downstream tasks then fail with `ERROR 000732: 数据集不存在或不受支持`, or the
  agent reports "that layer is empty" — and the blame lands on the agent.

That last part is the expensive bit. I burned a full evaluation cycle
attributing failures to the model before tracing them to the extension. Two of
six layers in my scene had been empty the whole time, and the model had been
correct about it: it said the facilities layer was empty, and it was.

## Suggested change

Options, in increasing order of effort:

1. **Document it.** A note that `.json` is required for point and polyline
   input would have saved the entire detour.
2. **Stage a temporary `.json`** when the input ends in `.geojson`, then call
   `JSONToFeatures`. Two lines, and it makes the tool behave the way a caller
   reasonably expects from something named *import_from_geojson*.
3. **Detect the empty result and raise.** `GetCount == 0` immediately after
   successfully importing a non-empty file is always a bug somewhere, and a
   loud failure beats a silent one.

I'd suggest (2) + (3). Happy to send a PR for either.

## Environment

- arcgis-mcp-bridge 0.6.6 (commit `2019d7b0`)
- ArcGIS Pro 3.6, Python 3.13.7, Windows 11 x64
- Input produced by Shapely 2.1.2 / pyproj 3.8.0, standard GeoJSON (RFC 7946)
