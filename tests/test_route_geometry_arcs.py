"""Independent metric/geometry checks; PROJ is shared only for WGS84 geodesics."""
from copy import deepcopy
import base64
import json
import math

import numpy as np
from pyproj import Geod
import pytest

from oceanroute.geodesy import densify, interpolate, inverse, split_antimeridian
from oceanroute.route_geometry import RouteSegment, render_route, route_segments, route_geometry_signature, segment_from_leg

G = Geod(ellps="WGS84")
A = 6378137.
F = 1/298.257223563
E2 = F*(2-F)


def arc(center=(179., 70.), radius=4000., start=20., sweep=120., **kw):
    g = {"type": "circular_arc", "schema_version": 1, "center": list(center),
         "radius_m": radius, "start_azimuth_deg": start, "sweep_deg": sweep}
    a, b = G.fwd(*center, start, radius)[:2], G.fwd(*center, start+sweep, radius)[:2]
    return RouteSegment(a, b, g, **kw)


def project(s):
    return {"schema_version": 1, "route": {"curve": "rhumb",
        "points": [{"longitude": p[0], "latitude": p[1]} for p in (s.start, s.end)],
        "legs": [{"geometry": s.geometry}]}}


def angle(a, b):
    return abs((a-b+180)%360-180)


@pytest.mark.parametrize("pole,sweep", [(90., 360.), (90., -210.), (-90., 90.), (-90., -360.)])
def test_pole_circle_length_matches_parallel_radius_not_planar_radius(pole, sweep):
    s = arc((23., pole), 1_000_000., 13., sweep)
    phi = math.radians(s.start[1])
    parallel_radius = A*math.cos(phi)/math.sqrt(1-E2*math.sin(phi)**2)
    expected = abs(math.radians(sweep))*parallel_radius
    assert s.length_m == pytest.approx(expected, abs=2e-8)
    assert abs(s.length_m-1_000_000.*abs(math.radians(sweep))) > 1000
    for f in (.125, .4, .9):
        p = s.point_at_fraction(f)
        assert p[1] == pytest.approx(s.start[1], abs=2e-13)
        assert G.inv(*s.geometry["center"], *p)[2] == pytest.approx(1_000_000., abs=2e-8)
        expected_heading = (270 if pole > 0 else 90) if sweep > 0 else (90 if pole > 0 else 270)
        assert angle(s.tangent_at_fraction(f), expected_heading) < 1e-10


def test_dense_independent_geodesic_chords_converge_to_integrated_arc_length():
    s = arc((143., 64.), 600_000., -20., 200.)
    g = s.geometry
    errors = []
    for n in (256, 512, 1024):
        alpha = np.linspace(g["start_azimuth_deg"], g["start_azimuth_deg"]+g["sweep_deg"], n+1)
        lon, lat, _ = G.fwd(np.full(n+1, 143.), np.full(n+1, 64.), alpha, np.full(n+1, 600_000.))
        lengths = G.inv(lon[:-1], lat[:-1], lon[1:], lat[1:])[2]
        errors.append(s.length_m-float(np.sum(lengths)))
    assert all(e > 0 for e in errors)
    assert errors[0]/errors[1] == pytest.approx(4, rel=1e-4)
    assert errors[1]/errors[2] == pytest.approx(4, rel=1e-4)


def test_physical_kp_fraction_is_not_radial_angle_fraction_at_high_latitude():
    s = arc((15., 64.), 1_000_000., 0., 180.)
    p = s.point_at_fraction(.4)
    angular = G.fwd(15., 64., 72., 1_000_000.)[:2]
    assert G.inv(*p, *angular)[2] > .01
    # Independent polygonal arc length on each side of the requested position.
    az = G.inv(15., 64., *p)[0]
    def chord_sum(lo, hi, n):
        a = np.linspace(lo, hi, n+1)
        lon, lat, _ = G.fwd(np.full(n+1, 15.), np.full(n+1, 64.), a, np.full(n+1, 1_000_000.))
        return float(np.sum(G.inv(lon[:-1], lat[:-1], lon[1:], lat[1:])[2]))
    partial = chord_sum(0., az, 6000)
    total = partial+chord_sum(az, 180., 9000)
    assert partial/total == pytest.approx(.4, abs=2e-9)
    assert s.point_at_distance(.4*s.length_m) == p
    assert s.solver["maximum_inversion_distance_residual_m"] < 1e-6


@pytest.mark.parametrize("sweep", [90., -90., 360., -360.])
def test_radius_tangent_reversal_and_true_split(sweep):
    s = arc((179.99, 70.), 5000., 0., sweep)
    left, right = s.split(.37*s.length_m)
    assert left.end == right.start == s.point_at_fraction(.37)
    assert left.length_m+right.length_m == pytest.approx(s.length_m, abs=2e-7)
    assert angle(left.tangent_at_fraction(1), right.tangent_at_fraction(0)) < 1e-9
    assert left.geometry["center"] == right.geometry["center"] == s.geometry["center"]
    for f in (0., .3, .8, 1.):
        rev = s.reversed()
        assert G.inv(*rev.point_at_fraction(1-f), *s.point_at_fraction(f))[2] < 2e-7
        assert angle(rev.tangent_at_fraction(1-f), s.tangent_at_fraction(f)+180) < 1e-8
        p = s.point_at_fraction(f)
        _, back, r = G.inv(*s.geometry["center"], *p)
        assert r == pytest.approx(5000., abs=1e-7)
        assert angle(s.tangent_at_fraction(f), back+180+math.copysign(90., sweep)) < 1e-8
    # New segment is reloadable without using a frozen drawing.
    loaded = segment_from_leg(left.start, left.end, {"geometry": json.loads(json.dumps(left.geometry))})
    assert loaded.length_m == left.length_m


@pytest.mark.parametrize("curve", ["rhumb", "geodesic"])
def test_straight_branch_matches_existing_functions_exactly(curve):
    a, b = (170., 67.), (-173., 72.)
    s = segment_from_leg(a, b, {"slack_pct": 2}, curve)
    assert s.length_m == inverse(*a, *b, curve)[0]
    for f in (0, .1, .7, 1):
        assert s.point_at_fraction(f) == interpolate(*a, *b, f, curve)
    p = {"route": {"curve": curve, "points": [{"longitude": a[0], "latitude": a[1]}, {"longitude": b[0], "latitude": b[1]}]}}
    rendered = render_route(p)
    coords = [list(q) for q in densify(*a, *b, curve)]
    assert rendered["coordinates"] == coords
    assert rendered["segments"] == split_antimeridian(coords, curve)


def test_zero_straight_and_full_circle_have_different_topology():
    zero = RouteSegment([3, 4], [3, 4])
    assert zero.length_m == 0
    assert zero.point_at_distance(0) == (3., 4.)
    assert zero.tangent_at_fraction(.2) is None
    circle = arc((0, 0), 1000, 0, 360)
    assert circle.length_m > 6000
    assert G.inv(*circle.start, *circle.end)[2] < 1e-7
    assert G.inv(*circle.start, *circle.point_at_fraction(.5))[2] > 1999


def test_unresolved_tiny_geodesic_heading_is_none_and_endpoint_bearings_preserve_legacy():
    tiny = RouteSegment([0, 89.99999], [1e-8, 89.99999], curve="geodesic")
    assert 0 < tiny.length_m <= 1e-8
    for fraction in (0, .5, 1):
        assert tiny.tangent_at_fraction(fraction) is None
    s = RouteSegment([0, 70], [90, 70], curve="geodesic")
    assert s.tangent_at_fraction(0) == inverse(0,70,90,70,"geodesic")[1]
    assert s.tangent_at_fraction(1) == (G.inv(0,70,90,70)[1]+180)%360
    render_route(project(RouteSegment([0,0],[0,1])), max_vertices=10_020_001)


def test_analytic_arc_upper_bound_does_not_rely_on_quadrature_error_estimate():
    s = arc((14, 65), 1_000_000, 0, 180)
    assert s.length_upper_bound_m >= s.length_m
    assert s.length_upper_bound_m == math.nextafter(1_000_000*math.pi, math.inf)
    left, right = s.split(s.length_m*.4)
    assert left.length_m <= s.length_upper_bound_m*.4+s.position_error_allowance_m
    assert right.length_m <= s.length_upper_bound_m*.6+s.position_error_allowance_m
    assert s.position_error_allowance_m > s.config["endpoint_tolerance_m"]


def test_small_arc_render_has_actual_shape_and_tighter_resolution_converges():
    s = arc((118., 22.), 100., 0., 120.)
    p = project(s)
    coarse = render_route(p, tolerance_m=1)
    fine = render_route(p, tolerance_m=.01)
    assert len(coarse["coordinates"]) > 2
    assert len(fine["coordinates"]) > len(coarse["coordinates"])
    assert coarse["maximum_sampled_chord_error_m"] <= 1
    assert fine["maximum_sampled_chord_error_m"] <= .01
    assert route_segments(p)[0].length_m == s.length_m
    assert p == project(s)


def test_dateline_cut_is_on_actual_circle_not_on_drawing_chord():
    s = arc((179.98, 61.), 5000., 0., 180.)
    rendered = render_route(project(s), spacing_m=5000, tolerance_m=5)
    seam_points = [p for piece in rendered["segments"] for p in piece if abs(p[0]) == 180]
    assert len(seam_points) == 4
    for point in seam_points:
        assert G.inv(*s.geometry["center"], *point)[2] == pytest.approx(5000., abs=1e-6)
    assert all(abs(b[0]-a[0]) <= 180 for piece in rendered["segments"] for a,b in zip(piece,piece[1:]))
    json.dumps(rendered, allow_nan=False)


def test_signature_changes_with_arc_while_endpoints_unchanged():
    s = arc((0, 0), 1000, 0, 90)
    p = project(s)
    q = deepcopy(p)
    q["route"]["legs"][0]["geometry"]["sweep_deg"] = -270
    assert route_geometry_signature(p) != route_geometry_signature(q)
    assert route_segments(p)[0].length_m < route_segments(q)[0].length_m


@pytest.mark.parametrize("field,value", [("radius_m", 0), ("radius_m", 1e6+1), ("sweep_deg", 0),
    ("sweep_deg", 361), ("sweep_deg", float("nan")), ("radius_m", 10**1000),
    ("schema_version", True), ("schema_version", 2), ("type", "polyline")])
def test_invalid_geometry_rejects_without_chord_fallback(field, value):
    s = arc()
    g = s.geometry
    g[field] = value
    with pytest.raises(ValueError):
        RouteSegment(s.start, s.end, g)


def test_endpoint_mismatch_work_samples_unknown_and_numerical_nonconvergence_reject():
    s = arc((1, 65), 1_000_000, 0, 180)
    with pytest.raises(ValueError, match="endpoints"):
        RouteSegment(s.start, (s.end[0]+.001, s.end[1]), s.geometry)
    with pytest.raises(ValueError, match="max_work_units"):
        RouteSegment(s.start, s.end, s.geometry, config={"max_work_units": 1})
    with pytest.raises(ValueError, match="max_points"):
        s.sample(1, max_points=3)
    with pytest.raises(ValueError, match="max_vertices"):
        render_route(project(s), max_vertices=2)
    with pytest.raises(ValueError, match="requires only"):
        RouteSegment(s.start, s.end, {**s.geometry, "drawing_points": []})
    with pytest.raises(ValueError, match="unsupported"):
        RouteSegment(s.start, s.end, s.geometry, config={"arbitrary": True})
    one_iteration = RouteSegment(s.start, s.end, s.geometry, config={"inversion_max_iterations": 1})
    with pytest.raises(ValueError, match="inversion did not converge"):
        one_iteration.point_at_fraction(.4)
    with pytest.raises(ValueError):
        s.subsegment(1, 0)


def terrain_project():
    s = arc((0, 0), 500, -90, 180)
    p = project(s)
    p["route"]["slack_pct"] = 1
    p.update(id="true-arc", name="explicit synthetic arc and analytical terrain", crs="EPSG:4326",
             cable_types=[{"id": "a", "cost_per_m": 1}], bodies=[])
    for i, point in enumerate(p["route"]["points"]):
        point.update(id=str(i), depth_m=1000)
    local = "+proj=aeqd +lat_0=0 +lon_0=0 +datum=WGS84 +units=m +type=crs"
    grid = "DSAA\n5 5\n-1000 1000\n-1000 1000\n900 1100\n"+"\n".join(" ".join(str(1000+.1*y) for x in range(5)) for y in (-1000,-500,0,500,1000))+"\n"
    p["terrain_sources"] = [{"id": "analytic", "name": "explicit synthetic north-depth plane", "kind": "surfer",
        "source_crs": local, "depth_positive": "down", "depth_units": "m", "vertical_datum": "SYNTHETIC",
        "data_base64": base64.b64encode(grid.encode()).decode(), "sampling": {"method": "linear"}}]
    return p, s


def test_true_arc_profile_and_transverse_tangent_query_the_actual_bed_not_chord():
    from oceanroute.side_slopes import side_slopes_from_sources
    from oceanroute.terrain_sources import profile_from_sources
    p, s = terrain_project()
    before = deepcopy(p)
    sampled = profile_from_sources(p, {"spacing_m": 400})
    rows = sampled["samples"]
    middle = rows[2]
    assert middle["kp_m"] == s.length_m/2
    assert middle["depth_m"] == pytest.approx(1050, abs=1e-9)
    assert G.inv(*s.geometry["center"], middle["longitude"], middle["latitude"])[2] == pytest.approx(500, abs=1e-7)
    assert sampled["budget"]["arc_geometry_work_units"] > 20
    section = side_slopes_from_sources(p, {"start_kp_m": s.length_m/2, "end_kp_m": s.length_m/2})["side_slopes"]["samples"][0]
    assert section["heading_deg"] == pytest.approx(90, abs=1e-9)
    assert [x["depth_m"] for x in section["transect"]] == pytest.approx([1060,1055,1050,1045,1040], abs=1e-8)
    assert section["side_slope_deg"] == pytest.approx(math.degrees(math.atan(.1)), abs=1e-9)
    assert p == before


def test_arc_kp_neighborhood_accepts_actual_circle_and_rejects_endpoint_chord():
    from oceanroute.terrain_slope_neighborhoods import sample_slope_neighborhood
    p, s = terrain_project()
    expected = G.fwd(0, 0, 0, 500)[:2]
    sampled = sample_slope_neighborhood(p, {"longitude": expected[0], "latitude": expected[1], "kp_m": s.length_m/2}, 20,
                                        {"spacing_m": 40, "vertical_datum": "SYNTHETIC"})
    assert sampled["center"]["route_position_error_m"] < 1e-6
    assert sampled["quality"]["triangle_complete"]
    assert sampled["budget"]["arc_geometry_work_units"] > 20
    with pytest.raises(ValueError, match="do not match"):
        sample_slope_neighborhood(p, {"longitude": 0, "latitude": 0, "kp_m": s.length_m/2}, 20, {"spacing_m": 40})


def test_arc_inline_rule_range_and_workspace_source_preview_preserve_true_length():
    from oceanroute.core import analyze_project
    from oceanroute.slope_rules import check_slope_rules
    from oceanroute.terrain_sources import profile_from_sources
    from oceanroute.workspace import migrate_project
    from oceanroute.workspace_terrain import preview_workspace_terrain
    p, s = terrain_project()
    sampled = profile_from_sources(p, {"spacing_m": 400})["project"]
    sampled["slope_rules"] = [{"id": "range", "kind": "inline", "start_kp_m": 1200, "end_kp_m": None, "max_inline_slope_deg": 1, "enabled": True}]
    # This start is beyond the 1000 m endpoint chord but inside the actual arc.
    result = check_slope_rules(sampled)
    assert result["results"][0]["end_kp_m"] == pytest.approx(s.length_m)
    assert analyze_project(sampled)["summary"]["surface_length_m"] == s.length_m
    sampled.pop("slope_rules")
    ws = migrate_project(sampled)["workspace"]
    original = deepcopy(ws)
    preview = preview_workspace_terrain(ws, sampled["terrain_sources"], {"spacing_m": 400})
    assert preview["can_apply"], preview.get("errors")
    assert preview["paths"][0]["quality"]["sample_count"] == 5
    assert ws == original


def test_arc_profile_budget_refuses_before_source_query_and_no_angular_or_chord_fallback():
    from oceanroute.terrain_sources import profile_from_sources, TerrainSourceError
    p, _ = terrain_project()
    with pytest.raises(TerrainSourceError, match="TERRAIN_WORK_BUDGET"):
        profile_from_sources(p, {"max_work_units": 1})
    with pytest.raises(TerrainSourceError, match="TERRAIN_WORK_BUDGET"):
        profile_from_sources(p, {"spacing_m": 200, "max_work_units": 50})


def test_reversed_arc_swaps_true_port_starboard_signs_and_preserves_sampled_depths():
    from oceanroute.side_slopes import side_slopes_from_sources
    p, s = terrain_project()
    rev = s.reversed()
    q = deepcopy(p)
    q["route"]["points"] = list(reversed(q["route"]["points"]))
    q["route"]["legs"] = [{"geometry": rev.geometry}]
    cfg = {"start_kp_m": s.length_m/2, "end_kp_m": s.length_m/2}
    forward = side_slopes_from_sources(p,cfg)["side_slopes"]["samples"][0]
    reverse = side_slopes_from_sources(q,cfg)["side_slopes"]["samples"][0]
    assert angle(forward["heading_deg"],reverse["heading_deg"]+180) < 1e-8
    assert forward["side_slope_deg"] == pytest.approx(-reverse["side_slope_deg"],abs=1e-9)
    assert [r["depth_m"] for r in forward["transect"]] == pytest.approx(list(reversed([r["depth_m"] for r in reverse["transect"]])))


def test_arc_nodata_is_not_repaired_from_flat_endpoint_waypoint_depths():
    from oceanroute.side_slopes import side_slopes_from_sources
    from oceanroute.terrain_sources import profile_from_sources
    p, s = terrain_project()
    lines = base64.b64decode(p["terrain_sources"][0]["data_base64"]).decode().splitlines()
    row = lines[8].split(); row[2] = "1.70141e38"; lines[8] = " ".join(row)
    p["terrain_sources"][0]["data_base64"] = base64.b64encode(("\n".join(lines)+"\n").encode()).decode()
    before = deepcopy(p)
    result = profile_from_sources(p,{"spacing_m":400})
    assert result["samples"][2]["depth_m"] is None
    assert not result["quality"]["complete"]
    side = side_slopes_from_sources(p,{"start_kp_m":s.length_m/2,"end_kp_m":s.length_m/2})["side_slopes"]["samples"][0]
    assert not side["complete"]
    assert side["side_slope_deg"] is None
    assert p == before
