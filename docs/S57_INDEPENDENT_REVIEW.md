# S-57 independent research and verification

This is 0.8 development work after the frozen 0.7 release. It checks native chart
import into reference GIS layers. It does not certify ECDIS/S-52 portrayal, chart
currency, navigation safety, engineering bathymetry, FME compatibility, or all
IHO/producer application profiles. The 0.7 packages and reports are unchanged.

## Product and public technical evidence

`resources/research/MakaiPlan.txt`, lines293–315, represents physical page9 of
the supplied 12-page `MakaiPlan.pdf`: the optional FME module lists S-57 among
its supported GIS formats. This is the specific product evidence for this
feature. The 354-page main manual describes GIS import but does not define a
public native S-57/GDAL API. OceanRoute's contract is an independent extension;
implementing this one format does not implement the advertised 150+ FME formats.

The [GDAL S57 driver documentation](https://gdal.org/en/stable/drivers/vector/s57.html)
describes class layers, DSID metadata, native updates, and sounding multipoints.
The [Pyogrio API](https://pyogrio.readthedocs.io/en/latest/api.html) defines the
read/count/open-option mechanisms. Runtime uses native Pyogrio/GDAL in a private
subprocess; tests do not convert GeoJSON into something named `.000`.

The [IHO main specification](https://iho.int/uploads/user/pubs/standards/s-57/31Main.pdf)
defines the ISO8211 fields and integer coordinate multipliers. The
[ENC product specification](https://iho.int/uploads/user/pubs/standards/s-57/20ApB1.pdf)
requires WGS84 geographic ENC coordinates and describes updates and reissues.
The [attribute catalog](https://iho.int/uploads/user/pubs/standards/s-57/31ApAch2.pdf)
identifies `VERDAT12` as MLLW and `VERDAT16` as MHW. These chart datums are retained;
they are not silently replaced by the dynamic model's sea surface.

## Real binary fixtures and permission

Two unchanged NOAA exchange sets were retrieved on 2026-10-04:

| Cell | Official source | Original base / supplied updates | Sounding arrays |
|---|---|---|---|
| US5A1KMJ | [NOAA ZIP](https://www.charts.noaa.gov/ENCs/US5A1KMJ.zip) | Edition1, UPDN0 / .001, .002 | 4 MultiPointZ features, 657 points |
| US5A1KMK | [NOAA ZIP](https://www.charts.noaa.gov/ENCs/US5A1KMK.zip) | Edition2, UPDN0 / .001, .002 | 2 MultiPointZ features, 1346 points |

The [NOAA ENC dataset record](https://repository.library.noaa.gov/view/noaa/71555)
identifies CC0-1.0. The [NOAA redistribution agreement](https://www.charts.noaa.gov/ENCs/ENC_Agreement.shtml)
allows distribution subject to its conditions; redistributed research fixtures
are not an official NOAA navigation/carriage product. Original ZIP readme,
agreement, catalog, and ancillary text are retained. No NOAA endorsement is
implied. The frozen samples are not for navigation.

Files are under `tests/fixtures/s57/noaa/`. Exact ZIP/member byte counts, SHA256,
source URLs, retrieval date, and licensing evidence are recorded in
`resources/research/s57_sources.json`; the fixture README explains attribution.
No IHO S64 data were bundled: a GDAL test script's MIT license does not establish
a redistribution license for externally authored test charts.

## Contract checked

All three HTTP operations use multipart `file` containing native bytes and
`config_json` containing a JSON object. Python functions have the equivalent
`data: bytes`, basename `filename`, and optional `config` arguments.

| Operation | Endpoint | Returned stage and application |
|---|---|---|
| Container inspection | `POST /api/import/s57/inspect` | `inspect`; no GDAL read, no class counts, never applicable |
| Native class catalog | `POST /api/import/s57/catalog` | `catalog`; DSID/class counts and schemas, no feature arrays, never applicable |
| Native selected import | `POST /api/import/s57` | `import`; complete reference layer bundle, explicit application required |

The response schema is `oceanroute.s57.bundle.v1`. It includes `source`,
`cells`, `classes_catalog`, `missing_classes_by_cell`, `layers`, `reader`,
`warnings`, `summary`, and `budget`. Each selected cell records original supplied
file paths/hashes, base and effective DSID, update metadata, and datum/unit codes.
Each nonempty class has its own reference layer/FeatureCollection with native
properties and geometry. Import is a preview; adding the complete layer group to
a workspace is a separate revision-protected transaction.

`config.cells` selects exact archive-relative `.000` paths. A multi-cell ZIP
requires an explicit selection. `config.classes` uses exact native class names;
unknown classes are errors. A class absent from one selected cell but present in
another is reported per cell. No supplied update is inferred from the upload's
original filesystem directory. Updates alone do not form a base.

`SOUNDG` remains MultiPoint with its third coordinate, encoded sign, and native
scale. `DSPM` unit/datum metadata remain distinct from model elevation. Unknown
or non-metre sounding units remain a code and `depth_units:null`, with a warning
and no conversion. Non-WGS84 `HDAT` or nongeographic `COUN` is rejected. Missing
numeric attributes become JSON `null`; lists remain lists; national text is
decoded according to DSSI. No terrain profile, material allocation, or route is
changed by a successful preview.

## Independent oracles and actual counterexamples

`tests/test_s57_independent.py` contains a small bounded **test-only** decoder for
the chosen fixtures' ISO8211 record directories. It reads these declared fields
directly, without OceanRoute's or GDAL's parsing/geometry helpers:

- `DSPM` integer `COMF/SOMF`, then `FRID.SOUNDG` → `FSPT.NAME` → `VRID.SG3D`, to
  compare every XYZ point against native output. The direct signed integer
  oracle also checks a derived negative sounding and a different SOMF.
- `FOID` agency/unsigned number/subdivision, to reproduce the exact unique LNAM.
  Native OGR `FIDN` can be signed32 for a source unsigned number above2³¹−1;
  preserved LNAM carries the full identity. It must not be described as lossless
  unsigned numeric identity solely by inspecting the FIDN scalar.
- Actual `.001` `ATTF` code90 values: KMJ RCID1016 changes100.5→100.6 and
  RCID1017 changes50.2→50.3. Area RCID423 also changes geometry/RVER.
- `FSPT.USAG=2`, `VRPT` endpoint nodes and `SG2D` edge coordinates, to reconstruct
  the real DEPARE425 interior ring. A point in that hole must remain outside the
  imported polygon. The full KMJ import retains1087 real feature records,
  26 nonempty class layers, and80572 coordinate vertices.
- Actual KMK `NATF` UCS-2 text `Kaxchim Chiĝanaa`, including the combining
  circumflex, to check national attribute recoding rather than ASCII replacement.

Other checks use real native base/updates, two-cell selection, finite JSON,
ambient GDAL-option isolation, topology warnings, native bad declarations,
truncation, disguised JSON, class/feature/vertex/output budgets, and malicious ZIP
paths/duplicates/symlinks/CRC/encryption/declared size. HTTP checks import actual
binary bytes, keep preview storage unchanged, save the entire layer group,
reopen it, reject a bad import without changes, and restore the prior revision.
Malformed JSON options are checked through the actual HTTP multipart interface.

Metadata alterations and adversarial ZIPs are created in memory and labelled
synthetic. They are not represented as original NOAA data or additional real
producer coverage. The fixture decoder is deliberately not a general S-57
implementation or conformance suite.

## Reissue limitation found independently

IHO permits a reissued `.000` with nonzero base UPDN: its next update starts at
base UPDN+1. Therefore `.000` UPDN1 followed by `.002` is a legal sequence; it
must not be called a missing `.001` source error.

An actual native probe on Pyogrio0.13.0/GDAL3.12.4 returned UPDN1 for both IGNORE
and APPLY on that derived declared-reissue example, even though `.002` was
supplied. The [GDAL3.12.4 reader source](https://github.com/OSGeo/gdal/blob/v3.12.4/ogr/ogrsf_frmts/s57/s57reader.cpp)
starts update lookup at1; [GDAL issue13456](https://github.com/OSGeo/gdal/issues/13456)
documents the problem. OceanRoute therefore explicitly rejects a nonzero-base
reissue with later supplied updates as a native-driver limitation. A reissue
without additional supplied updates remains readable. It must not fabricate
missing updates, rewrite native headers, or silently return the old chart.

Every supplied native update header is independently bound to cell identity,
edition, agency, update purpose/number before APPLY, and the effective final
number is checked after APPLY. Cancellation/new-edition announcements are
rejected for import rather than leaving the old cell represented as current.

## Budgets and remaining coverage

Input is at most128MiB; a ZIP has at most1000 members and128MiB aggregate declared
uncompressed bytes. Selected cells default to8 and are capped at32. The default
feature limit is100000, vertex limit200000 (hard250000), complete output32MB
(hard64MB), and per-class native array read is capped at20000 features. Native
execution has a30s default wall timeout, bounded to60s. Output and topology
validation fail atomically; partial layers are not returned as usable success.

Normalized work units charge supplied bytes, native reads, schemas, features,
vertices, and attributes. They are not measured FLOPs or a hard process memory
limit. The subprocess timeout bounds native execution; archive admission/CRC
checks happen separately. Large/high-complexity charts may be rejected. The
specific polygon organization performance warning is reported without skipping
organization or dropping holes; other native diagnostics remain errors.

The targeted independent run completed **66 passed in 23.38 s** on the local
Python 3.13 environment with Pyogrio 0.13.0/GDAL 3.12.4. This is a targeted native
and HTTP verification record, not a new release-wide gate. Full S-57/S-63/S-101
conformance, S-52 symbolization, tide/vertical conversions, automatic engineering
bathymetry, external-link execution, native ENC writing, and broader optional
FME format coverage remain outside this verified feature.
