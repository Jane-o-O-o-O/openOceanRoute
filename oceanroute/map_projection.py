"""Bounded display projection of explicit WGS84 map geometries.

This operation never changes the engineering document. Route curves are supplied
as already sampled GeoJSON; reference-layer edges retain GeoJSON's geographic
linear interpolation, rather than being reinterpreted as cable routes.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math

from pyproj.exceptions import ProjError
from shapely.geometry import shape

from .coordinate_transforms import _crs, _metadata, _operation, transform_coordinates
from .geodesy import GEOD, coordinate, finite_number

MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_VERTICES = 250_000


class _BudgetError(ValueError):
    pass


def _integer(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError(f"{name} must be a nonempty identifier of at most 128 characters")
    return value


def _position(value):
    if not isinstance(value, list) or len(value) not in (2, 3):
        raise ValueError("GeoJSON positions must contain longitude, latitude and optional unchanged altitude")
    lon, lat = coordinate(value[0], value[1])
    return [lon, lat] + ([finite_number(value[2], "altitude")] if len(value) == 3 else [])


class _Geometry:
    def __init__(self, maximum, spacing):
        self.maximum = maximum
        self.spacing = spacing
        self.positions = []
        self.references = []
        self.context = {}
        self.input_vertices = 0
        self.warnings = []

    def add(self, position, path):
        if len(self.positions) >= self.maximum:
            raise _BudgetError(f"map projection exceeds max_vertices={self.maximum}; increase spacing or reduce visible geometry")
        result = list(position)
        self.positions.append({"id": f"v{len(self.positions)}", "x": result[0], "y": result[1]})
        self.references.append((result, dict(self.context), path))
        return result

    def line(self, values, path, *, ring=False, densify=False):
        if not isinstance(values, list) or len(values) < (4 if ring else 2):
            raise ValueError("GeoJSON lines need at least two positions; closed rings need at least four")
        positions = [_position(v) for v in values]
        self.input_vertices += len(positions)
        if ring and positions[0] != positions[-1]:
            raise ValueError("GeoJSON polygon rings must be explicitly closed")
        result = [self.add(positions[0], path + "/0")]
        long_edges = 0
        for index, (a, b) in enumerate(zip(positions, positions[1:])):
            if len(a) != len(b):
                raise ValueError("GeoJSON edge endpoints must have matching dimensions")
            # An upper bound using WGS84 a: geographic linear edges can travel
            # the long way round Earth. Shortest geodesic length would silently
            # reinterpret a 179 -> -179 degree reference-layer edge.
            angular = math.hypot(b[0]-a[0], b[1]-a[1])
            count = max(1, math.ceil(angular * 111_700 / self.spacing)) if densify else 1
            if abs(b[0]-a[0]) > 180:
                long_edges += 1
            if len(self.positions)+count > self.maximum:
                raise _BudgetError(f"map projection exceeds max_vertices={self.maximum}; increase spacing or cut long geographic edges")
            for j in range(1, count+1):
                value = b if j == count else [av+(bv-av)*(j/count) for av, bv in zip(a, b)]
                result.append(self.add(value, f"{path}/{index+1}:{j}/{count}"))
        if long_edges:
            self.warnings.append({"code": "MAP_LONG_GEOGRAPHIC_EDGE", "entity": dict(self.context),
                                  "count": long_edges, "message": "Uncut GeoJSON longitude edges retain their supplied geographic interpolation; cut intended date-line crossings in the source geometry."})
        return result

    def geometry(self, value, path, *, densify=False, depth=0):
        if depth > 16 or not isinstance(value, dict):
            raise ValueError("GeoJSON geometry must be an object with nesting at most 16")
        kind = value.get("type")
        result = {k: deepcopy(v) for k, v in value.items() if k not in {"coordinates", "geometries", "bbox", "crs"}}
        coords = value.get("coordinates")
        if kind == "Point":
            self.input_vertices += 1
            result["coordinates"] = self.add(_position(coords), path+"/coordinates")
        elif kind == "LineString":
            result["coordinates"] = self.line(coords, path+"/coordinates", densify=densify)
        elif kind in {"MultiPoint", "MultiLineString", "Polygon", "MultiPolygon"}:
            if not isinstance(coords, list):
                raise ValueError(f"{kind}.coordinates must be an array")
            if kind in {"Polygon", "MultiPolygon"}:
                try:
                    polygon = shape(value)
                    if not polygon.is_valid or polygon.is_empty:
                        raise ValueError("GeoJSON polygon topology is invalid or empty; holes and rings are not repaired automatically")
                except (TypeError,KeyError,AttributeError) as error:
                    raise ValueError("GeoJSON polygon structure is invalid") from error
            if kind == "MultiPoint":
                self.input_vertices += len(coords)
                result["coordinates"] = [self.add(_position(p), f"{path}/coordinates/{i}") for i,p in enumerate(coords)]
            elif kind == "MultiPolygon":
                result["coordinates"] = []
                for i, polygon in enumerate(coords):
                    if not isinstance(polygon, list) or not polygon:
                        raise ValueError("MultiPolygon members need an exterior ring")
                    result["coordinates"].append([self.line(ring, f"{path}/coordinates/{i}/{j}", ring=True, densify=densify) for j,ring in enumerate(polygon)])
            else:
                if kind == "Polygon" and not coords:
                    raise ValueError("Polygon needs an exterior ring")
                result["coordinates"] = [self.line(line, f"{path}/coordinates/{i}", ring=kind=="Polygon", densify=densify) for i,line in enumerate(coords)]
        elif kind == "GeometryCollection":
            geometries = value.get("geometries")
            if not isinstance(geometries, list):
                raise ValueError("GeometryCollection.geometries must be an array")
            result["geometries"] = [self.geometry(g, f"{path}/geometries/{i}", densify=densify, depth=depth+1) for i,g in enumerate(geometries)]
        else:
            raise ValueError(f"Unsupported GeoJSON geometry type {kind!r}")
        return result

    def geojson(self, value, path, depth=0):
        if depth > 16 or not isinstance(value, dict):
            raise ValueError("GeoJSON must be an object with nesting at most 16")
        kind = value.get("type")
        result = {k:deepcopy(v) for k,v in value.items() if k not in {"features", "geometry", "bbox", "crs"}}
        if kind == "FeatureCollection":
            features = value.get("features")
            if not isinstance(features, list) or len(features)>20_000:
                raise ValueError("FeatureCollection.features must contain at most 20000 features")
            result["features"] = [self.geojson(feature, f"{path}/features/{i}", depth+1) for i,feature in enumerate(features)]
        elif kind == "Feature":
            geometry = value.get("geometry")
            result["geometry"] = None if geometry is None else self.geometry(geometry, path+"/geometry", densify=True)
        else:
            return self.geometry(value, path, densify=True)
        return result


def _local_axes(target, supplied, source, transformer=None):
    """Numerical WGS84 surface-to-display Jacobian, not a vertical scale factor."""
    if abs(source[1]) >= 90-1e-9:
        raise ValueError("Geographic east/north directions are undefined at the pole; no local compass/scale is asserted")
    if transformer is None:
        transformer, _ = _operation(_crs("EPSG:4326", "source_crs"), target, source)
    units = _metadata(target, supplied)["axis_units"]
    factors = [unit["conversion_to_m"] for unit in units]
    center = transformer.transform(*source,errcheck=True)
    directions = []
    for azimuth in (90., 0.):
        plus = GEOD.fwd(source[0], source[1], azimuth, 1.)[:2]
        minus = GEOD.fwd(source[0], source[1], azimuth+180, 1.)[:2]
        xp, yp = transformer.transform(*plus, errcheck=True)
        xm, ym = transformer.transform(*minus, errcheck=True)
        chord = [(a-b)*factors[i] for i,(a,b) in enumerate(zip((xp,yp),(xm,ym)))]
        asymmetry = [(a+b-2*c)*factors[i] for i,(a,b,c) in enumerate(zip((xp,yp),(xm,ym),center))]
        if math.hypot(*asymmetry)>max(1e-6,.01*math.hypot(*chord)):
            raise ValueError("The +/-1 m neighborhood crosses a projection seam or is not locally smooth; its wrapped difference is not a scale/compass estimate")
        directions.append([(xp-xm)/2, (yp-ym)/2])
    if not all(math.isfinite(v) for row in directions for v in row):
        raise ValueError("Local projection derivatives are not finite")
    east, north = directions
    scaled = [[row[i]*factors[i] for i in range(2)] for row in directions]
    scale_e, scale_n = [math.hypot(*row) for row in scaled]
    if scale_e <= 0 or scale_n <= 0:
        raise ValueError("Local display axes are singular")
    cos_angle = sum(a*b for a,b in zip(*scaled))/(scale_e*scale_n)
    return {"method": "central WGS84 geodesic +/-1 m finite difference through the selected horizontal operation",
            "east_scale":scale_e, "north_scale":scale_n,
            "east_north_angle_deg":math.degrees(math.acos(max(-1.,min(1.,cos_angle)))),
            "grid_north_clockwise_deg":math.degrees(math.atan2(north[0], north[1])),
            "east_vector_native_units_per_m":east, "north_vector_native_units_per_m":north,
            "includes_horizontal_datum_operation":True,
            "height_or_combined_ground_scale":False}


def project_map(payload):
    """Project explicit map geometry atomically; failures return no partial map."""
    if not isinstance(payload, dict) or set(payload)-{"target_crs", "routes", "points", "layers", "config"}:
        raise ValueError("map request accepts target_crs, routes, points, layers and config only")
    try:
        input_bytes = len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode())
    except (TypeError, ValueError, RecursionError, OverflowError) as error:
        raise ValueError("map request must be finite JSON") from error
    if input_bytes > MAX_INPUT_BYTES:
        raise ValueError("map request exceeds 16 MiB")
    supplied = payload.get("target_crs")
    target = _crs(supplied, "target_crs")
    if not target.is_projected:
        raise ValueError("map target_crs must be a two-dimensional projected CRS")
    config = payload.get("config", {})
    if not isinstance(config, dict) or set(config)-{"max_vertices", "densify_max_distance_m", "max_operation_selections"}:
        raise ValueError("map config accepts max_vertices, densify_max_distance_m and max_operation_selections only")
    maximum = _integer(config.get("max_vertices", 100_000), "max_vertices", 2, MAX_VERTICES)
    maximum_selections = _integer(config.get("max_operation_selections", 2048), "max_operation_selections", 1, 10000)
    try:
        spacing = finite_number(config.get("densify_max_distance_m", 5000), "densify_max_distance_m", minimum=1, maximum=100_000)
    except OverflowError as error:
        raise ValueError("densify_max_distance_m must be a finite bounded distance") from error
    builder = _Geometry(maximum, spacing)
    result = {"can_display":False, "source_crs":"EPSG:4326", "target_crs":_metadata(target,supplied),
              "axis_order":"xy", "routes":[], "points":[], "layers":[], "bounds":None,
              "operations":[], "warnings":[], "errors":[], "budget":{},
              "assumptions":["Display coordinates only: no route, depth, vertical datum, manufacturing quantity or stored geometry is changed.",
                             "Route curves and date-line segments are supplied as explicit sampled geometry; their engineering KP remains geographic.",
                             "Reference GeoJSON edges are densified by geographic linear interpolation; supplied rings, holes, altitude and feature properties are retained.",
                             "Coordinates use native projected CRS units and always XY order; web tiles are not reprojected.",
                             "Local scales are horizontal WGS84 finite differences, not elevation-corrected ground/combined scale or measurement accuracy."]}
    staged = {"routes":[], "points":[], "layers":[]}
    for key,limit in (("routes",100), ("points",10_000), ("layers",1000)):
        entities = payload.get(key, [])
        if not isinstance(entities,list) or len(entities)>limit:
            raise ValueError(f"map {key} must be an array of at most {limit} objects")
        ids = set()
        for index, entity in enumerate(entities):
            builder.context = {"kind":key[:-1], "index":index, "id":entity.get("id") if isinstance(entity,dict) and isinstance(entity.get("id"),str) else None}
            try:
                if not isinstance(entity,dict):
                    raise ValueError("map entity must be an object")
                identifier = _identifier(entity.get("id"), f"{key}.id")
                if identifier in ids:
                    raise ValueError(f"Duplicate {key} id")
                ids.add(identifier)
                allowed = {"id", "label", "longitude", "latitude"} if key=="points" else {"id", "name", "role", "geometry"} if key=="routes" else {"id", "name", "kind", "visible", "geojson"}
                if set(entity)-allowed:
                    raise ValueError(f"Unsupported {key} entity fields: {sorted(set(entity)-allowed)}")
                item = deepcopy(entity)
                if key == "layers":
                    item["visible"] = entity.get("visible",True)
                if key=="points":
                    source = list(coordinate(entity.get("longitude"),entity.get("latitude")))
                    builder.input_vertices += 1
                    item = {"id":identifier, "label":str(entity.get("label",identifier)), "source":source,
                            "coordinates":builder.add(source, f"points/{identifier}"), "local_axes":None}
                elif key=="routes":
                    geometry = entity.get("geometry")
                    if not isinstance(geometry,dict) or geometry.get("type") not in {"LineString","MultiLineString"}:
                        raise ValueError("map route geometry must be sampled LineString or MultiLineString")
                    item["geometry"] = builder.geometry(geometry, f"routes/{identifier}")
                elif not isinstance(entity.get("visible", True),bool):
                    raise ValueError("map layer visible must be boolean")
                elif entity.get("visible", True):
                    item["geojson"] = builder.geojson(entity.get("geojson"), f"layers/{identifier}")
                else:
                    item["geojson"] = {"type":"FeatureCollection", "features":[]}
                    item["skipped_reason"] = "hidden layer is excluded from display projection"
                staged[key].append(item)
            except (ValueError,TypeError,KeyError,OverflowError,RecursionError) as error:
                result["errors"].append({"code":"MAP_VERTEX_BUDGET" if isinstance(error,_BudgetError) else "MAP_GEOMETRY_INVALID",
                                         "entity":dict(builder.context), "message":str(error)})
                if isinstance(error,_BudgetError):
                    break
        if any(e["code"]=="MAP_VERTEX_BUDGET" for e in result["errors"]):
            break
    result["warnings"] = builder.warnings
    result["budget"] = {"input_bytes":input_bytes, "input_vertices":builder.input_vertices,
                        "generated_vertices":len(builder.positions), "transformed_vertices":0, "max_vertices":maximum,
                        "reference_edge_max_distance_upper_bound_m":spacing,
                        "route_interpolation":"supplied sampled vertices, unchanged connectivity",
                        "reference_interpolation":"linear longitude/latitude, optional altitude interpolated without datum conversion",
                        "local_axis_diagnostic_limit":1000, "max_output_bytes":MAX_OUTPUT_BYTES,
                        "operation_selections":0, "max_operation_selections":maximum_selections,
                        "selection_work_basis":"conservative selection allowance charged for each started batch/axis diagnostic; may exceed actual PROJ attempts on early failure, not CPU FLOPs or a time guarantee"}
    if result["errors"]:
        return result
    if not builder.positions:
        result["errors"].append({"code":"MAP_EMPTY", "message":"No visible geometry or control points to display"})
        return result
    method = target.coordinate_operation.method_name.lower() if target.coordinate_operation is not None else ""
    if method.startswith("mercator") or "pseudo mercator" in method:
        for index,position in enumerate(builder.positions):
            if abs(position["y"]) == 90:
                _,context,path=builder.references[index]
                result["errors"].append({"code":"MAP_PROJECTION_DOMAIN", "entity":context, "path":path,
                                         "message":"A Mercator projection has no finite geographic-pole image; a finite floating-point approximation is not accepted"})
                return result
    same_datum = _crs("EPSG:4326","source_crs").datum == target.datum
    axis_count = min(len(staged["points"]),1000)
    selections = (math.ceil(len(builder.positions)/10000)+(1 if axis_count else 0)
                  if same_datum else len(builder.positions)+axis_count)
    result["budget"]["estimated_operation_selections"] = selections
    if selections>maximum_selections:
        result["errors"].append({"code":"MAP_OPERATION_BUDGET", "message":"Regional operation selection exceeds its declared budget; no coordinates were transformed. Reduce visible geometry or explicitly increase the bounded selection allowance."})
        return result
    notices = {}
    for start in range(0,len(builder.positions),10_000):
        try:
            result["budget"]["operation_selections"] += 1 if same_datum else len(builder.positions[start:start+10_000])
            batch = transform_coordinates({"source_crs":"EPSG:4326", "target_crs":supplied,
                                           "points":builder.positions[start:start+10_000]})
        except (ValueError,ProjError) as error:
            result["errors"].append({"code":"MAP_OPERATION_UNAVAILABLE", "message":str(error)})
            return result
        for operation in batch["operations"]:
            if operation not in result["operations"]:
                result["operations"].append(operation)
        for row in batch["points"]:
            value, context, path = builder.references[start+row["index"]]
            result["budget"]["transformed_vertices"] += 1
            if row["accepted"]:
                x,y = row["output"]["x"],row["output"]["y"]
                if max(abs(x),abs(y))>1e12:
                    result["errors"].append({"code":"MAP_PROJECTION_DOMAIN", "entity":context, "path":path,
                                             "message":"Projected display coordinate exceeds 1e12 native units"})
                else:
                    value[:2] = [x,y]
            else:
                result["errors"].append({"code":"MAP_PROJECTION_DOMAIN", "entity":context, "path":path,
                                         "message":row["error"]["message"]})
        for warning in batch["warnings"]:
            _,context,path = builder.references[start+warning["index"]]
            key = (context["kind"],context["id"],warning["code"])
            if key not in notices:
                notices[key] = {"code":warning["code"], "entity":context, "count":0, "example_path":path,
                                "message":warning["message"]}
            notices[key]["count"] += 1
        if result["errors"]:
            break
    result["warnings"].extend(notices.values())
    if result["errors"]:
        return result
    bounds = [min(p[0][0] for p in builder.references), min(p[0][1] for p in builder.references),
              max(p[0][0] for p in builder.references), max(p[0][1] for p in builder.references)]
    fixed_axes_transformer = None
    if same_datum and axis_count:
        result["budget"]["operation_selections"] += 1
        fixed_axes_transformer,_ = _operation(_crs("EPSG:4326","source_crs"),target)
    for point in staged["points"][:1000]:
        try:
            if not same_datum:
                result["budget"]["operation_selections"] += 1
            point["local_axes"] = _local_axes(target,supplied,point["source"],fixed_axes_transformer)
        except (ValueError,ArithmeticError,ProjError) as error:
            result["warnings"].append({"code":"MAP_LOCAL_AXES_UNAVAILABLE", "entity":{"kind":"point","id":point["id"]}, "message":str(error)})
    result.update(staged, bounds=bounds, can_display=True)
    output_bytes = len(json.dumps(result,ensure_ascii=False,allow_nan=False).encode())
    result["budget"]["output_bytes_before_budget_field"] = output_bytes
    if output_bytes>MAX_OUTPUT_BYTES-256:
        result.update(can_display=False,routes=[],points=[],layers=[],bounds=None)
        result["errors"].append({"code":"MAP_OUTPUT_BUDGET", "message":"Projected display output exceeds its 64 MiB bound"})
    return result
