# Native S-57 reference charts

Development implementation for the next phase after the frozen 0.7 release. This
module imports actual ISO8211 S-57 bytes through the GDAL **S57** driver in
pyogrio. It does not require a preconverted GeoJSON, GeoPandas, Pandas, Fiona or
the Python `osgeo` package. It does not recreate optional FME or its format
collection, and is not an S-52/ECDIS navigation presentation or ENC certificate.
The typed field catalogue requires pyogrio 0.12 or later; the actual development
native validation used pyogrio 0.13.0 with its bundled GDAL 3.12.4.

## Public calls and three stages

```python
from oceanroute.s57 import inspect_s57, catalog_s57, import_s57
from pathlib import Path

path = Path("tests/fixtures/s57/noaa/US5A1KMJ.zip")
data = path.read_bytes()
inspection = inspect_s57(data, filename=path.name)
selected = [inspection["cells"][0]["path"]]
catalog = catalog_s57(data, filename=path.name, config={"cells": selected})
result = import_s57(data, filename=path.name,
                    config={"cells": selected, "classes": ["SOUNDG", "DEPARE"]})
assert result["can_apply"]
# result["layers"] are existing GIS layer objects, suitable for a separately
# validated atomic workspace transaction. This module does not save a project.
```

All functions take `bytes`, a basename `filename` and optional object `config`.
`inspect` checks the supplied container and lists candidates without invoking
GDAL. Its `accepted` means container validation only; native metadata is absent,
`classes_catalog=[]`, `reader.class_counts_known=False` and `can_apply=False`.
`catalog` reads the native base/update DSID records, actual class feature counts
and field schemas without reading class geometry/attribute arrays. `import`
reads only the selected object classes and returns the complete reference layers.
Only this last stage can return `can_apply=True`, and only with nonempty valid
geometry. Classes with zero features remain explicitly visible in the catalog.

The exact config keys are `name`, `cells`, `classes`, `max_cells`, `max_layers`,
`max_features`, `max_vertices`, `max_work_units`, `max_output_bytes`, `timeout_s`.
Unknown keys, nonfinite or noninteger limits, boolean numeric limits, duplicate
selections and empty `cells`/`classes` arrays are rejected. Cell selections use
the exact full POSIX paths returned by inspect; a bare filename is not an
ambiguous selector. Class selections use exact case-sensitive GDAL acronyms.
When classes exist only in some selected cells, `missing_classes_by_cell`
records this; a class absent from every selected cell is rejected. Catalog
feature counts do not consume the *import* feature/vertex caps, so a small
class selection can import part of a larger chart without loading its other
objects. Metadata/native-time/work limits still apply to catalog operations.

## Input and update contract

Direct `.000` input is a single supplied base. Nearby files on the user's host
are never read. To supply updates, send a ZIP with each base and its matching
updates in the same directory and stem. One cell can be selected implicitly;
a ZIP with multiple bases requires explicit `config.cells` for catalog/import.
The container stage may list multiple cells before any selection.

ZIP limits are 128 MiB compressed input, 128 MiB total uncompressed files,
1000 members and 128 base candidates. Stored/deflated compression is supported.
Absolute/traversing/noncanonical paths, backslashes, control characters,
case-conflicting/duplicate paths, encryption, symlinks, CRC errors and orphan
numeric update files are rejected. `CATALOG.031` is recognized as the legitimate
exchange catalogue, not an update. Other ancillary files are enumerated with
path, byte length and SHA, but are not interpreted, fetched or followed.
No untrusted path is used to extract files; selected files receive private
worker paths, and original paths/digests remain in the public evidence.

An update chain need not start at `.001`: a legitimate reissued base may already
have a positive `DSID_UPDN`. Internal gaps can be detected during inspect; after
native base reading, the first supplied update must be `base UPDN + 1` and all
subsequent numbers must be consecutive. Every update is separately read using
native `UPDATES=IGNORE` and its actual DSNM stem/suffix, edition, agency,
exchange purpose and update number are bound to the base and filename. Then
For an ordinary base with `UPDN=0`, GDAL applies the complete supplied chain
with `UPDATES=APPLY`; the final DSID identity/edition/update number is
independently checked. A positive-UPDN reissued base without later supplied
updates can be read. A legitimate positive-UPDN reissue **with subsequent
updates is explicitly rejected as a native-driver limitation**, after verifying
the standard chain and update headers and before APPLY. Actual GDAL 3.12.4
starts searching at `.001`, so it does not apply the legitimate `.002` after a
base declaring UPDN 1. Neither headers nor suffixes are rewritten, and no
missing update is fabricated. This gap is distinct from an invalid chain and
remains for a future native update-engine/driver repair. A cancelled edition
is rejected rather than displaying an old chart as a successful updated chart.
This verifies the supplied chain, not the producer's latest available chart.
It is not a cryptographic authentication of NOAA or another producer.

## Returned schema and preserved data

Every stage returns `schema="oceanroute.s57.bundle.v1"`, `stage`, `accepted`,
`can_apply`, `source`, `output_crs`, `cells`, `classes_catalog`,
`missing_classes_by_cell`, `layers`, `warnings`, `summary`, `budget`, `reader`.
`source.sha256` binds the exact uploaded bytes throughout all stages.

Each inspected cell has `path`, `base={path,bytes,sha256}`, `updates` (each
`path,number,bytes,sha256`), `selected`, `sequence_status`. Native stages add
complete `base_dsid`/updated `dsid` properties, `applied_update_number`, actual
update `dsid` properties, `datum_units` and `object_classes`.

Each class row has `name`, `geometry_type`, actual `feature_count`, `selected`,
`fields=[{name,dtype,ogr_type,ogr_subtype}]`, `crs` and `encoding`. Top-level
`classes_catalog` aggregates counts and available cell paths by acronym.
GDAL's layer type `Unknown` is legitimate for a class containing different
geometry types; actual WKB is checked separately. Unresolved generic classes,
unavailable counts and an unexpected native driver are rejected.

Layers retain the existing `id,name,kind,visible,crs,geojson` contract with
`kind="reference"`. One layer is returned per selected cell/object class with
features in native order and original native feature IDs. Every OGR field is
kept, including OBJL, RCID, AGEN, FIDN/FIDS, national text, relationship/linkage
arrays and nulls. NumPy's numeric-null NaN representation becomes JSON null;
actual infinity and unsupported attribute types are rejected, not stringified.
These are native OGR values, not a byte-for-byte reconstruction of every ISO
8211 numeric field: GDAL exposes FIDN as signed int32 for some unsigned source
values above 2^31-1. LNAM's exact hexadecimal AGEN/FIDN/FIDS identity and
LNAM_REFS are retained, and `source.feature_identity_semantics` declares this
representation. For example, NOAA SOUNDG FIDN 3041713370 becomes native
-1253253926 while LNAM `0226B54CDCDA21CA` preserves the unsigned identity.
Native encoding is recoded by DSSI; input national text is not normalized or
translated. Metadata records are kept separately rather than mistaken for GIS
geometry. Null spatial/nonspatial object geometries retain their attributes and
emit explicit diagnostics; an all-empty selected import is rejected.

Each layer's `source.cell_evidence` retains its original cell path, base file
descriptor, every update descriptor and native update DSID, base/effective DSID,
applied number and sequence status. `source.native_reader` retains actual
pyogrio/GDAL versions, driver/options, process isolation and applicable native
warnings. No private temporary worker path is included. These compact records
remain available after applying just the layers and saving/reopening a shared
workspace; the full class catalogue is not copied into every layer.

Point/MultiPoint, LineString/MultiLineString, Polygon/MultiPolygon and nested
GeometryCollection are supported. Native WKB topology, finite coordinates and
longitude/latitude bounds are checked. Polygon holes and native 3-D coordinates
are retained, with no simplification, polygon repair, `METHOD=SKIP` or 2-D cast.
The exact known GDAL `organizePolygons >100 parts` processing-cost warning is
retained in both `warnings` and `reader.reader_warnings`. Other native Python
warnings, native stderr diagnostics and unsupported input reject the complete
operation; the module never hides a failed class behind a successful bundle.

## Coordinate, sounding and datum boundary

The geographic contract requires native `DSPM_HDAT=2` (WGS84), `DSPM_COUN=1`
(longitude/latitude) and the object's declared CRS agreeing with WGS84. Other
horizontal datums/coordinate units are rejected instead of relabelled. There
is no inference from filenames or default PROJ settings.

`datum_units` exposes horizontal/vertical/sounding datum codes, depth/height/
positional-accuracy unit codes, `depth_units`, `height_units`,
`coordinate_unit_code`, `soundings_z_positive="down"`,
`soundings_z_is_model_height=False`, `units_converted=False`. Unit code 1 is
identified as metres; other depth/height codes remain unconverted and their
unit labels are null. Unknown depth units carry an explicit warning. All
original DSPM values, including COMF/SOMF, remain in DSID evidence. Local
M_SDAT/M_VDAT/M_UNIT objects and per-object datum attributes are retained when
their classes are selected; the module does not resolve a spatial datum map.

SOUNDG uses `SPLIT_MULTIPOINT=OFF`: the original multipoint grouping and every
finite `[longitude,latitude,encoded_signed_chart_depth]` are retained, including
negative sounding values. The depth sign convention is positive down; no
clamping or sign change is performed. These third
coordinates are chart sounding depths in declared chart units/datum, **not**
GeoJSON ellipsoidal altitude or model `z`. The layer source explicitly names
this exception in `geometry_z_semantics`; other optional Z values remain native
and uninterpreted. No depths are written into route/profile/terrain sources.
No sea-surface, tide, chart-datum, uncertainty or survey-quality conversion is
implied by successful reading.

## Bounded work and isolation

Defaults / hard maxima are respectively: selected cells 8/32, native layers
256/512, total imported features 100000/200000, vertices 200000/250000,
normalized work 20M/200M, complete compact UTF-8 output bytes 32M/64M and worker
wall time 30/60 seconds. A selected class with over 20000 features is rejected
before raw arrays because the existing shared-workspace class layer limit is
20000. This reader's vertex cap is at most 250000 per import request. Adding
layers to a populated workspace must also pass its layer validation and
32 MiB complete-JSON limit. The workspace's separate 250000 geometry budget
counts densified route geometry, not cumulative GIS coordinates; repeated
imports are not promised a 250000-coordinate GIS storage ceiling. Successful
import does not authorize a partial append.

Budget counters charge supplied uncompressed bytes/256, every native open/read
by cell bytes/256+1000, actual catalog counts/field schemas, imported records,
vertices, encoded attributes and the compact per-layer persisted provenance.
`native_open_read_calls`,
`native_input_bytes_charged`, imported counts and exact compact `output_bytes`
are reported. Work units are a bounded admission/charging model, **not** a count
of internal GDAL operations, memory bytes, FLOPs or a native-runtime guarantee.
Native parsing can still be expensive; the independent wall timeout terminates
the process and rejects any partial result. Feature limits are checked using
actual native catalog counts before geometry/attribute reading; vertices and
attributes are checked during conversion, and complete output size before
return. The module does not claim million-point performance.

Each native operation runs in a new subprocess with private supplied files,
sanitized GDAL/S57/PROJ overrides and process-local fixed open options. The
worker imports pyogrio/NumPy/Shapely directly. It does not modify the API
process's GDAL configuration or allow HTTP/vsicurl/native path input. Different
imports can therefore run concurrently without one request changing another
request's S57 options. Process failure, missing native driver/CSV support or
timeout produces a ValueError with no usable layers. Temporary files are
removed after the call. The main dependency decision is owned by the root;
the tested environment is pyogrio 0.13.0 / GDAL 3.12.4.

## Actual evidence and limitations

The implementation's own targeted tests use the actual NOAA US5A1KMJ exchange
set (base plus `.001/.002`) and US5A1KMK. These are native ISO8211 fixtures, not
fabricated GeoJSON. They verify container/selection guards, DSID 0→2, 657
SOUNDG XYZ points, all scalar/list/null attributes, actual updated ELEVAT,
Polygon holes/LineString coordinates against native WKB, full-chart warning
preservation, caps, concurrent configuration isolation and finite JSON. The
independent reviewer owns additional update-header/unit/encoding/HTTP tests;
their execution results belong in separate evidence, not a count guessed here.
The root runs the complete backend/browser/package gates after integration.

For the observed US5A1KMJ case, selected SOUNDG/DEPARE/DEPCNT import produced
215 features and 37644 vertices; full import produced 26 nonempty object-class
layers, 1087 features and 80572 vertices in under one second in the development
environment. These are small local fixture measurements, not chart-size or
cross-platform performance promises. No final 0.8 release is claimed here.

## Primary references

- [GDAL S57 driver](https://gdal.org/en/stable/drivers/vector/s57.html): native
  object-class translation, DSID metadata, update and sounding open options.
- [pyogrio API](https://pyogrio.readthedocs.io/en/latest/api.html) and
  [raw reader source](https://github.com/geopandas/pyogrio/blob/main/pyogrio/raw.py):
  layer inspection and raw WKB/NumPy columns; process-wide config explains the
  isolation requirement.
- [IHO S-57 ENC Product Specification](https://docs.iho.int/iho_pubs/standard/S-57Ed3.1/20ApB1.pdf):
  WGS84 geographic ENC parameters and reissue/update rules. Metadata codes are
  preserved; this importer is not a standard-conformance validator.
- [GDAL issue 13456](https://github.com/OSGeo/gdal/issues/13456): native
  reissued-cell update lookup limitation, also reproduced with a declared
  positive-UPDN modification of the official native fixture. This is an
  admission/driver test, not an assertion of NOAA's actual chart history.
- [NOAA chart download terms](https://www.charts.noaa.gov/ENCs/ENC_Agreement.shtml),
  [US5A1KMJ](https://www.charts.noaa.gov/ENCs/US5A1KMJ.zip),
  [US5A1KMK](https://www.charts.noaa.gov/ENCs/US5A1KMK.zip): official native test
  source. Download/licence details and frozen fixture hashes are recorded by the
  independent reviewer in `resources/research/s57_sources.json` and the fixture
  README. Derived OceanRoute display is an unofficial reference product.
