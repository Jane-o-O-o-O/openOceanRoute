"""Bounded, independent four-boundary affine-seabed catenary calculator.

This is a research inverse-boundary wrapper, not a replacement for the existing
forward or dynamic solvers. All mathematical roots in the declared finite
bottom-tension interval are enumerated before any root may be selected.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math

import numpy as np
from scipy.optimize import brentq

from .bathymetry import BathymetryGrid
from .simulation import _base, _config, _heading, _integer, _num, _warning
from .static_bathymetry import (
    _affine_plane, _asinh_difference, _GAUSS_T, _GAUSS_WEIGHTS, _point,
    _plane_curve, _stretched_length, slope_catenary,
)


_BOTTOM_MIN, _BOTTOM_MAX = 1e-6, 1e9
_EPS = np.finfo(float).eps
_TINY = float(np.nextafter(0., 1.))


class _NumericalFailure(Exception):
    def __init__(self, message, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic


class _Counter:
    def __init__(self, iterations, evaluations):
        self.max_iterations = iterations
        self.max_evaluations = evaluations
        self.evaluations = 0
        self.iterations = 0
        self.solves = 0

    def evaluate(self, function, value):
        if self.evaluations >= self.max_evaluations:
            raise _NumericalFailure("root evaluation budget exhausted")
        self.evaluations += 1
        result = float(function(value))
        if not math.isfinite(result):
            raise _NumericalFailure("root equation produced a nonfinite value")
        return result

    def root(self, function, lower, upper, *, endpoint_tolerance=0.):
        """Finite proven bracket; never extend it or infer roots from scanning."""
        first = self.evaluate(function, lower)
        last = self.evaluate(function, upper)
        if abs(first) <= endpoint_tolerance:
            return float(lower)
        if abs(last) <= endpoint_tolerance:
            return float(upper)
        if (first < 0) == (last < 0) or lower >= upper:
            raise _NumericalFailure("the proven finite bracket does not contain a root")
        self.solves += 1
        try:
            value, report = brentq(lambda u: self.evaluate(function, u), lower, upper,
                                  xtol=_TINY, rtol=8*_EPS, maxiter=self.max_iterations,
                                  full_output=True)
        except (ValueError, RuntimeError) as error:
            raise _NumericalFailure("a bracketed root did not converge within its budget") from error
        self.iterations += int(report.iterations)
        if not report.converged or not math.isfinite(value):
            raise _NumericalFailure("a bracketed root did not converge")
        return float(value)


def _terms(u, m):
    """Stable primitives in u=r-m, including the two peak derivatives Q/J.

    F=integral_0^u t/c(m+t) dt; I=integral_0^u c(m+t) dt.
    Small-u quadrature avoids cancellation and does not erase near-tangent
    boundary inputs by adding an arbitrary angle epsilon.
    """
    r, c0 = m+u, math.hypot(1., m)
    c = math.hypot(1., r)
    if u < .1:
        values = np.hypot(1., m+u*_GAUSS_T)
        f = float(u*u*np.dot(_GAUSS_WEIGHTS, _GAUSS_T/values))
        integral = float(u*np.dot(_GAUSS_WEIGHTS, values))
        q = u*u/c-f
        n = u*c-r*f
        j = u*c-2*integral
    else:
        difference = u*(r+m)/(c+c0)
        angle = float(_asinh_difference(m, u))
        f = difference-m*angle
        integral = .5*(u*c+m*difference+angle)
        # These forms remove O(r^2) subtraction in the derivative expressions.
        q = (m*m-1-2*m*r)/c+c0+m*angle
        n = r*(c0+m*angle)-m*c
        j = -m*difference-angle
    return r, c, f, integral, q, n, j


def _positive_quadratic(linear, quadratic, target):
    if linear <= 0 and quadratic <= 0:
        raise _NumericalFailure("positive root is outside the numerically resolved finite domain")
    if quadratic == 0:
        result = target/linear
    else:
        result = 2*target/(linear+math.hypot(linear, 2*math.sqrt(quadratic*target)))
    if not math.isfinite(result) or result <= 0:
        raise _NumericalFailure("positive quadratic root is not finite and positive")
    return result


def _monotone_roots(counter, function, lower, upper, derivative, tolerance):
    """Enumerate on at most two branches of a proved monotone/single-peak law."""
    if lower > upper:
        return [], []
    cuts = [lower, upper]
    if lower < upper:
        d0, d1 = counter.evaluate(derivative, lower), counter.evaluate(derivative, upper)
        if d0 > 0 and d1 < 0:
            cuts.insert(1, counter.root(derivative, lower, upper))
        elif d0 < 0 and d1 > 0:
            raise _NumericalFailure("derivative contradicts the proved single-peak partition")
    roots = []
    values = [counter.evaluate(function, value) for value in cuts]
    if len(cuts) == 3 and abs(values[1]) <= tolerance:
        # Rounded input at or extremely near a maximum cannot prove whether
        # the real curve has zero, one repeated, or two nearly coincident roots.
        raise _NumericalFailure("peak-near boundary is numerically unresolved; no unique root is certified",
                                {"peak_parameter": cuts[1], "peak_residual_m": values[1],
                                 "resolution_tolerance_m": tolerance,
                                 "possible_root_counts": [0, 1, 2]})
    for value, residual in zip(cuts, values):
        if residual == 0:
            roots.append(float(value))
        elif abs(residual) <= tolerance:
            raise _NumericalFailure("finite-domain endpoint root is numerically unresolved")
    for i in range(len(cuts)-1):
        if values[i] == 0 or values[i+1] == 0:
            continue
        if (values[i] < 0) != (values[i+1] < 0):
            roots.append(counter.root(function, cuts[i], cuts[i+1]))
    roots = sorted(set(roots))
    return roots, cuts


def _parse(config):
    c = _config(config)
    allowed = {"seabed_grid", "vessel_position_m", "heading_deg", "wet_weight_n_m",
               "ea_n", "nodes", "plane_tolerance_m", "max_natural_length_m",
               "boundary", "root_policy", "max_root_iterations",
               "max_function_evaluations", "max_work_units", "max_output_bytes"}
    if any(not isinstance(key, str) for key in c) or set(c)-allowed:
        raise ValueError("catenary calculator contains unsupported or duplicate boundary inputs")
    if "seabed_grid" not in c or not isinstance(c.get("boundary"), dict):
        raise ValueError("catenary calculator requires seabed_grid and exactly one boundary object")
    boundary = c["boundary"]
    kind = boundary.get("kind")
    fields = {"bottom_tension": {"kind", "value_n"},
              "top_tension": {"kind", "value_n"},
              "top_angle": {"kind", "value_deg", "reference", "direction"},
              "cable_in_water": {"kind", "value_m", "length_basis"}}
    if not isinstance(kind, str) or kind not in fields or set(boundary) != fields[kind]:
        raise ValueError("boundary must contain precisely the declared fields for one supported kind")
    if kind == "bottom_tension":
        target = _num(boundary, "value_n", 0, _BOTTOM_MIN, _BOTTOM_MAX)
    elif kind == "top_tension":
        target = _num(boundary, "value_n", 0, _BOTTOM_MIN, 1e12)
    elif kind == "top_angle":
        target = _num(boundary, "value_deg", 0, -90, 90)
        if not -90 < target < 90 or boundary["reference"] != "horizontal" or boundary["direction"] != "touchdown_to_vessel":
            raise ValueError("top_angle requires a signed angle in (-90,90), horizontal reference and touchdown_to_vessel direction")
    else:
        target = _num(boundary, "value_m", 0, .001, 1e6)
        if boundary["length_basis"] not in ("natural", "stretched_arc"):
            raise ValueError("cable_in_water length_basis must explicitly be natural or stretched_arc")
    policy = c.get("root_policy", "require_unique")
    if policy not in ("require_unique", "enumerate", "lowest_bottom_tension", "highest_bottom_tension"):
        raise ValueError("root_policy is unsupported")
    grid = BathymetryGrid(c["seabed_grid"])
    vessel = _point(c.get("vessel_position_m", [0., 0., 0.]), "vessel_position_m")
    if vessel[2] > 0:
        raise ValueError("vessel z must not exceed model sea surface zero")
    weight = _num(c, "wet_weight_n_m", 4, 1e-6, 20000)
    ea = None if c.get("ea_n", 1e8) is None else _num(c, "ea_n", 1e8, 100, 1e12)
    heading = _num(c, "heading_deg", 90, -36000, 36000)
    nodes = _integer(c, "nodes", 64, 3, 1000)
    plane_tolerance = _num(c, "plane_tolerance_m", 1e-7, 1e-10, 1e-5)
    maximum_length = _num(c, "max_natural_length_m", 1e6, .001, 1e6)
    iterations = _integer(c, "max_root_iterations", 100, 1, 200)
    evaluations = _integer(c, "max_function_evaluations", 1200, 1, 20000)
    work = _integer(c, "max_work_units", 1000000, 1, 5000000)
    output = _integer(c, "max_output_bytes", 8*1024*1024, 1, 32*1024*1024)
    # Two possible roots plus a selected copy of one complete forward result.
    forward_work = int(grid.z.size+12*(iterations+4)+8*nodes+24)
    estimated_work = int(10*grid.z.size+48*evaluations+2*forward_work+128)
    grid_bytes = len(json.dumps(grid.document, ensure_ascii=False, allow_nan=False).encode())
    estimated_output = int(3*(grid_bytes+30000+2000*nodes)+50000)
    if estimated_work > work:
        raise ValueError("calculator declared computation exceeds max_work_units")
    if estimated_output > output:
        raise ValueError("calculator declared candidate output exceeds max_output_bytes")
    center, zref, gradient, fit_error = _affine_plane(grid, plane_tolerance)
    if np.all(grid.z == grid.z.flat[0]):
        # An exactly constant complete grid has exactly zero grade. Do not
        # invent a tiny slope from least-squares roundoff at a near-bed angle.
        gradient, zref = np.zeros(2), float(grid.z.flat[0])
    bed, _ = grid.evaluate(vessel[None, :])
    depth = float(vessel[2]-(zref+gradient@(vessel[:2]-center)))
    if depth < .001 or vessel[2] <= bed[0]:
        raise ValueError("vessel must be at least .001 m above its known affine seabed")
    grade = float(gradient@_heading(heading)[:2])
    if kind == "top_angle" and target <= math.degrees(math.atan(grade)):
        raise ValueError("top_angle must exceed the actual seabed tangent angle")
    forward = {"seabed_grid": deepcopy(grid.document), "vessel_position_m": vessel.tolist(),
               "heading_deg": heading, "wet_weight_n_m": weight, "ea_n": ea, "nodes": nodes,
               "plane_tolerance_m": plane_tolerance, "max_natural_length_m": maximum_length,
               "max_root_iterations": iterations, "max_work_units": max(1, forward_work)}
    return {"boundary": deepcopy(boundary), "kind": kind, "target": target, "policy": policy,
            "grid": grid, "grade": grade, "depth": depth, "weight": weight,
            "inverse_ea": 0. if ea is None else 1/ea, "maximum_length": maximum_length,
            "counter": _Counter(iterations, evaluations), "forward": forward,
            "plane_error": fit_error, "max_work": work, "estimated_work": estimated_work,
            "max_output": output, "estimated_output": estimated_output, "forward_work": forward_work}


def _enumerate(p):
    """Return mathematical roots across the finite B interval, before coverage."""
    m, d, w, inv = p["grade"], p["depth"], p["weight"], p["inverse_ea"]
    target, kind, counter = p["target"], p["kind"], p["counter"]
    c0 = math.hypot(1., m)
    hmin, hmax = _BOTTOM_MIN/c0, _BOTTOM_MAX/c0
    tolerance = 64*_EPS*max(1., d)
    cuts, roots = [], []
    method = "finite-domain monotone or proved single-peak inverse boundary"
    if kind == "bottom_tension":
        h = target/c0
        # T(s)<=B+w*s implies rise(S)>=w*S^2/[2(B+w*S)].
        # This is a finite physical upper bracket, not adaptive expansion.
        upper = d+math.sqrt(d*d+2*d*target/w)
        def rise(length):
            u = w*length/h
            return h/w*_terms(u, m)[2]+.5*w*inv*length*length-d
        length = counter.root(rise, 0., upper)
        return [{"bottom_tension_n": target, "natural_length_m": length}], [0., upper], "prescribed bottom tension and finite monotone length bracket"
    if kind == "top_angle":
        r = math.tan(math.radians(target))
        u = r-m
        if u <= 0:
            return [], [], "top angle does not exceed the seabed tangent angle"
        _, _, f, _, _, _, _ = _terms(u, m)
        def height(h):
            return h/w*f+inv*h*h*u*u/(2*w)-d
        first, last = counter.evaluate(height, hmin), counter.evaluate(height, hmax)
        cuts = [hmin, hmax]
        if first > tolerance or last < -tolerance:
            return [], cuts, method
        if (first != 0 and abs(first) <= tolerance) or (last != 0 and abs(last) <= tolerance):
            raise _NumericalFailure("angle root at a finite tension-domain endpoint is numerically unresolved")
        h = counter.root(height, hmin, hmax)
        return [{"bottom_tension_n": h*c0, "natural_length_m": h*u/w}], cuts, method
    if kind == "top_tension":
        # |r| <= sqrt((T*c0/Bmin)^2-1); Bmax may exclude a middle interval.
        ratio = target*c0/_BOTTOM_MIN
        if ratio < 1:
            return [], [], method
        rmax = math.sqrt(max(0., (ratio-1)*(ratio+1)))
        lower, upper = max(0., -rmax-m), rmax-m
        if upper <= 0:
            return [], [], method
        domain = [(lower, upper)]
        maximum_ratio = target*c0/_BOTTOM_MAX
        if maximum_ratio > 1:
            rcut = math.sqrt((maximum_ratio-1)*(maximum_ratio+1))
            domain = [(lower, min(upper, -rcut-m)), (max(lower, rcut-m), upper)]
        def height(u):
            _, c, f, _, _, _, _ = _terms(u, m)
            return target/w*(f/c+.5*target*inv*(u/c)**2)-d
        def derivative(u):
            r, c, _, _, _, n, _ = _terms(u, m)
            # Divide by u: the continuous limit is positive at r=m.
            return n*c/u+target*inv*(1+m*r) if u else (1+target*inv)*c0*c0
        for first, last in domain:
            if first > last:
                continue
            found, branches = _monotone_roots(counter, height, first, last, derivative, tolerance)
            cuts.extend(branches)
            for u in found:
                _, c, _, _, _, _, _ = _terms(u, m)
                h = target/c
                roots.append({"bottom_tension_n": h*c0, "natural_length_m": h*u/w})
        return roots, cuts, method
    basis = p["boundary"]["length_basis"]
    if basis == "natural" or inv == 0:
        length = target
        lower, upper = w*length/hmax, w*length/hmin
        def height(u):
            return length*_terms(u, m)[2]/u+.5*w*inv*length*length-d
        def derivative(u):
            if u < .1:
                c = math.hypot(1., m+u)
                return 1/c-float(np.dot(_GAUSS_WEIGHTS, _GAUSS_T/np.hypot(1., m+u*_GAUSS_T)))
            return _terms(u, m)[4]
        found, cuts = _monotone_roots(counter, height, lower, upper, derivative, tolerance)
        return [{"bottom_tension_n": w*length/u*c0, "natural_length_m": length} for u in found], cuts, method
    # A fixed stretched arc has a strictly decreasing H(u), since A_H,A_u>0.
    # Its derivative and finite-domain branch proof are documented separately.
    def arc_at_h(u, h):
        return h/w*(u+inv*h*_terms(u, m)[3])
    def bound(h):
        maximum_u = w*target/h
        value = counter.root(lambda t: arc_at_h(t*maximum_u, h)/target-1., 0., 1.)
        u = maximum_u*value
        if u <= 0:
            raise _NumericalFailure("the finite stretched-arc domain is below numeric resolution")
        return u
    lower, upper = bound(hmax), bound(hmin)
    def horizontal(u):
        integral = _terms(u, m)[3]
        return _positive_quadratic(u, inv*integral, w*target)
    def height(u):
        h = horizontal(u)
        _, _, f, _, _, _, _ = _terms(u, m)
        return h/w*f+.5*inv*h*h*u*u/w-d
    def derivative(u):
        h = horizontal(u)
        _, c, _, _, q, _, j = _terms(u, m)
        if u < .1:
            values = np.hypot(1., m+u*_GAUSS_T)
            q_scaled = 1/c-float(np.dot(_GAUSS_WEIGHTS, _GAUSS_T/values))
            j_scaled = c-2*float(np.dot(_GAUSS_WEIGHTS, values))
            return q_scaled-inv*h*j_scaled/c
        return q-inv*h*u*j/c
    found, cuts = _monotone_roots(counter, height, lower, upper, derivative, tolerance)
    for u in found:
        h = horizontal(u)
        roots.append({"bottom_tension_n": h*c0, "natural_length_m": h*u/w})
    return roots, cuts, "finite stretched-arc constraint and single-peak height branches"


def _boundary_value(boundary, result):
    summary, kind = result["summary"], boundary["kind"]
    if kind == "bottom_tension":
        return summary["bottom_tension_n"], boundary["value_n"], "N"
    if kind == "top_tension":
        return summary["top_tension_n"], boundary["value_n"], "N"
    if kind == "top_angle":
        return summary["top_angle_from_horizontal_deg"], boundary["value_deg"], "degrees"
    key = "natural_length_m" if boundary["length_basis"] == "natural" else "stretched_arc_length_m"
    return summary[key], boundary["value_m"], "m"


def _theory(p, root):
    """Continuous root quantities before any original-grid shape is accepted."""
    bottom, length = root["bottom_tension_n"], root["natural_length_m"]
    horizontal = bottom/math.hypot(1., p["grade"])
    along, _, top, vertical = _plane_curve(length, horizontal, horizontal*p["grade"],
                                          p["weight"], p["inverse_ea"])
    return {"bottom_tension_n": bottom, "horizontal_tension_n": horizontal,
            "top_tension_n": float(top), "natural_length_m": length,
            "stretched_arc_length_m": _stretched_length(length, horizontal, horizontal*p["grade"],
                                                        p["weight"], p["inverse_ea"]),
            "top_angle_from_horizontal_deg": math.degrees(math.atan2(float(vertical), horizontal)),
            "layback_m": float(along), "verification_status": "mathematical_boundary_only"}


def calculate_catenary(config: dict) -> dict:
    """Calculate and independently verify every finite-domain boundary root.

    Invalid syntax/physical inputs and declared preflight budget violations are
    ValueError. Actual root or verification failures return accepted=False.
    No result is selected while root enumeration is incomplete or ambiguous.
    """
    p = _parse(config)
    result = _base("four-boundary-affine-seabed-elastic-catenary-calculator-v1", [
        "Independent uniform positively weighted flexible cable in still water, with linear axial elasticity or explicit inextensible limit.",
        "The entire canonical grid is verified affine; local metres and model sea zero must already be aligned by the user.",
        "The bottom tension is total magnitude, and the horizontal heading runs from tangent touchdown toward vessel.",
        "All mathematical roots in B=[1e-6,1e9] N are enumerated before selection; natural-length cap exclusions remain explicit.",
        "Natural material length and stretched arc length are distinct constraints; a sampled polyline length is neither.",
        "Each candidate uses the unchanged forward model and original grid for force, tangent, clearance and coverage verification.",
        "No current, seabed tail/friction history, bending, bodies, dynamics, stability proof, field calibration or original software equivalence."])
    result.update(accepted=False, boundary=p["boundary"], root_policy=p["policy"],
                  candidates=[], selected=None, summary={})
    complete, failure, failure_diagnostic, cuts = True, None, None, []
    try:
        mathematical, cuts, method = _enumerate(p)
    except _NumericalFailure as error:
        mathematical, complete, failure = [], False, str(error)
        failure_diagnostic = error.diagnostic
        method = "incomplete bounded root enumeration"
    # A root at a branch join is listed once. No merging of merely nearby roots.
    unique = {}
    for root in mathematical:
        bottom = root["bottom_tension_n"]
        if not math.isfinite(bottom):
            complete, failure = False, "inverse root tension is nonfinite"
            continue
        # Roundoff at finite B endpoints is corrected only at machine precision.
        if bottom < _BOTTOM_MIN and bottom >= _BOTTOM_MIN*(1-32*_EPS):
            bottom = _BOTTOM_MIN
        if bottom > _BOTTOM_MAX and bottom <= _BOTTOM_MAX*(1+32*_EPS):
            bottom = _BOTTOM_MAX
        root["bottom_tension_n"] = bottom
        unique[bottom] = root
    forward_evaluations, forward_work = 0, 0
    for index, root in enumerate(sorted(unique.values(), key=lambda value: value["bottom_tension_n"])):
        bottom = root["bottom_tension_n"]
        length = root["natural_length_m"]
        domain = _BOTTOM_MIN <= bottom <= _BOTTOM_MAX and (length is None or length <= p["maximum_length"]*(1+32*_EPS))
        candidate = {"root_id": f"root-{index+1}", "bottom_tension_n": bottom,
                     "natural_length_m": length, "within_declared_domain": bool(domain),
                     "theory": _theory(p, root),
                     "accepted": False, "boundary_residual": None, "result": None,
                     "rejection_codes": [], "messages": []}
        if not domain:
            candidate["rejection_codes"].append("DECLARED_DOMAIN_EXCLUSION")
            candidate["messages"].append("Mathematical B root exceeds the declared natural-length or bottom-tension bounds")
        else:
            forward_work += p["forward_work"]
            try:
                solved = slope_catenary({**p["forward"], "bottom_tension_n": bottom})
                forward_evaluations += solved["solver"]["function_evaluations"]
                candidate["natural_length_m"] = solved["summary"]["natural_length_m"]
                actual, target, units = _boundary_value(p["boundary"], solved)
                error = float(actual-target)
                # The legacy monotone-length root uses a 1e-10 m absolute xtol.
                # Report the check tolerance, rather than pretending exact data.
                scale = max(1., abs(actual), abs(target))
                check_tolerance = max(512*_EPS*scale, 1e-8*scale)
                candidate["boundary_residual"] = {"actual": actual, "target": target,
                                                    "residual": error, "units": units,
                                                    "tolerance": check_tolerance}
                if abs(error) > check_tolerance:
                    candidate["rejection_codes"].append("FORWARD_BOUNDARY_RESIDUAL")
                    candidate["messages"].append("Forward geometry fails the requested inverse boundary")
                if any(position[2] > 1e-9 for position in solved["nodes"]):
                    candidate["rejection_codes"].append("CABLE_ABOVE_MODEL_SEA")
                    candidate["messages"].append("Cable reaches above model sea zero")
                candidate["result"] = solved
                candidate["accepted"] = bool(solved["accepted"] and not candidate["rejection_codes"])
            except ValueError as error:
                message = str(error)
                if "max_natural_length" in message:
                    candidate["within_declared_domain"] = False
                    code = "DECLARED_DOMAIN_EXCLUSION"
                elif "coverage" in message or "NoData" in message:
                    code = "ORIGINAL_GRID_COVERAGE"
                elif "root" in message or "budget" in message:
                    code = "FORWARD_NUMERICAL_FAILURE"
                else:
                    code = "FORWARD_PHYSICAL_VERIFICATION"
                candidate["rejection_codes"].append(code)
                candidate["messages"].append(message)
        result["candidates"].append(candidate)
    eligible = [(i, root) for i, root in enumerate(result["candidates"]) if root["within_declared_domain"]]
    selected_index = None
    if complete and eligible:
        if p["policy"] == "require_unique" and len(eligible) == 1:
            selected_index = eligible[0][0]
        elif p["policy"] == "lowest_bottom_tension":
            selected_index = eligible[0][0]
        elif p["policy"] == "highest_bottom_tension":
            selected_index = eligible[-1][0]
    reason = "enumeration_incomplete" if not complete else "no_declared_domain_root"
    if complete and eligible:
        reason = "enumeration_only" if p["policy"] == "enumerate" else "ambiguous_boundary"
    if selected_index is not None:
        candidate = result["candidates"][selected_index]
        if candidate["accepted"]:
            result["selected"] = {"root_id": candidate["root_id"], "candidate_index": selected_index,
                                  "bottom_tension_n": candidate["bottom_tension_n"], "result": candidate["result"]}
            result["accepted"] = True
            reason = "selected_verified_root"
        else:
            reason = "requested_root_failed_verification"
    result["summary"] = {"mathematical_root_count": len(eligible) if complete else None,
                         "total_bottom_interval_root_count": len(result["candidates"]) if complete else None,
                         "found_bottom_interval_root_count": len(result["candidates"]),
                         "excluded_natural_length_count": sum(not c["within_declared_domain"] for c in result["candidates"]),
                         "usable_root_count": sum(c["accepted"] for c in result["candidates"]),
                         "selected_root_id": result["selected"]["root_id"] if result["selected"] else None}
    result["solver"] = {"root_enumeration_complete": complete, "method": method, "stop_reason": reason,
                        "failure": failure, "failure_diagnostic": failure_diagnostic, **result["summary"],
                        "bottom_tension_domain_n": [_BOTTOM_MIN, _BOTTOM_MAX],
                        "max_natural_length_m": p["maximum_length"], "finite_branch_cuts": cuts,
                        "branch_parameter": "natural_length_m" if p["kind"] == "bottom_tension" else "horizontal_tension_n" if p["kind"] == "top_angle" else "u=V_top/H-grade",
                        "inverse_function_evaluations": p["counter"].evaluations,
                        "inverse_iterations": p["counter"].iterations, "bracketed_solves": p["counter"].solves,
                        "forward_function_evaluations": forward_evaluations,
                        "forward_function_evaluations_basis": "successful forward calls only; each failed call is charged its full declared work bound",
                        "forward_attempts": int(forward_work//p["forward_work"]),
                        "estimated_work_units": p["estimated_work"], "charged_work_units": int(10*p["grid"].z.size+48*p["counter"].evaluations+forward_work+128),
                        "max_work_units": p["max_work"], "estimated_output_bytes": p["estimated_output"],
                        "max_output_bytes": p["max_output"],
                        "work_basis": "bounded primitive/quadrature evaluation units, every candidate forward preflight (including failed calls), grid fitting and output; normalized units, not FLOPs"}
    if not result["accepted"]:
        result["warnings"].append(_warning("CATENARY_ROOT_NOT_SELECTED", "No usable root was selected: "+reason))
    if not complete:
        result["warnings"].append(_warning("CATENARY_ENUMERATION_INCOMPLETE", failure or "Root enumeration incomplete"))
    actual_bytes = len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode())
    if actual_bytes > p["max_output"]:
        raise ValueError("calculator actual output exceeds max_output_bytes")
    return result
