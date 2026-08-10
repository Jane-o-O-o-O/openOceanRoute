"""Auditable conversion of manufacturing stock and a ship plan to a lay window.

This prepares an explicitly analytical initial state, not a reconstruction of
an actual vessel/cable state. Manufacturing and route coordinates remain distinct.
"""
from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
import hashlib
import json
import math

import numpy as np
from pyproj import CRS, Transformer

from .core import analyze_project
from .geodesy import GEOD, interpolate
from .shipplan import build_ship_plan
from .simulation import G, _MaterialModel, _config, _environment, _integer, _num, catenary


def _digest(value):
    def canonical(item):
        if isinstance(item, float) and math.isfinite(item) and item.is_integer():
            return int(item)
        if isinstance(item, dict):
            return {key: canonical(v) for key, v in item.items()}
        if isinstance(item, list):
            return [canonical(v) for v in item]
        return item
    return hashlib.sha256(json.dumps(canonical(value), sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _at(plan, time):
    rows = plan["instructions"]
    i = min(len(rows)-1, max(0, bisect_right([r["time_s"] for r in rows], time)-1))
    row = rows[i]
    fraction = min(1., max(0., (time-row["time_s"])/row["duration_s"]))
    a, b = row["vessel_start"], row["vessel_end"]
    az, _, distance = GEOD.inv(*a, *b)
    lon, lat, _ = GEOD.fwd(*a, az, distance*fraction)
    return {"longitude": float(lon), "latitude": float(lat),
            "material_m": row["cable_start_m"] + fraction*(row["cable_end_m"]-row["cable_start_m"]),
            "route_kp_m": row["kp_start_m"] + fraction*(row["kp_end_m"]-row["kp_start_m"]),
            "row": row, "heading_deg": row["heading_deg"]}


def _first_time_for_material(plan, material):
    for row in plan["instructions"]:
        if row["cable_end_m"] >= material-1e-10 and row["cable_end_m"] > row["cable_start_m"]:
            return row["time_s"] + (material-row["cable_start_m"])/row["payout_m_s"]
    raise ValueError("planned manufacturing stock is shorter than the initial suspended cable")


def read_plan_mapping(document, *, simulation=None, physical=None):
    """Validate the saved bridge and its actual material/control state."""
    if not isinstance(document, dict) or document.get("schema") != "oceanroute.plan-voyage-mapping" or type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise ValueError("unsupported planning mapping schema")
    payload = {key: value for key, value in document.items() if key != "checksum_sha256"}
    if document.get("checksum_sha256") != _digest(payload):
        raise ValueError("planning mapping checksum mismatch")
    if len(json.dumps(document, ensure_ascii=False, allow_nan=False).encode()) > 4_000_000:
        raise ValueError("planning mapping exceeds 4 MB")
    source = _config(document.get("source_simulation"))
    if simulation is not None and source != simulation:
        raise ValueError("prepared planning mapping no longer matches simulation inputs; prepare again")
    rows = document.get("instructions")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 499:
        raise ValueError("planning mapping requires actual instruction intervals")
    previous_time = 0.
    previous_material = _num(document, "initial_manufacturing_top_m", 0, 0, 1e12)
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("planning mapping intervals must be objects")
        start = _num(row, "local_start_s", 0, 0, 1e9)
        end = _num(row, "local_end_s", 0, start, 1e9, strict=True)
        first = _num(row, "manufacturing_start_m", 0, 0, 1e12)
        last = _num(row, "manufacturing_end_m", 0, first, 1e12)
        if abs(start-previous_time) > 1e-7 or abs(first-previous_material) > 1e-7:
            raise ValueError("planning mapping time and manufacturing intervals must be contiguous")
        previous_time, previous_material = end, last
    if physical is not None:
        time = _num(physical, "time_s", 0, 0, previous_time+1e-8)
        settings = physical["config"]
        for key, value in source.items():
            if settings.get(key) != value:
                raise ValueError("saved physical state no longer matches planning " + key)
        expected = document["initial_manufacturing_top_m"]
        for row in rows:
            fraction = min(1., max(0., (time-row["local_start_s"])/(row["local_end_s"]-row["local_start_s"])))
            expected += fraction*(row["manufacturing_end_m"]-row["manufacturing_start_m"])
        actual = physical["state"]["node_material_m"][0]
        if abs(actual-expected) > max(1e-6, abs(expected)*1e-10):
            raise ValueError("actual deployed material no longer matches planning manufacturing clock")
    return deepcopy(document)


def prepare_plan_voyage(project: dict, config: dict) -> dict:
    """Prepare a geographic, material-conserving analytical research window.

    Positive-length bodies need a separate rod/carrier model and are rejected.
    Zero-length bodies retain their declared stock coordinate and explicit loads.
    Changing depth is not substituted with a flat bed without telling the caller.
    """
    c = _config(config)
    allowed = {"plan", "simulation", "voyage", "start_time_s", "duration_s", "max_track_span_m", "projection_tolerance_m"}
    if set(c)-allowed:
        raise ValueError("unknown plan-voyage configuration: " + ", ".join(sorted(set(c)-allowed)))
    analysis = analyze_project(project)
    simulation = deepcopy(_config(c.get("simulation", {})))
    optional_physics = {"nodes", "bottom_tension_n", "water_density_kg_m3", "drag_coefficient", "current_x_m_s", "current_y_m_s",
                        "current_profile", "added_mass_coefficient", "damping_ratio", "seabed_friction", "heave_amplitude_m",
                        "heave_period_s", "max_tension_n", "min_bend_radius_m", "internal_dt_s", "dt_s", "solver_iterations",
                        "vessel_motion_series", "wave_kinematics"}
    forbidden = {"resume_state", "material_segments", "inline_bodies", "ship_plan", "ship_plan_horizon_s",
                 "initial_suspended_material_m", "seabed_profile", "depth_m", "heading_deg", "payout_m_s", "ship_speed_m_s",
                 "wet_weight_n_m", "diameter_m", "ea_n", "ei_n_m2", "mass_kg_m", "duration_s", "save_checkpoints", "checkpoint_times_s"}
    if set(simulation) & forbidden:
        raise ValueError("planning owns material/geometry/controls: " + ", ".join(sorted(set(simulation) & forbidden)))
    if set(simulation)-optional_physics:
        raise ValueError("unsupported planning simulation options: " + ", ".join(sorted(set(simulation)-optional_physics)))
    plan_config = deepcopy(_config(c.get("plan", {})))
    if any(key in plan_config for key in ("wet_weight_n_m", "diameter_m")):
        raise ValueError("set measured physical properties in the project cable library, not a uniform plan override")
    for key in ("bottom_tension_n", "current_x_m_s", "current_y_m_s", "drag_coefficient", "water_density_kg_m3"):
        if key in simulation:
            if key in plan_config and plan_config[key] != simulation[key]:
                raise ValueError("plan and dynamics must use the same " + key)
            plan_config[key] = simulation[key]
    plan = build_ship_plan(project, plan_config)
    if plan["summary"]["operator_extra_cable_m"] > 1e-8:
        raise ValueError("operator payout beyond manufacturing stock needs a revised assembly before dynamic mapping")
    profile = analysis["profile"]
    if len(profile) < 2 or any(row["depth_m"] is None for row in profile):
        raise ValueError("planning voyage requires complete, valid positive-depth profile coverage")
    depths = np.array([row["depth_m"] for row in profile])
    if np.ptp(depths) > 1e-5 or np.min(depths) < .001:
        raise ValueError("automatic geographic voyage mapping currently requires a flat positive-depth bed; changing bathymetry is not silently flattened")
    depth = float(depths[0])
    types = {str(row["id"]): row for row in project.get("cable_types", [])}
    rho = _num(simulation, "water_density_kg_m3", 1025., 1, 2000)
    cd = _num(simulation, "drag_coefficient", 1.2, 0, 10)
    materials, correspondence = [], []
    for entry in analysis["sld"]:
        if entry["end_m"] <= entry["start_m"]:
            continue
        if entry["kind"] == "body":
            raise ValueError("positive-length body " + str(entry["id"]) + " needs an explicit rod/carrier model; cable mass is not inserted into its stock interval")
        typ = types.get(str(entry["cable_type_id"]))
        if typ is None or any(k not in typ for k in ("wet_weight_n_m", "diameter_m", "ea_n")):
            raise ValueError("measured wet_weight_n_m, diameter_m and ea_n are required for cable " + str(entry["cable_type_id"]))
        weight = _num(typ, "wet_weight_n_m", 4, 1e-6, 20000)
        diameter = _num(typ, "diameter_m", .02, 1e-4, 2)
        row = {"id": str(entry["cable_type_id"]), "start_m": float(entry["start_m"]), "end_m": float(entry["end_m"]),
               "wet_weight_n_m": weight, "diameter_m": diameter,
               "ea_n": _num(typ, "ea_n", 1e8, 100, 1e12), "ei_n_m2": _num(typ, "ei_n_m2", 0, 0, 1e10),
               "mass_kg_m": _num(typ, "mass_kg_m", weight/G + rho*math.pi*diameter**2/4, 0, 50000, strict=True),
               "drag_coefficient": _num(typ, "drag_coefficient", cd, 0, 10)}
        if row["mass_kg_m"]*G <= weight:
            raise ValueError("cable dry mass must exceed wet weight/gravity")
        correspondence.append({**deepcopy(entry), "mass_source": "declared" if "mass_kg_m" in typ else "wet-weight-plus-circular-displacement"})
        keys = set(row)-{"id", "start_m", "end_m"}
        if materials and materials[-1]["id"] == row["id"] and all(materials[-1][k] == row[k] for k in keys):
            materials[-1]["end_m"] = row["end_m"]
        else:
            materials.append(row)
    if not materials or len(materials) > 256:
        raise ValueError("mapped assembly needs 1 to 256 contiguous material intervals")
    first = materials[0]
    if "start_time_s" in c:
        requested_start = _num(c, "start_time_s", 0, 0, plan["summary"]["duration_s"])
        station = _at(plan, requested_start)["material_m"]
        first = next((r for r in materials if r["start_m"] < station+1e-9 and r["end_m"] >= station), first)
    bottom = _num(plan_config, "bottom_tension_n", 1000, 0, 1e9)
    nodes = _integer(simulation, "nodes", 16, 6, 100)
    initial = catenary({"depth_m": depth, "wet_weight_n_m": first["wet_weight_n_m"],
                        "bottom_tension_n": bottom, "nodes": nodes, "heading_deg": 90})
    positions = np.array(initial["nodes"])
    tensions = np.array(initial["node_tension_n"])
    rest = np.linalg.norm(np.diff(positions, axis=0), axis=1)/(1+(tensions[:-1]+tensions[1:])/(2*first["ea_n"]))
    initial_length = float(rest.sum())
    earliest = _first_time_for_material(plan, initial_length)
    start = _num(c, "start_time_s", earliest, earliest, plan["summary"]["duration_s"])
    remaining = plan["summary"]["duration_s"]-start
    duration = _num(c, "duration_s", remaining, 0, remaining, strict=True)
    at_start, at_end = _at(plan, start), _at(plan, start+duration)
    origin_material = at_start["material_m"]-initial_length
    if origin_material < -1e-7:
        raise ValueError("initial manufacturing prefix is not yet available")
    initial_rows = [r for r in materials if r["start_m"] < at_start["material_m"]-1e-8 and r["end_m"] > origin_material+1e-8]
    physical_keys = set(first)-{"id", "start_m", "end_m"}
    if not initial_rows or any(any(row[k] != first[k] for k in physical_keys) for row in initial_rows):
        raise ValueError("analytical initial suspended interval crosses different material properties; supply an independently reconstructed initial state")
    bodies = []
    for body in analysis["bodies"]:
        if body["length_m"] > 0:
            raise ValueError("finite body material mapping requires a rod/carrier model")
        if body["start_m"] < max(0., origin_material)-1e-8:
            continue
        required = {"mass_kg", "wet_weight_n", "drag_area_m2"}
        if not required <= body.keys():
            raise ValueError("body " + body["id"] + " requires measured mass_kg, wet_weight_n and drag_area_m2")
        bodies.append({"id": body["id"], "material_m": body["start_m"], "length_m": 0.,
                       **{k: body[k] for k in required}, "drag_coefficient": body.get("drag_coefficient", cd)})
    local = CRS.from_proj4(f"+proj=aeqd +lat_0={at_start['latitude']} +lon_0={at_start['longitude']} +datum=WGS84 +units=m")
    forward = Transformer.from_crs("EPSG:4326", local, always_xy=True)
    maximum_span = _num(c, "max_track_span_m", 100000, 1, 1e6)
    tolerance = _num(c, "projection_tolerance_m", .1, 1e-6, 1000)
    controls, mapping = [], []
    largest_projection_error = 0.
    for instruction in plan["instructions"]:
        a = max(start, instruction["time_s"])
        b = min(start+duration, instruction["end_time_s"])
        if b-a <= 1e-10:
            continue
        aa, bb = _at(plan, a), _at(plan, b)
        ax, ay = forward.transform(aa["longitude"], aa["latitude"])
        bx, by = forward.transform(bb["longitude"], bb["latitude"])
        if max(math.hypot(ax, ay), math.hypot(bx, by)) > maximum_span:
            raise ValueError("mapped vessel track exceeds its declared local projection span")
        distance = math.hypot(bx-ax, by-ay)
        heading = math.degrees(math.atan2(bx-ax, by-ay)) % 360 if distance > 1e-9 else instruction["heading_deg"]
        middle = _at(plan, (a+b)/2)
        mx, my = forward.transform(middle["longitude"], middle["latitude"])
        error = math.hypot(mx-(ax+bx)/2, my-(ay+by)/2)
        largest_projection_error = max(largest_projection_error, error)
        if error > tolerance:
            raise ValueError("projected vessel chord error exceeds tolerance; refine ship-plan sampling")
        controls.append({"time_s": a-start, "speed_m_s": distance/(b-a), "heading_deg": heading,
                         "payout_m_s": instruction["payout_m_s"]})
        mapping.append({"kind": instruction["kind"], "local_start_s": a-start, "local_end_s": b-start,
                        "plan_start_s": a, "plan_end_s": b, "route_start_kp_m": aa["route_kp_m"], "route_end_kp_m": bb["route_kp_m"],
                        "manufacturing_start_m": aa["material_m"], "manufacturing_end_m": bb["material_m"],
                        "vessel_start_xy_m": [float(ax), float(ay)], "vessel_end_xy_m": [float(bx), float(by)],
                        "projection_midpoint_error_m": error})
    if not controls or len(controls) >= 500:
        raise ValueError("mapped dynamic window needs 1 to 499 instructions; select a shorter window or coarser plan")
    controls.append({"time_s": duration, "speed_m_s": 0., "heading_deg": controls[-1]["heading_deg"], "payout_m_s": 0.})
    paid = sum(r["payout_m_s"]*(mapping[i]["local_end_s"]-mapping[i]["local_start_s"]) for i, r in enumerate(controls[:-1]))
    balance = abs(paid-(at_end["material_m"]-at_start["material_m"]))
    if balance > max(1e-7, paid*1e-10):
        raise ValueError("mapped instructions do not conserve manufacturing payout")
    simulation.update({k: first[k] for k in physical_keys})
    simulation.update({"nodes": nodes, "depth_m": depth, "bottom_tension_n": bottom,
                       "initial_suspended_material_m": max(0., origin_material), "material_segments": materials,
                       "inline_bodies": bodies, "ship_plan": controls, "ship_plan_horizon_s": duration,
                       "ship_speed_m_s": controls[0]["speed_m_s"], "payout_m_s": controls[0]["payout_m_s"], "heading_deg": controls[0]["heading_deg"]})
    simulation.setdefault("internal_dt_s", .05)
    simulation.setdefault("dt_s", 1.)
    simulation.setdefault("solver_iterations", 24)
    used_types = {r["id"] for r in materials}
    tension_limits = [r["max_tension_n"] for k, r in types.items() if k in used_types and "max_tension_n" in r]
    radius_limits = [r["min_bend_radius_m"] for k, r in types.items() if k in used_types and "min_bend_radius_m" in r]
    if tension_limits:
        simulation["max_tension_n"] = min(_num(simulation, "max_tension_n", 1e12, 0, 1e12, strict=True), min(tension_limits))
    if radius_limits:
        simulation["min_bend_radius_m"] = max(_num(simulation, "min_bend_radius_m", 0, 0, 10000), max(radius_limits))
    material_model = _MaterialModel(simulation, _environment(simulation), first["ea_n"], first["ei_n_m2"], first["mass_kg_m"], simulation.get("added_mass_coefficient", 1))
    material_model.validate_coverage(at_end["material_m"])
    voyage = deepcopy(_config(c.get("voyage", {})))
    if set(voyage) & {"simulation", "resume_state", "duration_s"}:
        raise ValueError("prepare-plan owns voyage duration and initial simulation state")
    voyage.update({"duration_s": duration, "simulation": simulation})
    context = {"schema": "oceanroute.plan-voyage-mapping", "schema_version": 1,
               "route_signature": analysis["route_signature"], "project_sha256": _digest(project),
               "source_plan_start_s": start, "source_plan_end_s": start+duration,
               "manufacturing_origin_m": max(0., origin_material), "initial_manufacturing_top_m": at_start["material_m"],
               "initial_natural_length_m": initial_length, "final_manufacturing_top_m": at_end["material_m"],
               "local_crs": local.to_string(), "origin_wgs84": [at_start["longitude"], at_start["latitude"]],
               "initial_state": "analytical-homogeneous-catenary-zero-velocity", "instructions": mapping,
               "source_simulation": deepcopy(simulation),
               "manufacturing_intervals": correspondence, "projection_midpoint_error_m": largest_projection_error,
               "payout_balance_residual_m": balance}
    route_kps = [r["kp_m"] for r in analysis["rpl"]]
    index = max(0, min(len(route_kps)-2, bisect_right(route_kps, at_start["route_kp_m"])-1))
    point_a, point_b = project["route"]["points"][index:index+2]
    leg_length = route_kps[index+1]-route_kps[index]
    fraction = (at_start["route_kp_m"]-route_kps[index])/leg_length if leg_length > 1e-9 else 0.
    target_lon, target_lat = interpolate(point_a["longitude"], point_a["latitude"], point_b["longitude"], point_b["latitude"],
                                         fraction, project["route"].get("curve", "rhumb"))
    target_xy = list(map(float, forward.transform(target_lon, target_lat)))
    angle = math.radians(controls[0]["heading_deg"])
    layback = initial["summary"]["layback_m"]
    anchor_xy = [-layback*math.sin(angle), -layback*math.cos(angle)]
    context["initial_anchor_xy_m"] = anchor_xy
    context["planned_start_touchdown_xy_m"] = target_xy
    context["initial_target_touchdown_residual_m"] = math.dist(anchor_xy, target_xy)
    context["checksum_sha256"] = _digest(context)
    read_plan_mapping(context, simulation=simulation)
    voyage["plan_mapping"] = deepcopy(context)
    return {"model": "explicit-manufacturing-geographic-plan-voyage-preparation-v1", "validation_status": "research",
            "config": voyage, "mapping": context, "plan": plan,
            "warnings": plan["warnings"] + [{"code": "ANALYTICAL_START_NOT_SURVEY_RECONSTRUCTION", "severity": "warning",
                "message": "Initial cable is an analytical catenary with zero velocity, not a reconstructed installation state. Earlier plan payout occupies manufacturing stock and is not simulated again; the anchor is not forced to the planned geographic route."}],
            "assumptions": ["This preparation requires a known flat positive-depth bed and a homogeneous initial suspended interval.",
                            "Local vessel controls integrate projected instruction endpoints; route KP, manufacturing station, initial inventory and newly paid cable remain distinct.",
                            "Positive-length bodies require a rod/carrier model; point body loads are mapped only from declared measured physical data.",
                            "No past laying history, initial velocity, anchor survey or original-engine accuracy is inferred."]}
