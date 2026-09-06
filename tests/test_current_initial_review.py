"""Independent force/material audit of explicitly declared current startup.

Expected material totals use Decimal interval intersections. The off-bed
reference is constructed in segment-traction coordinates using an independent
force recurrence, not a production geometry, material or hydrodynamics helper.
Every returned candidate is audited again from its actual coordinates.
"""
from copy import deepcopy
from decimal import Decimal, localcontext
import hashlib
import json
import math
from pathlib import Path
import time

import numpy as np
import pytest
from scipy.optimize import root
from fastapi.testclient import TestClient

from oceanroute.api import create_app
from oceanroute.checkpoints import read_checkpoint
from oceanroute.initial_equilibrium import resolve_initial_equilibrium
from oceanroute.simulation import simulate_lay
from oceanroute.storage import ProjectStore
from oceanroute.voyage import read_voyage_checkpoint, run_voyage


G = 9.80665
REST = np.array([12., 2., 3., 1., 4., 2., 3.])
ORIGIN = 37.
RAW_CURRENT = "oceanroute.dynamic.initial-equilibrium.v2"
PROOF_CURRENT = "oceanroute.dynamic.initial-equilibrium.provenance.v3"
MODEL_CURRENT = "material-lumped-mass-xpbd-cable-lay-v5"


def dec(value):
    return Decimal.from_float(float(value))


def half_total(values):
    values = np.asarray(values, dtype=float)
    return np.r_[values[0]/2, (values[:-1]+values[1:])/2, values[-1]/2]


def interval_expected(c, rest):
    """60-digit local intersections; no production material operator imports."""
    with localcontext() as context:
        context.prec = 60
        lengths = [dec(value) for value in rest]
        q = [dec(c["initial_suspended_material_m"])+sum(lengths[j:], Decimal(0))
             for j in range(len(lengths)+1)]
        components = {key: [] for key in ("wet", "dry", "displaced", "compliance", "cd_d")}
        for low, high in zip(q[1:], q[:-1]):
            part = {key: Decimal(0) for key in components}
            covered = Decimal(0)
            for material in c["material_segments"]:
                overlap = max(Decimal(0), min(high, dec(material["end_m"]))-
                              max(low, dec(material["start_m"])))
                covered += overlap
                part["wet"] += overlap*dec(material["wet_weight_n_m"])
                part["dry"] += overlap*dec(material["mass_kg_m"])
                part["displaced"] += overlap*dec(c["water_density_kg_m3"]*math.pi*material["diameter_m"]**2/4)
                part["compliance"] += overlap/dec(material["ea_n"])
                part["cd_d"] += overlap*dec(material["drag_coefficient"])*dec(material["diameter_m"])
            assert covered == high-low, "independent fixture requires exact declared coverage"
            for key in components:
                components[key].append(float(part[key]))
        alpha, body_wet, body_dry, body_inertia, area = {}, *(np.zeros(len(q)) for _ in range(4))
        for body in c.get("inline_bodies", []):
            assert body["length_m"] == 0, "reference never approximates finite rods"
            station = dec(body["material_m"])
            fractions = np.zeros(len(q))
            if q[-1] <= station <= q[0]:
                for i, length in enumerate(lengths):
                    if q[i+1] <= station <= q[i]:
                        a = float((station-q[i+1])/length)
                        fractions[i:i+2] = [a, 1-a]
                        break
            alpha[body["id"]] = fractions
            body_wet += fractions*body["wet_weight_n"]
            body_dry += fractions*body["mass_kg"]
            body_inertia += fractions*(body["mass_kg"]+c["added_mass_coefficient"]*
                                      max(0., body["mass_kg"]-body["wet_weight_n"]/G))
            area += fractions*body["drag_coefficient"]*body["drag_area_m2"]
    cable_wet = half_total(components["wet"])
    return {"q": np.array([float(value) for value in q]), "alpha": alpha,
            "wet": cable_wet+body_wet, "cable_wet": cable_wet, "body_wet": body_wet,
            "dry": half_total(components["dry"])+body_dry,
            "mass": half_total(np.asarray(components["dry"])+c["added_mass_coefficient"]*
                               np.asarray(components["displaced"]))+body_inertia,
            "compliance": np.array(components["compliance"]),
            "ea": np.asarray(rest)/np.array(components["compliance"]),
            "kc": c["water_density_kg_m3"]/2*half_total(components["cd_d"]),
            "kb": c["water_density_kg_m3"]/2*area}


def velocity_expected(c, positions):
    """Explicit depth-linear interpolation and end holding, without helpers."""
    positions = np.asarray(positions)
    if "current_profile" not in c:
        return np.tile([c["current_x_m_s"], c["current_y_m_s"], 0.], (len(positions), 1))
    rows = c["current_profile"]
    result = []
    for z in positions[:, 2]:
        depth = max(-float(z), 0.)
        if depth <= rows[0]["depth_m"]:
            value = [rows[0]["x_m_s"], rows[0]["y_m_s"]]
        elif depth >= rows[-1]["depth_m"]:
            value = [rows[-1]["x_m_s"], rows[-1]["y_m_s"]]
        else:
            lo, hi = next((a, b) for a, b in zip(rows[:-1], rows[1:])
                          if a["depth_m"] <= depth <= b["depth_m"])
            fraction = (depth-lo["depth_m"])/(hi["depth_m"]-lo["depth_m"])
            value = [(1-fraction)*lo[key]+fraction*hi[key] for key in ("x_m_s", "y_m_s")]
        result.append([*value, 0.])
    return np.array(result)


def drag_expected(c, positions, loading, velocities=None):
    """Declared Morison normal cable and node-lumped isotropic point force."""
    positions = np.asarray(positions)
    secant = np.vstack([positions[1]-positions[0],
                        (positions[2:]-positions[:-2])/2,
                        positions[-1]-positions[-2]])
    tangent = secant/np.linalg.norm(secant, axis=1)[:, None]
    fluid = velocity_expected(c, positions)
    relative = fluid-(0. if velocities is None else np.asarray(velocities))
    normal = relative-np.sum(relative*tangent, axis=1)[:, None]*tangent
    cable = loading["kc"][:, None]*np.linalg.norm(normal, axis=1)[:, None]*normal
    body = loading["kb"][:, None]*np.linalg.norm(relative, axis=1)[:, None]*relative
    return {"fluid": fluid, "tangent": tangent, "cable": cable, "body": body}


def forces_expected(c, positions, rest, loading):
    positions = np.asarray(positions)
    delta = np.diff(positions, axis=0)
    lengths = np.linalg.norm(delta, axis=1)
    tension = np.maximum(lengths-np.asarray(rest), 0)/loading["compliance"]
    axial = np.zeros_like(positions)
    segment = tension[:, None]*delta/lengths[:, None]
    axial[:-1] += segment
    axial[1:] -= segment
    hydro = drag_expected(c, positions, loading)
    force = axial+hydro["cable"]+hydro["body"]
    force[:, 2] -= loading["wet"]
    return {"tension": tension, "force": force, "axial": axial, **hydro}


def explicit_bed():
    return {"schema": "oceanroute.bathymetry.v1", "x_m": [-100., 0., 100.],
            "y_m": [-100., 0., 100.], "z_m": [[-100., -100., -100.] for _ in range(3)],
            "source": {"name": "independent synthetic steady-flow audit; not bathymetric survey",
                       "horizontal_crs": "LOCAL_CARTESIAN_METRES", "origin_projected_m": [0., 0.],
                       "vertical_datum": "synthetic model sea zero"}}


def configuration(*, shear=False, nonzero=True, **changes):
    """Independent traction-coordinate reference, genuinely nonzero flow."""
    c = {"seabed_grid": explicit_bed(), "nodes": 8, "wet_weight_n_m": 4., "ea_n": 10000.,
         "ei_n_m2": 0., "diameter_m": .02, "mass_kg_m": 1.2, "water_density_kg_m3": 1025.,
         "added_mass_coefficient": .7, "drag_coefficient": 1.2, "initial_suspended_material_m": ORIGIN,
         "ship_speed_m_s": 0., "payout_m_s": 0., "current_x_m_s": .32, "current_y_m_s": -.27,
         "heading_deg": 90., "seabed_friction": .6, "damping_ratio": 0.,
         "duration_s": .04, "dt_s": .02, "internal_dt_s": .002, "solver_iterations": 32,
         "material_segments": [
             {"id": "lower", "start_m": 0., "end_m": 40.5, "wet_weight_n_m": 2.5,
              "ea_n": 8000., "mass_kg_m": .9, "diameter_m": .018, "drag_coefficient": .8, "ei_n_m2": 0.},
             {"id": "middle", "start_m": 40.5, "end_m": 50.75, "wet_weight_n_m": 5.,
              "ea_n": 14000., "mass_kg_m": 1.4, "diameter_m": .025, "drag_coefficient": 1.5, "ei_n_m2": 0.},
             {"id": "upper", "start_m": 50.75, "end_m": 120., "wet_weight_n_m": 3.,
              "ea_n": 45000., "mass_kg_m": 1.2, "diameter_m": .02, "drag_coefficient": 1.1, "ei_n_m2": 0.}],
         "inline_bodies": [
             {"id": "anchor", "material_m": 37., "mass_kg": 1., "wet_weight_n": 3.,
              "length_m": 0., "drag_area_m2": .005, "drag_coefficient": 1.2},
             {"id": "buoyant-off-node", "material_m": 39.25, "mass_kg": .4, "wet_weight_n": -25.,
              "length_m": 0., "drag_area_m2": .04, "drag_coefficient": 1.1},
             {"id": "upper-point", "material_m": 58.75, "mass_kg": .8, "wet_weight_n": 4.,
              "length_m": 0., "drag_area_m2": .02, "drag_coefficient": .9},
             {"id": "vessel", "material_m": 64., "mass_kg": 2., "wet_weight_n": 1.,
              "length_m": 0., "drag_area_m2": .015, "drag_coefficient": 1.4}],
         "checkpoint_times_s": [0.]}
    if shear:
        # Main values intentionally differ: the depth table replaces uniform U.
        c["current_x_m_s"], c["current_y_m_s"] = 1.7, -1.3
        c["current_profile"] = [{"depth_m": 0., "x_m_s": .2, "y_m_s": -.15},
                                {"depth_m": 40., "x_m_s": .55, "y_m_s": .25}]
    c.update(changes)
    loading = interval_expected(c, REST)
    bottom = np.array([150., 10., 35.])
    traction = np.tile(bottom, (len(REST), 1))
    for i in range(len(REST)-2, -1, -1):
        traction[i] = traction[i+1]+[0., 0., loading["wet"][i+1]]
    vessel = np.array([3., -4., -2.])

    def geometry(f):
        forces = np.vstack([f.reshape(-1, 3), bottom])
        norms = np.linalg.norm(forces, axis=1)
        vectors = (REST+norms*loading["compliance"])[:, None]*forces/norms[:, None]
        return np.vstack([vessel, vessel-np.cumsum(vectors, axis=0)])

    def recurrence(f):
        forces = np.vstack([f.reshape(-1, 3), bottom])
        p = geometry(f)
        drag = drag_expected(c, p, loading)
        loads = drag["cable"]+drag["body"]
        loads[:, 2] -= loading["wet"]
        return (forces[:-1]-forces[1:]+loads[1:-1]).ravel()

    solved = root(recurrence, traction[:-1].ravel(), tol=1e-11)
    assert solved.success and np.max(np.abs(recurrence(solved.x))) < 1e-9, solved.message
    positions = geometry(solved.x)
    direct = forces_expected(c, positions, REST, loading)
    assert np.max(np.linalg.norm(direct["force"][1:-1], axis=1)) < 1e-8
    if nonzero:
        assert np.max(np.linalg.norm(direct["cable"], axis=1)) > 1.
        assert np.max(np.abs(direct["cable"][:, 2])) > .1, "fixture must exercise vertical cable drag"
    return c, positions, loading, direct


def checksum(document):
    def normalize(value):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value
    payload = {key: value for key, value in document.items() if key != "checksum_sha256"}
    return hashlib.sha256(json.dumps(normalize(payload), sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def raw_current(c, positions, *, tight=True):
    """Declare raw history explicitly, independently of canonicalizer subject."""
    c = deepcopy(c)
    c["initial_equilibrium"] = {
        "schema": RAW_CURRENT, "vessel_position_m": positions[0].tolist(),
        "anchor_position_m": positions[-1].tolist(), "rest_lengths_m": REST.tolist(),
        "initial_positions_m": positions.tolist(),
        "initial_fluid": {"schema": "oceanroute.initial-fluid.v1",
            "operator": "node-secant-normal-cable-and-isotropic-body-drag-v1",
            "water_density_kg_m3": c["water_density_kg_m3"],
            "current_m_s": [c["current_x_m_s"], c["current_y_m_s"], 0.],
            "current_profile": deepcopy(c.get("current_profile")),
            "depth_reference": "max(-model_z_m,0)", "profile_extrapolation": "hold_endpoints"}}
    if tight:
        c["initial_equilibrium"]["solver"] = {"force_tolerance_n": 1e-6,
            "relative_force_tolerance": 1e-9, "contact_tolerance_m": 1e-8}
    return c


@pytest.fixture(scope="module", params=[False, True], ids=["uniform", "depth-shear"])
def actual_initial(request):
    c, reference, loading, direct = configuration(shear=request.param)
    c = raw_current(c, reference)
    return c, reference, loading, direct, simulate_lay({}, c)


def test_actual_flow_material_and_endpoint_reactions_match_independent_force_operator(actual_initial):
    c, reference, loading, _, result = actual_initial
    proof = result["initialization"]
    snap = proof["initial_snapshot"]
    p = np.asarray(snap["positions"])
    direct = forces_expected(c, p, REST, loading)
    hydro = proof["fluid_loading"]
    assert proof["schema"] == PROOF_CURRENT
    assert result["model"] == MODEL_CURRENT and result["checkpoint"]["schema_version"] == 4
    assert proof["verification"]["accepted"] is True and proof["solver"]["accepted"] is True
    assert snap["node_material_m"] == pytest.approx(loading["q"], abs=1e-8)
    assert snap["node_wet_weight_n"] == pytest.approx(loading["wet"], abs=1e-11)
    assert snap["node_dry_mass_kg"] == pytest.approx(loading["dry"], abs=1e-11)
    assert snap["node_mass_kg"] == pytest.approx(loading["mass"], abs=1e-11)
    assert snap["segment_ea_n"] == pytest.approx(loading["ea"], rel=1e-12)
    assert hydro["node_cable_drag_factor"] == pytest.approx(loading["kc"], abs=1e-11)
    assert hydro["node_body_drag_factor"] == pytest.approx(loading["kb"], abs=1e-11)
    assert hydro["node_fluid_velocity_m_s"] == pytest.approx(direct["fluid"], abs=1e-12)
    assert hydro["node_tangent"] == pytest.approx(direct["tangent"], abs=1e-12)
    assert hydro["node_cable_drag_n"] == pytest.approx(direct["cable"], abs=1e-10)
    assert hydro["node_body_drag_n"] == pytest.approx(direct["body"], abs=1e-10)
    external = direct["cable"]+direct["body"]
    external[:, 2] -= loading["wet"]
    assert hydro["node_external_force_n"] == pytest.approx(external, abs=1e-10)
    assert np.max(np.linalg.norm(direct["force"][1:-1], axis=1)) < 1e-6
    assert snap["node_boundary_force_n"] == pytest.approx(np.vstack(
        [-direct["force"][0], np.zeros((6, 3)), -direct["force"][-1]]), abs=1e-8)
    assert snap["segment_tension_n"] == pytest.approx(direct["tension"], abs=1e-8)
    assert np.max(np.linalg.norm(p-reference, axis=1)) < 1e-6
    assert result["frames"][0]["touchdown"] is None
    assert result["frames"][0]["bottom_tension_n"] is None
    assert proof["initial_paid_out_m"] == result["frames"][0]["paid_out_m"] == 0
    assert sum(result["checkpoint"]["state"]["rest_lengths_m"]) == pytest.approx(27., abs=1e-12)


def test_actual_point_drag_uses_each_node_depth_not_display_point_depth(actual_initial):
    c, _, loading, _, result = actual_initial
    p = np.asarray(result["initialization"]["initial_snapshot"]["positions"])
    expected = drag_expected(c, p, loading)["body"]
    at_display = np.zeros_like(p)
    for body in c["inline_bodies"]:
        alpha = loading["alpha"][body["id"]]
        u = velocity_expected(c, np.asarray([alpha@p]))[0]
        at_display += alpha[:, None]*(c["water_density_kg_m3"]/2*body["drag_coefficient"]*
                        body["drag_area_m2"]*np.linalg.norm(u)*u)
    if "current_profile" in c:
        assert np.max(np.abs(expected-at_display)) > 1e-4
    else:
        assert np.max(np.abs(expected-at_display)) < 1e-12
    assert result["initialization"]["fluid_loading"]["node_body_drag_n"] == pytest.approx(expected, abs=1e-10)


@pytest.mark.parametrize("h", [.008, .004, .002])
def test_nonzero_flow_actual_multistep_fixedpoint_not_cold_settling(h):
    c, reference, loading, _ = configuration(shear=True, duration_s=.08, internal_dt_s=h)
    c = raw_current(c, reference)
    result = simulate_lay({}, c)
    for frame in result["frames"]:
        assert np.max(np.linalg.norm(np.asarray(frame["nodes"])-reference, axis=1)) < 1e-7
        assert np.max(np.linalg.norm(np.asarray(frame["node_velocity_m_s"]), axis=1)) < 1e-6
        assert frame["paid_out_m"] == 0
        direct = forces_expected(c, np.asarray(frame["nodes"]), REST, loading)
        assert np.max(np.linalg.norm(direct["force"][1:-1], axis=1)) < 1e-6
    assert result["checkpoint"]["numerical"]["scheme"] == "implicit-compliant-material-nodes-current-equilibrium-prestress-v5"


def test_bad_seed_requires_real_flow_optimization_not_a_seed_only_shortcut():
    c, reference, loading, _ = configuration(shear=True, duration_s=.02)
    c = raw_current(c, reference)
    seed = reference.copy()
    seed[1:-1, 1] += [.03, -.025, .018, -.02, .012, -.015]
    seed[1:-1, 2] += [.02, -.015, .025, -.012, .018, -.02]
    c["initial_equilibrium"]["initial_positions_m"] = seed.tolist()
    initial_bad = forces_expected(c, seed, REST, loading)
    assert np.max(np.linalg.norm(initial_bad["force"][1:-1], axis=1)) > 100.
    actual = resolve_initial_equilibrium({}, c)
    p = np.asarray(actual["positions"])
    direct = forces_expected(c, p, REST, loading)
    assert actual["solver"]["function_evaluations"] > 1
    assert np.max(np.linalg.norm(direct["force"][1:-1], axis=1)) < 1e-6
    assert np.max(np.linalg.norm(p-reference, axis=1)) < 1e-5
    assert np.max(np.linalg.norm(p-seed, axis=1)) > .02


def test_real_motion_feed_is_not_frozen_and_initial_inventory_is_not_repaid():
    c, reference, _, _ = configuration(shear=True, duration_s=.08, ship_speed_m_s=.2, payout_m_s=.25)
    c = raw_current(c, reference)
    result = simulate_lay({}, c)
    p = np.asarray(result["frames"][-1]["nodes"])
    rest = result["checkpoint"]["state"]["rest_lengths_m"]
    loading = interval_expected(c, rest)
    assert p[-1] == pytest.approx(reference[-1], abs=1e-12)
    assert np.max(np.linalg.norm(p[1:-1]-reference[1:-1], axis=1)) > 1e-6
    assert result["checkpoint"]["state"]["paid_out_m"] == pytest.approx(.02, abs=1e-13)
    assert sum(rest) == pytest.approx(27.02, abs=1e-11)
    assert result["frames"][-1]["node_material_m"][0] == pytest.approx(64.02, abs=1e-11)
    assert result["frames"][-1]["node_wet_weight_n"] == pytest.approx(loading["wet"], abs=1e-10)
    assert result["frames"][-1]["node_mass_kg"] == pytest.approx(loading["mass"], abs=1e-10)


def test_json_resume_future_current_override_keeps_historical_flow_proof(actual_initial):
    c, _, _, _, first = actual_initial
    saved = json.loads(json.dumps(first["checkpoint"], allow_nan=False))
    read_checkpoint(saved)
    history = deepcopy(saved["state"]["initialization_provenance"])
    altered = simulate_lay({}, {"resume_state": saved, "duration_s": .04,
        "current_x_m_s": -.2, "current_y_m_s": .4, "current_profile": [
            {"depth_m": 0., "x_m_s": -.2, "y_m_s": .4},
            {"depth_m": 40., "x_m_s": .1, "y_m_s": -.1}]})
    assert altered["initialization"] == history
    assert altered["checkpoint"]["state"]["initialization_provenance"] == history
    assert altered["checkpoint"]["config"]["initial_equilibrium"]["initial_fluid"] == history["fluid_loading"]["initial_fluid"]
    assert altered["solver"]["initialization_work"]["static_optimizer_run_this_call"] is False
    assert altered["solver"]["initialization_work"]["estimated_work_units_this_run"] == 0
    assert altered["solver"]["initialization_work"]["checkpoint_proof_verification_work_this_run"] > 0
    read_checkpoint(json.loads(json.dumps(altered["checkpoint"], allow_nan=False)))
    assert np.max(np.linalg.norm(np.asarray(altered["frames"][-1]["nodes"])-
                                 np.asarray(first["frames"][-1]["nodes"]), axis=1)) > 1e-6


def contact_configuration(*, bilinear=False):
    c, _, loading, _ = configuration(shear=True, duration_s=.08)
    traction = np.tile([150., 10., 0.], (7, 1))
    traction[3:5, 2] = [4., -4.]
    for i in range(2, -1, -1):
        traction[i, 2] = traction[i+1, 2]+loading["wet"][i+1]
    for i in range(5, 7):
        traction[i, 2] = traction[i-1, 2]-loading["wet"][i]

    def geometry(f):
        f = f.reshape(7, 3)
        tension = np.linalg.norm(f, axis=1)
        vectors = (REST+tension*loading["compliance"])[:, None]*f/tension[:, None]
        return np.vstack([[3., -4., -2.], [3., -4., -2.]-np.cumsum(vectors, axis=0)])

    a, b, g = (.01, .015, .0003) if bilinear else (0., 0., 0.)

    def recurrence(values):
        f, normal_force = values[:21].reshape(7, 3), values[21]
        p = geometry(f)
        hydro = drag_expected(c, p, loading)
        external = hydro["cable"]+hydro["body"]
        external[:, 2] -= loading["wet"]
        force = f[:-1]-f[1:]+external[1:-1]
        normal = np.array([-a-g*p[4, 1], -b-g*p[4, 0], 1.])
        normal /= np.linalg.norm(normal)
        force[3] += normal_force*normal
        return np.r_[force.ravel(), f[3, 2]-4., f[4, 2]+4.,
                     f[-1, 0]-150., f[-1, 1]-10.]

    solved = root(recurrence, np.r_[traction.ravel(), 4.5], tol=1e-11)
    assert solved.success and np.max(np.abs(recurrence(solved.x))) < 1e-9, solved.message
    p = geometry(solved.x[:21])
    bed = float(p[4, 2]-a*p[4, 0]-b*p[4, 1]-g*p[4, 0]*p[4, 1])
    direct = forces_expected(c, p, REST, loading)
    normal = np.zeros(8)
    normal[4] = solved.x[21]
    floor = bed+a*p[:, 0]+b*p[:, 1]+g*p[:, 0]*p[:, 1]
    assert normal[4] > 4. and abs(p[4, 2]-floor[4]) < 1e-12
    assert np.min(p[:, 2]-floor) > -1e-12
    assert p[-1, 2]-floor[-1] > .4, "fixed anchor must be genuinely off-bed"
    axis = c["seabed_grid"]["x_m"]
    c["seabed_grid"]["z_m"] = [[bed+a*x+b*y+g*x*y for x in axis] for y in axis]
    return raw_current(c, p), p, loading, normal


def test_nonzero_shear_contact_support_balances_actual_drag_and_is_not_double_kicked():
    c, expected, loading, normal = contact_configuration()
    result = simulate_lay({}, c)
    snap = result["initialization"]["initial_snapshot"]
    p = np.asarray(snap["positions"])
    direct = forces_expected(c, p, REST, loading)
    residual = direct["force"].copy()
    residual[:, 2] += normal
    assert snap["node_contact_normal_force_n"] == pytest.approx(normal, abs=1e-7)
    assert np.max(np.linalg.norm(residual[1:-1], axis=1)) < 1e-6
    assert snap["node_boundary_force_n"] == pytest.approx(np.vstack(
        [-direct["force"][0], np.zeros((6, 3)), -direct["force"][-1]]), abs=1e-7)
    assert result["frames"][0]["touchdown"] == pytest.approx(p[4], abs=1e-8)
    for frame in result["frames"]:
        assert np.max(np.linalg.norm(np.asarray(frame["nodes"])-expected, axis=1)) < 1e-7
        assert np.max(np.linalg.norm(np.asarray(frame["node_velocity_m_s"]), axis=1)) < 1e-6
    assert result["checkpoint"]["state"]["anchor"] == pytest.approx(expected[-1], abs=1e-12)


def test_actual_xy_bilinear_cold_seed_exhaustion_is_rejected_without_balanced_output():
    c, reference, _, _ = configuration(nodes=18, duration_s=.02)
    c["current_x_m_s"], c["current_y_m_s"] = .1, -.08
    c = raw_current(c, reference, tight=False)
    axis = [-100., 0., 100.]
    c["seabed_grid"]["z_m"] = [[-40+.1*x+.05*y+.0003*x*y for x in axis] for y in axis]
    raw = c["initial_equilibrium"]
    raw.pop("initial_positions_m")
    raw.pop("rest_lengths_m")
    raw.update({"vessel_position_m": [0., 0., 0.],
                "anchor_position_m": [-60., -10., -46.32], "natural_length_m": 80.})
    with pytest.raises(ValueError, match="(?i)BUDGET_EXHAUSTED"):
        simulate_lay({}, c)


@pytest.mark.parametrize("bad_seed", [False, True], ids=["independent-traction-reference", "genuinely-perturbed-seed"])
def test_actual_xy_bilinear_current_solution_obeys_independent_normals_forces_and_full_chords(bad_seed):
    c, expected, _, _ = contact_configuration(bilinear=True)
    if bad_seed:
        seed = expected.copy()
        seed[1:-1, 1] += [.03, -.025, .018, -.02, .012, -.015]
        seed[1:-1, 2] += [.02, .015, .025, .012, .018, .02]
        c["initial_equilibrium"]["initial_positions_m"] = seed.tolist()
    result = simulate_lay({}, c)
    snap = result["initialization"]["initial_snapshot"]
    p = np.asarray(snap["positions"])
    rest = snap["rest_lengths_m"]
    loading = interval_expected(c, rest)
    direct = forces_expected(c, p, rest, loading)
    # Coefficients are the explicitly constructed global bilinear polynomial,
    # independently of the grid sampler and solver's normals.
    bed = c["seabed_grid"]["z_m"][1][1]
    a, b, g = .01, .015, .0003
    floor = bed+a*p[:, 0]+b*p[:, 1]+g*p[:, 0]*p[:, 1]
    normals = np.column_stack([-a-g*p[:, 1], -b-g*p[:, 0], np.ones(len(p))])
    normals /= np.linalg.norm(normals, axis=1)[:, None]
    support = np.maximum(-np.sum(direct["force"]*normals, axis=1), 0)*(p[:, 2]-floor <= 1e-8)
    support[[0, -1]] = 0
    assert np.count_nonzero(support) == 1
    assert snap["node_contact_normal_force_n"] == pytest.approx(support, abs=1e-7)
    assert result["frames"][0]["node_seabed_normal"] == pytest.approx(normals, abs=1e-12)
    residual = direct["force"]+support[:, None]*normals
    assert np.max(np.linalg.norm(residual[1:-1], axis=1)) < 1e-6
    assert snap["node_boundary_force_n"] == pytest.approx(np.vstack(
        [-direct["force"][0], np.zeros((6, 3)), -direct["force"][-1]]), abs=1e-7)
    for a, b in zip(p[:-1], p[1:]):
        dx, dy, dz = b-a
        qa = -g*dx*dy
        qb = dz-.01*dx-.015*dy-g*(a[0]*dy+a[1]*dx)
        qc = a[2]-bed-.01*a[0]-.015*a[1]-g*a[0]*a[1]
        values = [qc, qa+qb+qc]
        if qa > 0 and 0 < -qb/(2*qa) < 1:
            t = -qb/(2*qa)
            values.append(qa*t*t+qb*t+qc)
        assert min(values) >= -1e-8
    assert sum(rest) == pytest.approx(27., abs=1e-12)
    assert result["frames"][0]["paid_out_m"] == 0
    assert np.max(np.linalg.norm(p-expected, axis=1)) < 1e-5
    assert np.max(np.linalg.norm(np.asarray(result["frames"][-1]["nodes"])-p, axis=1)) < 1e-7
    if bad_seed:
        assert result["initialization"]["solver"]["function_evaluations"] > 1


@pytest.mark.parametrize("mutation", ["history_flow", "fluid_vector", "body_drag", "cable_drag",
    "drag_coefficient", "node_external", "fluid_digest", "raw_mismatch", "downgrade", "current_mass"])
def test_recomputed_checksum_cannot_authorize_false_flow_loading_or_scheme(actual_initial, mutation):
    _, _, _, _, result = actual_initial
    cp = deepcopy(result["checkpoint"])
    proof = cp["state"]["initialization_provenance"]
    if mutation == "history_flow":
        cp["config"]["initial_equilibrium"]["initial_fluid"]["current_m_s"][0] += .1
    elif mutation == "fluid_vector":
        proof["fluid_loading"]["node_fluid_velocity_m_s"][2][0] += .1
    elif mutation == "body_drag":
        proof["fluid_loading"]["node_body_drag_n"][6][0] += .1
    elif mutation == "cable_drag":
        proof["fluid_loading"]["node_cable_drag_n"][2][2] += .1
    elif mutation == "drag_coefficient":
        cp["config"]["material_segments"][1]["drag_coefficient"] += .1
    elif mutation == "node_external":
        proof["fluid_loading"]["node_external_force_n"][2][0] += .1
    elif mutation == "fluid_digest":
        proof["fluid_loading"]["initial_fluid_sha256"] = "0"*64
    elif mutation == "raw_mismatch":
        cp["config"]["initial_equilibrium"]["initial_fluid"]["water_density_kg_m3"] += 1.
    elif mutation == "downgrade":
        cp["schema_version"] = 3
        cp["model"] = "material-lumped-mass-xpbd-cable-lay-v4"
        cp["numerical"]["scheme"] = "implicit-compliant-material-nodes-equilibrium-prestress-v4"
    elif mutation == "current_mass":
        cp["state"]["node_mass_kg"][2] += 1.
    cp["checksum_sha256"] = checksum(cp)
    with pytest.raises(ValueError):
        read_checkpoint(cp)


@pytest.mark.parametrize("case", ["nonzero-drag", "co-moving", "zero-drag"])
def test_directional_predictor_is_positive_block_metric_with_independent_matrix_expected(case):
    # The helper is the subject, never the oracle. All material and hydrodynamic
    # arrays here are declared directly and the expected solve is a dense 3x3.
    from oceanroute.current_dynamics import current_predictor
    p = np.array([[0., 0., -1.], [-1., .2, -1.3], [-2., .5, -1.5],
                  [-3.2, .7, -1.9], [-4.1, 1., -2.1], [-5., 1.4, -2.7]])
    rest = np.linalg.norm(np.diff(p, axis=0), axis=1)
    mass = np.array([1., 2., 3., 4., 2.5, 1.5])
    velocity = np.tile([.1, -.03, .02], (6, 1))
    fluid = np.tile([.5, -.25, 0.], (6, 1))
    coefficient = np.array([1., 2., 3., 1.5, .7, 2.])
    body_coefficient = np.array([.5, 0., 1., 0., .6, .1])
    weight = np.array([1., 3., 2., 1., 4., 1.])
    if case == "co-moving":
        velocity = fluid.copy()
        weight[:] = 0
    elif case == "zero-drag":
        coefficient[:] = 0
        body_coefficient[:] = 0
    secant = np.vstack([p[1]-p[0], (p[2:]-p[:-2])/2, p[-1]-p[-2]])
    tangent = secant/np.linalg.norm(secant, axis=1)[:, None]
    relative = fluid-velocity
    normal = relative-np.sum(relative*tangent, axis=1)[:, None]*tangent
    total_drag = (coefficient[:, None]*np.linalg.norm(normal, axis=1)[:, None]*normal+
                  body_coefficient[:, None]*np.linalg.norm(relative, axis=1)[:, None]*relative)
    local = {"mass": mass, "ea": np.full(5, 10000.), "weight": weight,
             "drag": coefficient, "body_drag": body_coefficient}
    loading = {"node_tangent": tangent, "node_current_m_s": fluid,
               "node_total_drag_force_n": total_drag}
    h = .017
    bed = np.full(6, -100.)
    normals = np.tile([0., 0., 1.], (6, 1))
    actual, inverse, _, _ = current_predictor(p, velocity, rest, local, loading, bed, normals, h)
    for j in range(1, 5):
        projection = np.eye(3)-np.outer(tangent[j], tangent[j])
        block = (coefficient[j]*np.linalg.norm(normal[j])*projection+
                 body_coefficient[j]*np.linalg.norm(relative[j])*np.eye(3))
        matrix = mass[j]*np.eye(3)+h*block
        assert np.linalg.eigvalsh(matrix).min() > 0
        expected = np.linalg.solve(matrix, mass[j]*velocity[j]+h*(block@fluid[j]-[0., 0., weight[j]]))
        assert actual[j] == pytest.approx(expected, abs=1e-14)
        assert inverse[j] == pytest.approx(np.linalg.inv(matrix), abs=1e-14)
        assert inverse[j] == pytest.approx(inverse[j].T, abs=1e-15)
    assert np.max(np.abs(inverse[[0, -1]])) == 0
    if case == "co-moving":
        assert actual[1:-1] == pytest.approx(velocity[1:-1], abs=1e-14)


def test_explicit_new_zero_flow_keeps_new_dispatch_but_matches_real_old_force_branch():
    c, p, _, _ = configuration(nonzero=False, current_x_m_s=0., current_y_m_s=0.)
    current = raw_current(c, p)
    legacy = deepcopy(current)
    legacy["initial_equilibrium"]["schema"] = "oceanroute.dynamic.initial-equilibrium.v1"
    legacy["initial_equilibrium"].pop("initial_fluid")
    new, old = simulate_lay({}, current), simulate_lay({}, legacy)
    assert new["model"] == MODEL_CURRENT and new["checkpoint"]["schema_version"] == 4
    assert old["model"] == "material-lumped-mass-xpbd-cable-lay-v4" and old["checkpoint"]["schema_version"] == 3
    for field in ("positions", "velocities", "rest_lengths_m", "node_mass_kg", "node_wet_weight_n", "segment_ea_n"):
        assert new["checkpoint"]["state"][field] == pytest.approx(np.asarray(old["checkpoint"]["state"][field]), abs=1e-9)


@pytest.mark.parametrize("index", [0, 1])
def test_real_frozen_old_v4_json_does_not_upgrade_from_future_nonzero_current(index):
    evidence = json.loads((Path(__file__).resolve().parents[1]/"resources/validation/development_0.7_legacy_checkpoint_inputs.json").read_text())
    assert evidence["source_wheel_sha256"] == "bc8e20bdf26656e6c5453d25fe385cad61238b48e30b5159471d72c2b8c35141"
    cp = evidence["cases"][index]["checkpoint"]
    assert cp["schema_version"] == 3 and cp["state"]["initialization_provenance"]["schema"].endswith(".v1")
    read_checkpoint(cp)
    result = simulate_lay({}, {"resume_state": cp, "duration_s": .02,
                              "current_x_m_s": .15, "current_y_m_s": -.1})
    assert result["model"] == "material-lumped-mass-xpbd-cable-lay-v4"
    assert result["checkpoint"]["schema_version"] == 3
    assert result["initialization"] == cp["state"]["initialization_provenance"]
    assert result["solver"]["initialization_work"]["static_optimizer_run_this_call"] is False


@pytest.mark.parametrize("reason", ["finite-body", "EI", "wave", "NoData", "fluid-mismatch"])
def test_unsupported_loading_unknown_bed_and_false_fluid_declaration_are_rejected(reason):
    c, p, _, _ = configuration()
    c = raw_current(c, p)
    if reason == "finite-body":
        c["inline_bodies"][1]["length_m"] = .2
    elif reason == "EI":
        c["material_segments"][1]["ei_n_m2"] = 1.
    elif reason == "wave":
        c["wave_kinematics"] = {"depth_m": 100., "time_origin_s": 0.,
            "spatial_origin_xy_m": [0., 0.], "components": [
                {"frequency_hz": .25, "amplitude_m": .01, "phase_deg": 0., "direction_deg": 90.}]}
    elif reason == "NoData":
        c["seabed_grid"]["z_m"][1][1] = None
    elif reason == "fluid-mismatch":
        c["initial_equilibrium"]["initial_fluid"]["current_m_s"][0] += .01
    with pytest.raises(ValueError):
        simulate_lay({}, c)


def test_consistently_resigned_new_history_drag_still_requires_original_physical_balance(actual_initial):
    c, _, loading, _, result = actual_initial
    cp = deepcopy(result["checkpoint"])
    raw = cp["config"]["initial_equilibrium"]
    history = cp["state"]["initialization_provenance"]
    fluid = raw["initial_fluid"]
    fluid["current_m_s"][1] += .1
    changed = deepcopy(c)
    changed["current_y_m_s"] += .1
    if fluid["current_profile"] is not None:
        for row in fluid["current_profile"]:
            row["y_m_s"] += .1
        changed["current_profile"] = deepcopy(fluid["current_profile"])
    p = np.asarray(history["initial_snapshot"]["positions"])
    drag = drag_expected(changed, p, loading)
    fields = history["fluid_loading"]
    fields["initial_fluid"] = deepcopy(fluid)
    fields["initial_fluid_sha256"] = checksum(fluid)
    fields["node_fluid_velocity_m_s"] = drag["fluid"].tolist()
    fields["node_cable_drag_n"] = drag["cable"].tolist()
    fields["node_body_drag_n"] = drag["body"].tolist()
    external = drag["cable"]+drag["body"]
    external[:, 2] -= loading["wet"]
    fields["node_external_force_n"] = external.tolist()
    history["request_sha256"] = checksum(raw)
    cp["checksum_sha256"] = checksum(cp)
    assert np.max(np.linalg.norm(forces_expected(changed, p, REST, loading)["force"][1:-1], axis=1)) > .1
    with pytest.raises(ValueError, match="(?i)force|fluid|loading|evidence"):
        read_checkpoint(cp)


@pytest.mark.parametrize("reason", ["work", "standalone", "batch", "response"])
def test_declared_work_and_actual_repeated_bytes_reject_before_optimizer(reason, monkeypatch):
    c, p, _, _ = configuration(duration_s=2. if reason in {"batch", "response"} else .02,
        internal_dt_s=.004, save_checkpoints=reason == "batch")
    c = raw_current(c, p)
    if reason == "work":
        c["initial_equilibrium"]["solver"]["max_work_units"] = 1
        expression = "(?i)work|budget|computation"
    else:
        length = {"standalone": 1010000, "batch": 80000, "response": 800000}[reason]
        c["inline_bodies"][0]["id"] = "x"*length
        expression = "(?i)volume|serialized|limit"
    import oceanroute.initial_equilibrium as initializer
    original = initializer.resolve_initial_equilibrium
    calls = []
    def observer(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(initializer, "resolve_initial_equilibrium", observer)
    with pytest.raises(ValueError, match=expression):
        simulate_lay({}, c)
    assert calls == [], "declared work/capacity failure must precede the actual optimizer"


def completed(client, identifier):
    deadline = time.monotonic()+30
    while time.monotonic() < deadline:
        response = client.get(f"/api/voyage/jobs/{identifier}")
        assert response.status_code == 200, response.text
        state = response.json()
        if state["status"] not in {"queued", "running", "cancelling"}:
            assert state["status"] == "completed", state
            return state
        time.sleep(.01)
    pytest.fail("actual current-initial-state job failed to complete")


def test_actual_http_prepare_dynamic_and_durable_owner_reopen_resume(tmp_path):
    c, p, _, _ = configuration(shear=True, payout_m_s=.25, duration_s=.08)
    c = raw_current(c, p)
    c.pop("checkpoint_times_s")
    policy = {"simulation": c, "duration_s": .08, "chunk_duration_s": .02,
              "max_total_work_units": 160000000, "adaptive_mesh": {"enabled": False}}
    whole = run_voyage({}, policy)
    assert whole["status"] == "completed"
    database = tmp_path/"actual-current-initial.sqlite3"
    with TestClient(create_app(ProjectStore(database))) as client:
        # Project is truly optional for explicit local material boundaries.
        response = client.post("/api/simulation/prepare-equilibrium-initial", json={"config": c})
        assert response.status_code == 200, response.text
        assert response.json()["provenance"]["schema"] == PROOF_CURRENT
        response = client.post("/api/simulation/dynamic", json={"project": {}, "config": c})
        assert response.status_code == 200, response.text
        assert response.json()["checkpoint"]["schema_version"] == 4
        response = client.post("/api/voyage/jobs", json={"project": {}, "config": {**policy, "duration_s": .04}})
        assert response.status_code == 200, response.text
        parent = response.json()["id"]
        completed(client, parent)
        saved = client.get(f"/api/voyage/jobs/{parent}/checkpoint").json()
        read_voyage_checkpoint(saved)
        assert saved["physical_checkpoint"]["schema_version"] == 4
    with TestClient(create_app(ProjectStore(database))) as client:
        assert client.get(f"/api/voyage/jobs/{parent}/checkpoint").json() == saved
        response = client.post(f"/api/voyage/jobs/{parent}/resume", json={"duration_s": .04})
        assert response.status_code == 200, response.text
        child = response.json()["id"]
        assert completed(client, child)["parent_job_id"] == parent
        actual = client.get(f"/api/voyage/jobs/{child}/result").json()
        for field in ("positions", "velocities", "rest_lengths_m", "node_material_m",
                      "node_mass_kg", "node_wet_weight_n", "segment_ea_n"):
            assert actual["checkpoint"]["physical_checkpoint"]["state"][field] == pytest.approx(
                np.asarray(whole["checkpoint"]["physical_checkpoint"]["state"][field]), abs=1e-9)
        history = saved["physical_checkpoint"]["state"]["initialization_provenance"]
        assert actual["checkpoint"]["physical_checkpoint"]["state"]["initialization_provenance"] == history
        assert actual["summary"]["paid_out_m"] == pytest.approx(.02, abs=1e-13)
        assert all(row["solver"]["initialization_work"]["static_optimizer_run_this_call"] is False
                   for row in actual["chunks"])
        read_voyage_checkpoint(json.loads(json.dumps(actual["checkpoint"], allow_nan=False)))


def test_actual_hydrodynamic_directional_derivative_has_secant_and_shear_terms():
    from oceanroute.hydrodynamics import hydrodynamic_loads
    c, p, expected, _ = configuration(shear=True)
    c = raw_current(c, p)
    local = {"drag": expected["kc"], "body_drag": expected["kb"]}
    fluid = c["initial_equilibrium"]["initial_fluid"]
    velocity = np.tile([.02, -.015, .005], (8, 1))
    direction = np.zeros_like(p)
    direction[2] = [.3, -.2, .1]
    direction[3] = [-.1, .25, -.15]
    secant = np.vstack([p[1]-p[0], (p[2:]-p[:-2])/2, p[-1]-p[-2]])
    d_secant = np.vstack([direction[1]-direction[0],
        (direction[2:]-direction[:-2])/2, direction[-1]-direction[-2]])
    tangent = secant/np.linalg.norm(secant, axis=1)[:, None]
    projection = np.eye(3)[None, :, :]-tangent[:, :, None]*tangent[:, None, :]
    d_tangent = np.einsum("nij,nj->ni", projection, d_secant)/np.linalg.norm(secant, axis=1)[:, None]
    relative = velocity_expected(c, p)-velocity
    slope = np.array([(.55-.2)/40, (.25+.15)/40, 0.])
    d_fluid = -direction[:, 2, None]*slope
    normal = np.einsum("nij,nj->ni", projection, relative)
    d_normal = (np.einsum("nij,nj->ni", projection, d_fluid)-
        d_tangent*np.sum(tangent*relative, axis=1)[:, None]-
        tangent*np.sum(d_tangent*relative, axis=1)[:, None])
    norm = np.linalg.norm(normal, axis=1)
    expected_cable = expected["kc"][:, None]*(norm[:, None]*d_normal+
        normal*(np.sum(normal*d_normal, axis=1)/norm)[:, None])
    speed = np.linalg.norm(relative, axis=1)
    expected_body = expected["kb"][:, None]*(speed[:, None]*d_fluid+
        relative*(np.sum(relative*d_fluid, axis=1)/speed)[:, None])
    for step in [1e-5, 1e-6]:
        plus = hydrodynamic_loads(p+step*direction, velocity, local, fluid)
        minus = hydrodynamic_loads(p-step*direction, velocity, local, fluid)
        assert (plus["node_cable_drag_force_n"]-minus["node_cable_drag_force_n"])/(2*step) == pytest.approx(expected_cable, abs=1e-7)
        assert (plus["node_body_drag_force_n"]-minus["node_body_drag_force_n"])/(2*step) == pytest.approx(expected_body, abs=1e-7)
    actual = hydrodynamic_loads(p, velocity, local, fluid)
    direct = drag_expected(c, p, expected, velocity)
    assert actual["node_cable_drag_force_n"] == pytest.approx(direct["cable"], abs=1e-10)
    assert actual["node_body_drag_force_n"] == pytest.approx(direct["body"], abs=1e-10)


def test_shear_operator_holds_end_values_and_zero_relative_flow_is_drag_free():
    from oceanroute.hydrodynamics import hydrodynamic_loads
    c, p, expected, _ = configuration(shear=True)
    c = raw_current(c, p)
    p = p.copy()
    p[0, 2], p[-1, 2] = 1., -50.
    local = {"drag": expected["kc"], "body_drag": expected["kb"]}
    fluid = c["initial_equilibrium"]["initial_fluid"]
    velocity = velocity_expected(c, p)
    result = hydrodynamic_loads(p, velocity, local, fluid)
    assert result["node_current_m_s"][0] == pytest.approx([.2, -.15, 0.], abs=1e-15)
    assert result["node_current_m_s"][-1] == pytest.approx([.55, .25, 0.], abs=1e-15)
    assert np.max(np.abs(result["node_total_drag_force_n"])) < 1e-14


def test_node_clear_force_balanced_seed_still_rejects_real_mid_chord_bed_intersection():
    c, p, _, _ = configuration(shear=True)
    c = raw_current(c, p)
    middle = (p[0]+p[1])/2
    ridge = float(middle[2]+.2)
    xs = [-100., float(middle[0]-.1), float(middle[0]), float(middle[0]+.1), 100.]
    c["seabed_grid"]["x_m"] = xs
    c["seabed_grid"]["z_m"] = [[-100., -100., ridge, -100., -100.] for _ in range(3)]
    for point in p:
        floor = float(np.interp(point[0], xs, c["seabed_grid"]["z_m"][0]))
        assert point[2]-floor > 10., "all seed nodes deliberately clear the known bed"
    assert middle[2]-ridge == pytest.approx(-.2, abs=1e-12)
    with pytest.raises(ValueError, match="(?i)chord|segment|bed|accepted"):
        resolve_initial_equilibrium({}, c)


def test_direct_prepare_complete_proof_capacity_fails_before_current_core(monkeypatch):
    c, p, _, _ = configuration(shear=True)
    c = raw_current(c, p)
    c["inline_bodies"][0]["id"] = "x"*1010000
    import oceanroute.current_equilibrium as module
    original = module.solve_current_equilibrium
    calls = []
    def observer(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "solve_current_equilibrium", observer)
    with pytest.raises(ValueError, match="(?i)2 MB|serialized|volume|capacity|proof"):
        resolve_initial_equilibrium({}, c)
    assert calls == [], "standalone prepare must not defer oversized proof rejection until after optimization"


def test_actual_bad_seed_finite_difference_counter_is_enforced_not_only_preflight(monkeypatch):
    c, p, _, _ = configuration(shear=True)
    c = raw_current(c, p)
    seed = p.copy()
    seed[1:-1, 1] += [.03, -.025, .018, -.02, .012, -.015]
    c["initial_equilibrium"]["initial_positions_m"] = seed.tolist()
    c["initial_equilibrium"]["solver"]["max_function_evaluations"] = 25
    import oceanroute.current_equilibrium as module
    original = module.solve_current_equilibrium
    attempted = []
    def observer(*args, **kwargs):
        result = original(*args, **kwargs)
        attempted.append(result)
        return result
    monkeypatch.setattr(module, "solve_current_equilibrium", observer)
    with pytest.raises(ValueError, match="(?i)BUDGET_EXHAUSTED"):
        resolve_initial_equilibrium({}, c)
    assert len(attempted) == 1
    result = attempted[0]
    assert result["accepted"] is False
    assert result["solver"]["finite_difference_evaluations"] > 0
    assert result["solver"]["function_evaluations"] <= 25
    assert "COMPUTATION_BUDGET_EXHAUSTED" in result["solver"]["rejection_codes"]


@pytest.mark.parametrize("shear", [False, True])
def test_actual_horizontal_rotation_covariance_includes_drag_and_signed_points(shear):
    c, p, expected, _ = configuration(shear=shear)
    rotation = np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
    reference = p@rotation.T
    c["current_x_m_s"], c["current_y_m_s"] = -c["current_y_m_s"], c["current_x_m_s"]
    for row in c.get("current_profile", []):
        row["x_m_s"], row["y_m_s"] = -row["y_m_s"], row["x_m_s"]
    c = raw_current(c, reference)
    result = simulate_lay({}, c)
    snap = result["initialization"]["initial_snapshot"]
    actual = np.asarray(snap["positions"])
    direct = forces_expected(c, actual, REST, expected)
    assert np.max(np.linalg.norm(actual-reference, axis=1)) < 1e-7
    assert np.max(np.linalg.norm(direct["force"][1:-1], axis=1)) < 1e-6
    assert result["initialization"]["fluid_loading"]["node_cable_drag_n"] == pytest.approx(direct["cable"], abs=1e-10)
    assert result["initialization"]["fluid_loading"]["node_body_drag_n"] == pytest.approx(direct["body"], abs=1e-10)
