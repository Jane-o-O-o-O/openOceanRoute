import base64
import json
import math

import numpy as np
import pytest
from pyproj import CRS, Transformer

from oceanroute.dtm import build_dtm


def plane_text():
    inverse=Transformer.from_crs(CRS.from_proj4("+proj=aeqd +lat_0=22 +lon_0=118 +datum=WGS84 +units=m"),"EPSG:4326",always_xy=True)
    rows=["longitude latitude depth_m"]
    for x in range(-3000,3001,1000):
        for y in range(-3000,3001,1000):
            lon,lat=inverse.transform(x,y)
            rows.append(f"{lon} {lat} {100+.01*x+.02*y}")
    return "\n".join(rows)


def test_dtm_known_slope_contours_raster_nodata_and_units():
    pytest.importorskip("rasterio")
    pytest.importorskip("contourpy")
    from rasterio.io import MemoryFile
    result=build_dtm(plane_text(),{"grid_spacing_m":500,"max_gap_m":2000,"contour_interval_m":20,"vertical_datum":"known-test"})
    slopes=np.asarray([[np.nan if v is None else v for v in row] for row in result["preview"]["slope_deg"]])
    assert np.nanmedian(slopes)==pytest.approx(math.degrees(math.atan(math.hypot(.01,.02))),abs=.001)
    assert result["metadata"]["contour_count"]>0
    assert result["metadata"]["missing_cells"]>0
    with MemoryFile(base64.b64decode(result["geotiff_base64"])) as memory:
        with memory.open() as raster:
            assert raster.count==4
            assert raster.tags()["vertical_datum"]=="known-test"
            assert raster.descriptions[0]=="depth_m_positive_down"
            assert raster.nodata==-9999
            assert raster.crs is not None
    json.dumps(result,allow_nan=False)


def test_dtm_rejects_unbounded_grid():
    with pytest.raises(ValueError,match="250,000"):
        build_dtm(plane_text(),{"grid_spacing_m":1})
