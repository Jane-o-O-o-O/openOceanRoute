"""Actual native NOAA ENC fixtures; safety/capacity and lossless reference data."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import io
import json
import math
from pathlib import Path
import stat
import zipfile

import numpy as np
import pyogrio
from pyogrio.raw import read
import pytest
from shapely import from_wkb
from shapely.geometry import shape

from oceanroute.s57 import import_s57, inspect_s57, catalog_s57, OPTIONS

FIXTURES = Path(__file__).parent / "fixtures/s57/noaa"
CELL = "ENC_ROOT/US5A1KMJ/US5A1KMJ.000"


def data(cell="US5A1KMJ"):
    return (FIXTURES / (cell+".zip")).read_bytes()


def bundle(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name, payload in entries:
            z.writestr(name, payload)
    return buffer.getvalue()


def cell_files(cell="US5A1KMJ"):
    with zipfile.ZipFile(io.BytesIO(data(cell))) as z:
        return [(n, z.read(n)) for n in z.namelist() if n.endswith((".000", ".001", ".002"))]


def native(cls, *, updates="APPLY", cell="US5A1KMJ"):
    return read(FIXTURES/cell/(cell+".000"), layer=cls, return_fids=True,
                force_2d=False, **{**OPTIONS, "UPDATES": updates})


def find_layer(result, name):
    return next(l for l in result["layers"] if l["source"]["object_class"] == name)


def test_container_stage_preserves_exact_source_paths_updates_and_no_native_guesses():
    payload = data()
    result = inspect_s57(payload, filename="US5A1KMJ.zip")
    assert result["source"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["cells"][0]["path"] == CELL
    assert [u["number"] for u in result["cells"][0]["updates"]] == [1, 2]
    assert "dsid" not in result["cells"][0] and "object_classes" not in result["cells"][0]
    assert not result["reader"]["native"] and not result["can_apply"]
    assert any(x["path"].endswith("CATALOG.031") for x in result["source"]["auxiliary_files"])
    assert result["budget"]["output_bytes"] == len(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode())


def test_native_catalog_applies_updates_and_counts_without_importing_objects():
    result = catalog_s57(data(), filename="US5A1KMJ.zip", config={"max_features": 1, "max_vertices": 1})
    cell = result["cells"][0]
    assert cell["base_dsid"]["DSID_UPDN"] == "0"
    assert cell["dsid"]["DSID_UPDN"] == "2" and cell["dsid"]["DSPM_HDAT"] == 2
    assert cell["datum_units"]["sounding_datum_code"] == 12
    assert cell["datum_units"]["depth_units"] == "m" and not cell["datum_units"]["units_converted"]
    assert [u["dsid"]["DSID_UPDN"] for u in cell["updates"]] == ["1", "2"]
    classes = {x["name"]:x for x in cell["object_classes"]}
    assert classes["SOUNDG"]["feature_count"] == 4 and classes["SOUNDG"]["geometry_type"] == "MultiPoint Z"
    assert classes["DEPARE"]["geometry_type"] == "Unknown"  # legitimate mixed S57 schema
    assert not result["layers"] and not result["can_apply"]
    assert any(x["name"] == "LNAM_REFS" and x["dtype"] == "list(str)" for x in classes["SOUNDG"]["fields"])


def test_soundings_native_multipoint_z_all_attributes_lists_and_nulls_are_preserved():
    result = import_s57(data(), filename="chart.zip", config={"classes":["SOUNDG"]})
    layer = find_layer(result,"SOUNDG")
    meta, fids, geometries, arrays = native("SOUNDG")
    actual = layer["geojson"]["features"]
    assert len(actual) == len(fids) == 4
    expected_points = 0
    for feature, fid, wkb, i in zip(actual, fids, geometries, range(len(fids))):
        geom = from_wkb(wkb)
        expected_points += len(geom.geoms)
        assert feature["id"] == fid and feature["geometry"]["type"] == "MultiPoint"
        assert np.asarray(feature["geometry"]["coordinates"]) == pytest.approx(np.array([list(p.coords[0]) for p in geom.geoms]), abs=0)
        assert set(feature["properties"]) == set(meta["fields"])
        for name, values in zip(meta["fields"], arrays):
            value = values[i]
            if value is None or isinstance(value, float) and math.isnan(value):
                assert feature["properties"][name] is None
            elif isinstance(value,np.ndarray):
                assert feature["properties"][name] == value.tolist()
            else:
                assert feature["properties"][name] == value
    assert expected_points == result["summary"]["imported_vertices"] == 657
    assert actual[0]["geometry"]["coordinates"][0] == pytest.approx([177.5191417,51.9181095,23.7])
    assert layer["kind"] == "reference" and not layer["source"]["depth_is_engineering_water_depth"]
    assert layer["source"]["geometry_z_semantics"] == "positive_sounding_depth_in_declared_chart_units"
    assert result["can_apply"] and result["reader"]["options"]["UPDATES"] == "APPLY"
    json.dumps(result,allow_nan=False)


def test_polygon_holes_and_line_coordinates_match_the_original_native_driver():
    result = import_s57(data(),filename="chart.zip",config={"classes":["DEPARE","DEPCNT"]})
    hole_count = 0
    for cls in ["DEPARE","DEPCNT"]:
        _, fids, geometries, _ = native(cls)
        features = find_layer(result,cls)["geojson"]["features"]
        assert [f["id"] for f in features] == fids.tolist()
        for feature, wkb in zip(features,geometries):
            original = from_wkb(wkb); actual = shape(feature["geometry"])
            assert original.equals_exact(actual,0)
            if actual.geom_type=="Polygon":hole_count += len(actual.interiors)
    assert hole_count > 0


def test_real_native_update_changes_attributes_and_base_upload_reads_no_neighbours():
    base = (FIXTURES/"US5A1KMJ/US5A1KMJ.000").read_bytes()
    old = import_s57(base,filename="US5A1KMJ.000",config={"classes":["LNDELV"]})
    updated = import_s57(data(),filename="chart.zip",config={"classes":["LNDELV"]})
    assert old["cells"][0]["applied_update_number"] == 0
    assert updated["cells"][0]["applied_update_number"] == 2
    before = {f["properties"]["RCID"]:f["properties"] for f in old["layers"][0]["geojson"]["features"]}
    after = {f["properties"]["RCID"]:f["properties"] for f in updated["layers"][0]["geojson"]["features"]}
    assert before[1016]["ELEVAT"] != after[1016]["ELEVAT"]


def test_multiple_cells_require_exact_explicit_selection_and_no_ambiguous_basename():
    payload = bundle(cell_files()+cell_files("US5A1KMK"))
    listed = inspect_s57(payload,filename="two.zip")
    assert len(listed["cells"]) == 2 and listed["summary"]["selected_cells"] == 0
    with pytest.raises(ValueError,match="multiple cells"):
        catalog_s57(payload,filename="two.zip")
    with pytest.raises(ValueError,match="does not exist"):
        import_s57(payload,filename="two.zip",config={"cells":["US5A1KMJ.000"]})
    result = import_s57(payload,filename="two.zip",config={"cells":[CELL],"classes":["SOUNDG"]})
    assert [v["path"] for v in result["cells"]] == [CELL] and len(result["layers"]) == 1


def test_full_native_import_keeps_polygon_cost_warning_without_skipping_organization():
    result=import_s57(data(),filename="chart.zip")
    assert result["summary"]["imported_features"] > 1000
    warnings=result["reader"]["reader_warnings"]
    assert warnings and warnings[0]["code"]=="NATIVE_POLYGON_PROCESSING_COST"
    assert not warnings[0]["geometry_organization_skipped"]
    assert warnings[0] in result["warnings"]
    assert result["budget"]["output_bytes"] <= result["budget"]["max_output_bytes"]


@pytest.mark.parametrize("config",[{"classes":[]},{"cells":[]},{"classes":["SOUNDG","SOUNDG"]},
    {"cells":["not.000"]},{"max_vertices":250001},{"max_work_units":True},{"timeout_s":float("nan")},
    {"unknown":1},{"max_features":10**1000},{"classes":[1]},None])
def test_strict_bad_config_does_not_invoke_native_reader(monkeypatch,config):
    if config is None:config=[]
    monkeypatch.setattr("oceanroute.s57.subprocess.run",lambda *a,**k:pytest.fail("invalid config launched native reader"))
    with pytest.raises(ValueError):
        import_s57(data(),filename="chart.zip",config=config)


@pytest.mark.parametrize("entries",[
    [("../chart.000",b"x")],[("/chart.000",b"x")],[("folder\\chart.000",b"x")],
    [("chart.000",b"x"),("CHART.000",b"x")],[("chart.001",b"x")],
    [("chart.000",b"x"),("chart.001",b"x"),("chart.003",b"x")],
    [("chart.000",b"x"),("other.001",b"x")],[("C:/chart.000",b"x")],
])
def test_paths_duplicates_and_update_gaps_reject_before_native(entries,monkeypatch):
    monkeypatch.setattr("oceanroute.s57.subprocess.run",lambda *a,**k:pytest.fail("bad ZIP launched native reader"))
    with pytest.raises(ValueError):inspect_s57(bundle(entries),filename="bad.zip")


def test_symlink_and_corrupt_native_data_do_not_return_empty_success():
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,"w") as z:
        info=zipfile.ZipInfo("chart.000");info.external_attr=(stat.S_IFLNK|0o777)<<16;z.writestr(info,b"target")
    with pytest.raises(ValueError,match="symlinks"):inspect_s57(buffer.getvalue(),filename="bad.zip")
    with pytest.raises(ValueError,match="native reader rejected"):
        import_s57(b'{"type":"FeatureCollection","features":[]}',filename="chart.000")


def test_unknown_classes_and_all_real_resource_caps_fail_atomically():
    for config, pattern in [({"classes":["ABSENT"]},"does not exist"),
                            ({"max_layers":1},"max_layers"),({"max_work_units":1},"before native"),
                            ({"max_output_bytes":1},"before native"),
                            ({"classes":["SOUNDG"],"max_features":3},"max_features"),
                            ({"classes":["SOUNDG"],"max_vertices":656},"max_vertices")]:
        with pytest.raises(ValueError,match=pattern):import_s57(data(),filename="chart.zip",config=config)


def test_native_process_config_isolation_under_concurrent_imports():
    before=pyogrio.get_gdal_config_option("OGR_S57_OPTIONS")
    def imported(filename):return import_s57(data(),filename=filename,config={"classes":["SOUNDG"]})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first,second=list(pool.map(imported,["first.zip","second.zip"]))
    assert pyogrio.get_gdal_config_option("OGR_S57_OPTIONS")==before
    assert first["reader"]["process_isolated"] and second["reader"]["process_isolated"]
    assert first["layers"][0]["geojson"]==second["layers"][0]["geojson"]


def test_persisted_layer_reopens_with_exact_native_file_chain_and_reader_evidence(tmp_path):
    from oceanroute.workspace_storage import WorkspaceStore

    result = import_s57(data(), filename="US5A1KMJ.zip", config={"classes": ["SOUNDG"]})
    source = result["layers"][0]["source"]
    cell = result["cells"][0]
    proof = source["cell_evidence"]
    assert set(proof) == {"path", "base", "updates", "base_dsid", "dsid",
                          "applied_update_number", "sequence_status"}
    assert all(proof[key] == cell[key] for key in proof)
    with zipfile.ZipFile(io.BytesIO(data())) as archive:
        for file in [proof["base"], *proof["updates"]]:
            assert file["sha256"] == hashlib.sha256(archive.read(file["path"])).hexdigest()
    assert proof["base_dsid"]["DSID_UPDN"] == "0"
    assert proof["dsid"]["DSID_UPDN"] == "2"
    assert source["native_reader"]["options"] == OPTIONS
    assert source["native_reader"]["gdal_version"] == pyogrio.__gdal_version_string__
    assert "worker_path" not in json.dumps(source) and "oceanroute-s57" not in json.dumps(source)
    workspace = {"schema_version": 2, "id": "native-chart-reference", "name": "native chart",
                 "currency": "CNY", "active_path_id": None, "paths": [], "assemblies": [],
                 "associations": [], "cable_types": [{"id": "declared-library"}],
                 "layers": result["layers"], "terrain_sources": []}
    store = WorkspaceStore(tmp_path / "reference.sqlite3")
    saved = store.save(json.loads(json.dumps(workspace, ensure_ascii=False, allow_nan=False)))
    reopened = WorkspaceStore(tmp_path / "reference.sqlite3").get(workspace["id"])
    assert reopened["layers"][0]["source"] == source
    assert reopened["layers"] == saved["workspace"]["layers"]
