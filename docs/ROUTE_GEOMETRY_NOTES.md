# True WGS84 radius-circle segment geometry

This is an independent geometric interpretation of the public Radius AC workflow. It is not the manufacturer's hidden circle construction, native file format, numerical tolerances, or field-accuracy certification. Original manual physical pages 195–196 describe Split AC as inserted equal-angle, equal-internal-distance straight legs. Pages 197–198 describe Radius AC as inserting a path point and moving the original point so a radius-defined arc fits between the two points while preserving the adjacent bearings. The page 198 menu explicitly distinguishes `Radius - Fix points - convert to rhumb lines` from a retained radius arc. A drawing polyline is therefore not a substitute for the retained continuous segment.

## Persistent descriptor and compatibility

Global `route.curve` remains `rhumb` or `geodesic`, and governs straight legs. Existing point IDs, manufacture inventory, slack, material and body options are outside this module. A leg may additionally contain:

```json
{
  "geometry": {
    "type": "circular_arc",
    "schema_version": 1,
    "center": [118.0, 22.0],
    "radius_m": 4000.0,
    "start_azimuth_deg": 0.0,
    "sweep_deg": 90.0
  }
}
```

The descriptor must agree with the actual adjoining route point coordinates within the explicitly bounded endpoint tolerance. Positive sweep increases the center's radial compass azimuth clockwise from north; negative sweep decreases it. Latitude/longitude are WGS84 degrees; radius and KP are metres. Radius must be `.001..1,000,000 m`, nonzero signed sweep must lie in `[-360,360] degrees`, and the schema/fields are strict. Full circles are supported and have positive length even though their endpoints coincide. The radius cap is this implementation's admitted geodesic domain, not a claim about the original application's maximum radius. No unknown geometry is silently converted to a chord.

Missing/null geometry uses the existing `geodesy.inverse` and `geodesy.interpolate` functions. Straight distance and interpolation retain their previous numerical values. Straight geodesic start/end tangents retain the old inverse initial/back-azimuth semantics; unresolved positive geodesic legs with no numerical bearing return a null tangent. Zero-length straight segments have a null tangent. Root consumers preserve their old no-arc signature; new arc descriptors invalidate sampled route-bound evidence even when endpoint coordinates are unchanged.

## Intrinsic curve, length and tangent

For center `C`, radius `R`, initial radial azimuth `alpha0` and signed sweep `Delta`, the position is the WGS84 direct geodesic `p(t)=Direct(C, alpha0+Delta*t, R)`. This is a level set of geodesic distance, not a circle in an arbitrary map projection. GeographicLib's reduced length `m12` gives endpoint displacement transverse to the radial geodesic when its initial azimuth changes: `dl=m12*dalpha`, where the angle differential is in radians. The arc length is therefore the integral of positive `m12` over the signed radial-angle interval with absolute orientation.

The implementation integrates this metric and inverts cumulative physical length. `point_at_fraction(f)` always means physical length fraction, not radial-angle fraction. The latter differs on general ellipsoidal circles. The circle tangent at each actual arc point is its radial geodesic **forward arrival azimuth plus sign(sweep)*90 degrees**. Gauss' lemma makes this tangent orthogonal to the radial geodesic. The `azi2` returned by GeographicLib is a forward arrival azimuth; PROJ inverse's back azimuth must first have 180 degrees added.

Sources: [GeographicLib reduced length and geodesic definitions](https://geographiclib.sourceforge.io/html/python/geodesics.html), [units, masks and forward arrival conventions](https://geographiclib.sourceforge.io/html/python/interface.html), [the endpoint displacement direction azi2+90°](https://geographiclib.sourceforge.io/html/java/net/sf/geographiclib/Geodesic.html), and [PROJ Geod direct/inverse API](https://pyproj4.github.io/pyproj/stable/api/geod.html). GeographicLib is an explicit runtime dependency. No commercial source code or paid algorithm was used.

## Shared Python interface

`segment_from_leg(a,b,leg=None,curve='rhumb',config=None)` takes point dictionaries or `[longitude,latitude]` endpoints and the existing leg options object. `route_segments(project,config=None)` returns one `RouteSegment` per leg, retaining zero straight legs.

Each segment exposes `start`, `end`, `length_m`, `geometry`, `is_arc`, `solver`, `length_upper_bound_m`, and `position_error_allowance_m`.

- `point_at_distance(s)` / `point_at_fraction(f)` return a `(longitude,latitude)` tuple.
- `tangent_at_distance(s)` / `tangent_at_fraction(f)` return degrees or null for unresolved/zero straight legs.
- `subsegment(start_m,end_m)`, `split(distance_m)` and `reversed()` return **new segment objects**. Persist their `.geometry` in each new leg and their endpoints in route points. They preserve the original circle, its radius and its true sub-sweep. A zero-sweep circular subsegment or endpoint circular split is rejected.
- `.descriptor()` returns `{start:[lon,lat],end:[lon,lat],geometry:...}`; this whole object is not itself a leg's geometry descriptor.
- `.sample(spacing_m=5000,max_points=...)` returns coordinates at true physical-KP intervals. A vertex limit rejects the requested job; it does not silently increase spacing or change the mathematical curve.

Thin public helpers additionally expose `segment_length`, `segment_at_fraction`, `segment_at_distance`, `segment_tangent`, `split_segment`, `reverse_segment`, and a geometry-only `route_geometry_signature`. Core's project signature remains the authoritative persisted freshness binding.

## Numerical and resource boundaries

Default endpoint tolerance is `1e-4 m` (configurable `1e-8..1e-3`). Default integral absolute tolerance is `1e-7 m` (`1e-9..1e-3`), relative tolerance `5e-14` (`2e-14..1e-10`), and inversion maximum 32 iterations (1..64). Default per-segment native-evaluation work cap is 2,000,000 (1..10,000,000); default maximum sample count is 10,000 (2..100,000). Every native direct/inverse/interpolate evaluation is counted in normalized units, not FLOPs, wall time or token charges. Quad warnings, nonpositive reduced length, nonfinite calculations, iteration exhaustion and counter exhaustion reject rather than return an angular-fraction/chord fallback. Quadrature diagnostics are error **estimates**, not interval-arithmetic proofs.

For the admitted positive-curvature WGS84 circle domain, reduced length is at most `R`. Consequently `R*abs(sweep_rad)` is an analytic total-length upper bound, rounded upwards with `nextafter`. An interval of physical fraction width `df` has length at most that upper bound times `df`. `position_error_allowance_m` adds the configured endpoint, integration/inversion and native floating-point allowances; it explicitly is not an analytic directed-rounding proof for the numeric engines. Geometry checkers must preserve this distinction when adding the allowance to continuous curve bounds.

Terrain profile, side-section, KP-rule and 2D slope-window consumers use real segment lengths/positions/tangents. Their arc integration/inversion evaluation counters enter the respective job budget, and the remaining allowance is passed to terrain preparation/querying. Atomic workspace terrain preview additionally charges its arc-length preflight. Its ordinary workspace/schema/admission checks remain separate from the sampled query work, as before. The explicit 2D bathymetry bridge queries its actual WGS84 grid nodes and binds the result to core's arc-aware route signature; it does not infer a route-based bathymetry field from an arc drawing.

## Drawing and date line

`render_route(project,spacing_m=5000,max_vertices=250000,tolerance_m=1)` returns `coordinates`, date-line-separated `segments`, `render_model`, and `render_tolerance_m`. Pure straight routes retain the old `densify(...maximum=1000)` plus `split_antimeridian` operations. The maximum vertex input supports 10,020,001 to retain old long straight-path compatibility; default arc rendering remains bounded at 250,000.

True arcs refine actual quarter/mid/three-quarter points against the geographic chord, with an additional maximum 5-degree radial rotation and physical-length spacing. Even an arc shorter than 5 km retains visible curved geometry. This is an explicitly **sampled adaptive chord criterion**, not a proved continuous Hausdorff bound or chart accuracy. Vertex/work/refinement limits reject; they do not silently return a straight chord. Date-line intersections are solved on the original radius circle, so emitted ±180° seam points retain the actual radius. Rendering never overwrites the persistent descriptor or changes length/KP with drawing resolution.

## Independent validation scope

The new tests use an independent analytic parallel circumference for circles centered at either geographic pole, with WGS84 prime-vertical radius. These distinguish the true ellipsoidal length from `R*angle`. Independently generated PROJ circle points and their geodesic chord sums converge to the integrated metric with the expected fourfold refinement reduction. Additional tests distinguish physical KP fraction from angle fraction, verify radius/tangent/split/reversal/full-circle and true date-line cuts, compare old straight numerical results exactly, and reject invalid descriptors, endpoint mismatches, work/vertex exhaustion and actual inverse nonconvergence.

Terrain tests use a declared synthetic north-depth plane and a semicircle of radius 500 m. Its actual middle depth is 1050 m while its endpoint chord passes over depth 1000 m. True arc side tangents, opposite signs after reversal, 2D-window KP centers, a KP rule starting beyond the chord length, atomic workspace sampling and input immutability are exercised. A missing arc-middle probe stays missing despite known flat endpoint depths. These are module/workflow tests, not a manufacturer comparison, whole-backend gate or fresh-installation result. The current joint result is recorded to the parent agent from actual test execution; later release evidence must refer to its own source-bound run.

Retained limitations include the explicit radius/solver/output limits, floating-point allowances, sampled drawing/terrain coverage, and no surface/bottom-control or motion model implied by radius editing. Circle generation in the separate altercourse tool is a bounded local tangent construction with independent endpoint/tangent checks; this module does not claim all tangent construction roots were globally enumerated.
