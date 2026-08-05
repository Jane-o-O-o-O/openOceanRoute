import json
import struct
import numpy as np
import pytest

from oceanroute.surfer import BLANK, read_grid, profile_from_surfer, _sample
from oceanroute.core import sample_project, analyze_project


def grid_file(fmt, version=1):
    values = [100, 200, 300, BLANK]
    if fmt == "DSAA":
        return ("DSAA\n2 2\n118 119\n22 23\n100 300\n" + " ".join(map(str, values))).encode()
    if fmt == "DSBB":
        return b"DSBB" + struct.pack("<hh6d4f", 2, 2, 118, 119, 22, 23, 100, 300, *values)
    return b"DSRB" + struct.pack("<ii", 4, version) + b"GRID" + struct.pack("<iii8d", 72, 2, 2, 118, 22, 1, 1, 100, 300, 0, BLANK) + b"DATA" + struct.pack("<i4d", 32, *values)


@pytest.mark.parametrize("fmt", ["DSAA", "DSBB", "DSRB"])
def test_three_surfer_formats_keep_south_to_north_orientation_and_nodata(fmt):
    grid = read_grid(grid_file(fmt))
    assert grid["values"][0, 0] == 100
    assert grid["values"][1, 0] == 300
    assert np.isnan(grid["values"][1, 1])
    assert _sample(grid, 118, 22, "linear") == 100
    assert _sample(grid, 118.5, 22, "linear") == 150
    assert _sample(grid, 118.5, 22.5, "linear") is None
    assert _sample(grid, 117.9, 22, "nearest") is None


def test_dsr_b_version2_exact_blank():
    data = grid_file("DSRB", 2)
    data = data[:-8] + struct.pack("<d", BLANK * 2)
    assert read_grid(data)["values"][1, 1] == BLANK * 2
    assert np.isnan(read_grid(grid_file("DSRB", 2))["values"][1, 1])


def test_surfer_profile_signature_depth_direction_and_finite_json():
    project = sample_project()
    project["route"]["points"] = [project["route"]["points"][0], project["route"]["points"][1]]
    for point, lon in zip(project["route"]["points"], (118.1, 118.9)):
        point.update(longitude=lon, latitude=22)
    project["route"]["legs"] = project["route"]["legs"][:1]
    project["bodies"], project["events"] = [], []
    result = profile_from_surfer(project, grid_file("DSAA"), source_crs="EPSG:4326", spacing_m=10000)
    assert result["project"]["profile"]["samples"][0]["depth_m"] == pytest.approx(110)
    assert result["project"]["profile"]["samples"][-1]["depth_m"] == pytest.approx(190)
    assert analyze_project(result["project"])["profile_metadata"]["imported_profile_valid"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("data", [b"BAD!", b"DSBB", b"DSAA\n2 2 1 2 1 2 1 2 3", b"DSRB"+struct.pack("<ii", 4, 99)])
def test_corrupt_or_unknown_grid_rejected(data):
    with pytest.raises(ValueError):
        read_grid(data)
