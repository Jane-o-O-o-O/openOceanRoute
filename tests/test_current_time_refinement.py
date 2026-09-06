"""Driven v5 time refinement against an independent continuous-time ODE.

The reference has the same fixed natural discrete material, zero bending and
no contact, with actual p-dependent normal drag and signed point loads. It
uses DOP853 rather than the production compliant constraint/predictor step.
This checks consistency of a driven branch, not just its stationary state.
"""
from copy import deepcopy

import numpy as np
import pytest
from scipy.integrate import solve_ivp

from oceanroute.simulation import simulate_lay
from test_current_initial_review import configuration, raw_current, forces_expected


def evaluate_time_refinement(shear):
    config, initial, loading, _ = configuration(shear=shear)
    config = raw_current(config, initial)
    speed, end, nf = .12, .08, len(initial)-2
    config.update({"ship_speed_m_s": speed, "payout_m_s": 0., "duration_s": end,
                   "damping_ratio": 0., "dt_s": .04, "checkpoint_times_s": [],
                   "solver_iterations": 40})
    rest = np.asarray(config["initial_equilibrium"]["rest_lengths_m"])
    initial_free = initial[1:-1].copy()
    def ode(time, state):
        p = initial.copy()
        p[0] += [speed*time, 0., 0.]
        p[1:-1] = state[:3*nf].reshape(nf, 3)
        v = np.zeros_like(p)
        v[0] = [speed, 0., 0.]
        v[1:-1] = state[3*nf:].reshape(nf, 3)
        forces = forces_expected(config, p, rest, loading)
        # That independent audit helper reports zero-velocity drag, so form
        # actual relative velocity here using the same independent oracle.
        from test_current_initial_review import drag_expected
        moving = drag_expected(config, p, loading, v)
        dynamic = forces["axial"]+moving["cable"]+moving["body"]
        dynamic[:, 2] -= loading["wet"]
        return np.r_[v[1:-1].ravel(), (dynamic[1:-1]/loading["mass"][1:-1, None]).ravel()]
    seed = np.r_[initial_free.ravel(), np.zeros(3*nf)]
    reference = solve_ivp(ode, (0., end), seed, method="DOP853", rtol=2e-11,
                          atol=2e-12, max_step=.0005)
    assert reference.success and np.all(np.isfinite(reference.y))
    exact = reference.y[:3*nf, -1].reshape(nf, 3)
    assert np.max(np.linalg.norm(exact-initial_free, axis=1)) > 1e-4
    errors, cases = [], []
    for h in (.008, .004, .002):
        result = simulate_lay({}, {**deepcopy(config), "internal_dt_s": h})
        final = np.asarray(result["checkpoint"]["state"]["positions"])
        assert final[0] == pytest.approx(initial[0]+[speed*end, 0., 0.], abs=1e-12)
        assert final[-1] == pytest.approx(initial[-1], abs=1e-12)
        assert result["checkpoint"]["schema_version"] == 4
        errors.append(float(np.max(np.linalg.norm(final[1:-1]-exact, axis=1))))
        cases.append({"internal_dt_s": h, "maximum_free_node_position_error_m": errors[-1],
                      "physical_checkpoint_schema_version": result["checkpoint"]["schema_version"],
                      "model": result["model"]})
    assert errors[2] < errors[1] < errors[0], errors
    assert errors[2] < .002, errors
    return {"flow": "depth-shear" if shear else "uniform", "duration_s": end,
            "ship_speed_m_s": speed, "reference_method": "DOP853 independent discrete force ODE",
            "reference_rtol": 2e-11, "reference_atol": 2e-12, "reference_max_step_s": .0005,
            "reference_function_evaluations": reference.nfev, "cases": cases,
            "maximum_reference_free_node_displacement_m": float(np.max(np.linalg.norm(exact-initial_free, axis=1)))}


@pytest.mark.parametrize("shear", [False, True], ids=["uniform", "depth-shear"])
def test_driven_current_time_refinement_converges_to_independent_force_ode(shear):
    evaluate_time_refinement(shear)
