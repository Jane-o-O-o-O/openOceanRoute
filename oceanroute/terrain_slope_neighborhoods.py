"""Two-dimensional, provenance-aware sampled terrain slopes around WGS84 centres.

The mesh is an inscribed polygon, not a proof of the continuous circular bed.
All triangle vertices, edge midpoints and centroids are genuine source queries.
"""
from __future__ import annotations

from copy import deepcopy
import bisect
import json
import math

from .core import route_signature
from .geodesy import GEOD, coordinate, finite_number, interpolate, inverse
from .terrain_sources import (MAX_QUERY_POINTS, _bytes, _query, _raster_header,
                              _surfer_dimensions, normalize_sources)

MODEL = "terrain-slope-neighborhood-v1"
MAX_WINDOWS = 200
CENTER_ROUTE_TOLERANCE_M = 1e-4


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError("slope neighborhoods require finite JSON values") from error


def _integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in {low}..{high}")
    return value


def _config(raw):
    raw = {} if raw is None else raw
    if not isinstance(raw, dict) or set(raw)-{"spacing_m", "vertical_datum", "max_query_points", "max_work_units", "max_output_bytes"}:
        raise ValueError("slope neighborhood config contains unsupported fields")
    result = deepcopy(raw)
    result["spacing_m"] = finite_number(result.get("spacing_m", 50), "spacing_m", minimum=.001, maximum=100_000)
    result["max_query_points"] = _integer(result.get("max_query_points", MAX_QUERY_POINTS), "max_query_points", 1, MAX_QUERY_POINTS)
    result["max_work_units"] = _integer(result.get("max_work_units", 30_000_000), "max_work_units", 1, 200_000_000)
    result["max_output_bytes"] = _integer(result.get("max_output_bytes", 16*1024**2), "max_output_bytes", 1024, 64*1024**2)
    if "vertical_datum" in result:
        datum = result["vertical_datum"]
        if not isinstance(datum, str) or not datum.strip() or len(datum) > 128:
            raise ValueError("vertical_datum must be a nonempty name of at most 128 characters")
        result["vertical_datum"] = datum.strip()
    return result


def _counts(radius, spacing):
    step = spacing/math.sqrt(2)
    rings = max(1, math.ceil(radius/step))
    angles = max(8, 4*math.ceil(2*math.pi*radius/step/4))
    vertices = 1+rings*angles
    triangles = (2*rings-1)*angles
    edges = (3*triangles+angles)//2
    return {"radial_ring_count": rings, "angular_count": angles, "vertex_count": vertices,
            "base_triangle_count": triangles, "triangle_count": 6*triangles,
            "query_count_upper_bound": vertices+edges+triangles}


def _mesh(radius, count):
    """Fixed connectivity retains holes; it never retriangulates valid samples."""
    nr, na = count["radial_ring_count"], count["angular_count"]
    points = [(0., 0.)]
    for ring in range(1, nr+1):
        r = radius*ring/nr
        for angle in range(na):
            # Exact cardinal coordinates avoid tiny artificial edge/support offsets.
            if angle*4 % na == 0:
                points.append(((r, 0.), (0., r), (-r, 0.), (0., -r))[angle*4//na])
            else:
                theta = 2*math.pi*angle/na
                points.append((r*math.cos(theta), r*math.sin(theta)))
    triangles = [(0, 1+j, 1+(j+1)%na) for j in range(na)]
    for ring in range(1, nr):
        inner, outer = 1+(ring-1)*na, 1+ring*na
        for j in range(na):
            k = (j+1)%na
            triangles.extend([(inner+j, outer+j, outer+k), (inner+j, outer+k, inner+k)])
    edge_indices = {}
    supports = []
    for corners in triangles:
        mids = []
        for a, b in zip(corners, (corners[1], corners[2], corners[0])):
            edge = tuple(sorted((a, b)))
            if edge not in edge_indices:
                edge_indices[edge] = len(points)
                points.append(((points[a][0]+points[b][0])/2, (points[a][1]+points[b][1])/2))
            mids.append(edge_indices[edge])
        centre = len(points)
        points.append((sum(points[k][0] for k in corners)/3, sum(points[k][1] for k in corners)/3))
        supports.append((*corners, *mids, centre))
    return points, triangles, supports


class SlopeNeighborhoodSampler:
    """Normalize once; query all windows together under one finite global budget.

    Instances are reusable, but each sample_many call is a distinct charged job.
    The caller must aggregate budgets across separate calls/rules.
    """

    def __init__(self, project, config=None, *, sources=None):
        if not isinstance(project, dict):
            raise ValueError("project must be an object")
        if project.get("crs", "EPSG:4326") != "EPSG:4326":
            raise ValueError("project route coordinates must be EPSG:4326; convert explicit source CRS before use")
        self.config = _config(config)
        if "route" in project:
            route = project["route"]
            if not isinstance(route, dict) or not isinstance(route.get("points"), list) or not 2 <= len(route["points"]) <= 10_000:
                raise ValueError("an optional route must have 2..10000 coordinate objects")
            if route.get("curve", "rhumb") not in ("rhumb", "geodesic"):
                raise ValueError("route.curve must be rhumb or geodesic")
            for point in route["points"]:
                if not isinstance(point, dict):
                    raise ValueError("route points must be coordinate objects")
                coordinate(point.get("longitude"), point.get("latitude"))
        self.project = {key: deepcopy(project[key]) for key in ("route", "crs") if key in project}
        raw = project.get("terrain_sources", []) if sources is None else sources
        raw_size = len(_json(raw))
        # Explicit normalized units for bounded serialization, header parsing and
        # copying. This is not a CPU-operation or byte-accurate cost model.
        self.normalization_work = 128+math.ceil(raw_size/4)
        if self.normalization_work >= self.config["max_work_units"]:
            raise ValueError("source normalization exceeds max_work_units")
        self.sources = normalize_sources(raw)
        self.preparation_upper = 0
        self.query_cost_upper = 0
        for source in self.sources:
            if not source["enabled"] or ("vertical_datum" in self.config and source["vertical_datum"] != self.config["vertical_datum"]):
                continue
            if source["kind"] == "xyz":
                n = sum(bool(line.strip()) and not line.lstrip().startswith("#") for line in source["text"].splitlines())
                self.preparation_upper += n*(32+math.ceil(math.log2(max(n, 2))))
                self.query_cost_upper += 32
            else:
                data = _bytes(source)
                nx, ny = _raster_header(source, data) if source["kind"] == "geotiff" else _surfer_dimensions(data)
                self.preparation_upper += nx*ny*4
                self.query_cost_upper += 8
        self._route = None

    def _route_position(self, kp):
        if self._route is None:
            route = self.project.get("route")
            if not isinstance(route, dict) or route.get("curve", "rhumb") not in ("rhumb", "geodesic"):
                raise ValueError("a KP centre requires an explicit rhumb/geodesic WGS84 route")
            raw = route.get("points")
            if not isinstance(raw, list) or not 2 <= len(raw) <= 10_000:
                raise ValueError("route.points must contain 2..10000 coordinate objects")
            points = []
            for p in raw:
                if not isinstance(p, dict):
                    raise ValueError("route points must be coordinate objects")
                points.append(coordinate(p.get("longitude"), p.get("latitude")))
            curve, total, legs = route.get("curve", "rhumb"), 0., []
            for a, b in zip(points, points[1:]):
                length, heading = inverse(*a, *b, curve)
                if length > 0:
                    if heading is None:
                        raise ValueError("a positive route leg has unresolved azimuth")
                    legs.append((a, b, total, length))
                    total += length
            self._route = (curve, legs, [leg[2] for leg in legs], total, points[0], route_signature(self.project))
        curve, legs, starts, total, first, signature = self._route
        kp = finite_number(kp, "center.kp_m", minimum=0, maximum=total)
        if not legs:
            # A 2D circular window needs no route tangent. All-zero legs are
            # valid at KP0, unlike a route-normal transverse section.
            return first, kp, signature
        a, b, start, length = legs[min(len(legs)-1, max(0, bisect.bisect_right(starts, kp)-1))]
        fraction = min(1., max(0., (kp-start)/length))
        return interpolate(*a, *b, fraction, curve), kp, signature

    def _windows(self, windows):
        if not isinstance(windows, list) or not 1 <= len(windows) <= MAX_WINDOWS:
            raise ValueError(f"windows must contain 1..{MAX_WINDOWS} explicit requests; no truncation")
        result = []
        for raw in windows:
            if not isinstance(raw, dict) or set(raw) != {"center", "radius_m"}:
                raise ValueError("each window requires only center and radius_m")
            c = raw["center"]
            if not isinstance(c, dict) or set(c)-{"longitude", "latitude", "kp_m"}:
                raise ValueError("center requires WGS84 longitude/latitude and optional finite kp_m")
            lon, lat = coordinate(c.get("longitude"), c.get("latitude"))
            if abs(lat) == 90:
                raise ValueError("a local east/north slope frame at a geographic pole is undefined")
            center = {"longitude": lon, "latitude": lat}
            signature = None
            if "kp_m" in c:
                expected, kp, signature = self._route_position(c["kp_m"])
                mismatch = GEOD.inv(lon, lat, *expected)[2]
                if not math.isfinite(mismatch) or mismatch > CENTER_ROUTE_TOLERANCE_M:
                    raise ValueError("center coordinates do not match the declared route KP")
                center.update(kp_m=kp, route_position_error_m=float(mismatch))
            radius = finite_number(raw["radius_m"], "radius_m", minimum=.001, maximum=100_000)
            count = _counts(radius, self.config["spacing_m"])
            result.append({"center": center, "radius_m": radius, "counts": count, "route_signature": signature})
        return result

    def _estimate(self, windows):
        q = sum(row["counts"]["query_count_upper_bound"] for row in windows)
        t = sum(row["counts"]["triangle_count"] for row in windows)
        vertices = sum(row["counts"]["vertex_count"] for row in windows)
        route = self.project.get("route")
        points = route.get("points") if isinstance(route, dict) else None
        route_work = 16*max(0, len(points)-1) if isinstance(points, list) else 0
        geometry = route_work+128*len(windows)+32*vertices+48*q+128*t
        enabled_ids = [len(_json(s["id"])) for s in self.sources if s["enabled"]]
        # Full source payloads are not returned. Account for long UTF-8 IDs in
        # every attempt and provenance record, and complete triangle evidence.
        probe_bytes = 800+(max(enabled_ids, default=0))+sum(320+n for n in enabled_ids)
        triangle_bytes = 1800+2*max(enabled_ids, default=0)
        source_metadata_bytes = sum(len(_json({k: v for k, v in s.items() if k not in ("text", "data_base64")}))
                                    for s in self.sources)
        # CRS declarations may be long WKT strings and are repeated by actual
        # operation evidence. Keep the real declaration size in the bound.
        output = 131072+4*source_metadata_bytes+q*probe_bytes+t*triangle_bytes+len(windows)*6000
        estimated = self.normalization_work+geometry+self.preparation_upper+q*self.query_cost_upper
        return {"window_count": len(windows), "query_count_upper_bound": q, "triangle_count": t,
                "normalization_work_units": self.normalization_work, "geometry_work_units": geometry,
                "terrain_work_units_upper_bound": self.preparation_upper+q*self.query_cost_upper,
                "estimated_work_units": estimated, "output_upper_bound_bytes": output,
                "max_query_points": self.config["max_query_points"], "max_work_units": self.config["max_work_units"],
                "max_output_bytes": self.config["max_output_bytes"]}

    def estimate_many(self, windows):
        """Pure bounded mesh/query admission estimate; no terrain interpolation."""
        return self._estimate(self._windows(windows))

    def sample_many(self, windows):
        requests = self._windows(windows)
        estimate = self._estimate(requests)
        if estimate["query_count_upper_bound"] > self.config["max_query_points"]:
            raise ValueError("whole slope-neighborhood batch exceeds max_query_points before mesh generation")
        if estimate["estimated_work_units"] > self.config["max_work_units"]:
            raise ValueError("whole slope-neighborhood batch exceeds max_work_units before source preparation")
        if estimate["output_upper_bound_bytes"] > self.config["max_output_bytes"]:
            raise ValueError("whole slope-neighborhood batch exceeds max_output_bytes before mesh generation")
        queries, query_indices, meshes = [], {}, []
        for request in requests:
            xy, triangles, supports = _mesh(request["radius_m"], request["counts"])
            indices = []
            lon0, lat0 = request["center"]["longitude"], request["center"]["latitude"]
            for x, y in xy:
                if x == y == 0:
                    lon, lat = lon0, lat0
                else:
                    lon, lat, _ = GEOD.fwd(lon0, lat0, math.degrees(math.atan2(x, y)), math.hypot(x, y))
                key = (float(lon), float(lat))
                if key not in query_indices:
                    query_indices[key] = len(queries)
                    queries.append({"longitude": key[0], "latitude": key[1]})
                indices.append(query_indices[key])
            meshes.append((xy, triangles, supports, indices))
        query_config = {key: self.config[key] for key in ("max_query_points", "max_output_bytes")}
        query_config["max_work_units"] = self.config["max_work_units"]-estimate["normalization_work_units"]-estimate["geometry_work_units"]
        if "vertical_datum" in self.config:
            query_config["vertical_datum"] = self.config["vertical_datum"]
        # Private batch operator deliberately receives the already normalized
        # library. Public query_terrain would normalize/read headers repeatedly.
        query = _query(self.sources, queries, query_config)
        neighborhoods = []
        for request, mesh in zip(requests, meshes):
            xy, corners_list, support_list, indices = mesh
            samples = [{**deepcopy(query["samples"][qi]), "index": i, "x_m": x, "y_m": y,
                        "query_index": qi} for i, ((x, y), qi) in enumerate(zip(xy, indices))]
            triangles = []
            valid_area = missing_area = seam_area = 0.
            for family, (family_corners, support) in enumerate(zip(corners_list, support_list)):
                probes = [samples[k] for k in support]
                missing = any(p["depth_m"] is None for p in probes)
                identities = {(p["source_id"], p["source_fingerprint"]) for p in probes if p["depth_m"] is not None}
                seam = len(identities) > 1
                valid = not missing and not seam
                identity = next(iter(identities)) if valid else (None, None)
                va, vb, vc, mab, mbc, mca, centre = support
                children = [(va, mab, centre), (mab, vb, centre), (vb, mbc, centre),
                            (mbc, vc, centre), (vc, mca, centre), (mca, va, centre)]
                witness = {key: samples[centre][key] for key in
                           ("index", "query_index", "x_m", "y_m", "longitude", "latitude", "depth_m", "source_id", "source_fingerprint")}
                for corners in children:
                    a, b, c = (samples[k] for k in corners)
                    dx1, dy1 = b["x_m"]-a["x_m"], b["y_m"]-a["y_m"]
                    dx2, dy2 = c["x_m"]-a["x_m"], c["y_m"]-a["y_m"]
                    determinant = dx1*dy2-dy1*dx2
                    area = abs(determinant)/2
                    if not math.isfinite(area) or area <= 0:
                        raise ValueError("degenerate slope triangle; no slope can be resolved")
                    gradient = slope = None
                    if valid:
                        dz1, dz2 = a["depth_m"]-b["depth_m"], a["depth_m"]-c["depth_m"]
                        gradient = [(dz1*dy2-dy1*dz2)/determinant, (dx1*dz2-dz1*dx2)/determinant]
                        slope = math.degrees(math.atan(math.hypot(*gradient)))
                        if not all(math.isfinite(v) for v in [*gradient, slope]):
                            raise ValueError("slope computation is nonfinite")
                        valid_area += area
                    if missing: missing_area += area
                    if seam: seam_area += area
                    cx, cy = sum(p["x_m"] for p in (a, b, c))/3, sum(p["y_m"] for p in (a, b, c))/3
                    lon, lat, _ = GEOD.fwd(request["center"]["longitude"], request["center"]["latitude"],
                                            math.degrees(math.atan2(cx, cy)), math.hypot(cx, cy))
                    triangles.append({"index": len(triangles), "family_index": family,
                                      "family_vertex_indices": list(family_corners),
                                      "family_centroid_sample_index": centre,
                                      "vertex_indices": list(corners), "support_indices": list(support),
                                      "sampled_witness": deepcopy(witness),
                                      "centroid": {"x_m": cx, "y_m": cy, "longitude": float(lon), "latitude": float(lat),
                                                   "depth_m": None, "location_kind": "geometric_child_centroid_not_queried"},
                                      "area_m2": area, "valid": valid, "slope_deg": slope, "gradient_height": gradient,
                                      "reason": "nodata" if missing else "source_boundary" if seam else None,
                                      "missing_support": missing, "source_boundary": seam,
                                      "source_id": identity[0], "source_fingerprint": identity[1]})
            radius, na = request["radius_m"], request["counts"]["angular_count"]
            disk_area = math.pi*radius**2
            polygon_area = na*radius**2*math.sin(2*math.pi/na)/2
            area_metadata = {"disk_area_m2": disk_area, "triangulated_area_m2": polygon_area,
                             "missing_area_m2": max(0., disk_area-polygon_area), "coverage_fraction": polygon_area/disk_area}
            quality = {"sample_complete": all(s["depth_m"] is not None for s in samples),
                       "triangle_complete": all(t["valid"] for t in triangles),
                       "source_boundary": any(t["source_boundary"] for t in triangles),
                       "sample_count": len(samples), "missing_sample_count": sum(s["depth_m"] is None for s in samples),
                       "valid_triangle_count": sum(t["valid"] for t in triangles),
                       "missing_triangle_count": sum(t["missing_support"] for t in triangles),
                       "source_boundary_triangle_count": sum(t["source_boundary"] for t in triangles),
                       "max_sampled_slope_deg": max((t["slope_deg"] for t in triangles if t["valid"]), default=None),
                       "valid_triangulated_area_m2": valid_area, "nodata_triangle_area_m2": missing_area,
                       "source_boundary_triangle_area_m2": seam_area, "valid_disk_area_fraction": valid_area/disk_area,
                       "continuous_bed_verified": False, **area_metadata}
            metadata = {"model": MODEL, "schema_version": 1, "terrain_library_signature": query["quality"]["library_signature"],
                        "vertical_datum": query["quality"]["vertical_datum"], "spacing_m": self.config["spacing_m"],
                        "route_signature": request["route_signature"], "center_route_tolerance_m": CENTER_ROUTE_TOLERANCE_M,
                        "local_crs": f"+proj=aeqd +lat_0={request['center']['latitude']} +lon_0={request['center']['longitude']} +datum=WGS84 +units=m",
                        "axis_units": "m", "axis_directions": ["east", "north"], "depth_positive": "down",
                        "height_definition": "minus_depth_relative_to_declared_vertical_datum", "slope_units": "degrees",
                        "slope_definition": "atan(norm(affine_triangle_gradient_of_height))",
                        "mesh_policy": "shared-angle_concentric_rings_inscribed_polygon_each_family_split_into_six_sampled_children",
                        "support_policy": "family_three_vertices_three_edge_midpoints_and_centroid_actually_queried_all_required_for_each_child",
                        "edge_length_bound_m": self.config["spacing_m"], **request["counts"], **area_metadata}
            neighborhoods.append({"model": MODEL, "validation_status": "research", "metadata": metadata,
                                  "center": request["center"], "radius_m": radius, "samples": samples,
                                  "triangles": triangles, "quality": quality})
        warnings = deepcopy(query["warnings"])
        if any(n["quality"]["source_boundary"] for n in neighborhoods):
            warnings.append({"code": "SLOPE_NEIGHBORHOOD_SOURCE_BOUNDARY", "severity": "warning", "message": "Triangles with any differing support source have null slopes; source jumps are not bed gradients"})
        if any(not n["quality"]["sample_complete"] for n in neighborhoods):
            warnings.append({"code": "SLOPE_NEIGHBORHOOD_NODATA", "severity": "warning", "message": "Missing supports invalidate affected triangles without bridging holes; remaining maxima do not certify the unknown area"})
        warnings.append({"code": "SLOPE_NEIGHBORHOOD_SAMPLED_ONLY", "severity": "info", "message": "Finite probes on an inscribed disk polygon cannot prove continuous bed coverage or exclude hazards between probes"})
        result = {"model": MODEL, "validation_status": "research", "neighborhoods": neighborhoods, "sources": query["sources"],
                  "quality": query["quality"], "warnings": warnings,
                  "assumptions": ["Independent sampled terrain assessment, not the manufacturer's slope algorithm or field precision",
                                  "AEQD radial distances are WGS84 metres; transverse metric and triangle gradients use the local planar approximation",
                                  "Datum names are selected, not transformed; height=-depth and no engineering water-depth replacement occurs",
                                  "No interpolation across missing support or source seams; undetected holes between probes remain possible",
                                  "Inscribed circular boundary leaves explicitly reported unsampled area; finite slopes are not continuous maxima"],
                  "budget": {**estimate, "query_count": sum(len(m[0]) for m in meshes), "unique_query_count": len(queries),
                             "cache_hits": sum(len(m[0]) for m in meshes)-len(queries),
                             "source_preparation_cache_hits": query["budget"]["cache_hits"],
                             "source_preparation_cache_misses": query["budget"]["cache_misses"],
                             "terrain_query_work_units": query["budget"]["work_units"],
                             "work_units": estimate["normalization_work_units"]+estimate["geometry_work_units"]+query["budget"]["work_units"],
                             "work_basis": "normalized source serialization/header allowance plus bounded mesh/support/projection/gradient units and genuine terrain preparation/attempted-point charges; not FLOPs, CPU time or tokens",
                             "output_bytes": 0}}
        _finish_bytes(result, self.config["max_output_bytes"])
        return result


def _finish_bytes(result, maximum):
    # Updating the decimal counter can itself change the encoded length.
    for _ in range(8):
        size = len(_json(result))
        if size > maximum:
            raise ValueError("actual finite slope-neighborhood output exceeds max_output_bytes; no truncation")
        if result["budget"]["output_bytes"] == size:
            return
        result["budget"]["output_bytes"] = size
    raise ValueError("slope-neighborhood output byte counter did not stabilize")


def sample_slope_neighborhood(project, center, radius_m, config=None, *, sources=None):
    """Single-window wrapper; no route/profile/material/project mutation."""
    sampler = SlopeNeighborhoodSampler(project, config, sources=sources)
    batch = sampler.sample_many([{"center": center, "radius_m": radius_m}])
    result = {**batch["neighborhoods"][0], **{key: value for key, value in batch.items() if key != "neighborhoods" and key != "quality"}}
    # Preserve both mesh completeness and genuine underlying point-query quality.
    result["quality"]["terrain_query"] = batch["quality"]
    _finish_bytes(result, sampler.config["max_output_bytes"])
    return result
