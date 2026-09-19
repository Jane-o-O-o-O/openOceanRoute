# Two-dimensional sampled terrain slopes around body locations

This independent research module samples a genuine two-dimensional terrain window. It supports the public MakaiPlan geographic-rule capability of examining seabed slopes near cable bodies, described on physical PDF pages 264–266 / printed pages 256–258 of the supplied 6.2 manual. Those pages do not disclose an original numerical grid, interpolation or continuous-maximum algorithm. This implementation does not claim that algorithm, its formats, field accuracy, safe burial conditions or navigation suitability.

The existing longitudinal profile samples depth along route KP. A route side-slope section samples a transverse line. Neither establishes terrain slope at a location away from those lines. This module obtains actual two-dimensional measurements/interpolated depth from the existing `terrain_sources` library, preserving the selected datum, priority, source fingerprints and every attempted-source status. It does not alter a project, path, profile, material inventory or source declaration.

## Public entry points and bounds

```python
sample_slope_neighborhood(project, center, radius_m, config=None, *, sources=None)

sampler = SlopeNeighborhoodSampler(project, config=None, *, sources=None)
estimate = sampler.estimate_many(windows)
batch = sampler.sample_many(windows)
# windows = [{"center": {"longitude": ..., "latitude": ..., "kp_m": ...},
#             "radius_m": ...}, ...]
```

Both entry points return `model="terrain-slope-neighborhood-v1"` and `validation_status="research"`. The class normalizes a copied source library once. It queries the entire batch in one invocation of the genuine existing terrain operator, sharing identical geographic query points and existing prepared-source cache entries. Each `sample_many` call has its own explicit total budget; a caller evaluating several batches/rules must sum their costs. The maximum is 200 windows per call, with no truncation.

`center` accepts only WGS84 `longitude`, `latitude` and optional `kp_m`. When KP is supplied it must be finite, nonnegative and within the current route length; explicit `null` is rejected. The module reconstructs the actual rhumb/geodesic route position and requires the supplied centre to match within `0.0001 m` of WGS84 surface distance. It records the geometry/curve `route_signature` and actual `route_position_error_m`. Endpoints and repeated zero-length legs are supported. A completely coincident route may supply KP0: a circular window needs no route heading. Missing KP instead requests an explicitly located local window; its `route_signature` is null and it is not evidence that the centre belongs to a body or route. A caller evaluating a body must supply and bind that body's actual KP and position. The module does not infer a point from a body icon or its finite physical length.

Project internal CRS, when specified, must be `EPSG:4326`. Optional routes have 2–10,000 finite coordinate objects and a supported curve. Longitude is −180…180°, latitude −90…90°; a centre exactly at a geographic pole is rejected because its east/north convention is undefined. Source CRS may differ and is handled by the existing strict source transformation operator.

| Parameter | Default | Explicit supported bound |
| --- | --- | --- |
| `radius_m` | required | 0.001…100,000 m; zero/null are rejected rather than replaced |
| `spacing_m` | 50 | 0.001…100,000 m, upper bound on base and child triangle edges |
| `vertical_datum` | selected implicitly only if unambiguous | Nonempty declared datum name, at most 128 characters |
| `max_query_points` | 50,000 | Integer 1…50,000 for the entire declared batch |
| `max_work_units` | 30,000,000 | Integer 1…200,000,000 for the entire batch |
| `max_output_bytes` | 16 MiB | Integer 1 KiB…64 MiB for the whole finite UTF-8 result |

The config accepts only the five fields above. Unknown fields, booleans used as numbers, strings used as numeric values, nonfinite values and integers too large for a finite float are rejected. A proximity rule with a zero-distance point search cannot request a nonzero slope neighbourhood implicitly. Unsupported extremely small/large radii must remain explicit failures/unknowns in the caller, not silently be changed.

## Geometry, probes and actual slopes

The centre defines a WGS84 ellipsoidal azimuthal equidistant (AEQD) local frame, with x east and y north in metres. A local `(x,y)` becomes an actual WGS84 position by a geodesic forward calculation at azimuth `atan2(x,y)` and surface distance `hypot(x,y)`. The [PROJ AEQD reference](https://proj.org/en/stable/operations/projections/aeqd.html) documents this projection, including its ellipsoidal form and origin parameters. [pyproj Geod](https://pyproj4.github.io/pyproj/stable/api/geod.html) specifies the genuine forward/inverse distance and azimuth operations. This is neither longitude/latitude Euclidean distance nor a degrees-to-metres conversion. At high latitudes and the date line, probes follow these real geodesics and longitudes wrap correctly.

Radial distance from the centre is exact for the chosen WGS84 geodesic. Transverse distances and slope gradients are evaluated in the local planar approximation. The 100 km domain bound is a computation limit, not a certified projection-error or measurement-accuracy bound. Near-antipodal/nonunique route branches inherit the existing geodesic branch convention; this module does not enumerate alternate route interpretations.

Let `R=radius_m`, `s=spacing_m`, `d=s/sqrt(2)`. The ring count is `n=max(1,ceil(R/d))`; angular count is `N=max(8,4*ceil((2*pi*R/d)/4))`. Every ring shares N evenly spaced angles including the cardinal axes. A centre fan and fixed diagonals between successive rings form base triangle families. Radial step and maximum outer arc are each at most d, bounding every base edge by s. Fixed connectivity is kept even when source values are missing; known points are never retriangulated across a hole.

For each base family, all three corners, three edge midpoints and its centroid are **actually queried**. Shared edges reuse their midpoint. Each family is then split into six child triangles between consecutive corner/midpoint points and its actual sampled centroid. Thus the real values at midpoints and centroids contribute to slopes, rather than serving only as availability checks. A known raised midpoint surrounded by flat corners is detectable at the declared resolution. A narrower ridge between all declared points can still remain undetected.

For a child triangle with vertices `(x_i,y_i)` and actual depths `D_i`, `h_i=-D_i`. Solve the two linear edge equations:

```text
[x1-x0, y1-y0] . g = h1-h0
[x2-x0, y2-y0] . g = h2-h0
slope_deg = degrees(atan(hypot(gx, gy)))
```

`gradient_height=[gx,gy]` is a signed east/north gradient of this particular affine triangle. `slope_deg` is its nonnegative steepest magnitude, unlike the signed route side angle. No heading from the route is used. `max_sampled_slope_deg` is the maximum over valid child triangles, not a continuous bed maximum or a point derivative. On a smooth affine field every gradient is exact apart from numerical transformation error. On a curved field it is a finite triangular secant. Across a kink or abrupt sampled ridge, the affine gradient can exceed the one-sided smooth derivative; it must not be described as that derivative. Smooth-field refinement is tested against the independent analytic gradient, but it does not certify all arbitrary terrain fields.

## NoData, source boundaries and circular coverage

Every sample retains depth in metres positive down, source ID/fingerprint, fallbacks and full attempts. Corrupt sources and missing required horizontal operation grids are errors. GeoJSON/KML Z values and S-57 soundings do not become engineering terrain sources automatically. NoData stays null; there is no zero fill or coverage extrapolation. The existing operator only mixes explicitly matching named vertical datums. An explicit selection excludes other datums and reports them; no tide/datum transformation occurs. Equal datum names are user declarations, not survey alignment or accuracy evidence.

For each family, **all seven** supporting samples must be known and carry the same `(source_id, source_fingerprint)`. Any missing sample makes all six children's slope/gradient null. Any known support identity change similarly makes all six null rather than turning an inter-source depth jump into a slope. `missing_support` and `source_boundary` are independent booleans; if both hold, `reason="nodata"` takes precedence while the seam flag remains true. Family-wide refusal is deliberately conservative even if a smaller child happens to have three known corners.

`sample_complete` means every declared sample has a depth. `triangle_complete` means every child also meets the same-source family guard. A fully sampled seam may therefore have `sample_complete=true` and `triangle_complete=false`. Unknown triangles remain unknown even when a separate known triangle establishes an exceedance. `max_sampled_slope_deg` is null if no valid child exists. A rule consumer must never infer a pass solely because a known maximum is under its threshold while unknown pieces remain.

The outer boundary is an **inscribed N-gon**, not the entire closed circle. Output records:

```text
disk_area_m2          = pi*R^2
triangulated_area_m2  = N*R^2*sin(2*pi/N)/2
missing_area_m2       = disk_area_m2 - triangulated_area_m2
coverage_fraction    = triangulated_area_m2 / disk_area_m2
```

This `missing_area_m2` is the geometric circular fringe, distinct from `nodata_triangle_area_m2` and `source_boundary_triangle_area_m2`. The last two can overlap when both problems occur. `valid_disk_area_fraction` counts accepted child area against the full disk. The mesh covers two dimensions within the polygon, but finite point sampling does not verify the intervening raster cells, empty patches or sub-probe hazards. Every result explicitly has `continuous_bed_verified=false`; even complete samples and triangles cannot constitute continuous circular-domain acceptance.

## Response and locations

A single result contains `metadata`, `center`, `radius_m`, `samples`, `triangles`, `quality`, `sources`, `budget`, `warnings`, `assumptions`. The batch instead contains `neighborhoods` with each window's metadata/samples/triangles/quality and one shared source/quality/budget/warning section. Single `quality.terrain_query` preserves underlying source-query quality; batch root `quality` refers to that query and each window has its own completeness fields.

Metadata includes `model`, `schema_version=1`, library/optional route signatures, selected vertical datum, spacing, local CRS/axes, geometry policies, `vertex_count`, `base_triangle_count`, six-times `triangle_count`, ring/angular counts and circular coverage quantities. Signatures bind the declarations that actually produced the samples; this module does not persist freshness decisions by itself.

Each sample contains `index`, local x/y, geographic position, depth/provenance/attempts and `query_index` in the shared actual query batch. Each child triangle contains:

```text
index, family_index, family_vertex_indices[3], family_centroid_sample_index,
vertex_indices[3], support_indices[7], area_m2,
valid, slope_deg, gradient_height,
reason, missing_support, source_boundary, source_id, source_fingerprint,
centroid, sampled_witness
```

The `centroid` is the geometric child centroid, geographically located by the same AEQD/geodesic mapping. It was **not** separately queried: its `depth_m` is null and `location_kind="geometric_child_centroid_not_queried"`. Do not fill it with interpolated depth while calling that depth measured. `sampled_witness` is the family's actual queried centroid, a common vertex of its six children. It includes the real sample index/query index/local/geographic coordinates/depth/source fields and can locate the evidence with an actual queried water depth. The three vertex indices retain all actual depth values used by the slope calculation. The witness is a sample of the interpolated source terrain, not a claim of a new in-situ measurement.

## Batch costs and admission

The base mesh has `V=1+n*N` corners, `T=(2*n-1)*N` families and `E=(3*T+N)/2` unique edges. Declared probe count is `Q=V+E+T`; the returned child count is `6*T`. Counts are evaluated before mesh arrays or full source interpolation. There is no automatic thinning. All windows' Q are summed for the 50,000-point limit even when exact geographic duplicates will later be reused; this conservative admission is reproducible. Exact coordinate tuple equality avoids arbitrary rounded-coordinate merging.

`estimate_many` validates the explicit centres/ranges and returns whole-batch count/work/output bounds. Source normalization/header checks occur once in the class constructor, not per window. The estimate includes declared normalization allowance `128+ceil(raw_source_JSON_bytes/4)`, route allowance 16 per leg, 128 per window, 32 per corner, 48 per support and 128 per actual child. It also includes the existing terrain operator's worst-case preparation and attempted-query charges for all enabled sources in the selected datum, independent of cache warmth. These are **charged normalized logical units**, not actual floating-point operation counts, CPU duration, token billing or native-library memory measurements.

Actual `budget` records `query_count` (declared per-window probes), `unique_query_count` (actual deduplicated query batch), `cache_hits` (coordinate reuse), separate `source_preparation_cache_hits/misses`, normalization/geometry/terrain work, total `work_units`, preflight estimates and final `output_bytes`. Existing terrain preparation is charged identically on source cache hits and misses. An instance can be reused across calls, but it does not silently grant an unlimited caller-wide budget; multi-rule callers must aggregate their separate returned costs or batch all eligible windows at once.

The entire response has conservative preflight bytes for all samples/attempts, actual UTF-8/escaped source ID sizes, six-child evidence, source/CRS metadata and windows. Final finite compact UTF-8 JSON is measured including its stabilized byte counter and rejected if over the selected limit. Estimates intentionally may reject an allowance under which a particular eventual result would have been smaller. No oversized output, missing diagnostic or window is silently dropped.

## Reproducible synthetic input

```python
import base64
from oceanroute.terrain_slope_neighborhoods import sample_slope_neighborhood

axis = [-200 + 50*i for i in range(9)]
rows = [[1000 + .2*x - .07*y for x in axis] for y in axis]
text = "DSAA\n9 9\n-200 200\n-200 200\n0 1000000\n"
text += "\n".join(" ".join(map(str, row)) for row in rows) + "\n"
source = {
    "id": "explicit-synthetic-plane", "kind": "surfer",
    "source_crs": "+proj=aeqd +lat_0=0 +lon_0=0 +datum=WGS84 +units=m",
    "depth_positive": "down", "depth_units": "m", "vertical_datum": "TEST_DATUM",
    "sampling": {"method": "linear"},
    "data_base64": base64.b64encode(text.encode()).decode(),
}
result = sample_slope_neighborhood({"terrain_sources": [source]},
    {"longitude": 0, "latitude": 0}, 100, {"spacing_m": 50})
```

This real query gives 321 declared support points, 100 base families / 600 children and gradient `[-.2,+.07]`, hence slope `11.9637948053°`. The polygon fraction is `0.9836316430834658`; its 514.2271 m² fringe remains unrepresented, and continuous coverage is false. The source is explicitly synthetic, not a chart or measured sea floor.

## Actual development checks

The new module's 48 executed cases include independent affine gradients, WGS84 radius/AEQD locations, edge/area bounds, off-route transverse hazards, genuine two-dimensional V/ridge fields, smooth curved-field refinement against analytic derivatives, an actual raised edge midpoint with flat corners, NoData at an edge midpoint despite known corners, source seams, explicit datum selection, upward feet conversion, high-latitude/date-line probes, rhumb/geodesic KP binding/endpoints/repeats, all-zero route, immutable inputs/signature changes, cache/work accounting, global batch bounds and strict finite/null rejection. Expected field values/gradients are not obtained from this module's slope helpers. PROJ is shared with the independent geodesic/coordinate oracle, which is not an independent survey or another geodesic library.

The final targeted combined run of the new cases and existing terrain-source / route side-slope suites executed **129 tests, all passed in 3.72 s**. This is a development check, not an installation/browser/release gate or original-model equivalence result. Subsequent production integration and any later source edits require their own actual evidence.
