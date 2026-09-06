"""Explicit fluid history, independently reproduced proof, and exact recovery.

These are new tests; legacy initialization/checkpoint tests remain unchanged.
The source example is a declared synthetic input, not a measured cable state.
"""
from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pytest

from oceanroute.checkpoints import _digest, read_checkpoint
from oceanroute.hydrodynamics import canonical_initial_fluid
from oceanroute.initial_equilibrium import (
    SCHEMA_V2, PROVENANCE_V3, estimate_initial_equilibrium_work,
    resolve_initial_equilibrium, validate_initialization_provenance,
)
from oceanroute.simulation import simulate_lay


def request(*, profile=False, **changes):
    path = Path(__file__).resolve().parents[1]/"examples/heterogeneous-initial-dynamic.json"
    c = json.loads(path.read_text())["config"]
    c.update(water_density_kg_m3=1025., drag_coefficient=1.2,
             current_x_m_s=.1, current_y_m_s=.12)
    c.update(changes)
    c["inline_bodies"][0].update(drag_area_m2=.02, drag_coefficient=1.4)
    if profile:
        c["current_profile"] = [
            {"depth_m":0.,"x_m_s":.1,"y_m_s":.12},
            {"depth_m":10.,"x_m_s":.04,"y_m_s":.18},
            {"depth_m":30.,"x_m_s":-.02,"y_m_s":.23},
        ]
    raw = c["initial_equilibrium"]
    raw["schema"] = SCHEMA_V2
    raw["initial_fluid"] = canonical_initial_fluid(c)
    return c


def independent_loading(c, p):
    """Natural overlap integrals and node-index secants, without load helpers."""
    rest = np.array(c["initial_equilibrium"]["rest_lengths_m"])
    q = c["initial_suspended_material_m"]+np.r_[np.cumsum(rest[::-1])[::-1],0.]
    rho = c["water_density_kg_m3"]
    element_weight, element_drag, compliance = [], [], []
    for lower, upper in zip(q[1:],q[:-1]):
        overlaps = [max(0.,min(upper,r["end_m"])-max(lower,r["start_m"]))
                    for r in c["material_segments"]]
        element_weight.append(sum(length*r["wet_weight_n_m"] for length,r in zip(overlaps,c["material_segments"])))
        element_drag.append(rho/2*sum(length*r.get("drag_coefficient",c["drag_coefficient"])*
                                     r.get("diameter_m",c["diameter_m"])
                                     for length,r in zip(overlaps,c["material_segments"])))
        compliance.append(sum(length/r["ea_n"] for length,r in zip(overlaps,c["material_segments"])))
    def half(elements):
        return np.r_[elements[0]/2,(np.array(elements[:-1])+elements[1:])/2,elements[-1]/2]
    weight = half(element_weight)
    cable_factor = half(element_drag)
    body_factor = np.zeros(len(p))
    for body in c["inline_bodies"]:
        for i in range(len(rest)):
            if q[i+1] <= body["material_m"] <= q[i]:
                a = (body["material_m"]-q[i+1])/rest[i]
                shares = np.array([a,1-a])
                weight[i:i+2] += shares*body["wet_weight_n"]
                body_factor[i:i+2] += rho/2*shares*body["drag_coefficient"]*body["drag_area_m2"]
                break
    secants = np.vstack([p[1]-p[0],(p[2:]-p[:-2])/2,p[-1]-p[-2]])
    tangent = secants/np.linalg.norm(secants,axis=1)[:,None]
    fluid = c["initial_equilibrium"]["initial_fluid"]
    current = np.tile(fluid["current_m_s"],(len(p),1))
    if fluid["current_profile"] is not None:
        rows = fluid["current_profile"]
        depths = np.maximum(-p[:,2],0)
        for axis,key in enumerate(("x_m_s","y_m_s")):
            current[:,axis] = np.interp(depths,[r["depth_m"] for r in rows],[r[key] for r in rows])
    projected = current-np.sum(current*tangent,axis=1)[:,None]*tangent
    cable = cable_factor[:,None]*np.linalg.norm(projected,axis=1)[:,None]*projected
    body = body_factor[:,None]*np.linalg.norm(current,axis=1)[:,None]*current
    external = cable+body
    external[:,2] -= weight
    lengths = np.linalg.norm(np.diff(p,axis=0),axis=1)
    tension = np.maximum(lengths-rest,0)/np.array(compliance)
    segment_force = tension[:,None]*np.diff(p,axis=0)/lengths[:,None]
    internal = np.zeros_like(p)
    internal[:-1] += segment_force
    internal[1:] -= segment_force
    return current,tangent,cable_factor,body_factor,cable,body,external,internal,tension


@pytest.mark.parametrize("profile",[False,True])
def test_fresh_proof_has_actual_historical_fluid_and_independent_nodal_loads(profile):
    c = request(profile=profile)
    result = resolve_initial_equilibrium({},c)
    proof = result["provenance"]
    assert proof["schema"] == PROVENANCE_V3
    assert proof["source"] == "oceanroute.current_equilibrium.solve_current_equilibrium"
    assert proof["verification"]["accepted"] is True
    assert proof["initial_paid_out_m"] == 0
    p = np.array(proof["initial_snapshot"]["positions"])
    u,tangent,kc,kb,dc,db,external,internal,tension = independent_loading(c,p)
    loading = proof["fluid_loading"]
    assert loading["initial_fluid"] == c["initial_equilibrium"]["initial_fluid"]
    assert loading["initial_fluid_sha256"] == _digest(loading["initial_fluid"])
    for field,expected in (("node_fluid_velocity_m_s",u),("node_tangent",tangent),
                           ("node_cable_drag_factor",kc),("node_body_drag_factor",kb),
                           ("node_cable_drag_n",dc),("node_body_drag_n",db),
                           ("node_external_force_n",external)):
        assert np.array(loading[field]) == pytest.approx(expected,rel=2e-12,abs=1e-11)
    snapshot = proof["initial_snapshot"]
    assert snapshot["segment_tension_n"] == pytest.approx(tension,rel=1e-10,abs=1e-8)
    boundary = np.array(snapshot["node_boundary_force_n"])
    assert boundary[[0,-1]] == pytest.approx(-(internal+external)[[0,-1]],abs=1e-8)
    assert np.max(np.linalg.norm((internal+external)[1:-1],axis=1)) <= proof["verification"]["force_tolerance_n"]
    assert np.max(np.abs(dc[:,2])) > 0  # Normal cable drag has a true vertical component.
    assert snapshot["node_contact_normal_force_n"] == [0.]*len(p)
    assert proof["material_loading"]["point_bodies"][0]["wet_weight_n"] == -30
    validate_initialization_provenance(json.loads(json.dumps(proof)),c)
    json.dumps(result,allow_nan=False)


@pytest.mark.parametrize("field",["current_x_m_s","current_y_m_s","water_density_kg_m3"])
def test_fresh_requires_raw_fluid_to_match_actual_current_and_density(field):
    c = request()
    c[field] += .01
    with pytest.raises(ValueError,match="must match.*fresh current/rho"):
        resolve_initial_equilibrium({},c)


@pytest.mark.parametrize("change",[
    lambda raw: raw.pop("initial_fluid"),
    lambda raw: raw["initial_fluid"].update(unknown=1),
    lambda raw: raw["initial_fluid"]["current_m_s"].__setitem__(2,.01),
    lambda raw: raw["initial_fluid"].update(operator="unrecognized operator"),
])
def test_raw_v2_requires_complete_supported_horizontal_fluid(change):
    c = request()
    change(c["initial_equilibrium"])
    with pytest.raises(ValueError,match="initial_fluid"):
        estimate_initial_equilibrium_work({},c)


def test_v1_zero_current_contract_is_preserved_and_does_not_auto_upgrade():
    c = request()
    c["initial_equilibrium"]["schema"] = "oceanroute.dynamic.initial-equilibrium.v1"
    c["initial_equilibrium"].pop("initial_fluid")
    with pytest.raises(ValueError,match="zero initial current"):
        resolve_initial_equilibrium({},c)


def test_real_v5_json_resume_keeps_historical_flow_without_another_optimization(monkeypatch):
    c = request(duration_s=.04)
    first = simulate_lay({},c)
    cp = json.loads(json.dumps(first["checkpoint"]))
    assert cp["schema_version"] == 4
    assert cp["model"] == "material-lumped-mass-xpbd-cable-lay-v5"
    assert cp["numerical"]["scheme"] == "implicit-compliant-material-nodes-current-equilibrium-prestress-v5"
    import oceanroute.current_equilibrium as solver
    monkeypatch.setattr(solver,"solve_current_equilibrium",lambda *a,**k:pytest.fail("resume reoptimized historical initialization"))
    resumed = simulate_lay({}, {"resume_state":cp,"duration_s":.04,
                                "current_x_m_s":-.15,"current_y_m_s":.25})
    assert resumed["checkpoint"]["config"]["current_x_m_s"] == -.15
    assert resumed["initialization"] == first["initialization"]
    assert resumed["frames"][0]["nodes"] == first["frames"][-1]["nodes"]
    assert resumed["summary"]["paid_out_m"] == 0
    assert resumed["frames"][-1]["touchdown"] is None
    assert resumed["frames"][-1]["bottom_tension_n"] is None
    assert np.max(np.linalg.norm(np.array(resumed["frames"][-1]["nodes"])[1:-1]-np.array(resumed["frames"][0]["nodes"])[1:-1],axis=1)) > 1e-9
    read_checkpoint(resumed["checkpoint"])


def test_future_depth_profile_override_does_not_rewrite_the_original_profile(monkeypatch):
    c = request(profile=True,duration_s=.02)
    first = simulate_lay({},c)
    import oceanroute.current_equilibrium as solver
    monkeypatch.setattr(solver,"solve_current_equilibrium",lambda *a,**k:pytest.fail("future profile reoptimized history"))
    future = [{"depth_m":0.,"x_m_s":-.2,"y_m_s":.05},
              {"depth_m":30.,"x_m_s":-.1,"y_m_s":-.15}]
    resumed = simulate_lay({}, {"resume_state":json.loads(json.dumps(first["checkpoint"])),
                                "duration_s":.02,"current_profile":future})
    assert resumed["checkpoint"]["config"]["current_profile"] == future
    assert resumed["initialization"] == first["initialization"]
    assert resumed["initialization"]["fluid_loading"]["initial_fluid"]["current_profile"] == c["current_profile"]
    read_checkpoint(resumed["checkpoint"])


def test_future_controls_cannot_replace_the_frozen_raw_initial_fluid():
    first = simulate_lay({},request(duration_s=.02))
    raw = deepcopy(first["checkpoint"]["config"]["initial_equilibrium"])
    raw["initial_fluid"]["current_m_s"][0] += .1
    with pytest.raises(ValueError,match="resume cannot change initial_equilibrium"):
        simulate_lay({}, {"resume_state":first["checkpoint"],"duration_s":.02,"initial_equilibrium":raw})


def test_relabelled_original_current_and_updated_hashes_still_require_real_force_balance():
    cp = deepcopy(simulate_lay({},request(profile=True,duration_s=.02))["checkpoint"])
    raw = cp["config"]["initial_equilibrium"]
    raw["initial_fluid"]["current_profile"][1]["x_m_s"] += .25
    proof = cp["state"]["initialization_provenance"]
    proof["request_sha256"] = _digest(raw)
    proof["initial_conditions"]["initial_fluid_sha256"] = _digest(raw["initial_fluid"])
    proof["fluid_loading"]["initial_fluid"] = deepcopy(raw["initial_fluid"])
    proof["fluid_loading"]["initial_fluid_sha256"] = _digest(raw["initial_fluid"])
    cp["checksum_sha256"] = _digest(cp)
    with pytest.raises(ValueError,match="force/geometry proof failed|historical fluid/drag/external"):
        read_checkpoint(cp)


def test_zero_flow_raw_v2_is_still_an_explicit_v5_branch():
    result = simulate_lay({},request(current_x_m_s=0.,current_y_m_s=0.,duration_s=.02))
    assert result["checkpoint"]["schema_version"] == 4
    assert result["initialization"]["schema"] == PROVENANCE_V3
    assert result["initialization"]["fluid_loading"]["node_cable_drag_n"] == [[0.,0.,0.]]*13


@pytest.mark.parametrize("field",["node_fluid_velocity_m_s","node_tangent","node_cable_drag_factor",
                                  "node_body_drag_factor","node_cable_drag_n","node_body_drag_n",
                                  "node_external_force_n"])
def test_recomputed_checksum_cannot_authorize_forged_historical_drag(field):
    cp = deepcopy(simulate_lay({},request(duration_s=.02))["checkpoint"])
    value = cp["state"]["initialization_provenance"]["fluid_loading"][field]
    if isinstance(value[0],list): value[0][0] += .01
    else: value[0] += .01
    cp["checksum_sha256"] = _digest(cp)
    with pytest.raises(ValueError,match="historical fluid/drag/external"):
        read_checkpoint(cp)


@pytest.mark.parametrize("field",["node_wet_weight_n","node_mass_kg","segment_ea_n","node_material_m",
                                  "node_cable_drag_factor","node_body_drag_factor"])
def test_all_current_material_evidence_is_rechecked_after_nonzero_current_step(field):
    cp = deepcopy(simulate_lay({},request(duration_s=.02))["checkpoint"])
    cp["state"][field][2] += .1
    cp["checksum_sha256"] = _digest(cp)
    with pytest.raises(ValueError,match="current material/loading"):
        read_checkpoint(cp)


def test_schema4_cannot_be_downgraded_or_given_legacy_numerical_scheme():
    cp = simulate_lay({},request(duration_s=.02))["checkpoint"]
    bad = deepcopy(cp)
    bad["schema_version"] = 3
    bad["model"] = "material-lumped-mass-xpbd-cable-lay-v4"
    bad["numerical"]["scheme"] = "implicit-compliant-material-nodes-equilibrium-prestress-v4"
    bad["checksum_sha256"] = _digest(bad)
    with pytest.raises(ValueError,match="raw initial equilibrium/proof"):
        read_checkpoint(bad)
    bad = deepcopy(cp)
    bad["numerical"]["scheme"] = "implicit-compliant-material-nodes-equilibrium-prestress-v4"
    bad["checksum_sha256"] = _digest(bad)
    with pytest.raises(ValueError,match="integration scheme"):
        read_checkpoint(bad)


def test_new_initializer_budget_and_real_metadata_capacity_precede_solver(monkeypatch):
    import oceanroute.current_equilibrium as solver
    monkeypatch.setattr(solver,"solve_current_equilibrium",lambda *a,**k:pytest.fail("optimized before a declared resource guard"))
    c = request()
    c["initial_equilibrium"]["solver"]["max_work_units"] = 1
    with pytest.raises(ValueError,match="work"):
        resolve_initial_equilibrium({},c)
    c = request(duration_s=.02)
    c["inline_bodies"][0]["id"] = "流"*350000
    with pytest.raises(ValueError,match="standalone checkpoint JSON volume"):
        resolve_initial_equilibrium({},c)
    with pytest.raises(ValueError,match="standalone checkpoint JSON volume"):
        simulate_lay({},c)


def test_actual_06_wheel_schema3_proof_keeps_old_branch_without_current_optimizer(monkeypatch):
    import oceanroute.current_equilibrium as solver
    monkeypatch.setattr(solver,"solve_current_equilibrium",lambda *a,**k:pytest.fail("legacy state was migrated to the current solver"))
    path = Path(__file__).resolve().parents[1]/"resources/validation/development_0.7_legacy_checkpoint_inputs.json"
    evidence = json.loads(path.read_text())
    assert "oceanroute-0.6.0-py3-none-any.whl/oceanroute/simulation.py" in evidence["imported_source"]
    for case in evidence["cases"]:
        cp = case["checkpoint"]
        assert cp["schema_version"] == 3
        assert read_checkpoint(cp)["state"]["initialization_provenance"]["schema"].endswith("provenance.v1")
        r = simulate_lay({}, {"resume_state":deepcopy(cp),"duration_s":.02})
        assert r["checkpoint"]["schema_version"] == 3
        assert r["checkpoint"]["model"] == cp["model"]
        assert r["initialization"] == cp["state"]["initialization_provenance"]
