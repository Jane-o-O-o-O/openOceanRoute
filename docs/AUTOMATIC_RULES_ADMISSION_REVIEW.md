# Automatic-rule admission review for 0.10 development

This review uses current code and actual small ASGI requests. It does not rerun
the full backend, browser, wheel or clean-install gates, and is not original
Makai/FME algorithm or field-accuracy validation. Historical releases remain
unchanged. The reviewer edited only new tests, these notes and separately
assigned installation smoke scripts; production fixes belong to the root agent.

## Scope and two real failures

Read `oceanroute/core.py` (`_layer_geometries`, `_crossings`, `analyze_project`),
`oceanroute/workspace.py` (`_validate`), `oceanroute/api.py` (four automatic-rule
endpoints), and `oceanroute/automatic_rules.py` (admission, catalog and budget).

The private `_check_legacy_crossings=False` path admits and analyses the full
workspace without computing legacy per-leg projected GIS contacts. It neither
accepts this flag in public request/config objects nor filters legacy crossing
results to create the independent checker output. Ordinary analysis and save
continue using the established default. Manufacturing, summary, RPL and route
signatures are unchanged by the private mode; its analysis explicitly has
`crossings=null` and `legacy_crossings_evaluated=false`.

Actual pre-fix requests found an empty-workspace hole: a legal schema2 workspace
with `paths=[]`, `active_path_id=null`, empty assemblies/associations and a hidden,
unselected layer containing `Point[181,0]` received HTTP200 from catalog, check
and import. The same layer with a path received422. Validation previously
admitted geometry only through each materialized path. Root added source
geometry admission for the empty-workspace private path, without changing the
ordinary empty-workspace default.

The first independent test run then produced **18 passed, 8 failed in1.92s**:
Shapely's `GeometryTypeError` from `type='UnsupportedObject'` escaped the narrow
admission handler, across all four endpoints and both empty/nonempty workspaces.
Root added explicit `GeometryTypeError` and `GEOSException` handling at geometry
construction. A one-vertex LineString independently exercises the second native
exception. These are input422 responses rather than partial clear reports or
unhandled500 errors.

## Actual targeted evidence and remaining admission semantics

Final command:

```sh
.venv/bin/python -B -m pytest -o addopts= -q tests/test_automatic_rules_admission_review.py
```

Actual result: **35 passed in1.01s** (tool wall1.312s). The32 parametrized cases
cover four endpoints × empty/nonempty workspace × out-of-range Point, nested
GeometryCollection latitude, unknown type and one-point LineString. Three more
tests check default legacy execution/parity, rejection of public private-flag
bypasses, and direct empty admission versus the unchanged old default. Rejected
requests leave the database empty and preserve the caller's input. No old tests
or production source were edited by this reviewer.

This evidence is specifically structural/coordinate admission. The established
source helper still permits null/empty geometry and warns/skips topologically
invalid geometry. It is not a claim that every invalid polygon is globally
rejected. Independent checking of a selected unsupported/invalid primitive must
remain unresolved with a diagnostic rather than certified clear.

Workspace/source admission, source normalization, ordinary manufacturing
validation and core route output precede the new checker's declared normalized
work budget. The fast admission excludes the old projected crossing operation;
it does not make all source parsing free of CPU/memory costs or charge them to
the checker. Workspace32MiB and existing source/path/library limits remain
separate from the rule result/HTTP-wrapper bounds. Logical work units are not
FLOPs, wall time, resident memory or token charges.

## Installed workflow harness

`scripts/automatic_rules_smoke.py` is stdlib-only. A real local ASGI development
execution made15 requests and returned six rules exercising all three families,
typed integer/string IDs and explicit source indexes, real equatorial contacts,
22.114855m Point proximity, an independent longitudinal angle, and321 genuine
2D source queries producing600 child-triangle violations. Every published
triangle gradient is reconstructed from its three returned queried vertices;
source fingerprint, real witness depth, actual body radius centre and null
unqueried centroid depth are checked separately. The source is explicitly
synthetic, with `D=1000+0.1x+0.2y`; sampled height gradient is `(-0.1,-0.2)`.

The helper checks preview non-persistence, an open package roundtrip, complete
atomic application, two paths sharing one unchanged inventory, explicit save,
API reread, whole-budget422 without save, and a common-vertex high-latitude
single-geodesic turn of0°. Missing references persist as `reference_error` and
nullable ends remain null. A caller must separately provide actual storage-owner
recreation/server restart: the helper itself explicitly reports that it has not
closed an owner. This development execution is not evidence that a not-yet-built
0.10 wheel or clean portable installation passed. Real release executions are
recorded by the separate wheel and portable runners after packaging.

The two smoke runners gained only `version>=0.10` branches. Their original
0.9 archive versions were read from the frozen ZIP: all four existing wheel
child-code literals are byte-identical, the existing native S57 HTTP function
is AST-identical, and each old `smoke()` AST is unchanged after removing the new
0.10 branches. The new portable branch stops and restarts the actual launcher
process before its second workspace GET, then verifies the launcher's actual
fresh-venv editable source registration and module paths/bytes before adding
test dependencies. The new wheel branch
creates a genuinely new ASGI/storage owner after closing the first.

`scripts/verify_0_10_runtime.py` is a standalone derivative of the read-only
0.9 evidence checker; it does not import or edit the old runner. It binds exact
0.10 versions and actual full gate commands, derives counts from current
XML/JSON and input sets, verifies the added smoke contract, current package and
complete wheel RECORD, real loopback resources, PDF/review provenance, initial
runtime ZIP members and the frozen history. Both new helper/verifier must be
present in the initial runtime snapshot. Existing evidence requires
`--verify-only`; normal mode exclusively creates a new report only after all
checks succeed. It never embeds the selected total ZIP hash in its own member.

Initial compile checks and pure subset-rejection probes passed; no then-absent
release0.10 reports/package were read or called verified. An actual read-only
history check compared **116** rows from
`development_0.10_frozen_artifacts.json` with current bytes; all matched. The
unchanged old `verify_0_9_runtime.py` was separately compared byte-for-byte with
its frozen ZIP member (SHA256
`89e7523c74be77ce82ef0e34cc49b27087444dbb93df425249339d2209605260`).

## Real first clean-install contract failure and correction

The first actual clean launcher execution exited1 at the newly added module
location assertion: the smoke incorrectly required module files inside the
venv. The product launcher, including previous0.9, actually runs
`pip install -e ROOT[terrain]` in a fresh environment. Its editable registration
correctly points to the extracted `OceanRoute/oceanroute` source files. This was
a **smoke contract mistake**, not a product installation or rule-engine fix.
The separate extracted-wheel smoke passed its own distinct scope; it does not
turn the failed first complete source installation into a pass.

Original evidence is preserved in `release_0.10_initial_archive.json` (first ZIP
SHA256`45228aad6b869b51e6c659a7b13175d42d0969f9657f992b51ceea09fdb40ffd`),
`development_0.10_portable_first_contract_failure.json`, and the actual captured
stderr `development_0.10_portable_first_failure_stderr.log`. The original stdout
log is genuinely empty and is not replaced by invented output. These notes read
the preserved trace rather than rerunning or independently re-creating the
root's failed installation execution.

Only the new0.10 portable branch and verifier are corrected: metadata name and
version, `.dist-info` inside the fresh venv, real `sys.prefix`, `direct_url.json`
with `editable=true` pointing to the extracted source root, and each module's
actual source path/bytes are checked from a child cwd **outside** that source
checkout. Both declared and actual paths are resolved, including macOS
`/var`→`/private/var`. The report labels this
`installation_kind='fresh_venv_editable_source'` and records
`launcher_installed_modules_before_test_dependencies`; it does not describe
launcher HTTP as wheel installation. The unchanged helper still makes its same
15 requests, and the caller still really stops/restarts the installed server.

The next clean-install candidate gets a separate
`release_0.10_verified_initial_archive.json`. The verifier binds current input
and final-selected ZIP bytes to that actually retried candidate. It retains
both initial snapshots and the first failure's trace/runner association, rather
than requiring the obsolete validation scripts in the failed first snapshot to
equal their corrected current bytes. Compile checks are not a second successful
clean-install execution; that evidence must come from the root's actual rerun.
