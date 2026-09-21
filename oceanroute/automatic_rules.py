"""Declared geographic screening rules; independent of Makai native formats.

Predicates describe error triggers, not implicitly inverted engineering limits.
The bounded geometry model, sampled terrain scope and unresolved references are
reported independently from real violations. This module never persists data.
"""
from __future__ import annotations

import bisect
from copy import deepcopy
import hashlib
import json
import math

from .geodesy import GEOD, finite_number, inverse, interpolate
from .route_geometry import route_segments

MODEL = "automatic-geographic-rules-v1"
PACKAGE_SCHEMA = "oceanroute.automatic-rules/v1"


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError("Automatic rules require finite JSON values") from error


def _canonical(value):
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _canonical(child) for key, child in sorted(value.items())}
    if isinstance(value, list):
        return [_canonical(child) for child in value]
    return value


def _hash(value):
    return hashlib.sha256(_json(_canonical(value))).hexdigest()


def _object(value, fields, name):
    if not isinstance(value, dict) or set(value)-set(fields):
        raise ValueError(f"{name} must be an object with only declared fields")
    return value


def _text(value, name, maximum=128):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be nonempty text of at most {maximum} characters")
    return value.strip()


def _integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _typed_id(value):
    if isinstance(value, str):
        if len(value) > 1024:
            raise ValueError("native feature ID is too long")
        return "s", value
    if type(value) is int:
        finite_number(value, 'feature ID')
        return 'n', value
    if type(value) is float:
        value = finite_number(value, "feature ID")
        return "n", int(value) if value.is_integer() else value
    raise ValueError("native feature ID must be a string or finite number")


def _selectors(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 1000:
        raise ValueError("selectors must contain 1..1000 GIS layer selections")
    result = []
    for item in values:
        _object(item, {"layer_id", "feature_ids", "feature_indexes"}, "GIS selector")
        if ("feature_ids" in item) == ("feature_indexes" in item):
            raise ValueError("selector requires exactly one of feature_ids or feature_indexes")
        row = {"layer_id": _text(item.get("layer_id"), "layer_id")}
        field = "feature_ids" if "feature_ids" in item else "feature_indexes"
        values = item[field]
        if values is None and field == "feature_ids":
            row[field] = None
        else:
            if not isinstance(values, list) or not 1 <= len(values) <= 20000:
                raise ValueError("selected IDs/indexes must contain 1..20000 entries")
            keys = [_typed_id(v) if field == "feature_ids" else _integer(v, "feature index", 0, 100000) for v in values]
            if len(set(keys)) != len(keys):
                raise ValueError("duplicate feature references are not allowed")
            row[field] = [key[1] for key in keys] if field == "feature_ids" else list(values)
        result.append(row)
    return result


def normalize_automatic_rules(rules, *, max_rules=512):
    maximum = _integer(max_rules, "max_rules", 1, 512)
    if not isinstance(rules, list) or len(rules) > maximum:
        raise ValueError(f"automatic_rules must be an array of at most {maximum} rules; no truncation")
    result = []
    seen = set()
    common = {"id", "name", "enabled", "path_id", "kind", "start_kp_m", "end_kp_m"}
    specific = {
        "crossing": {"selectors", "conditions", "match_mode", "body_distance_mode"},
        "proximity": {"around", "targets", "selectors", "distance_m", "water_depth_m", "slope_threshold_deg", "slope_probe_spacing_m", "slope_vertical_datum"},
        "slope": {"slope_basis", "max_inline_slope_deg", "max_side_slope_deg"},
    }
    for item in rules:
        if not isinstance(item, dict) or not isinstance(item.get('kind'),str) or item.get("kind") not in specific:
            raise ValueError("rule kind must be crossing, proximity or slope")
        kind = item["kind"]
        _object(item, common | specific[kind], "automatic rule")
        identifier = _text(item.get("id"), "rule.id")
        if identifier in seen:
            raise ValueError("automatic rule IDs must be unique")
        seen.add(identifier)
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("rule.enabled must be boolean")
        start = finite_number(item.get("start_kp_m", 0), "start_kp_m", minimum=0, maximum=1e9)
        end = None if item.get("end_kp_m") is None else finite_number(item["end_kp_m"], "end_kp_m", minimum=0, maximum=1e9)
        if end is not None and start >= end:
            raise ValueError("numeric rule end_kp_m must exceed start_kp_m")
        row = {"id": identifier, "name": _text(item.get("name", identifier), "rule.name", 512),
               "enabled": enabled, "path_id": _text(item.get("path_id"), "rule.path_id"),
               "kind": kind, "start_kp_m": start, "end_kp_m": end}
        if kind == "crossing":
            row["selectors"] = _selectors(item.get("selectors"))
            conditions = item.get("conditions", [])
            if not isinstance(conditions, list) or len(conditions) > 3:
                raise ValueError("crossing conditions must contain at most three fields")
            fields = set()
            row["conditions"] = []
            for condition in conditions:
                _object(condition, {"field", "comparison", "value"}, "crossing predicate")
                field = condition.get("field")
                comparison = condition.get("comparison")
                if not isinstance(field,str) or not isinstance(comparison,str) or field not in {"angle_deg", "depth_m", "body_distance_m"} or field in fields or comparison not in {"lt", "gt", "le", "ge"}:
                    raise ValueError("invalid or duplicated crossing predicate")
                fields.add(field)
                value = finite_number(condition.get("value"), "predicate.value", minimum=0, maximum=90 if field == "angle_deg" else 1e9)
                row["conditions"].append({"field": field, "comparison": comparison, "value": value})
            row["match_mode"] = item.get("match_mode", "all")
            row["body_distance_mode"] = item.get("body_distance_mode", "horizontal")
            if not isinstance(row['match_mode'],str) or not isinstance(row['body_distance_mode'],str) or row["match_mode"] not in {"all", "any"} or row["body_distance_mode"] not in {"horizontal", "route_kp"}:
                raise ValueError("invalid crossing match/distance mode")
        elif kind == "proximity":
            row["around"] = item.get("around")
            targets = item.get("targets")
            if not isinstance(row['around'],str) or row["around"] not in {"path", "bodies", "altercourses", "transitions"} or not isinstance(targets, list) or not targets or any(not isinstance(t,str) for t in targets) or len(set(targets)) != len(targets) or any(t not in {"bodies", "altercourses", "transitions", "gis", "slopes"} for t in targets):
                raise ValueError("invalid proximity subject/target groups")
            if row["around"] == "path" and targets != ["gis"]:
                raise ValueError("whole-path proximity may only target GIS features")
            if "slopes" in targets and row["around"] != "bodies":
                raise ValueError("seabed slope neighborhoods may only surround bodies")
            row["targets"] = list(targets)
            row["distance_m"] = finite_number(item.get("distance_m"), "distance_m", minimum=0, maximum=100000)
            depth = item.get("water_depth_m", {"min_m": 0, "max_m": None})
            _object(depth, {"min_m", "max_m"}, "water_depth_m")
            minimum = finite_number(depth.get("min_m", 0), "min_m", minimum=0, maximum=20000)
            maximum_depth = None if depth.get("max_m") is None else finite_number(depth["max_m"], "max_m", minimum=0, maximum=20000)
            if maximum_depth is not None and maximum_depth < minimum:
                raise ValueError("water depth range must be ordered")
            row["water_depth_m"] = {"min_m": minimum, "max_m": maximum_depth}
            if "gis" in targets:
                row["selectors"] = _selectors(item.get("selectors"))
            elif "selectors" in item:
                raise ValueError("GIS selectors require the gis target")
            if "slopes" in targets:
                if row["distance_m"] < .001:
                    raise ValueError("two-dimensional slope neighborhoods require distance_m >= .001")
                row["slope_threshold_deg"] = finite_number(item.get("slope_threshold_deg"), "slope_threshold_deg", minimum=0, maximum=89.999999)
                row["slope_probe_spacing_m"] = finite_number(item.get("slope_probe_spacing_m", 50), "slope_probe_spacing_m", minimum=.001, maximum=100000)
                if "slope_vertical_datum" in item:
                    row["slope_vertical_datum"] = _text(item["slope_vertical_datum"], "slope_vertical_datum")
            elif set(item) & {"slope_threshold_deg", "slope_probe_spacing_m", "slope_vertical_datum"}:
                raise ValueError("slope parameters require the slopes target")
        else:
            from .slope_rules import normalize_slope_rules
            basis = item.get("slope_basis", "both")
            slope = {key: value for key, value in row.items() if key not in {"path_id", "kind"}}
            slope["kind"] = basis
            for field in ("max_inline_slope_deg", "max_side_slope_deg"):
                if field in item:
                    slope[field] = item[field]
            validated = normalize_slope_rules([slope], max_rules=1)[0]
            row["slope_basis"] = validated["kind"]
            row.update({key: value for key, value in validated.items() if key in {"max_inline_slope_deg", "max_side_slope_deg"}})
        result.append(row)
    _json(result)
    return result


def _config(config):
    config = {} if config is None else config
    limits = {"max_rules": (128, 1, 512), "max_features": (20000, 1, 100000),
              "max_vertices": (250000, 1, 1000000), "max_pairs": (1000000, 1, 10000000),
              "max_events": (10000, 1, 100000), "max_work_units": (30000000, 1, 200000000),
              "max_output_bytes": (16*1024**2, 1024, 64*1024**2)}
    fields = set(limits) | {"rules", "geometry_tolerance_m", "max_segment_m", "max_curve_chunk_m"}
    _object(config, fields, "automatic checker config")
    result = {key: _integer(config.get(key, default), key, low, high) for key, (default, low, high) in limits.items()}
    for key, default, low, high in (("geometry_tolerance_m", 1, .01, 100), ("max_segment_m", 1000, 10, 10000), ("max_curve_chunk_m", 100000, 1000, 200000)):
        result[key] = finite_number(config.get(key, default), key, minimum=low, maximum=high)
    if "rules" in config:
        result["rules"] = config["rules"]
    return result


class _Budget:
    def __init__(self, config):
        self.config = config
        self.work = self.vertices = self.pairs = self.events = self.features = 0
        self.terrain_query_count = 0
        self.output_upper = 0

    def charge(self, count=1):
        self.work += int(count)
        if self.work > self.config["max_work_units"]:
            _evaluation_error("AUTOMATIC_RULE_WORK_LIMIT", "Complete report rejected; no truncation")

    def retain(self, value):
        # Violations/diagnostics also appear in errors/warnings. Reserve their
        # duplicated finite JSON before accumulating an unbounded report.
        self.output_upper += 3*len(_json(value))+1024
        if self.output_upper>self.config["max_output_bytes"]:
            _evaluation_error("AUTOMATIC_RULE_OUTPUT_LIMIT", "Complete result exceeds output budget; no truncation")

    def add(self, field, count=1):
        value = getattr(self, field)+int(count)
        if value > self.config["max_"+field]:
            _evaluation_error(f"AUTOMATIC_RULE_{field.upper()}_LIMIT", "Complete report rejected; no truncation")
        setattr(self, field, value)
        self.charge(count)


class _RetainedList(list):
    def __init__(self,budget):
        super().__init__(); self.budget=budget
    def append(self,value):
        self.budget.retain(value); super().append(value)
    def extend(self,values):
        for value in values:self.append(value)


def _workspace(workspace, analyses=None):
    if not isinstance(workspace, dict) or workspace.get("schema_version") != 2:
        raise ValueError("automatic rules require a complete schema2 workspace")
    if len(_json(workspace)) > 32*1024**2:
        raise ValueError("automatic workspace input exceeds 32 MiB")
    if analyses is None:
        from .workspace import _validate
        return _validate(workspace,_check_legacy_crossings=False)[:2]
    if not isinstance(analyses, dict):
        raise ValueError("analyses must be a current path-ID map")
    return workspace, analyses


def _materialize(workspace, path):
    from .workspace import _materialize as materialize
    return materialize(workspace, path)


def _features(layer):
    geo = layer.get("geojson", {})
    if not isinstance(geo, dict):
        raise ValueError("layer.geojson must be an object")
    rows = geo.get("features", []) if geo.get("type") == "FeatureCollection" else [geo]
    if not isinstance(rows, list):
        raise ValueError("GeoJSON features must be an array")
    return rows


def automatic_rule_catalog(workspace):
    if not isinstance(workspace, dict) or workspace.get("schema_version") != 2:
        raise ValueError("catalog requires a schema2 workspace")
    if len(_json(workspace))>32*1024**2: raise ValueError("catalog workspace exceeds32 MiB")
    source_features=0
    paths = []
    for path in workspace.get("paths", []):
        points = path["project"]["route"]["points"]
        curve = path["project"]["route"].get("curve", "rhumb")
        total = sum(segment.length_m for segment in route_segments(path["project"]))
        paths.append({"id": path["id"], "name": path.get("name", path["id"]), "start_kp_m": 0., "end_kp_m": total})
    layers = []
    catalog_upper=1024+len(_json(paths))
    for layer in workspace.get("layers", []):
        rows = _features(layer)
        source_features+=len(rows)
        if source_features>100000: _evaluation_error("AUTOMATIC_RULE_CATALOG_LIMIT","Catalog exceeds100000 features; no truncation")
        counts = {}
        for feature in rows:
            if isinstance(feature, dict) and feature.get("id") is not None:
                token = _typed_id(feature["id"])
                counts[token] = counts.get(token, 0)+1
        catalog = []
        for index, feature in enumerate(rows):
            if not isinstance(feature, dict):
                raise ValueError("GeoJSON feature must be an object")
            identifier = feature.get("id")
            unique = identifier is not None and counts[_typed_id(identifier)] == 1
            geometry = feature.get("geometry") if feature.get("type") == "Feature" else feature
            props = feature.get("properties") or {}
            if not isinstance(props,dict): raise ValueError("Feature.properties must be an object or null")
            catalog.append({"id": identifier, "index": index, "name": str(props.get("name", identifier if identifier is not None else f"Feature {index}")),
                            "geometry_type": geometry.get("type") if isinstance(geometry, dict) else None,
                            "id_unique": unique,
                            "selector": {"layer_id": layer["id"], "feature_ids": [identifier]} if unique else {"layer_id": layer["id"], "feature_indexes": [index]}})
        descriptor={"id": layer["id"], "name": layer.get("name", layer["id"]), "source_signature":_hash({k:v for k,v in layer.items() if k not in {"visible","opacity","display_order","name"}}), "features": catalog}
        catalog_upper+=len(_json(descriptor))
        if catalog_upper>16*1024**2:_evaluation_error("AUTOMATIC_RULE_CATALOG_OUTPUT_LIMIT","Catalog exceeds16 MiB; no truncation")
        layers.append(descriptor)
    result = {"paths": paths, "layers": layers, "budget":{"features":source_features,"max_features":100000,"max_output_bytes":16*1024**2},"scope": "Native typed IDs or explicit indexes; hidden layers remain selectable"}
    if len(_json(result)) > 16*1024**2:
        _evaluation_error("AUTOMATIC_RULE_CATALOG_OUTPUT_LIMIT","Catalog exceeds16 MiB; no truncation")
    return result


class AutomaticRuleEvaluationError(ValueError):
    """Declared screening budget/scope failure; never an empty clear result."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _evaluation_error(code, message):
    raise AutomaticRuleEvaluationError(code, message)


def _diag(code, message, **extra):
    return {"code": code, "message": message, **extra}


def _selected(workspace, selectors, budget):
    from .automatic_rule_geometry import primitive_records, vertex_count
    layers = {layer['id']: layer for layer in workspace.get('layers', [])}
    selected, diagnostics, seen = [], [], set()
    for selector in selectors:
        layer = layers.get(selector['layer_id'])
        if layer is None:
            diagnostics.append(_diag('LAYER_REFERENCE_MISSING', 'Selected GIS layer is absent', layer_id=selector['layer_id']))
            continue
        rows = _features(layer)
        budget.charge(len(rows))
        matches = []
        if 'feature_indexes' in selector:
            for index in selector['feature_indexes']:
                if index >= len(rows):
                    diagnostics.append(_diag('FEATURE_REFERENCE_MISSING', 'Selected source feature index is absent', layer_id=layer['id'], feature_index=index))
                else: matches.append(index)
        elif selector['feature_ids'] is None:
            matches = list(range(len(rows)))
        else:
            lookup = {}
            for index, feature in enumerate(rows):
                if isinstance(feature, dict) and feature.get('id') is not None:
                    lookup.setdefault(_typed_id(feature['id']), []).append(index)
            for identifier in selector['feature_ids']:
                indexes = lookup.get(_typed_id(identifier), [])
                if len(indexes) != 1:
                    diagnostics.append(_diag('FEATURE_REFERENCE_AMBIGUOUS' if indexes else 'FEATURE_REFERENCE_MISSING',
                                             'Native typed feature ID must resolve to exactly one source feature', layer_id=layer['id'], feature_id=identifier))
                else: matches.extend(indexes)
        for index in matches:
            if (layer['id'],index) in seen: continue
            seen.add((layer['id'],index)); budget.add('features')
            feature = rows[index]
            if not isinstance(feature, dict): raise ValueError('GeoJSON feature must be an object')
            geometry = feature.get('geometry') if feature.get('type')=='Feature' else feature
            identity = {'layer_id':layer['id'], 'feature_id':feature.get('id'), 'feature_index':index, 'feature_geometry_signature':_hash(geometry),'layer_source_signature':_hash({k:v for k,v in layer.items() if k not in {'geojson','visible','opacity','display_order','name'}})}
            for primitive_path,geom,error in primitive_records(geometry):
                if error is not None:
                    diagnostics.append(_diag('GIS_GEOMETRY_UNAVAILABLE',error,**identity,primitive_path=list(primitive_path)))
                    continue
                budget.add('vertices', vertex_count(geom))
                selected.append({'identity':{**identity,'primitive_path':list(primitive_path)}, 'geometry':geom})
    return selected, diagnostics


class _Profile:
    def __init__(self, project, route, budget):
        # Use the same signature/NoData source admission as the core, without
        # invoking manufacture analysis or recursively invoking this checker.
        from .core import _profiles, route_signature
        warnings=[]
        points=[{**p,'depth_m':p.get('depth_m')} for p in route.points]
        budget.charge(len(points)+len(project.get('profile',{}).get('samples',[])))
        self.rows,self.at,self.metadata=_profiles(project,points,route.kps,route_signature(project),warnings)
        self.warnings=warnings

    def intervals(self, start, end, depth_range):
        """Clip actual valid linear-depth intervals; never bridge missing depth."""
        good, missing = [], []
        cursor=start
        for a,b in zip(self.rows,self.rows[1:]):
            lo,hi=max(start,a['kp_m']),min(end,b['kp_m'])
            if hi<=lo: continue
            if lo>cursor+1e-7: missing.append([cursor,lo])
            cursor=hi
            if a['depth_m'] is None or b['depth_m'] is None:
                missing.append([lo,hi]); continue
            d0,d1=self.at(lo),self.at(hi)
            lower,upper=depth_range['min_m'],depth_range['max_m']
            if abs(d1-d0)<1e-15:
                if d0>=lower and (upper is None or d0<=upper): good.append([lo,hi])
            else:
                values=[0.,1.]
                for d in (lower,upper):
                    if d is not None: values.append((d-d0)/(d1-d0))
                values=sorted(set(max(0.,min(1.,v)) for v in values))
                for x,y in zip(values,values[1:]):
                    d=d0+(d1-d0)*(x+y)/2
                    if d>=lower and (upper is None or d<=upper): good.append([lo+(hi-lo)*x,lo+(hi-lo)*y])
        if cursor<end-1e-7: missing.append([cursor,end])
        # A closed depth filter may select a lone exact depth crossing or an
        # independently known sample between gaps. Retain actual singletons.
        candidates=[]
        for a,b in zip(self.rows,self.rows[1:]):
            if a['depth_m'] is None or b['depth_m'] is None or a['depth_m']==b['depth_m']:continue
            for depth in (depth_range['min_m'],depth_range['max_m']):
                if depth is None:continue
                f=(depth-a['depth_m'])/(b['depth_m']-a['depth_m'])
                if 0<=f<=1:candidates.append(a['kp_m']+(b['kp_m']-a['kp_m'])*f)
        candidates.extend(p['kp_m'] for p in self.rows if p['depth_m'] is not None)
        for kp in candidates:
            if not start<=kp<=end:continue
            depth=self.at(kp)
            if depth is None or depth<depth_range['min_m']-1e-10 or (depth_range['max_m'] is not None and depth>depth_range['max_m']+1e-10):continue
            if not any(lo-1e-7<=kp<=hi+1e-7 for lo,hi in good):good.append([kp,kp])
        return sorted(good),missing


def _location(route, profile, path_id, kp, **extra):
    lon,lat=route.at(kp)
    return {'path_id':path_id,'kp_m':kp,'longitude':lon,'latitude':lat,'depth_m':profile.at(kp),**extra}


def _objects(project, analysis, route, profile):
    rows={'bodies':[], 'altercourses':[], 'transitions':[]}
    for body in analysis.get('bodies',[]):
        kp=finite_number(body['kp_m'],'resolved body kp',minimum=0,maximum=route.total+1e-7)
        rows['bodies'].append({'kind':'bodies','id':body['id'],'name':body.get('name',body['id']),'kp_m':kp})
    for i,kp in enumerate(route.kps[1:-1],1):
        before=route.legs[i-1] if route.legs[i-1].length>1e-7 else None
        after=route.legs[i] if route.legs[i].length>1e-7 else None
        if before and after:
            az0=before.tangent(1.)
            az1=after.tangent(0.)
            turn=abs((az1-az0+180)%360-180)
            if turn>1e-6:
                rows['altercourses'].append({'kind':'altercourses','id':route.points[i]['id'],'name':route.points[i].get('label',route.points[i]['id']), 'kp_m':kp,'turn_deg':turn})
        legs=analysis.get('legs',[])
        if before and after and i<len(legs) and legs[i-1]['cable_type_id']!=legs[i]['cable_type_id']:
            rows['transitions'].append({'kind':'transitions','id':route.points[i]['id'],'name':route.points[i].get('label',route.points[i]['id']), 'kp_m':kp,'from_cable_type_id':legs[i-1]['cable_type_id'],'to_cable_type_id':legs[i]['cable_type_id']})
    return rows


def _predicate(value, comparison, threshold, uncertainty=0.):
    if value is None: return None
    delta=value-threshold
    epsilon=8*max(math.ulp(float(value)),math.ulp(float(threshold)))
    if uncertainty and abs(delta)<=uncertainty+epsilon: return None
    if abs(delta)<=epsilon: delta=0.
    return {'lt':delta<0,'gt':delta>0,'le':delta<=0,'ge':delta>=0}[comparison]


def _trigger(predicates, mode):
    if not predicates: return True
    values=[row['triggered'] for row in predicates]
    if mode=='all': return False if False in values else None if None in values else True
    return True if True in values else None if None in values else False


def _body_distance(event_kp, location, objects, mode, budget):
    if not objects['bodies']: return None
    if mode=='route_kp': return min(abs(event_kp-b['kp_m']) for b in objects['bodies'])
    budget.charge(len(objects['bodies']))
    return min(abs(GEOD.inv(location['longitude'],location['latitude'],b['longitude'],b['latitude'])[2]) for b in objects['bodies'])


def _crossing_rule(rule, row, selected, project, analysis, route, profile, objects, budget):
    from .automatic_rule_geometry import contacts, closest_primitive
    tolerance=budget.config['geometry_tolerance_m']
    start,end=row['coverage']['checked_range_m']
    unresolved=0
    for _,curve in route.curves(start,end):
        try:rendered=route.rendered(curve)
        except ValueError as error:_evaluation_error('AUTOMATIC_RULE_GEOMETRY_RESOLUTION',str(error))
        if max(abs(p[1]) for _,p in rendered)>85:
            row['diagnostics'].append(_diag('GEOMETRY_POLAR_SCOPE', 'Geographic overlay above 85 degrees latitude is unresolved'))
            unresolved+=1; continue
        for source in selected:
            events=contacts(curve,rendered,source['geometry'],budget)
            if not events:
                near=closest_primitive(curve,source['geometry'],budget,tolerance/4)
                if near and near['lower_bound_m']<=tolerance:
                    unresolved+=1
                    row['diagnostics'].append(_diag('GEOMETRY_NEAR_CONTACT', 'A contact within the geographic rendering tolerance cannot be excluded', source=source['identity'],distance_bounds_m=[near['lower_bound_m'],near['upper_bound_m']]))
                continue
            for event in events:
                if not event.get('verified',False):
                    unresolved+=1
                    row['diagnostics'].append(_diag('GEOMETRY_CONTACT_UNVERIFIED','Rendered contact cannot be confirmed on the actual route curve',source=source['identity'],location=_location(route,profile,rule['path_id'],curve.kp(event['fraction']))))
                    continue
                budget.add('events')
                kp=curve.kp(event['fraction'])
                location=_location(route,profile,rule['path_id'],kp)
                body=_body_distance(kp,location,objects,rule['body_distance_mode'],budget)
                values={'angle_deg':event['angle_deg'],'depth_m':location['depth_m'],'body_distance_m':body}
                predicates=[{**c,'actual':values[c['field']],'triggered':_predicate(values[c['field']],c['comparison'],c['value'])} for c in rule['conditions']]
                trigger=_trigger(predicates,rule['match_mode'])
                if trigger is None:
                    unresolved+=1; row['diagnostics'].append(_diag('CROSSING_PREDICATE_UNKNOWN','Missing depth, body or unambiguous incident tangent prevents predicate evaluation',location=location,source=source['identity'],event=event['event'],predicates=predicates))
                elif trigger:
                    if any(v['source']==source['identity'] and v['event']==event['event'] and abs(v['kp_m']-kp)<1e-5 for v in row['violations']):continue
                    row['violations'].append({'event':event['event'],'location':location,'kp_m':kp,'source':source['identity'],'values':values,'predicates':predicates,'match_mode':rule['match_mode'],'message':f"GIS {event['event']} triggers declared crossing rule",'geometry_basis':'adaptive geographic curve overlay; actual WGS84 route station'})
    row['coverage'].update(geometry_complete=unresolved==0,unresolved_contact_count=unresolved)


def _gis_proximity(rule,row,subjects,selected,route,profile,budget):
    from .automatic_rule_geometry import point_curve, closest_primitive, contacts
    tolerance=budget.config['geometry_tolerance_m']
    radius=rule['distance_m']
    for subject in subjects:
        curve=subject['curve']
        for source in selected:
            event=None
            polar=curve.kind!='native' and any(abs(curve.at(t)[1])>85 for t in (0.,.5,1.))
            if polar and source['geometry'].geom_type=='Polygon':
                row['diagnostics'].append(_diag('GEOMETRY_POLAR_SCOPE','Polygon containment along a polar route interval is unresolved',target=source['identity']))
                continue
            if curve.kind!='native' and not polar:
                events=contacts(curve,route.rendered(curve),source['geometry'],budget)
                event=next((e for e in events if e.get('verified')),None)
            near=closest_primitive(curve,source['geometry'],budget,tolerance/4)
            if event:
                p=curve.at(event['fraction'])
                near={'distance_m':0.,'lower_bound_m':0.,'upper_bound_m':0.,'first_fraction':event['fraction'],'first':p,'second':p}
            if near is None: continue
            kp=curve.kp(near['first_fraction']) if curve.kind!='native' else subject['kp_m']
            location=_location(route,profile,rule['path_id'],kp,radius_m=radius)
            if near['upper_bound_m']<=radius+1e-9:
                budget.add('events')
                row['violations'].append({'event':'proximity','location':location,'kp_m':kp,'subject':subject.get('identity',{'kind':'path'}),'target':source['identity'],'distance_m':near['distance_m'],'distance_bounds_m':[near['lower_bound_m'],near['upper_bound_m']],'target_location':{'longitude':near['second'][0],'latitude':near['second'][1]},'threshold_m':radius,'message':'Horizontal separation is within the declared proximity radius'})
            elif near['lower_bound_m']<=radius:
                row['diagnostics'].append(_diag('PROXIMITY_THRESHOLD_UNCERTAIN','Continuous-curve distance bound straddles the declared radius',location=location,target=source['identity'],distance_bounds_m=[near['lower_bound_m'],near['upper_bound_m']]))


def _proximity_rule(rule,row,selected,route,profile,objects,budget,slope_jobs):
    from .automatic_rule_geometry import point_curve
    start,end=row['coverage']['checked_range_m']
    depth_range=rule['water_depth_m']
    if rule['around']=='path':
        good,missing=profile.intervals(start,end,depth_range)
        if missing: row['diagnostics'].append(_diag('PROXIMITY_DEPTH_UNAVAILABLE','Subject depth filter cannot be evaluated across missing/stale depths',unknown_ranges_m=missing))
        subjects=[]
        for lo,hi in good:
            if hi==lo:
                subjects.append({'curve':point_curve(route.at(lo)),'identity':{'kind':'path'},'kp_m':lo})
                continue
            subjects.extend({'curve':curve,'identity':{'kind':'path'}} for _,curve in route.curves(lo,hi))
        row['coverage'].update(depth_filter_complete=not missing,depth_eligible_ranges_m=good,unknown_depth_ranges_m=missing)
    else:
        subjects=[]
        for obj in objects[rule['around']]:
            kp=obj['kp_m']
            if not start-1e-7<=kp<=end+1e-7: continue
            depth=profile.at(kp)
            if depth is None:
                row['diagnostics'].append(_diag('PROXIMITY_DEPTH_UNAVAILABLE','Subject depth is missing/stale; subject eligibility is unresolved',subject={k:v for k,v in obj.items() if k not in {'longitude','latitude'}},location=_location(route,profile,rule['path_id'],kp)))
                continue
            if depth<depth_range['min_m'] or (depth_range['max_m'] is not None and depth>depth_range['max_m']): continue
            subjects.append({'curve':point_curve((obj['longitude'],obj['latitude'])),'identity':{k:v for k,v in obj.items() if k not in {'longitude','latitude'}},'kp_m':kp})
        row['coverage'].update(subject_count=len(subjects),depth_filter_complete=not any(d['code']=='PROXIMITY_DEPTH_UNAVAILABLE' for d in row['diagnostics']))
    if 'gis' in rule['targets']: _gis_proximity(rule,row,subjects,selected,route,profile,budget)
    for target_kind in rule['targets']:
        if target_kind in {'gis','slopes'}: continue
        for subject in subjects:
            for target in objects[target_kind]:
                if subject['identity']['kind']==target_kind and subject['identity']['id']==target['id']: continue
                budget.add('pairs')
                p=subject['curve'].at(0.)
                distance=abs(GEOD.inv(*p,target['longitude'],target['latitude'])[2])
                if distance<=rule['distance_m']+1e-9:
                    budget.add('events'); kp=subject['kp_m']
                    row['violations'].append({'event':'proximity','kp_m':kp,'location':_location(route,profile,rule['path_id'],kp,radius_m=rule['distance_m']),'subject':subject['identity'],'target':{k:v for k,v in target.items() if k not in {'longitude','latitude'}},'distance_m':distance,'threshold_m':rule['distance_m'],'target_location':{'longitude':target['longitude'],'latitude':target['latitude']},'message':'Object separation is within the declared proximity radius'})
    if 'slopes' in rule['targets']:
        for subject in subjects:
            slope_jobs.append({'rule':rule,'row':row,'subject':subject,'route':route,'profile':profile,'center':_location(route,profile,rule['path_id'],subject['kp_m'])})


def _slope_neighborhoods(jobs,projects,budget):
    from .terrain_slope_neighborhoods import SlopeNeighborhoodSampler
    if len(jobs)>200:_evaluation_error('AUTOMATIC_RULE_TERRAIN_WINDOW_LIMIT','All rules exceed200 slope windows; no truncation')
    groups={}
    for job in jobs:
        rule=job['rule']; key=(rule['path_id'],rule['slope_probe_spacing_m'],rule.get('slope_vertical_datum'))
        groups.setdefault(key,[]).append(job)
    # Preflight every group before terrain queries, with a cumulative budget.
    batches=[]; estimates=0; query_upper=0
    for (path_id,spacing,datum),rows in groups.items():
        config={'spacing_m':spacing,'max_work_units':min(200000000,budget.config['max_work_units']),'max_output_bytes':budget.config['max_output_bytes']}
        if datum is not None: config['vertical_datum']=datum
        try:sampler=SlopeNeighborhoodSampler(projects[path_id],config)
        except ValueError as error:_evaluation_error('AUTOMATIC_RULE_TERRAIN_BUDGET' if 'max_' in str(error) else 'AUTOMATIC_RULE_TERRAIN_SCOPE',str(error))
        windows=[{'center':{k:job['center'][k] for k in ('longitude','latitude','kp_m')},'radius_m':job['rule']['distance_m']} for job in rows]
        try: estimate=sampler.estimate_many(windows)
        except ValueError as error: _evaluation_error('AUTOMATIC_RULE_TERRAIN_SCOPE',str(error))
        estimates+=estimate['estimated_work_units']; query_upper+=estimate['query_count_upper_bound']
        if query_upper>50000 or estimates+budget.work>budget.config['max_work_units']:
            _evaluation_error('AUTOMATIC_RULE_TERRAIN_BUDGET','All rule slope windows exceed cumulative query/work budget; no truncation')
        batches.append((sampler,windows,rows))
    for sampler,windows,rows in batches:
        try: batch=sampler.sample_many(windows)
        except ValueError as error: _evaluation_error('AUTOMATIC_RULE_TERRAIN_EVALUATION',str(error))
        budget.charge(batch['budget']['work_units']); budget.terrain_query_count+=batch['budget']['query_count']
        for job,window in zip(rows,batch['neighborhoods']):
            rule,row=job['rule'],job['row']; quality=window['quality']
            row['coverage'].setdefault('slope_neighborhoods',[]).append({'subject':job['subject']['identity'],'quality':quality,'metadata':window.get('metadata',{})})
            if not quality['sample_complete'] or not quality['triangle_complete'] or quality['source_boundary']:
                row['diagnostics'].append(_diag('TERRAIN_SLOPE_WINDOW_INCOMPLETE','Actual two-dimensional probes include NoData or source boundaries; unknown triangles are not interpolated',subject=job['subject']['identity'],quality=quality))
            for triangle in window['triangles']:
                value=triangle['slope_deg']
                if value is None or not _predicate(value,'gt',rule['slope_threshold_deg']): continue
                budget.add('events')
                witness=triangle.get('sampled_witness')
                center=triangle['centroid']
                location={**job['center'],'longitude':center['longitude'],'latitude':center['latitude'],'depth_m':None,'radius_m':rule['distance_m'],'radius_center':{'longitude':job['center']['longitude'],'latitude':job['center']['latitude']},'location_kind':'geometric_child_centroid_not_queried'}
                row['violations'].append({'event':'terrain_slope','kp_m':job['center']['kp_m'],'location':location,'subject':job['subject']['identity'],'slope_deg':value,'threshold_deg':rule['slope_threshold_deg'],'triangle_index':triangle['index'],'vertex_indices':triangle['vertex_indices'],'support_indices':triangle['support_indices'],'queried_vertices':[{k:window['samples'][i].get(k) for k in ('index','query_index','x_m','y_m','longitude','latitude','depth_m','source_id','source_fingerprint','fallback','fallback_count')} for i in triangle['vertex_indices']],'sampled_witness':witness,'source_id':triangle.get('source_id'),'source_fingerprint':triangle.get('source_fingerprint',triangle.get('fingerprint')) or (witness or {}).get('source_fingerprint'),'gradient_height':triangle['gradient_height'],'message':'Actual two-dimensional sampled triangle exceeds the declared slope threshold','continuous_bed_verified':False})
            row['coverage']['terrain_sampling_only']=True


def check_automatic_rules(workspace, config=None, *, analyses=None):
    """Check declared rules without modifying any input or manufacturing entity."""
    from .automatic_rule_geometry import Route
    config=_config(config); workspace,analyses=_workspace(workspace,analyses)
    rules=normalize_automatic_rules(config.get('rules',workspace.get('automatic_rules',[])),max_rules=config['max_rules'])
    budget=_Budget(config); budget.charge(len(rules))
    paths={p['id']:p for p in workspace['paths']}
    contexts={}; projects={}; results=[]; slope_jobs=[]
    for rule in rules:
        row={'rule_id':rule['id'],'id':rule['id'],'name':rule['name'],'path_id':rule['path_id'],'path_name':paths.get(rule['path_id'],{}).get('name',rule['path_id']),'kind':rule['kind'],'enabled':rule['enabled'],'requested_range_m':[rule['start_kp_m'],rule['end_kp_m']],'resolved_range_m':None,'status':'disabled','violations':_RetainedList(budget),'diagnostics':_RetainedList(budget),'coverage':{}}
        results.append(row)
        if not rule['enabled']: continue
        if rule['path_id'] not in paths:
            row['status']='reference_error'; row['diagnostics'].append(_diag('PATH_REFERENCE_MISSING','Declared path is absent; reference is retained'))
            continue
        path_id=rule['path_id']
        if path_id not in contexts:
            project=_materialize(workspace,paths[path_id]); projects[path_id]=project
            analysis=analyses.get(path_id)
            from .core import route_signature
            if not isinstance(analysis,dict) or analysis.get('route_signature')!=route_signature(project):
                raise ValueError('analyses must contain the current analysis for every referenced path')
            route=Route(project,budget); profile=_Profile(project,route,budget)
            objects=_objects(project,analysis,route,profile)
            for rows in objects.values():
                for obj in rows: obj['longitude'],obj['latitude']=route.at(obj['kp_m'])
            contexts[path_id]=(project,analysis,route,profile,objects)
        project,analysis,route,profile,objects=contexts[path_id]
        end=route.total if rule['end_kp_m'] is None else rule['end_kp_m']
        start=rule['start_kp_m']; lo,hi=max(0.,start),min(end,route.total)
        row['resolved_range_m']=[start,end]
        row['coverage']={'route_length_m':route.total,'declared_range_complete':start<end and start<=route.total and end<=route.total+1e-7,'checked_range_m':[lo,max(lo,hi)],'continuous_bed_verified':False}
        if hi<=lo:
            row['status']='unknown'; row['diagnostics'].append(_diag('RULE_RANGE_EMPTY','No positive route interval is available; declaration is retained'))
            continue
        if not row['coverage']['declared_range_complete']:
            row['diagnostics'].append(_diag('RULE_RANGE_UNCOVERED','Declared KP interval exceeds the current route; it is not silently clamped'))
        selected=[]
        if 'selectors' in rule:
            selected,diagnostics=_selected(workspace,rule['selectors'],budget); row['diagnostics'].extend(diagnostics)
        if rule['kind']=='crossing': _crossing_rule(rule,row,selected,project,analysis,route,profile,objects,budget)
        elif rule['kind']=='proximity': _proximity_rule(rule,row,selected,route,profile,objects,budget,slope_jobs)
        else:
            from .slope_rules import check_slope_rules
            transformed={k:v for k,v in rule.items() if k not in {'path_id','slope_basis','kind'}}; transformed['kind']=rule['slope_basis']
            from .slope_rules import SlopeRuleError
            try:
                output=check_slope_rules(project,{'rules':[transformed],'max_rules':1,'max_work_units':min(20000000,max(1,config['max_work_units']-budget.work)),'max_output_bytes':min(32*1024**2,config['max_output_bytes'])},analysis=analysis)
            except SlopeRuleError as error:
                if error.code in {'SLOPE_RULE_LIMIT','SLOPE_RULE_WORK_BUDGET','SLOPE_RULE_OUTPUT_BUDGET'}:
                    _evaluation_error('AUTOMATIC_RULE_'+error.code,str(error))
                raise
            budget.charge(output['budget']['estimated_work_units'])
            slope=output['results'][0]; row['components']=slope['components']; row['diagnostics'].extend(slope['diagnostics'])
            for violation in slope['violations']:
                budget.add('events'); kp=violation.get('kp_m',violation.get('start_kp_m'))
                row['violations'].append({**violation,'event':'sampled_slope','kp_m':kp,'location':_location(route,profile,path_id,kp),'message':'Sampled route slope exceeds the declared absolute threshold'})
            row['coverage']['terrain_sampling_only']=True
            row['coverage']['slope_components_complete']=all(c['coverage']['complete'] for c in slope['components'].values())
        # Ref errors do not suppress independent valid selections/evidence.
    if slope_jobs: _slope_neighborhoods(slope_jobs,projects,budget)
    errors=[]; warnings=[]
    for row in results:
        if not row['enabled']: continue
        codes=[d['code'] for d in row['diagnostics']]
        references=any('REFERENCE_' in c for c in codes)
        if references: row['status']='reference_error'
        elif row['violations']: row['status']='violations'
        elif row['diagnostics']: row['status']='incomplete' if any(c in {'RULE_RANGE_UNCOVERED','PROXIMITY_DEPTH_UNAVAILABLE','TERRAIN_SLOPE_WINDOW_INCOMPLETE','INLINE_COVERAGE_INCOMPLETE','SIDE_COVERAGE_INCOMPLETE'} for c in codes) else 'unknown'
        elif row['coverage'].get('slope_components_complete') is False: row['status']='incomplete'
        elif row['coverage'].get('terrain_sampling_only'): row['status']='sampled_pass'
        else: row['status']='clear'
        for index,v in enumerate(row['violations']):
            errors.append({'id':f"{row['rule_id']}:v:{index}",'rule_id':row['rule_id'],'kind':row['kind'],'path_id':row['path_id'],'path_name':row['path_name'],'kp_m':v.get('kp_m'),'message':v['message'],'status':'violation','location':v.get('location'),'violation':v})
        for index,d in enumerate(row['diagnostics']):
            errors.append({'id':f"{row['rule_id']}:d:{index}",'rule_id':row['rule_id'],'kind':row['kind'],'path_id':row['path_id'],'path_name':row['path_name'],'kp_m':d.get('location',{}).get('kp_m') if d.get('location') else None,'message':d['message'],'status':'reference_error' if 'REFERENCE_' in d['code'] else 'unknown','location':d.get('location'),'diagnostic':d})
            warnings.append({**d,'rule_id':row['rule_id'],'severity':'warning'})
    states=[r['status'] for r in results if r['enabled']]
    summary_status='not_configured' if not rules else 'disabled' if not states else next((s for s in ('reference_error','violations','unknown','incomplete','sampled_pass') if s in states),'clear')
    geometry_paths=[]
    for path in workspace['paths']:
        route=path['project']['route']
        item={'id':path['id'],'curve':route.get('curve','rhumb'),
              'points':[[p['longitude'],p['latitude']] for p in route['points']]}
        if any(o.get('geometry') is not None for o in route.get('legs',[])):
            item['leg_geometry']=[o.get('geometry') for o in route.get('legs',[])]
        geometry_paths.append(item)
    output={'model':MODEL,'schema_version':1,'validation_status':'research','rules':rules,'results':results,'errors':errors,
            'summary':{'status':summary_status,'enabled_rules':len(states),'disabled_rules':len(rules)-len(states),'violating_rules':sum(bool(r['violations']) for r in results),'violation_count':sum(len(r['violations']) for r in results),'reference_error_rules':states.count('reference_error'),'unknown_rules':states.count('unknown'),'incomplete_rules':states.count('incomplete'),'continuous_bed_verified':False},
            'metadata':{'rules_signature':_hash(rules),'geometry_signature':_hash({'paths':geometry_paths,'layers':[{'id':layer['id'],'features':[{'id':f.get('id'),'geometry':f.get('geometry') if f.get('type')=='Feature' else f} for f in _features(layer)]} for layer in workspace['layers']]}),'input_signature':_hash({'rules':rules,'paths':[{k:p[k] for k in ('id','project')} for p in workspace['paths']],'cable_types':workspace['cable_types'],'terrain_sources':workspace.get('terrain_sources',[]),'layers':[{k:v for k,v in layer.items() if k not in {'visible','opacity','display_order'}} for layer in workspace['layers']]}),'geometry_model':'native geographic-linear GIS primitives; actual WGS84 route curves; adaptive full-segment overlay and metric distance bounds','geometry_tolerance_m':config['geometry_tolerance_m']},
            'budget':{'work_units':budget.work,'features':budget.features,'vertices':budget.vertices,'pairs':budget.pairs,'events':budget.events,'terrain_query_count':budget.terrain_query_count,'work_basis':'logical rule parsing, geometry, interval bounds and checker terrain costs; preceding admission costs excluded; not FLOPs, wall-clock or RSS guarantee','admission_exclusions':['workspace_json_size_checks_and_canonical_serialization','workspace_shared_source_normalization','core_route_densification_and_profile_material_manufacture_currency_relationship_validation','pure_source_gis_admission_before_checker'],**{k:v for k,v in config.items() if k.startswith('max_')}},'warnings':warnings,
            'assumptions':['Predicates are explicit error triggers; all/any use three-valued missing-data logic.','Hidden layers participate. Native typed feature IDs and explicit source indexes remain distinct.','GIS source edges are geographic-linear, including long-way date-line edges; whole geometries may be translated by 360 degrees, never individual vertices.','Full primitives, polygon holes and recursive GeometryCollections are checked. Distance bounds span complete continuous intervals.','Geographic overlay follows adaptively rendered real route curves. Contacts near tolerance, polar scope, ambiguous tangents and missing data are unresolved rather than silently clear.','Two-dimensional terrain triangles and saved inline/side slopes are sampled research screens, not continuous terrain certification or Makai/FME native compatibility.','No route, source library, manufacture, fixed constraint or saved revision is altered or persisted.']}
    encoded=_json(output)
    if len(encoded)>config['max_output_bytes']: _evaluation_error('AUTOMATIC_RULE_OUTPUT_LIMIT','Complete result exceeds output budget; no truncation')
    output['budget']['output_bytes']=len(encoded)
    while True:
        actual=len(_json(output))
        if actual==output['budget']['output_bytes']:break
        output['budget']['output_bytes']=actual
    if len(_json(output))>config['max_output_bytes']: _evaluation_error('AUTOMATIC_RULE_OUTPUT_LIMIT','Complete result exceeds output budget; no truncation')
    return output


def export_automatic_rules(workspace, config=None):
    config={} if config is None else config
    _object(config,{'rule_ids'},'automatic rule export config')
    rules=normalize_automatic_rules(workspace.get('automatic_rules',[]))
    if 'rule_ids' in config:
        ids=config['rule_ids']
        if not isinstance(ids,list) or len(ids)>512 or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids): raise ValueError('rule_ids must be unique string IDs')
        available={r['id'] for r in rules}
        if set(ids)-available: raise ValueError('export rule reference is absent')
        rules=[r for r in rules if r['id'] in ids]
    package={'schema':PACKAGE_SCHEMA,'schema_version':1,'rules':rules}
    return {'package':package,'text':_json(package).decode('utf-8')}


def import_automatic_rules(workspace, package, config=None):
    config={} if config is None else config
    _object(config,{'binding','path_id','mode','duplicate_ids'},'automatic rule import config')
    workspace,analyses=_workspace(workspace)
    if isinstance(package,str):
        if len(package.encode('utf-8'))>16*1024**2: raise ValueError('automatic rule package exceeds 16 MiB')
        try: package=json.loads(package)
        except (ValueError,TypeError) as error: raise ValueError('automatic rule package must contain valid JSON') from error
    if len(_json(package))>16*1024**2:raise ValueError('automatic rule package exceeds16 MiB')
    _object(package,{'schema','schema_version','rules'},'automatic rule package')
    if package.get('schema')!=PACKAGE_SCHEMA or type(package.get('schema_version')) is not int or package['schema_version']!=1: raise ValueError('unsupported automatic rule package schema')
    incoming=normalize_automatic_rules(package.get('rules'))
    binding=config.get('binding','active_path'); mode=config.get('mode','append'); duplicate=config.get('duplicate_ids','reject')
    if not isinstance(binding,str) or not isinstance(mode,str) or not isinstance(duplicate,str) or binding not in {'active_path','retain','explicit'} or mode not in {'append','replace'} or duplicate not in {'reject','rename'}: raise ValueError('invalid automatic rule import policy')
    if binding=='explicit': target=_text(config.get('path_id'),'explicit path_id')
    else:
        if 'path_id' in config: raise ValueError('path_id requires explicit binding')
        target=workspace.get('active_path_id') if binding=='active_path' else None
    output=[] if mode=='replace' else normalize_automatic_rules(workspace.get('automatic_rules',[]))
    seen={r['id'] for r in output}; renamed=[]; path_bindings=[]
    for original in incoming:
        row=deepcopy(original)
        if target is not None:
            row['path_id']=target
        path_bindings.append({'rule_id':row['id'],'from_path_id':original['path_id'],'to_path_id':row['path_id']})
        if row['id'] in seen:
            if duplicate=='reject': raise ValueError('duplicate imported automatic rule ID')
            old=row['id']; suffix=2
            while f'{old[:115]}-{suffix}' in seen: suffix+=1
            row['id']=f'{old[:115]}-{suffix}'; renamed.append({'from':old,'to':row['id']})
        seen.add(row['id']); output.append(row)
    output=normalize_automatic_rules(output)
    candidate=deepcopy(workspace); candidate['automatic_rules']=output
    checks=check_automatic_rules(candidate,{'max_rules':512},analyses=analyses)
    result={'workspace':candidate,'rules':output,'checks':checks,'report':{'operation':'import_automatic_rules','binding':binding,'mode':mode,'duplicate_ids':duplicate,'imported_count':len(incoming),'renamed_ids':renamed,'path_bindings':path_bindings,'source_schema':PACKAGE_SCHEMA},'warnings':checks['warnings']}
    if len(_json(result))>64*1024**2: _evaluation_error('AUTOMATIC_RULE_IMPORT_OUTPUT_LIMIT','Complete import candidate exceeds output budget')
    return result
