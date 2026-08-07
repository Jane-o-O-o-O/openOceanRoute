"""Transponder observation and uniform-current inverse research model.

The observation operator runs the existing steady normal-drag cable solver.
This is a bounded batch inverse problem, not a Kalman filter or ship controller.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math

import numpy as np
from scipy.optimize import least_squares
from scipy.stats import chi2

from .repair import steady_tow
from .simulation import _config, _integer, _num, _warning


def _vector(value, field, size=3, limit=1e9):
    if not isinstance(value, list) or len(value) != size:
        raise ValueError(f"{field} must contain {size} finite numbers")
    return np.array([_num({"v": v}, "v", 0, -limit, limit) for v in value])


def _id(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError(f"{field} must be a nonempty string of at most 128 characters")
    return value


def _parse(config, estimate):
    source = _config(config)
    try:
        size = len(json.dumps(source, allow_nan=False, separators=(",", ":")).encode("utf-8"))
    except (ValueError, TypeError, RecursionError, OverflowError) as error:
        raise ValueError("seismic input must be finite serializable JSON") from error
    if size > 2_000_000:
        raise ValueError("seismic configuration exceeds 2 MB")
    c = deepcopy(source)
    if estimate and "current_m_s" in c:
        raise ValueError("estimate_current uses initial_current_m_s and bounds, not a prescribed current_m_s")
    frame = _config(c.get("reference_frame", {}))
    if frame.get("kind") != "local_enu":
        raise ValueError("reference_frame.kind must explicitly be local_enu")
    if "origin_wgs84" not in frame:
        raise ValueError("reference_frame.origin_wgs84 [longitude,latitude,height] is required")
    origin = _vector(frame["origin_wgs84"], "origin_wgs84", limit=1e5)
    if abs(origin[0]) > 180 or abs(origin[1]) > 90:
        raise ValueError("reference_frame origin must use WGS84 longitude/latitude degrees")
    surface = _num(frame, "sea_surface_z_m", 0, -1e5, 1e5)
    line = deepcopy(_config(c.get("line", {})))
    unsupported = {"material_segments", "inline_bodies", "current_profile", "seabed_profile",
                   "wave_kinematics", "vessel_motion_series", "resume_state", "ship_plan",
                   "suspended_length_m", "initial_suspended_length_m", "retrieved_length_m"}
    if set(line) & unsupported or set(c) & unsupported:
        raise ValueError("seismic steady observations do not support mixed materials, waves, terrain, dynamic states or length-controlled recovery")
    for key in ("current_x_m_s", "current_y_m_s", "ship_speed_m_s", "heading_deg", "bottom_heading_deg"):
        if key in line:
            raise ValueError(f"{key} belongs in current_m_s or snapshot controls, not line")
    for key in ("depth_m", "wet_weight_n_m", "diameter_m", "bottom_tension_n"):
        if key not in line:
            raise ValueError(f"line.{key} is required; observation inversion must not silently invent cable properties")
    _num(line, "depth_m", 100, .001, 12000)
    _num(line, "wet_weight_n_m", 4, 1e-6, 20000)
    _num(line, "diameter_m", .02, 1e-4, 2)
    _num(line, "bottom_tension_n", 1000, 0, 1e9, strict=True)
    _num(line, "drag_coefficient", 1.2, 0, 10)
    _num(line, "water_density_kg_m3", 1025, 1, 2000)
    line.setdefault("drag_coefficient", 1.2)
    line.setdefault("water_density_kg_m3", 1025)
    if _num(line, "ei_n_m2", 0, 0, 1e10) or _num(line, "heave_amplitude_m", 0, 0, 20):
        raise ValueError("seismic observation operator omits bending and heave")
    line["nodes"] = _integer(line, "nodes", 128, 16, 500)
    raw = c.get("snapshots")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 8:
        raise ValueError("snapshots must contain 1 to 8 independent steady records")
    snapshots = []
    names = set()
    count = 0
    last_time = -math.inf
    for i, value in enumerate(raw):
        row = deepcopy(_config(value))
        if set(row) & unsupported:
            raise ValueError("snapshot requests unsupported nonsteady or heterogeneous cable physics")
        name = _id(row.get("id"), "snapshot id")
        if name in names:
            raise ValueError("snapshot ids must be unique")
        names.add(name)
        time = _num(row, "time_s", i, 0, 1e9)
        if time <= last_time:
            raise ValueError("snapshot time_s must strictly increase; times label independent static records, not a dynamic trajectory")
        last_time = time
        position = _vector(row.get("vessel_position_m"), "vessel_position_m")
        if abs(position[2]-surface) > 1e-8:
            raise ValueError("steady fully-immersed snapshots require vessel_position_m.z at declared mean sea_surface_z_m")
        if "ship_speed_m_s" not in row or "heading_deg" not in row:
            raise ValueError("each snapshot must declare known ship_speed_m_s and heading_deg")
        speed = _num(row, "ship_speed_m_s", 0, 0, 20)
        heading = _num(row, "heading_deg", 90, -36000, 36000)
        bottom_heading = _num(row, "bottom_heading_deg", heading, -36000, 36000)
        material_top = _num(row, "top_material_m", 0, 0, 1e9) if "top_material_m" in row else None
        items = row.get("observations")
        if not isinstance(items, list) or not 1 <= len(items) <= 64:
            raise ValueError("each snapshot requires 1 to 64 transponder observations")
        observation_ids = set()
        observations = []
        for item in items:
            o = deepcopy(_config(item))
            oid = _id(o.get("id"), "observation id")
            if oid in observation_ids:
                raise ValueError("observation ids must be unique within a snapshot")
            observation_ids.add(oid)
            if ("arc_from_vessel_m" in o) == ("material_m" in o):
                raise ValueError("provide exactly one arc_from_vessel_m or material_m for each transponder")
            material = None
            if "material_m" in o:
                if material_top is None:
                    raise ValueError("material-coordinate observation requires measured top_material_m")
                material = _num(o, "material_m", 0, 0, 1e9)
                arc = material_top-material
                if arc < 0:
                    raise ValueError("transponder material_m cannot exceed top_material_m")
            else:
                arc = _num(o, "arc_from_vessel_m", 0, 0, 1e6)
            if arc > 1e6:
                raise ValueError("transponder suspended arc exceeds research bounds")
            observed = o.get("position_m")
            if observed is not None:
                if not isinstance(observed, list) or len(observed) != 3:
                    raise ValueError("position_m must be three ENU components, with null only for a missing component")
                observed = [None if v is None else float(_num({"v": v}, "v", 0, -1e9, 1e9)) for v in observed]
            axes = [j for j in range(3) if observed is not None and observed[j] is not None]
            if estimate and not axes:
                raise ValueError("estimation requires at least one measured position component per observation")
            if "sigma_m" in o and "covariance_m2" in o:
                raise ValueError("provide sigma_m or covariance_m2, not both")
            covariance = None
            if "sigma_m" in o:
                sigmas = _vector(o["sigma_m"], "sigma_m", limit=1e5)
                if np.min(sigmas) < 1e-6:
                    raise ValueError("position standard deviations must be at least 1e-6 m")
                if np.max(sigmas)/np.min(sigmas) > 1e6:
                    raise ValueError("position uncertainty anisotropy exceeds the resolvable covariance limit")
                covariance = np.diag(sigmas**2)
            elif "covariance_m2" in o:
                raw_cov = o["covariance_m2"]
                if not isinstance(raw_cov, list) or len(raw_cov) != 3:
                    raise ValueError("covariance_m2 must be a symmetric positive-definite 3x3 matrix")
                covariance = np.array([_vector(v, "covariance row", limit=1e10) for v in raw_cov])
                if not np.allclose(covariance, covariance.T, atol=1e-12, rtol=1e-10):
                    raise ValueError("covariance_m2 must be symmetric")
                eigen = np.linalg.eigvalsh(covariance)
                if np.min(eigen) < 1e-12 or np.max(eigen)/np.min(eigen) > 1e12:
                    raise ValueError("covariance_m2 must be positive definite and numerically resolvable")
            if estimate and covariance is None:
                raise ValueError("each measured transponder requires absolute sigma_m or covariance_m2")
            if covariance is not None and axes:
                cholesky = np.linalg.cholesky(covariance[np.ix_(axes, axes)])
            else:
                cholesky = None
            observations.append({"id": oid, "arc": float(arc), "material": material,
                                 "observed": observed, "axes": axes, "cholesky": cholesky,
                                 "covariance": covariance})
            count += 1
        snapshots.append({"id": name, "time": time, "vessel": position, "speed": speed,
                          "heading": heading, "bottom_heading": bottom_heading,
                          "observations": observations})
    if count > 256:
        raise ValueError("total transponder observations exceed 256")
    c["reference_frame"] = {**frame, "origin_wgs84": origin.tolist(), "sea_surface_z_m": surface}
    c["line"] = deepcopy(line)
    return c, line, snapshots


class _Operator:
    def __init__(self, config, line, snapshots):
        self.line = line
        self.snapshots = snapshots
        self.solve_limit = _integer(config, "max_forward_solves", 400, 1, 2000)
        self.rhs_limit = _integer(config, "max_ode_evaluations", 300000, 100, 3000000)
        self.solves = self.rhs = self.calls = 0
        self.cache = {}

    def evaluate(self, current):
        key = tuple(float(x) for x in current)
        if key in self.cache:
            return self.cache[key]
        output = []
        weighted = []
        self.calls += 1
        for snapshot in self.snapshots:
            if self.solves >= self.solve_limit or self.rhs >= self.rhs_limit:
                raise ValueError("seismic forward-model budget exhausted; reduce snapshots/evaluations or increase the declared bounded budget")
            self.solves += 1
            shape = steady_tow({**self.line, "ship_speed_m_s": snapshot["speed"],
                                "heading_deg": snapshot["heading"], "bottom_heading_deg": snapshot["bottom_heading"],
                                "current_x_m_s": key[0], "current_y_m_s": key[1]})
            self.rhs += shape["solver"]["function_evaluations"]
            if self.rhs > self.rhs_limit:
                raise ValueError("seismic cumulative ODE-evaluation budget exhausted")
            length = shape["summary"]["suspended_length_m"]
            arc_grid = np.linspace(0, length, len(shape["nodes"]))
            nodes = np.array(shape["nodes"])+snapshot["vessel"]
            records = []
            for o in snapshot["observations"]:
                if o["arc"] > length+1e-8:
                    raise ValueError(f"transponder {o['id']} arc is outside the suspended cable at evaluated current {key}; revise sensor mapping or current bounds")
                point = np.array([np.interp(o["arc"], arc_grid, nodes[:, j]) for j in range(3)])
                residual = [None if o["observed"] is None or o["observed"][j] is None else float(point[j]-o["observed"][j]) for j in range(3)]
                whitened = np.linalg.solve(o["cholesky"], np.array(residual, dtype=object)[o["axes"]].astype(float)) if o["cholesky"] is not None else np.empty(0)
                weighted.extend(whitened.tolist())
                records.append({"id": o["id"], "arc_from_vessel_m": o["arc"], "material_m": o["material"],
                                "predicted_position_m": point.tolist(), "observed_position_m": o["observed"],
                                "position_covariance_m2": o["covariance"].tolist() if o["covariance"] is not None else None,
                                "residual_m": residual, "observed_axes": ["xyz"[j] for j in o["axes"]],
                                "whitened_residual": whitened.tolist(),
                                "mahalanobis_norm": float(np.linalg.norm(whitened)) if len(whitened) else None})
            output.append({"id": snapshot["id"], "time_s": snapshot["time"], "vessel_position_m": snapshot["vessel"].tolist(),
                           "ship_speed_m_s": snapshot["speed"], "heading_deg": snapshot["heading"],
                           "bottom_heading_deg": snapshot["bottom_heading"],
                           "nodes": nodes.tolist(), "node_tension_n": shape["node_tension_n"],
                           "touchdown_m": nodes[-1].tolist(), "suspended_length_m": length,
                           "end_forces": shape["end_forces"], "solver": shape["solver"], "observations": records})
        result = np.array(weighted), output
        if len(self.cache) >= 6:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = result
        return result

    def statistics(self):
        return {"current_evaluations": self.calls, "forward_snapshot_solves": self.solves,
                "ode_function_evaluations": self.rhs, "max_forward_solves": self.solve_limit,
                "max_ode_evaluations": self.rhs_limit}


def _base(config, model):
    return {"model": model, "validation_status": "research", "reference_frame": config["reference_frame"],
            "input_config": deepcopy(config),
            "coordinate_convention": "Local ENU metres, X east, Y north, Z up; all vessel and transponder positions share the declared origin. Bearings north 0 clockwise.",
            "assumptions": [
                "Actual uniform-cable flat-bed steady normal-drag equilibrium is the observation operator; no decorative sensor path.",
                "Known submerged cable properties, horizontal bottom traction, ship speed/heading and exact vessel positions; one uniform horizontal current is shared by independent snapshots.",
                "Sensor suspended arc is measured from the vessel. Material coordinates require the contemporaneous vessel material coordinate; route KP is not a substitute.",
                "Sensor positions are interpolated linearly between steady solver nodes; refine nodes relative to observation accuracy.",
                "Declared position covariance describes independent observations; correlated components within an observation are whitened by Cholesky decomposition.",
                "No acoustic range/sound-speed/instrument model, temporal filter, depth-varying current, dynamic assimilation, hard sensor constraint, control feedback or original Seismic equivalence."],
            "warnings": [_warning("RESEARCH_TRANSPONDER_MODEL", "This inverse research model is not calibrated against real transponders, original software or sea trials; covariance excludes cable/model/vessel-location and arc-mapping errors.")]}


def predict_transponders(config: dict) -> dict:
    """Compute actual sensor positions at a declared current, without random noise."""
    c, line, snapshots = _parse(config, False)
    if "current_m_s" not in c:
        raise ValueError("predict_transponders requires current_m_s [east,north]")
    current = _vector(c["current_m_s"], "current_m_s", 2, 20)
    operator = _Operator(c, line, snapshots)
    residual, predictions = operator.evaluate(current)
    result = _base(c, "steady-cable-transponder-observation-operator-v1")
    result.update({"current_m_s": current.tolist(), "snapshots": predictions,
                   "summary": {"snapshot_count": len(predictions),
                               "observation_count": sum(len(s["observations"]) for s in snapshots),
                               "weighted_residual_sum_squares": float(residual@residual) if len(residual) else None},
                   "solver": {"converged": True, **operator.statistics()}})
    return result


def estimate_current(config: dict) -> dict:
    """Bounded weighted nonlinear least squares with local identifiability checks."""
    c, line, snapshots = _parse(config, True)
    initial = _vector(c.get("initial_current_m_s", [0, 0]), "initial_current_m_s", 2, 20)
    limits = _config(c.get("current_bounds_m_s", {"lower": [-2, -2], "upper": [2, 2]}))
    low = _vector(limits.get("lower"), "current lower bounds", 2, 20)
    high = _vector(limits.get("upper"), "current upper bounds", 2, 20)
    if np.any(high <= low) or np.any(initial < low) or np.any(initial > high):
        raise ValueError("current bounds must increase and contain initial_current_m_s")
    maximum = _integer(c, "max_evaluations", 60, 1, 120)
    step = _num(c, "jacobian_step_m_s", 1e-4, 1e-6, .01)
    outlier = _num(c, "outlier_sigma", 5, 1, 100)
    significance = _num(c, "minimum_fit_pvalue", .001, 0, .5)
    rank_tolerance = _num(c, "rank_relative_tolerance", 1e-6, 1e-12, .1)
    operator = _Operator(c, line, snapshots)
    starting_residual = operator.evaluate(initial)[0]

    def residual(current):
        return operator.evaluate(current)[0]

    def jacobian(current, delta=step):
        columns = []
        for j in range(2):
            minus = current.copy(); plus = current.copy()
            minus[j] = max(low[j], current[j]-delta)
            plus[j] = min(high[j], current[j]+delta)
            if plus[j] == minus[j]:
                raise ValueError("current bound interval cannot resolve the finite-difference Jacobian")
            columns.append((residual(plus)-residual(minus))/(plus[j]-minus[j]))
        return np.column_stack(columns)

    fit = least_squares(residual, initial, jac=jacobian, bounds=(low, high), method="trf",
                        loss="linear", max_nfev=maximum, ftol=1e-10, xtol=1e-10, gtol=1e-8)
    weighted, predictions = operator.evaluate(fit.x)
    coarse = jacobian(fit.x)
    fine = jacobian(fit.x, step/2)
    jacobian_change = float(np.linalg.norm(fine-coarse)/max(np.linalg.norm(fine), 1e-12))
    stable = jacobian_change <= .05
    _, singular, vt = np.linalg.svd(fine, full_matrices=False)
    padded = np.r_[singular, np.zeros(2-len(singular))]
    threshold = max(1e-8, padded[0]*rank_tolerance)
    rank = int(np.count_nonzero(padded > threshold))
    identifiable = rank == 2 and stable
    bound_tolerance = np.maximum(1e-6, (high-low)*1e-7)
    active = ["lower" if fit.x[j]-low[j] <= bound_tolerance[j] else "upper" if high[j]-fit.x[j] <= bound_tolerance[j] else None for j in range(2)]
    squared = float(weighted@weighted)
    dof = len(weighted)-rank
    pvalue = float(chi2.sf(squared, dof)) if dof > 0 else None
    outliers = []
    errors = []
    for snapshot in predictions:
        for observation in snapshot["observations"]:
            observation["is_outlier"] = observation["mahalanobis_norm"] > outlier
            if observation["is_outlier"]:
                outliers.append({"snapshot_id": snapshot["id"], "observation_id": observation["id"],
                                 "mahalanobis_norm": observation["mahalanobis_norm"]})
            errors.extend(v for v in observation["residual_m"] if v is not None)
    consistent = not outliers and (pvalue is None or pvalue >= significance)
    accepted = bool(fit.success and identifiable and not any(active) and consistent)
    covariance = None
    standard = None
    if accepted:
        cov = (vt.T/(singular**2))@vt
        if not np.isfinite(cov).all():
            raise ValueError("current covariance exceeded finite numerical bounds")
        covariance = cov.tolist()
        standard = np.sqrt(np.maximum(0, np.diag(cov))).tolist()
    result = _base(c, "weighted-steady-transponder-uniform-current-estimation-v1")
    result["assumptions"].extend([
        "The optimizer finds a local bounded least-squares minimum; no uniqueness or global convergence is guaranteed.",
        "Accepted covariance is inverse J-transpose-J using absolute user measurement covariance, not rescaled to force reduced chi-square to one.",
        "Local Jacobian rank and step refinement assess linear identifiability only; reported covariance is not a true-vessel confidence guarantee."])
    if not fit.success:
        result["warnings"].append(_warning("CURRENT_ESTIMATION_NOT_CONVERGED", "The numerical termination criteria were not met; no accepted estimate or covariance is claimed."))
    if rank < 2:
        result["warnings"].append(_warning("CURRENT_NOT_IDENTIFIABLE", "Transponder observations do not resolve both horizontal-current components locally; covariance is null."))
    if not stable:
        result["warnings"].append(_warning("CURRENT_JACOBIAN_UNSTABLE", "Halving the finite-difference step changed the sensitivity substantially; zero-flow or numerical/local nonlinearity prevents a reliable linear covariance."))
    if any(active):
        result["warnings"].append(_warning("CURRENT_BOUND_ACTIVE", "At least one estimated current component is at its bound; the unconstrained Gaussian covariance is invalid."))
    if not consistent:
        result["warnings"].append(_warning("TRANSPONDER_RESIDUAL_MISMATCH", "Residuals are inconsistent with the declared uncertainties or include outliers; inspect observations, mapping and model assumptions. No accepted covariance is reported."))
    if dof <= 0:
        result["warnings"].append(_warning("NO_RESIDUAL_REDUNDANCY", "There are no residual degrees of freedom for a goodness-of-fit test; more independent measured components are needed."))
    result.update({"estimated_current_m_s": fit.x.tolist(), "initial_current_m_s": initial.tolist(),
                   "parameter_order": ["current_x_m_s", "current_y_m_s"],
                   "current_bounds_m_s": {"lower": low.tolist(), "upper": high.tolist()},
                   "snapshots": predictions, "parameter_covariance_m2_s2": covariance,
                   "parameter_std_m_s": standard, "weighted_jacobian": fine.tolist(),
                   "information_matrix": (fine.T@fine).tolist(), "outliers": outliers,
                   "identifiability": {"rank": rank, "parameter_count": 2, "identifiable": identifiable,
                                       "singular_values": padded.tolist(), "condition_number": float(padded[0]/padded[1]) if rank == 2 else None,
                                       "rank_threshold": threshold, "jacobian_relative_step_change": jacobian_change,
                                       "jacobian_step_m_s": step, "jacobian_stable": stable, "active_bounds": active},
                   "summary": {"estimate_accepted": accepted, "measurement_component_count": len(weighted),
                               "snapshot_count": len(snapshots), "observation_count": sum(len(s["observations"]) for s in snapshots),
                               "residual_rms_m": math.sqrt(float(np.mean(np.square(errors)))),
                               "weighted_residual_sum_squares": squared,
                               "initial_weighted_residual_sum_squares": float(starting_residual@starting_residual),
                               "residual_degrees_of_freedom": dof, "reduced_chi_square": squared/dof if dof > 0 else None,
                               "goodness_of_fit_pvalue": pvalue, "consistent_with_declared_uncertainty": consistent,
                               "outlier_count": len(outliers), "covariance_valid": covariance is not None},
                   "solver": {"converged": bool(fit.success), "status": int(fit.status), "message": str(fit.message),
                              "optimizer_evaluations": int(fit.nfev), "max_evaluations": maximum,
                              "optimality": float(fit.optimality), **operator.statistics()}})
    return result
