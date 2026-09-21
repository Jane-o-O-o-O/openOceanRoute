# OceanRoute 0.11 altercourse UI

## Scope and current validation status

`EngineeringTools` has a separate **转向点处理 / SPLIT AC / RADIUS AC** entry.
The existing **工程拆分 / SPLIT PROJECT** retains its separate meaning.
`AltercourseTools` previews one selected actual internal `point_id`; endpoints
are not silently replaced by another point. Split AC uses
`max_turn_angle_deg` (strictly between 0 and 180) and
`min_turn_distance_m` (.001–1,000,000). Radius AC uses `radius_m`
(.001–1,000,000). The service validates rigid-point membership, real geometry,
available leg length, material boundaries and manufacturing domains.

The component, App transaction integration, actual-geometry consumers and
eight browser scenarios have been implemented. A real `tsc -b` succeeds.
The eight scenarios passed against the actual same-origin 0.11 development
service on port 8778, with one worker and zero retries. Two later focused runs
repeated the same Radius scenario after screenshot framing and projected
layout repairs. These are eight distinct scenarios, not ten. They are
development checks, not the final frozen full regression. The frozen
0.1–0.10 artifacts are outside this edit.

## Component and API contract

The panel receives the current materialized `project`, `analysis`,
`analysisCurrent`, and an `AltercourseContext`:

```ts
{
  document: WorkspaceDocument;
  contextKey: string; // exact JSON of the complete document, including draft
  selected: string | null;
  selectionRevision: number;
  pending: boolean;
  onSelectPoint: (pointId: string) => void;
  onApply: (
    candidate: AltercoursePreview,
    expected: AltercourseSnapshot,
    isCurrent: () => boolean,
  ) => Promise<boolean>;
}
```

`AltercourseSnapshot` captures the source document **object**, its JSON,
selected `point_id` and `selectionRevision`. App maintains the revision across
all shared sidebar, map, RPL and profile selection changes. Selecting another
point and selecting the original again invalidates the earlier candidate.
The panel separately increments a document-identity revision when a new
document object appears, even if undo restores equal JSON.

Preview posts to `/api/workspace/altercourse-preview`:

```json
{
  "workspace": "complete current Workspace object",
  "path_id": "actual active path ID",
  "kind": "split or radius",
  "config": "the operation-specific declared parameters and point_id"
}
```

The actual response contains `workspace`, materialized `project`,
`analysis.active_path_analysis`, workspace `report`, `tool_report`, `warnings`,
normalized `operation`, and `result_selection_point_id`. Before admitting a
preview, the UI checks workspace identity, saved revision, active path,
operation identity, real geometry and resulting point membership.
`tool_report.changed === false` is displayed as a no-change result; its Apply
button is disabled, so no meaningless undo step is created. Full service JSON
can be downloaded; this does not save the workspace.

## Input and asynchronous transaction guards

`NumberField` commits on blur. The panel listens to actual `input` events on
its form and increments `uiRevision`, so even an unblurred numeric draft makes
an in-flight preview or apply obsolete. The full signature also binds the
parameters, mode, selected point/revision, exact document JSON and object
revision. Sequence and mounted checks discard results after a menu switch or
unmount. A changed save revision invalidates previews without changing routes.

App applies only the selected candidate path's **stored** `project` through
`/api/workspace/action` using `action: update_path` and
`assembly_policy: auto_exclusive`. It does not publish arbitrary candidate
workspace fields, send shared-library updates, or save. Before and after the
await, App checks exact document identity and JSON, workspace ID,
`saved_revision`, `active_path_id`, actual selected internal point/revision,
no unresolved draft or pending transaction, loading state, mounted state,
operation ownership and the panel's latest `isCurrent` predicate. The service
then validates the path against the complete current workspace. Result
selection must exist in the real returned route.

One accepted response adds one complete prior `WorkspaceDocument` to undo,
clears redo, publishes the complete server workspace, resets analysis and rule
location, selects the actual returned point and marks the document dirty.
Only the user's **保存** action posts to the persistent workspace store.

## True circular geometry and truthful depth

Radius AC persists a per-leg descriptor:

```ts
{
  type: 'circular_arc', schema_version: 1,
  center: [longitude, latitude], radius_m,
  start_azimuth_deg, sweep_deg,
}
```

The UI does not create an arc from a cloud of RPL points. Candidate and active
maps use the service's actual `route_geometry`/`route_geometry_segments`.
Geographic maps draw each antimeridian segment separately, unwrap display
longitudes consistently, and fit the actual analyzed geometry. An arc without
current analysis is explicitly waiting; no substitute straight chord appears.
An actual arc reaching beyond the geographic display domain (85° latitude) is
explicitly withheld together with its markers; the map does not clip it into a
false flat shape. The user can choose a suitable projected view. This threshold
is a display-domain guard, not a limit on the stored WGS84 geometry.
Projected maps transform the same actual segments and hide stale arc geometry.
Their initial fit measures the drawable layout after metadata is laid out and
commits the size and camera together in `useLayoutEffect`, before exposing the
SVG or allowing edits: revealing metadata must not clip endpoint markers or
leave a temporarily unfitted plane available for pointer operations.
An actual browser assertion verifies every stored route marker, including its
circle radius, lies inside the projected SVG view box after the initial fit.
Stored route points remain the only editable route markers.

The candidate arc table shows persisted radius, signed sweep, and actual
analysis leg surface length. Split evidence displays actual turn angles and
internal distances. Manufacturing before/after quantities, per-cable changes,
profile/side-slope invalidation, and real service warnings remain visible.
The RPL adds read-only outgoing curve, radius and signed sweep fields; its KP
and cable KP come from actual analysis. Profile charts use backend KP/depth
samples. Null depth remains null/“—”; it is never converted into a measurement
at zero or certified from an obsolete profile.

Ordinary App coordinate changes, geographic dragging, deletion of an arc
endpoint and straight-point insertion within an arc are explicitly rejected.
The projected-apply guard similarly rejects changed/deleted arc endpoints.
Existing geometry is retained. The service independently validates endpoint
binding; the UI does not silently remove or rewrite the arc descriptor.

## Executed independent browser scenarios

All eight scenarios use real HTTP results and actual visible DOM controls.
Latency guards hold an already obtained real response, rather than fabricating
a success body. The endpoint environment must bind browser assets and API to
the same actual 0.11 service. New screenshots use an `altercourse-0.11-` name
under a designated new artifact directory.

1. Split AC: actual selected internal ID, reviewed complete candidate, no
   preview/application persistence, one undo, redo, explicit save, and retained
   other paths/shared data/rule declarations/workspace extensions.
2. Radius AC: one stored true arc, additional analyzed samples with nonzero
   departure from the straight chord, actual geographic/projected display,
   RPL and profile, download, explicit save and reopen.
3. Actual endpoint prohibition, real unreachable-radius HTTP rejection, and
   all-null unknown depth preserved in an accepted arc and profile.
4. Real delayed preview rejected by an unblurred numeric change, selection
   away-and-back, or a completed Save that changes revision.
5. Real delayed `update_path` rejected by an unblurred number change or panel
   unmount; an explicit later save preserves original routes and inventory.
6. Candidate application invalidated by selection or actual active-path
   changes, retaining the other stored paths.
7. Split below the declared maximum angle returns `changed: false`, disables
   Apply, and produces no undo operation.
8. A fixed shared manufacturing association permits Radius AC with unchanged
   inventory and physical length; a flexible shared association receives the
   real `WORKSPACE_SHARED_ASSEMBLY_CHANGED` rejection and keeps paths and
   inventory intact. No silent fork or shared-library mutation occurs.

The fixtures are explicitly synthetic. Their known-depth case uses a flat
1,000 m synthetic grid; the unknown-depth case supplies null depths. No case
claims external survey data or treats synthetic depths as measurements.

## Development execution and visual review

The native JSON and stdout logs are retained in `resources/validation`:

| Report stem | Actual result | Native duration | Purpose |
| --- | --- | --- | --- |
| `development_0.11_altercourse_browser_first` | 8 passed; 0 failed, skipped or flaky | 23.283636 s | All eight distinct scenarios |
| `development_0.11_altercourse_browser_view_repair` | 1 passed; 0 failed, skipped or flaky | 4.856658 s | Same Radius scenario, corrected actual map/profile screenshot framing |
| `development_0.11_altercourse_browser_fit_repair` | 1 passed; 0 failed, skipped or flaky | 5.330907 s | Same Radius scenario, actual projected layout and endpoint-domain assertion |

The first functional run passed. Its geographic screenshot captured the
manufacturing table rather than the map, so it was insufficient visual
evidence for the map/profile claim. The focused framing repair retained the
first files and captured the actual preview-map container. Independent image
review then found the projected view's initial fit could clip endpoint circles
by approximately 1–2 pixels when metadata reduced drawable height. The fit was
repaired, a real SVG-boundary assertion was added to the existing Radius case,
and that case passed again. The final actual geographic/projected images show
the true arc and profile gap together; the saved RPL shows four stored points,
radius 150 m, signed sweep −90°, and null inserted/relocated point depths.

All 17 PNGs from the three development artifact directories were individually
opened with `view_image`, including the superseded framing and fit images.
The inventory, hashes, exact reviewed paths and per-image observations are in
`development_0.11_altercourse_ui_evidence.json`. No screenshots were synthesized,
no first-run asset hash was reconstructed after a rebuild, and the reports are
not presented as formal release evidence.

During an independent direct numeric check, the legitimate positive subnormal
angle `5e-324` exposed an overflowing division in the Split solver's turn-count
calculation. The planning engine owner repaired the budget check before the
division and added a real backend regression. This was a concrete numeric
defect discovered before browser execution, not a failed Chrome scenario.

The earlier development frontend compiled into `web/dist-0.11-next` with main
asset `assets/index-CwSG5eVA.js`. That report records the source and asset hashes
at that development stage, not the subsequently repaired build.

## First complete gate and original-test repairs

The first actual full same-origin Chrome run on 8779 executed all 107 scenarios
with one worker and zero retries. Native result: 101 passed / 6 failed, no
skipped or flaky cases, 597.749506 s; wall duration 598.067108 s. All 190 before
and after input records were unchanged. Its original JSON/log/execution/inputs
remain under `development_0.11_final_browser*`; it is failed evidence and is
not copied as the canonical release gate. Its 100 artifact-directory PNGs,
five exact retained copies of legacy tests' hardcoded outputs and six failure
screenshots are inventoried separately. Only 30 first-gate images were actually
viewed; the first-gate inventory explicitly labels the remainder hash-only.

The actual failures exposed two frontend defects. First, a deferred double-RAF
fit exposed a drawable projected plane before fitting it. Existing opacity
checks read the unfitted coordinates, and four existing drag scenarios could
use a temporarily wrong screen location and never issue an inverse request.
The fix lays out result metadata, measures the real host and commits the fit
before the SVG becomes visible/editable; a geometry refresh after an edit
continues to preserve the engineering viewport. Second, a rapid Split
apply/undo path switch encountered a Leaflet animated-zoom callback after map
removal. Engineering geographic maps disable zoom/marker zoom animation, use
nonanimated programmatic fits, guard ResizeObserver disposal and detach the
grid's move listener during cleanup. No private Leaflet state is rewritten.

The original tests and fixtures were not changed to hide either failure.
The six exact failed titles were independently listed and then executed as
`development_0.11_map_repair_selected_browser`: 6 / 6 passed, no skipped,
failed or flaky tests, native 25.491720 s, wall 25.674630 s; 190 inputs unchanged.
The prior selector attempt, `development_0.11_map_repair_browser`, matched zero
tests because anchoring the title omitted Playwright's filename prefix. That
failed orchestration report is retained; the corrected suffix-title selector
was actually listed as six tests in three files before its real execution.
This six-case repair gate used the existing 8779 API owner and is not the full
107-case verification on a fresh final backend.

TypeScript and Vite genuinely succeeded after the two-component repair. Both
`web/dist-0.11-next` and the new, not-yet-issued `web/dist-0.11-release` contain
eight actual files; their main asset is `assets/index-mx8xlnJN.js`, 882,228 B,
SHA-256 `fd6dc0c4ff1902f446a4f88ff2f4bf6c0a72ee713b1f5f3b9f6dc35bb50ea3b6`.
Every served 8779 asset was byte-checked against that release directory.
The later full gate must use a new evidence stem and output directory, after
the root restarts the final API owner and freezes all inputs.
