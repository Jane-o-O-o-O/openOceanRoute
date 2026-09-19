# Automatic geographic rules: independent development implementation

This module implements declared geographic checks over a schema-2 OceanRoute
workspace. It is an independent interpretation of public product documentation,
not Makai native-file compatibility, a reconstruction of its private algorithm,
or a certificate of continuous terrain or survey accuracy. Frozen 0.9 artifacts
are unaffected.

## Public evidence and explicit choices

The supplied manual's physical pages262–272 (printed254–264) describe three rule
groups: Path Crossings, Proximity, and Slope; KP-range declarations; enabled
rules; per-instance results sortable by path/type/KP; error localization; and
rule import/export with active-path binding and missing-reference repair.

Physical263–264 describe crossing angle, water depth and distance to cable
bodies. The actual physical264 screenshot has error triggers **angle less than**,
**depth more than**, and **body distance less than**, in degrees/metres. Its prose
uses max/min terminology that must not silently invert the screenshot triggers.
Physical265–266 define the proximity matrix: a whole path queries GIS features;
bodies/altercourses/transitions can query bodies/altercourses/transitions/GIS;
only bodies can query surrounding slopes. KP, depth and radius are in metres.
Physical266 also selects inline/side/both slope checks. Physical267–268 show
per-instance errors and map circles; physical269–272 cover portable rule
configuration and retaining missing references.

The public source does not define Boolean combination/equality, acute versus
oriented crossing angles, horizontal versus along-route body distance, exact
terrain differentiation, or native serialization. OceanRoute makes each
supported choice explicit. No access to paid/original GIS resources is implied.

## Functions, persistence and requests

`oceanroute.automatic_rules` exports:

- `normalize_automatic_rules(rules, *, max_rules=512)`: strict declaration-only
  validation. Missing references are valid saved declarations.
- `automatic_rule_catalog(workspace)`: paths and original source-feature
  identities; typed native IDs and explicit source indexes remain distinct.
- `check_automatic_rules(workspace, config=None, *, analyses=None)`: read-only
  checks. Internal workspace callers can provide already current path analyses;
  supplied route signatures are checked. Direct calls obtain current analyses
  through workspace validation, without recursively running this checker.
- `export_automatic_rules(workspace, config=None)`: `{package,text}`.
- `import_automatic_rules(workspace, package, config=None)`: complete candidate,
  normalized rules, actual checks, import mapping report and warnings.

Root integration exposes POST `/api/automatic-rules/catalog`, `/check`, `/export`
and `/import`, with `{workspace,config?}` and additionally `package` for import.
The complete endpoint names are `/api/automatic-rules/check`,
`/api/automatic-rules/export`, and `/api/automatic-rules/import`.

`workspace.automatic_rules` is the shared rule list; every rule references one
`path_id`. Existing per-path `project.slope_rules` remain intact. Check returns
`{workspace,rules,checks,scope}`; only the candidate's `automatic_rules` is
changed. Import returns `{workspace,rules,checks,report,warnings}`. Neither
operation saves SQLite, changes a route/source/inventory/fixed constraint, forks
an assembly, changes an active path, nor discards `saved_revision`. Explicit
application and the normal whole-workspace revision-protected save remain
separate operations. Reopening a workspace recomputes current evidence; the
results are not a persistent engineering certificate.

The import package is strict finite JSON:

```json
{"schema":"oceanroute.automatic-rules/v1","schema_version":1,"rules":[]}
```

Import config: `binding`=`active_path` (default), `retain`, or `explicit` (requires
`path_id`); `mode`=`append` (default) or `replace`; `duplicate_ids`=`reject`
(default) or `rename`. Without an active path, default binding keeps original
references. Missing paths/layers/features remain editable `reference_error`
results. Renaming uses a deterministic unique suffix. Text and object packages
both have a16MiB limit. Export optionally accepts a unique `rule_ids` list.

## Declaration semantics

Each rule has unique string `id`, `name`, boolean `enabled`, string `path_id`,
`kind`, `start_kp_m` (default0), `end_kp_m` (defaultnull). KPs refer to route surface
length, not manufactured cable length. A null end remains null in storage and
resolves against the current route on every check. Numeric ranges must be
ordered; a numeric range beyond a shortened route is retained and reported
incomplete. A start beyond the current route yields `RULE_RANGE_EMPTY`, not a
clamped pass. Disabled declarations remain present and are not geometrically
checked.

Crossing declarations contain `selectors`, `conditions` (at most one each for
`angle_deg`, `depth_m`, `body_distance_m`), `match_mode`=`all|any`, and
`body_distance_mode`=`horizontal|route_kp`. Comparisons `lt|gt|le|ge` explicitly
trigger errors; empty conditions report all confirmed contacts. `angle_deg` is
an acute unoriented0–90° angle between unambiguous incident tangents. A sharp
GIS vertex, overlap, containment, or path-end touch has no invented zero angle.
Water depth comes from the admitted current route profile, including an
explicitly labelled waypoint approximation if no imported profile takes
precedence. S57 raw Z is never engineering bathymetry. A stale supplied profile
is not replaced by waypoint depths. Horizontal body distance uses WGS84 surface
distance to core-resolved leading body positions; `route_kp` uses absolute surface
KP separation. Missing bodies do not produce infinity or a fabricated zero.

Predicate evaluation uses three-valued logic: `false AND unknown` is false;
`true OR unknown` is true; otherwise unresolved predicates remain unknown.
Known violations and unresolved other selections/components are retained
independently. Floating equality uses only a small ULP guard, not an engineering
angle tolerance.

Proximity declarations contain `around`=`path|bodies|altercourses|transitions`,
`targets`, `distance_m`, `water_depth_m:{min_m,max_m}`, and GIS selectors when
GIS is a target. `min_m` is finite and nonnegative; `max_m` can be null. The default
is `{min_m:0,max_m:null}`. Both depth-filter endpoints are inclusive: an exact
single depth crossing is a legitimate point subject, even when there is no
positive eligible interval. Valid adjacent profile intervals are analytically
clipped to the depth range. Independently known samples between NoData gaps are
retained, while the surrounding gaps still produce incomplete evidence.

All selected target groups are checked independently; object pairs are not
collapsed into a single average or closest object. A continuous path/feature
primitive pair is represented by its bounded minimum-distance witness in each
checked route/depth interval, not an infinite list of all nearby continuum points. Identical self-object pairs
are excluded; distinct colocated bodies remain actual zero-distance pairs.
Subject KP/depth filters do not silently filter target objects. Radius is
inclusive; it can be zero for confirmed contacts. A nearest-distance bound
straddling the radius is unknown, rather than forced inside/outside.

Bodies use core-resolved route KPs, including physical-cable KP, slack and
allowance mapping. Actual incoming geodesic arrival and outgoing initial
azimuths define altercourses; rhumb azimuth is constant. Remote initial azimuths
and finite chord averages cannot manufacture high-latitude turns. Both directly
adjacent legs must have positive length; repeated vertices do not invent two
turns or a transition through a zero-length virtual cable run. Transitions use
adjacent actual cable-type changes at such vertices.

Slope declarations have `slope_basis`=`inline|side|both` and the corresponding
absolute maximum angles, each in0≤angle<90°. They delegate to the existing
[slope-rule contract](SLOPE_RULES_NOTES.md), preserving current profile/side-slope
signatures, coverage gaps, source boundaries and sampled-only results.

## Native GIS topology and continuous distances

Selectors contain `layer_id` and exactly one of `feature_ids` or
`feature_indexes`. `feature_ids:null` selects the entire layer. Number1 and
string`"1"` are distinct; large native integers are not rounded through binary
floating point. Missing/null native IDs use explicit source indexes. Duplicate
native IDs are ambiguous and produce reference errors instead of silently
selecting one. Source indexes are not disguised as strings such as`index:3` that
can collide with real IDs. Hidden/transparent layers still participate.
Catalog rows retain original IDs/indexes, uniqueness, geometry type and an exact
selector. Catalog and selected-feature source signatures bind identities to the
current source content.

GeometryCollection and multi-geometries are recursively flattened with their
`primitive_path`. Polygon holes remain holes. Original GeoJSON edges are linear
in their original longitude/latitude coordinates. A native179→−179 edge travels
through longitude0; it is not silently reinterpreted as a short date-line cable.
Only whole-geometry360° translations align a geographic chart to a route
fragment. Source coordinates/Z/attributes are never rewritten.

Route curves use the core's actual WGS84 geodesic/rhumb interpolation. Complete
curves are split into bounded arc-length domains; within each domain they are
adaptively rendered, enforcing a maximum segment length and checking metric
quarter/midpoint departure from the geographic-linear chord. Shapely overlays
whole rendered segments against whole native primitives, including polygon
boundaries and holes. Contacts distinguish crossing, touch, overlap, area entry,
area exit and containment. Proper line contacts are refined on the actual route
curve against the native edge. Rendered contacts that cannot be confirmed on
the real curve become `GEOMETRY_CONTACT_UNVERIFIED`. Vertex/tangent ambiguity
cannot manufacture a crossing angle. Overlay above85° latitude is unresolved;
continuous metric point/line proximity remains usable, while polar polygon
containment is explicitly unknown.

This adaptive topology is a declared geographic screening approximation. The
sampled chord-departure checks are not a formal interval bound on every
unsampled point. Near contacts within `geometry_tolerance_m` are therefore
explicitly unresolved; `clear` describes this bounded screening model, not
survey-grade topological proof. Degrees are used only for native topology and
analytic native tangents, never substituted for metres.

Distances are stronger than nearest-vertex sampling. A continuous route interval
lies in a WGS84 geodesic ball of radius half its arc length. A native geographic-
linear edge interval has a conservative length bound

```text
Rmax * hypot(delta_lat_radians, cos_nearest_lat * delta_lon_radians),
Rmax = 6399600 m.
```

This exceeds both WGS84 surface metric radii over the interval. The geodesic
center distance minus both ball radii is a lower bound by the metric triangle
inequality. Every actual evaluated pair supplies an upper bound. A priority
queue subdivides complete parameter intervals until the global upper/lower gap
is at most a quarter of the declared metric tolerance, or rejects on budget.
An additional conservative latitude-separation bound uses the minimum WGS84
meridional radius. Local minimization only improves an actual upper-bound
witness; it does not certify global optimality or replace interval coverage.
Polygon containment gives actual zero separation, while holes do not.

## Two-dimensional body slope windows

`targets:["slopes"]` is permitted only around bodies and requires
`distance_m≥.001`, `slope_threshold_deg` in0..90 exclusive90,
`slope_probe_spacing_m` (default50), optional `slope_vertical_datum`.
[Terrain slope neighborhoods](TERRAIN_SLOPE_NEIGHBORHOOD_NOTES.md) actually query
the shared source library over a circular neighborhood; no route-only profile
is extruded to stand in for the surrounding seabed.

All three original corner probes, three edge-midpoint probes and the family
centroid are real queries. Each family is split into six child triangles, whose
slopes use three actual queried vertices. Any missing/seam family support makes
all its children unknown. This catches sampled interior ridges that an endpoint
average would miss. The inscribed mesh has an explicit circle-fringe area
shortfall. NoData/source boundaries and partial triangles remain incomplete;
otherwise a nonviolating result is `sampled_pass`, never continuous-bed
certification.

Child `location` is its geometric centroid with `depth_m:null` and
`location_kind:"geometric_child_centroid_not_queried"`. `sampled_witness` preserves
a real family-centroid vertex's depth/source/query identity. Each violation also
includes its three actual `queried_vertices`, `vertex_indices` and family
`support_indices`, allowing reconstruction of the reported triangle gradient.
Nonviolating windows retain actual quality/counts/maximum and metadata rather
than falsely claiming to return every probe/facet. `radius_center`
contains the checked body center, so a map circle is not accidentally centered
on the offending child. Error KP is the anchoring body's route KP; an off-route
triangle centroid has no invented route projection KP. Maximum slopes are not
averaged across the neighborhood.

## Results, signatures and failure

Each result includes `rule_id`, `path_id`, `path_name`, `kind`, enabled state,
requested/resolved KP range, `violations`, `diagnostics`, `coverage`. Statuses are
`disabled`, `reference_error`, `violations`, `clear`, `sampled_pass`, `incomplete`,
`unknown`. A real violation can coexist with missing coverage, preserved in the
same row. Flattened `errors` have stable rule-instance IDs, actual location or
null for missing references, and status`violation` (singular),`reference_error`
or`unknown`. This differs intentionally from the plural per-rule`violations`.
The complete evidence remains attached to each row; no fake coordinate is used
for unresolved references.

`metadata.rules_signature` binds canonical normalized declarations.
`geometry_signature` binds actual route curves/positions and source geometry
identities, excluding display state. `input_signature` additionally binds the
broader project/material/profile/source declarations; editorial project changes
can change this broader binding. Layer visible/opacity/display_order are omitted
from engineering binding. Numeric int/float equivalents and negative zero are
canonicalized. These hashes provide stale-evidence consistency, not provenance
authentication. The UI additionally guards its complete document/config snapshot
and pending request identity before applying a whole candidate.

`AutomaticRuleEvaluationError(ValueError)` provides a typed`code` for declared
budget/scope failures. Direct endpoints reject the whole report (422). Workspace
aggregate analysis may report unavailable checks while leaving saved declarations
editable; it must not convert that failure into an empty clear result. Unexpected
programming errors are not caught as screening outcomes.

## Budgets

| Config | Default | Hard maximum |
|---|---:|---:|
| max_rules |128|512|
| max_features |20000|100000|
| max_vertices |250000|1000000|
| max_pairs |1000000|10000000|
| max_events |10000|100000|
| max_work_units |30000000|200000000|
| max_output_bytes |16MiB|64MiB|
| geometry_tolerance_m |1m|100m (minimum.01m)|
| max_segment_m |1000m|10000m (minimum10m)|
| max_curve_chunk_m |100000m|200000m (minimum1000m)|

`max_curve_chunk_m` really bounds each checked route subcurve's arc length; there
is no inactive projection-radius option. Normalization/storage and workspace
aggregate checks support512 rules. Direct checker default128 can be explicitly
raised to512; no list is truncated.

The checker admits a finite32MiB workspace. Whole catalogs are capped at100000
features/16MiB output. Selected primitives, adaptive vertices, actual interval
pairs/events and terrain costs have cumulative logical counters. Retained
violations/diagnostics reserve duplicated error/warning JSON capacity before
unbounded accumulation; the final report records its exact compact UTF-8 JSON
byte count. Terrain windows are preflighted across all rules before any terrain
query: at most200 windows/50000 query points, cumulative declared work/output
limits. Source preparation/cache costs remain distinct from coordinate reuse.
A budget failure rejects the complete report, never a truncated clear result.

These counters bound this checker and its terrain calls, not CPU FLOPs, a hard
wall-clock deadline or peak RSS. Workspace admission/manufacture analysis has
its own existing validation limits; new automatic-rule entry points bypass
only the duplicate legacy GIS screen through a private internal flag, retaining
all material/profile/currency/relationship validation; a provided current analysis avoids repeating
that admission. The returned `budget.admission_exclusions` explicitly records
the preceding workspace JSON size checks/canonical serialization, shared-source
normalization, core route densification/profile/material/manufacture/currency/
relationship validation and pure source-GIS admission. These are not charged as
this checker's logical work; terrain preparation performed inside its sampler
remains included in the declared checker terrain costs.

Pure GIS admission retains the existing policy: malformed structure and
out-of-bounds coordinates on admitted valid geometry reject the input; invalid
topology can remain as a warning/skipped legacy feature, including when it is
unselected. The new checker reports a selected invalid topology as unresolved
evidence, preserving valid siblings; it does not silently certify that feature.
This admission is also retained for an empty workspace at the new endpoints.
Root HTTP wrappers separately cap the complete workspace-plus-
report response at64MiB; `max_output_bytes` alone is the checker report limit.

## Meaningful verification scope

The new independent tests cover equatorial true crossing KP/angle, numeric/string
native IDs and missing/ambiguous references, recursive collections, polygon
holes and containment, V-vertex touches, overlap ambiguity, actual date-line
route versus native long-way edges, high-latitude true-straight turn, repeated
vertices, full-segment metric distance bounds, closed single-depth filters,
NoData without interpolation, physical-body KP through slack/allowance,
compound targets/self-exclusion, real two-dimensional planar gradients, sampled
witness versus geometric centroid, source seams, NoData windows, prequery
budgets, finite serialization, imports/revisions and512-rule declarations.
Root HTTP tests separately exercise complete candidates, actual new-app storage
ownership/reopen/restore, optimistic revision conflict and strict422 failures.
Tests are synthetic mathematical/engineering cases; they do not validate every
IHO chart, field survey, original native rule database, continuous terrain,
private Makai algorithm, or operational acceptance standard.
