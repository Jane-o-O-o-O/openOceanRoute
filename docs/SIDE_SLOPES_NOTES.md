# Route side slopes: independent sampled terrain sections

`oceanroute.side_slopes.side_slopes_from_sources(project, config=None, *, sources=None)` returns actual two-dimensional bathymetry samples across the route and a separate reviewable project candidate. This is an independent research implementation. It does not establish Makai algorithm equivalence, measurement accuracy, plow stability or a safe burial corridor. The frozen 0.8 releases and evidence are unchanged.

## Public grounding and distinction from existing slopes

The supplied MakaiPlan 6.2 manual, physical PDF page 114 / printed page 106, describes the Side Slope tab and makes uphill on the starboard side positive. Physical page 273 / printed page 265 distinguishes along-track from across-track terrain, and physical page 307 / printed page 299 describes reference-line slices. Makai's [DTM article](https://www.makai.com/brochures/DTMArticle.pdf), pp. 1–4, and [MakaiPlan brochure](https://www.makai.com/brochures/MakaiPlan.pdf), p. 8, also describe along/across route profiles. These sources establish the public capability and sign convention, but do not specify this implementation's probe spacing, width, root policies or numerical formulas.

Existing `core` profile `slope_deg` is the positive-down depth difference divided by adjacent along-route KP difference, converted to degrees. A leg's `max_slope_deg` is the greatest absolute value of those **longitudinal** sampled secants. `dtm` slope is the two-dimensional gradient magnitude / steepest slope; `terrain_slice` slope is along its declared projected reference polyline. None is inherently a route side slope.

The new module generates route-normal WGS84 cross sections and samples them with the existing real source library. The [PROJ geodesic documentation](https://proj.org/en/stable/geodesic.html) distinguishes initial and terminal forward azimuths. [pyproj Geod](https://pyproj4.github.io/pyproj/stable/api/geod.html) supplies the actual forward azimuth at an interior/terminal position. A geodesic leg's initial bearing must not be reused for all cross sections. Rhumb heading is constant, and the existing ellipsoidal rhumb-distance/interpolation implementation is reused.

## Coordinates, sign and numerical meaning

Route coordinates are the existing WGS84 longitude/latitude route points. KP is horizontal surface distance along the declared `route.curve`, `rhumb` or `geodesic`. Along-route stations include both requested range endpoints, all route vertices in that closed range and an evenly subdivided station sequence with gaps no larger than `spacing_m`. Repeated coincident vertices do not create a new tangent. At an internal waypoint, the outgoing nonzero leg supplies the one-sided heading; at the route terminal, the incoming leg supplies it. A true geographic pole has no unique local compass frame and is rejected. Geodesic branch selection follows the existing PROJ route convention; this is not a claim that all shortest geodesics are unique.

For heading `theta` measured clockwise from north, the local forward direction is `(sin(theta), cos(theta))` in east/north coordinates, and the rightward direction is `(cos(theta), -sin(theta))`. Each probe lies on a WGS84 geodesic ray leaving the station at `theta+90°` on starboard or `theta-90°` on port. Its signed surface distance is `offset_m`, positive starboard. Longitude is correctly wrapped at the antimeridian. Each cross section is normal at its own station; the sections are not assumed globally parallel or a globally orthogonal mesh.

Let `w=half_width_m`, `D` be depth in metres positive down in the explicitly selected source datum and `Z=-D`. Every angle uses an increasing **port-to-starboard** axis and is positive for rising elevation toward starboard:

```text
port_slope_deg       = degrees(atan((D(-w)-D(0))/w))
starboard_slope_deg  = degrees(atan((D(0)-D(w))/w))
side_slope_deg       = degrees(atan((D(-w)-D(w))/(2*w)))
segment_i_deg       = degrees(atan((D(u_i)-D(u_(i+1)))/(u_(i+1)-u_i)))
max_sampled_abs_slope_deg = max(abs(segment_i_deg) over adjacent known probes)
```

The half/full angles are finite-width endpoint **secants**, not the point derivative at the centre. At a smooth point the true local side derivative would be `-gradient(D) dot right_direction`, but that derivative is not asserted by this module. A symmetric ridge can have zero full-width angle and large half/adjacent angles. A sampled cross-section maximum is neither a continuous-area corridor maximum nor the terrain's steepest slope in an arbitrary direction.

Both halves contain symmetric probes and zero exactly; actual probe step is `w/ceil(w/cross_spacing_m)`. The endpoints are retained even when requested spacing is greater than the half-width.

## Sources, gaps and transitions

The same `terrain_sources.query_terrain` point sampler handles embedded XYZ, GeoTIFF and Surfer sources, source CRS operations, explicit depth sign/units, priority, declared vertical datum, source interpolation, coverage and source fallbacks. It does not read external paths, assume GeoJSON third coordinates are bathymetry, infer datum transforms or treat S-57 SOUNDG as engineering depth. Missing best horizontal datum grids and corrupt source data are errors, rather than hidden low-priority fallbacks. Unknown depth remains `null`.

Every probe retains source ID/fingerprint, fallback counts and complete attempted-source statuses. Different enabled datums require explicit selection; other datums are excluded and reported. A constant vertical translation within one common datum does not change slopes, but independently offset sources can manufacture an apparent slope at their seam. Same-name datum declarations are not an accuracy proof.

If **any** probe in a half or full width is missing, that entire half/full secant is `null`, even when its two endpoints are known. Individual adjacent segments still have an angle when both of their endpoints are valid. Missing points are never connected to the next valid point across a gap. If there is no valid adjacent segment, `max_sampled_abs_slope_deg` is `null`.

`complete` means all declared probes in a section have depth. `source_boundary` is a boolean indicating more than one `(source_id, source_fingerprint)` identity among its valid probes, including changes separated by a missing probe. `source_boundary_to_next` separately identifies an actual adjacent valid-source change. Such displayed secants remain potentially caused by source-depth jumps; they are not certified continuous seabed slopes. Source-boundary and incomplete sections must remain uncertain in rule evaluation.

NoData or a narrow hazard **between** probe points or between longitudinal stations may remain undetected. No cell-topology traversal, gap-width guarantee, whole-swath coverage proof or continuous maximum is claimed. `metadata.completeness_basis` records this limitation, and `complete` must not be presented as engineering acceptance.

## Config and result contract

Supported config fields are strict; unknown fields, booleans used as numbers and nonfinite or unrepresentable numeric inputs are rejected:

| Field | Default | Range / meaning |
| --- | --- | --- |
| `spacing_m` | 1000 | 1–100,000 m maximum along-route station spacing |
| `half_width_m` | 100 | 0.001–100,000 m each side |
| `cross_spacing_m` | 50 | 0.001–100,000 m maximum transverse probe spacing |
| `start_kp_m` | 0 | Closed range start, within route |
| `end_kp_m` | total route length | Omitted or explicit `null` uses the current route length; otherwise a finite closed range end. Start may equal end for one section. |
| `vertical_datum` | implicit only if unambiguous | Nonempty declared datum name, at most 128 characters |
| `max_query_points` | 50,000 | Integer 1–50,000; stations × probes |
| `max_work_units` | 30,000,000 | Integer 1–200,000,000 normalized work units |
| `max_output_bytes` | 16 MiB | Integer 1 KiB–64 MiB; conservative preflight and actual final JSON |

```text
result.model = 'route-side-slopes-v1'
result.validation_status = 'research'
result.side_slopes = {
  model: 'route-side-slopes-v1', schema_version: 1, route_signature,
  metadata: {
    model: 'route-side-slopes-v1', terrain_library_signature, vertical_datum,
    spacing_m, half_width_m, cross_spacing_m, actual_cross_spacing_m,
    start_kp_m, end_kp_m, route_length_m, route_curve,
    heading_policy: 'outgoing_one_sided_at_waypoint_incoming_at_terminal',
    offset_positive: 'starboard',
    slope_positive: 'rising_elevation_towards_starboard',
    slope_definition: 'finite_width_endpoint_secants_and_adjacent_sampled_segments',
    depth_positive: 'down', units: 'm', slope_units: 'degrees', ...
  },
  samples: [{
    kp_m, longitude, latitude, heading_deg,
    port_slope_deg, starboard_slope_deg, side_slope_deg,
    max_sampled_abs_slope_deg, complete, source_boundary,
    transect: [{
      offset_m, longitude, latitude, depth_m,
      source_id, source_fingerprint, fallback, fallback_count, attempts,
      slope_to_next_deg, source_boundary_to_next
    }]
  }]
}
result.project = reviewable candidate
result.sources = actual source metadata and horizontal-operation evidence
result.quality = source counts/gaps/fallbacks plus section counts and sampled maximum
result.budget = combined work, actual query point count, preflight/actual response bytes
result.warnings / result.assumptions = explicit uncertainty and model scope
```

The returned candidate adds `side_slopes` and normalized source declarations. It does not change the original input or replace its route, longitudinal profile, cable library, assembly, manufacture inventory or arbitrary extension fields. Callers must review/apply the whole candidate through the existing transaction workflow. Library priority/enabling/content changes alter `terrain_library_signature`; route geometry/curve changes alter `route_signature`. This module supplies those bindings but does not edit the existing rule or persistence modules.

## Resource accounting

Station × probe limits are checked before allocating coordinate/transect arrays and before reading source rasters. Route geometry allows 16 units per route leg, station positioning 48 per station and probe/section processing 24 per probe. These declared allowances plus the genuine existing terrain sampler's preparation/query charges share the one requested `max_work_units`; the sampler receives only the remaining budget. Its conservative preparation allowance is charged on cache hits too. These are normalized logical work units, not CPU FLOPs, time, opaque native allocation limits or token prices.

Output preflight includes the candidate's real JSON bytes, duplicate stored/top-level sections and encoded enabled-source ID lengths/attempt metadata. Final finite compact UTF-8 JSON is measured again, including its byte counter, and rejected if over budget. Nothing is silently thinned, omitted, truncated or called partially completed. The preflight is deliberately conservative and may reject a small requested response cap even when a particular eventual serialization would have been smaller.

## Numerical evidence and independent checks

The targeted test file uses explicit affine and bilinear depth fields, rather than feeding the implementation's returned slopes back into its own slope helper. At the centre of a declared AEQD grid, geodesic radial coordinates have the prescribed metre distances, so planar directional grades give an independent closed-form oracle. A north-going route through `D(x,y)=1000+0.2*x` has a flat longitudinal profile but a side angle `-atan(0.2)=-11.309932474020215°`. Reversing the route exchanges port/starboard and reverses each signed angle.

Actual source tests cover a bilinear curved field, a ridge whose full-width angle cancels, an internal missing probe with known endpoints, explicit unit/sign conversion and datum exclusion, genuine priority fallback/source jumps and finite JSON. High-latitude geodesic azimuth/probe-distance tests use direct independent `pyproj.Geod` calls rather than this module's station helper; an ellipsoidal parallel's rhumb distance is independently calculated from its prime-vertical radius. These check the route mathematics but do not constitute an independent implementation of PROJ's geodesic library. Corners, repeated vertices, endpoint ranges, date-line wrapping, undefined polar tangents, budgets before the reader and profile/stock nonmutation are also checked.

This document does not claim release packaging, installation, full backend/browser gates or original-product validation. Those require separate actual development/release evidence.
