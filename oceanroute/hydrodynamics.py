"""Canonical stationary fluid and the actual nodal cable/point drag operator.

Coefficients supplied by the natural-material loader already contain rho/2.
This is an independent research operator, not a continuous rod/body model.
"""
from __future__ import annotations

import math
import numpy as np

FLUID_SCHEMA = "oceanroute.initial-fluid.v1"
DRAG_OPERATOR = "node-secant-normal-cable-and-isotropic-body-drag-v1"
_FIELDS = {"schema", "operator", "water_density_kg_m3", "current_m_s",
           "current_profile", "depth_reference", "profile_extrapolation"}


def _number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(name+" must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(name+" must be a finite number") from error
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{name} must be finite in [{minimum}, {maximum}]")
    return result


def _profile(raw, strict):
    if raw is None:
        return None
    if not isinstance(raw, list) or not 2 <= len(raw) <= 500:
        raise ValueError("current_profile requires 2 to 500 rows")
    result = []
    for row in raw:
        if not isinstance(row, dict) or "depth_m" not in row:
            raise ValueError("current_profile rows require depth_m")
        if set(row)-{"depth_m", "x_m_s", "y_m_s"} or (strict and set(row) != {"depth_m", "x_m_s", "y_m_s"}):
            raise ValueError("current_profile unsupported or missing canonical fields")
        result.append({"depth_m": _number(row["depth_m"], "current_profile.depth_m", 0, 12000),
                       "x_m_s": _number(row.get("x_m_s", 0), "current_profile.x_m_s", -20, 20),
                       "y_m_s": _number(row.get("y_m_s", 0), "current_profile.y_m_s", -20, 20)})
    if any(b["depth_m"] <= a["depth_m"] for a, b in zip(result[:-1], result[1:])):
        raise ValueError("current_profile depths must be strictly increasing")
    return result


def canonical_initial_fluid(config: dict) -> dict:
    """Freeze the actual rho/current configuration without averaging its profile."""
    if not isinstance(config, dict):
        raise ValueError("fluid config must be an object")
    return {"schema": FLUID_SCHEMA, "operator": DRAG_OPERATOR,
            "water_density_kg_m3": _number(config.get("water_density_kg_m3", 1025), "water_density_kg_m3", 1, 2000),
            "current_m_s": [_number(config.get("current_x_m_s", 0), "current_x_m_s", -20, 20),
                            _number(config.get("current_y_m_s", 0), "current_y_m_s", -20, 20), 0.],
            "current_profile": _profile(config.get("current_profile"), False),
            "depth_reference": "max(-model_z_m,0)", "profile_extrapolation": "hold_endpoints"}


def validate_initial_fluid(raw: dict) -> dict:
    """Validate a complete historical declaration; never silently add its fields."""
    if not isinstance(raw, dict) or set(raw) != _FIELDS:
        raise ValueError("initial_fluid requires exactly the complete canonical fields")
    if raw["schema"] != FLUID_SCHEMA or raw["operator"] != DRAG_OPERATOR:
        raise ValueError("initial_fluid schema/operator is unsupported")
    if raw["depth_reference"] != "max(-model_z_m,0)" or raw["profile_extrapolation"] != "hold_endpoints":
        raise ValueError("initial_fluid depth/extrapolation convention is unsupported")
    vector = raw["current_m_s"]
    if not isinstance(vector, list) or len(vector) != 3:
        raise ValueError("initial_fluid.current_m_s requires three components")
    vector = [_number(value, "initial_fluid.current_m_s", -20, 20) for value in vector]
    if vector[2] != 0:
        raise ValueError("initial_fluid only supports horizontal flow")
    return {"schema": FLUID_SCHEMA, "operator": DRAG_OPERATOR,
            "water_density_kg_m3": _number(raw["water_density_kg_m3"], "water_density_kg_m3", 1, 2000),
            "current_m_s": vector, "current_profile": _profile(raw["current_profile"], True),
            "depth_reference": raw["depth_reference"], "profile_extrapolation": raw["profile_extrapolation"]}


def _array(value, shape, name, nonnegative=False):
    try:
        array = np.asarray(value, dtype=float)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(name+" must be a finite numeric array") from error
    if array.shape != shape or not np.isfinite(array).all():
        raise ValueError(name+" has invalid shape/nonfinite values")
    if nonnegative and np.any(array < 0):
        raise ValueError(name+" must be nonnegative")
    return array


def _loads_validated(p, v, local, fluid, profile_arrays=None):
    """Internal fast path after canonical-fluid validation; still checks geometry."""
    positions = np.asarray(p, dtype=float)
    if positions.ndim != 2 or positions.shape[1] != 3 or not 2 <= len(positions) <= 256:
        raise ValueError("hydrodynamic positions require 2 to 256 three-component nodes")
    n = len(positions)
    positions = _array(positions, (n, 3), "hydrodynamic positions")
    velocities = _array(v, (n, 3), "hydrodynamic velocities")
    if not isinstance(local, dict) or "drag" not in local or "body_drag" not in local:
        raise ValueError("hydrodynamic local coefficients require drag and body_drag")
    cable = _array(local["drag"], (n,), "cable drag factor", True)
    body = _array(local["body_drag"], (n,), "body drag factor", True)
    secant = np.gradient(positions, axis=0)
    with np.errstate(over="ignore", invalid="ignore"):
        magnitude = np.linalg.norm(secant, axis=1)
    if not np.isfinite(magnitude).all() or np.any(magnitude <= 1e-10):
        raise ValueError("degenerate node secant tangent (norm <= 1e-10 m) is unsupported")
    tangent = secant/magnitude[:, None]
    profile = fluid["current_profile"]
    current = np.tile(fluid["current_m_s"], (n, 1))
    if profile is not None:
        rows = profile_arrays if profile_arrays is not None else np.array([[r["depth_m"], r["x_m_s"], r["y_m_s"]] for r in profile])
        depth = np.maximum(-positions[:, 2], 0.)
        current[:, 0] = np.interp(depth, rows[:, 0], rows[:, 1])
        current[:, 1] = np.interp(depth, rows[:, 0], rows[:, 2])
    relative = current-velocities
    normal = relative-np.sum(relative*tangent, axis=1)[:, None]*tangent
    with np.errstate(over="ignore", invalid="ignore"):
        cable_force = cable[:, None]*np.linalg.norm(normal, axis=1)[:, None]*normal
        body_force = body[:, None]*np.linalg.norm(relative, axis=1)[:, None]*relative
        total = cable_force+body_force
    if not np.isfinite(total).all():
        raise ValueError("hydrodynamic force exceeds the finite numerical domain")
    return {"node_current_m_s": current, "node_tangent": tangent,
            "node_cable_drag_force_n": cable_force, "node_body_drag_force_n": body_force,
            "node_total_drag_force_n": total}


def hydrodynamic_loads(p, v, local, fluid) -> dict:
    """Normal cable drag and isotropic node-lumped point drag, in N.

    Tangents use node-index secants, matching the actual laying model. The
    supplied coefficients already contain fluid density and natural shares.
    """
    return HydrodynamicField(fluid).loads(p, v, local)


class HydrodynamicField:
    """Validated stationary field with its depth table parsed exactly once.

    The public scalar-call operator and this reusable fast path are identical.
    ``profile_rows`` exposes the one-time bounded parsing charge to callers.
    """
    def __init__(self, initial_fluid):
        self.fluid = validate_initial_fluid(initial_fluid)
        rows = self.fluid["current_profile"]
        self.profile_rows = 0 if rows is None else len(rows)
        self._profile_arrays = None if rows is None else np.array([[r["depth_m"],r["x_m_s"],r["y_m_s"]] for r in rows])

    def loads(self, p, v, local):
        return _loads_validated(p,v,local,self.fluid,self._profile_arrays)
