"""Compare the actual discrete initializer with an analytic continuous cable.

This is a traction/material-length refinement benchmark in still water with
no bed contact. Each mesh's independently balanced shape supplies its own
fixed endpoints to the initializer. It is not a common fixed-end BVP, a field
validation, or evidence for an independent body/contact/rotation model.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
from datetime import datetime, timezone

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from oceanroute.simulation import simulate_lay


def continuous_displacement(boundary, body_station, body_weight):
    """Integrate the continuous elastic cable exactly between load jumps."""
    horizontal, vertical, displacement = 150., 20., np.zeros(3)
    cuts = sorted({0., boundary, body_station, 24.})
    for a, b in zip(cuts[:-1], cuts[1:]):
        if a == body_station:
            vertical += body_weight
        wet, ea = (4., 10000.) if (a+b)/2 < boundary else (7., 24000.)
        next_vertical = vertical+wet*(b-a)
        displacement[0] += horizontal/wet*(math.asinh(next_vertical/horizontal)-math.asinh(vertical/horizontal)) + horizontal*(b-a)/ea
        displacement[2] += (math.hypot(horizontal, next_vertical)-math.hypot(horizontal, vertical))/wet + (vertical+next_vertical)*(b-a)/(2*ea)
        vertical = next_vertical
    return displacement, vertical


def discrete_fixture(elements, boundary, body_station, body_weight):
    origin, horizontal = 37., 150.
    rest = np.full(elements, 24./elements)
    coordinates = origin+np.r_[np.cumsum(rest[::-1])[::-1], 0.]
    a_length = np.array([max(0., min(high, origin+boundary)-max(low, origin))
                         for high, low in zip(coordinates[:-1], coordinates[1:])])
    b_length = rest-a_length
    segment_weight = 4*a_length+7*b_length
    ea = rest/(a_length/10000.+b_length/24000.)
    nodes_weight = np.r_[segment_weight[0]/2, (segment_weight[:-1]+segment_weight[1:])/2, segment_weight[-1]/2]
    body = origin+body_station
    alpha = np.zeros(elements+1)
    for i, (high, low) in enumerate(zip(coordinates[:-1], coordinates[1:])):
        if low <= body <= high:
            alpha[i] = (body-low)/rest[i]
            alpha[i+1] = (high-body)/rest[i]
            break
    nodes_weight += body_weight*alpha
    vertical = np.empty(elements); vertical[-1] = 20.+segment_weight[-1]/2
    for j in range(elements-1, 0, -1):
        vertical[j-1] = vertical[j]+nodes_weight[j]
    tension = np.hypot(horizontal, vertical)
    direction = np.column_stack((horizontal/tension, np.zeros(elements), vertical/tension))
    vectors = rest[:, None]*(1+tension[:, None]/ea[:, None])*direction
    vessel = np.array([0., 0., -2.])
    positions = np.vstack((vessel, vessel-np.cumsum(vectors, axis=0)))
    c = {"nodes": elements+1, "wet_weight_n_m": 4., "diameter_m": .02, "mass_kg_m": 1.2,
         "ea_n": 10000., "ei_n_m2": 0., "initial_suspended_material_m": origin,
         "material_segments": [
             {"start_m": 0., "end_m": origin+boundary, "wet_weight_n_m": 4., "ea_n": 10000., "mass_kg_m": 1.2},
             {"start_m": origin+boundary, "end_m": origin+25., "wet_weight_n_m": 7., "ea_n": 24000., "mass_kg_m": 1.8}],
         "inline_bodies": [{"id": "point", "material_m": body, "length_m": 0., "mass_kg": 10., "wet_weight_n": body_weight}],
         "seabed_grid": {"schema": "oceanroute.bathymetry.v1", "x_m": [-100., 0., 100.], "y_m": [-100., 0., 100.],
             "z_m": [[-100.]*3 for _ in range(3)], "source": {"name": "synthetic no-contact refinement bed",
                 "horizontal_crs": "LOCAL_CARTESIAN_METRES", "origin_projected_m": [0., 0.], "vertical_datum": "model sea zero"}},
         "initial_equilibrium": {"schema": "oceanroute.dynamic.initial-equilibrium.v1",
             "vessel_position_m": vessel.tolist(), "anchor_position_m": positions[-1].tolist(),
             "rest_lengths_m": rest.tolist(), "initial_positions_m": positions.tolist(),
             "solver": {"max_solver_iterations": 2, "max_function_evaluations": 20,
                        "force_tolerance_n": 1e-6, "relative_force_tolerance": 1e-9}},
         "ship_speed_m_s": 0., "payout_m_s": 0., "current_x_m_s": 0., "current_y_m_s": 0.,
         "seabed_friction": 0., "damping_ratio": 0., "internal_dt_s": .002, "dt_s": .02,
         "duration_s": .002, "solver_iterations": 32}
    return c, positions, nodes_weight


def benchmark():
    cases = []
    for name, boundary, body_station in [("aligned", 8., 14.), ("unaligned", 8.3, 14.7)]:
        for body_weight in [30., -30.]:
            continuous, top_vertical = continuous_displacement(boundary, body_station, body_weight)
            errors = []
            for elements in [12, 24, 48]:
                c, expected, expected_weight = discrete_fixture(elements, boundary, body_station, body_weight)
                result = simulate_lay({}, c)
                first = result["frames"][0]
                actual = np.asarray(first["nodes"])
                proof = result["initialization"]
                assert proof["schema"] == "oceanroute.dynamic.initial-equilibrium.provenance.v2"
                assert np.max(np.abs(actual-expected)) < 1e-8
                assert np.max(np.abs(np.asarray(first["node_wet_weight_n"])-expected_weight)) < 1e-8
                assert first["touchdown"] is None
                displacement = actual[0]-actual[-1]
                error = float(np.linalg.norm(displacement-continuous))
                errors.append(error)
                boundary_force = np.asarray(proof["initial_snapshot"]["node_boundary_force_n"])
                assert np.max(np.abs(boundary_force[0]-[150., 0., top_vertical])) < 1e-6
                assert np.max(np.abs(boundary_force[-1]-[-150., 0., -20.])) < 1e-6
                cases.append({"alignment": name, "body_wet_weight_n": body_weight, "elements": elements,
                    "initial_displacement_m": displacement.tolist(), "continuous_displacement_m": continuous.tolist(),
                    "endpoint_displacement_error_m": error, "independent_force_residual_n": proof["verification"]["max_node_force_residual_n"],
                    "actual_first_step_drift_m": float(np.max(np.linalg.norm(np.asarray(result["frames"][-1]["nodes"])-expected, axis=1)))})
            assert errors[2] < errors[1] < errors[0], (name, body_weight, errors)
    source_files = ["oceanroute/simulation.py", "oceanroute/static_bathymetry.py", "oceanroute/initial_equilibrium.py", "oceanroute/checkpoints.py"]
    return {"status": "passed", "recorded_at_utc": datetime.now(timezone.utc).isoformat(), "cases": cases,
        "source_files": [{"path": p, "sha256": hashlib.sha256((ROOT/p).read_bytes()).hexdigest()} for p in source_files],
        "reference": "Exact continuous still-water extensible cable integral for common horizontal/end vertical traction, natural stock, material intervals and point wet load. Mesh endpoints vary and converge; this is not a common fixed-end BVP.",
        "limitations": "Synthetic no-contact refinement only. No current, waves, EI, friction history, independent point/body contact or rotation, finite-length rod, field calibration or original product equivalence. Off-node point interpolation retains a single straight element; endpoint convergence does not validate a local point kink."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="resources/validation/development_0.7_continuous_refinement.json")
    args = parser.parse_args()
    report = benchmark()
    output = ROOT/args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+"\n")
    print(json.dumps({"status": report["status"], "cases": len(report["cases"]), "output": str(output)}, ensure_ascii=False))
