# Shared module contract - development v0.1

Product working name: OceanRoute. Python package: `oceanroute`; frontend: `web/`.
This contract describes the baseline integration. Detailed current extension contracts are maintained in the notes linked below.

## Project JSON

```json
{
  "schema_version": 1,
  "id": "project-id",
  "name": "海缆规划工程",
  "crs": "EPSG:4326",
  "route": {
    "id": "route-1", "name": "主路由", "curve": "rhumb",
    "mode": "flexible", "slack_basis": "surface", "slack_pct": 1.5,
    "points": [{"id":"p1","label":"登陆点","longitude":118.0,"latitude":22.0,"depth_m":30.0,"note":""}],
    "legs": [{"cable_type_id":"LW","slack_pct":1.5,"fixed_cable_length_m":null,"burial":false}]
  },
  "profile": {"samples": [{"kp_m":0,"depth_m":30}], "route_signature": "optional signature", "source":"user"},
  "cable_types": [{"id":"LW","name":"轻型缆","diameter_m":0.02,"wet_weight_n_m":4,"cost_per_m":20,"lay_speed_m_s":1.5,"ea_n":100000000,"ei_n_m2":10,"max_tension_n":40000,"min_bend_radius_m":1}],
  "bodies": [{"id":"b1","name":"中继器","kind":"repeater","kp_m":500,"cost":1000,"length_m":2}],
  "costs": {"currency":"CNY","vessel_day_rate":100000,"burial_per_m":10,"contingency_pct":0},
  "rules": {"max_slope_deg":15,"min_slack_pct":0,"max_slack_pct":5,"corridor_m":500},
  "layers": [{"id":"layer-1","name":"既有海缆","kind":"cable","visible":true,"geojson":{"type":"FeatureCollection","features":[]}}]
}
```

Route points have at least two entries. `legs[i]` belongs to `points[i] → points[i+1]`; absent leg options use defaults. Coordinates are longitude/latitude decimal degrees WGS84, depth positive downward, internal length metres, time seconds, force newtons. `slack_pct` is display percentage (1.5 means 0.015 dimensionless). Unknown depth is null, never silently zero. Keep route geometry, surface KP, bottom distance and cable distance separate.

`profile.samples` uses surface KP. Its validity must be tied to the route signature. Waypoint depths alone may produce an explicitly labelled linear approximation, not a measured seabed. JSON must contain no NaN/Infinity.

## Core module ownership and outputs

`oceanroute/core.py`: `analyze_project(project: dict) -> dict`, `sample_project() -> dict`, `route_signature(project: dict) -> str`. Geometry and planning analytics have no HTTP dependency.

Analysis shape:

```json
{
 "summary":{"surface_length_m":0,"bottom_length_m":null,"cable_length_m":0,"material_cost":0,"body_cost":0,"vessel_cost":0,"burial_cost":0,"cost_total":0,"time_hours":0,"currency":"CNY","slack_pct":null},
 "rpl":[{"id":"p1","index":0,"label":"","longitude":0,"latitude":0,"depth_m":null,"kp_m":0,"bottom_kp_m":null,"cable_kp_m":0,"bearing_deg":null,"cable_type_id":"LW","surface_slack_pct":null,"bottom_slack_pct":null,"note":""}],
 "legs":[{"index":0,"surface_length_m":0,"bottom_length_m":null,"cable_length_m":0,"bearing_deg":0,"slope_deg":null,"surface_slack_pct":null,"bottom_slack_pct":null,"cable_type_id":"LW","time_hours":0,"material_cost":0}],
 "profile":[{"kp_m":0,"depth_m":0,"bottom_kp_m":0,"slope_deg":0}],
 "warnings":[{"code":"CODE","message":"说明","severity":"warning","point_id":null}],
 "sld":[{"kind":"cable","name":"","start_m":0,"end_m":0,"cable_type_id":"LW"}],
 "crossings":[]
}
```

Extra diagnostic fields are permitted. Invalid input raises `ValueError`; missing input may produce warnings with null derived results. Every approximation identifies its assumptions.

## Simulation module

`oceanroute/simulation.py`: `catenary(config: dict) -> dict`, `steady_state(config: dict) -> dict`, `simulate_lay(project: dict, config: dict) -> dict`, `span_analysis(config: dict) -> dict`.

Simulation config may include `duration_s`, `dt_s`, `nodes`, `depth_m`, `ship_speed_m_s`, `payout_m_s`, `current_x_m_s`, `current_y_m_s`, `wet_weight_n_m`, `diameter_m`, `drag_coefficient`, `ea_n`, `ei_n_m2`, `bottom_tension_n`. Result should include model identity, assumptions, frames and summary. Dynamic model must have actual numerical physics; do not substitute a decorative playback and claim dynamic equivalence. Baseline returns `validation_status: "research"` unless independently validated.

Frame `{time_s, ship: [x,y,z], nodes:[[x,y,z]], top_tension_n, bottom_tension_n, touchdown:[x,y,z]}`. All coordinates metres; vertical z may use positive upward but the result must state convention. Solvers must reject invalid inputs, bound computation, expose convergence and numerical warnings. Analytical catenary and simplest dynamic invariants need meaningful tests.

## HTTP integration (root owner)

- GET `/api/health`, GET `/api/sample`, GET `/api/capabilities`
- POST `/api/analyze` body is Project JSON, returns Analysis
- GET `/api/projects`; POST `/api/projects` saves Project JSON; GET `/api/projects/{id}`; GET `/api/projects/{id}/revisions`; POST `/api/projects/{id}/restore/{revision}`
- POST `/api/import/rpl` body `{text, delimiter?, mapping?}` returns `{points, warnings}`
- POST `/api/import/profile` body `{text}` returns `{samples,warnings}`
- POST `/api/import/geojson` body `{text, name?, kind?}` returns Layer
- POST `/api/export/{format}` body Project; formats `csv`, `kml`, `geojson`, `dxf`, `sld` (SVG), `report` (HTML), `project` (JSON), `assembly` (manufacturing CSV)
- POST `/api/route/reverse` body Project returns Project; split/merge endpoints follow later
- POST `/api/simulation/{kind}` body `{project, config}`; kinds `catenary`, `steady`, `dynamic`, `span`

Frontend uses same-origin `/api` through Vite proxy during development; production is served by FastAPI. HTTP errors shape `{detail: string, code?: string}`. Capabilities distinguish implemented, research, planned and unverified.

## Current extension contracts

- Engineering tools: `/api/tools/{kind}` and route transformations; [TOOLS_NOTES.md](TOOLS_NOTES.md).
- Local bounded route search: `/api/routing/search`; [ROUTING_NOTES.md](ROUTING_NOTES.md).
- Captured manufacturing domains: `/api/constraints/{configure|edit|solve}`; [CONSTRAINT_NOTES.md](CONSTRAINT_NOTES.md).
- Manufacturing CSV preview: `/api/assembly/import`; [ASSEMBLY_NOTES.md](ASSEMBLY_NOTES.md).
- XYZ/GeoTIFF/Surfer profiles, KML and Shapefile ZIP, DTM grid: [GEODATA_NOTES.md](GEODATA_NOTES.md), `/api/dtm/grid`.
- RPL fixed-width/multiline templates and explicit preview policy: `/api/import/rpl`, `/api/import/rpl/template`, `/api/import/rpl/templates`; [RPL_TEMPLATE_NOTES.md](RPL_TEMPLATE_NOTES.md).
- Variational minimum-curvature grid, declared BLN/masks and full-raster slices: `/api/dtm/{grid|slice|bln/read|bln/write}`; [DTM_NOTES.md](DTM_NOTES.md).
- Full dynamic checkpoints are fields in actual dynamic results; continuation passes `config.resume_state`; [MODEL_NOTES.md](MODEL_NOTES.md).
- Initial ship plan, actual branch Look Ahead and bounded tension search: `/api/shipplan/{generate|lookahead|optimize}`; [SHIPPLAN_NOTES.md](SHIPPLAN_NOTES.md).
- Explicit project/manufacturing-to-voyage preparation: `/api/shipplan/prepare-voyage`; [PLAN_VOYAGE_NOTES.md](PLAN_VOYAGE_NOTES.md). Preserve returned `config.plan_mapping` when submitting a job; mapped continuation stays within the prepared window.
- Sea spectrum/RAO/real Monte Carlo: `/api/sea/{generate|simulate|montecarlo}`; [SEA_NOTES.md](SEA_NOTES.md).
- Survey reconciliation: `/api/survey/reconcile`; [SURVEY_NOTES.md](SURVEY_NOTES.md).
- Research recovery/tow/grapnel-rope/buoy tools: `/api/repair/{recovery|tow|rope|buoy}`; [REPAIR_NOTES.md](REPAIR_NOTES.md).

Saved projects expose `saved_revision`. Existing-ID saves require the exact current revision, including after same-ID tool transforms; missing or stale revision is rejected. JSON import and route split/merge create new IDs. Assembly references have `{id,name,cable_kp_m,note?}` and contribute zero length and zero cost. Stored constraint states are opaque integrity records; edit them through the constraint APIs, or explicitly clear and recapture them.


## Route side slopes and KP slope rules (0.9)

Each schema1 path may preserve optional `side_slopes` (model `route-side-slopes-v1`, schema1) and `slope_rules` (strict independent list, at most512). The shared terrain library remains on the schema2 workspace; neither field creates manufacturing inventory. `POST /api/terrain/side-slopes` produces genuine normal-ray probes and a pure candidate; `POST /api/tools/slope-rules` changes only the normalized rule list in its candidate. Only explicit whole-workspace saving writes a revision.

Core adds `side_slopes_metadata` and `slope_rule_checks` only when their corresponding fields are present. Geometry/library bindings, raw transects, coverage and current analysis identity govern use; missing, stale or uncertain data never become a pass. Nullable sampling end resolves to the actual end of this computation; nullable saved rule end remains dynamic for future route checks. Signed side angles are positive for rising elevation towards starboard. Rules use absolute sampled maxima and preserve simultaneous violations and incomplete coverage. Full contracts: [SIDE_SLOPES_NOTES.md](SIDE_SLOPES_NOTES.md), [SLOPE_RULES_NOTES.md](SLOPE_RULES_NOTES.md).
