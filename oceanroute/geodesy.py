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


def _isometric_difference(phi1: float, phi2: float, delta_phi=None) -> float:
    """Stable ellipsoidal Δψ, including almost east/west rhumb legs.

    Subtracting two absolute isometric latitudes loses the small change.
    The atanh subtraction identity uses a trigonometric divided difference;
    the denominator identity also avoids cancellation near either pole.
    A wide interval whose ratio rounds to one uses the absolute expression,
    where subtractive cancellation is absent.
    """
    delta = phi2-phi1 if delta_phi is None else delta_phi
    if delta == 0:
        return 0.
    midpoint = phi1+delta/2
    half_sine = math.sin(delta/2)
    cosine = math.cos(midpoint)
    difference = 2*cosine*half_sine
    denominator = cosine*cosine+half_sine*half_sine
    ratio = difference/denominator
    spherical = (math.atanh(ratio) if abs(ratio) < 1 else
                 math.asinh(math.tan(phi2))-math.asinh(math.tan(phi1)))
    ellipsoidal_ratio = WGS84_E*difference/(1-WGS84_E2+WGS84_E2*denominator)
    return spherical-WGS84_E*math.atanh(ellipsoidal_ratio)


def _rhumb_psi_per_meridian(phi1: float, phi2: float, *, latitude1=None, latitude2=None) -> float:
    """Smooth Δψ/ΔM divided difference for a near-parallel interval.

    Both derivatives are integrated on the same normalized interval. The
    small latitude difference cancels algebraically, before any division;
    four-point Gauss quadrature replaces subtraction of rounded positions.
    This helper is restricted below to |Δφ| < .001*cos(midlatitude).
    """
    midpoint, half = (phi1+phi2)/2, (phi2-phi1)/2
    nodes = ((.3399810435848563, .6521451548625461),
             (.8611363115940526, .3478548451374538))
    psi_terms, meridian_terms = [], []
    for node, weight in nodes:
        for sign in (-1., 1.):
            phi = midpoint+sign*node*half
            sine, cosine = math.sin(phi), math.cos(phi)
            if latitude1 is not None and abs(latitude1)>45 and latitude1*latitude2>0:
                # Form the small polar complement before converting to
                # radians. Subtracting rounded radians from π/2 loses it.
                fraction=(1+sign*node)/2
                polar_degrees=(90-abs(latitude1))-math.copysign(1.,latitude1)*(latitude2-latitude1)*fraction
                theta=math.radians(polar_degrees)
                sine, cosine = math.copysign(math.cos(theta),latitude1), math.sin(theta)
            scale = 1-WGS84_E2*sine*sine
            psi_terms.append(weight*(1-WGS84_E2)/(cosine*scale))
            meridian_terms.append(weight*WGS84_A*(1-WGS84_E2)/scale**1.5)
    return math.fsum(psi_terms)/math.fsum(meridian_terms)


def _isometric_latitude_degrees(latitude: float) -> float:
    if abs(latitude)<=45:
        return isometric_latitude(math.radians(latitude))
    theta=math.radians(90-abs(latitude))
    spherical=-math.log(math.tan(theta/2))
    ellipsoidal=WGS84_E*math.atanh(WGS84_E*math.cos(theta))
    return math.copysign(spherical-ellipsoidal,latitude)


def _isometric_delta_degrees(latitude1: float, latitude2: float) -> float:
    delta=math.radians(latitude2-latitude1)
    if delta==0:
        return 0.
    p1,p2=map(math.radians,(latitude1,latitude2))
    if abs(delta)<1e-3*abs(math.cos((p1+p2)/2)) and max(abs(latitude1),abs(latitude2))<89:
        return _isometric_difference(p1,p2,delta)
    # The atanh subtraction ratio is ill-conditioned near ±1 even before
    # rounding to one. Wide intervals instead subtract stable absolute ψ.
    return _isometric_latitude_degrees(latitude2)-_isometric_latitude_degrees(latitude1)


def _meridian_degrees(latitude1,latitude2):
    if latitude1==latitude2:
        return 0.
    distance=GEOD.inv(0.,latitude1,0.,latitude2)[2]
    return math.copysign(distance,latitude2-latitude1)


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
        return abs(_meridian_degrees(lat1, lat2)), 0.0 if p2 > p1 else 180.0
    dp = math.radians(lat2-lat1)
    pm = (p1 + p2) / 2.0
    if abs(dp) < 1e-3*abs(math.cos(pm)):
        # Cancel the near-zero meridian difference algebraically. Native
        # meridian inverse results cannot resolve nanometre latitude spans.
        dpsi = _isometric_delta_degrees(lat1, lat2)
        distance = math.hypot(dl, dpsi)/_rhumb_psi_per_meridian(p1, p2, latitude1=lat1, latitude2=lat2)
    else:
        dpsi = _isometric_delta_degrees(lat1, lat2)
        distance = abs(_meridian_degrees(lat1, lat2) / dpsi) * math.hypot(dl, dpsi)
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
    signed_meridian = _meridian_degrees(lat1, lat2)
    north = 0.0 if signed_meridian > 0 else 180.0
    _, lat, _ = GEOD.fwd(lon1, lat1, north, abs(signed_meridian) * fraction)
    if abs(lat1) == 90 or abs(lat2) == 90:
        return lon1, lat
    phi1, phi2, phif = map(math.radians, (lat1, lat2, lat))
    delta_phi = math.radians(lat2-lat1)
    if abs(delta_phi) < 1e-3*abs(math.cos((phi1+phi2)/2)):
        # Meridian fraction is the requested physical fraction. Multiplying
        # it by the smooth divided-difference ratio avoids dividing a rounded
        # latitude by an extremely small end-to-end latitude difference.
        ratio = fraction*_rhumb_psi_per_meridian(phi1, phif,latitude1=lat1,latitude2=lat)/_rhumb_psi_per_meridian(phi1, phi2,latitude1=lat1,latitude2=lat2)
        return wrap_longitude(lon1+dl*ratio), lat
    change = _isometric_delta_degrees(lat1, lat2)
    partial = _isometric_delta_degrees(lat1, lat)
    return wrap_longitude(lon1 + dl * partial/change), lat


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
