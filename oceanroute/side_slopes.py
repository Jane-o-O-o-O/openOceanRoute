"""Route-normal WGS84 bathymetry sections with explicit sampled-slope limits."""
from __future__ import annotations

import bisect
from copy import deepcopy
import json
import math

from .core import route_signature
from .geodesy import GEOD, coordinate, finite_number, interpolate, inverse
from .route_geometry import route_segments
from .terrain_sources import MAX_QUERY_POINTS, normalize_sources, query_terrain

MODEL = "route-side-slopes-v1"
HEADING_POLICY = "outgoing_one_sided_at_waypoint_incoming_at_terminal"


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError("side slopes requires finite, JSON-serializable project/config values") from error


def _integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in {low}..{high}")
    return value


def _config(value):
    value = {} if value is None else value
    allowed = {"spacing_m", "half_width_m", "cross_spacing_m", "start_kp_m", "end_kp_m",
               "vertical_datum", "max_query_points", "max_work_units", "max_output_bytes"}
    if not isinstance(value, dict) or set(value)-allowed:
        raise ValueError("side slope config contains unsupported fields")
    result = deepcopy(value)
    for field, default, low, high in (("spacing_m", 1000, 1, 100_000),
                                    ("half_width_m", 100, .001, 100_000),
                                    ("cross_spacing_m", 50, .001, 100_000)):
        result[field] = finite_number(result.get(field, default), field, minimum=low, maximum=high)
    result["max_query_points"] = _integer(result.get("max_query_points", MAX_QUERY_POINTS), "max_query_points", 1, MAX_QUERY_POINTS)
    result["max_work_units"] = _integer(result.get("max_work_units", 30_000_000), "max_work_units", 1, 200_000_000)
    result["max_output_bytes"] = _integer(result.get("max_output_bytes", 16*1024**2), "max_output_bytes", 1024, 64*1024**2)
    if "vertical_datum" in result:
        datum = result["vertical_datum"]
        if not isinstance(datum, str) or not datum.strip() or len(datum) > 128:
            raise ValueError("vertical_datum must be a nonempty name of at most 128 characters")
        result["vertical_datum"] = datum.strip()
    return result


def _route(project, max_work_units=30_000_000):
    if not isinstance(project, dict) or not isinstance(project.get("route"), dict):
        raise ValueError("side slopes requires a project with an explicit WGS84 route")
    route = project["route"]
    raw = route.get("points")
    if not isinstance(raw, list) or not 2 <= len(raw) <= 10_000:
        raise ValueError("route.points must contain 2..10000 coordinate objects")
    curve = route.get("curve", "rhumb")
    if curve not in ("rhumb", "geodesic"):
        raise ValueError("route.curve must be rhumb or geodesic")
    coordinates = []
    for point in raw:
        if not isinstance(point, dict):
            raise ValueError("each route point must be a coordinate object")
        coordinates.append(coordinate(point.get("longitude"), point.get("latitude")))
    legs, kps = [], [0.]
    segments = route_segments(project, {"max_work_units": min(10_000_000, max_work_units)})
    for a, b, segment in zip(coordinates, coordinates[1:], segments):
        length, heading = segment.length_m, segment.tangent_at_fraction(0)
        if length > 0:
            if heading is None:
                raise ValueError("a positive route leg has numerically unresolved azimuth")
            legs.append({"a": a, "b": b, "start": kps[-1], "end": kps[-1]+length,
                         "length": length, "initial_heading": heading, "segment": segment})
        kps.append(kps[-1]+length)
    if not legs or kps[-1] <= 0:
        raise ValueError("all route points coincide; a transverse direction is undefined")
    return curve, legs, kps


def _station(kp, curve, legs, starts):
    # Exact waypoint stations take the outgoing nonzero leg. Repeated route
    # vertices do not invent a tangent; the terminal station uses the last leg.
    index = min(len(legs)-1, max(0, bisect.bisect_right(starts, kp)-1))
    leg = legs[index]
    distance = min(leg["length"], max(0., kp-leg["start"]))
    if leg["segment"].is_arc:
        lon, lat = leg["segment"].point_at_distance(distance)
        heading = leg["segment"].tangent_at_distance(distance)
    elif curve == "geodesic":
        lon, lat, heading = GEOD.fwd(*leg["a"], leg["initial_heading"], distance,
                                    return_back_azimuth=False)
        if distance == 0:
            lon, lat = leg["a"]
        elif distance == leg["length"]:
            lon, lat = leg["b"]
    else:
        lon, lat = interpolate(*leg["a"], *leg["b"], distance/leg["length"], curve)
        heading = leg["initial_heading"]
    if abs(lat) == 90:
        raise ValueError("a transverse compass frame at the geographic pole is undefined")
    return {"kp_m": kp, "longitude": float(lon), "latitude": float(lat), "heading_deg": float(heading % 360)}


def side_slopes_from_sources(project, config=None, *, sources=None):
    """Return a reviewable candidate; neither longitudinal profile nor stock changes.

    Side angles use elevation=-depth and an increasing port-to-starboard axis.
    They are finite sampled secants, not point derivatives or continuous maxima.
    """
    config = _config(config)
    curve, legs, waypoint_kps = _route(project, config["max_work_units"])
    total = waypoint_kps[-1]
    start = finite_number(config.get("start_kp_m", 0), "start_kp_m", minimum=0, maximum=total)
    end_value = config.get("end_kp_m")
    end = total if end_value is None else finite_number(end_value, "end_kp_m", minimum=0, maximum=total)
    if start > end:
        raise ValueError("start_kp_m must not exceed end_kp_m")
    half_count = int(math.ceil(config["half_width_m"]/config["cross_spacing_m"]))
    transect_count = 2*half_count+1
    along_count = int(math.ceil((end-start)/config["spacing_m"])) if end > start else 0
    # Refuse before creating coordinates/transects or reading source rasters.
    if transect_count*(along_count+1) > config["max_query_points"]:
        raise ValueError("side slopes station × transverse-point count exceeds max_query_points; no automatic thinning")
    stations = {start, end}
    if along_count:
        stations.update(start+(end-start)*i/along_count for i in range(1, along_count))
    stations.update(kp for kp in waypoint_kps if start <= kp <= end)
    stations = sorted(stations)
    point_count = len(stations)*transect_count
    if point_count > config["max_query_points"]:
        raise ValueError("side slopes including required route vertices exceeds max_query_points; no vertex omission")
    geometry_work = 16*(len(waypoint_kps)-1)+48*len(stations)+24*point_count
    if geometry_work >= config["max_work_units"]:
        raise ValueError("side slopes geometry work exceeds max_work_units before source preparation")
    normalized = normalize_sources(project.get("terrain_sources", []) if sources is None else sources)
    candidate_base = deepcopy(project)
    candidate_base.pop("side_slopes", None)
    candidate_base["terrain_sources"] = normalized
    # Long UTF-8 IDs/metadata and the duplicate persistent/candidate section are
    # included. This is a conservative response bound, not a short-ID policy.
    id_bytes = [len(_json(s["id"])) for s in normalized if s["enabled"]]
    point_upper = 720+(max(id_bytes) if id_bytes else 0)+sum(120+n for n in id_bytes)
    output_upper = len(_json(candidate_base))+2*point_count*point_upper+1600*len(stations)+65536
    if output_upper > config["max_output_bytes"]:
        raise ValueError("side slopes conservative output bytes exceed max_output_bytes before source preparation")
    offsets = [config["half_width_m"]*i/half_count for i in range(-half_count, half_count+1)]
    starts = [leg["start"] for leg in legs]
    sections, queries = [], []
    for kp in stations:
        section = _station(kp, curve, legs, starts)
        sections.append(section)
        for offset in offsets:
            if offset == 0:
                lon, lat = section["longitude"], section["latitude"]
            else:
                azimuth = section["heading_deg"]+(90 if offset > 0 else -90)
                lon, lat, _ = GEOD.fwd(section["longitude"], section["latitude"], azimuth, abs(offset))
            queries.append([float(lon), float(lat)])
    # Count true arc integration/inversion in addition to legacy normalized
    # geometry allowances. The remaining budget, not the original cap, goes to
    # actual source preparation/querying; no per-arc cap multiplies the job cap.
    arc_work = sum(leg["segment"].solver["work_units"] for leg in legs if leg["segment"].is_arc)
    geometry_work += arc_work
    if geometry_work >= config["max_work_units"]:
        raise ValueError("side slopes true-arc geometry work exceeds max_work_units before source preparation")
    query_config = {key: config[key] for key in ("max_query_points", "max_output_bytes")}
    query_config["max_work_units"] = config["max_work_units"]-geometry_work
    if "vertical_datum" in config:
        query_config["vertical_datum"] = config["vertical_datum"]
    query = query_terrain(normalized, queries, query_config)
    for index, section in enumerate(sections):
        transect = [{**deepcopy(row), "offset_m": offset, "slope_to_next_deg": None,
                     "source_boundary_to_next": False}
                    for offset, row in zip(offsets, query["samples"][index*transect_count:(index+1)*transect_count])]
        angles = []
        for a, b in zip(transect, transect[1:]):
            if a["depth_m"] is None or b["depth_m"] is None:
                continue
            angle = math.degrees(math.atan2(a["depth_m"]-b["depth_m"], b["offset_m"]-a["offset_m"]))
            a["slope_to_next_deg"] = angle
            a["source_boundary_to_next"] = (a["source_id"] != b["source_id"] or a["source_fingerprint"] != b["source_fingerprint"])
            angles.append(abs(angle))
        def secant(first, last):
            part = transect[first:last+1]
            if any(row["depth_m"] is None for row in part):
                return None
            return math.degrees(math.atan2(part[0]["depth_m"]-part[-1]["depth_m"], part[-1]["offset_m"]-part[0]["offset_m"]))
        section.update(port_slope_deg=secant(0, half_count), starboard_slope_deg=secant(half_count, 2*half_count),
                       side_slope_deg=secant(0, 2*half_count),
                       max_sampled_abs_slope_deg=max(angles) if angles else None,
                       complete=all(row["depth_m"] is not None for row in transect),
                       source_boundary=len({(row["source_id"], row["source_fingerprint"])
                                            for row in transect if row["depth_m"] is not None}) > 1,
                       transect=transect)
    metadata = {"model": MODEL, "terrain_library_signature": query["quality"]["library_signature"],
                "vertical_datum": query["quality"]["vertical_datum"], "spacing_m": config["spacing_m"],
                "half_width_m": config["half_width_m"], "cross_spacing_m": config["cross_spacing_m"],
                "actual_cross_spacing_m": config["half_width_m"]/half_count,
                "start_kp_m": start, "end_kp_m": end, "route_length_m": total, "route_curve": curve,
                "heading_policy": HEADING_POLICY, "offset_positive": "starboard",
                "slope_positive": "rising_elevation_towards_starboard", "depth_positive": "down", "units": "m",
                "slope_units": "degrees", "slope_definition": "finite_width_endpoint_secants_and_adjacent_sampled_segments",
                "station_policy": "maximum_spacing_plus_range_endpoints_and_all_route_vertices",
                "cross_track_geometry": "WGS84 geodesic normal rays at each station; signed surface distance from station",
                "completeness_basis": "all_declared_probe_points_known_not_continuous_seabed_coverage",
                "leg_geometry_policy": "explicit_circular_arcs_use_true_integrated_KP_and_actual_circle_tangent; otherwise route.curve"}
    side = {"model": MODEL, "schema_version": 1, "route_signature": route_signature(project), "metadata": metadata, "samples": sections}
    candidate = candidate_base
    candidate["side_slopes"] = deepcopy(side)
    boundaries = sum(row["source_boundary"] for row in sections)
    incomplete = sum(not row["complete"] for row in sections)
    warnings = deepcopy(query["warnings"])
    if boundaries:
        warnings.append({"code": "SIDE_SLOPE_SOURCE_BOUNDARY", "severity": "warning",
                         "message": f"{boundaries} transverse sections change source; displayed secants may include source-depth jumps, not verified continuous seabed slope"})
    if incomplete:
        warnings.append({"code": "SIDE_SLOPE_INCOMPLETE", "severity": "warning",
                         "message": f"{incomplete} transverse sections contain missing probes; half/full angles do not bridge missing points"})
    result = {"model": MODEL, "validation_status": "research", "side_slopes": side, "project": candidate,
              "sources": query["sources"],
              "quality": {**query["quality"], "station_count": len(sections), "points_per_transect": transect_count,
                          "incomplete_station_count": incomplete, "source_boundary_station_count": boundaries,
                          "complete": not incomplete,
                          "max_sampled_abs_slope_deg": max((r["max_sampled_abs_slope_deg"] for r in sections if r["max_sampled_abs_slope_deg"] is not None), default=None)},
              "budget": {**query["budget"], "work_units": geometry_work+query["budget"]["work_units"],
                         "max_work_units": config["max_work_units"], "geometry_work_units": geometry_work,
                         "terrain_query_work_units": query["budget"]["work_units"], "query_point_count": point_count,
                         "arc_geometry_work_units": arc_work,
                         "output_preflight_upper_bound_bytes": output_upper,
                         "work_basis": "normalized route-inverse/station/geodesic-probe/section allowances plus genuine terrain preparation and point-query charges; not CPU FLOPs"},
              "warnings": warnings,
              "assumptions": [*query["assumptions"],
                  "Rightward/starboard uphill is positive; elevation is minus the declared positive-down metre depth.",
                  "Half/full angles are finite-width endpoint secants, not local derivatives; sampled maximum uses only adjacent known probes.",
                  "Complete means all declared probes have depth; narrow hazards/NoData between probes and between route stations may remain undetected.",
                  "Source transitions may create apparent slopes. They remain explicitly uncertain even when all probes have values.",
                  "Explicit circular arcs use integrated physical length and their actual radius-normal tangent. Geodesic forward azimuth varies along a straight leg; rhumb heading is constant. Corners use the outgoing one-sided tangent and the terminal uses the incoming tangent.",
                  "Each cross section follows station-normal WGS84 geodesic rays. Parallel offsets, a globally orthogonal corridor mesh, and a continuous-area maximum are not inferred.",
                  "No profile, route geometry, cable material, assembly or manufacture inventory is modified; the caller reviews the separate side-slopes candidate."]}
    volume = len(_json(result))
    if volume > config["max_output_bytes"]:
        raise ValueError("side slopes actual finite JSON exceeds max_output_bytes; no partial/truncated result")
    result["budget"]["output_bytes"] = 0
    # Converge the decimal length of the self-describing byte counter.
    for _ in range(4):
        current_volume = len(_json(result))
        if current_volume == result["budget"]["output_bytes"]:
            break
        result["budget"]["output_bytes"] = current_volume
    if len(_json(result)) > config["max_output_bytes"]:
        raise ValueError("side slopes final finite JSON exceeds max_output_bytes")
    return result
