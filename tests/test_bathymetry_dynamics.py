"""2D terrain mechanics, persistence and meaningful negative cases."""
from copy import deepcopy
import json

import numpy as np
import pytest

from oceanroute.bathymetry import BathymetryGrid
from oceanroute.checkpoints import pack_checkpoint, read_checkpoint
from oceanroute.simulation import catenary, simulate_lay, span_analysis, steady_state


def plane(sx=.02, sy=-.02, depth=12, axes=(-30, 0, 30)):
    x = np.array(axes, dtype=float); y = x.copy()
    return {"schema":"oceanroute.bathymetry.v1", "x_m":x.tolist(), "y_m":y.tolist(),
            "z_m":(-depth+sx*x[None,:]+sy*y[:,None]).tolist(),
            "source":{"name":"explicit synthetic plane", "horizontal_crs":"LOCAL_CARTESIAN_METRES",
                      "origin_projected_m":[0,0], "vertical_datum":"already aligned model sea surface z=0"}}


BASE = {"depth_m":12,"bottom_tension_n":10,"wet_weight_n_m":4,"nodes":16,
        "ship_speed_m_s":.2,"payout_m_s":.4,"duration_s":2,"dt_s":.5,
        "internal_dt_s":.02,"solver_iterations":24,"heading_deg":90}


def repack(saved):
    return pack_checkpoint(saved["config"], saved["state"], saved["time_s"], saved["numerical"])


def test_affine_and_bilinear_height_gradient_on_nonuniform_axes():
    document = plane(.04, -.07, axes=(-20, -3, 5, 30))
    field = BathymetryGrid(document)
    points = np.array([[-20,-20], [-2,4], [5,5], [30,30], [12,-9]])
    z, gradient = field.evaluate(points)
    assert z == pytest.approx(-12+.04*points[:,0]-.07*points[:,1], abs=1e-13)
    assert gradient == pytest.approx(np.tile([.04,-.07], (len(points),1)), abs=1e-13)
    document["z_m"] = [[-12+.04*x-.07*y+.001*x*y for x in document["x_m"]] for y in document["y_m"]]
    field = BathymetryGrid(document)
    z, gradient = field.evaluate(points)
    assert z == pytest.approx(-12+.04*points[:,0]-.07*points[:,1]+.001*points[:,0]*points[:,1], abs=1e-13)
    assert gradient[:,0] == pytest.approx(.04+.001*points[:,1])
    assert gradient[:,1] == pytest.approx(-.07+.001*points[:,0])
    assert np.linalg.norm(field.surface(points)[1], axis=1) == pytest.approx(np.ones(len(points)))


def test_height_at_known_node_ignores_zero_weights_but_normal_requires_known_cell():
    document = plane(); document["z_m"][1][1] = None
    # All cells here use centre NoData: enlarge to retain a known cell.
    document = plane(axes=(-30,-10,10,30)); document["z_m"][1][1] = None
    field = BathymetryGrid(document)
    assert field.evaluate([[-30,-30]], gradient=False)[0] == document["z_m"][0][0]
    with pytest.raises(ValueError, match="NoData"):
        field.surface([[-30,-30]])
    with pytest.raises(ValueError, match="NoData"):
        field.evaluate([[-20,-20]], gradient=False)
    with pytest.raises(ValueError, match="outside"):
        field.surface([[30.00001,0]])
    assert field.surface([[30,30]])[0][0] == document["z_m"][-1][-1]


@pytest.mark.parametrize("mutate", [
    lambda g:g.update(schema="invented"), lambda g:g.update(x_m=[0,0,1]),
    lambda g:g.update(x_m=[0,1e-6,1]), lambda g:g.update(x_m=[False,1,2]),
    lambda g:g.update(x_m=[-(10**10000),0,1]),
    lambda g:g.update(y_m=[0,"1",2]), lambda g:g.update(z_m=[[0]*3]*3),
    lambda g:g.update(z_m=[[None]*3]*3), lambda g:g["z_m"][0].pop(),
    lambda g:g["z_m"][0].__setitem__(0,float("nan")),
    lambda g:g["source"].update(horizontal_crs="EPSG:4326"),
    lambda g:g["source"].update(horizontal_crs="EPSG:2263"),
    lambda g:g["source"].update(origin_projected_m=[10,0]),
    lambda g:g["source"].pop("vertical_datum"), lambda g:g["source"].update(sha256="bad"),
    lambda g:g.update(x_m=list(range(101)),y_m=list(range(101)))])
def test_invalid_grid_schema_values_units_missing_and_budget_rejected(mutate):
    grid = plane(); mutate(grid)
    with pytest.raises(ValueError, match="seabed_grid|LOCAL"):
        BathymetryGrid(grid)


def test_projected_metre_provenance_is_label_not_a_coordinate_or_vertical_transform():
    document = plane(); document["source"].update(horizontal_crs="EPSG:32650", origin_projected_m=[400000,3500000], vertical_datum="input chart datum, explicitly already corrected to model surface")
    field = BathymetryGrid(document)
    assert field.surface([[0,0]])[0] == pytest.approx([-12])
    assert field.metadata()["grid"] == document


def test_normal_projection_crosses_cells_and_preserves_plane_tangent_displacement():
    document = plane(.2,.1,axes=(-2,0,2)); field = BathymetryGrid(document)
    p = np.array([[.03,.4,-12.8]])
    original = p.copy(); multiplier = np.zeros(1)
    sweeps, correction = field.project(p,np.array([.5]),multiplier)
    assert p[0,0] < 0  # Real normal correction crosses the x=0 cell boundary.
    assert sweeps >= 2 and multiplier[0] > 0
    normal = np.array([-.2,-.1,1])/np.sqrt(1.05)
    assert p[0]-original[0] == pytest.approx(np.dot(p[0]-original[0],normal)*normal, abs=1e-12)
    assert p[:,2] == pytest.approx(field.surface(p)[0], abs=1e-10)
    assert correction == pytest.approx(2*(p-original), abs=1e-12)
    curved = deepcopy(document)
    curved["z_m"] = [[-12+.2*x+.1*y+.02*x*y for x in curved["x_m"]] for y in curved["y_m"]]
    p = original.copy(); f = BathymetryGrid(curved)
    f.project(p,np.array([.5]),np.zeros(1))
    assert p[:,2] == pytest.approx(f.surface(p)[0], abs=1e-9)


def test_projection_budget_outside_and_unknown_new_cell_fail_instead_of_flat_floor():
    field = BathymetryGrid(plane(.2,.1,axes=(-2,0,2)))
    with pytest.raises(ValueError, match="did not converge"):
        field.project(np.array([[.03,.4,-12.8]]),np.ones(1),np.zeros(1),max_iterations=1)
    with pytest.raises(ValueError, match="outside"):
        field.project(np.array([[-2.,0.,-14.]]),np.ones(1),np.zeros(1))
    document=plane(.2,.1,axes=(-2,0,2,4)); document["z_m"][0][0]=None
    # Starts in known [0,2]x[-2,0], projects left into unknown [-2,0].
    with pytest.raises(ValueError, match="NoData"):
        BathymetryGrid(document).project(np.array([[.01,-.4,-12.8]]),np.ones(1),np.zeros(1))


@pytest.mark.parametrize("heading", [0,45,90])
def test_actual_cross_and_along_slope_contacts_obey_normal_velocity_and_coulomb_bound(heading):
    document = plane(.2,.1)
    result=simulate_lay({}, {**BASE,"seabed_grid":document,"heading_deg":heading,"duration_s":3})
    assert result["model"].endswith("v3") and result["validation_status"] == "research"
    assert result["solver"]["converged"]
    assert result["summary"]["material_balance_residual_m"] < 1e-10
    assert result["solver"]["contact"]["actual_bilinear_node_queries_this_run"] <= result["solver"]["work_basis"]["additional_bilinear_node_query_upper_bound"]
    assert result["summary"]["contact"]["friction_dissipation_j"] > 0
    total_contact = 0
    for frame in result["frames"]:
        p=np.array(frame["nodes"]); v=np.array(frame["node_velocity_m_s"])
        n=np.array(frame["node_seabed_normal"]); mask=np.array(frame["node_contact_mask"])
        jn=np.array(frame["node_contact_normal_impulse_n_s"]); jt=np.array(frame["node_contact_friction_impulse_n_s"])
        expected_bed=-12+.2*p[:,0]+.1*p[:,1]
        assert frame["node_seabed_z_m"] == pytest.approx(expected_bed,abs=1e-12)
        assert np.min(p[:,2]-expected_bed) > -1e-8
        assert np.min(jn) >= 0 and np.max(np.linalg.norm(jt,axis=1)-.5*jn) < 1e-9
        assert np.max(np.abs(np.sum(jt*n,axis=1))) < 1e-9
        free=mask.copy(); free[[0,-1]]=False
        assert np.min(np.sum(v*n,axis=1)[free],initial=0) > -1e-8
        assert np.all(jn[[0,-1]] == 0)
        total_contact += int(np.count_nonzero(free))
        assert all(np.isfinite(value) for value in frame["energy"].values())
    assert total_contact > 0
    assert result["seabed"]["grid"] == document
    json.dumps(result,allow_nan=False)


def test_friction_changes_actual_sliding_geometry_and_dissipates_energy():
    document=plane(.2,.1)
    smooth=simulate_lay({}, {**BASE,"seabed_grid":document,"duration_s":4,"seabed_friction":0})
    rough=simulate_lay({}, {**BASE,"seabed_grid":document,"duration_s":4,"seabed_friction":.5})
    assert smooth["summary"]["contact"]["friction_dissipation_j"] == 0
    assert rough["summary"]["contact"]["friction_dissipation_j"] > .01
    assert np.max(np.linalg.norm(np.array(smooth["frames"][-1]["nodes"])-rough["frames"][-1]["nodes"],axis=1)) > .02
    assert np.max(np.abs(np.array(rough["frames"][-1]["nodes"])[1:-1,1])) > 1e-4
    losses=[f["energy"]["cumulative_contact_friction_dissipation_j"] for f in rough["frames"]]
    assert np.min(np.diff(losses)) >= 0


def test_varying_bilinear_cells_change_actual_contact_normals_and_remain_finite():
    document=plane(.1,.05,axes=(-30,-5,0,5,30))
    document["z_m"]=[[z+.002*x*y for x,z in zip(document["x_m"],row)] for y,row in zip(document["y_m"],document["z_m"])]
    result=simulate_lay({}, {**BASE,"seabed_grid":document,"duration_s":3,"current_y_m_s":.2})
    assert result["solver"]["converged"]
    normals=np.array(result["frames"][-1]["node_seabed_normal"])
    assert np.ptp(normals[:,0]) > 1e-4 and np.ptp(normals[:,1]) > 1e-3
    field=BathymetryGrid(document)
    for frame in result["frames"]:
        p=np.array(frame["nodes"])
        assert np.min(p[:,2]-field.surface(p)[0]) >= -1e-8
    assert result["summary"]["material_balance_residual_m"] < 1e-10
    json.dumps(result,allow_nan=False)


def test_temporal_refinement_reduces_position_and_tension_change_on_sloped_elastic_case():
    positions=[]; tensions=[]
    for dt in (.04,.02,.01):
        result=simulate_lay({}, {**BASE,"seabed_grid":plane(.2,.1),"internal_dt_s":dt,
                                 "payout_m_s":.2,"ea_n":1e4})
        assert result["solver"]["converged"]
        positions.append(np.array(result["frames"][-1]["nodes"]))
        tensions.append(result["frames"][-1]["top_tension_n"])
    changes=[np.max(np.linalg.norm(a-b,axis=1)) for a,b in zip(positions[:-1],positions[1:])]
    assert changes[1] < .8*changes[0]
    assert abs(tensions[2]-tensions[1]) < .5*abs(tensions[1]-tensions[0])
    assert changes[1] < .005


def test_stationary_flat_grid_frictionless_path_matches_legacy_positions_velocity_and_tension():
    config={**BASE,"ship_speed_m_s":0,"payout_m_s":0,"seabed_friction":0}
    legacy=simulate_lay({},config)
    grid=simulate_lay({},dict(config,seabed_grid=plane(0,0)))
    assert legacy["checkpoint"]["schema_version"] == 1
    for a,b in zip(legacy["frames"],grid["frames"]):
        for key in ("nodes","node_velocity_m_s","node_tension_n"):
            assert np.array(a[key]) == pytest.approx(np.array(b[key]),abs=1e-8,rel=1e-8)
    old=simulate_lay({}, {"resume_state":legacy["checkpoint"],"duration_s":1})
    assert old["checkpoint"]["schema_version"] == 1


def test_complete_2d_checkpoint_restores_contacts_and_actual_state_identically():
    config={**BASE,"seabed_grid":plane(.2,.1),"duration_s":4,"checkpoint_times_s":[1.375]}
    full=simulate_lay({},config)
    saved=full["checkpoints"][0]
    resumed=simulate_lay({}, {"resume_state":json.loads(json.dumps(saved)),"duration_s":2.625})
    assert saved["schema_version"] == 2
    for key in ("positions","velocities","rest_lengths_m","node_material_m","last_segment_tensions_n",
                "node_seabed_normal","node_contact_normal_impulse_n_s","node_contact_friction_impulse_n_s"):
        assert np.array(resumed["checkpoint"]["state"][key]) == pytest.approx(np.array(full["checkpoint"]["state"][key]),abs=1e-8,rel=1e-9)
    assert resumed["frames"][0]["node_contact_normal_impulse_n_s"] == saved["state"]["node_contact_normal_impulse_n_s"]
    assert resumed["summary"]["contact"] == pytest.approx(full["summary"]["contact"],abs=1e-10)
    assert resumed["checkpoint"]["state"]["contact_mask"] == full["checkpoint"]["state"]["contact_mask"]
    assert resumed["checkpoint"]["config"]["seabed_grid"] == config["seabed_grid"]
    assert read_checkpoint(resumed["checkpoint"])["time_s"] == 4


@pytest.mark.parametrize("field", ["node_seabed_normal","node_contact_normal_impulse_n_s","node_contact_friction_impulse_n_s","last_contact_step_s"])
def test_missing_contact_checkpoint_state_rejected(field):
    checkpoint=simulate_lay({},dict(BASE,seabed_grid=plane(),duration_s=.1))["checkpoint"]
    checkpoint["state"].pop(field)
    with pytest.raises(ValueError,match="contact"):
        read_checkpoint(repack(checkpoint))


def test_2d_checkpoint_cannot_change_grid_datum_contacts_or_fake_version():
    checkpoint=simulate_lay({},dict(BASE,seabed_grid=plane(),duration_s=.1))["checkpoint"]
    changed=plane(); changed["source"]["vertical_datum"]="another"
    with pytest.raises(ValueError,match="cannot change seabed_grid"):
        simulate_lay({}, {"resume_state":checkpoint,"duration_s":1,"seabed_grid":changed})
    for mutate in (lambda cp:cp["state"]["node_seabed_normal"][1].__setitem__(0,1),
                   lambda cp:cp["state"]["node_contact_friction_impulse_n_s"][1].__setitem__(0,100),
                   lambda cp:cp["state"]["statistics"].pop("friction_dissipation_j")):
        cp=deepcopy(checkpoint); mutate(cp)
        with pytest.raises(ValueError,match="normal|friction|statistics"):
            read_checkpoint(repack(cp))
    cp=deepcopy(checkpoint); cp["schema_version"]=1
    from oceanroute.checkpoints import _digest
    cp["checksum_sha256"]=_digest(cp)
    with pytest.raises(ValueError,match="incompatible"):
        read_checkpoint(cp)
    legacy=simulate_lay({},dict(BASE,duration_s=.1))["checkpoint"]
    with pytest.raises(ValueError,match="cannot change seabed_grid"):
        simulate_lay({}, {"resume_state":legacy,"duration_s":1,"seabed_grid":plane()})


def test_missing_origin_suspended_cable_extent_and_late_ship_coverage_reject():
    with pytest.raises(ValueError,match="outside|insufficient"):
        simulate_lay({},dict(BASE,seabed_grid=plane(axes=(-1,0,1))))
    document=plane(axes=(-30,-2,2,30)); document["z_m"][2][2]=None
    with pytest.raises(ValueError,match="NoData"):
        simulate_lay({},dict(BASE,seabed_grid=document))
    document=plane(axes=(-30,0,1))
    with pytest.raises(ValueError,match="outside"):
        simulate_lay({},dict(BASE,seabed_grid=document,duration_s=1,ship_speed_m_s=2,payout_m_s=2))
    with pytest.raises(ValueError,match="computation"):
        simulate_lay({},dict(BASE,seabed_grid=plane(),max_work_units=100))


def test_grid_profile_and_unsupported_static_or_wave_paths_reject_explicitly():
    config=dict(BASE,seabed_grid=plane(),seabed_profile=[{"x_m":-30,"depth_m":12},{"x_m":30,"depth_m":12}])
    with pytest.raises(ValueError,match="choose"):
        simulate_lay({},config)
    for function in (catenary,steady_state,span_analysis):
        with pytest.raises(ValueError,match="seabed_grid"):
            function({"seabed_grid":plane()})
    from oceanroute.sea import generate_sea_state
    sea=generate_sea_state({"spectrum":"regular","hs_m":.2,"tp_s":4,"duration_s":2,"sample_dt_s":.1,
                            "depth_m":12,"heave_rao":[{"frequency_hz":.25,"amplitude_m_m":1,"phase_deg":0}]})
    # AiryField consumes this documented actual sea configuration envelope.
    with pytest.raises(ValueError,match="Airy|seabed_grid"):
        simulate_lay({},dict(BASE,seabed_grid=plane(),wave_kinematics=sea["wave_kinematics"]))


def test_actual_http_dynamic_grid_json_resume_and_missing_coverage_errors(tmp_path):
    from fastapi.testclient import TestClient
    from oceanroute.api import create_app
    from oceanroute.storage import ProjectStore
    with TestClient(create_app(ProjectStore(tmp_path/"terrain.sqlite3"))) as client:
        config=dict(BASE,seabed_grid=plane(),duration_s=.5)
        response=client.post("/api/simulation/dynamic",json={"config":config})
        assert response.status_code == 200
        result=response.json(); json.dumps(result,allow_nan=False)
        assert result["seabed"]["grid"] == config["seabed_grid"]
        assert result["checkpoint"]["schema_version"] == 2
        response=client.post("/api/simulation/dynamic",json={"config":{"resume_state":result["checkpoint"],"duration_s":.5}})
        assert response.status_code == 200
        assert response.json()["summary"]["end_time_s"] == 1
        response=client.post("/api/simulation/dynamic",json={"config":dict(config,seabed_grid=plane(axes=(-1,0,1)))})
        assert response.status_code == 422
        assert "insufficient" in response.json()["detail"] or "outside" in response.json()["detail"]
        response=client.post("/api/simulation/steady",json={"config":{"seabed_grid":plane()}})
        assert response.status_code == 422
        assert "seabed_grid" in response.json()["detail"]
