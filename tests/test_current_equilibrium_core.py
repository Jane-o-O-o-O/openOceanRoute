"""Independent force/geometry checks of the current core and nodal operator.

Prescribed shapes below are made into exact reference balances by choosing
tractions and explicit load coefficients. Expected forces never call production
hydrodynamics, material or static helpers.
"""
from copy import deepcopy
import json
import math

import numpy as np
import pytest

from oceanroute.current_equilibrium import estimate_current_equilibrium_work, solve_current_equilibrium
from oceanroute.hydrodynamics import canonical_initial_fluid, validate_initial_fluid, hydrodynamic_loads, HydrodynamicField


def grid(x=None,y=None,bed=None):
    x = [-100.,100.] if x is None else x
    y = [-100.,100.] if y is None else y
    if bed is None:
        bed = lambda x,y: -100.
    return {"schema":"oceanroute.bathymetry.v1","x_m":x,"y_m":y,
            "z_m":[[bed(xx,yy) for xx in x] for yy in y],
            "source":{"name":"explicit synthetic independent reference","horizontal_crs":"LOCAL_CARTESIAN_METRES",
                      "origin_projected_m":[0,0],"vertical_datum":"model sea zero"}}


def force(p,rest,ea,w,kc,kb,fluid):
    """Direct mathematical oracle, including endpoint drag/gravity."""
    d = np.diff(p,axis=0)
    lengths = np.sqrt(np.sum(d*d,axis=1))
    t = ea*np.maximum(lengths/rest-1,0)
    traction = t[:,None]*d/lengths[:,None]
    internal = np.zeros_like(p)
    internal[:-1] += traction
    internal[1:] -= traction
    secants = np.vstack((p[1]-p[0],(p[2:]-p[:-2])/2,p[-1]-p[-2]))
    tangent = secants/np.sqrt(np.sum(secants*secants,axis=1))[:,None]
    u = np.tile(fluid["current_m_s"],(len(p),1))
    if fluid["current_profile"] is not None:
        rows = fluid["current_profile"]
        for axis,key in enumerate(("x_m_s","y_m_s")):
            u[:,axis] = np.interp(np.maximum(-p[:,2],0),[r["depth_m"] for r in rows],[r[key] for r in rows])
    normal = u-np.sum(u*tangent,axis=1)[:,None]*tangent
    dc = kc[:,None]*np.linalg.norm(normal,axis=1)[:,None]*normal
    db = kb[:,None]*np.linalg.norm(u,axis=1)[:,None]*u
    external = dc+db
    external[:,2] -= w
    return internal+external,internal,external,t,dc,db,u,tangent


def reference(contact=False):
    """Exact Hooke/normal-flow balance; nonuniform EA and natural lengths."""
    p = np.column_stack((-np.arange(8)*5.,np.zeros(8),[0,-10,-18,-24,-28,-30,-31,-31.5]))
    ea = np.linspace(1e4,2e4,7)
    delta = np.diff(p,axis=0)
    length = np.linalg.norm(delta,axis=1)
    h = np.r_[np.full(4,100.),np.full(3,120.)] if contact else np.arange(7)*2+100.
    t = h*length/5
    rest = length/(1+t/ea)
    traction = t[:,None]*delta/length[:,None]
    internal = np.zeros_like(p)
    internal[:-1] += traction
    internal[1:] -= traction
    kc,kb = np.zeros(8),np.zeros(8)
    fluid = canonical_initial_fluid({"current_x_m_s":.3,"current_y_m_s":.2 if contact else 0})
    u = np.array(fluid["current_m_s"])
    contact_n = np.zeros(8)
    if contact:
        kb[4] = 20/(np.linalg.norm(u)*(.3-.2*.45/2))
        normal = np.array([-.45,-2.,1.]);normal /= np.linalg.norm(normal)
        db = kb[:,None]*np.linalg.norm(u)*u
        contact_n[4] = -db[4,1]/normal[1]
        w = internal[:,2]+db[:,2]+contact_n*normal[2]
        terrain = grid([-40.,-20.,5.],[-3.,0.,3.],lambda x,y:-28+.45*(x+20)+2*y+.002*(x+20)*y)
    else:
        kb[3] = 1.
        secant = np.vstack((p[1]-p[0],(p[2:]-p[:-2])/2,p[-1]-p[-2]))
        tangent = secant/np.linalg.norm(secant,axis=1)[:,None]
        projected = u-np.sum(u*tangent,axis=1)[:,None]*tangent
        per = np.linalg.norm(projected,axis=1)[:,None]*projected
        db = kb[:,None]*np.linalg.norm(u)*u
        kc[1:-1] = (-internal[1:-1,0]-db[1:-1,0])/per[1:-1,0]
        assert np.min(kc) >= 0
        w = internal[:,2]+kc*per[:,2]+db[:,2]
        terrain = grid()
    w[0],w[-1] = 20.,10.
    c = {"seabed_grid":terrain,"vessel_position_m":p[0].tolist(),"anchor_position_m":p[-1].tolist(),
         "rest_lengths_m":rest.tolist(),"initial_positions_m":p.tolist(),"max_solver_iterations":100,
         "max_function_evaluations":2000,"force_tolerance_n":1e-6,"relative_force_tolerance":1e-9,
         "contact_tolerance_m":1e-8}
    args = {"segment_ea_n":ea,"node_wet_weight_n":w,"cable_drag_factor":kc,"body_drag_factor":kb,
            "initial_fluid":fluid,"load_scale_n":float(np.sum(np.abs(w)))}
    return c,args,p,t,contact_n


def test_profile_canonicalization_preserves_actual_density_and_overrides_constant():
    f = canonical_initial_fluid({"water_density_kg_m3":997.,"current_x_m_s":12.,
                                "current_profile":[{"depth_m":1.,"x_m_s":.4},{"depth_m":20.,"y_m_s":-.3}]})
    assert validate_initial_fluid(json.loads(json.dumps(f))) == f
    assert f["current_m_s"] == [12.,0.,0.]
    assert f["water_density_kg_m3"] == 997.
    p = np.array([[0,0,1],[0,0,-10],[0,0,-30]],dtype=float)
    local = {"drag":np.ones(3),"body_drag":np.ones(3)}
    out = hydrodynamic_loads(p,np.zeros_like(p),local,f)
    assert out["node_current_m_s"] == pytest.approx(np.array([[.4,0,0],[.4*(10/19),-.3*(9/19),0],[0,-.3,0]]))
    assert out["node_total_drag_force_n"] == pytest.approx(2*np.linalg.norm(out["node_current_m_s"],axis=1)[:,None]*out["node_current_m_s"])


@pytest.mark.parametrize("change",[
    lambda f:f.pop("operator"),lambda f:f.update(extra=0),lambda f:f.update(schema="future"),
    lambda f:f.update(operator="unknown"),lambda f:f["current_m_s"].__setitem__(2,.1),
    lambda f:f.update(water_density_kg_m3=True),lambda f:f.update(water_density_kg_m3=10**1000),
    lambda f:f.update(current_profile=[{"depth_m":2,"x_m_s":0,"y_m_s":0},{"depth_m":1,"x_m_s":0,"y_m_s":0}]),
    lambda f:f.update(current_profile=[{"depth_m":0},{"depth_m":1}]),
    lambda f:f["current_m_s"].__setitem__(0,float("nan")),
])
def test_invalid_historical_fluid_is_rejected(change):
    f = canonical_initial_fluid({})
    change(f)
    with pytest.raises(ValueError):
        validate_initial_fluid(f)


def test_depth_shear_drag_is_actual_secant_and_node_lumped_point_force():
    p = np.array([[0,0,-2],[-3,-1,-4],[-5,-2,-5],[-8,-2,-8]],dtype=float)
    f = canonical_initial_fluid({"current_profile":[{"depth_m":0,"x_m_s":.4,"y_m_s":.1},
                                                   {"depth_m":10,"x_m_s":-.2,"y_m_s":.5}]})
    kc,kb = np.array([2.,4.,3.,5.]),np.array([0.,6.,4.,0.])
    expected = force(p,np.ones(3),np.ones(3)*100,np.zeros(4),kc,kb,f)
    out = hydrodynamic_loads(p,np.zeros_like(p),{"drag":kc,"body_drag":kb},f)
    for key,index in (("node_cable_drag_force_n",4),("node_body_drag_force_n",5),("node_current_m_s",6),("node_tangent",7)):
        assert out[key] == pytest.approx(expected[index],abs=1e-13)
    assert np.max(np.abs(out["node_cable_drag_force_n"][:,2])) > .05
    # A point placed 60/40 between nodes is not evaluated at one averaged depth.
    point_u = .6*expected[6][1]+.4*expected[6][2]
    assert not np.allclose(np.sum(expected[5],axis=0),10*np.linalg.norm(point_u)*point_u)
    assert hydrodynamic_loads(p,out["node_current_m_s"],{"drag":kc,"body_drag":kb},f)["node_total_drag_force_n"] == pytest.approx(np.zeros_like(p))


def test_field_caches_complete_profile_and_owns_input_copy():
    f = canonical_initial_fluid({"current_profile":[{"depth_m":0,"x_m_s":.2},{"depth_m":10,"x_m_s":.4}]})
    field = HydrodynamicField(f)
    f["current_profile"][0]["x_m_s"] = 20
    p = np.array([[0,0,0],[0,0,-10]],dtype=float)
    assert field.profile_rows == 2
    for unused in range(3):
        assert field.loads(p,np.zeros_like(p),{"drag":[1,1],"body_drag":[0,0]})["node_current_m_s"][:,0] == pytest.approx([.2,.4])


def test_hydrodynamic_force_jacobian_is_nonconservative():
    p = np.column_stack((np.zeros(6),np.zeros(6),-2*np.arange(6))).astype(float)
    f = canonical_initial_fluid({"current_x_m_s":1.})
    local = {"drag":np.ones(6),"body_drag":np.zeros(6)}
    def load(points):return hydrodynamic_loads(points,np.zeros_like(points),local,f)["node_total_drag_force_n"]
    q = p.copy();q[3,0] += 1e-6
    dz_dx = (load(q)[2,2]-load(p)[2,2])/1e-6
    q = p.copy();q[2,2] += 1e-6
    dx_dz = (load(q)[3,0]-load(p)[3,0])/1e-6
    assert dz_dx == pytest.approx(.25,abs=1e-8)
    assert dx_dz == 0


def test_degenerate_centered_tangent_is_rejected_even_if_flow_is_zero():
    with pytest.raises(ValueError,match="degenerate"):
        hydrodynamic_loads([[0,0,0],[1,0,-1],[0,0,0]],np.zeros((3,3)),{"drag":[0]*3,"body_drag":[0]*3},canonical_initial_fluid({}))


def test_exact_current_balance_keeps_reference_and_actual_endpoint_support():
    c,a,p,t,normal = reference()
    r = solve_current_equilibrium(c,**a)
    assert r["accepted"]
    assert r["nodes"] == pytest.approx(p,abs=1e-7)
    assert r["segment_tension_n"] == pytest.approx(t,abs=1e-7)
    actual = force(np.array(r["nodes"]),np.array(c["rest_lengths_m"]),a["segment_ea_n"],a["node_wet_weight_n"],a["cable_drag_factor"],a["body_drag_factor"],a["initial_fluid"])
    assert np.max(np.linalg.norm(actual[0][1:-1],axis=1)) < 1e-6
    assert r["end_forces_on_cable_n"]["vessel"] == pytest.approx(-actual[0][0],abs=1e-8)
    assert r["end_forces_on_cable_n"]["anchor"] == pytest.approx(-actual[0][-1],abs=1e-8)
    assert r["solver"]["finite_difference_evaluations"] > 0
    assert r["solver"]["actual_work_units"] <= r["solver"]["estimated_work_units"]
    json.dumps(r,allow_nan=False)


def test_bad_shape_actually_optimizes_full_cable_drag_balance():
    c,a,p,t,normal = reference()
    q = p.copy();q[2] += [.2,.15,.1];q[5] += [-.1,-.1,.2]
    c["initial_positions_m"] = q.tolist()
    assert np.max(np.linalg.norm(force(q,np.array(c["rest_lengths_m"]),a["segment_ea_n"],a["node_wet_weight_n"],a["cable_drag_factor"],a["body_drag_factor"],a["initial_fluid"])[0][1:-1],axis=1)) > 100
    r = solve_current_equilibrium(c,**a)
    assert r["accepted"],r["solver"]
    assert r["nodes"] == pytest.approx(p,abs=1e-6)
    assert r["solver"]["iterations"] > 2
    assert r["initialization"]["auxiliary_no_current_solves"] == 0


def test_bilinear_normal_contact_has_nonzero_xy_fluid_and_exact_reaction():
    c,a,p,t,normal = reference(contact=True)
    r = solve_current_equilibrium(c,**a)
    assert r["accepted"],r["solver"]
    assert r["nodes"] == pytest.approx(p,abs=1e-7)
    assert r["node_contact_normal_force_n"] == pytest.approx(normal,abs=1e-7)
    assert r["node_contact_mask"][4] and not r["node_contact_mask"][-1]
    n = np.array([-.45,-2.,1.]);n /= np.linalg.norm(n)
    assert r["node_seabed_normal"][4] == pytest.approx(n,abs=1e-12)
    assert r["node_contact_force_n"][4] == pytest.approx(normal[4]*n,abs=1e-7)
    assert r["segment_clearance"]["minimum_clearance_m"] >= -1e-8
    assert np.linalg.norm(r["summary"]["total_force_balance_n"]) < 1e-6


def test_current_contact_reoptimizes_when_normal_cable_drag_is_added():
    c,a,p,t,normal = reference(contact=True)
    a["cable_drag_factor"] = np.ones(8)
    r = solve_current_equilibrium(c,**a)
    assert r["accepted"],r["solver"]
    assert np.max(np.linalg.norm(np.array(r["nodes"])-p,axis=1)) > .001
    assert r["node_contact_normal_force_n"][4] > normal[4]
    computed = force(np.array(r["nodes"]),np.array(c["rest_lengths_m"]),a["segment_ea_n"],a["node_wet_weight_n"],a["cable_drag_factor"],a["body_drag_factor"],a["initial_fluid"])[0]
    assert np.max(np.linalg.norm((computed+np.array(r["node_contact_force_n"]))[1:-1],axis=1)) < 1e-6


def test_rotation_covariance_of_solved_current_and_supports():
    c,a,p,t,normal = reference()
    r = solve_current_equilibrium(c,**a)
    rot = np.array([[0,-1,0],[1,0,0],[0,0,1.]])
    for field in ("vessel_position_m","anchor_position_m"):
        c[field] = (rot@np.array(c[field])).tolist()
    c["initial_positions_m"] = (p@rot.T).tolist()
    a["initial_fluid"] = canonical_initial_fluid({"current_y_m_s":.3})
    s = solve_current_equilibrium(c,**a)
    assert s["accepted"]
    assert s["nodes"] == pytest.approx(np.array(r["nodes"])@rot.T,abs=1e-7)
    assert s["end_forces_on_cable_n"]["vessel"] == pytest.approx(rot@r["end_forces_on_cable_n"]["vessel"],abs=1e-7)
    assert s["segment_tension_n"] == pytest.approx(r["segment_tension_n"],abs=1e-7)


def test_budget_preflight_uses_4nf_and_rejects_before_solver(monkeypatch):
    c,a,p,t,normal = reference()
    bound = estimate_current_equilibrium_work(c)
    assert bound["variables"] == 24
    assert bound["components"]["solver_dense"] == 100*24**3
    monkeypatch.setattr("oceanroute.current_equilibrium.least_squares",lambda *args,**kwargs:pytest.fail("optimizer called"))
    c["max_work_units"] = bound["estimated_work_units"]-1
    with pytest.raises(ValueError,match="4NF"):
        solve_current_equilibrium(c,**a)


def test_actual_evaluation_counter_exhaustion_returns_finite_unaccepted_candidate():
    c,a,p,t,normal = reference()
    c["max_function_evaluations"] = 1
    r = solve_current_equilibrium(c,**a)
    assert not r["accepted"]
    assert "COMPUTATION_BUDGET_EXHAUSTED" in r["solver"]["rejection_codes"]
    assert r["solver"]["function_evaluations"] == 1
    json.dumps(r,allow_nan=False)


def test_missing_bed_data_and_unsupported_sticking_are_not_fallbacks():
    c,a,p,t,normal = reference()
    c["seabed_grid"]["x_m"] = [-100.,0.,100.]
    c["seabed_grid"]["z_m"] = [[-100.,None,-100.],[-100.,-100.,-100.]]
    with pytest.raises(ValueError,match="complete known"):
        solve_current_equilibrium(c,**a)
    c,a,p,t,normal = reference()
    c["contact_policy"] = "prescribed_stick"
    with pytest.raises(ValueError,match="frictionless"):
        solve_current_equilibrium(c,**a)


def test_whole_chord_rejection_even_when_all_nodes_and_forces_are_valid():
    c,a,p,t,normal = reference()
    ridge_x = float((p[0,0]+p[1,0])/2)
    ridge_z = float((p[0,2]+p[1,2])/2+.2)
    heights = {float(point[0]):float(point[2]-5) for point in p}
    heights.update({-100.:-80.,100.:-5.,ridge_x:ridge_z})
    xs = sorted(heights)
    terrain = grid(xs,[-2.,2.])
    terrain["z_m"] = [[heights[x] for x in xs] for y in [-2,2]]
    c["seabed_grid"] = terrain
    r = solve_current_equilibrium(c,**a)
    assert not r["accepted"]
    assert np.min(r["node_clearance_m"]) > 1
    assert r["solver"]["max_node_force_residual_n"] < 1e-6
    assert r["segment_clearance"]["minimum_clearance_m"] == pytest.approx(-.2,abs=1e-6)
    assert "STRAIGHT_SEGMENT_BED_INTERSECTION_OR_UNKNOWN" in r["solver"]["rejection_codes"]
