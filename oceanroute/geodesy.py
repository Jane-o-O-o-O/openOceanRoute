"""WGS84 route geometry, independent of HTTP and product-specific formats.

Rhumb distance uses ellipsoidal isometric latitude and the meridian arc from
PROJ's geodesic solver. Along a constant-bearing curve ds = dM / cos(azimuth).
This is not a spherical great-circle approximation. Source references:
https://geographiclib.sourceforge.io/C++/doc/RhumbSolve.1.html
https://pyproj4.github.io/pyproj/stable/api/geod.html
"""
from __future__ import annotations

import math

from pyproj import Geod

WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)
WGS84_E = math.sqrt(WGS84_E2)
GEOD = Geod(ellps="WGS84")


def finite_number(value, field: str, *, minimum=None, maximum=None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是有限数值")
    try:
        value = float(value)
    except OverflowError as error:
        raise ValueError(f"{field} 必须是有限数值") from error
    if not math.isfinite(value):
        raise ValueError(f"{field} 必须是有限数值")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field} 必须不小于 {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field} 必须不大于 {maximum}")
    return value


def coordinate(longitude, latitude) -> tuple[float, float]:
    return (finite_number(longitude, "longitude", minimum=-180, maximum=180),
            finite_number(latitude, "latitude", minimum=-90, maximum=90))


def wrap_longitude(lon: float) -> float:
    if -180.0 <= lon <= 180.0:
        return lon
    result = (lon + 180.0) % 360.0 - 180.0
    return 180.0 if result == -180.0 and lon > 0 else result


def longitude_delta(lon1: float, lon2: float) -> float:
    """Shortest signed longitude arc; retain direction for an exact 180° arc."""
    raw = lon2 - lon1
    if -180.0 <= raw <= 180.0:
        return raw
    d = (raw + 180.0) % 360.0 - 180.0
    return 180.0 if d == -180.0 and raw > 0 else d


def isometric_latitude(phi: float) -> float:
    sine = math.sin(phi)
    return math.asinh(math.tan(phi)) - WGS84_E * math.atanh(WGS84_E * sine)


def _meridian(phi1: float, phi2: float) -> float:
    if phi1 == phi2:
        return 0.0
    _, _, distance = GEOD.inv(0.0, math.degrees(phi1), 0.0, math.degrees(phi2))
    return math.copysign(distance, phi2 - phi1)


def inverse(lon1, lat1, lon2, lat2, curve="rhumb") -> tuple[float, float | None]:
    """Return metres and initial compass bearing in [0, 360); repeated = None."""
    lon1, lat1 = coordinate(lon1, lat1)
    lon2, lat2 = coordinate(lon2, lat2)
    if curve not in ("rhumb", "geodesic"):
        raise ValueError("route.curve 必须为 rhumb 或 geodesic")
    dl = math.radians(longitude_delta(lon1, lon2))
    if abs(lat2 - lat1) < 1e-14 and abs(dl) < 1e-14:
        return 0.0, None
    if curve == "geodesic":
        bearing, _, distance = GEOD.inv(lon1, lat1, lon2, lat2)
        return distance, bearing % 360.0 if distance > 1e-8 else None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    if abs(lat1) == 90 or abs(lat2) == 90:
        if abs(dl) > 1e-14:
            raise ValueError("恒向线不能以非子午方向连接极点，请使用 geodesic")
        return abs(_meridian(p1, p2)), 0.0 if p2 > p1 else 180.0
    dp = p2 - p1
    pm = (p1 + p2) / 2.0
    if abs(dp) < 1e-8 and abs(dp) < 1e-5 * abs(math.cos(pm)):
        # Stable midpoint divided difference, including an exact parallel.
        sinm = math.sin(pm)
        n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sinm * sinm)
        meridian_radius = WGS84_A * (1.0 - WGS84_E2) / (1.0 - WGS84_E2 * sinm * sinm) ** 1.5
        dpsi = dp * (1.0 - WGS84_E2) / (math.cos(pm) * (1.0 - WGS84_E2 * sinm * sinm))
        distance = math.hypot(meridian_radius * dp, n * math.cos(pm) * dl)
    else:
        dpsi = isometric_latitude(p2) - isometric_latitude(p1)
        distance = abs(_meridian(p1, p2) / dpsi) * math.hypot(dl, dpsi)
    bearing = math.degrees(math.atan2(dl, dpsi)) % 360.0
    return distance, bearing


def interpolate(lon1, lat1, lon2, lat2, fraction: float, curve="rhumb") -> tuple[float, float]:
    """Point at a fraction of physical surface length, with dateline wrapping."""
    lon1, lat1 = coordinate(lon1, lat1)
    lon2, lat2 = coordinate(lon2, lat2)
    fraction = finite_number(fraction, "fraction", minimum=0, maximum=1)
    if fraction == 0:
        return lon1, lat1
    if fraction == 1:
        return lon2, lat2
    distance, bearing = inverse(lon1, lat1, lon2, lat2, curve)
    if distance == 0:
        return lon1, lat1
    if curve == "geodesic":
        lon, lat, _ = GEOD.fwd(lon1, lat1, bearing, distance * fraction)
        return lon, lat
    dl = longitude_delta(lon1, lon2)
    if lat2 == lat1:
        return wrap_longitude(lon1 + dl * fraction), lat1 + (lat2 - lat1) * fraction
    # Constant bearing means meridian arc progresses linearly with distance.
    signed_meridian = _meridian(math.radians(lat1), math.radians(lat2))
    north = 0.0 if signed_meridian > 0 else 180.0
    _, lat, _ = GEOD.fwd(lon1, lat1, north, abs(signed_meridian) * fraction)
    if abs(lat1) == 90 or abs(lat2) == 90:
        return lon1, lat
    psi1 = isometric_latitude(math.radians(lat1))
    psi2 = isometric_latitude(math.radians(lat2))
    psif = isometric_latitude(math.radians(lat))
    return wrap_longitude(lon1 + dl * (psif - psi1) / (psi2 - psi1)), lat


def densify(lon1, lat1, lon2, lat2, curve="rhumb", spacing_m=5000.0, maximum=1000):
    distance, _ = inverse(lon1, lat1, lon2, lat2, curve)
    count = max(1, min(maximum, int(math.ceil(distance / spacing_m))))
    return [interpolate(lon1, lat1, lon2, lat2, i / count, curve) for i in range(count + 1)]


def split_antimeridian(coords, curve="rhumb") -> list[list[list[float]]]:
    """Cut rendered geometry at ±180°, as recommended by RFC 7946 §3.1.9.

    Cutting positions are found on the chosen geographic curve, rather than
    guessing a latitude between the two geographic vertices.
    """
    if not coords:
        return []
    segments, current = [], [[float(coords[0][0]), float(coords[0][1])]]
    for a, b in zip(coords, coords[1:]):
        if abs(b[0] - a[0]) <= 180:
            current.append([float(b[0]), float(b[1])])
            continue
        delta = longitude_delta(a[0], b[0])
        if abs(delta) < 1e-14:
            # Two spellings of the same meridian are not a voyage round Earth.
            current[-1][0] = float(b[0])
            if a[1] != b[1]:
                current.append([float(b[0]), float(b[1])])
            continue
        boundary = 180.0 if delta > 0 else -180.0
        lo, hi = 0.0, 1.0
        for _ in range(45):
            fraction = (lo + hi) / 2
            lon, lat = interpolate(*a, *b, fraction, curve)
            unrolled = a[0] + longitude_delta(a[0], lon)
            if (unrolled < boundary) == (delta > 0):
                lo = fraction
            else:
                hi = fraction
        _, latitude = interpolate(*a, *b, (lo + hi) / 2, curve)
        if current[-1] != [boundary, latitude]:
            current.append([boundary, latitude])
        if len(current) > 1:
            segments.append(current)
        current = [[-boundary, latitude], [float(b[0]), float(b[1])]]
    if len(current) > 1:
        segments.append(current)
    return segments
