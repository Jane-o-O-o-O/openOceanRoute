"""Intrinsic WGS84 circular arcs and the unchanged existing straight curves.

A circle is GEOD.fwd(center, alpha, radius), not a polyline or projected
Euclidean circle. GeographicLib's reduced length m12 gives dl=m12*dalpha
(radians). Quadrature and distance inversion share that metric. See
https://geographiclib.sourceforge.io/html/python/geodesics.html .
Numerical error estimates describe the solver, not chart/measurement accuracy.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math

from geographiclib.geodesic import Geodesic
from scipy.integrate import quad

from .geodesy import (GEOD, coordinate, densify, finite_number, interpolate,
                      inverse, longitude_delta, split_antimeridian)

MODEL = "wgs84-geodesic-radius-circle-v1"
MAX_RADIUS_M = 1_000_000.0
_GL = Geodesic.WGS84
_MASK = Geodesic.STANDARD | Geodesic.REDUCEDLENGTH


def _point(value):
    if isinstance(value, dict):
        return coordinate(value.get("longitude"), value.get("latitude"))
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return coordinate(*value)
    raise ValueError("route segment endpoints must be coordinate objects or [longitude, latitude]")


def _integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in {low}..{high}")
    return value


def _config(raw):
    raw = {} if raw is None else raw
    fields = {"endpoint_tolerance_m", "integration_absolute_tolerance_m",
              "integration_relative_tolerance", "max_work_units", "max_samples",
              "inversion_max_iterations"}
    if not isinstance(raw, dict) or set(raw)-fields:
        raise ValueError("route geometry config contains unsupported fields")
    result = {
        "endpoint_tolerance_m": finite_number(raw.get("endpoint_tolerance_m", 1e-4), "endpoint_tolerance_m", minimum=1e-8, maximum=1e-3),
        "integration_absolute_tolerance_m": finite_number(raw.get("integration_absolute_tolerance_m", 1e-7), "integration_absolute_tolerance_m", minimum=1e-9, maximum=1e-3),
        "integration_relative_tolerance": finite_number(raw.get("integration_relative_tolerance", 5e-14), "integration_relative_tolerance", minimum=2e-14, maximum=1e-10),
        "max_work_units": _integer(raw.get("max_work_units", 2_000_000), "max_work_units", 1, 10_000_000),
        "max_samples": _integer(raw.get("max_samples", 10_000), "max_samples", 2, 100_000),
        "inversion_max_iterations": _integer(raw.get("inversion_max_iterations", 32), "inversion_max_iterations", 1, 64),
    }
    return result


def _geometry(raw):
    if not isinstance(raw, dict) or set(raw) != {"type", "schema_version", "center", "radius_m", "start_azimuth_deg", "sweep_deg"}:
        raise ValueError("circular_arc geometry requires only type/schema_version/center/radius_m/start_azimuth_deg/sweep_deg")
    if raw["type"] != "circular_arc" or isinstance(raw["schema_version"], bool) or not isinstance(raw["schema_version"], int) or raw["schema_version"] != 1:
        raise ValueError("unsupported route leg geometry type or schema_version")
    center = _point(raw["center"])
    radius = finite_number(raw["radius_m"], "geometry.radius_m", minimum=.001, maximum=MAX_RADIUS_M)
    start = finite_number(raw["start_azimuth_deg"], "geometry.start_azimuth_deg", minimum=-360, maximum=360) % 360
    sweep = finite_number(raw["sweep_deg"], "geometry.sweep_deg", minimum=-360, maximum=360)
    if sweep == 0:
        raise ValueError("geometry.sweep_deg must be nonzero")
    return {"type": "circular_arc", "schema_version": 1, "center": list(center), "radius_m": radius,
            "start_azimuth_deg": start, "sweep_deg": sweep}


class RouteSegment:
    """One bounded, endpoint-bound geographic segment.

    split/subsegment/reversed return new RouteSegment objects. Their .geometry
    is the persistent per-leg descriptor; .descriptor() also includes endpoints.
    fraction always means physical length fraction, including on an arc.
    Work counts native direct/inverse/interpolate calls, not CPU FLOPs.
    """

    def __init__(self, a, b, geometry=None, *, curve="rhumb", config=None):
        self.start, self.end = _point(a), _point(b)
        if curve not in ("rhumb", "geodesic"):
            raise ValueError("route.curve must be rhumb or geodesic")
        self.curve = curve
        self.config = _config(config)
        self._geometry = None if geometry is None else _geometry(geometry)
        self._work = 0
        self._integral_calls = 0
        self._maximum_error = 0.
        self._inversions = 0
        self._maximum_distance_residual = 0.
        self._fraction_cache = {0.: 0., 1.: 1.}
        self._endpoint_errors = [0., 0.]
        if self._geometry is None:
            self._charge()
            self.length_m, self._bearing = inverse(*self.start, *self.end, curve)
        else:
            self._bearing = None
            for i, (t, endpoint) in enumerate(((0., self.start), (1., self.end))):
                radial = self._radial(t)
                self._charge()
                error = GEOD.inv(radial["lon2"], radial["lat2"], *endpoint)[2]
                if not math.isfinite(error) or error > self.config["endpoint_tolerance_m"]:
                    raise ValueError("circular_arc descriptor does not match its actual route endpoints")
                self._endpoint_errors[i] = float(error)
            self.length_m = self._integral(1.)[0]
            if not math.isfinite(self.length_m) or self.length_m <= 0:
                raise ValueError("circular_arc has no numerically resolved positive length")

    @property
    def geometry(self):
        return deepcopy(self._geometry)

    @property
    def is_arc(self):
        return self._geometry is not None

    @property
    def length_upper_bound_m(self):
        # Positive Gaussian curvature on WGS84 gives reduced length m12<=R
        # before the first conjugate point. Our R<=1000km stays inside that
        # domain. This analytic upper bound does not use quadrature estimates.
        if self.is_arc:
            return math.nextafter(self._geometry["radius_m"]*abs(math.radians(self._geometry["sweep_deg"])), math.inf)
        return self.length_m

    @property
    def position_error_allowance_m(self):
        if not self.is_arc:
            return 0.
        eps = max(self.config["integration_absolute_tolerance_m"],
                  self.config["integration_relative_tolerance"]*self.length_upper_bound_m)
        # Explicit numerical admission allowance, not an analytic directed-
        # rounding proof for PROJ/GeographicLib/SciPy's floating point engines.
        return self.config["endpoint_tolerance_m"]+6*eps+1e-7

    @property
    def solver(self):
        return {"model": MODEL if self.is_arc else self.curve, "work_units": self._work,
                "max_work_units": self.config["max_work_units"], "work_basis": "native WGS84 direct/inverse/interpolate evaluations; not FLOPs",
                "integral_calls": self._integral_calls, "distance_inversions": self._inversions,
                "maximum_quadrature_error_estimate_m": self._maximum_error,
                "maximum_inversion_distance_residual_m": self._maximum_distance_residual,
                "endpoint_error_m": list(self._endpoint_errors),
                "length_upper_bound_m": self.length_upper_bound_m,
                "position_error_allowance_m": self.position_error_allowance_m,
                "position_allowance_basis": "endpoint tolerance plus integration/inversion tolerances and native floating point allowance; not certified rounding interval",
                "length_basis": "integral_of_GeographicLib_reduced_length_over_center_azimuth_radians" if self.is_arc else "existing_geodesy_inverse",
                "position_basis": "WGS84_geodesic_radius_level_set" if self.is_arc else self.curve}

    def _charge(self, units=1):
        if self._work+units > self.config["max_work_units"]:
            raise ValueError("route geometry max_work_units exceeded; no chord fallback or partial result")
        self._work += units

    def _radial(self, angular_fraction):
        self._charge()
        g = self._geometry
        result = _GL.Direct(g["center"][1], g["center"][0],
                            g["start_azimuth_deg"]+g["sweep_deg"]*angular_fraction, g["radius_m"], _MASK)
        if any(not math.isfinite(result[key]) for key in ("lon2", "lat2", "azi2", "m12")) or result["m12"] <= 0:
            raise ValueError("circular_arc reduced length is nonpositive or nonfinite; circle domain is unsupported")
        return result

    def _integral(self, angular_fraction):
        if angular_fraction == 0:
            return 0., 0.
        scale = abs(math.radians(self._geometry["sweep_deg"]))
        self._integral_calls += 1
        value = quad(lambda t: self._radial(t)["m12"]*scale, 0., angular_fraction,
                     epsabs=self.config["integration_absolute_tolerance_m"],
                     epsrel=self.config["integration_relative_tolerance"], limit=64, full_output=1)
        integral, error = float(value[0]), float(value[1])
        if len(value) != 3 or not math.isfinite(integral) or not math.isfinite(error):
            raise ValueError("circular_arc length quadrature did not converge")
        allowance = max(self.config["integration_absolute_tolerance_m"],
                        abs(integral)*self.config["integration_relative_tolerance"])
        if error > allowance:
            raise ValueError("circular_arc quadrature error estimate exceeds the requested numerical tolerance")
        self._maximum_error = max(self._maximum_error, error)
        return integral, error

    def _angular_fraction(self, fraction):
        if fraction in self._fraction_cache:
            return self._fraction_cache[fraction]
        target = fraction*self.length_m
        lo, hi, t = 0., 1., fraction
        tolerance = max(self.config["integration_absolute_tolerance_m"],
                        self.config["integration_relative_tolerance"]*self.length_m)
        self._inversions += 1
        for _ in range(self.config["inversion_max_iterations"]):
            actual, error = self._integral(t)
            residual = actual-target
            if abs(residual)+error <= tolerance*2:
                self._maximum_distance_residual = max(self._maximum_distance_residual, abs(residual))
                if len(self._fraction_cache) < self.config["max_samples"]+2:
                    self._fraction_cache[fraction] = t
                return t
            if residual > 0:
                hi = t
            else:
                lo = t
            derivative = self._radial(t)["m12"]*abs(math.radians(self._geometry["sweep_deg"]))
            next_t = t-residual/derivative
            t = next_t if lo < next_t < hi else (lo+hi)/2
        raise ValueError("circular_arc distance inversion did not converge; no angular-fraction fallback")

    def point_at_fraction(self, fraction):
        fraction = finite_number(fraction, "fraction", minimum=0, maximum=1)
        if fraction == 0:
            return self.start
        if fraction == 1:
            return self.end
        if not self.is_arc:
            self._charge()
            return interpolate(*self.start, *self.end, fraction, self.curve)
        radial = self._radial(self._angular_fraction(fraction))
        return float(radial["lon2"]), float(radial["lat2"])

    def point_at_distance(self, distance_m):
        distance_m = finite_number(distance_m, "distance_m", minimum=0, maximum=self.length_m)
        return self.point_at_fraction(distance_m/self.length_m if self.length_m else 0.)

    def tangent_at_fraction(self, fraction):
        fraction = finite_number(fraction, "fraction", minimum=0, maximum=1)
        if self.is_arc:
            radial = self._radial(self._angular_fraction(fraction))
            return (radial["azi2"]+math.copysign(90., self._geometry["sweep_deg"])) % 360.
        if self.length_m == 0 or self._bearing is None:
            return None
        if self.curve == "rhumb":
            return self._bearing
        if fraction == 0:
            return self._bearing
        if fraction == 1:
            self._charge()
            return float((GEOD.inv(*self.start, *self.end)[1]+180.) % 360.)
        self._charge()
        _, _, azimuth = GEOD.fwd(*self.start, self._bearing, self.length_m*fraction, return_back_azimuth=False)
        return float(azimuth % 360.)

    def tangent_at_distance(self, distance_m):
        distance_m = finite_number(distance_m, "distance_m", minimum=0, maximum=self.length_m)
        return self.tangent_at_fraction(distance_m/self.length_m if self.length_m else 0.)

    def subsegment(self, start_m, end_m):
        start_m = finite_number(start_m, "start_m", minimum=0, maximum=self.length_m)
        end_m = finite_number(end_m, "end_m", minimum=0, maximum=self.length_m)
        if end_m < start_m:
            raise ValueError("subsegment end_m must not precede start_m; use reversed explicitly")
        if self.is_arc and end_m == start_m:
            raise ValueError("a zero-length circular subsegment has no nonzero sweep")
        f0, f1 = (start_m/self.length_m, end_m/self.length_m) if self.length_m else (0., 0.)
        a, b = self.point_at_fraction(f0), self.point_at_fraction(f1)
        geometry = self.geometry
        if geometry:
            t0, t1 = self._angular_fraction(f0), self._angular_fraction(f1)
            geometry["start_azimuth_deg"] = (geometry["start_azimuth_deg"]+geometry["sweep_deg"]*t0) % 360
            geometry["sweep_deg"] *= t1-t0
        return RouteSegment(a, b, geometry, curve=self.curve, config=self.config)

    def split(self, distance_m):
        distance_m = finite_number(distance_m, "distance_m", minimum=0, maximum=self.length_m)
        if self.is_arc and distance_m in (0, self.length_m):
            raise ValueError("circular_arc split requires an interior distance")
        return self.subsegment(0., distance_m), self.subsegment(distance_m, self.length_m)

    def reversed(self):
        geometry = self.geometry
        if geometry:
            geometry["start_azimuth_deg"] = (geometry["start_azimuth_deg"]+geometry["sweep_deg"]) % 360
            geometry["sweep_deg"] = -geometry["sweep_deg"]
        return RouteSegment(self.end, self.start, geometry, curve=self.curve, config=self.config)

    def sample(self, spacing_m=5000., *, max_points=None):
        spacing_m = finite_number(spacing_m, "spacing_m", minimum=.001, maximum=100_000_000)
        maximum = self.config["max_samples"] if max_points is None else _integer(max_points, "max_points", 2, self.config["max_samples"])
        count = max(1, math.ceil(self.length_m/spacing_m))
        if count+1 > maximum:
            raise ValueError("route geometry sample count exceeds max_points; no automatic thinning")
        if self.is_arc and self._work+21*(count-1) > self.config["max_work_units"]:
            raise ValueError("route geometry sampling minimum work exceeds max_work_units before allocating points")
        return [self.point_at_fraction(i/count) for i in range(count+1)]

    def descriptor(self):
        return {"start": list(self.start), "end": list(self.end), "geometry": self.geometry}


def segment_from_leg(a, b, leg=None, curve="rhumb", config=None):
    if leg is not None and not isinstance(leg, dict):
        raise ValueError("route leg options must be objects")
    return RouteSegment(a, b, None if leg is None else leg.get("geometry"), curve=curve, config=config)


def route_segments(project, config=None):
    route = project.get("route") if isinstance(project, dict) else None
    if not isinstance(route, dict) or not isinstance(route.get("points"), list) or not 2 <= len(route["points"]) <= 10_000:
        raise ValueError("route geometry requires 2..10000 WGS84 route points")
    legs = route.get("legs", [])
    if not isinstance(legs, list) or len(legs) > len(route["points"])-1:
        raise ValueError("route.legs must be an options list no longer than the route segment count")
    return [segment_from_leg(a, b, legs[i] if i < len(legs) else None, route.get("curve", "rhumb"), config)
            for i, (a, b) in enumerate(zip(route["points"], route["points"][1:]))]


def route_geometry_signature(project):
    """Geometry-only digest; core owns the legacy-compatible project signature."""
    segments = route_segments(project)
    payload = {"curve": project["route"].get("curve", "rhumb"), "segments": [s.descriptor() for s in segments]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def segment_length(a, b, leg=None, curve="rhumb", config=None):
    return segment_from_leg(a, b, leg, curve, config).length_m


def segment_at_fraction(a, b, fraction, leg=None, curve="rhumb", config=None):
    return segment_from_leg(a, b, leg, curve, config).point_at_fraction(fraction)


def segment_at_distance(a, b, distance_m, leg=None, curve="rhumb", config=None):
    return segment_from_leg(a, b, leg, curve, config).point_at_distance(distance_m)


def segment_tangent(a, b, fraction=0., leg=None, curve="rhumb", config=None):
    return segment_from_leg(a, b, leg, curve, config).tangent_at_fraction(fraction)


def split_segment(a, b, distance_m, leg=None, curve="rhumb", config=None):
    return segment_from_leg(a, b, leg, curve, config).split(distance_m)


def reverse_segment(a, b, leg=None, curve="rhumb", config=None):
    return segment_from_leg(a, b, leg, curve, config).reversed()


def render_route(project, spacing_m=5000., max_vertices=250_000, tolerance_m=1.):
    """Draw original curves without replacing their mathematical descriptors.

    Straight-only paths reproduce the legacy densify/antimeridian operations.
    Arc intervals test quarter/mid/three-quarter points against the geographic
    chord, cap radial rotation at 5 degrees, and cap physical interval length.
    tolerance is this adaptive sampled chord criterion, not a certified global
    Hausdorff error or positional survey accuracy. Dateline cuts are solved on
    the actual circle, rather than on its rendering chords.
    """
    spacing_m = finite_number(spacing_m, "spacing_m", minimum=.001, maximum=100_000_000)
    tolerance_m = finite_number(tolerance_m, "tolerance_m", minimum=1e-5, maximum=10_000)
    max_vertices = _integer(max_vertices, "max_vertices", 2, 10_020_001)
    segments = route_segments(project)
    curve = project["route"].get("curve", "rhumb")
    minimum_vertices = 1+sum(max(1, math.ceil(s.length_m/spacing_m), math.ceil(abs(s._geometry["sweep_deg"])/5))
                             if s.is_arc else max(1, min(1000, math.ceil(s.length_m/spacing_m))) for s in segments)
    if minimum_vertices > max_vertices:
        raise ValueError("route rendering minimum vertex count exceeds max_vertices before refinement")
    if not any(s.is_arc for s in segments):
        coordinates = [list(segments[0].start)]
        for s in segments:
            coordinates.extend([list(p) for p in densify(*s.start, *s.end, curve, spacing_m, 1000)[1:]])
            if len(coordinates) > max_vertices:
                raise ValueError("route rendering exceeds max_vertices")
        pieces = split_antimeridian(coordinates, curve)
        if sum(map(len, pieces)) > max_vertices:
            raise ValueError("antimeridian rendering exceeds max_vertices")
        return {"coordinates": coordinates, "segments": pieces, "render_model": "legacy_straight_densify_and_antimeridian",
                "render_tolerance_m": None}
    coordinates, pieces, current = [list(segments[0].start)], [], [list(segments[0].start)]
    max_error = 0.
    render_count = 1

    def append(point):
        nonlocal render_count
        if current[-1] != point:
            render_count += 1
            if render_count > max_vertices:
                raise ValueError("arc rendering exceeds max_vertices; no thinning or chord fallback")
            current.append(point)

    for s in segments:
        if s.is_arc:
            cache = {0.: s.start, 1.: s.end}

            def point(f):
                if f not in cache:
                    cache[f] = s.point_at_fraction(f)
                return cache[f]

            def refine(f0, f1, depth=0):
                nonlocal max_error
                a, b = point(f0), point(f1)
                error = 0.
                for fraction in (.25, .5, .75):
                    actual = point(f0+(f1-f0)*fraction)
                    s._charge(2)
                    chord = interpolate(*a, *b, fraction, "geodesic")
                    error = max(error, GEOD.inv(*actual, *chord)[2])
                angle = abs(s._geometry["sweep_deg"]*(s._angular_fraction(f1)-s._angular_fraction(f0)))
                if error <= tolerance_m and (f1-f0)*s.length_m <= spacing_m and angle <= 5.:
                    max_error = max(max_error, error)
                    return [(f1, b)]
                if depth >= 32 or len(cache) > 4*max_vertices:
                    raise ValueError("arc rendering refinement/max_vertices limit exceeded; no chord fallback")
                mid = (f0+f1)/2
                return refine(f0, mid, depth+1)+refine(mid, f1, depth+1)

            sampled = [(0., s.start), *refine(0., 1.)]
        else:
            raw = densify(*s.start, *s.end, curve, spacing_m, 1000)
            sampled = [(i/(len(raw)-1), p) for i, p in enumerate(raw)]
        if len(coordinates)+len(sampled)-1 > max_vertices:
            raise ValueError("route rendering coordinates exceed max_vertices")
        for (f0, a), (f1, b) in zip(sampled, sampled[1:]):
            coordinates.append(list(b))
            if abs(b[0]-a[0]) <= 180:
                append(list(b))
                continue
            if not s.is_arc:
                cut = split_antimeridian([a, b], curve)
                if len(cut) == 1:
                    for p in cut[0][1:]:
                        append(p)
                    continue
                for p in cut[0][1:]:
                    append(p)
                if len(current) > 1:
                    pieces.append(current)
                current = [cut[1][0]]
                render_count += 1
                for p in cut[1][1:]:
                    append(p)
                continue
            delta = longitude_delta(a[0], b[0])
            if abs(delta) < 1e-14:
                current[-1][0] = float(b[0])
                append(list(b))
                continue
            boundary = 180. if delta > 0 else -180.
            lo, hi = s._angular_fraction(f0), s._angular_fraction(f1)
            for _ in range(54):
                t = (lo+hi)/2
                radial = s._radial(t)
                unrolled = a[0]+longitude_delta(a[0], radial["lon2"])
                if (unrolled < boundary) == (delta > 0):
                    lo = t
                else:
                    hi = t
            radial = s._radial((lo+hi)/2)
            latitude = float(radial["lat2"])
            append([boundary, latitude])
            if len(current) > 1:
                pieces.append(current)
            current = [[-boundary, latitude]]
            render_count += 1
            append(list(b))
    if len(current) > 1:
        pieces.append(current)
    if render_count > max_vertices:
        raise ValueError("arc antimeridian rendering exceeds max_vertices")
    return {"coordinates": coordinates, "segments": pieces,
            "render_model": "adaptive_true_arc_points_and_true_arc_antimeridian_cuts_with_legacy_straight_segments",
            "render_tolerance_m": tolerance_m, "maximum_sampled_chord_error_m": max_error,
            "tolerance_basis": "quarter/mid/three-quarter checks; <=5deg radial rotation; not a certified continuous Hausdorff bound",
            "geometry_work_units": sum(s.solver["work_units"] for s in segments)}
