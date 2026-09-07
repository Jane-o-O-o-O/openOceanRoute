"""Actual current-startup refinement against an independent continuous cable.

Common applied anchor traction, natural inventory and declarations are held
fixed. Each discrete mesh supplies its own fixed endpoints: this is a traction
refinement experiment, not a common fixed-end BVP or field validation. No
production material/hydrodynamic helper supplies a reference or expected load.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import math
from pathlib import Path
import sys
import time

import numpy as np
from scipy.integrate import solve_ivp
from scipy.optimize import root

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from oceanroute.simulation import simulate_lay

ORIGIN, LENGTH, RHO, G = 37., 24., 1025., 9.80665
CURRENT = np.array([.32, -.27, 0.])
ANCHOR = np.array([0., 0., -30.])
BOTTOM = np.array([150., 10., 35.])
BODY_AREA, BODY_CD = .02, 1.2
SOURCE_FILES = ["oceanroute/simulation.py", "oceanroute/hydrodynamics.py",
                "oceanroute/current_equilibrium.py", "oceanroute/current_dynamics.py",
                "oceanroute/initial_equilibrium.py", "oceanroute/checkpoints.py"]


def declarations(boundary, point_station, point_weight):
    return {"origin": ORIGIN, "length": LENGTH, "rho": RHO,
            "current": CURRENT.copy(), "anchor": ANCHOR.copy(), "bottom": BOTTOM.copy(),
            "rows": [{"start_m": 0., "end_m": ORIGIN+boundary,
                      "wet_weight_n_m": 4., "ea_n": 10000., "mass_kg_m": 1.2,
                      "diameter_m": .02, "drag_coefficient": 1.1, "ei_n_m2": 0.},
                     {"start_m": ORIGIN+boundary, "end_m": ORIGIN+LENGTH+1.,
                      "wet_weight_n_m": 7., "ea_n": 24000., "mass_kg_m": 1.8,
                      "diameter_m": .025, "drag_coefficient": 1.4, "ei_n_m2": 0.}],
            "body": {"id": "signed-off-node-point", "material_m": ORIGIN+point_station,
                     "mass_kg": 10., "wet_weight_n": point_weight, "length_m": 0.,
                     "drag_area_m2": BODY_AREA, "drag_coefficient": BODY_CD}}


def relative_coordinate(absolute, origin):
    return float(Decimal.from_float(float(absolute))-Decimal.from_float(float(origin)))


def continuous_reference(declared, *, relative_tolerance=1e-11, absolute_tolerance=1e-12):
    """Piecewise extensible still-geometry ODE with a true signed point jump.

    s grows oldest->ship, f is the traction in the same direction. Endpoint
    applied traction equals f(0); distributed drag is per natural length.
    """
    origin, length, fluid, rho = (declared[k] for k in ("origin", "length", "current", "rho"))
    body = declared["body"]
    point = relative_coordinate(body["material_m"], origin)
    boundaries = [relative_coordinate(row["end_m"], origin) for row in declared["rows"][:-1]]
    cuts = sorted({0., length, point, *boundaries})
    state = np.r_[declared["anchor"], declared["bottom"]].astype(float)
    evaluations, jumps = 0, []
    point_drag = rho/2*body["drag_coefficient"]*body["drag_area_m2"]*np.linalg.norm(fluid)*fluid
    for start, end in zip(cuts[:-1], cuts[1:]):
        if start == point:
            before = state.copy()
            state[3:] += np.array([0., 0., body["wet_weight_n"]])-point_drag
            jumps.append({"station_from_anchor_m": point, "before": before.tolist(),
                          "after": state.tolist(), "body_drag_n": point_drag.tolist()})
        material = next(row for row in declared["rows"]
                        if row["start_m"] <= origin+(start+end)/2 <= row["end_m"])
        coefficient = rho/2*material["drag_coefficient"]*material["diameter_m"]
        def derivative(s, value):
            nonlocal evaluations
            evaluations += 1
            traction = value[3:]
            magnitude = np.linalg.norm(traction)
            if magnitude <= 1e-10:
                raise ValueError("continuous reference has a singular traction")
            tangent = traction/magnitude
            normal = fluid-np.dot(fluid, tangent)*tangent
            drag = coefficient*np.linalg.norm(normal)*normal
            return np.r_[(1+magnitude/material["ea_n"])*tangent,
                         np.array([0., 0., material["wet_weight_n_m"]])-drag]
        solved = solve_ivp(derivative, (start, end), state, method="DOP853",
            rtol=relative_tolerance, atol=absolute_tolerance, max_step=.25)
        if not solved.success:
            raise ValueError("independent continuous flow ODE failed: "+solved.message)
        state = solved.y[:, -1]
    return {"vessel": state[:3], "displacement": state[:3]-declared["anchor"],
            "top_traction": state[3:], "point_jumps": jumps, "evaluations": evaluations,
            "relative_tolerance": relative_tolerance, "absolute_tolerance": absolute_tolerance}


def half(values):
    values = np.asarray(values)
    return np.r_[values[0]/2, (values[:-1]+values[1:])/2, values[-1]/2]


def expected_loads(declared, rest):
    """Independent Decimal local intersections and natural point fractions."""
    with localcontext() as context:
        context.prec = 60
        dec = lambda value: Decimal.from_float(float(value))
        origin = dec(declared["origin"])
        lengths = [dec(value) for value in rest]
        q = [origin+sum(lengths[j:], Decimal(0)) for j in range(len(rest)+1)]
        totals = {key: [] for key in ("wet", "dry", "volume_mass", "compliance", "cd_d")}
        for low, high in zip(q[1:], q[:-1]):
            part = {key: Decimal(0) for key in totals}
            covered = Decimal(0)
            for row in declared["rows"]:
                overlap = max(Decimal(0), min(high, dec(row["end_m"]))-max(low, dec(row["start_m"])))
                covered += overlap
                part["wet"] += overlap*dec(row["wet_weight_n_m"])
                part["dry"] += overlap*dec(row["mass_kg_m"])
                part["volume_mass"] += overlap*dec(declared["rho"]*math.pi*row["diameter_m"]**2/4)
                part["compliance"] += overlap/dec(row["ea_n"])
                part["cd_d"] += overlap*dec(row["drag_coefficient"])*dec(row["diameter_m"])
            if covered != high-low:
                raise ValueError("reference material interval lacks complete declared coverage")
            for key in totals:
                totals[key].append(float(part[key]))
        alpha = np.zeros(len(q))
        body = declared["body"]
        point = dec(body["material_m"])
        for i, length in enumerate(lengths):
            if q[i+1] <= point <= q[i]:
                a = float((point-q[i+1])/length)
                alpha[i:i+2] = [a, 1-a]
                break
        if abs(sum(alpha)-1.) > 1e-12:
            raise ValueError("point reference is not fully deployed")
    cable_wet = half(totals["wet"])
    added = .7
    body_mass = body["mass_kg"]+added*max(0., body["mass_kg"]-body["wet_weight_n"]/G)
    return {"q": np.array([float(value) for value in q]), "alpha": alpha,
            "weight": cable_wet+alpha*body["wet_weight_n"],
            "dry_mass": half(totals["dry"])+alpha*body["mass_kg"],
            "mass": half(np.asarray(totals["dry"])+added*np.asarray(totals["volume_mass"]))+alpha*body_mass,
            "compliance": np.asarray(totals["compliance"]),
            "ea": np.asarray(rest)/np.asarray(totals["compliance"]),
            "cable_drag": declared["rho"]/2*half(totals["cd_d"]),
            "body_drag": declared["rho"]/2*alpha*body["drag_coefficient"]*body["drag_area_m2"]}


def direct_drag(p, declared, loads):
    secant = np.vstack([p[1]-p[0], (p[2:]-p[:-2])/2, p[-1]-p[-2]])
    tangent = secant/np.linalg.norm(secant, axis=1)[:, None]
    fluid = declared["current"]
    normal = fluid-np.sum(fluid*tangent, axis=1)[:, None]*tangent
    return (loads["cable_drag"][:, None]*np.linalg.norm(normal, axis=1)[:, None]*normal+
            loads["body_drag"][:, None]*np.linalg.norm(fluid)*fluid)


def discrete_reference(elements, declared):
    """Traction-coordinate nonlinear reference including endpoint half drag.

    The oldest element traction is solved rather than simply set to the applied
    endpoint traction: half cable weight AND half drag belong to the anchor.
    """
    rest = np.full(elements, declared["length"]/elements)
    loads = expected_loads(declared, rest)
    guess = np.tile(declared["bottom"], (elements, 1)).astype(float)
    guess[-1, 2] += loads["weight"][-1]
    for j in range(elements-2, -1, -1):
        guess[j] = guess[j+1]+[0., 0., loads["weight"][j+1]]
    def geometry(flat):
        traction = flat.reshape(elements, 3)
        norm = np.linalg.norm(traction, axis=1)
        vectors = (rest+norm*loads["compliance"])[:, None]*traction/norm[:, None]
        return np.vstack([declared["anchor"]+np.cumsum(vectors[::-1], axis=0)[::-1], declared["anchor"]])
    def residual(flat):
        traction = flat.reshape(elements, 3)
        p = geometry(flat)
        external = direct_drag(p, declared, loads)
        external[:, 2] -= loads["weight"]
        free = traction[:-1]-traction[1:]+external[1:-1]
        anchor = traction[-1]+external[-1]-declared["bottom"]
        return np.r_[free.ravel(), anchor]
    solved = root(residual, guess.ravel(), method="hybr", tol=1e-11)
    residual_n = float(np.max(np.abs(residual(solved.x))))
    if not solved.success or residual_n > 1e-8:
        raise ValueError(f"independent discrete traction root failed: {solved.message}; residual {residual_n}")
    p, traction = geometry(solved.x), solved.x.reshape(elements, 3)
    external = direct_drag(p, declared, loads)
    external[:, 2] -= loads["weight"]
    return {"positions": p, "rest": rest, "loads": loads,
            "traction": traction, "top_reaction": traction[0]-external[0],
            "reference_residual_n": residual_n, "reference_evaluations": solved.nfev}


def dynamic_configuration(discrete, declared, *, duration=.002, step=.002):
    p, rest = discrete["positions"], discrete["rest"]
    fluid = {"schema": "oceanroute.initial-fluid.v1", "operator": "node-secant-normal-cable-and-isotropic-body-drag-v1",
        "water_density_kg_m3": declared["rho"], "current_m_s": declared["current"].tolist(), "current_profile": None,
        "depth_reference": "max(-model_z_m,0)", "profile_extrapolation": "hold_endpoints"}
    return {"nodes": len(p), "wet_weight_n_m": 4., "diameter_m": .02, "mass_kg_m": 1.2,
        "ea_n": 10000., "ei_n_m2": 0., "water_density_kg_m3": declared["rho"],
        "added_mass_coefficient": .7, "initial_suspended_material_m": declared["origin"],
        "material_segments": declared["rows"], "inline_bodies": [declared["body"]],
        "seabed_grid": {"schema": "oceanroute.bathymetry.v1", "x_m": [-100., 0., 100.],
            "y_m": [-100., 0., 100.], "z_m": [[-100.]*3 for _ in range(3)],
            "source": {"name": "explicit synthetic no-contact steady-flow refinement",
                "horizontal_crs": "LOCAL_CARTESIAN_METRES", "origin_projected_m": [0., 0.],
                "vertical_datum": "already aligned synthetic model sea zero"}},
        "initial_equilibrium": {"schema": "oceanroute.dynamic.initial-equilibrium.v2", "initial_fluid": fluid,
            "vessel_position_m": p[0].tolist(), "anchor_position_m": p[-1].tolist(),
            "rest_lengths_m": rest.tolist(), "initial_positions_m": p.tolist(),
            "solver": {"max_solver_iterations": 3, "max_function_evaluations": 600,
                "force_tolerance_n": 1e-6, "relative_force_tolerance": 1e-9}},
        "ship_speed_m_s": 0., "payout_m_s": 0., "current_x_m_s": declared["current"][0],
        "current_y_m_s": declared["current"][1], "seabed_friction": 0., "damping_ratio": 0.,
        "internal_dt_s": step, "dt_s": .02, "duration_s": duration, "solver_iterations": 32}


def actual_case(elements, boundary, point_station, point_weight):
    declared = declarations(boundary, point_station, point_weight)
    continuous = continuous_reference(declared)
    discrete = discrete_reference(elements, declared)
    c = dynamic_configuration(discrete, declared)
    result = simulate_lay({}, c)
    first, last, proof = result["frames"][0], result["frames"][-1], result["initialization"]
    p, loads = np.asarray(first["nodes"]), discrete["loads"]
    assert proof["schema"] == "oceanroute.dynamic.initial-equilibrium.provenance.v3"
    assert result["model"] == "material-lumped-mass-xpbd-cable-lay-v5" and result["checkpoint"]["schema_version"] == 4
    assert proof["solver"]["accepted"] is True and proof["verification"]["accepted"] is True
    assert np.max(np.abs(p-discrete["positions"])) < 1e-8
    for field, key in [("node_material_m", "q"), ("node_wet_weight_n", "weight"),
                       ("node_mass_kg", "mass"), ("node_dry_mass_kg", "dry_mass"), ("segment_ea_n", "ea")]:
        assert np.max(np.abs(np.asarray(first[field])-loads[key])) < 1e-8, field
    boundary_force = np.asarray(proof["initial_snapshot"]["node_boundary_force_n"])
    assert np.max(np.abs(boundary_force[-1]+declared["bottom"])) < 1e-6
    assert np.max(np.abs(boundary_force[0]-discrete["top_reaction"])) < 1e-6
    assert first["touchdown"] is None and first["bottom_tension_n"] is None
    assert first["paid_out_m"] == last["paid_out_m"] == 0
    assert last["material_length_m"] == declared["length"]
    delta = np.diff(p, axis=0)
    norm = np.linalg.norm(delta, axis=1)
    tension = np.maximum(norm-discrete["rest"], 0)/loads["compliance"]
    force = direct_drag(p, declared, loads)
    force[:, 2] -= loads["weight"]
    force[:-1] += tension[:, None]*delta/norm[:, None]
    force[1:] -= tension[:, None]*delta/norm[:, None]
    residual_n = float(np.max(np.linalg.norm(force[1:-1], axis=1)))
    assert residual_n < 1e-6
    drift = float(np.max(np.linalg.norm(np.asarray(last["nodes"])-p, axis=1)))
    assert drift < 1e-7
    displacement = p[0]-p[-1]
    return {"elements": elements, "boundary_from_anchor_m": boundary,
        "point_from_anchor_m": point_station, "body_wet_weight_n": point_weight,
        "discrete_displacement_m": displacement.tolist(), "continuous_displacement_m": continuous["displacement"].tolist(),
        "endpoint_displacement_error_m": float(np.linalg.norm(displacement-continuous["displacement"])),
        "discrete_top_reaction_n": boundary_force[0].tolist(), "continuous_top_traction_n": continuous["top_traction"].tolist(),
        "top_traction_error_n": float(np.linalg.norm(boundary_force[0]-continuous["top_traction"])),
        "anchor_reaction_n": boundary_force[-1].tolist(), "independent_force_residual_n": residual_n,
        "actual_first_step_drift_m": drift, "continuous_function_evaluations": continuous["evaluations"],
        "discrete_root_function_evaluations": discrete["reference_evaluations"],
        "actual_initial_jacobians": proof["solver"]["iterations"],
        "actual_initial_function_evaluations": proof["solver"]["function_evaluations"],
        "actual_static_core_work_units": proof["solver"]["actual_work_units"],
        "declared_static_and_mapping_work_units": proof["solver"]["estimated_work_units"]}


def source_snapshot():
    return [{"path": name, "sha256": hashlib.sha256((ROOT/name).read_bytes()).hexdigest()} for name in SOURCE_FILES]


def driven_time_probe():
    """Actual driven transient; 2ms is an allowed numerical reference, not truth."""
    declared = declarations(8.3, 14.7, -30.)
    discrete = discrete_reference(24, declared)
    outputs = []
    for h in [.016, .008, .004, .002]:
        c = dynamic_configuration(discrete, declared, duration=.08, step=h)
        c["ship_speed_m_s"], c["payout_m_s"] = .2, .25
        result = simulate_lay({}, c)
        frame = result["frames"][-1]
        p = np.asarray(frame["nodes"])
        assert frame["paid_out_m"] == .02
        assert abs(frame["material_length_m"]-24.02) < 1e-11
        assert p[-1].tolist() == declared["anchor"].tolist()
        assert np.max(np.linalg.norm(p[1:-1]-discrete["positions"][1:-1], axis=1)) > .001
        outputs.append({"h": h, "positions": p, "frame": frame})
    reference = outputs[-1]["positions"]
    cases = [{"internal_dt_s": row["h"], "max_position_difference_from_2ms_m":
              float(np.max(np.linalg.norm(row["positions"]-reference, axis=1))),
              "max_free_node_displacement_m": float(np.max(np.linalg.norm(
                  row["positions"][1:-1]-discrete["positions"][1:-1], axis=1))),
              "paid_out_m": row["frame"]["paid_out_m"],
              "material_length_m": row["frame"]["material_length_m"]} for row in outputs]
    errors = [row["max_position_difference_from_2ms_m"] for row in cases[:-1]]
    return {"elements": 24, "boundary_from_anchor_m": 8.3, "point_from_anchor_m": 14.7,
            "body_wet_weight_n": -30., "duration_s": .08, "ship_speed_m_s": .2,
            "payout_m_s": .25, "reference_internal_dt_s": .002, "cases": cases,
            "differences_to_finest_strictly_decrease": bool(errors[2] < errors[1] < errors[0]),
            "scope": "Same discrete mesh and true fixed endpoints except prescribed ship motion; an actual driven transient comparison. The allowed finest 2ms split integrator is a numerical reference, not an exact dynamics solution or sea-trial evidence."}


def benchmark():
    started, sources_before = time.perf_counter(), source_snapshot()
    cases, groups = [], []
    for name, boundary, point_station in [("aligned", 8., 14.), ("unaligned", 8.3, 14.7)]:
        for weight in [30., -30.]:
            group = []
            for elements in [12, 24, 48]:
                actual = actual_case(elements, boundary, point_station, weight)
                actual["alignment"] = name
                cases.append(actual)
                group.append(actual)
            errors = [row["endpoint_displacement_error_m"] for row in group]
            groups.append({"alignment": name, "body_wet_weight_n": weight,
                "elements": [12, 24, 48], "endpoint_errors_m": errors,
                "strictly_decreasing_endpoint_error": bool(errors[2] < errors[1] < errors[0]),
                "top_traction_errors_n": [row["top_traction_error_n"] for row in group]})
    probe = driven_time_probe()
    sources_after = source_snapshot()
    if sources_after != sources_before:
        raise ValueError("runtime source bytes changed during the actual refinement run; rerun with a stable snapshot")
    return {"status": "passed", "validation_status": "research", "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": time.perf_counter()-started, "source_files": sources_after,
        "cases": cases, "groups": groups, "driven_time_probe": probe,
        "all_endpoint_errors_strictly_decrease": all(g["strictly_decreasing_endpoint_error"] for g in groups),
        "reference": "Piecewise continuous extensible natural-coordinate ODE in a nonzero stationary horizontal current, including material discontinuities and the full isotropic signed point-load/drag traction jump; independent discrete traction root with actual endpoint half gravity and drag.",
        "shared_declarations": {"natural_length_m": LENGTH, "manufacturing_origin_m": ORIGIN,
            "applied_bottom_traction_n": BOTTOM.tolist(), "uniform_current_m_s": CURRENT.tolist(),
            "anchor_position_m": ANCHOR.tolist(), "body_drag_area_m2": BODY_AREA, "body_drag_coefficient": BODY_CD,
            "continuous_method": "DOP853", "continuous_rtol": 1e-11, "continuous_atol": 1e-12,
            "actual_first_step_s": .002},
        "limits": "Synthetic no-contact common-traction/natural-stock refinement; each mesh has its own fixed vessel endpoint. This is not a common fixed-end BVP, shear/contact convergence or field validation. Endpoint trends are recorded without imposing monotonicity, tuning tolerances or discarding cases. A point may lie within one straight discrete element; local concentrated-load kink, body rotation/inertia, finite rod, EI, waves, friction history and original-product equivalence remain unverified."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="resources/validation/development_0.7_current_continuous.json")
    args = parser.parse_args()
    report = benchmark()
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT/output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+"\n")
    print(json.dumps({"status": report["status"], "cases": len(report["cases"]),
        "elapsed_s": report["elapsed_s"], "monotonic_endpoint_errors": report["all_endpoint_errors_strictly_decrease"],
        "output": str(output)}, ensure_ascii=False, allow_nan=False))
