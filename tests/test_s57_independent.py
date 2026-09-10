"""Independent native ENC checks, using unchanged NOAA ISO8211 bytes.

The small test-only decoder reads fixed fixture fields from the published S57
layout, independently of GDAL/OceanRoute. It is not a second general ENC reader.
Altered fields/archives below are synthetic adversarial inputs, never attributed
to NOAA as original charts. See fixtures/s57/README.md for source and license.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import stat
import struct
import zipfile

import pytest
from shapely.geometry import Polygon, shape

from oceanroute.s57 import catalog_s57, import_s57, inspect_s57


FIXTURES = Path(__file__).parent / "fixtures" / "s57" / "noaa"
J = "US5A1KMJ"
K = "US5A1KMK"


def native(cell=J, extension="000"):
    return (FIXTURES / cell / f"{cell}.{extension}").read_bytes()


def archive(cell=J):
    return (FIXTURES / f"{cell}.zip").read_bytes()


def records(data):
    """Bounded ISO8211 directory/field views; uses no runtime parser/helper."""
    offset = 0
    while offset < len(data):
        leader = data[offset:offset + 24]
        assert len(leader) == 24
        size, base = int(leader[:5]), int(leader[12:17])
        lw, pw, tw = (int(chr(leader[i])) for i in (20, 21, 23))
        record = data[offset:offset + size]
        assert len(record) == size and 24 < base <= size
        assert record[base - 1] == 0x1E
        fields, ranges = {}, {}
        for p in range(24, base - 1, lw + pw + tw):
            tag = record[p:p + tw].decode("ascii")
            length = int(record[p + tw:p + tw + lw])
            position = int(record[p + tw + lw:p + tw + lw + pw])
            assert base + position + length <= size
            fields[tag] = record[base + position:base + position + length]
            ranges[tag] = (offset + base + position, length)
        yield leader, fields, ranges
        offset += size
    assert offset == len(data)


def data_records(data):
    return [f for leader, f, _ in records(data) if leader[6:7] != b"L"]


def integer_dspm(data):
    f = next(r["DSPM"] for r in data_records(data) if "DSPM" in r)
    return {"HDAT": f[5], "VDAT": f[6], "SDAT": f[7],
            "CSCL": int.from_bytes(f[8:12], "little"),
            "DUNI": f[12], "HUNI": f[13], "PUNI": f[14], "COUN": f[15],
            "COMF": int.from_bytes(f[16:20], "little"),
            "SOMF": int.from_bytes(f[20:24], "little")}


def feature_records(data):
    return {int.from_bytes(r["FRID"][1:5], "little"): r
            for r in data_records(data) if "FRID" in r}


def vector_records(data):
    return {(r["VRID"][0], int.from_bytes(r["VRID"][1:5], "little")): r
            for r in data_records(data) if "VRID" in r}


def attributes(payload):
    # ATTF: repeating ATTL=b12 (little u16), ATVL=A terminated by UT.
    result, pos = {}, 0
    while pos < len(payload) - 1:
        code = int.from_bytes(payload[pos:pos + 2], "little")
        end = payload.index(b"\x1f", pos + 2)
        result[code] = payload[pos + 2:end].decode("utf-8")
        pos = end + 1
    assert payload[pos:] == b"\x1e"
    return result


def sounding_oracle(data):
    """SOUNDG OBJL129 -> FSPT NAME -> VRID SG3D Y/X/Z integer arrays."""
    dspm, vectors = integer_dspm(data), vector_records(data)
    result = {}
    for rcid, record in feature_records(data).items():
        if int.from_bytes(record["FRID"][7:9], "little") != 129:
            continue
        points = []
        pointers = record["FSPT"][:-1]
        assert len(pointers) % 8 == 0
        for p in range(0, len(pointers), 8):
            item = pointers[p:p + 8]
            key = item[0], int.from_bytes(item[1:5], "little")
            coords = vectors[key]["SG3D"][:-1]
            assert len(coords) % 12 == 0
            for y, x, z in struct.iter_unpack("<iii", coords):
                points.append((x / dspm["COMF"], y / dspm["COMF"], z / dspm["SOMF"]))
        result[rcid] = points
    return result


def hole_oracle(data, rcid=425):
    """Reconstruct one real interior ring from FSPT USAG2 and edge endpoints."""
    records_by_name, scale = vector_records(data), integer_dspm(data)["COMF"]
    pointers = feature_records(data)[rcid]["FSPT"][:-1]
    holes = []
    for p in range(0, len(pointers), 8):
        item = pointers[p:p + 8]
        if item[6] != 2:
            continue
        edge = records_by_name[(item[0], int.from_bytes(item[1:5], "little"))]
        endpoints = {}
        for v in range(0, len(edge["VRPT"]) - 1, 9):
            ptr = edge["VRPT"][v:v + 9]
            node = records_by_name[(ptr[0], int.from_bytes(ptr[1:5], "little"))]
            endpoints[ptr[7]] = tuple(struct.unpack("<ii", node["SG2D"][:-1]))
        ring_yx = [endpoints[1], *struct.iter_unpack("<ii", edge["SG2D"][:-1]), endpoints[2]]
        if item[5] == 2:
            ring_yx.reverse()
        holes.append([(x / scale, y / scale) for y, x in ring_yx])
    # This chosen fixture hole is one closed edge; no generalized topology guess.
    assert len(holes) == 1 and holes[0][0] == holes[0][-1]
    return holes[0]


def edit_field(data, tag, change):
    """Same-length binary mutation of one actual ISO8211 data field."""
    result = bytearray(data)
    for leader, fields, ranges in records(data):
        if leader[6:7] != b"L" and tag in fields:
            altered = change(bytearray(fields[tag]))
            start, length = ranges[tag]
            assert len(altered) == length
            result[start:start + length] = altered
            return bytes(result)
    raise AssertionError(f"fixture has no {tag}")


def dspm_change(data, index, value):
    def change(field):
        field[index] = value
        return field
    return edit_field(data, "DSPM", change)


def zip_bytes(entries, compression=zipfile.ZIP_DEFLATED):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=compression) as z:
        for name, data in entries:
            z.writestr(name, data)
    return out.getvalue()


def native_entries(cell=J):
    return [(f"ENC_ROOT/{cell}/{cell}.{ext}", native(cell, ext)) for ext in ("000", "001", "002")]


def layer(bundle, name, cell=None):
    matches = [r for r in bundle["layers"] if r["source"]["object_class"] == name
               and (cell is None or Path(r["source"]["cell"]).stem == cell)]
    assert len(matches) == 1
    return matches[0]


def by_rcid(row):
    return {f["properties"]["RCID"]: f for f in row["geojson"]["features"]}


@pytest.fixture(scope="module")
def base_soundings():
    return import_s57(native(), filename=J + ".000", config={"classes": ["SOUNDG"]})


@pytest.fixture(scope="module")
def updated_soundings():
    return import_s57(archive(), filename=J + ".zip", config={"classes": ["SOUNDG"]})


@pytest.fixture(scope="module")
def updated_elevation_depth_areas():
    return import_s57(archive(), filename=J + ".zip", config={"classes": ["LNDELV", "DEPARE"]})


def test_original_fixture_hash_and_extracted_binary_parity():
    manifest = json.loads((Path(__file__).parents[1] / "resources/research/s57_sources.json").read_text())
    for entry in manifest["fixtures"]:
        for file in entry["files"]:
            data = (Path(__file__).parents[1] / file["path"]).read_bytes()
            assert len(data) == file["bytes"]
            assert hashlib.sha256(data).hexdigest() == file["sha256"]
        with zipfile.ZipFile(io.BytesIO(archive(entry["cell"]))) as z:
            assert z.testzip() is None
            assert "ENC_ROOT/USERAGREEMENT.TXT" in z.namelist()
            for ext in ("000", "001", "002"):
                assert native(entry["cell"], ext) == z.read(f"ENC_ROOT/{entry['cell']}/{entry['cell']}.{ext}")


def test_single_base_does_not_implicitly_read_adjacent_disk_updates(base_soundings):
    assert (FIXTURES / J / (J + ".001")).is_file()
    cell = base_soundings["cells"][0]
    assert cell["applied_update_number"] == 0 and cell["dsid"]["DSID_UPDN"] == "0"
    assert base_soundings["summary"]["update_files_applied"] == 0
    assert base_soundings["reader"]["driver"] == "S57"
    assert base_soundings["reader"]["process_isolated"] is True


def test_actual_sounding_arrays_match_independent_scaled_binary_integers(base_soundings):
    expected = sounding_oracle(native())
    actual = by_rcid(layer(base_soundings, "SOUNDG"))
    assert set(actual) == set(expected)
    assert len(actual) == 4 and sum(map(len, expected.values())) == 657
    for rcid, points in expected.items():
        geometry = actual[rcid]["geometry"]
        assert geometry["type"] == "MultiPoint"
        assert Counter(map(tuple, geometry["coordinates"])) == Counter(points)
    assert min(p[2] for ps in expected.values() for p in ps) == .6
    assert max(p[2] for ps in expected.values() for p in ps) == 64


def test_unique_lnam_exactly_preserves_native_unsigned_feature_object_identity(base_soundings):
    source_features = feature_records(native())
    names = []
    large_unsigned = 0
    for feature in layer(base_soundings, "SOUNDG")["geojson"]["features"]:
        fields = source_features[feature["properties"]["RCID"]]
        foid = fields["FOID"]
        agency = int.from_bytes(foid[:2], "little")
        number = int.from_bytes(foid[2:6], "little")
        subdivision = int.from_bytes(foid[6:8], "little")
        expected = f"{agency:04X}{number:08X}{subdivision:04X}"
        assert feature["properties"]["LNAM"] == expected
        assert feature["properties"]["AGEN"] == agency
        assert feature["properties"]["FIDS"] == subdivision
        names.append(expected)
        large_unsigned += number > 2**31 - 1
    assert len(names) == len(set(names)) == 4
    assert large_unsigned == 2  # Native OGR FIDN scalar is signed32, LNAM isn't.


def test_datum_units_and_reference_semantics_are_explicit(base_soundings):
    binary = integer_dspm(native())
    cell = base_soundings["cells"][0]
    for field, value in binary.items():
        assert cell["dsid"]["DSPM_" + field] == value
    metadata = layer(base_soundings, "SOUNDG")["source"]["datum_units"]
    assert metadata["sounding_datum_code"] == 12  # IHO VERDAT: MLLW.
    assert metadata["vertical_datum_code"] == 16  # IHO VERDAT: MHW.
    assert metadata["depth_units"] == "m" and metadata["units_converted"] is False
    assert metadata["soundings_z_is_model_height"] is False
    assert layer(base_soundings, "SOUNDG")["kind"] == "reference"
    assert layer(base_soundings, "SOUNDG")["source"]["depth_is_engineering_water_depth"] is False


def test_real_updates_change_attribute_values_from_encoded_patch(updated_elevation_depth_areas):
    # ELEVAT is official ATTF code90, not a value obtained from tested helpers.
    base = feature_records(native())
    patch = feature_records(native(extension="001"))
    imported = by_rcid(layer(updated_elevation_depth_areas, "LNDELV"))
    for rcid in (1016, 1017):
        old = float(attributes(base[rcid]["ATTF"])[90])
        new = float(attributes(patch[rcid]["ATTF"])[90])
        assert old != new and new == imported[rcid]["properties"]["ELEVAT"]
        assert imported[rcid]["properties"]["RVER"] == 2
    assert old == 50.2 and new == 50.3
    cell = updated_elevation_depth_areas["cells"][0]
    assert cell["base_dsid"]["DSID_UPDN"] == "0"
    assert cell["dsid"]["DSID_UPDN"] == "2" and cell["applied_update_number"] == 2
    assert [u["number"] for u in cell["updates"]] == [1, 2]
    assert all(u["sha256"] == hashlib.sha256(native(extension=f"{u['number']:03}")).hexdigest()
               for u in cell["updates"])


def test_real_polygon_interior_ring_preserved_against_spatial_pointer_oracle(updated_elevation_depth_areas):
    expected_hole = hole_oracle(native())
    imported = by_rcid(layer(updated_elevation_depth_areas, "DEPARE"))[425]["geometry"]
    assert imported["type"] == "Polygon" and len(imported["coordinates"]) == 2
    assert Counter(map(tuple, imported["coordinates"][1])) == Counter(expected_hole)
    hole = Polygon(expected_hole)
    assert hole.area > 0
    q = hole.representative_point()
    assert not shape(imported).covers(q)
    assert Polygon(imported["coordinates"][0]).contains(q)
    assert shape(imported).is_valid


def test_chart_update_geometry_really_changes_the_designated_area():
    original = import_s57(native(), filename=J + ".000", config={"classes": ["DEPARE"]})
    updated = import_s57(archive(), filename=J + ".zip", config={"classes": ["DEPARE"]})
    a = by_rcid(layer(original, "DEPARE"))[423]
    b = by_rcid(layer(updated, "DEPARE"))[423]
    assert not shape(a["geometry"]).equals_exact(shape(b["geometry"]), 0)
    assert a["properties"]["RVER"] == 1 and b["properties"]["RVER"] == 2


def test_update_soundings_remain_true_three_dimensional_points(updated_soundings):
    expected = sounding_oracle(native())
    actual = by_rcid(layer(updated_soundings, "SOUNDG"))
    for rcid, points in expected.items():
        assert Counter(map(tuple, actual[rcid]["geometry"]["coordinates"])) == Counter(points)


def test_national_utf16_attribute_survives_without_ascii_replacement():
    data = native(K)
    raw = feature_records(data)[965]["NATF"]
    assert int.from_bytes(raw[:2], "little") == 301  # NOBJNM.
    expected = raw[2:-4].decode("utf-16-le")  # UTF16 UT and FT terminators.
    assert expected == "Kaxchim Chig\u0302anaa"
    result = import_s57(data, filename=K + ".000", config={"classes": ["RIVERS"]})
    value = by_rcid(layer(result, "RIVERS"))[965]["properties"]["NOBJNM"]
    assert value == expected and "\ufffd" not in value


def test_encoded_signed_sounding_is_not_flipped_clamped_or_mapped_to_elevation():
    def alter(field):
        field[8:12] = struct.pack("<i", -15)
        return field
    data = edit_field(native(), "SG3D", alter)
    expected = sounding_oracle(data)
    result = import_s57(data, filename=J + ".000", config={"classes": ["SOUNDG"]})
    actual = by_rcid(layer(result, "SOUNDG"))
    assert any(p[2] == -1.5 for ps in expected.values() for p in ps)
    for rcid, points in expected.items():
        assert Counter(map(tuple, actual[rcid]["geometry"]["coordinates"])) == Counter(points)
    assert layer(result, "SOUNDG")["source"]["depth_is_engineering_water_depth"] is False


def test_native_lists_nulls_and_finite_json_roundtrip(base_soundings):
    features = layer(base_soundings, "SOUNDG")["geojson"]["features"]
    encoded_features = feature_records(native())
    for feature in features:
        pointer = encoded_features[feature["properties"]["RCID"]]["FSPT"]
        assert feature["properties"]["NAME_RCNM"] == [pointer[0]]
        assert feature["properties"]["NAME_RCID"] == [int.from_bytes(pointer[1:5], "little")]
    assert any(v is None for f in features for v in f["properties"].values())
    encoded = json.dumps(base_soundings, allow_nan=False, ensure_ascii=False)
    assert json.loads(encoded) == base_soundings


def test_real_list_valued_quality_attribute_keeps_catalog_values():
    result = import_s57(native(), filename=J + ".000", config={"classes": ["OBSTRN"]})
    encoded = feature_records(native())
    checked = 0
    for rcid, feature in by_rcid(layer(result, "OBSTRN")).items():
        values = attributes(encoded[rcid]["ATTF"])
        if 125 in values:  # IHO QUASOU, type L.
            assert feature["properties"]["QUASOU"] == values[125].split(",")
            checked += 1
    assert checked > 0


def test_full_real_chart_keeps_all_feature_records_and_reports_slow_polygon_warning():
    result = import_s57(archive(), filename=J + ".zip")
    assert result["summary"]["imported_features"] == len(feature_records(native())) == 1087
    assert result["summary"]["imported_layers"] == 26
    assert result["summary"]["imported_vertices"] == 80572
    assert all(r["kind"] == "reference" for r in result["layers"])
    assert any("organizePolygons" in str(w) for w in result["warnings"])
    assert len(by_rcid(layer(result, "DEPARE"))[425]["geometry"]["coordinates"]) == 2
    json.dumps(result, allow_nan=False)


def test_catalog_reports_classes_without_materializing_geometry():
    result = catalog_s57(archive(), filename=J + ".zip", config={"classes": ["SOUNDG"]})
    assert result["stage"] == "catalog" and result["can_apply"] is False
    assert result["layers"] == [] and result["summary"]["native_read_performed"] is True
    soundings = next(c for c in result["classes_catalog"] if c["name"] == "SOUNDG")
    assert soundings["feature_count"] == 4
    row = next(c for c in result["cells"][0]["object_classes"] if c["name"] == "SOUNDG")
    assert any(f["name"] == "QUASOU" and "List" in f["ogr_type"] for f in row["fields"])


def test_multiple_real_cells_require_exact_selection_and_keep_provenance():
    data = zip_bytes(native_entries(J) + native_entries(K))
    inspected = inspect_s57(data, filename="both.zip")
    assert inspected["summary"]["available_cells"] == 2
    assert inspected["summary"]["native_read_performed"] is False
    with pytest.raises(ValueError, match="multiple cells"):
        import_s57(data, filename="both.zip", config={"classes": ["SOUNDG"]})
    paths = [f"ENC_ROOT/{cell}/{cell}.000" for cell in (J, K)]
    result = import_s57(data, filename="both.zip", config={"cells": paths, "classes": ["SOUNDG"]})
    assert result["summary"]["selected_cells"] == 2
    assert result["summary"]["imported_features"] == 6
    assert result["summary"]["imported_vertices"] == 2003
    assert len({r["id"] for r in result["layers"]}) == 2
    for cell in (J, K):
        actual = by_rcid(layer(result, "SOUNDG", cell))
        for rcid, points in sounding_oracle(native(cell)).items():
            assert Counter(map(tuple, actual[rcid]["geometry"]["coordinates"])) == Counter(points)
    with pytest.raises(ValueError, match="does not exist"):
        import_s57(data, filename="both.zip", config={"cells": [J + ".000"]})


@pytest.mark.parametrize("index,value", [(5, 1), (15, 2)])
def test_non_wgs84_or_nongeographic_coordinate_declarations_are_rejected(index, value):
    data = dspm_change(native(), index, value)
    with pytest.raises(ValueError, match="HDAT|COUN|WGS84"):
        import_s57(data, filename=J + ".000", config={"classes": ["SOUNDG"]})


def test_non_metre_sounding_unit_is_not_silently_labelled_or_converted():
    data = dspm_change(native(), 12, 2)
    result = import_s57(data, filename=J + ".000", config={"classes": ["SOUNDG"]})
    source = layer(result, "SOUNDG")["source"]
    assert source["datum_units"]["depth_unit_code"] == 2
    assert source["datum_units"]["depth_units"] is None
    assert source["datum_units"]["units_converted"] is False
    assert source["depth_is_engineering_water_depth"] is False
    assert any(w["code"] == "UNKNOWN_SOUNDING_UNITS" for w in result["warnings"])
    for rcid, points in sounding_oracle(data).items():
        assert Counter(map(tuple, by_rcid(layer(result, "SOUNDG"))[rcid]["geometry"]["coordinates"])) == Counter(points)


def test_native_soundings_use_actual_declared_integer_scale():
    def change(field):
        field[20:24] = struct.pack("<I", 100)
        return field
    data = edit_field(native(), "DSPM", change)
    result = import_s57(data, filename=J + ".000", config={"classes": ["SOUNDG"]})
    assert result["cells"][0]["dsid"]["DSPM_SOMF"] == 100
    for rcid, points in sounding_oracle(data).items():
        assert Counter(map(tuple, by_rcid(layer(result, "SOUNDG"))[rcid]["geometry"]["coordinates"])) == Counter(points)


@pytest.mark.parametrize("budget,value,match", [
    ("max_features", 3, "max_features"),
    ("max_vertices", 656, "max_vertices"),
    ("max_work_units", 1, "before native reader"),
    ("max_output_bytes", 512, "max_output_bytes"),
    ("max_layers", 1, "max_layers"),
])
def test_real_native_budget_failures_return_no_partial_bundle(budget, value, match):
    with pytest.raises(ValueError, match=match):
        import_s57(native(), filename=J + ".000", config={"classes": ["SOUNDG"], budget: value})


@pytest.mark.parametrize("name", ["../outside.000", "/absolute.000", "a/../chart.000", "a\\chart.000", "C:chart.000", "a//chart.000", "./chart.000"])
def test_unsafe_zip_member_paths_fail_before_native_reader(name):
    data = zip_bytes([(name, native())])
    with pytest.raises(ValueError, match="unsafe|noncanonical"):
        inspect_s57(data, filename="bad.zip")


def test_duplicate_and_case_conflicting_zip_members_are_rejected():
    for names in [(J + ".000", J + ".000"), (J + ".000", J.lower() + ".000")]:
        with pytest.warns(UserWarning) if names[0] == names[1] else _no_warning_context():
            data = zip_bytes([(name, native()) for name in names])
        with pytest.raises(ValueError, match="duplicate|case-conflicting"):
            inspect_s57(data, filename="duplicate.zip")


def _no_warning_context():
    from contextlib import nullcontext
    return nullcontext()


def test_zip_symlink_rejected_without_extracting_to_user_files():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(J + ".000", native())
        link = zipfile.ZipInfo("other.txt")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(link, "/etc/passwd")
    with pytest.raises(ValueError, match="symlinks"):
        inspect_s57(out.getvalue(), filename="symlink.zip")


def test_zip_declared_size_budget_is_checked_before_decompression():
    data = bytearray(zip_bytes([(J + ".000", native())]))
    p = data.index(b"PK\x01\x02")
    # Central directory uncompressed size offset24; no giant allocation needed.
    data[p + 24:p + 28] = struct.pack("<I", 128 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="128 MiB"):
        inspect_s57(bytes(data), filename="size.zip")


def test_zip_crc_failure_and_encryption_declaration_rejected():
    data = bytearray(zip_bytes([(J + ".000", native())], zipfile.ZIP_STORED))
    local = data.index(b"PK\x03\x04")
    nlen, elen = struct.unpack_from("<HH", data, local + 26)
    data[local + 30 + nlen + elen + 50] ^= 1
    with pytest.raises(ValueError, match="CRC|decoded"):
        inspect_s57(bytes(data), filename="crc.zip")
    data = bytearray(zip_bytes([(J + ".000", native())]))
    p = data.index(b"PK\x01\x02")
    data[p + 8:p + 10] = struct.pack("<H", 1)
    with pytest.raises(ValueError, match="encrypted"):
        inspect_s57(bytes(data), filename="encrypted.zip")


@pytest.mark.parametrize("entries,match", [
    ([(J + ".001", native(extension="001"))], "base"),
    ([(J + ".000", native()), (J + ".001", native(extension="001")), (J + ".003", native(extension="002"))], "missing number"),
    ([(J + ".000", native()), (J + ".002", native(extension="002"))], "start immediately"),
    ([(J + ".000", native()), ("different.001", native(extension="001"))], "no same-directory"),
])
def test_missing_and_mismatched_update_sets_rejected(entries, match):
    with pytest.raises(ValueError, match=match):
        import_s57(zip_bytes(entries), filename="sequence.zip", config={"classes": ["SOUNDG"]})


@pytest.mark.parametrize("mutation", ["different-cell", "wrong-edition", "cancellation", "wrong-update-number", "base-profile"])
def test_each_native_update_header_is_bound_not_just_final_applied_number(mutation):
    def change(field):
        if mutation == "different-cell":
            return field.replace(b"US5A1KMJ", b"US5A1KMK", 1)
        if mutation in {"wrong-edition", "cancellation", "wrong-update-number"}:
            replacement = {"wrong-edition": b"\x1f9\x1f1\x1f", "cancellation": b"\x1f0\x1f1\x1f",
                           "wrong-update-number": b"\x1f1\x1f9\x1f"}[mutation]
            assert b"\x1f1\x1f1\x1f" in field
            return field.replace(b"\x1f1\x1f1\x1f", replacement, 1)
        field[5] = 1  # EXPP EN base instead of ER update.
        return field
    altered = edit_field(native(extension="001"), "DSID", change)
    data = zip_bytes([(J + ".000", native()), (J + ".001", altered),
                      (J + ".002", native(extension="002"))])
    with pytest.raises(ValueError, match="DSID|edition|native"):
        import_s57(data, filename="forged-header.zip", config={"classes": ["SOUNDG"]})


def test_legal_reissue_followup_is_explicit_driver_limitation_not_old_chart_success():
    # Synthetic declared reissue for admission only, not an official NOAA
    # reissue/history assertion. Original real 0->1->2 changes are tested above.
    def change(field):
        assert b"\x1f1\x1f0\x1f" in field
        return field.replace(b"\x1f1\x1f0\x1f", b"\x1f1\x1f1\x1f", 1)
    reissued = edit_field(native(), "DSID", change)
    data = zip_bytes([(J + ".000", reissued), (J + ".002", native(extension="002"))])
    # IHO Appendix B1 table5.1 permits this native base-number/next-number
    # relation. GDAL3.12.4 starts lookup at.001 and cannot apply this.002. The
    # implementation must call it a driver limitation and not return old data.
    with pytest.raises(ValueError, match="(?i)re.?issue.*GDAL|GDAL.*re.?issue"):
        import_s57(data, filename="declared-reissue.zip", config={"classes": ["SOUNDG"]})
    # A declared reissue without a later supplied update has no missing update.
    result = import_s57(reissued, filename=J + ".000", config={"classes": ["SOUNDG"]})
    assert result["cells"][0]["base_dsid"]["DSID_UPDN"] == "1"
    assert result["cells"][0]["applied_update_number"] == 1
    assert result["summary"]["update_files_applied"] == 0


@pytest.mark.parametrize("mutation", ["update-profile", "cancelled-edition", "update-identity"])
def test_update_disguised_as_native_base_or_cancelled_base_is_rejected(mutation):
    def change(field):
        if mutation == "update-profile":
            field[5] = 2
        elif mutation == "cancelled-edition":
            field = field.replace(b"\x1f1\x1f0\x1f", b"\x1f0\x1f0\x1f", 1)
        else:
            field = field.replace(b"US5A1KMJ.000", b"US5A1KMJ.001", 1)
        return field
    with pytest.raises(ValueError, match="base|edition|identity"):
        import_s57(edit_field(native(), "DSID", change), filename=J + ".000", config={"classes": ["SOUNDG"]})


def test_ambient_gdal_config_cannot_change_actual_soundings_or_native_source(monkeypatch):
    monkeypatch.setenv("OGR_S57_OPTIONS", "UPDATES=IGNORE,SPLIT_MULTIPOINT=ON,LIST_AS_STRING=ON")
    monkeypatch.setenv("S57_CSV", "/does/not/exist")
    result = import_s57(archive(), filename=J + ".zip", config={"classes": ["SOUNDG"]})
    assert result["cells"][0]["applied_update_number"] == 2
    assert result["summary"]["imported_features"] == 4
    assert all(f["geometry"]["type"] == "MultiPoint" for f in layer(result, "SOUNDG")["geojson"]["features"])
    assert result["reader"]["options"]["LIST_AS_STRING"] == "OFF"


@pytest.mark.parametrize("data,filename", [(b'{"type":"FeatureCollection"}', "fake.000"), (native()[:100], "truncated.000"), (native(extension="001"), J + ".001")])
def test_json_disguised_as_native_truncation_and_update_only_are_not_chart_success(data, filename):
    with pytest.raises(ValueError):
        import_s57(data, filename=filename, config={"classes": ["SOUNDG"]})


@pytest.mark.parametrize("config", [{"classes": ["NOT_A_REAL_CLASS"]}, {"classes": ["soundg"]}, {"classes": []}, {"classes": ["SOUNDG", "SOUNDG"]}, {"max_features": True}, {"max_vertices": 1000001}, {"UPDATES": "IGNORE"}])
def test_unknown_classes_and_unsupported_config_do_not_silently_change_native_contract(config):
    with pytest.raises(ValueError):
        import_s57(native(), filename=J + ".000", config=config)


def test_actual_http_binary_import_is_preview_only_and_atomic_workspace_persistence(tmp_path):
    from fastapi.testclient import TestClient
    from oceanroute.api import create_app
    from oceanroute.storage import ProjectStore
    with TestClient(create_app(ProjectStore(tmp_path / "s57.sqlite3"))) as client:
        sample = client.get("/api/sample").json()
        migrated = client.post("/api/workspace/migrate", json={"project": sample})
        assert migrated.status_code == 200, migrated.text
        saved = client.post("/api/workspaces", json=migrated.json()["workspace"]).json()["workspace"]
        before = deepcopy(saved)
        response = client.post("/api/import/s57", files={"file": (J + ".zip", archive(), "application/zip")},
                               data={"config_json": json.dumps({"classes": ["SOUNDG", "LNDELV"]})})
        assert response.status_code == 200, response.text
        bundle = response.json()
        assert bundle["accepted"] is True and bundle["can_apply"] is True
        assert len(bundle["layers"]) == 2
        assert client.get(f"/api/workspaces/{saved['id']}").json() == before
        candidate = deepcopy(saved)
        candidate["layers"].extend(bundle["layers"])
        stored = client.post("/api/workspaces", json=candidate)
        assert stored.status_code == 200, stored.text
        current = stored.json()["workspace"]
        reopened = client.get(f"/api/workspaces/{saved['id']}").json()
        assert reopened == current
        for source in bundle["layers"]:
            assert next(r for r in reopened["layers"] if r["id"] == source["id"]) == source
        rejected = client.post("/api/import/s57", files={"file": ("bad.000", b"bad", "application/octet-stream")})
        assert rejected.status_code == 422
        assert client.get(f"/api/workspaces/{saved['id']}").json() == current
        restored = client.post(f"/api/workspaces/{saved['id']}/restore/1", json={"expected_revision": 2})
        assert restored.status_code == 200, restored.text
        assert restored.json()["layers"] == before["layers"]


@pytest.mark.parametrize("config_json", ['[]', '{"max_features":1,"max_features":2}', '{"max_vertices":NaN}', '{"max_vertices":Infinity}', '{"classes":["SOUNDG"],"bad":1}'])
def test_actual_http_config_json_is_strict(tmp_path, config_json):
    from fastapi.testclient import TestClient
    from oceanroute.api import create_app
    from oceanroute.storage import ProjectStore
    with TestClient(create_app(ProjectStore(tmp_path / "strict.sqlite3"))) as client:
        response = client.post("/api/import/s57", files={"file": (J + ".000", native(), "application/octet-stream")},
                               data={"config_json": config_json})
        assert response.status_code == 422, response.text
