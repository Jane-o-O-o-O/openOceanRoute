import io
import math

import numpy as np
import pytest
from pyproj import CRS, Transformer

from oceanroute.gis import profile_from_xyz, profile_from_geotiff, sample_route
from oceanroute.core import analyze_project


def project():
    return {"schema_version": 1, "id": "terrain-test", "name": "地形验证", "crs": "EPSG:4326",
            "route": {"curve": "rhumb", "mode": "flexible", "slack_basis": "surface", "slack_pct": 1,
                      "points": [{"id":"a", "longitude":118, "latitude":22, "depth_m":None},
                                 {"id":"b", "longitude":118.02, "latitude":22, "depth_m":None}]},
            "cable_types": [{"id":"LW", "name":"LW", "cost_per_m":10, "lay_speed_m_s":1}],
            "costs": {"currency":"CNY", "vessel_day_rate":1000}}


def test_xyz_known_plane_and_no_extrapolation():
    p = project()
    transform = Transformer.from_crs("EPSG:4326", CRS.from_proj4("+proj=aeqd +lat_0=22 +lon_0=118 +datum=WGS84 +units=m"), always_xy=True)
    rows = ["longitude latitude depth_m"]
    for lon in [117.99,118.01,118.03]:
        for lat in [21.99,22.01]:
            x,y = transform.transform(lon,lat)
            rows.append(f"{lon} {lat} {100 + .01*x + .02*y}")
    result = profile_from_xyz(p, "\n".join(rows), spacing_m=200,max_gap_m=3000)
    samples = sample_route(p,200)
    for s, depth in zip(samples, result["project"]["profile"]["samples"]):
        x,y=transform.transform(s["longitude"],s["latitude"])
        assert depth["depth_m"] == pytest.approx(100+.01*x+.02*y, abs=1e-7)
    analyzed = analyze_project(result["project"])
    assert analyzed["summary"]["bottom_length_m"] > analyzed["summary"]["surface_length_m"]
    far = project()
    far["route"]["points"][1]["longitude"] = 119
    missing = profile_from_xyz(far,"\n".join(rows),spacing_m=10000,max_gap_m=3000)
    assert missing["quality"]["missing_count"] > 0
    assert missing["project"]["profile"]["samples"][-1]["depth_m"] is None


def test_raster_crs_and_nodata_are_preserved():
    rasterio = pytest.importorskip("rasterio")
    from rasterio.io import MemoryFile
    from rasterio.transform import from_origin
    grid=np.array([[100,100,-9999,100],[100,100,-9999,100]],dtype="float32")
    with MemoryFile() as mem:
        with mem.open(driver="GTiff",height=2,width=4,count=1,dtype="float32",crs="EPSG:4326",transform=from_origin(117.995,22.01,.01,.01),nodata=-9999) as ds:
            ds.write(grid,1)
        data=mem.read()
    p=project()
    p["route"]["points"][1]["longitude"]=118.03
    result=profile_from_geotiff(p,data,spacing_m=250,vertical_datum="test-datum")
    assert result["quality"]["missing_count"] > 0
    assert result["quality"]["source"]["vertical_datum"] == "test-datum"
    assert all(s["depth_m"] in (100,None) for s in result["project"]["profile"]["samples"])


def test_repeated_point_does_not_create_duplicate_profile_kp():
    p=project()
    p["route"]["points"].insert(1,{**p["route"]["points"][0],"id":"same"})
    samples=sample_route(p,100)
    assert all(b["kp_m"]>a["kp_m"] for a,b in zip(samples,samples[1:]))
