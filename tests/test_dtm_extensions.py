"""Independent analytic surface, clipping and full-raster profile acceptance."""

import base64
import csv
import io
import json
import math

import numpy as np
import pytest
from pyproj import CRS, Transformer
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from scipy.interpolate import LinearNDInterpolator
from shapely.geometry import shape, Point, LineString

from oceanroute.dtm import build_dtm, extract_dtm_slice, _minimum_curvature
from oceanroute.terrain_boundaries import read_bln, write_bln


LOCAL = CRS.from_proj4("+proj=aeqd +lat_0=22 +lon_0=118 +datum=WGS84 +units=m +no_defs")
INVERSE = Transformer.from_crs(LOCAL, "EPSG:4326", always_xy=True)


def source_text(function=None, step=400, size=1200):
    function = function or (lambda x, y: 100 + .01*x + .02*y)
    text = ["longitude latitude depth_m"]
    for x in range(-size, size+1, step):
        for y in range(-size, size+1, step):
            lon, lat = INVERSE.transform(x, y)
            text.append(f"{lon:.15g} {lat:.15g} {function(x,y):.15g}")
    return "\n".join(text)


def mc_config(**kwargs):
    return {"grid_spacing_m":100,"max_gap_m":700,"contour_interval_m":10,"method":"minimum_curvature",**kwargs}


def assert_finite_json(value):
    json.dumps(value, allow_nan=False)


@pytest.mark.parametrize("tension", [0,.35,1])
def test_affine_plane_reproduction_includes_tension_and_free_edges(tension):
    result = build_dtm(source_text(), mc_config(minimum_curvature={"tension":tension}))
    local_inverse = Transformer.from_crs(result["metadata"]["crs"], LOCAL, always_xy=True)
    xx, yy = np.meshgrid(result["preview"]["x_m"], result["preview"]["y_m"])
    x, y = local_inverse.transform(xx, yy)
    depth = np.array([[np.nan if value is None else value for value in row] for row in result["preview"]["depth_m"]])
    assert np.nanmax(abs(depth-(100+.01*x+.02*y))) < .0001
    assert result["metadata"]["solver"]["converged"]
    assert result["metadata"]["solver"]["data_max_abs_error_m"] < .0001
    assert_finite_json(result)


def test_held_out_curved_surface_improves_over_linear_without_sampling_truth_as_constraints():
    # 36 sparse observed points, 441 independent grid nodes. Tests only nodes
    # absent from the observation set and away from free outer edges.
    x = y = np.arange(-10, 11, dtype=float)
    xx, yy = np.meshgrid(x,y)
    xy = np.array([(a,b) for a in range(-10,11,4) for b in range(-10,11,4)], dtype=float)
    truth = lambda a,b: 100+.12*a+.2*b+.4*(a*a+b*b)
    depth, solver = _minimum_curvature(x,y,xy,truth(xy[:,0],xy[:,1]),np.ones(xx.shape,dtype=bool),{})
    linear = LinearNDInterpolator(xy,truth(xy[:,0],xy[:,1]))(xx,yy)
    held_out = (np.abs(xx)<7)&(np.abs(yy)<7)&np.isfinite(linear)
    for a,b in xy:
        held_out &= ~((xx==a)&(yy==b))
    rmse = np.sqrt(np.mean((depth[held_out]-truth(xx,yy)[held_out])**2))
    linear_rmse = np.sqrt(np.mean((linear[held_out]-truth(xx,yy)[held_out])**2))
    assert solver["converged"]
    assert rmse < .2
    assert rmse < .15*linear_rmse
    assert solver["data_rmse_m"] > .001  # genuinely soft constraints
    assert solver["bending_energy_m2"] > 0


def test_tension_changes_real_curved_solution_and_reduces_residual_membrane_energy():
    x = y = np.arange(-10,11,dtype=float)
    xx, yy = np.meshgrid(x,y)
    xy = np.array([(a,b) for a in range(-10,11,5) for b in range(-10,11,5)],dtype=float)
    z = 100+15*np.exp(-(xy[:,0]**2+xy[:,1]**2)/20)
    low, a = _minimum_curvature(x,y,xy,z,np.ones(xx.shape,dtype=bool),{"tension":0})
    high, b = _minimum_curvature(x,y,xy,z,np.ones(xx.shape,dtype=bool),{"tension":1})
    assert a["converged"] and b["converged"]
    assert np.max(abs(low-high)) > .1
    # Actual membrane minimization changes the shape between observations.
    membrane = lambda d: np.sum(np.diff(d,axis=0)**2)+np.sum(np.diff(d,axis=1)**2)
    assert membrane(high) < membrane(low)


def test_single_node_wide_l_mask_rejects_unidentified_thin_plate_but_membrane_is_unique():
    x=y=np.arange(9,dtype=float)
    active=np.zeros((9,9),dtype=bool)
    active[0,:]=True
    active[:,0]=True
    xy=np.array([[0,0],[0,4],[0,8],[4,0],[8,0]],dtype=float)
    z=100+.1*(xy[:,0]**2+xy[:,1]**2)
    with pytest.raises(ValueError,match="tension > 0"):
        _minimum_curvature(x,y,xy,z,active.copy(),{"tension":0})
    depth,model=_minimum_curvature(x,y,xy,z,active.copy(),{"tension":.2})
    assert model["converged"] and model["unique_solution_verified"]
    assert np.isfinite(depth[active]).all()
    assert np.isnan(depth[~active]).all()
    assert model["matrix_nonzeros"]>len(xy)


def test_orphan_edge_node_becomes_nodata_in_thin_plate_and_is_solved_with_membrane():
    x=y=np.arange(7,dtype=float)
    active=np.zeros((7,7),dtype=bool)
    active[2:5,2:5]=True
    active[3,5]=True
    xy=np.array([[2,2],[2,4],[4,2],[4,4]],dtype=float)
    z=100+.1*xy[:,0]+.2*xy[:,1]
    depth,model=_minimum_curvature(x,y,xy,z,active.copy(),{})
    assert np.isnan(depth[3,5])
    assert model["stencil_unidentified_missing_nodes"]==1
    assert model["unique_solution_verified"]
    membrane,model=_minimum_curvature(x,y,xy,z,active.copy(),{"tension":.1})
    assert membrane[3,5]==pytest.approx(101.1)
    assert model["unique_solution_verified"]


def test_cells_connected_only_at_corner_have_extra_affine_nullspace_and_are_rejected():
    x=y=np.arange(4,dtype=float)
    active=np.zeros((4,4),dtype=bool)
    active[:2,:2]=True
    active[1:3,1:3]=True
    xy=np.array([[0,0],[0,1],[1,0],[2,2],[1,2]],dtype=float)
    with pytest.raises(ValueError,match="仅角连接"):
        _minimum_curvature(x,y,xy,np.ones(5)*100,active.copy(),{})
    depth,model=_minimum_curvature(x,y,xy,np.ones(5)*100,active.copy(),{"tension":1})
    assert model["unique_solution_verified"]
    assert np.isfinite(depth[active]).all()


def test_wide_nonrectangular_l_has_provable_affine_kernel_and_plane_reproduction():
    x=y=np.arange(9,dtype=float)
    active=np.zeros((9,9),dtype=bool)
    active[:3,:]=True
    active[:,:3]=True
    xy=np.array([[0,0],[0,8],[8,0],[2,7],[7,2]],dtype=float)
    z=100+.1*xy[:,0]+.2*xy[:,1]
    depth,model=_minimum_curvature(x,y,xy,z,active.copy(),{})
    xx,yy=np.meshgrid(x,y)
    assert np.max(abs(depth[active]-(100+.1*xx+.2*yy)[active]))<1e-10
    assert model["unique_solution_verified"] and model["converged"]


def test_actual_observation_affine_rank_not_raw_source_rank_controls_identifiability():
    x=y=np.arange(7,dtype=float)
    active=np.zeros((7,7),dtype=bool)
    active[1:6,3:6]=True
    # Raw source positions are non-collinear; renormalized valid weights all
    # lie on x=3, so B[1,x,y] cannot anchor an affine x-gradient.
    xy=np.array([[2.1,1.5],[2.3,2.5],[2.1,3.5]],dtype=float)
    assert np.linalg.matrix_rank(np.column_stack((np.ones(3),xy)))==3
    with pytest.raises(ValueError,match="实际算子"):
        _minimum_curvature(x,y,xy,np.ones(3)*100,active.copy(),{})


def test_unobserved_disconnected_region_remains_missing_not_trend_fill():
    x=y=np.arange(10,dtype=float)
    active=np.zeros((10,10),dtype=bool)
    active[1:4,1:4]=True
    active[6:9,6:9]=True
    xy=np.array([[1,1],[1,3],[3,1],[3,3]],dtype=float)
    depth,model=_minimum_curvature(x,y,xy,np.ones(4)*100,active,{})
    assert np.isnan(depth[6:9,6:9]).all()
    assert model["unanchored_missing_nodes"]==9
    assert np.isfinite(depth[1:4,1:4]).all()


def test_iteration_limit_returns_candidate_and_real_diagnostic_not_false_convergence():
    result = build_dtm(source_text(lambda x,y:100+.00002*(x*x+y*y)),mc_config(minimum_curvature={"max_iterations":1}))
    assert not result["metadata"]["converged"]
    assert result["metadata"]["solver"]["stop_code"] == 7
    assert result["metadata"]["solver"]["iterations"] == 1
    assert any(w["code"]=="DTM_MINIMUM_CURVATURE_NOT_CONVERGED" for w in result["warnings"])
    assert result["metadata"]["valid_cells"] > 0
    assert_finite_json(result)


@pytest.mark.parametrize("parameters,match", [
    ({"max_work_units":10},"预算|max_work_units"),
    ({"max_nodes":4},"max_nodes"),
    ({"max_iterations":True},"有限"),
    ({"max_iterations":2.5},"整数"),
    ({"tension":1.1},"不大于"),
    ({"tolerance":float("nan")},"有限"),
    ({"boundary_tension":1},"不支持"),
])
def test_minimum_curvature_rejects_invalid_parameters_and_work(parameters,match):
    with pytest.raises(ValueError,match=match):
        build_dtm(source_text(),mc_config(minimum_curvature=parameters))


def bln_boundary():
    return "5,0,include\n-1100,-1100\n1100,-1100\n1100,1100\n-1100,1100\n-1100,-1100\n5,1,hole\n-250,-250\n250,-250\n250,250\n-250,250\n-250,-250"


@pytest.mark.parametrize("method", ["linear","idw","minimum_curvature"])
def test_bln_inclusion_exclusion_clip_points_raster_contours_and_derivatives(method):
    result = build_dtm(source_text(),mc_config(method=method,boundary_bln=bln_boundary(),boundary_crs=LOCAL.to_string()))
    assert result["metadata"]["boundary_excluded_source_points"] > 0
    assert result["metadata"]["boundary"]["include_count"]==1
    assert result["metadata"]["boundary"]["exclude_count"]==1
    with MemoryFile(base64.b64decode(result["geotiff_base64"])) as mem, mem.open() as raster:
        data = raster.read(1,masked=True)
        projected = Transformer.from_crs(LOCAL,raster.crs,always_xy=True)
        for x,y in [(0,0),(-150,50),(1150,0)]:
            px,py = projected.transform(x,y)
            r,c = raster.index(px,py)
            if 0<=r<raster.height and 0<=c<raster.width:
                assert np.ma.is_masked(data[r,c])
        assert raster.tags()["boundary_projected_json"]
    forward = Transformer.from_crs("EPSG:4326",LOCAL,always_xy=True)
    for feature in result["contours"]["features"]:
        coords = feature["geometry"]["coordinates"]
        lines = [coords] if feature["geometry"]["type"]=="LineString" else coords
        for line in lines:
            points = np.array([forward.transform(*p) for p in line])
            # No contour interior intersects the excluded square.
            excluded = shape({"type":"Polygon","coordinates":[[[-249,-249],[249,-249],[249,249],[-249,249],[-249,-249]]]})
            assert not LineString(points).intersects(excluded)
    assert_finite_json(result)


def test_geojson_polygon_holes_and_separate_exclusions_are_preserved():
    outer = [list(INVERSE.transform(x,y)) for x,y in [(-1100,-1100),(1100,-1100),(1100,1100),(-1100,1100),(-1100,-1100)]]
    inner = [list(INVERSE.transform(x,y)) for x,y in [(-250,-250),(250,-250),(250,250),(-250,250),(-250,-250)]]
    result = build_dtm(source_text(),mc_config(boundary_geojson={"type":"Polygon","coordinates":[outer,inner]}))
    assert result["metadata"]["boundary"]["source_crs"]=="EPSG:4326"
    forward = Transformer.from_crs(LOCAL,result["metadata"]["crs"],always_xy=True)
    px,py=forward.transform(0,0)
    xs,ys=result["preview"]["x_m"],result["preview"]["y_m"]
    j,i=np.argmin(abs(np.array(xs)-px)),np.argmin(abs(np.array(ys)-py))
    assert result["preview"]["depth_m"][i][j] is None


def test_bln_roundtrip_preserves_xy_xyz_flags_names_and_multiple_objects():
    text = bln_boundary()+"\n3,1,\"Survey line, measured\"\n1,2,100\n2,3,101\n4,5,103\n1\n3,4"
    document = read_bln(text,LOCAL.to_string())
    assert len(document["objects"])==4
    assert document["objects"][2]["coordinates"][0]==[1,2,100]
    assert read_bln(write_bln(document),document["crs"])==document
    assert_finite_json(document)


@pytest.mark.parametrize("text,crs", [
    ("5,0\n0,0\n1,1\n0,1\n1,0\n0,0","EPSG:4326"),
    ("5,0\n0,0", "EPSG:4326"),
    ("1,2\n0,0", "EPSG:4326"),
    ("1,1\nNaN,2", "EPSG:4326"),
    ("1,1\n1,2", None),
])
def test_invalid_bln_rejected(text,crs):
    with pytest.raises(ValueError):
        read_bln(text,crs)


@pytest.mark.parametrize("boundary", [
    {"boundary_bln":"3,0\n1,2\n2,3\n4,5","boundary_crs":"EPSG:4326"},
    {"boundary_bln":bln_boundary()},
    {"boundary_geojson":{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,1]]]}},
    {"boundary_geojson":{"type":"Point","coordinates":[118,22]}},
    {"boundary_geojson":{"type":"Polygon","coordinates":[[[179,0],[-179,0],[-179,1],[179,1],[179,0]]]}},
    {"boundary_bln":bln_boundary(),"boundary_crs":LOCAL.to_string(),"boundary_geojson":{"type":"Polygon"}},
])
def test_invalid_boundary_rejected_without_healing_or_crs_guess(boundary):
    with pytest.raises(ValueError):
        build_dtm(source_text(),mc_config(**boundary))


def full_raster(array,*,boundary_tag="",width=None,description="depth_m_positive_down",positive="down",converged="true"):
    array = np.asarray(array,dtype="float32")
    with MemoryFile() as mem:
        with mem.open(driver="GTiff",width=array.shape[1],height=array.shape[0],count=1,dtype="float32",crs=LOCAL,
                      transform=from_origin(-.5,array.shape[0]-.5,1,1),nodata=-9999) as raster:
            raster.write(np.where(np.isfinite(array),array,-9999),1)
            raster.set_band_description(1,description)
            raster.update_tags(depth_positive=positive,vertical_datum="analytic",boundary_projected_json=boundary_tag,solver_converged=converged)
        return {"geotiff_base64":base64.b64encode(mem.read()).decode(),"preview":{"depth_m":[[0]]}}


def test_full_raster_affine_slice_bottom_length_slope_and_csv_ignore_preview():
    # >128 nodes on a side: this deliberately exceeds the normal preview size.
    xx,yy = np.meshgrid(np.arange(180),np.arange(180)[::-1])
    grid = full_raster(100+2*xx+3*yy)
    result = extract_dtm_slice(grid,{"line":{"crs":LOCAL.to_string(),"coordinates":[[20,25],[160,145]]},"spacing_m":30})
    length = math.hypot(140,120)
    for item in result["samples"]:
        assert item["depth_m"]==pytest.approx(100+2*item["x_m"]+3*item["y_m"],abs=1e-9)
        assert item["slope_deg"]==pytest.approx(math.degrees(math.atan(640/length)),abs=1e-7)
    assert result["summary"]["complete"]
    assert result["summary"]["horizontal_length_m"]==pytest.approx(length)
    assert result["summary"]["bottom_length_m"]==pytest.approx(math.hypot(length,640))
    assert len(result["samples"])>10
    assert result["metadata"]["raster_width"]==180
    assert len(list(csv.DictReader(io.StringIO(result["csv_text"]))))==len(result["samples"])
    document=read_bln(result["slice_bln_text"],LOCAL.to_string())
    assert len(document["objects"])==1
    assert len(document["objects"][0]["coordinates"][0])==3
    assert_finite_json(result)


@pytest.mark.parametrize("method",["bilinear","nearest"])
def test_slice_nodata_is_never_zero_or_bridged_even_when_spacing_skips_hole(method):
    array=np.ones((10,21))*100
    array[:,9:12]=np.nan
    result=extract_dtm_slice(full_raster(array),{"line":{"crs":LOCAL.to_string(),"coordinates":[[2,4],[18,4]]},"spacing_m":100,"method":method})
    assert result["summary"]["missing_count"]>0
    assert result["summary"]["bottom_length_m"] is None
    assert result["summary"]["known_bottom_length_m"]<16
    assert len(result["valid_segments"])==2
    assert all(item["depth_m"] in (None,100) for item in result["samples"])
    for row in csv.DictReader(io.StringIO(result["csv_text"])):
        if not row["depth_m"]:
            assert not row["slope_deg"]
    assert len(read_bln(result["slice_bln_text"],LOCAL.to_string())["objects"])==2
    assert_finite_json(result)


def test_subpixel_polygon_exclusion_is_checked_by_complete_slice_and_splits_valid_segments():
    tag=json.dumps({"include":None,"exclude":{"type":"Polygon","coordinates":[[[9.1,0],[9.2,0],[9.2,9],[9.1,9],[9.1,0]]]}})
    result=extract_dtm_slice(full_raster(np.ones((10,21))*100,boundary_tag=tag),{"line":{"crs":LOCAL.to_string(),"coordinates":[[2,4],[18,4]]},"spacing_m":100})
    assert result["summary"]["missing_count"]>=1
    assert len(result["valid_segments"])==2
    assert result["summary"]["bottom_length_m"] is None
    assert result["summary"]["known_bottom_length_m"]<15.9


def test_slice_exact_node_does_not_require_zero_weight_nodata_neighbours():
    array=np.ones((4,4))*100
    array[1,2]=np.nan
    result=extract_dtm_slice(full_raster(array),{"line":{"crs":LOCAL.to_string(),"coordinates":[[1,2],[1,0]]},"spacing_m":1})
    assert all(item["depth_m"]==100 for item in result["samples"])
    assert result["summary"]["complete"]


def test_outside_raster_and_all_nodata_profile_is_explicit_not_extrapolated():
    result=extract_dtm_slice(full_raster(np.ones((4,4))*100),{"line":{"crs":LOCAL.to_string(),"coordinates":[[-10,-10],[-5,-5]]}})
    assert result["summary"]["valid_count"]==0
    assert result["summary"]["known_bottom_length_m"]==0
    assert result["summary"]["bottom_length_m"] is None
    assert result["valid_segments"]==[]
    assert result["slice_bln_text"]==""
    assert_finite_json(result)


def test_slice_bln_reference_and_nonconverged_source_are_reported():
    grid=full_raster(np.ones((10,10))*100,converged="false")
    result=extract_dtm_slice(grid,{"line_bln":"2,1,path\n1,1\n8,8","line_crs":LOCAL.to_string()})
    assert not result["summary"]["source_converged"]
    assert any(w["code"]=="DTM_SOURCE_NOT_CONVERGED" for w in result["warnings"])


def test_external_source_without_convergence_tag_is_unknown_not_claimed_converged():
    with MemoryFile() as memory:
        with memory.open(driver="GTiff",width=3,height=3,count=1,dtype="float32",crs=LOCAL,
                         transform=from_origin(-.5,2.5,1,1),nodata=-9999) as raster:
            raster.write(np.ones((3,3),dtype="float32")*100,1)
            raster.set_band_description(1,"depth_m_positive_down")
            raster.update_tags(depth_positive="down")
        grid={"geotiff_base64":base64.b64encode(memory.read()).decode()}
    result=extract_dtm_slice(grid,{"line":{"crs":LOCAL.to_string(),"coordinates":[[0,0],[2,2]]}})
    assert result["summary"]["source_converged"] is None
    assert result["summary"]["complete"]
    assert any(w["code"]=="DTM_SOURCE_CONVERGENCE_UNSPECIFIED" for w in result["warnings"])


def test_contour_and_extreme_depth_budgets_reject_before_large_allocation():
    with pytest.raises(ValueError,match="500"):
        build_dtm(source_text(lambda x,y:1000+.5*x+.2*y),mc_config(contour_interval_m=.1))
    with pytest.raises(ValueError,match="1,000,000"):
        build_dtm(source_text(lambda x,y:1e300),mc_config())


@pytest.mark.parametrize("grid,config", [
    ({"preview":{"depth_m":[[100]]}},{"line":{"coordinates":[[1,1],[2,2]],"crs":"EPSG:4326"}}),
    ({"geotiff_base64":"not base64"},{}),
    (full_raster(np.ones((10,10))*100),{"line":{"coordinates":[[1,1],[8,8]],"crs":LOCAL.to_string()},"max_samples":2}),
    (full_raster(np.ones((10,10))*100),{"line":{"coordinates":[[1,1],[8,8]]}}),
    (full_raster(np.ones((10,10))*100),{"line":{"coordinates":[[1,1],[1,1]],"crs":LOCAL.to_string()}}),
    (full_raster(np.ones((10,10))*100),{"line":{"coordinates":[[1,1],[8,8]],"crs":LOCAL.to_string()},"max_samples":True}),
    (full_raster(np.ones((10,10))*100,positive="up"),{"line":{"coordinates":[[1,1],[8,8]],"crs":LOCAL.to_string()}}),
])
def test_invalid_slice_input_budget_and_vertical_direction_rejected(grid,config):
    with pytest.raises(ValueError):
        extract_dtm_slice(grid,config)


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient
    from oceanroute.api import create_app
    from oceanroute.storage import ProjectStore
    return TestClient(create_app(ProjectStore(tmp_path/"dtm-extensions.sqlite3")))


def test_http_actual_grid_boundary_slice_and_bln_roundtrip_finite_json(client):
    response=client.post("/api/dtm/grid",json={"text":source_text(),"config":mc_config(boundary_bln=bln_boundary(),boundary_crs=LOCAL.to_string())})
    assert response.status_code==200,response.text
    grid=response.json()
    assert grid["metadata"]["solver"]["converged"]
    assert grid["metadata"]["solver"]["unique_solution_verified"]
    response=client.post("/api/dtm/slice",json={"grid":{"geotiff_base64":grid["geotiff_base64"]},"config":{"line":{"coordinates":[[-700,0],[700,0]],"crs":LOCAL.to_string()},"spacing_m":500}})
    assert response.status_code==200,response.text
    assert response.json()["summary"]["missing_count"]>0
    assert response.json()["summary"]["bottom_length_m"] is None
    assert_finite_json(response.json())
    response=client.post("/api/dtm/bln/read",json={"text":bln_boundary(),"crs":LOCAL.to_string()})
    assert response.status_code==200
    document=response.json()
    output=client.post("/api/dtm/bln/write",json={"document":document})
    assert output.status_code==200
    assert "attachment" in output.headers["content-disposition"]
    assert read_bln(output.text,document["crs"])==document


@pytest.mark.parametrize("path,payload",[
    ("/api/dtm/slice",{"grid":{"preview":{"depth_m":[[100]]}},"config":{}}),
    ("/api/dtm/bln/read",{"text":bln_boundary()}),
    ("/api/dtm/bln/write",{"document":{}}),
    ("/api/dtm/grid",{"text":source_text(),"config":mc_config(minimum_curvature={"max_work_units":1})}),
    ("/api/dtm/grid",{"text":source_text(),"config":[]}),
])
def test_http_invalid_dtm_contract_returns_422_not_internal_error(client,path,payload):
    response=client.post(path,json=payload)
    assert response.status_code==422,response.text
    assert_finite_json(response.json())
