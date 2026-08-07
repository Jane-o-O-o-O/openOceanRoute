"""Independent cable mechanics models in SI units.

These are inspectable research models, not calibrated substitutes for any
commercial solver. See docs/MODEL_NOTES.md for equations and limitations.
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
from typing import Any

import numpy as np
from scipy.integrate import solve_ivp
from scipy.linalg import solve_banded
from scipy.optimize import brentq
from scipy.sparse import diags
from scipy.sparse.linalg import spsolve

from .checkpoints import merge_resume_config, pack_checkpoint

G = 9.80665
VALIDATION = "research"


def _num(c: dict, key: str, default: float, lo: float | None = None,
         hi: float | None = None, *, strict: bool = False) -> float:
    value = c.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a finite number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{key} must be finite")
    if lo is not None and (value <= lo if strict else value < lo):
        raise ValueError(f"{key} must be {'greater than' if strict else 'at least'} {lo}")
    if hi is not None and value > hi:
        raise ValueError(f"{key} must not exceed {hi}")
    return value


def _integer(c: dict, key: str, default: int, lo: int, hi: int) -> int:
    value = _num(c, key, default, lo, hi)
    if value != int(value):
        raise ValueError(f"{key} must be an integer")
    return int(value)


def _config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    return config


def _warning(code: str, message: str, severity: str = "warning") -> dict:
    return {"code": code, "message": message, "severity": severity}


def _heading(degrees: float) -> np.ndarray:
    """Compass heading: 0 north (+y), 90 east (+x)."""
    angle = math.radians(degrees % 360)
    return np.array([math.sin(angle), math.cos(angle), 0.0])


def _base(model: str, assumptions: list[str]) -> dict:
    return {"model": model, "validation_status": VALIDATION,
            "coordinate_convention": "local metres; x east, y north, z positive upward; sea surface z=0",
            "assumptions": assumptions, "warnings": []}


def _layback(depth: float, a: float) -> float:
    # acosh(1+x) loses all precision when x is smaller than machine epsilon.
    return 2 * a * math.asinh(math.sqrt(depth/(2*a))) if a else 0.


def catenary(config: dict) -> dict:
    """Exact, inextensible, positively weighted catenary with horizontal touchdown.

    Supply exactly one of bottom_tension_n, cable_length_m, layback_m, or omit
    all three to use 1000 N bottom tension. Length refers to suspended arc.
    """
    c = _config(config)
    depth = _num(c, "depth_m", 1000, .001, 12000)
    weight = _num(c, "wet_weight_n_m", 4, 1e-6, 20000)
    n = _integer(c, "nodes", 64, 3, 1000)
    heading = _num(c, "heading_deg", 90, -36000, 36000)
    supplied = [k for k in ("bottom_tension_n", "cable_length_m", "layback_m") if k in c]
    if len(supplied) > 1:
        raise ValueError("provide only one of bottom_tension_n, cable_length_m, layback_m")
    iterations = 0
    if "cable_length_m" in c:
        length = _num(c, "cable_length_m", depth, depth, 1000000)
        a = (length - depth) * (length + depth) / (2 * depth)
        horizontal = weight * a
    elif "layback_m" in c:
        requested = _num(c, "layback_m", depth, 0, 1000000)
        if requested == 0:
            a = 0.0
        else:
            def f(a_: float) -> float:
                return _layback(depth, a_) - requested
            upper = max(depth, requested ** 2 / depth)
            while f(upper) < 0:
                upper *= 2
            a, solved = brentq(f, 1e-12, upper, full_output=True, xtol=1e-9)
            iterations = solved.iterations
        horizontal = weight * a
        length = math.sqrt(depth * (depth + 2 * a))
    else:
        horizontal = _num(c, "bottom_tension_n", 1000, 0, 1e9)
        a = horizontal / weight
        length = math.sqrt(depth * (depth + 2 * a))
    layback = _layback(depth, a)
    arc = np.linspace(length, 0, n)
    if a:
        along = a * np.arcsinh(arc / a) - layback
        z = -depth + arc * arc / (np.sqrt(a * a + arc * arc) + a)
    else:
        along = np.zeros(n)
        z = arc - depth
    points = along[:, None] * _heading(heading)[None, :]
    points[:, 2] = z
    points[0] = [0, 0, 0]
    points[-1, 2] = -depth
    tensions = np.sqrt(horizontal ** 2 + (weight * arc) ** 2)
    minimum_radius = a if a else None
    result = _base("analytic-inextensible-catenary-v1", [
        "Uniform positive submerged weight; no current, elasticity, bending stiffness or inline bodies.",
        "Flat seabed and zero vertical tangent at touchdown; bottom tension is the horizontal force."])
    if horizontal == 0:
        result["warnings"].append(_warning("VERTICAL_LIMIT", "Zero bottom tension is the vertical limiting solution; touchdown curvature is singular."))
    result.update({"nodes": points.tolist(), "node_tension_n": tensions.tolist(),
                   "frames": [{"time_s": 0.0, "ship": [0.0, 0.0, 0.0], "nodes": points.tolist(),
                               "top_tension_n": float(tensions[0]), "bottom_tension_n": horizontal,
                               "touchdown": points[-1].tolist()}],
                   "summary": {"depth_m": depth, "layback_m": layback,
                               "suspended_length_m": length, "top_tension_n": horizontal + weight * depth,
                               "bottom_tension_n": horizontal, "vertical_top_force_n": weight * length,
                               "minimum_bend_radius_m": minimum_radius,
                               "top_angle_from_horizontal_deg": math.degrees(math.atan2(weight * length, horizontal))},
                   "solver": {"converged": True, "iterations": iterations, "residual_m": abs(points[0, 2])}})
    return result


def _environment(c: dict) -> dict:
    return {"depth": _num(c, "depth_m", 1000, .001, 12000),
            "weight": _num(c, "wet_weight_n_m", 4, 1e-6, 20000),
            "diameter": _num(c, "diameter_m", .02, 1e-4, 2),
            "rho": _num(c, "water_density_kg_m3", 1025, 1, 2000),
            "cd": _num(c, "drag_coefficient", 1.2, 0, 10),
            "speed": _num(c, "ship_speed_m_s", 1.5, 0, 20),
            "payout": _num(c, "payout_m_s", c.get("ship_speed_m_s", 1.5), 0, 25),
            "heading": _num(c, "heading_deg", 90, -36000, 36000),
            "current": np.array([_num(c, "current_x_m_s", 0, -20, 20),
                                 _num(c, "current_y_m_s", 0, -20, 20), 0.0]),
            "bottom": _num(c, "bottom_tension_n", 1000, 0, 1e9)}


def steady_state(config: dict) -> dict:
    """3D static force integration in a translating cable frame.

    Normal Morison drag is independent of material payout speed. The model
    prescribes bottom horizontal tension and integrates until the surface.
    """
    c = _config(config)
    e = _environment(c)
    n = _integer(c, "nodes", 64, 3, 500)
    if e["bottom"] <= 0:
        raise ValueError("steady_state bottom_tension_n must be positive")
    direction = _heading(e["heading"])
    rel = e["current"] - direction * e["speed"]
    q = .5 * e["rho"] * e["cd"] * e["diameter"]

    evaluations = 0
    def ode(s: float, state: np.ndarray) -> np.ndarray:
        nonlocal evaluations
        evaluations += 1
        if evaluations > 20000:
            raise ValueError("steady force integration exceeded its computation limit")
        tension = state[3:]
        norm = np.linalg.norm(tension)
        tangent = tension / max(norm, 1e-10)
        normal = rel - np.dot(rel, tangent) * tangent
        force = q * np.linalg.norm(normal) * normal
        force[2] -= e["weight"]
        return np.r_[tangent, -force]

    def event(s: float, state: np.ndarray) -> float:
        return state[2] - e["depth"]
    event.terminal = True
    event.direction = 1
    initial = np.r_[np.zeros(3), direction * e["bottom"]]
    length_guess = math.sqrt(e["depth"] * (e["depth"] + 2 * e["bottom"] / e["weight"]))
    limit = min(1e6, max(10 * length_guess, 10 * e["depth"]))
    integration = solve_ivp(ode, (0, limit), initial, events=event, dense_output=True,
                            rtol=1e-7, atol=1e-7, max_step=limit / 300)
    if not integration.success or len(integration.t_events[0]) == 0:
        raise ValueError("steady force integration did not reach the surface; reduce current or change tension")
    length = float(integration.t_events[0][0])
    states = integration.sol(np.linspace(length, 0, n)).T
    points = states[:, :3] - states[0, :3]
    points[0] = 0
    tension = np.linalg.norm(states[:, 3:], axis=1)
    bottom = points[-1]
    result = _base("steady-translating-3d-force-integration-v1", [
        "Inextensible cable, flat seabed, homogeneous cable; prescribed horizontal bottom tension.",
        "Steady cable shape translates with the vessel; Morison normal drag uses current minus vessel velocity.",
        "Payout contributes tangential material motion, omitted tangential drag; payout does not alter the force solution.",
        "This is a first ship-plan approximation, not a dynamic calculation or optimized ship plan."])
    slope_slack = 100 * (e["payout"] / e["speed"] - 1) if e["speed"] else None
    if e["speed"] == 0 and e["payout"] > 0:
        result["warnings"].append(_warning("NONSTEADY_PAYOUT", "Positive payout with a stopped vessel has no steady laying solution."))
    if slope_slack is not None and abs(slope_slack) > 5:
        result["warnings"].append(_warning("NONSTEADY_SLACK", "Payout/vessel speed differs by over 5%; this steady shape cannot resolve the accumulating slack or tension."))
    result.update({"nodes": points.tolist(), "node_tension_n": tension.tolist(),
                   "frames": [{"time_s": 0, "ship": [0, 0, 0], "nodes": points.tolist(),
                               "top_tension_n": float(tension[0]), "bottom_tension_n": e["bottom"],
                               "touchdown": bottom.tolist()}],
                   "summary": {"suspended_length_m": length, "layback_m": float(np.linalg.norm(bottom[:2])),
                               "cross_track_offset_m": float(np.dot(bottom, np.array([direction[1], -direction[0], 0]))),
                               "top_tension_n": float(tension[0]), "bottom_tension_n": e["bottom"],
                               "nominal_bottom_slack_pct": slope_slack, "touchdown": bottom.tolist(),
                               "recommended_ship_offset_m": (-bottom[:2]).tolist()},
                   "solver": {"converged": True, "function_evaluations": integration.nfev,
                              "vertical_residual_m": abs(float(states[0, 2]) - e["depth"])}})
    return result


def _profile(c: dict, key: str, max_rows: int = 2000) -> tuple[np.ndarray, np.ndarray] | None:
    rows = c.get(key)
    if rows is None:
        return None
    if not isinstance(rows, list) or not 2 <= len(rows) <= max_rows:
        raise ValueError(f"{key} needs 2 to {max_rows} ordered samples")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{key} samples must be objects")
    if any("x_m" not in row or "depth_m" not in row for row in rows):
        raise ValueError(f"{key} samples require x_m and depth_m")
    x = np.array([_num(row, "x_m", 0, -1e7, 1e7) for row in rows])
    depth = np.array([_num(row, "depth_m", 0, 0, 12000) for row in rows])
    if np.any(np.diff(x) <= 0):
        raise ValueError(f"{key} x_m must be strictly increasing")
    return x, -depth


def _ship_plan(c: dict, e: dict, duration: float) -> list[dict]:
    rows = c.get("ship_plan", [])
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValueError("ship_plan must be an array of at most 500 instructions")
    horizon = _num(c,"ship_plan_horizon_s",duration,duration,1e9)
    plan = [{"time_s": 0., "speed_m_s": e["speed"], "heading_deg": e["heading"], "payout_m_s": e["payout"]}]
    last = -1.0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("ship_plan instructions must be objects")
        if "time_s" not in row:
            raise ValueError("ship_plan instructions require time_s")
        time = _num(row, "time_s", 0, 0, horizon)
        if time <= last:
            raise ValueError("ship_plan times must be strictly increasing")
        previous = plan[-1]
        item = {"time_s": time,
                "speed_m_s": _num(row, "speed_m_s", previous["speed_m_s"], 0, 20),
                "heading_deg": _num(row, "heading_deg", previous["heading_deg"], -36000, 36000),
                "payout_m_s": _num(row, "payout_m_s", previous["payout_m_s"], 0, 25)}
        if time == 0:
            plan[0] = item
        else:
            plan.append(item)
        last = time
    return plan


def _payout_between(plan, start, end):
    return sum(row["payout_m_s"]*max(0., min(end,plan[i+1]["time_s"] if i+1<len(plan) else end)-max(start,row["time_s"]))
               for i,row in enumerate(plan))


def _resumed_plan(raw, config, saved, environment, duration):
    state = saved["state"]
    time = saved["time_s"]
    old_plan = deepcopy(state["plan"])
    index = min(len(old_plan)-1,max(0,np.searchsorted([r["time_s"] for r in old_plan],time+1e-10,side="right")-1))
    active = old_plan[index]
    changes = {key:raw[key] for key in ("ship_speed_m_s","heading_deg","payout_m_s")
               if key in raw}
    replace = "ship_plan" in raw
    if not changes and not replace:
        return old_plan,int(index)
    local_environment = {**environment,"speed":active["speed_m_s"],"heading":active["heading_deg"],"payout":active["payout_m_s"]}
    for key, target in (("ship_speed_m_s","speed"),("heading_deg","heading"),("payout_m_s","payout")):
        if key in changes:
            local_environment[target] = _num(changes,key,local_environment[target],-36000 if target=="heading" else 0,36000 if target=="heading" else (20 if target=="speed" else 25))
    history = [r for r in old_plan if r["time_s"] < time-1e-10]
    if replace:
        local_config = {**config,"ship_plan_horizon_s":raw.get("ship_plan_horizon_s",max(duration,config.get("ship_plan_horizon_s",duration)))}
        local = _ship_plan(local_config,local_environment,duration)
        future = [{**r,"time_s":r["time_s"]+time} for r in local]
    else:
        future = [{"time_s":time,"speed_m_s":local_environment["speed"],"heading_deg":local_environment["heading"],"payout_m_s":local_environment["payout"]}]
        future.extend(r for r in old_plan if r["time_s"] > time+1e-10)
    plan = history+future
    return plan,len(history)


def _current_profile(c: dict, e: dict):
    rows = c.get("current_profile")
    if rows is None:
        return lambda positions: np.broadcast_to(e["current"], positions.shape).copy()
    if not isinstance(rows, list) or not 2 <= len(rows) <= 500:
        raise ValueError("current_profile needs 2 to 500 ordered depth samples")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("current_profile samples must be objects")
    if any("depth_m" not in row for row in rows):
        raise ValueError("current_profile samples require depth_m")
    depths = np.array([_num(row, "depth_m", 0, 0, 12000) for row in rows])
    cx = np.array([_num(row, "x_m_s", 0, -20, 20) for row in rows])
    cy = np.array([_num(row, "y_m_s", 0, -20, 20) for row in rows])
    if np.any(np.diff(depths) <= 0):
        raise ValueError("current_profile depths must be strictly increasing")

    def values(positions):
        depths_at_nodes = np.maximum(-positions[:, 2], 0)
        return np.column_stack([np.interp(depths_at_nodes, depths, cx),
                                np.interp(depths_at_nodes, depths, cy), np.zeros(len(positions))])
    return values


def _cable_defaults(project: dict, config: dict) -> dict:
    if not isinstance(project, dict):
        raise ValueError("project must be an object")
    c = dict(config)
    cables = project.get("cable_types", [])
    if not isinstance(cables, list):
        raise ValueError("project cable_types must be an array")
    selected = c.get("cable_type_id")
    legs = project.get("route", {}).get("legs", []) if isinstance(project.get("route", {}), dict) else []
    if not selected and legs and isinstance(legs[0], dict):
        selected = legs[0].get("cable_type_id")
    cable = next((x for x in cables if isinstance(x, dict) and x.get("id") == selected),
                 cables[0] if cables and isinstance(cables[0], dict) else {})
    for key in ("wet_weight_n_m", "diameter_m", "ea_n", "ei_n_m2", "mass_kg_m", "max_tension_n", "min_bend_radius_m"):
        if key not in c and key in cable:
            c[key] = cable[key]
    return c


class _MaterialModel:
    """Piecewise-constant material properties and consistent nodal body loads.

    Material coordinate increases from oldest bottom cable toward the vessel.
    Bodies add loads to cable; they do not silently remove cable properties.
    """
    def __init__(self, config, environment, ea, ei, dry_mass, added):
        self.origin = _num(config, "initial_suspended_material_m", 0, 0, 1e8)
        self.rho = environment["rho"]
        self.added = added
        defaults = {"wet_weight_n_m": environment["weight"], "diameter_m": environment["diameter"],
                    "mass_kg_m": dry_mass, "ea_n": ea, "ei_n_m2": ei,
                    "drag_coefficient": environment["cd"]}
        raw = config.get("material_segments")
        self.explicit = raw is not None
        if raw is None:
            raw = [{"start_m": 0., "end_m": 1e12, **defaults}]
        elif not isinstance(raw, list) or not 1 <= len(raw) <= 256:
            raise ValueError("material_segments requires 1 to 256 contiguous intervals")
        rows, values = [], []
        for i, item in enumerate(raw):
            if not isinstance(item, dict) or "start_m" not in item or "end_m" not in item:
                raise ValueError("material_segments entries require start_m and end_m")
            start = _num(item, "start_m", 0, 0, 1e12)
            end = _num(item, "end_m", 0, 0, 1e12)
            if end-start < 1e-6:
                raise ValueError("material segment intervals must have positive length of at least 1e-6 m")
            if rows and abs(start-rows[-1]["end_m"]) > 1e-8:
                raise ValueError("material_segments must be ordered and contiguous without gaps or overlaps")
            weight = _num(item, "wet_weight_n_m", defaults["wet_weight_n_m"], 1e-6, 20000)
            diameter = _num(item, "diameter_m", defaults["diameter_m"], 1e-4, 2)
            # A changed weight/diameter needs a corresponding default dry mass.
            derived = weight/G + self.rho*math.pi*diameter**2/4
            mass = _num(item, "mass_kg_m", derived if self.explicit else dry_mass, 0, 50000, strict=True)
            if mass*G <= weight:
                raise ValueError("material mass_kg_m must exceed local wet_weight_n_m / gravity")
            stiffness = _num(item, "ea_n", defaults["ea_n"], 100, 1e12)
            bending = _num(item, "ei_n_m2", defaults["ei_n_m2"], 0, 1e10)
            drag = _num(item, "drag_coefficient", defaults["drag_coefficient"], 0, 10)
            rows.append({"id": str(item.get("id", f"material-{i+1}")), "start_m": start, "end_m": end,
                         "wet_weight_n_m": weight, "diameter_m": diameter, "mass_kg_m": mass,
                         "ea_n": stiffness, "ei_n_m2": bending, "drag_coefficient": drag})
            values.append([weight, mass, self.rho*math.pi*diameter**2/4,
                           diameter*drag, 1/stiffness, bending, diameter])
        self.rows = rows
        self.starts = np.array([r["start_m"] for r in rows])
        self.ends = np.array([r["end_m"] for r in rows])
        self.values = np.array(values)
        self.prefix = np.vstack([np.zeros(7), np.cumsum((self.ends-self.starts)[:, None]*self.values, axis=0)])
        bodies = config.get("inline_bodies", [])
        if not isinstance(bodies, list) or len(bodies) > 128:
            raise ValueError("inline_bodies must be an array of at most 128 entries")
        self.bodies = []
        names = set()
        for i, item in enumerate(bodies):
            required = ("material_m", "mass_kg", "wet_weight_n")
            if not isinstance(item, dict) or any(k not in item for k in required):
                raise ValueError("inline_bodies entries require material_m, mass_kg and wet_weight_n")
            name = str(item.get("id", f"body-{i+1}"))
            if not name or name in names:
                raise ValueError("inline body ids must be nonempty and unique")
            names.add(name)
            material = _num(item, "material_m", 0, self.origin, 1e9)
            length = _num(item, "length_m", 0, 0, 1e6)
            mass = _num(item, "mass_kg", 0, 0, 1e6)
            weight = _num(item, "wet_weight_n", 0, -1e7, 1e7)
            if weight > mass*G:
                raise ValueError("inline body wet_weight_n exceeds dry mass gravity; implied displaced volume is negative")
            area = _num(item, "drag_area_m2", 0, 0, 10000)
            drag = _num(item, "drag_coefficient", 1.2, 0, 10)
            if self.explicit and material+length > self.ends[-1]+1e-8:
                raise ValueError("inline body extent exceeds declared material coverage")
            self.bodies.append({"id": name, "material_m": material, "length_m": length,
                                "mass_kg": mass, "wet_weight_n": weight,
                                "drag_area_m2": area, "drag_coefficient": drag})
        self.bodies.sort(key=lambda body: body["material_m"])
        for a, b in zip(self.bodies, self.bodies[1:]):
            if b["material_m"] < a["material_m"]+a["length_m"]-1e-8 or abs(b["material_m"]-a["material_m"]) <= 1e-8:
                raise ValueError("inline body material intervals must not overlap")

    def validate_coverage(self, required_end):
        if self.starts[0] > self.origin+1e-8 or self.ends[-1] < required_end-1e-8:
            raise ValueError("material_segments must cover the entire initial cable and all requested payout")

    def _integral(self, coordinates):
        index = np.clip(np.searchsorted(self.starts, coordinates, side="right")-1, 0, len(self.starts)-1)
        return self.prefix[index] + (coordinates-self.starts[index])[:, None]*self.values[index]

    @staticmethod
    def _nodes(segment_totals):
        return np.r_[segment_totals[0]/2, (segment_totals[:-1]+segment_totals[1:])/2, segment_totals[-1]/2]

    def loads(self, rest):
        coordinates = self.origin + np.r_[np.cumsum(rest[::-1])[::-1], 0.]
        self.validate_coverage(float(coordinates[0]))
        if len(self.rows) == 1:
            totals = rest[:, None]*self.values[0]
        else:
            integrals = self._integral(coordinates)
            totals = integrals[:-1]-integrals[1:]
        effective = totals[:, 1]+self.added*totals[:, 2]
        mass = self._nodes(effective)
        dry = self._nodes(totals[:, 1])
        weight = self._nodes(totals[:, 0])
        drag = .5*self.rho*self._nodes(totals[:, 3])
        segment_ea = rest/totals[:, 4]
        segment_ei = totals[:, 5]/rest
        left, right = segment_ei[:-1], segment_ei[1:]
        bend_ei = np.zeros(len(rest)-1)
        bending_active = (left>0) & (right>0)
        bend_ei[bending_active] = (rest[:-1]+rest[1:])[bending_active]/(
            rest[:-1][bending_active]/left[bending_active] + rest[1:][bending_active]/right[bending_active])
        body_drag = np.zeros(len(rest)+1)
        body_weights = []
        for body in self.bodies:
            coordinate = body["material_m"]
            weights = np.zeros(len(rest)+1)
            if body["length_m"] == 0:
                if coordinates[-1]-1e-8 <= coordinate <= coordinates[0]+1e-8:
                    j = min(len(rest)-1, max(0, np.searchsorted(-coordinates, -coordinate, side="right")-1))
                    top_fraction = np.clip((coordinate-coordinates[j+1])/rest[j], 0, 1)
                    weights[j], weights[j+1] = top_fraction, 1-top_fraction
            else:
                lower = np.maximum(coordinates[1:], coordinate)
                upper = np.minimum(coordinates[:-1], coordinate+body["length_m"])
                overlap = np.maximum(upper-lower, 0)
                top_share = np.where(overlap>0,
                    ((upper-coordinates[1:])**2-(lower-coordinates[1:])**2)/(2*rest), 0)
                top_share = np.maximum(top_share, 0)
                weights[:-1] += top_share/body["length_m"]
                weights[1:] += (overlap-top_share)/body["length_m"]
            body_effective = body["mass_kg"]+self.added*max(0, body["mass_kg"]-body["wet_weight_n"]/G)
            mass += weights*body_effective
            dry += weights*body["mass_kg"]
            weight += weights*body["wet_weight_n"]
            body_drag += .5*self.rho*body["drag_coefficient"]*body["drag_area_m2"]*weights
            body_weights.append(weights)
        return {"coordinates": coordinates, "mass": mass, "dry_mass": dry, "weight": weight,
                "drag": drag, "body_drag": body_drag, "ea": segment_ea, "ei": bend_ei,
                "segment_ei": segment_ei,
                "segment_weight": totals[:, 0]/rest, "segment_diameter": totals[:, 6]/rest,
                "body_weights": body_weights}

    def body_frames(self, positions, loads, contact):
        output = []
        for body, weights in zip(self.bodies, loads["body_weights"]):
            fraction = float(np.sum(weights))
            position = (weights@positions/fraction).tolist() if fraction>1e-12 else None
            output.append({"id": body["id"], "material_m": body["material_m"],
                           "deployed_fraction": fraction, "position": position,
                           "deployed_mass_kg": fraction*body["mass_kg"],
                           "deployed_wet_weight_n": fraction*body["wet_weight_n"],
                           "contact_fraction": float(np.sum(weights[contact]))/fraction if fraction>1e-12 else 0.})
        return output


def _stretch_project(p: np.ndarray, inv_mass: np.ndarray, rest: np.ndarray,
                     ea: float | np.ndarray, h: float, multipliers: np.ndarray) -> float:
    delta = np.diff(p, axis=0)
    length = np.linalg.norm(delta, axis=1)
    tangent = delta / np.maximum(length[:, None], 1e-12)
    compliance = rest / (ea * h * h)
    constraint = length - rest
    active = (constraint > 0) | (multipliers < 0)
    diagonal = inv_mass[:-1] + inv_mass[1:] + compliance
    off = -inv_mass[1:-1] * np.sum(tangent[:-1] * tangent[1:], axis=1)
    off *= active[:-1] & active[1:]
    band = np.zeros((3, len(rest)))
    band[1] = np.where(active, diagonal, 1.)
    band[0, 1:] = off
    band[2, :-1] = off
    rhs = np.where(active, -constraint - compliance * multipliers, 0.)
    change = solve_banded((1, 1), band, rhs, overwrite_ab=True, overwrite_b=True, check_finite=False)
    proposed = np.minimum(multipliers + change, 0.)
    change = proposed - multipliers
    multipliers[:] = proposed
    corrections = change[:, None] * tangent
    p[:-1] -= inv_mass[:-1, None] * corrections
    p[1:] += inv_mass[1:, None] * corrections
    return float(np.max(np.abs(change)))


def _bend_project(p: np.ndarray, inv_mass: np.ndarray, rest: np.ndarray,
                  ei: np.ndarray, h: float, multipliers: np.ndarray) -> None:
    # Discrete tangent-difference energy EI/(2*mean_length) * |t_next-t_previous|².
    # Derivatives are the small-strain secant approximation, not a rod with torsion.
    for color in range(3):
        j = np.arange(1 + color, len(p) - 1, 3)
        j = j[ei[j-1]>0]
        if not len(j):
            continue
        left, right = rest[j - 1], rest[j]
        a = 1 / left
        b = -(1 / left + 1 / right)
        d = 1 / right
        constraint = a[:, None] * p[j - 1] + b[:, None] * p[j] + d[:, None] * p[j + 1]
        compliance = (left + right) / (2 * ei[j-1] * h * h)
        denom = inv_mass[j - 1] * a*a + inv_mass[j] * b*b + inv_mass[j + 1] * d*d + compliance
        change = (-constraint - compliance[:, None] * multipliers[j - 1]) / denom[:, None]
        multipliers[j - 1] += change
        p[j - 1] += (inv_mass[j - 1] * a)[:, None] * change
        p[j] += (inv_mass[j] * b)[:, None] * change
        p[j + 1] += (inv_mass[j + 1] * d)[:, None] * change


def simulate_lay(project: dict, config: dict, *, state_observer=None) -> dict:
    """Bounded material-node dynamics with surface feed and unilateral seabed contact.

    dt_s is the requested output interval; internal_dt_s is a solver step cap.
    The oldest bottom endpoint is anchored; the model is a finite lay window.
    """
    raw = _config(config)
    if state_observer is not None and not callable(state_observer):
        raise ValueError("state_observer must be a callable internal diagnostic hook")
    merged, saved = merge_resume_config(raw)
    c = _cable_defaults(project, merged)
    e = _environment(c)
    duration = _num(c, "duration_s", 120, 0, 7200, strict=True)
    output_dt = _num(c, "dt_s", 1, .02, 60)
    internal_dt = _num(c, "internal_dt_s", .1, .002, .25)
    n = _integer(c, "nodes", 24, 6, 100)
    iterations = _integer(c, "solver_iterations", 16, 2, 40)
    ea = _num(c, "ea_n", 1e8, 100, 1e12)
    ei = _num(c, "ei_n_m2", 0, 0, 1e10)
    added = _num(c, "added_mass_coefficient", 1, 0, 10)
    displaced_mass = e["rho"] * math.pi * e["diameter"] ** 2 / 4
    dry_mass = _num(c, "mass_kg_m", e["weight"] / G + displaced_mass, 0, 50000, strict=True)
    if dry_mass * G <= e["weight"]:
        raise ValueError("mass_kg_m must exceed wet_weight_n_m / gravity (positive displaced volume)")
    material_model = _MaterialModel(c, e, ea, ei, dry_mass, added)
    damping = _num(c, "damping_ratio", .03, 0, 1)
    friction = _num(c, "seabed_friction", .5, 0, 2)
    heave_amp = _num(c, "heave_amplitude_m", 0, 0, 20)
    heave_period = _num(c, "heave_period_s", 10, .1, 1000)
    max_tension = _num(c, "max_tension_n", 1e12, 0, 1e12, strict=True)
    min_radius = _num(c, "min_bend_radius_m", 0, 0, 10000)
    start_time = saved["time_s"] if saved else 0.
    finish_time = start_time+duration
    if finish_time>1e9:
        raise ValueError("absolute simulation time exceeds 1e9 seconds")
    if saved:
        plan, initial_plan_index = _resumed_plan(raw,c,saved,e,duration)
    else:
        plan, initial_plan_index = _ship_plan(c, e, duration),0
    save_all = c.get("save_checkpoints",False)
    if not isinstance(save_all,bool):
        raise ValueError("save_checkpoints must be boolean")
    requested = c.get("checkpoint_times_s",[])
    if not isinstance(requested,list) or len(requested)>256:
        raise ValueError("checkpoint_times_s must be an array of at most 256 local save times")
    checkpoint_times = [_num({"t":t},"t",0,0,duration)+start_time for t in requested]
    if len(set(checkpoint_times))!=len(checkpoint_times):
        raise ValueError("checkpoint save times must be unique")
    if len(plan)>1000:
        raise ValueError("checkpoint ship-command history exceeds 1000 events; start a new bounded research window")
    currents = _current_profile(c, e)
    from .sea import AiryField, MotionSeries
    motion_rows=c.get("vessel_motion_series")
    motion=MotionSeries(motion_rows) if motion_rows else None
    if motion_rows is not None and not isinstance(motion_rows,list):
        raise ValueError("vessel_motion_series must be an array")
    if motion is not None and heave_amp:
        raise ValueError("choose vessel_motion_series or sinusoidal heave, not both")
    wave_config=c.get("wave_kinematics")
    waves=AiryField(wave_config) if wave_config is not None else None
    if waves is not None and abs(waves.depth-e["depth"])>1e-6:
        raise ValueError("Airy wave reference depth must match the declared cable water depth")
    seabed = _profile(c, "seabed_profile")
    if waves is not None and seabed is not None and (np.ptp(seabed[1])>1e-8 or abs(seabed[1][0]+waves.depth)>1e-6):
        raise ValueError("Airy wave kinematics currently require a flat seabed at the reference water depth")
    def bed(p):
        return np.interp(p[:, 0], *seabed) if seabed is not None else np.full(len(p), -e["depth"])

    if seabed is not None and not saved:
        if float(bed(np.array([[0., 0., 0.]]))[0]) >= -.001:
            raise ValueError("seabed_profile must leave positive water depth at the initial ship position")
        initial_heading = _heading(plan[0]["heading_deg"])
        a = e["bottom"]/e["weight"]
        def initial_depth_residual(d):
            x = -_layback(d, a)*initial_heading[0]
            return d + float(np.interp(x, *seabed))
        try:
            e["depth"] = brentq(initial_depth_residual, .001, 12000, xtol=1e-8)
        except ValueError as error:
            raise ValueError("seabed_profile has no initial horizontal-touchdown catenary within the depth limit") from error
    if saved:
        a, state = saved["arrays"],saved["state"]
        p,v,rest = a["positions"].copy(),a["velocities"].copy(),a["rest_lengths_m"].copy()
        ship,anchor = a["ship"].copy(),a["anchor"].copy()
        initial_length,segment_target = state["initial_material_length_m"],state["segment_target_m"]
        last_tensions = a["last_segment_tensions_n"].copy()
        paid = state["paid_out_m"]
        output_origin = saved["numerical"]["output_grid_origin_s"]
        heave_phase_origin = state["heave_phase_origin_s"]
        heave_offset = state["heave_offset_z_m"]
        if "vessel_motion_series" in raw or any(key in raw and raw[key]!=saved["config"].get(key) for key in ("heave_amplitude_m","heave_period_s")):
            heave_phase_origin,heave_offset = start_time,float(ship[2])
        if np.min(p[:,2]-bed(p)) < -1e-7:
            raise ValueError("checkpoint cable penetrates its declared seabed")
    else:
        initial = catenary({"depth_m": e["depth"], "wet_weight_n_m": e["weight"],
                            "bottom_tension_n": e["bottom"], "nodes": n, "heading_deg": plan[0]["heading_deg"]})
        p = np.array(initial["nodes"], dtype=float)
        if seabed is not None and np.max(bed(p) - p[:, 2]) > 1e-8:
            p[:, 2] = np.maximum(p[:, 2], bed(p))
        v = np.zeros_like(p)
        ship = p[0].copy()
        anchor = p[-1].copy()
        anchor[2] = float(bed(anchor[None, :])[0])
        p[-1] = anchor
        tension_nodes = np.array(initial["node_tension_n"])
        last_tensions = (tension_nodes[:-1]+tension_nodes[1:])/2
        rest = np.linalg.norm(np.diff(p, axis=0), axis=1) / (1 + last_tensions/ea)
        if np.min(rest) < 1e-6:
            raise ValueError("initial mesh contains a zero length segment")
        if material_model.explicit:
            chords = np.linalg.norm(np.diff(p, axis=0), axis=1)
            for _ in range(12):
                local = material_model.loads(rest)
                next_rest = chords/(1+last_tensions/local["ea"])
                if np.max(np.abs(next_rest-rest)) < 1e-10:
                    rest = next_rest
                    break
                rest = next_rest
            else:
                raise ValueError("initial material/axial-compliance coordinate mapping did not converge")
        initial_length = float(np.sum(rest))
        segment_target = float(np.mean(rest))
        paid = 0.
        output_origin = 0.
        heave_phase_origin,heave_offset = 0.,0.
    if motion is not None:
        motion.displacement(finish_time-heave_phase_origin)
    payout_total = _payout_between(plan,start_time,finish_time)
    material_model.validate_coverage(material_model.origin+initial_length+paid+payout_total)
    local = material_model.loads(rest)
    if saved:
        for field,key in (("node_material_m","coordinates"),("node_mass_kg","mass"),("node_dry_mass_kg","dry_mass"),
                          ("node_wet_weight_n","weight"),("segment_ea_n","ea"),("segment_wet_weight_n_m","segment_weight"),
                          ("segment_diameter_m","segment_diameter"),("segment_ei_n_m2","segment_ei"),
                          ("node_cable_drag_factor","drag"),("node_body_drag_factor","body_drag")):
            if not np.allclose(local[key],saved["arrays"][field],rtol=1e-10,atol=1e-8):
                raise ValueError("checkpoint material properties do not match its saved actual state")
        actual_contact = (p[:,2]<=bed(p)+1e-8).tolist()
        if actual_contact != saved["state"]["contact_mask"]:
            raise ValueError("checkpoint seabed contact state is inconsistent")
    predicted_nodes = len(p) + math.ceil(payout_total / segment_target) + 2
    motion_cap=motion.step_cap if motion is not None else internal_dt
    wave_cap=waves.step_cap if waves is not None else internal_dt
    effective_cap=min(internal_dt,output_dt,motion_cap,wave_cap,heave_period/40 if heave_amp else internal_dt)
    step_estimate = math.ceil(duration/effective_cap) + len(plan) + math.ceil(duration/output_dt)
    frame_estimate = math.ceil(duration / output_dt) + len(checkpoint_times) + 2
    if predicted_nodes > 256:
        raise ValueError("payout would exceed 256 material nodes; reduce duration/payout or initial nodes")
    material_work = len(material_model.bodies) if payout_total else 0
    work_estimate = (step_estimate*predicted_nodes*(iterations+material_work)
                     + frame_estimate*predicted_nodes*len(material_model.bodies)
                     +step_estimate*predicted_nodes*6*(int(np.count_nonzero(waves.amplitudes)) if waves is not None else 0))
    max_work=_num(c,"max_work_units",12000000,1,12000000)
    if frame_estimate > 2001 or step_estimate > 30000 or work_estimate > max_work:
        raise ValueError("simulation exceeds computation limit; shorten duration or coarsen dt/nodes")
    checkpoint_estimate = frame_estimate if save_all else len(checkpoint_times)+1
    if checkpoint_estimate>256 or checkpoint_estimate*predicted_nodes>32000:
        raise ValueError("saved checkpoint volume exceeds its limit; select fewer times or reduce duration/nodes")
    result = _base("material-lumped-mass-xpbd-cable-lay-v2", [
        "Newtonian material nodes, submerged weight, normal quadratic Morison drag and isotropic added mass.",
        "Tension-only axial elasticity solved by compliant implicit position constraints; no torsion or fluid acceleration.",
        "Cable enters at the vessel by growing and subdividing the top material element; node count changes with payout.",
        "Oldest bottom endpoint is anchored. This is a finite lay window, not a complete freely sliding trans-oceanic cable.",
        "Seabed contact is impenetrable projection with regularized Coulomb sliding friction; bathymetry may vary in x only.",
        "Cable weight and drag use constant full immersion; changing immersion near the sea surface is omitted.",
        "Piecewise constant ship instructions and optional prescribed heave; no vessel dynamics or controller model.",
        "A fresh initial state is a no-current catenary; continuation restores the actual saved material-node state."])
    if np.any(local["ei"] > 0) or any(row["ei_n_m2"] > 0 for row in material_model.rows):
        result["assumptions"].append("Bending uses a discrete secant tangent-difference energy; it is an approximate isotropic beam, not a full finite-rotation rod.")
    result["material_coordinate_convention"] = "Material coordinate increases from fixed oldest seabed endpoint toward vessel; initial_suspended_material_m is the bottom origin; vessel coordinate advances by payout."
    if material_model.explicit:
        result["assumptions"].append("Piecewise material properties are integrated over each natural-length element; axial EA uses series compliance and bending uses neighboring local stiffness.")
        result["warnings"].append(_warning("HETEROGENEOUS_INITIAL_TRANSIENT", "Initial geometry uses baseline uniform catenary weight with locally adjusted axial prestress; mixed weights and inline-body loads need a startup settling and convergence analysis."))
    if material_model.bodies:
        result["assumptions"].append("Inline bodies add translational mass, submerged weight and isotropic drag through material-coordinate nodal shape functions; finite bodies deploy progressively.")
        result["warnings"].append(_warning("LUMPED_INLINE_BODY", "Inline-body rotation, rigidity, hydrodynamic shape, bend restriction and geometric collision are not resolved; body loads act through cable nodes."))
    if project.get("bodies") and "inline_bodies" not in c:
        result["warnings"].append(_warning("PROJECT_BODIES_NEED_MATERIAL_MAPPING", "Project body route KP is not dynamic material coordinate; supply inline_bodies with measured mass/weight/drag rather than assume a conversion."))
    if len(project.get("cable_types", [])) > 1 and not material_model.explicit:
        result["warnings"].append(_warning("PROJECT_TYPES_NEED_MATERIAL_MAPPING", "Multiple project cable types require explicit material_segments for local dynamic transitions; selected base properties are otherwise uniform."))
    if seabed is not None:
        result["warnings"].append(_warning("BATHYMETRY_EXTRAPOLATION", "Seabed profile is extruded in y and held at the endpoint depth outside its x range."))
        result["warnings"].append(_warning("INITIAL_BATHYMETRY_APPROXIMATION", "Initial catenary touchdown depth is solved from the seabed profile; any interior seabed intersections are projected and will create a startup transient."))
    if saved:
        result["warnings"].append(_warning("CHECKPOINT_RESUMED","Actual saved positions, velocities, material state, boundaries and absolute command clock were restored; frames retain absolute simulation time.","info"))
        if heave_phase_origin == start_time and (heave_phase_origin != saved["state"]["heave_phase_origin_s"] or heave_offset != saved["state"]["heave_offset_z_m"]):
            result["warnings"].append(_warning("RESUMED_HEAVE_CONTROL","New prescribed heave starts at the saved ship height; a new commanded vertical velocity may create a transient."))
    if motion is not None:
        result["assumptions"].append("Cable fairlead vertical motion is a piecewise-linear prescribed time series relative to its saved height; no vessel dynamics is inferred.")
    if waves is not None:
        result["assumptions"].append("Constant-depth Airy particle velocities modify relative drag. Kinematics above mean sea level are capped at surface values; full cable immersion remains assumed.")
        result["warnings"].append(_warning("LINEAR_WAVE_DRAG_ONLY","Wave acceleration/inertia, breaking, radiation/diffraction, changing immersion and vessel response are not solved; user linear kinematics enter drag only."))
    canonical_config = {"duration_s":duration,"dt_s":output_dt,"internal_dt_s":internal_dt,"nodes":n,
        "solver_iterations":iterations,"depth_m":e["depth"],"wet_weight_n_m":e["weight"],
        "diameter_m":e["diameter"],"drag_coefficient":e["cd"],"water_density_kg_m3":e["rho"],
        "ea_n":ea,"ei_n_m2":ei,"mass_kg_m":dry_mass,"added_mass_coefficient":added,
        "bottom_tension_n":e["bottom"],"ship_speed_m_s":e["speed"],"payout_m_s":e["payout"],
        "heading_deg":e["heading"],"current_x_m_s":float(e["current"][0]),"current_y_m_s":float(e["current"][1]),
        "damping_ratio":damping,"seabed_friction":friction,"heave_amplitude_m":heave_amp,
        "heave_period_s":heave_period,"max_tension_n":max_tension,"min_bend_radius_m":min_radius,
        "initial_suspended_material_m":material_model.origin}
    for key in ("material_segments","inline_bodies","seabed_profile","current_profile","cable_type_id","ship_plan","ship_plan_horizon_s","vessel_motion_series","wave_kinematics"):
        if key in c:
            canonical_config[key] = deepcopy(c[key])
    try:
        configuration_bytes=len(json.dumps(canonical_config,ensure_ascii=False,allow_nan=False,separators=(",",":")).encode("utf-8"))
    except (TypeError,ValueError,OverflowError,RecursionError) as error:
        raise ValueError("checkpoint configuration must be finite serializable JSON") from error
    # Motion/profile tables are repeated in a portable standalone checkpoint;
    # the node-count budget alone would not bound the resulting response size.
    if (configuration_bytes+predicted_nodes*1024+len(plan)*256+3000)*checkpoint_estimate>16000000:
        raise ValueError("saved checkpoint JSON volume exceeds 16 MB; select fewer save times or shorten motion/profile tables")
    numerical = {"scheme":"implicit-compliant-material-nodes-v2","output_grid_origin_s":output_origin,
                 "internal_dt_s":internal_dt,"output_dt_s":output_dt,"solver_iterations":iterations}
    frames,checkpoints = [],[]
    stats = saved["state"]["statistics"] if saved else {}
    worst_residual = stats.get("worst_residual_m",0.)
    max_strain = stats.get("max_strain",0.)
    step_count = stats.get("steps",0)
    initial_step_count = step_count
    max_force = stats.get("max_force_n",float(np.max(last_tensions)))
    minimum_radius = stats.get("minimum_radius_m") if saved else None
    minimum_radius = minimum_radius if minimum_radius is not None else math.inf
    max_output_top = stats.get("max_output_top_tension_n",float(last_tensions[0]))
    max_internal_top = stats.get("max_internal_top_tension_n",float(last_tensions[0]))
    interval_max_force = float(np.max(last_tensions))
    interval_max_internal_top = float(last_tensions[0])
    interval_minimum_radius = math.inf
    time = start_time
    plan_index = initial_plan_index

    def frame(t):
        bottoms = bed(p)
        contact = np.flatnonzero(p[:, 2] <= bottoms + 1e-8)
        contact = contact[contact > 0]
        td_index = int(contact[0]) if len(contact) else len(p) - 1
        # Tension on the segment immediately before first bottom contact.
        td_force = float(last_tensions[min(td_index - 1, len(last_tensions)-1)])
        lengths = np.linalg.norm(np.diff(p, axis=0), axis=1)
        return {"time_s": float(t), "ship": p[0].tolist(), "nodes": p.tolist(), "node_velocity_m_s":v.tolist(),
                "top_tension_n": float(last_tensions[0]), "bottom_tension_n": td_force,
                "touchdown": p[td_index].tolist(), "touchdown_node_index": td_index,
                "node_tension_n": np.r_[last_tensions[0], .5*(last_tensions[:-1]+last_tensions[1:]), last_tensions[-1]].tolist(),
                "paid_out_m": paid, "material_length_m": float(np.sum(rest)),
                "geometric_length_m": float(np.sum(lengths)),
                "max_axial_strain": float(np.max(np.maximum(lengths/rest - 1, 0))),
                "contact_nodes": len(contact), "node_material_m": local["coordinates"].tolist(),
                "node_mass_kg": local["mass"].tolist(), "node_wet_weight_n": local["weight"].tolist(),
                "node_dry_mass_kg": local["dry_mass"].tolist(),
                "segment_ea_n": local["ea"].tolist(), "segment_wet_weight_n_m": local["segment_weight"].tolist(),
                "segment_diameter_m": local["segment_diameter"].tolist(),
                "inline_bodies": material_model.body_frames(p, local, contact)}

    def checkpoint():
        state = {"positions":p.tolist(),"velocities":v.tolist(),"rest_lengths_m":rest.tolist(),
            "node_material_m":local["coordinates"].tolist(),"node_mass_kg":local["mass"].tolist(),
            "node_dry_mass_kg":local["dry_mass"].tolist(),"node_wet_weight_n":local["weight"].tolist(),
            "segment_ea_n":local["ea"].tolist(),"segment_wet_weight_n_m":local["segment_weight"].tolist(),
            "segment_ei_n_m2":local["segment_ei"].tolist(),
            "node_cable_drag_factor":local["drag"].tolist(),"node_body_drag_factor":local["body_drag"].tolist(),
            "segment_diameter_m":local["segment_diameter"].tolist(),"ship":ship.tolist(),"anchor":anchor.tolist(),
            "paid_out_m":paid,"initial_material_length_m":initial_length,"segment_target_m":segment_target,
            "last_segment_tensions_n":last_tensions.tolist(),"plan":deepcopy(plan),"plan_index":plan_index,
            "contact_mask":(p[:,2]<=bed(p)+1e-8).tolist(),"heave_phase_origin_s":heave_phase_origin,
            "heave_offset_z_m":heave_offset,"statistics":{"steps":step_count,"max_force_n":max_force,
                "max_strain":max_strain,"worst_residual_m":worst_residual,
                "minimum_radius_m":minimum_radius if math.isfinite(minimum_radius) else None,
                "max_output_top_tension_n":max_output_top,"max_internal_top_tension_n":max_internal_top}}
        return pack_checkpoint(canonical_config,state,time,numerical)

    def observe_state():
        if state_observer is not None:
            # Copies of scalar diagnostics cannot mutate the integrator state.
            state_observer({"time_s": float(time), "node_material_m": local["coordinates"].tolist(),
                            "node_speed_m_s": np.linalg.norm(v, axis=1).tolist(),
                            "contact_mask": (p[:,2] <= bed(p)+1e-8).tolist()})

    observe_state()
    frames.append(frame(start_time))
    if save_all or any(abs(t-start_time)<1e-9 for t in checkpoint_times):
        checkpoints.append(checkpoint())
    first_regular = math.floor((start_time-output_origin)/output_dt+1e-10)+1
    regular = output_origin+np.arange(first_regular,math.ceil((finish_time-output_origin)/output_dt))*output_dt
    output_times = sorted({float(t) for t in regular if start_time<t<finish_time} | {finish_time} | {t for t in checkpoint_times if t>start_time})
    for output_time in output_times:
        while time < output_time - 1e-10:
            while plan_index + 1 < len(plan) and plan[plan_index + 1]["time_s"] <= time + 1e-10:
                plan_index += 1
            instruction = plan[plan_index]
            next_event = plan[plan_index + 1]["time_s"] if plan_index + 1 < len(plan) else finish_time
            h = min(internal_dt,motion_cap,wave_cap,float(output_time)-time, next_event-time,
                    heave_period/40 if heave_amp else internal_dt)
            if h < 1e-10:
                time = next_event
                continue
            speed = instruction["speed_m_s"] * _heading(instruction["heading_deg"])
            ship[:2] += speed[:2] * h
            ship[2] = heave_offset+(motion.displacement(time+h-heave_phase_origin) if motion is not None else heave_amp*math.sin(2*math.pi*(time+h-heave_phase_origin)/heave_period))
            if ship[2] <= float(bed(ship[None, :])[0]):
                raise ValueError("prescribed ship heave intersects the seabed")
            feed = instruction["payout_m_s"] * h
            rest[0] += feed
            paid += feed
            while rest[0] > 2 * segment_target:
                fraction = segment_target / rest[0]
                new = (1-fraction) * p[0] + fraction * p[1]
                new_v = (1-fraction) * v[0] + fraction * v[1]
                p = np.insert(p, 1, new, axis=0)
                v = np.insert(v, 1, new_v, axis=0)
                rest = np.r_[segment_target, rest[0]-segment_target, rest[1:]]
                last_tensions = np.r_[last_tensions[0], last_tensions]
            if feed:
                local = material_model.loads(rest)
            old = p.copy()
            mass = local["mass"]
            inv_mass = 1 / mass
            inv_mass[[0, -1]] = 0
            tangent = np.gradient(p, axis=0)
            tangent /= np.maximum(np.linalg.norm(tangent, axis=1)[:, None], 1e-12)
            fluid=currents(p)+(waves.velocity(p,time+h) if waves is not None else 0)
            relative = fluid - v
            normal = relative - np.sum(relative*tangent, axis=1)[:, None] * tangent
            coefficient = local["drag"]
            normal_speed = np.linalg.norm(normal, axis=1)
            # Semi-implicit normal drag avoids explicit quadratic-drag instability.
            drag_factor = h * coefficient * normal_speed / mass
            v += drag_factor[:, None] * normal / (1 + drag_factor[:, None])
            if np.any(local["body_drag"]):
                body_relative = fluid-v
                body_speed = np.linalg.norm(body_relative, axis=1)
                body_factor = h*local["body_drag"]*body_speed/mass
                v += body_factor[:, None]*body_relative/(1+body_factor[:, None])
            v[:, 2] -= h * local["weight"] / mass
            p += h * v
            p[0], p[-1] = ship, anchor
            multipliers = np.zeros(len(rest))
            bend_multipliers = np.zeros((len(p)-2, 3))
            for _ in range(iterations):
                _stretch_project(p, inv_mass, rest, local["ea"], h, multipliers)
                if np.any(local["ei"] > 0):
                    _bend_project(p, inv_mass, rest, local["ei"], h, bend_multipliers)
                p[1:-1, 2] = np.maximum(p[1:-1, 2], bed(p)[1:-1])
                p[0], p[-1] = ship, anchor
            last_tensions = -multipliers / (h*h)
            lengths = np.linalg.norm(np.diff(p, axis=0), axis=1)
            residual = np.maximum(lengths-rest, 0) - last_tensions*rest/local["ea"]
            worst_residual = max(worst_residual, float(np.max(np.abs(residual))))
            max_strain = max(max_strain, float(np.max(np.maximum(lengths/rest-1, 0))))
            v = (p - old) / h
            # Dissipate only relative axial velocity, preserving rigid translation.
            if damping:
                for color in range(2):
                    j = np.arange(color, len(rest), 2)
                    tangent_seg = (p[j+1]-p[j])/np.maximum(lengths[j, None], 1e-12)
                    rate = np.sum((v[j+1]-v[j])*tangent_seg, axis=1)
                    inverse = inv_mass[j]+inv_mass[j+1]
                    frequency = np.sqrt(local["ea"][j]/rest[j]*inverse)
                    impulse = (1-np.exp(-2*damping*frequency*h))*rate/np.maximum(inverse, 1e-12)
                    v[j] += inv_mass[j, None]*impulse[:, None]*tangent_seg
                    v[j+1] -= inv_mass[j+1, None]*impulse[:, None]*tangent_seg
            touching = p[:, 2] <= bed(p) + 1e-8
            v[touching, 2] = np.maximum(v[touching, 2], 0)
            horizontal = np.linalg.norm(v[:, :2], axis=1)
            friction_decrement = friction * np.maximum(local["weight"], 0) / mass * h
            multiplier = np.maximum(1-friction_decrement/np.maximum(horizontal, 1e-12), 0)
            v[touching, :2] *= multiplier[touching, None]
            v[0] = (ship-old[0])/h
            v[-1] = 0
            directions = np.diff(p, axis=0)/np.maximum(lengths[:, None], 1e-12)
            curvature = np.linalg.norm(np.diff(directions, axis=0), axis=1)/(.5*(lengths[:-1]+lengths[1:]))
            if np.any(curvature > 1e-10):
                minimum_radius = min(minimum_radius, float(1/np.max(curvature)))
                interval_minimum_radius = min(interval_minimum_radius,float(1/np.max(curvature)))
            max_force = max(max_force, float(np.max(last_tensions)))
            max_internal_top = max(max_internal_top,float(last_tensions[0]))
            interval_max_force = max(interval_max_force,float(np.max(last_tensions)))
            interval_max_internal_top = max(interval_max_internal_top,float(last_tensions[0]))
            if not np.all(np.isfinite(p)) or not np.all(np.isfinite(last_tensions)):
                raise ValueError("dynamic solver diverged; shorten internal_dt_s or change mesh/material")
            time += h
            step_count += 1
            observe_state()
        time = float(output_time)
        frames.append(frame(float(output_time)))
        max_output_top = max(max_output_top,float(last_tensions[0]))
        if save_all or any(abs(t-time)<1e-9 for t in checkpoint_times):
            checkpoints.append(checkpoint())
    tolerance = max(1e-5, segment_target*1e-3)
    converged = worst_residual <= tolerance
    if not converged:
        result["warnings"].append(_warning("ITERATION_RESIDUAL", "Some compliant constraint solves exceeded the spatial residual tolerance; refine internal_dt_s/solver_iterations and compare results."))
    if max_strain > .05:
        result["warnings"].append(_warning("HIGH_STRAIN", "Axial strain exceeded 5%; linear elasticity and this mesh may be unsuitable."))
    if max_force > max_tension:
        result["warnings"].append(_warning("MAX_TENSION_EXCEEDED", "Simulated tension exceeded the supplied cable limit."))
    if minimum_radius < min_radius:
        result["warnings"].append(_warning("MIN_RADIUS_EXCEEDED", "Discrete curvature implies a radius below the supplied cable minimum; confirm with a refined bending model."))
    result["warnings"].append(_warning("UNCALIBRATED_DYNAMICS", "Research model: tension, touchdown and curvature require mesh/time convergence and independent laboratory or sea-trial validation."))
    result.update({"frames": frames,"checkpoint":checkpoint(),
                   "summary": {"duration_s": duration,"start_time_s":start_time,"end_time_s":finish_time,"paid_out_m": paid,
                               "initial_material_length_m": initial_length, "final_material_length_m": float(np.sum(rest)),
                               "interval_paid_out_m": paid-(saved["state"]["paid_out_m"] if saved else 0.),
                               "max_top_tension_n": max_output_top,
                               "interval_max_top_tension_n":max(f["top_tension_n"] for f in frames),
                               "max_internal_top_tension_n":max_internal_top,
                               "interval_max_internal_top_tension_n":interval_max_internal_top,
                               "interval_max_tension_n":interval_max_force,
                               "interval_minimum_bend_radius_m":interval_minimum_radius if math.isfinite(interval_minimum_radius) else None,
                               "max_tension_n": max_force, "max_axial_strain": max_strain,
                               "minimum_bend_radius_m": minimum_radius if math.isfinite(minimum_radius) else None,
                               "final_touchdown": frames[-1]["touchdown"], "final_nodes": len(p),
                               "initial_suspended_material_m": material_model.origin,
                               "initial_vessel_material_m": material_model.origin+initial_length,
                               "final_vessel_material_m": float(local["coordinates"][0]),
                               "material_segment_count": len(material_model.rows) if material_model.explicit else 1,
                               "inline_body_count": len(material_model.bodies),
                               "final_total_effective_mass_kg": float(np.sum(local["mass"])),
                               "final_total_dry_mass_kg": float(np.sum(local["dry_mass"])),
                               "final_total_wet_weight_n": float(np.sum(local["weight"])),
                               "material_balance_residual_m": abs(float(np.sum(rest))-initial_length-paid)},
                   "solver": {"converged": converged, "steps": step_count, "iterations_per_step": iterations,
                              "steps_this_run":step_count-initial_step_count,
                              "internal_dt_cap_s": internal_dt, "max_compliance_residual_m": worst_residual,
                              "effective_internal_dt_cap_s":effective_cap,
                              "estimated_work_units": work_estimate,
                              "spatial_residual_tolerance_m": tolerance}})
    if save_all or checkpoint_times:
        result["checkpoints"] = checkpoints
    return result


def span_analysis(config: dict) -> dict:
    """Convex small-slope tensioned-beam obstacle problem on an x/z profile."""
    c = _config(config)
    profile = _profile(c, "profile")
    if profile is None:
        profile = _profile(c, "seabed_profile")
    if profile is None:
        raise ValueError("span_analysis requires profile [{x_m, depth_m}, ...]")
    n = _integer(c, "nodes", 101, 5, 401)
    tension = _num(c, "bottom_tension_n", 1000, 0, 1e9)
    weight = _num(c, "wet_weight_n_m", 4, 1e-6, 20000)
    ei = _num(c, "ei_n_m2", 0, 0, 1e12)
    min_radius = _num(c, "min_bend_radius_m", 0, 0, 10000)
    clearance_tol = _num(c, "span_clearance_m", .01, 0, 10, strict=True)
    x = np.linspace(profile[0][0], profile[0][-1], n)
    length = float(x[-1]-x[0])
    if length < .1 or length > 1e6:
        raise ValueError("span profile horizontal extent must be between .1 and 1000000 metres")
    dx = length/(n-1)
    bottom = np.interp(x, *profile)
    d1 = diags([-np.ones(n-1), np.ones(n-1)], [0, 1], shape=(n-1, n), format="csr")
    d2 = diags([np.ones(n-2), -2*np.ones(n-2), np.ones(n-2)], [0, 1, 2], shape=(n-2, n), format="csr")
    stiffness = (tension/dx*(d1.T@d1) + ei/dx**3*(d2.T@d2)).tocsr()
    load = np.full(n, weight*dx)
    load[[0, -1]] *= .5
    z = bottom.copy()
    active = np.ones(n, dtype=bool)
    converged = False
    iterations = 0
    scale = max(float(np.max(np.abs(stiffness@bottom))) if tension or ei else 0, weight*dx, 1)
    reaction_tolerance = scale*1e-9
    if not tension and not ei:
        converged = True
    else:
        for iterations in range(1, 2001):
            free = ~active
            if np.any(free):
                z[free] = spsolve(stiffness[free][:, free], -load[free]-stiffness[free][:, active]@bottom[active])
            z[active] = bottom[active]
            violating = (z < bottom-1e-8) & free
            if np.any(violating):
                active[violating] = True
                continue
            reaction = stiffness@z + load
            release = active & (reaction < -reaction_tolerance)
            release[[0, -1]] = False
            if np.any(release):
                active[release] = False
                continue
            converged = True
            break
    reaction = stiffness@z + load
    z = np.maximum(z, bottom)
    slope = np.gradient(z, dx, edge_order=2)
    curvature = np.gradient(slope, dx, edge_order=2)/(1+slope*slope)**1.5
    radii = [float(1/abs(k)) if abs(k)>1e-12 else None for k in curvature]
    moment = ei*curvature
    shear = np.gradient(moment, dx, edge_order=2)
    clearance = z-bottom
    suspended = clearance > clearance_tol
    intervals = []
    indices = np.flatnonzero(suspended)
    if len(indices):
        for group in np.split(indices, np.flatnonzero(np.diff(indices)>1)+1):
            first, last = int(group[0]), int(group[-1])
            left = max(first-1, 0)
            right = min(last+1, n-1)
            intervals.append({"start_m": float(x[left]), "end_m": float(x[right]),
                              "length_m": float(x[right]-x[left]),
                              "max_clearance_m": float(np.max(clearance[group]))})
    result = _base("static-tensioned-beam-obstacle-v1", [
        "Small-slope Euler-Bernoulli beam under uniform submerged weight and prescribed horizontal tension.",
        "Cable lies above a rigid 2D seabed; endpoints pinned at profile depths with free rotation.",
        "Convex contact active-set solution; no seabed friction, soil penetration, torsion, VIV, fatigue or material-length constraint.",
        "Vertical reactions are nodal forces; reported tension and curvature include geometric slope corrections only."])
    if not converged:
        result["warnings"].append(_warning("SPAN_NONCONVERGENCE", "Contact active-set solver reached its iteration limit; results need refinement."))
    if np.max(np.abs(slope)) > .3:
        result["warnings"].append(_warning("SMALL_SLOPE_EXCEEDED", "Cable slope exceeds 0.3; the small-slope beam approximation may be inaccurate."))
    if np.min(reaction[[0, -1]]) < -reaction_tolerance:
        result["warnings"].append(_warning("ENDPOINT_HOLD_DOWN", "A pinned endpoint requires downward force; it would lift off with unilateral contact alone."))
    smallest = min((r for r in radii if r is not None), default=None)
    if smallest is not None and smallest < min_radius:
        result["warnings"].append(_warning("MIN_RADIUS_EXCEEDED", "Calculated bend radius is below the supplied minimum."))
    free = clearance > 1e-7
    equilibrium = float(np.max(np.abs(reaction[free]))) if np.any(free) else 0.
    result.update({"nodes": np.column_stack([x, np.zeros(n), z]).tolist(),
                   "profile": [{"x_m": float(x[i]), "depth_m": float(-bottom[i]), "cable_depth_m": float(-z[i]),
                                "clearance_m": float(clearance[i]), "reaction_n": float(reaction[i]),
                                "tension_n": float(tension*math.sqrt(1+slope[i]**2)),
                                "bend_radius_m": radii[i], "moment_n_m": float(moment[i]), "shear_n": float(shear[i])}
                               for i in range(n)], "spans": intervals,
                   "summary": {"span_count": len(intervals), "max_span_m": max((s["length_m"] for s in intervals), default=0.),
                               "max_clearance_m": float(np.max(clearance)), "minimum_bend_radius_m": smallest,
                               "total_vertical_reaction_n": float(np.sum(reaction)), "total_submerged_weight_n": weight*length,
                               "max_reaction_n": float(np.max(reaction)), "max_moment_n_m": float(np.max(np.abs(moment)))},
                   "solver": {"converged": converged, "iterations": iterations,
                              "free_equilibrium_residual_n": equilibrium,
                              "contact_penetration_m": float(max(0, np.max(bottom-z))),
                              "vertical_balance_residual_n": abs(float(np.sum(reaction))-weight*length)}})
    return result
