"""Real coordinate/frame counterexamples independent of the bridge's helper."""
from copy import deepcopy
import math

import numpy as np
import pytest

from oceanroute.geodesy import WGS84_A
from oceanroute.plan_equilibrium_frame import PlanBathymetryFrame, initial_equilibrium_length, _digest


def grid():
    return {"schema": "oceanroute.bathymetry.v1", "x_m": [-100., 0., 100.], "y_m": [-100., 0., 100.],
            "z_m": [[-30., -29., -30.], [-30., -30., -30.], [-30., -31., -30.]],
            "source": {"name": "declared curved equatorial test field", "horizontal_crs": "EPSG:3857",
                       "origin_projected_m": [-10., 20.], "vertical_datum": "model sea surface z0"}}


def options():
    return {"anchor": {"longitude": math.degrees(10/WGS84_A), "latitude": 0., "z_model_m": -10.},
            "vessel_z_m": -1., "natural_length_m": 20.,
            "initial_positions_m": [[30.-2*i, -20., -1.-1.8*i] for i in range(6)]}


def start():
    return {"longitude": math.degrees(20/WGS84_A), "latitude": 0.}


def test_real_rebasing_translates_axes_endpoints_and_seed_together_without_height_change():
    original, initial = grid(), options()
    keep_grid, keep_initial = deepcopy(original), deepcopy(initial)
    frame = PlanBathymetryFrame(original, start(), initial, 6)
    # EPSG:3857 equatorial easting is a*longitude_radians, not output of
    # the function under test or of the internal transform wrapper.
    np.testing.assert_allclose(frame.absolute_origin, [20., 0.], atol=1e-11)
    np.testing.assert_allclose(frame.shift, [30., -20.], atol=1e-11)
    np.testing.assert_allclose(frame.grid["x_m"], [-130., -30., 70.], atol=1e-11)
    np.testing.assert_allclose(frame.grid["y_m"], [-80., 20., 120.], atol=1e-11)
    assert frame.grid["z_m"] == original["z_m"]
    np.testing.assert_allclose(frame.request["vessel_position_m"], [0., 0., -1.], atol=1e-12)
    np.testing.assert_allclose(frame.request["anchor_position_m"], [-10., 0., -10.], atol=1e-11)
    np.testing.assert_allclose(frame.request["initial_positions_m"], [[-2*i, 0., -1.-1.8*i] for i in range(6)], atol=1e-11)
    assert original == keep_grid and initial == keep_initial
    assert frame.metadata()["vertical_translation_m"] == 0
    assert all(op["ballpark"] is False and op["best_available"] is True for op in frame.operations)


def test_known_height_and_gradient_are_invariant_under_actual_rebasing():
    from oceanroute.bathymetry import BathymetryGrid
    frame = PlanBathymetryFrame(grid(), start(), options(), 6)
    old_xy = np.array([[30., -20.], [-20., -10.], [50., 60.]])
    old_z, old_gradient = BathymetryGrid(grid()).evaluate(old_xy)
    new_z, new_gradient = frame.field.evaluate(old_xy-np.array([30., -20.]))
    np.testing.assert_allclose(old_z, new_z, atol=1e-12)
    np.testing.assert_allclose(old_gradient, new_gradient, atol=1e-12)
    assert frame.metadata()["original_grid_sha256"] != frame.metadata()["rebased_grid_sha256"]


@pytest.mark.parametrize("kind", ["unbound-local", "feet", "geographic", "missing-ship-cell", "outside-ship-grid"])
def test_no_frame_label_swap_unit_coercion_or_bed_extension(kind):
    document = grid()
    if kind == "unbound-local":
        document["source"].update(horizontal_crs="LOCAL_CARTESIAN_METRES", origin_projected_m=[0, 0])
    elif kind == "feet":
        document["source"]["horizontal_crs"] = "EPSG:2277"
    elif kind == "geographic":
        document["source"]["horizontal_crs"] = "EPSG:4326"
    elif kind == "missing-ship-cell":
        document["z_m"][0][2] = None
    else:
        document["source"]["origin_projected_m"] = [1000, 1000]
    with pytest.raises(ValueError):
        PlanBathymetryFrame(document, start(), options(), 6)


@pytest.mark.parametrize("value", [True, "1", float("nan"), float("inf"), 10**1000])
def test_invalid_seed_numbers_do_not_become_valid_floats(value):
    initial = options(); initial["initial_positions_m"][2][1] = value
    with pytest.raises(ValueError):
        PlanBathymetryFrame(grid(), start(), initial, 6)


def test_length_is_declared_material_not_chord_or_route_kp_and_float_json_is_equivalent():
    initial = options(); del initial["natural_length_m"]
    initial["rest_lengths_m"] = [1., 2., 3., 4., 10.]
    assert initial_equilibrium_length(initial, 6) == 20.
    assert _digest({"a": [1., 2., -0.]}) == _digest({"a": [1, 2, 0]})
    assert _digest({"a": [True]}) != _digest({"a": [1]})


@pytest.mark.parametrize("kind", ["both-lengths", "no-length", "wrong-segment-count", "unknown-input", "boolean-length"])
def test_explicit_equilibrium_inventory_contract(kind):
    initial = options()
    if kind == "both-lengths": initial["rest_lengths_m"] = [4]*5
    elif kind == "no-length": del initial["natural_length_m"]
    elif kind == "wrong-segment-count":
        del initial["natural_length_m"]; initial["rest_lengths_m"] = [5]*4
    elif kind == "unknown-input": initial["accepted"] = True
    else: initial["natural_length_m"] = True
    with pytest.raises(ValueError): initial_equilibrium_length(initial, 6)
