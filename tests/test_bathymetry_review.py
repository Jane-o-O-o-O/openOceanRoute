"""Independent physical invariants for the actual 2D lay/contact implementation."""
from copy import deepcopy
import json

import numpy as np
import pytest

from oceanroute.checkpoints import read_checkpoint
from oceanroute.simulation import simulate_lay


def plane(sx=.17, sy=-.11, axes=(-40, 0, 40)):
    return {"schema": "oceanroute.bathymetry.v1", "x_m": list(axes), "y_m": list(axes),
            "z_m": [[-12+sx*x+sy*y for x in axes] for y in axes],
            "source": {"name": "independent rotation-invariant synthetic plane",
                       "horizontal_crs": "LOCAL_CARTESIAN_METRES", "origin_projected_m": [0, 0],
                       "vertical_datum": "explicitly aligned model surface z=0"}}


def config():
    return {"seabed_grid": plane(), "depth_m": 12, "wet_weight_n_m": 4,
            "bottom_tension_n": 10, "nodes": 12, "ship_speed_m_s": .2, "payout_m_s": .4,
            "heading_deg": 35, "duration_s": 1.5, "dt_s": .25, "internal_dt_s": .025,
            "solver_iterations": 24, "ea_n": 2e4, "current_x_m_s": .1,
            "current_y_m_s": -.2, "initial_suspended_material_m": 10,
            "material_segments": [
                {"id": "older", "start_m": 0, "end_m": 18, "wet_weight_n_m": 4,
                 "mass_kg_m": 1, "diameter_m": .025, "ea_n": 2e4},
                {"id": "newer", "start_m": 18, "end_m": 100, "wet_weight_n_m": 6,
                 "mass_kg_m": 2, "diameter_m": .04, "ea_n": 4e4}],
            "inline_bodies": [{"id": "observed-point-load", "material_m": 14,
                               "mass_kg": 2, "wet_weight_n": 10, "drag_area_m2": .04}],
            "ship_plan": [{"time_s": .7, "heading_deg": 110, "speed_m_s": .35,
                           "payout_m_s": .7}]}


def test_rotating_real_grid_current_turns_and_material_loads_preserves_dynamics():
    original = config()
    # Horizontal +90° rotation: (x,y)->(-y,x), navigation heading -> heading-90.
    rotated = deepcopy(original)
    rotated["seabed_grid"] = plane(.11, .17)
    rotated["heading_deg"] -= 90
    rotated["current_x_m_s"], rotated["current_y_m_s"] = .2, .1
    rotated["ship_plan"][0]["heading_deg"] -= 90
    a, b = simulate_lay({}, original), simulate_lay({}, rotated)
    rotation = np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
    assert a["solver"]["converged"] and b["solver"]["converged"]
    for fa, fb in zip(a["frames"], b["frames"]):
        for key in ("nodes", "node_velocity_m_s", "node_seabed_normal",
                    "node_contact_friction_impulse_n_s"):
            assert np.asarray(fa[key])@rotation.T == pytest.approx(np.asarray(fb[key]), abs=2e-9, rel=2e-9)
        for key in ("node_material_m", "node_mass_kg", "node_wet_weight_n",
                    "node_tension_n", "node_contact_normal_impulse_n_s"):
            assert fa[key] == pytest.approx(fb[key], abs=2e-8, rel=2e-9)
        assert fa["node_contact_mask"] == fb["node_contact_mask"]
        assert fa["energy"] == pytest.approx(fb["energy"], abs=2e-8, rel=2e-9)
    assert a["summary"]["contact"] == pytest.approx(b["summary"]["contact"], abs=2e-8, rel=2e-9)


def test_heterogeneous_manufacturing_and_body_weights_remain_physical_across_turn_resume():
    c = config(); c["checkpoint_times_s"] = [.75]
    uninterrupted = simulate_lay({}, c)
    saved = json.loads(json.dumps(uninterrupted["checkpoints"][0]))
    resumed = simulate_lay({}, {"resume_state": saved, "duration_s": .75})
    for frame in uninterrupted["frames"]:
        material = np.asarray(frame["node_material_m"])
        total = uninterrupted["checkpoint"]["state"]["initial_material_length_m"]+frame["paid_out_m"]
        assert frame["material_length_m"] == pytest.approx(total, abs=1e-10)
        assert material[-1] == 10 and material[0]-material[-1] == pytest.approx(total, abs=1e-10)
        assert np.all(np.diff(material) < 0)
        old = max(0., min(material[0], 18)-10)
        new = max(0., material[0]-18)
        # Body at physical coordinate14 is fully on the cable, counted once.
        assert sum(frame["node_wet_weight_n"]) == pytest.approx(4*old+6*new+10, abs=1e-9)
        assert sum(frame["node_dry_mass_kg"]) == pytest.approx(old+2*new+2, abs=1e-9)
        assert frame["inline_bodies"][0]["deployed_fraction"] == pytest.approx(1)
    for key in ("positions", "velocities", "rest_lengths_m", "node_material_m", "node_mass_kg",
                "segment_ea_n", "node_contact_normal_impulse_n_s", "node_contact_friction_impulse_n_s"):
        assert np.asarray(resumed["checkpoint"]["state"][key]) == pytest.approx(
            np.asarray(uninterrupted["checkpoint"]["state"][key]), abs=1e-8, rel=1e-9)
    assert resumed["checkpoint"]["schema_version"] == 2
    assert read_checkpoint(resumed["checkpoint"])["time_s"] == 1.5


def test_late_turn_to_missing_ship_cell_rejects_despite_valid_initial_depth_and_state():
    c = config(); c.update(heading_deg=90, ship_speed_m_s=0, payout_m_s=0, duration_s=.5)
    c.pop("ship_plan")
    c["seabed_grid"] = plane(0, 0, axes=(-40, 0, .25, 40))
    # Initial y=0 selects the fully known [0,.25] strip. Northward command
    # enters the upper strip whose far corner is missing; even ship samples
    # require a real four-corner normal and must not fall back to depth_m=12.
    c["seabed_grid"]["z_m"][-1][2] = None
    first = simulate_lay({}, c)
    with pytest.raises(ValueError, match="NoData"):
        simulate_lay({}, {"resume_state": first["checkpoint"], "duration_s": 1,
                          "ship_speed_m_s": 1, "heading_deg": 0, "payout_m_s": .5})
    assert first["checkpoint"]["schema_version"] == 2


def test_legacy_checkpoint_retains_legacy_version_and_cannot_silently_upgrade_to_grid():
    c = config(); c.pop("seabed_grid"); c.update(duration_s=.25, ship_speed_m_s=0, payout_m_s=0)
    c.pop("ship_plan")
    legacy = simulate_lay({}, c)["checkpoint"]
    restored = simulate_lay({}, {"resume_state": json.loads(json.dumps(legacy)), "duration_s": .25})
    assert legacy["schema_version"] == restored["checkpoint"]["schema_version"] == 1
    with pytest.raises(ValueError, match="cannot change seabed_grid"):
        simulate_lay({}, {"resume_state": legacy, "duration_s": .25, "seabed_grid": plane()})
