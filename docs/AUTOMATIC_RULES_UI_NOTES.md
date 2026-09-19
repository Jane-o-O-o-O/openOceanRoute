# Automatic Rule Checker UI — OceanRoute 0.10 development

This document records the implemented UI contract and targeted development
evidence. It does not declare a release or replace the final complete browser
gate. The 116 historical objects recorded in
`resources/validation/development_0.10_frozen_artifacts.json` remain frozen.

## Integration contract

`web/src/AutomaticRulesPanel.tsx` exports the default panel and
`AutomaticRulesContext`:

```ts
type AutomaticRulesContext = {
  workspace: Workspace;
  contextKey: string;
  pending?: boolean;
  onApply: (
    candidate: Workspace,
    expectedContext: string,
    isCurrent: () => boolean
  ) => Promise<boolean> | boolean;
  onLocate?: (
    location: RuleErrorLocation,
    options: {circle: boolean; center: boolean},
    expectedContext: string,
    isCurrent?: () => boolean
  ) => Promise<boolean> | boolean;
};
```

The panel also accepts `notify(message, error?)`. `EngineeringTools` receives
the same contract through its optional `automaticRulesContext` prop and exposes
the `automatic-rules` tool. The zero-path workspace opens the same panel directly
inside its rules dialog. App, EmptyWorkspace and workspace application belong to
the root integration; the panel never saves a workspace or mutates inventory.

Rules are declared in `workspace.automatic_rules`, each with its own `path_id`.
The existing per-project `slope_rules` are preserved independently. Shared storage
does not imply applying every rule to every path.

POST endpoints are `/api/automatic-rules/catalog`, `/check`, `/import` and
`/export`. The check wrapper is `{workspace, rules, checks, scope}`; actual
`results`, `errors`, `summary`, `metadata`, `budget` and `assumptions` are inside
`checks`. Import adds its policy `report` and `warnings`. Export returns the open
`package` and strict finite JSON `text`. No proprietary binary compatibility is
claimed.

## Editing, candidates and snapshots

Add, rename, edit, enable, disable and delete update a local rule draft. Checking
and parsing imports create complete, unapplied candidates. Applying commits the
whole declaration list through the root callback, with one undo entry. A separate
explicit Save writes the workspace and revision.

Crossing comparisons `lt`, `gt`, `le` and `ge` are displayed as error-trigger
predicates. `match_mode` exposes AND/OR without inferring a violation direction
from a maximum/minimum label. The acute crossing angle has a 0–90° domain;
`body_distance_mode` is `horizontal` or `route_kp` and never means cable KP.
Empty conditions report actual geometric contacts.

Proximity permits only GIS targets around the entire path and permits surrounding
seabed slopes only around bodies. Switching target groups removes inapplicable
GIS or slope parameters from submitted rules. `water_depth_m.min_m` is a finite
nonnegative value; `max_m: null` means no upper depth limit. No missing measured
depth is filled with zero.

GIS catalogs preserve number and string IDs separately. Whole-layer selection
uses `feature_ids: null`; explicit native IDs use typed `feature_ids`, while
objects without unique native IDs use `feature_indexes`. The UI can narrow whole
layers and imported multi-ID declarations, remove preserved missing references
and reselect actual features. Hidden layers remain selectable and checkable.

Check/import/export responses are accepted only while their operation sequence,
mounted panel and entire input/workspace snapshot remain current. Applying also
passes a live `isCurrent()` closure to the root asynchronous callback. Locating
passes the same guard so a delayed cross-path `set_active` cannot commit after
editing or leaving the tool.

The shared `NumberField` normally commits values on blur. The rules panel
therefore observes real editor/import/budget input events and increments an
independent UI revision immediately. A number typed during a held response
invalidates that candidate even before blur. Sorting and location-display
checkboxes do not invalidate engineering input. Workspaces and complete rules
remain unchanged when stale responses are discarded.

## Read-only map and profile locations

`MapView`, `ProjectedMapView` and `ProfileChart` accept optional
`ruleLocation: RuleLocationView | null`:

```ts
type RuleErrorLocation = {
  path_id: string;
  kp_m: number;
  longitude: number;
  latitude: number;
  depth_m: number | null;
  radius_m?: number;
  radius_center?: {longitude: number; latitude: number};
  [key: string]: unknown;
};
type RuleLocationView = {
  location: RuleErrorLocation;
  circle: boolean;
  center: boolean;
  token: string;
};
```

The root binds a view to the accepted workspace-document snapshot and may first
activate its actual path. No fake selected RPL point is created, moved or inserted.
Reference errors without actual coordinates have disabled location buttons.

Map markers use the actual error-instance longitude/latitude. Profile markers
use the actual subject KP and returned instance depth. The profile does not
interpolate a missing instance depth or substitute route depth for an off-route
point. A geometry-only child centroid has `depth_m: null` and
`location_kind: geometric_child_centroid_not_queried`; the main view displays
missing instance depth. Actual queried depth and provenance remain in the
expanded `violation.sampled_witness` evidence.

`automaticRuleLocation.ts` obtains every one of 32 boundary vertices through the
real WGS84 geodesic direct API. Its center is `radius_center` when supplied;
otherwise it is the actual instance location. Thus terrain-child markers remain
at their geometric centroids while their radius polygon stays around the original
queried body. These are finite display polygons, not continuous-distance
certificates.

The geographic display unwraps boundary longitudes around the instance and
rejects incomplete circles, its ±85° display-domain overflow and unsafe longitude
spans. The projected display batches actual vertices through
`/api/coordinates/transform`; any failed vertex prevents a partial polygon. Both
displays report failure explicitly. The manual CRS map display is independent of
the automatic checker, which uses the declared GeoJSON topology and true WGS84
route/continuous-distance model rather than a local map projection.

## Declared numeric domains

The UI presents these current declared domains. Slope angles are below 90°;
crossing-angle predicates still permit 90°.

| Field | Minimum | Maximum |
| --- | ---: | ---: |
| Crossing angle trigger / degrees | 0 | 90 |
| Body-neighborhood terrain slope threshold / degrees | 0 | 89.999999 |
| Inline/side slope threshold / degrees | 0 | 89.99999999999999 (largest JS double below 90) |
| Body-slope probe spacing / m | 0.001 | 100000 |
| Geometry tolerance / m | 0.01 | 100 |
| Route maximum segment / m | 10 | 10000 |
| Route subcurve domain cap `max_curve_chunk_m` / m | 1000 | 200000 |
| Rule count | 1 | 512 |
| GIS feature count | 1 | 100000 |
| Event count | 1 | 100000 |
| Logical work units | 1 | 200000000 |
| Complete report JSON bytes | 1024 | 67108864 |

Counts use integer control steps. Backend validation remains authoritative and
rejects a whole malformed or over-budget operation. The route-subcurve field is
not a projection-radius control. The delegated inline/side normalizer explicitly rejects exactly 90°; its UI
maximum is the largest JavaScript double below 90 so legal near-vertical imported
values remain editable. The body-neighborhood threshold follows its separate
exact 89.999999° backend cap. Legacy project schemas are not changed.

## Actual targeted development evidence

The test source is `web/tests/automatic-rules.spec.ts`. Real requests ran against
the isolated development API and same-origin production build at port 8776.
Response-delay tests call `route.fetch()` and then hold the actual response; they
do not invent successful checker or application payloads.

| Actual report | Outcome | Scope |
| --- | --- | --- |
| `resources/validation/development_0.10_automatic_rules_browser_first.json` | 6 passed, 1 failed; 49.139 s | Seven initial unique UI scenarios. |
| `resources/validation/development_0.10_automatic_rules_browser_empty_repair.json` | 1 passed; 2.236 s | Corrected the initially invalid zero-path fixture. |
| `resources/validation/development_0.10_automatic_rules_browser_radius_semantics.json` | 1 passed; 7.517 s | Strengthened real terrain-child/witness/radius-center assertions. |

These are **eight passing executions across seven unique scenarios**, with the
original failed execution retained. They are development evidence, not a single
all-pass run of the final source and not the formal complete 99-test gate.

The first failure occurred before the empty-workspace UI ran: its test fixture
supplied `cable_types: []`, violating the existing `WORKSPACE_CABLE_TYPE` resource
declaration. The repaired fixture explicitly declares only a GENERIC material ID
and name, without supplying fabricated physical properties. Zero paths, null
active path, preserved missing references, saving and undo then passed.

The successful scenarios cover explicit comparisons and AND/OR; typed IDs and
indexes; proximity-group restrictions and distinct zero-distance objects; real
2D body slopes and both route-slope directions; missing-reference repair; import
retain/active/explicit binding, append/replace, duplicate rejection/rename and bad
schema rejection; complete apply, save, export and undo with unchanged path and
inventory arrays; geographic/profile/projected locations; real budget rejection;
held check/apply responses; unblurred numeric edits; panel unmount; and held
cross-path activation.

The strengthened terrain case selects an actual `terrain_slope` child instance,
asserts its null depth and actual `radius_center`, and observes all 64 real
geodesic requests across the two map displays. Every circle request uses the
original body center and declared 100 m radius. Its profile visibly retains
missing instance depth. The source test also now checks numeric KP ordering and
requests a predicate-editor screenshot for the forthcoming complete gate.

Screenshots are in `web/artifacts/development-0.10-target/` with
`automatic-rules-0.10-*.png` names. The explicit-predicate results and true WGS84
neighborhood projected-circle screenshots were visually inspected; the latter
shows the off-center geometric child marker, correctly centered body circle and
missing-depth profile label. Final routine form-range changes and the final
budget-field rename receive a TypeScript/Vite build check; the complete final
browser regression and visual review remain owned by root.
