"""Filename admission and actual HTTP header encoding, independent of export data."""
from email.message import Message
from urllib.parse import unquote
import unicodedata

import pytest
from starlette.responses import Response

from oceanroute import __version__
from oceanroute.core import analyze_project, sample_project
from oceanroute.exchange import export_content_disposition, export_filename, export_report


def utf8_filename(header):
    marker = "filename*=UTF-8''"
    assert marker in header
    return unquote(header.split(marker, 1)[1])


@pytest.mark.parametrize("format_name", ["workspace", "project", "csv", "assembly", "report"])
def test_actual_response_header_roundtrip_is_safe_and_readable(format_name):
    document = {"id": '真实ID/"\r\n', "name": '海缆工程/东线\\试验\r\n"下载"\x00'}
    header = export_content_disposition(document, format_name)
    response = Response(b"actual payload", headers={"Content-Disposition": header})
    raw = next(value for key, value in response.raw_headers if key == b"content-disposition")
    assert raw.decode("ascii") == header
    filename = utf8_filename(header)
    assert filename == export_filename(document, format_name)
    assert "海缆工程" in filename and "东线" in filename
    assert not any(char in filename for char in '/\\\r\n\x00"<>:|?*')
    assert filename.startswith(f"OceanRoute-{__version__.removesuffix('.0')}-")
    assert len(filename.encode("utf-8")) < 255
    message = Message()
    message["Content-Disposition"] = header
    assert message.get_content_disposition() == "attachment"
    assert response.body == b"actual payload"


def test_distinct_real_paths_and_workspace_are_not_confused_by_same_name():
    first = {"id": "route-east", "name": "重复路径"}
    second = {"id": "route-west", "name": "重复路径"}
    assert export_filename(first, "csv") != export_filename(second, "csv")
    assert "-workspace-" in export_filename(first, "workspace")
    assert "-workspace-" in export_filename(first, "automatic_rules")
    assert "-path-" in export_filename(first, "project")


def test_long_unicode_name_has_complete_characters_and_fixed_extension():
    filename = export_filename({"id": "long", "name": "海缆🌊" * 1000}, "workspace")
    assert "海缆" in filename
    assert filename.encode().decode() == filename
    assert len(filename.encode()) < 255
    assert filename.endswith(".oceanroute.json")


def test_nfc_names_keep_deterministic_identity_and_no_hidden_file_prefix():
    a = {"id": "same", "name": "Cafe\u0301"}
    b = {"id": "same", "name": "Café"}
    assert export_filename(a, "csv") == export_filename(b, "csv")
    assert unicodedata.is_normalized("NFC", export_filename(a, "csv"))
    blank = export_filename({"id": "blank", "name": '.\x00/\\\r\n  '}, "csv")
    assert "未命名" in blank
    assert not blank.startswith(".")


@pytest.mark.parametrize("kind", ["../bad", "csv\r\nInjected", [], None])
def test_unregistered_export_formats_never_enter_a_header(kind):
    with pytest.raises(ValueError):
        export_content_disposition({"name": "工程"}, kind)


def test_html_report_uses_the_actual_product_version_and_escapes_name():
    project = sample_project()
    project["name"] = '中文路线 <script>bad()</script> "引用"'
    result = export_report(project, analyze_project(project))
    assert f"OceanRoute {__version__.removesuffix('.0')} · WGS84" in result
    assert "OceanRoute 0.1 · WGS84" not in result
    assert "<script>bad()</script>" not in result
    assert "中文路线" in result
