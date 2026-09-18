# Independent geographic-rule review

This development review covers the new automatic geographic-rule engine, its continuous-curve geometry helper and the root-owned HTTP/workspace integration. It is not a manufacturer algorithm comparison, survey validation, navigation certificate or evidence that every arbitrary GIS feature can be evaluated without uncertainty. Only the new independent test file and these notes were edited by this reviewer. Frozen 0.9 and earlier artifacts, previous tests and the implementation modules were not edited.

## Scope and independent oracles

The implementation contract read was `resources/documentation_0_10_automatic_rules_contract.md`, followed by actual `oceanroute/automatic_rules.py`, `automatic_rule_geometry.py`, relevant `api.py`, `workspace.py` and `core.py` behavior. The supplied MakaiPlan 6.2 manual's physical PDF pages 262–272 / printed pages 254–264 ground the public crossing/proximity/slope capability and error display. In particular the actual page-264 interface shows crossing angle **less than**, depth **more than** and body distance **less than** triggers. It does not disclose a native algorithm, complete rule file format, equality/combination policy or exact distance/slope numerical method. The implementation explicitly declares its own comparison, all/any and distance conventions.

The independent tests construct geometry directly. They do not obtain expected locations, gradients, distances, angles or KP by calling production `automatic_rule_geometry` helpers. The principal oracles are:

- Equatorial route distance and meridional separation from direct WGS84 geodesic calculations; a perpendicular Point is nearest to the interior meridian foot, rather than a route endpoint.
- Axis-aligned geographic rectangles/holes and native straight source lines, whose exact topological contacts and geographic coordinates are constructed by hand.
- A true geodesic midpoint obtained by direct WGS84 forward calculation at half the full end-to-end distance. A rhumb parallel stays at its original latitude; its length is independently computed from the ellipsoidal parallel metric.
- Piecewise-linear depth on a straight route, whose admissible closed subintervals and isolated admissible values are analytic.
- Uniform 1% surface cable slack and a declared 40 m allowance, giving physical station 615's leading surface KP `(615-40)/1.01` independently of the production material mapper.
- A declared actual 2D field `depth=1000+.2*x`, so height gradient is `[-.2,0]` and sampled affine slope is `atan(.2)`, independent of the checker or triangle-gradient helper.

The distance/coordinate construction shares the PROJ engine with the implementation. This is a non-circular geometry/logic oracle, but not a comparison with an independent geodesic library or a field measurement. The [pyproj Geod reference](https://pyproj4.github.io/pyproj/stable/api/geod.html) defines forward/back azimuth and distance semantics. The [GeoJSON standard](https://www.rfc-editor.org/rfc/rfc7946#section-3.1.1) explicitly defines coordinate-linear source edges; native source longitude interpolation is not automatically a shortest geodesic. The [Shapely manual](https://shapely.readthedocs.io/en/stable/manual.html) describes planar topology and the boundary/containment distinctions; it does not make raw coordinate-plane distances ellipsoidal metres.

## Three real defects identified and repaired by the implementation owner

These were actually reproduced against the then-current public `check_automatic_rules` entry point before their independent regression expectations were added. The reviewer reported them to the parent and planner; the planner repaired implementation, not the reviewer.

1. **A same-geodesic waypoint falsely became an altercourse.** The actual WGS84 route `(0,70) → (45.00000000000001,75.57008147661035) → (90,70)` lies on one geodesic. Its incoming arrival and outgoing initial tangents coincide. The new engine had used two finite `1e-6`-fraction chord azimuths, returning a spurious `turn_deg=5.43312842182786e-05`. With a Point at that waypoint and an altercourse proximity radius of 1 m, it returned a real but false zero-distance violation. The corrected operator uses the real geodesic arrival/initial bearings, or the constant rhumb bearings. The independent test requires `clear`, no violations and zero altercourse subjects.

2. **Skipping a zero leg invented duplicate alteration objects.** Route `(0,0) → (.005,0) → (.005,0) → (.005,.005)` had produced two separately identified 90° subjects at the repeated location by searching backwards/forwards across the zero leg. Both immediate vertex turn angles in the existing core are undefined. The corrected object operator requires adjacent nonzero legs for an actual altercourse/transition; it does not invent two valid events from repeated records. The independent regression requires no altercourse subjects under this explicit policy. A future deliberate coalescing policy would be a different declared object model.

3. **A closed water-depth filter discarded valid singleton locations.** On the synthetic straight route `(0,0,D0) → (.01,0,D100)`, a valid filter `min_m=max_m=50` admits the exact middle KP. A Point at `(.005,.0001)` is only about 11.0574 m from that subject and must violate a 100 m proximity radius. The prior midpoint-only interval filter produced no admissible subject and falsely returned `clear`. The corrected engine retains isolated closed-filter points and represents them with an actual point curve and explicit surface KP. The tests also retain both route-endpoint cases, and a known isolated profile sample adjacent to missing intervals: true proximity evidence remains listed together with the unresolved depth ranges.

These are behavior corrections, not new claims of original algorithm equivalence. No failed baseline test count is invented: the original reproductions were direct public-entry-point probes, and the independent pytest file was run after the fixes landed.

## Actual final independent coverage

`tests/test_automatic_rules_independent.py` executed **51 tests, all passed in 1.09 s** using:

```text
.venv/bin/python -B -m pytest -o addopts= -q tests/test_automatic_rules_independent.py
```

Ordinary supported cases require actual violations or clear results; their assertions are not relaxed to permit an uninformative unknown. Genuine sharp contacts, declared unsupported/polar domains, stale/missing depths and numerical radius boundaries retain explicit uncertainty instead.

The tests cover:

- Recursive nested GeometryCollection Polygon containing the entire route, retaining its `primitive_path` and an actual containment error. This detects behavior that filtering the old area-only crossing list could not establish.
- A V-shaped line touching the route at its vertex. Its two incident tangents cannot be averaged to create a false 0° angle; conditional angle evaluation is unknown and an unconditional contact is a real touch.
- A hidden restricted Point at latitude0.0002° beside the route interior, whose real WGS84 separation is about 22.1149 m. The 100 m query produces an actual proximity violation; a 10 m query is clear. A layer's visible flag is not an engineering filter.
- Polygon holes and all four expected entry/exit locations on outer and inner rings; a route entirely within the hole remains outside the area. Line overlaps retain their real extent endpoints, and Point contacts remain actual contacts.
- A high-latitude geodesic crossing the native meridian feature at its actual75.570081…° midpoint while a constant-latitude70° rhumb path does not. The route nodes alone cannot establish the crossing.
- A native source edge from longitude179° to−179° crossing the Greenwich meridian. Its original coordinate-linear long-way semantics are preserved; it is not turned into a short date-line cable.
- Numeric native ID 1, string ID`"1"`, a genuinely unnamed source feature at index 2, and native ID`"index:2"`, which remain distinct. Missing layer/feature/index and ambiguous duplicate typed IDs produce reference errors; an independent valid selection's real violation is preserved when a second reference is missing.
- Whole-path proximity after clipping the complete eligible depth interval: a nearest unfiltered point outside the depth band does not hide a qualifying location elsewhere within the radius. Closed singleton/endpoints and known points adjacent to NoData are covered. An explicitly stale survey is not replaced by incidental waypoint depths.
- Explicit `lt/le/gt/ge` equality behavior and three-valued all/any predicates. Missing nearest-body distance stays null, including in a true disjunction, rather than becoming a convenient 0.
- A physically located body whose leading route KP is independently reconstructed through slack and reserve. Distinct co-located bodies produce genuine zero-distance pairs; self-identity pairs are excluded.
- Actual two-dimensional body slope queries, exact affine gradient, complete source fingerprint/query witness, a geometric child location with null depth, and circular coverage below 1. A missing window cannot pass merely because remaining known slopes are below the threshold.
- Whole-report work/feature/vertex/event/byte exhaustion, all of which reject rather than return a truncated clear report. Polar crossing overlay is explicitly unknown. A true metric-radius boundary is either supported by its actual distance upper bound or remains threshold-uncertain.
- Strict rejection of the obsolete `max_projection_radius_m` config name. Its replacement, `max_curve_chunk_m`, is actually used for bounded route curve domains; it does not imply a planar projection distance calculation.
- Export/import of nullable ends, typed selectors and unresolved references without inventory changes.
- Actual HTTP check preview, one explicit complete candidate save, revision preservation/conflict-free advancement, unchanged two-path shared alternative inventory/associations, rejected typed 422 budget failure leaving stored state/revision history intact, and a genuinely new application reading the same durable database and obtaining the same real violation. The original input is unchanged. The tests do not claim any browser/undo gate or physical device owner test.

Only this targeted suite was run by this reviewer; existing root API tests, the planner's own numerical tests, full backend, browser, installation and release gates are separate evidence. Their counts are not accumulated here.

## Limits and user-facing meaning

Native GIS topology uses the original geographic-linear primitives, recursively retaining holes and collection paths. Actual geodesic/rhumb route curves are adaptively overlaid in geographic coordinates, with event verification and genuine KP locations. This is still a declared bounded screening implementation. Tolerance-sensitive contact, a sharp undefined tangent or unsupported overlay scope can remain unknown. Raw degrees or a local projected chord are never claimed to be a WGS84 separation.

The continuous proximity operator bounds whole parameter intervals using ellipsoidal distances and conservative geodesic arc-length balls. A witness gives an upper bound; triangle-inequality bounds keep the unsampled remainder in scope. The tests confirm actual interior minima and declared-threshold behavior for the listed synthetic cases; they are not a formal proof of all floating-point geometries or arbitrary near-antipodal curve uniqueness.

The route depth function is an explicitly bound piecewise-linear KP profile or declared waypoint approximation. A geometrically exact crossing does not create a valid depth where data are missing/stale or replace the declared datum. Whole-path depth filters apply to complete eligible subdomains, including closed isolated points. Body positions use the core-resolved leading route KP, distinct from physical manufacturing station and finite body's actual deployed shape.

The 2D slope window has real queried supports, same-source guards, NoData preservation, a declared inscribed polygon fringe and `continuous_bed_verified=false`. Its triangular secants may exceed a smooth one-sided derivative across a kink. `sampled_pass` does not certify the continuous sea floor, a plow's stability or undiscovered hazards between probes. A geometric centroid is a locator with null depth; actual query provenance is retained separately in the sampled witness.

`clear` describes only the declared geographic predicates/domain and supported bounded model. Missing references, absent/stale data, failed budgets and incomplete geometry must not become an empty pass. `violations` can coexist with unresolved diagnostics, and errors retain actual locations only when a real one is available. Analysis/import does not save or replace a partial workspace. Applying a candidate remains a separate complete transaction; shared manufacturing relationships and revision semantics must survive it.

At this review's targeted run there was no remaining reproduced failure in the 51 cases. The implementation owner was still performing additional development review; later edits require their own targeted/full evidence rather than treating this document as a frozen final release validation.
