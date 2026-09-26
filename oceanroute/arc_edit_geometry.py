"""Rebuild an intrinsic WGS84 radius circle after editing its endpoints.

This is a pure numerical geometry operation. It does not edit a project,
replace an arc by a polyline, or infer any manufacturer's hidden constraints.
See ARC_EDIT_GEOMETRY_NOTES.md for branch and numerical admission limits.
"""
from __future__ import annotations

from copy import deepcopy
import math

from scipy.optimize import brentq

from .geodesy import GEOD, coordinate, finite_number
from .route_geometry import RouteSegment

MODEL = "wgs84-fixed-radius-arc-edit-v1"
_FIELDS = {"type", "schema_version", "center", "radius_m", "start_azimuth_deg", "sweep_deg"}


def _point(raw):
    if isinstance(raw, dict) and {"longitude", "latitude"} <= set(raw):
        return coordinate(raw["longitude"], raw["latitude"])
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return coordinate(*raw)
    raise ValueError("arc endpoints must be [longitude, latitude] or coordinate objects")


def _integer(raw, name, low, high):
    if isinstance(raw, bool) or not isinstance(raw, int) or not low <= raw <= high:
        raise ValueError(f"{name} must be an integer in {low}..{high}")
    return raw


def _config(raw):
    raw = {} if raw is None else raw
    fields = {"branch", "full_circle_policy", "radius_m", "original_endpoints", "max_work_units", "max_iterations",
              "radius_tolerance_m", "branch_angle_tolerance_deg"}
    if not isinstance(raw, dict) or set(raw)-fields:
        raise ValueError("arc edit config contains unsupported fields")
    branch = raw.get("branch", "preserve")
    circle = raw.get("full_circle_policy", "reject_move")
    if branch not in ("preserve", "minor", "major", "left", "right"):
        raise ValueError("branch must be preserve, minor, major, left or right")
    if circle not in ("reject_move", "preserve_endpoint_center_bearing"):
        raise ValueError("unsupported full_circle_policy")
    out = {"branch": branch, "full_circle_policy": circle,
           "max_work_units": _integer(raw.get("max_work_units", 20000), "max_work_units", 1, 2_000_000),
           "max_iterations": _integer(raw.get("max_iterations", 96), "max_iterations", 1, 256),
           "radius_tolerance_m": finite_number(raw.get("radius_tolerance_m", 1e-6), "radius_tolerance_m", minimum=1e-8, maximum=1e-3),
           "branch_angle_tolerance_deg": finite_number(raw.get("branch_angle_tolerance_deg", 1e-8), "branch_angle_tolerance_deg", minimum=1e-12, maximum=1e-4)}
    if "radius_m" in raw:
        out["radius_m"] = finite_number(raw["radius_m"], "radius_m", minimum=.001, maximum=1_000_000.)
    if "original_endpoints" in raw:
        points = raw["original_endpoints"]
        if not isinstance(points, (list, tuple)) or len(points) != 2:
            raise ValueError("original_endpoints must contain the actual old start and end coordinates")
        out["original_endpoints"] = tuple(_point(x) for x in points)
    return out


class _Rejected(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


class _Work:
    def __init__(self, cap):
        self.cap, self.used, self.direct, self.inverse = cap, 0, 0, 0
        self.primitive = 0

    def charge(self, n=1):
        if self.used+n > self.cap:
            raise _Rejected("ARC_EDIT_WORK_LIMIT", "native geometry work budget exhausted; no fallback or partial geometry")
        self.used += n

    def fwd(self, p, azimuth, distance):
        self.charge(); self.direct += 1
        q = GEOD.fwd(*p, azimuth, distance)
        if any(not math.isfinite(x) for x in q):
            raise _Rejected("ARC_EDIT_NATIVE_UNRESOLVED", "nonfinite WGS84 direct result")
        return float(q[0]), float(q[1])

    def inv(self, p, q):
        self.charge(); self.inverse += 1
        value = GEOD.inv(*p, *q)
        if any(not math.isfinite(x) for x in value):
            raise _Rejected("ARC_EDIT_NATIVE_UNRESOLVED", "nonfinite WGS84 inverse result")
        return tuple(float(x) for x in value)

    def segment(self, a, b, g, tolerance):
        remaining = self.cap-self.used
        if remaining <= 0:
            self.charge()
        # Retain the object if __init__ fails so even failed quadrature work is
        # charged. RouteSegment itself rejects before executing over its cap.
        obj = RouteSegment.__new__(RouteSegment)
        try:
            obj.__init__(a, b, g, config={"max_work_units": remaining,
                         "endpoint_tolerance_m": tolerance})
        except ValueError as error:
            if "max_work_units exceeded" in str(error):
                raise _Rejected("ARC_EDIT_WORK_LIMIT", str(error)) from error
            raise _Rejected("ARC_EDIT_PRIMITIVE_REJECTED", str(error)) from error
        finally:
            n = getattr(obj, "_work", 0)
            self.charge(n); self.primitive += n
        return obj

    def dto(self):
        return {"work_units": self.used, "max_work_units": self.cap,
                "direct_evaluations": self.direct, "inverse_evaluations": self.inverse,
                "route_segment_work_units": self.primitive,
                "work_basis": "actual native WGS84 direct/inverse and RouteSegment reduced-length/inversion evaluations; not FLOPs, tokens or elapsed time"}


def _kind(sweep, tolerance):
    if abs(abs(sweep)-180.) <= tolerance:
        return "semicircle_unresolved"
    return "minor" if abs(sweep) < 180. else "major"


def _side(angle, tolerance):
    value = (angle+180.) % 360.-180.
    if abs(value) <= tolerance or abs(abs(value)-180.) <= tolerance:
        return "on_chord_unresolved"
    return "right" if value > 0 else "left"


def rebuild_arc_endpoints(start_new, end_new, geometry, *, config=None):
    """Return ``accepted/geometry/evidence/budget/rejection_codes``.

    Geometry is the existing exact six-field circular_arc descriptor. The
    default retains radius, sweep sign, and minor/major branch. Config can
    explicitly select minor/major/left/right, override radius, or jointly move
    a closed circle by preserving its old endpoint-to-center bearing. Invalid
    input raises ValueError; a valid but inadmissible numerical edit returns a
    diagnostic with geometry=None. Inputs are never mutated.
    """
    a, b = _point(start_new), _point(end_new)
    c = _config(config)
    if not isinstance(geometry, dict) or set(geometry) != _FIELDS:
        raise ValueError("circular_arc geometry requires exactly its six declared fields")
    if geometry["type"] != "circular_arc" or isinstance(geometry["schema_version"], bool) or not isinstance(geometry["schema_version"], int) or geometry["schema_version"] != 1:
        raise ValueError("unsupported circular_arc type or schema_version")
    old_center = _point(geometry["center"])
    original_radius = finite_number(geometry["radius_m"], "geometry.radius_m", minimum=.001, maximum=1_000_000.)
    old_start_az = finite_number(geometry["start_azimuth_deg"], "geometry.start_azimuth_deg", minimum=-360, maximum=360) % 360.
    old_sweep = finite_number(geometry["sweep_deg"], "geometry.sweep_deg", minimum=-360, maximum=360)
    if old_sweep == 0:
        raise ValueError("geometry.sweep_deg must be nonzero")
    radius = c.get("radius_m", original_radius)
    requested_tolerance = c["radius_tolerance_m"]
    # A millimetre radius must not inherit a micrometre/metre-scale admission
    # tolerance without an explicit relative bound. The primitive's smallest
    # absolute tolerance is 1e-8m; R>=.001m keeps this cap above that floor.
    c["radius_tolerance_m"] = min(requested_tolerance, radius*1e-4)
    direction = 1 if old_sweep > 0 else -1
    w = _Work(c["max_work_units"])
    evidence = {"radius_m": radius, "original_radius_m": original_radius,
                "radius_overridden": radius != original_radius, "branch_policy": c["branch"],
                "requested_radius_tolerance_m": requested_tolerance,
                "effective_radius_tolerance_m": c["radius_tolerance_m"],
                "maximum_relative_radius_binding_tolerance": 1e-4,
                "signed_direction": direction, "old_sweep_deg": old_sweep,
                "original_minor_major": _kind(old_sweep, c["branch_angle_tolerance_deg"]),
                "centers_candidates": [], "root_iterations": 0, "root_function_calls": 0,
                "native_numerical_resolution_m": max(2e-8, 16*math.ulp(2*radius)),
                "full_circle_policy": c["full_circle_policy"]}
    result = {"model": MODEL, "accepted": False, "geometry": None, "evidence": evidence,
              "budget": None, "rejection_codes": [], "warnings": [],
              "assumptions": ["WGS84 intrinsic geodesic-radius circle; no projected circle or chord fallback",
                              "Independent endpoint reconstruction, without manufacturer-specific constraint equivalence",
                              "Native floating point tolerances are numerical admission allowances, not certified rounding intervals"]}
    try:
        old_a = w.fwd(old_center, old_start_az, original_radius)
        old_b = w.fwd(old_center, old_start_az+old_sweep, original_radius)
        old_g = {"type": "circular_arc", "schema_version": 1, "center": list(old_center),
                 "radius_m": original_radius, "start_azimuth_deg": old_start_az, "sweep_deg": old_sweep}
        evidence["original_endpoint_reference_basis"] = "descriptor_direct"
        if "original_endpoints" in c:
            old_refs = c["original_endpoints"]
            errors = [0. if x == y else w.inv(x, y)[2] for x, y in zip((old_a, old_b), old_refs)]
            evidence["original_endpoint_binding_errors_m"] = errors
            if max(errors) > min(requested_tolerance, original_radius*1e-4):
                raise _Rejected("ARC_EDIT_ORIGINAL_ENDPOINT_MISMATCH", "declared old endpoint references do not bind to the original radius circle")
            old_a, old_b = old_refs
            evidence["original_endpoint_reference_basis"] = "independently_bound_original_endpoints"
        evidence["original_endpoints"] = [list(old_a), list(old_b)]
        # PROJ inverse can report tiny nonzero distance for identical binary
        # lon/lat. Exact identity is a stronger certificate than such native
        # roundoff, without admitting any different endpoint via a tolerance.
        same_a = old_a == a or w.inv(old_a, a)[2] == 0.
        same_b = old_b == b or w.inv(old_b, b)[2] == 0.
        changed = not (same_a and same_b) or radius != original_radius or c["branch"] != "preserve"
        chord_az, _, distance = (0., 0., 0.) if a == b else w.inv(a, b)
        evidence["endpoint_separation_m"] = distance
        if abs(old_sweep) == 360. and c["branch"] != "preserve":
            raise _Rejected("ARC_EDIT_FULL_CIRCLE_BRANCH_UNSUPPORTED", "a full circle has no open minor/major or left/right center branch; use preserve")
        if not changed:
            new_g = old_g
            evidence["selection"] = "unchanged_descriptor"
        elif abs(old_sweep) == 360.:
            if distance != 0.:
                raise _Rejected("ARC_EDIT_FULL_CIRCLE_NOT_CLOSED", "a full circle cannot retain different new endpoints; no single-endpoint arc fallback")
            if c["full_circle_policy"] != "preserve_endpoint_center_bearing":
                raise _Rejected("ARC_EDIT_FULL_CIRCLE_POLICY_REQUIRED", "moving a closed circle requires the explicit endpoint-to-center-bearing policy")
            az = w.inv(old_a, old_center)[0]
            center = w.fwd(a, az, radius)
            start_az = w.inv(center, a)[0] % 360.
            new_g = {"type": "circular_arc", "schema_version": 1, "center": list(center),
                     "radius_m": radius, "start_azimuth_deg": start_az, "sweep_deg": old_sweep}
            evidence["preserved_endpoint_to_center_azimuth_deg"] = az % 360.
            evidence["selection"] = "joint_closed_endpoint_move"
        else:
            if distance == 0.:
                raise _Rejected("ARC_EDIT_COINCIDENT_ENDPOINTS", "distinct open-arc endpoints are required; do not manufacture a full circle")
            if distance > 2*radius:
                code = ("ARC_EDIT_INTERSECTION_UNRESOLVED" if distance-2*radius <= evidence["native_numerical_resolution_m"]
                        else "ARC_EDIT_ENDPOINTS_TOO_FAR")
                raise _Rejected(code, "new endpoint separation exceeds the fixed-radius circle diameter")
            if min(distance, 2*radius-distance) <= evidence["native_numerical_resolution_m"]:
                raise _Rejected("ARC_EDIT_INTERSECTION_UNRESOLVED", "coincident or diameter-limit center branches are not numerically separable; no guessed midpoint or branch")
            kind = evidence["original_minor_major"]
            if c["branch"] == "preserve" and kind == "semicircle_unresolved":
                raise _Rejected("ARC_EDIT_BRANCH_REQUIRED", "original near-180-degree arc does not determine a resolvable minor/major branch; select one explicitly")
            old_chord_az = w.inv(old_a, old_b)[0]
            old_to_center_az = w.inv(old_a, old_center)[0]
            evidence["original_center_side"] = _side(old_to_center_az-old_chord_az, c["branch_angle_tolerance_deg"])
            cache = {}
            def residual(theta):
                evidence["root_function_calls"] += 1
                if theta not in cache:
                    center = w.fwd(a, chord_az+theta, radius)
                    cache[theta] = (w.inv(center, b)[2]-radius, center)
                return cache[theta][0]
            f0, f180, fn180 = residual(0.), residual(180.), residual(-180.)
            if not f0 < 0 or not f180 > 0 or not fn180 > 0:
                raise _Rejected("ARC_EDIT_INTERSECTION_UNRESOLVED", "native center-circle bracket did not resolve two admissible half-circle intersections")
            candidates = []
            for lo, hi in ((-180., 0.), (0., 180.)):
                try:
                    theta, info = brentq(residual, lo, hi, xtol=2e-13, rtol=4*math.ulp(1.),
                                          maxiter=c["max_iterations"], full_output=True, disp=False)
                except (RuntimeError, ValueError) as error:
                    raise _Rejected("ARC_EDIT_ROOT_NOT_CONVERGED", str(error)) from error
                evidence["root_iterations"] += int(info.iterations)
                if not info.converged:
                    raise _Rejected("ARC_EDIT_ROOT_NOT_CONVERGED", "fixed-radius center root did not converge")
                residual(theta)
                center = cache[theta][1]
                az0, _, r0 = w.inv(center, a)
                az1, _, r1 = w.inv(center, b)
                sweep = ((az1-az0) % 360.) if direction > 0 else -((az0-az1) % 360.)
                candidate = {"center": list(center), "start_azimuth_deg": az0 % 360.,
                             "sweep_deg": sweep, "center_side": _side(theta, c["branch_angle_tolerance_deg"]),
                             "minor_major": _kind(sweep, c["branch_angle_tolerance_deg"]),
                             "endpoint_radius_errors_m": [abs(r0-radius), abs(r1-radius)],
                             "root_residual_m": cache[theta][0], "root_parameter_deg": theta}
                candidates.append(candidate)
            evidence["centers_candidates"] = deepcopy(candidates)
            if any(max(x["endpoint_radius_errors_m"]) > c["radius_tolerance_m"] or
                   x["sweep_deg"] == 0 or abs(x["sweep_deg"]) >= 360 or
                   x["minor_major"] == "semicircle_unresolved" or
                   x["center_side"] == "on_chord_unresolved" for x in candidates):
                raise _Rejected("ARC_EDIT_INTERSECTION_UNRESOLVED", "candidate branch or radius was not numerically resolved to the declared admission tolerance")
            wanted = kind if c["branch"] == "preserve" else c["branch"]
            selected = [x for x in candidates if (x["minor_major"] == wanted if wanted in ("minor", "major") else x["center_side"] == wanted)]
            if len(selected) != 1:
                raise _Rejected("ARC_EDIT_BRANCH_UNRESOLVED", "declared branch does not select exactly one verified center")
            chosen = selected[0]
            if c["branch"] == "preserve" and chosen["center_side"] != evidence["original_center_side"]:
                raise _Rejected("ARC_EDIT_BRANCH_UNRESOLVED", "minor/major and original center side disagree numerically")
            new_g = {"type": "circular_arc", "schema_version": 1, "center": chosen["center"],
                     "radius_m": radius, "start_azimuth_deg": chosen["start_azimuth_deg"], "sweep_deg": chosen["sweep_deg"]}
            evidence["selection"] = wanted
        errors = [abs(w.inv(new_g["center"], p)[2]-radius) for p in (a, b)]
        if max(errors) > c["radius_tolerance_m"]:
            raise _Rejected("ARC_EDIT_RADIUS_VERIFICATION_FAILED", "new endpoint radii failed independent inverse checks")
        # Native Direct endpoint reconstruction is separately tested by the
        # shared primitive; all its actual integral evaluations are charged.
        segment = w.segment(a, b, new_g, c["radius_tolerance_m"])
        headings = []
        for fraction in (0., 1.):
            before = segment.solver["work_units"]
            segment.config["max_work_units"] = before+w.cap-w.used
            try:
                headings.append(segment.tangent_at_fraction(fraction))
            except ValueError as error:
                if "max_work_units exceeded" in str(error):
                    raise _Rejected("ARC_EDIT_WORK_LIMIT", str(error)) from error
                raise
            finally:
                n = segment.solver["work_units"]-before
                w.charge(n); w.primitive += n
        evidence.update({"new_center": new_g["center"], "new_sweep_deg": new_g["sweep_deg"],
                         "new_minor_major": _kind(new_g["sweep_deg"], c["branch_angle_tolerance_deg"]),
                         "endpoint_radius_errors_m": errors, "end_binding_errors_m": segment.solver["endpoint_error_m"],
                         "length_m": segment.length_m, "length_upper_bound_m": segment.length_upper_bound_m,
                         "start_tangent_deg": headings[0], "end_tangent_deg": headings[1],
                         "segment_solver": segment.solver})
        result.update(accepted=True, geometry=segment.geometry)
    except _Rejected as error:
        result["rejection_codes"] = [error.code]
        evidence["rejection_message"] = error.message
    finally:
        result["budget"] = w.dto()
    return result
