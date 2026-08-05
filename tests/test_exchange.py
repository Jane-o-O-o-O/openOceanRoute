import json
from xml.etree import ElementTree

import pytest

from oceanroute.exchange import coordinate, import_rpl, import_profile, import_geojson, export_csv, export_kml, export_report
from oceanroute.storage import ProjectStore


def test_coordinates_reject_conflicting_hemisphere_and_accept_dms():
    assert coordinate("N 22° 30' 0\"", latitude=True) == 22.5
    assert coordinate("118 15 E", latitude=False) == 118.25
    assert coordinate("-22.5 S", latitude=True) == -22.5
    with pytest.raises(ValueError):
        coordinate("-22 N", latitude=True)
    with pytest.raises(ValueError):
        coordinate("22 65 N", latitude=True)


def test_rpl_quoted_chinese_and_bad_row_diagnostics():
    result = import_rpl('标签,经度,纬度,水深,备注\n"点,一",118.5,22.5,100,"中文,备注"\n错误,181,22,30,\n点二,119,23,,')
    assert len(result["points"]) == 2
    assert result["points"][0]["label"] == "点,一"
    assert result["points"][0]["note"] == "中文,备注"
    assert result["points"][1]["depth_m"] is None
    assert result["warnings"][0]["row"] == 3


def test_profile_preserves_missing_depth_and_rejects_duplicate_kp():
    result = import_profile("kp_m\tdepth_m\n0\t30\n100\t\n100\t20\n200\t40")
    assert result["samples"][1]["depth_m"] is None
    assert result["rejected_rows"] == 1


def test_geojson_requires_geographic_coordinates():
    with pytest.raises(ValueError, match="范围"):
        import_geojson(json.dumps({"type": "Point", "coordinates": [380000, 2500000]}))


def test_storage_revision_restore_and_stale_save(tmp_path):
    store = ProjectStore(tmp_path / "test.db")
    first = store.save({"id": "a", "name": "第一版", "route": {}})
    old = first["project"]
    second = store.save({**old, "name": "第二版"})
    assert second["revision"] == 2
    with pytest.raises(ValueError, match="另一窗口"):
        store.save(old)
    restored = store.restore("a", 1)
    assert restored["name"] == "第一版"
    assert restored["saved_revision"] == 3
    assert len(store.revisions("a")) == 3


def test_export_encoding_kml_depth_metadata_and_report_escaping():
    project = {"name": "<script>bad</script>", "route": {"name": "海缆", "points": [
        {"longitude": 118, "latitude": 22}, {"longitude": 119, "latitude": 23}
    ]}, "cable_types": []}
    rows = [dict(index=i, id=str(i), label="=1+1" if i == 0 else "终点", longitude=118+i, latitude=22+i,
                 depth_m=100, kp_m=i*1000, cable_kp_m=i*1010, note="中文") for i in range(2)]
    analysis = {"rpl": rows, "summary": {"cable_length_m": 1010}, "sld": [], "warnings": []}
    csv_text = export_csv(project, analysis)
    assert "'=1+1" in csv_text
    assert import_rpl(csv_text)["points"][1]["note"] == "中文"
    kml = ElementTree.fromstring(export_kml(project, analysis))
    assert kml.findall(".//{*}Data[@name='depth_m']")
    assert "<script>bad</script>" not in export_report(project, analysis)
