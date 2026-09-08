"""Native S-57 chart catalog/import through an isolated GDAL/pyogrio worker.

The charts are reference layers. Sounding Z retains the encoded signed value
under the positive-down depth convention
and datum; it is never converted to model elevation or route/terrain water depth.
Only supplied bytes enter the worker. No implicit adjacent files are read.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
from uuid import NAMESPACE_URL, uuid5
import zipfile
import zlib

SCHEMA = "oceanroute.s57.bundle.v1"
MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_MEMBERS = 1000
OPTIONS = {"UPDATES": "APPLY", "SPLIT_MULTIPOINT": "OFF", "ADD_SOUNDG_DEPTH": "OFF",
           "RETURN_PRIMITIVES": "OFF", "RETURN_LINKAGES": "ON", "LNAM_REFS": "ON",
           "LIST_AS_STRING": "OFF", "RECODE_BY_DSSI": "ON"}
LIMITS = {"max_cells": (8, 32), "max_layers": (256, 512), "max_features": (100000, 200000),
          "max_vertices": (200000, 250000), "max_work_units": (20000000, 200000000),
          "max_output_bytes": (32000000, 64000000), "timeout_s": (30, 60)}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json(data):
    try:
        return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
        raise ValueError("S57 result must contain finite UTF-8 JSON") from exc


def _finalize(result, config):
    # Include the counter's own digits in the actual compact UTF-8 byte count.
    result["budget"]["output_bytes"] = 0
    for _ in range(8):
        count = len(_json(result))
        if count > config["max_output_bytes"]:
            raise ValueError("S57 complete result exceeds max_output_bytes; select fewer classes")
        if result["budget"]["output_bytes"] == count:
            return result
        result["budget"]["output_bytes"] = count
    raise ValueError("S57 output-byte accounting did not stabilize")


def _config(raw):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("S57 config must be an object")
    if set(raw) - {"name", "cells", "classes", *LIMITS}:
        raise ValueError("unknown S57 configuration: " + ", ".join(sorted(set(raw)-{"name", "cells", "classes", *LIMITS})))
    result = {"name": raw.get("name", "S-57 reference chart")}
    if not isinstance(result["name"], str) or not result["name"].strip() or len(result["name"].encode("utf-8")) > 2048:
        raise ValueError("S57 name must be nonempty UTF-8 text of at most 2048 bytes")
    for key, (default, maximum) in LIMITS.items():
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError(f"S57 {key} must be an integer between 1 and {maximum}")
        result[key] = value
    for key in ("cells", "classes"):
        if key in raw:
            values = raw[key]
            if not isinstance(values, list) or not values or len(values) > 512:
                raise ValueError(f"S57 {key} must be a nonempty array of at most 512 exact names")
            if any(not isinstance(v, str) or not v or len(v.encode("utf-8")) > 512 for v in values):
                raise ValueError(f"S57 {key} names must be nonempty UTF-8 text of at most 512 bytes")
            if len(set(values)) != len(values):
                raise ValueError(f"S57 {key} contains duplicate selections")
            if key == "classes" and any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,39}", v) for v in values):
                raise ValueError("S57 classes require exact GDAL object-class acronyms")
            result[key] = list(values)
    return result


def _safe_path(name, *, directory=False):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or any(ord(c) < 32 for c in name):
        raise ValueError("S57 ZIP contains an unsafe member path")
    path = PurePosixPath(name.rstrip("/") if directory else name)
    if path.is_absolute() or ".." in path.parts or "." in name.split("/") or "" in name.rstrip("/").split("/"):
        raise ValueError("S57 ZIP contains an unsafe member path")
    if path.as_posix() != name.rstrip("/") or len(name.encode("utf-8")) > 512:
        raise ValueError("S57 ZIP member path is noncanonical or too long")
    return path


def _container(data, filename):
    if not isinstance(data, bytes) or not data or len(data) > MAX_INPUT_BYTES:
        raise ValueError("S57 input must be nonempty bytes of at most 128 MiB")
    path = _safe_path(filename)
    if len(path.parts) != 1:
        raise ValueError("S57 upload filename must be a basename, not a filesystem path")
    source = {"filename": filename, "sha256": _sha(data), "format": "S57", "input_bytes": len(data)}
    suffix = path.suffix.casefold()
    if suffix == ".000":
        source.update(container="000", uncompressed_bytes=len(data))
        return source, [{"path": filename, "base": {"path": filename, "bytes": len(data), "sha256": _sha(data)}, "updates": []}], {filename: data}
    if suffix != ".zip":
        raise ValueError("S57 requires a native .000 base or a .zip exchange set; update files alone are not bases")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("S57 ZIP is invalid") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS or sum(x.file_size for x in infos) > MAX_INPUT_BYTES:
            raise ValueError("S57 ZIP exceeds 1000 members or 128 MiB uncompressed bytes")
        names = {}
        for item in infos:
            _safe_path(item.filename, directory=item.is_dir())
            if item.flag_bits & 1 or stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError("S57 ZIP encrypted members and symlinks are unsupported")
            if item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ValueError("S57 ZIP requires stored/deflated members; other compression methods are unsupported")
            folded = item.filename.rstrip("/").casefold()
            if folded in names:
                raise ValueError("S57 ZIP contains duplicate or case-conflicting member paths")
            names[folded] = item
        try:
            if archive.testzip() is not None:
                raise ValueError("S57 ZIP CRC failure")
            files = {x.filename: archive.read(x) for x in infos if not x.is_dir() and
                     re.fullmatch(r"\.[0-9]{3}", PurePosixPath(x.filename).suffix)}
            auxiliary = [{"path": x.filename, "bytes": x.file_size,
                          "sha256": _sha(archive.read(x)), "content_interpreted": False}
                         for x in infos if not x.is_dir() and
                         (x.filename not in files or PurePosixPath(x.filename).name.casefold() == "catalog.031")]
        except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, EOFError, zlib.error) as exc:
            raise ValueError("S57 ZIP could not be safely decoded") from exc
    bases = sorted(n for n in files if PurePosixPath(n).suffix == ".000")
    if not bases or len(bases) > 128:
        raise ValueError("S57 ZIP requires 1 to 128 native .000 cell bases")
    groups = {str(PurePosixPath(n).with_suffix("")).casefold(): n for n in bases}
    updates = {n: [] for n in bases}
    for n, payload in files.items():
        number = int(PurePosixPath(n).suffix[1:])
        if not number:
            continue
        # The exchange catalogue uses ISO8211 too but is not a cell update.
        if PurePosixPath(n).name.casefold() == "catalog.031":
            continue
        base = groups.get(str(PurePosixPath(n).with_suffix("")).casefold())
        if base is None:
            raise ValueError("S57 ZIP update has no same-directory same-stem .000 base: " + n)
        updates[base].append({"path": n, "number": number, "bytes": len(payload), "sha256": _sha(payload)})
    cells = []
    for n in bases:
        rows = sorted(updates[n], key=lambda x: x["number"])
        if any(b["number"] != a["number"]+1 for a, b in zip(rows, rows[1:])):
            raise ValueError("S57 update sequence has a missing number: " + n)
        cells.append({"path": n, "base": {"path": n, "bytes": len(files[n]), "sha256": _sha(files[n])}, "updates": rows})
    source.update(container="zip", uncompressed_bytes=sum(x.file_size for x in infos), member_count=len(infos), auxiliary_files=auxiliary)
    return source, cells, files


def _selections(cells, config, *, required):
    paths = {c["path"] for c in cells}
    selected = config.get("cells")
    if selected is None:
        if len(cells) == 1:
            selected = [cells[0]["path"]]
        elif required:
            raise ValueError("S57 ZIP contains multiple cells; explicitly select exact paths in config.cells")
        else:
            selected = []
    if set(selected)-paths:
        raise ValueError("S57 selected cell path does not exist: " + ", ".join(sorted(set(selected)-paths)))
    if len(selected) > config["max_cells"]:
        raise ValueError("S57 selected cells exceed max_cells")
    return selected


def _envelope(stage, source, cells):
    return {"schema": SCHEMA, "stage": stage, "accepted": True, "can_apply": stage == "import",
            "source": source, "output_crs": "EPSG:4326", "cells": cells, "classes_catalog": [],
            "missing_classes_by_cell": [], "layers": [], "warnings": [], "summary": {}}


def inspect_s57(data: bytes, *, filename: str, config: dict | None = None) -> dict:
    """Validate supplied container/sequence and list cell candidates, without GDAL."""
    c = _config(config)
    source, cells, _ = _container(data, filename)
    selected = _selections(cells, c, required=False)
    for cell in cells:
        cell["selected"] = cell["path"] in selected
        cell["sequence_status"] = "base-update-number-awaits-native-validation"
    result = _envelope("inspect", source, cells)
    result["summary"] = {"available_cells": len(cells), "selected_cells": len(selected),
                         "native_read_performed": False, "update_files": sum(len(v["updates"]) for v in cells)}
    result["reader"] = {"native": False, "class_counts_known": False}
    charge = math.ceil(source["uncompressed_bytes"]/256)
    if charge > c["max_work_units"]:
        raise ValueError("S57 container exceeds max_work_units before native reader")
    result["budget"] = {"work_units": charge, "max_work_units": c["max_work_units"],
                        "work_basis": "normalized supplied uncompressed bytes / 256; not CPU time or GDAL FLOPs"}
    return _finalize(result, c)


def catalog_s57(data: bytes, *, filename: str, config: dict | None = None) -> dict:
    """Native DSID and class counts/schema; no object geometry/attribute arrays."""
    return _run(data, filename, config, "catalog")


def import_s57(data: bytes, *, filename: str, config: dict | None = None) -> dict:
    """Native updated features, separated by cell/object class as reference GIS."""
    return _run(data, filename, config, "import")


def _run(data, filename, config, stage):
    c = _config(config)
    source, cells, files = _container(data, filename)
    selected = _selections(cells, c, required=True)
    charge = math.ceil(source["uncompressed_bytes"]/256)
    if charge > c["max_work_units"]:
        raise ValueError("S57 container exceeds max_work_units before native reader")
    if len(_json(_envelope(stage, source, cells))) > c["max_output_bytes"]:
        raise ValueError("S57 minimum source catalog exceeds max_output_bytes before native reader")
    try:
        import importlib.util
        if importlib.util.find_spec("pyogrio") is None:
            raise ValueError("S57 native reader requires pyogrio and its GDAL S57 driver")
    except (ImportError, ModuleNotFoundError) as exc:
        raise ValueError("S57 native reader requires pyogrio and its GDAL S57 driver") from exc
    with tempfile.TemporaryDirectory(prefix="oceanroute-s57-") as temporary:
        directory = Path(temporary)
        supplied = []
        for cell in cells:
            if cell["path"] not in selected:
                continue
            cell = deepcopy(cell)
            # Normalize case on the private filenames used for adjacent updates.
            # Original paths/hashes remain in every public provenance record.
            folder = directory / ("cell-" + str(len(supplied)))
            folder.mkdir()
            base_path = folder / "chart.000"
            base_path.write_bytes(files[cell["path"]])
            for update in cell["updates"]:
                (folder/f"chart.{update['number']:03d}").write_bytes(files[update["path"]])
            cell["worker_path"] = str(base_path)
            supplied.append(cell)
        job = {"stage": stage, "source": source, "cells": supplied, "config": c, "initial_work": charge}
        jobfile = directory / "job.json"
        jobfile.write_bytes(_json(job))
        environment = os.environ.copy()
        for key in ("OGR_S57_OPTIONS", "S57_CSV", "GDAL_DATA", "GDAL_DRIVER_PATH", "GDAL_SKIP", "OGR_SKIP",
                    "CPL_DEBUG", "CPL_LOG", "GDAL_CONFIG_FILE", "PROJ_LIB", "PROJ_DATA"):
            environment.pop(key, None)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        environment["PYTHONNOUSERSITE"] = "1"
        try:
            completed = subprocess.run([sys.executable, "-B", "-m", "oceanroute.s57", "--worker", str(jobfile)],
                cwd=directory, env=environment, timeout=c["timeout_s"], capture_output=True)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("S57 native reader exceeded timeout_s; no partial chart is returned") from exc
        if len(completed.stdout) > c["max_output_bytes"]:
            raise ValueError("S57 native result exceeds max_output_bytes")
        try:
            response = json.loads(completed.stdout)
        except (ValueError, UnicodeError) as exc:
            raise ValueError("S57 native worker failed without a complete JSON result: " + completed.stderr.decode("utf-8", "replace")[:1000]) from exc
        if completed.returncode or "error" in response:
            raise ValueError("S57 native reader rejected input: " + str(response.get("error", "worker terminated"))[:2000])
        if completed.stderr.strip():
            # Native CPL errors which bypass Python warnings must not be hidden.
            raise ValueError("S57 native reader emitted diagnostics; no partial chart is returned: " + completed.stderr.decode("utf-8", "replace")[:1000])
        if response.get("schema") != SCHEMA or response.get("source") != source:
            raise ValueError("S57 worker result does not match the supplied source")
        _json(response)
        return response


def _scalar(value):
    """Keep OGR scalars/lists and convert its numeric-null NaN representation."""
    import numpy as np
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (float, np.floating)):
        number = float(value)
        if math.isnan(number):
            return None
        if not math.isfinite(number):
            raise ValueError("native S57 attribute contains infinity")
        return number
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_scalar(v) for v in value]
    if isinstance(value, np.datetime64):
        return None if np.isnat(value) else str(value)
    raise ValueError("unsupported native S57 attribute type: " + type(value).__name__)


class _Budget:
    def __init__(self, config, initial):
        self.config = config
        self.work = initial
        self.native_calls = 0
        self.native_input_bytes = 0
        self.features = 0
        self.vertices = 0
        self.attribute_bytes = 0

    def add(self, amount):
        self.work += int(amount)
        if self.work > self.config["max_work_units"]:
            raise ValueError("S57 exceeds max_work_units; select fewer cells/classes or explicitly raise the bounded budget")

    def native(self, source_bytes):
        self.add(math.ceil(source_bytes/256)+1000)
        self.native_calls += 1
        self.native_input_bytes += source_bytes


def _geometry(wkb, budget):
    from shapely import from_wkb
    from shapely.geometry import mapping
    try:
        geom = from_wkb(wkb)
    except Exception as exc:
        raise ValueError("native S57 WKB geometry cannot be decoded") from exc
    if geom.is_empty:
        return None
    allowed = {"Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon", "GeometryCollection"}
    if geom.geom_type not in allowed or not geom.is_valid:
        raise ValueError("native S57 geometry is unsupported or invalid: " + geom.geom_type)
    value = mapping(geom)
    def visit(g):
        if g["type"] == "GeometryCollection":
            for child in g["geometries"]:
                visit(child)
            return
        def coords(a):
            if a and isinstance(a[0], (int, float)):
                if len(a) not in (2, 3) or not all(math.isfinite(v) for v in a) or abs(a[0]) > 180 or abs(a[1]) > 90:
                    raise ValueError("native S57 geometry contains nonfinite/out-of-WGS84 coordinates")
                budget.vertices += 1
                if budget.vertices > budget.config["max_vertices"]:
                    raise ValueError("S57 exceeds max_vertices; select fewer classes")
                budget.add(6)
            else:
                for child in a:
                    coords(child)
        coords(g["coordinates"])
    visit(value)
    return value


def _native(job):
    import warnings
    import pyogrio
    from pyogrio.raw import read
    from pyproj import CRS
    if "r" not in pyogrio.list_drivers().get("S57", ""):
        raise ValueError("installed GDAL has no native S57 read driver")
    # list_layers lacks open-option kwargs. This is process-local configuration,
    # never a shared API/GDAL session, and all read_info/raw calls repeat options.
    pyogrio.set_gdal_config_options({"OGR_S57_OPTIONS": ",".join(k+"="+v for k, v in OPTIONS.items())})
    c = job["config"]
    budget = _Budget(c, job["initial_work"])
    result = _envelope(job["stage"], job["source"], [])
    result["warnings"] = [
        {"code": "REFERENCE_CHART_ONLY", "severity": "info", "message": "Native S57 object layers are reference GIS, not S52/ECDIS navigation portrayal or verified engineering water depth."},
        {"code": "SOUNDING_DATUM_NOT_CONVERTED", "severity": "warning", "message": "SOUNDG Z retains the encoded signed chart sounding value with a positive-down depth convention and declared units/datum; no tide, ellipsoid-height or model-sea-surface conversion is performed."}]
    native_warnings = []
    aggregate = {}
    native_layers = 0
    geometric_features = 0
    def native_call(fn, cell, **kwargs):
        size = cell["base"]["bytes"]+sum(u["bytes"] for u in cell["updates"])
        budget.native(size)
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            answer = fn(cell["worker_path"], **kwargs)
        for warning in captured:
            message = str(warning.message)
            # This specific GDAL warning concerns processing cost, not missing
            # data or invalid topology. Do NOT change METHOD, skip organization,
            # or discard holes. Geometry is independently validated below.
            if message.startswith("organizePolygons() received a polygon with more than 100 parts. The processing may be really slow."):
                row = {"code": "NATIVE_POLYGON_PROCESSING_COST", "severity": "warning", "message": message,
                       "cell": cell["path"], "layer": kwargs.get("layer"), "geometry_organization_skipped": False}
                if row not in native_warnings:
                    native_warnings.append(row)
                continue
            raise ValueError("native GDAL warning: " + message[:1500])
        return answer
    def dsid(cell, updates):
        options = {**OPTIONS, "UPDATES": updates}
        meta, fids, _, arrays = native_call(read, cell, layer="DSID", return_fids=True, max_features=2, **options)
        if fids is None or len(fids) != 1:
            raise ValueError("S57 must contain exactly one native DSID/DSPM record")
        return {str(k): _scalar(v[0]) for k, v in zip(meta["fields"], arrays)}
    for cell in job["cells"]:
        base = dsid(cell, "IGNORE")
        try:
            base_number = int(base["DSID_UPDN"])
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError("S57 base DSID_UPDN is missing or invalid") from exc
        if not 0 <= base_number <= 999:
            raise ValueError("S57 base update number is out of range")
        identity = base.get("DSID_DSNM")
        edition = base.get("DSID_EDTN")
        if (not isinstance(identity, str) or PurePosixPath(identity).suffix != ".000"
                or not isinstance(edition, str) or not edition.isdigit() or int(edition) <= 0
                or base.get("DSID_EXPP") != 1):
            raise ValueError("native S57 base must declare .000 identity, positive edition and base exchange purpose; cancelled editions are not imported")
        expected_updates = list(range(base_number+1, base_number+1+len(cell["updates"])))
        if [u["number"] for u in cell["updates"]] != expected_updates:
            raise ValueError("S57 updates must start immediately after the native base DSID_UPDN and be consecutive")
        # GDAL APPLY alone is not a validation certificate for each update.
        # Read every supplied update's native DSID in IGNORE mode and bind its
        # declared cell, edition, agency and sequence before applying anything.
        update_headers = []
        for update in cell["updates"]:
            own = deepcopy(cell)
            own["worker_path"] = str(Path(cell["worker_path"]).with_suffix(f".{update['number']:03d}"))
            own["base"] = update
            own["updates"] = []
            header = dsid(own, "IGNORE")
            identity = header.get("DSID_DSNM")
            base_identity = base.get("DSID_DSNM")
            if (not isinstance(identity, str) or not isinstance(base_identity, str)
                    or PurePosixPath(identity).stem.casefold() != PurePosixPath(base_identity).stem.casefold()
                    or PurePosixPath(identity).suffix != f".{update['number']:03d}"
                    or header.get("DSID_EDTN") != base.get("DSID_EDTN")
                    or header.get("DSID_AGEN") != base.get("DSID_AGEN")
                    or header.get("DSID_EXPP") != 2
                    or int(header.get("DSID_UPDN", -1)) != update["number"]):
                raise ValueError("S57 update DSID identity/edition/agency/purpose/number does not match its base and suffix")
            update_headers.append(header)
        if base_number > 0 and cell["updates"]:
            raise ValueError("S57 native-driver limitation: legitimate reissued base with positive DSID_UPDN and subsequent updates is unsupported by the current GDAL APPLY path; no update is omitted or renumbered")
        after = dsid(cell, "APPLY")
        if after.get("DSID_EDTN") != base.get("DSID_EDTN") or after.get("DSID_DSNM") != base.get("DSID_DSNM") or int(after.get("DSID_UPDN", -1)) != base_number+len(cell["updates"]):
            raise ValueError("native S57 update application did not preserve edition/cell identity or reach supplied update number")
        if after.get("DSPM_HDAT") != 2 or after.get("DSPM_COUN") != 1:
            raise ValueError("S57 ENC requires explicit WGS84 HDAT=2 and geographic coordinate units COUN=1; other datums are unsupported")
        catalog = native_call(pyogrio.list_layers, cell)
        native_layers += len(catalog)
        if native_layers > c["max_layers"]:
            raise ValueError("S57 native layers exceed max_layers")
        public = {k: deepcopy(v) for k, v in cell.items() if k != "worker_path"}
        for update, header in zip(public["updates"], update_headers):
            update["dsid"] = header
        public.update(selected=True, dsid=after, base_dsid=base,
                      applied_update_number=base_number+len(cell["updates"]), sequence_status="native-base-and-APPLY-verified",
                      datum_units={"horizontal_datum_code": after.get("DSPM_HDAT"), "vertical_datum_code": after.get("DSPM_VDAT"),
                                   "sounding_datum_code": after.get("DSPM_SDAT"), "depth_unit_code": after.get("DSPM_DUNI"),
                                   "depth_units": "m" if after.get("DSPM_DUNI") == 1 else None,
                                   "height_unit_code": after.get("DSPM_HUNI"),
                                   "height_units": "m" if after.get("DSPM_HUNI") == 1 else None,
                                   "positional_accuracy_unit_code": after.get("DSPM_PUNI"),
                                   "coordinate_unit_code": after.get("DSPM_COUN"),
                                   "soundings_z_positive": "down", "soundings_z_is_model_height": False, "units_converted": False},
                      object_classes=[])
        if after.get("DSPM_DUNI") != 1:
            result["warnings"].append({"code": "UNKNOWN_SOUNDING_UNITS", "severity": "warning", "message": "Depth units are retained by DSPM_DUNI code without conversion: " + cell["path"]})
        available = []
        for name, listed_geometry in catalog:
            name = str(name)
            if name == "DSID":
                continue
            if name in {"Generic", "Unknown"}:
                raise ValueError("native S57 object class is unresolved: " + name)
            info = native_call(pyogrio.read_info, cell, layer=name, force_feature_count=True, **OPTIONS)
            if info["driver"] != "S57" or info["features"] < 0:
                raise ValueError("native S57 class feature count or driver is unresolved")
            if info["crs"] is not None and not CRS.from_user_input(info["crs"]).equals(CRS.from_epsg(4326), ignore_axis_order=True):
                raise ValueError("native S57 object CRS is not WGS84")
            fields = [{"name": str(n), "dtype": str(t), "ogr_type": str(o), "ogr_subtype": str(s)}
                      for n, t, o, s in zip(info["fields"], info["dtypes"], info["ogr_types"], info["ogr_subtypes"])]
            row = {"name": name, "geometry_type": _scalar(info["geometry_type"]), "feature_count": int(info["features"]),
                   "selected": "classes" not in c or name in c["classes"], "fields": fields,
                   "crs": info["crs"], "encoding": info["encoding"]}
            public["object_classes"].append(row)
            available.append(name)
            budget.add(int(info["features"])+len(fields)*4)
            record = aggregate.setdefault(name, {"name": name, "feature_count": 0, "cells": [], "geometry_types": []})
            record["feature_count"] += int(info["features"])
            record["cells"].append(cell["path"])
            if row["geometry_type"] not in record["geometry_types"]:
                record["geometry_types"].append(row["geometry_type"])
        missing = sorted(set(c.get("classes", []))-set(available))
        if missing:
            result["missing_classes_by_cell"].append({"cell": cell["path"], "classes": missing})
        result["cells"].append(public)
    if set(c.get("classes", []))-set(aggregate):
        raise ValueError("selected S57 class does not exist in any selected cell: " + ", ".join(sorted(set(c["classes"])-set(aggregate))))
    result["classes_catalog"] = [aggregate[n] for n in sorted(aggregate)]
    if len(_json(result)) > c["max_output_bytes"]:
        raise ValueError("S57 native class catalog exceeds max_output_bytes before geometry/attribute arrays")
    if job["stage"] == "import":
        selected_count = sum(row["feature_count"] for cell in result["cells"] for row in cell["object_classes"] if row["selected"])
        if selected_count > c["max_features"]:
            raise ValueError("selected S57 classes exceed max_features before geometry/attribute arrays; select fewer classes")
        if any(row["selected"] and row["feature_count"] > 20000 for cell in result["cells"] for row in cell["object_classes"]):
            raise ValueError("S57 class layer exceeds the shared-workspace 20000-feature limit; choose smaller cells/classes")
        for cell, public in zip(job["cells"], result["cells"]):
            for row in public["object_classes"]:
                if not row["selected"] or not row["feature_count"]:
                    continue
                meta, fids, geometries, arrays = native_call(read, cell, layer=row["name"], return_fids=True,
                    max_features=row["feature_count"]+1, force_2d=False, datetime_as_string=True, **OPTIONS)
                if (fids is None or len(fids) != row["feature_count"]
                        or list(map(str, meta["fields"])) != [f["name"] for f in row["fields"]]):
                    raise ValueError("native S57 feature/schema changed between catalog and import")
                features, nulls, actual_types = [], 0, set()
                for i, fid in enumerate(fids):
                    budget.features += 1
                    budget.add(10+len(arrays)*2)
                    properties = {str(k): _scalar(v[i]) for k, v in zip(meta["fields"], arrays)}
                    attributes = len(_json(properties))
                    budget.attribute_bytes += attributes
                    budget.add(math.ceil(attributes/64))
                    if budget.attribute_bytes > c["max_output_bytes"]:
                        raise ValueError("S57 attributes exceed max_output_bytes")
                    geometry = None if geometries is None or geometries[i] is None else _geometry(geometries[i], budget)
                    if geometry is None:
                        nulls += 1
                    else:
                        if row["name"] == "SOUNDG":
                            coordinates = geometry.get("coordinates", [])
                            positions = [coordinates] if geometry["type"] == "Point" else coordinates
                            if geometry["type"] not in {"Point", "MultiPoint"} or any(len(q) != 3 for q in positions):
                                raise ValueError("native SOUNDG must retain finite three-dimensional sounding coordinates")
                        geometric_features += 1
                        actual_types.add(geometry["type"])
                    features.append({"type": "Feature", "id": int(fid), "properties": properties, "geometry": geometry})
                if nulls:
                    result["warnings"].append({"code": "NONSPATIAL_FEATURES_RETAINED", "severity": "warning", "message": f"{cell['path']} / {row['name']}: {nulls} features retain attributes with null geometry."})
                identity = job["source"]["sha256"]+":"+cell["path"]+":"+row["name"]
                # Applications persist layers independently of this bundle.
                # Bind every persisted layer to the actual uploaded file chain
                # and native metadata, not just the aggregate ZIP digest.
                evidence = {k: deepcopy(public[k]) for k in
                            ("path", "base", "updates", "base_dsid", "dsid", "applied_update_number", "sequence_status")}
                reader_evidence = {"driver": "S57", "pyogrio_version": pyogrio.__version__,
                                   "gdal_version": pyogrio.__gdal_version_string__, "options": deepcopy(OPTIONS),
                                   "process_isolated": True,
                                   "reader_warnings": [deepcopy(w) for w in native_warnings
                                                       if w["cell"] == cell["path"] and w["layer"] in (None, row["name"])]}
                budget.add(math.ceil((len(_json(evidence))+len(_json(reader_evidence)))/64))
                result["layers"].append({"id": str(uuid5(NAMESPACE_URL, identity)), "name": c["name"]+" / "+PurePosixPath(cell["path"]).stem+" / "+row["name"],
                    "kind": "reference", "visible": True, "crs": "EPSG:4326", "geojson": {"type": "FeatureCollection", "features": features},
                    "source": {"format": "S57", "source_sha256": job["source"]["sha256"], "cell": cell["path"], "object_class": row["name"],
                               "output_crs": "EPSG:4326", "fields": row["fields"], "datum_units": public["datum_units"],
                               "geometry_z_semantics": "positive_sounding_depth_in_declared_chart_units" if row["name"] == "SOUNDG" else "native_optional_z_uninterpreted",
                               "feature_identity_semantics": "LNAM retains exact hexadecimal AGEN/FIDN/FIDS identity; GDAL FIDN may expose unsigned source values as signed int32",
                               "cell_evidence": evidence, "native_reader": reader_evidence,
                               "applied_update_number": public["applied_update_number"], "depth_is_engineering_water_depth": False},
                    "diagnostics": {"feature_count": len(features), "null_geometry_count": nulls, "actual_geometry_types": sorted(actual_types)}})
        if not geometric_features:
            raise ValueError("selected S57 cells/classes have no supported nonempty geometry; no empty import success")
    result["summary"] = {"selected_cells": len(result["cells"]), "native_layers": native_layers, "available_object_classes": len(aggregate),
        "imported_layers": len(result["layers"]), "imported_features": budget.features, "imported_geometric_features": geometric_features,
        "imported_vertices": budget.vertices, "attribute_bytes": budget.attribute_bytes, "native_read_performed": True,
        "update_files_applied": sum(len(x["updates"]) for x in job["cells"])}
    result["warnings"].extend(native_warnings)
    result["reader"] = {"native": True, "driver": "S57", "pyogrio_version": pyogrio.__version__, "gdal_version": pyogrio.__gdal_version_string__,
                        "options": OPTIONS, "process_isolated": True, "reader_warnings": native_warnings,
                        "policy": "only the explicit organizePolygons processing-cost warning is retained without changing organization; other native warnings/unsupported input reject atomically; null records retained"}
    result["budget"] = {"work_units": budget.work, "max_work_units": c["max_work_units"], "native_open_read_calls": budget.native_calls,
                        "native_input_bytes_charged": budget.native_input_bytes, "max_features": c["max_features"], "max_vertices": c["max_vertices"],
                        "max_cells": c["max_cells"], "max_layers": c["max_layers"], "max_layer_features": 20000,
                        "max_output_bytes": c["max_output_bytes"], "timeout_s": c["timeout_s"],
                        "work_basis": "normalized container bytes/256 + each native call cell-bytes/256+1000 + catalog counts/field schemas + imported records/vertices/attribute and persisted provenance bytes; not GDAL operation/FLOP count; wall timeout separately bounds native work"}
    return _finalize(result, c)


def _worker_main(filename):
    try:
        result = _native(json.loads(Path(filename).read_bytes()))
        sys.stdout.buffer.write(_json(result))
    except Exception as exc:
        sys.stdout.buffer.write(_json({"error": str(exc)[:2000]}))
        raise SystemExit(1)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--worker":
        raise SystemExit("S57 worker is an internal isolated native reader")
    _worker_main(sys.argv[2])
