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
from .plan_equilibrium_frame import PlanBathymetryFrame, initial_equilibrium_length
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
    if not isinstance(document, dict) or document.get("schema") != "oceanroute.plan-voyage-mapping" or type(document.get("schema_version")) is not int or document["schema_version"] not in (1, 2):
        raise ValueError("unsupported planning mapping schema")
    payload = {key: value for key, value in document.items() if key != "checksum_sha256"}
    if document.get("checksum_sha256") != _digest(payload):
        raise ValueError("planning mapping checksum mismatch")
    if len(json.dumps(document, ensure_ascii=False, allow_nan=False).encode()) > 4_000_000:
        raise ValueError("planning mapping exceeds 4 MB")
    source = _config(document.get("source_simulation"))
    if simulation is not None and source != simulation:
        raise ValueError("prepared planning mapping no longer matches simulation inputs; prepare again")
    if document["schema_version"] == 2:
        from .plan_equilibrium_frame import _digest as frame_digest
        from .initial_equilibrium import validate_initialization_provenance
        from .geodesy import finite_number
        frame = document.get("terrain_frame")
        request = source.get("initial_equilibrium")
        if not isinstance(frame, dict) or not isinstance(request, dict) or "seabed_grid" not in source:
            raise ValueError("equilibrium planning mapping requires its actual terrain frame and raw initial request")
        if frame.get("rebased_grid_sha256") != frame_digest(source["seabed_grid"]):
            raise ValueError("equilibrium planning mapping bed frame no longer matches its actual grid")
        if document.get("initial_state") != "verified-discrete-equilibrium-zero-velocity":
            raise ValueError("equilibrium planning mapping has an unsupported initial-state method")
        origin = _num(document, "manufacturing_origin_m", 0, 0, 1e12)
        top = _num(document, "initial_manufacturing_top_m", 0, 0, 1e12)
        length = _num(document, "initial_natural_length_m", 0, .001, 1e6)
        if _num(source, "initial_suspended_material_m", 0, 0, 1e12) != origin:
            raise ValueError("equilibrium planning material origin does not match its actual source")
        anchor_geo = frame.get("anchor_wgs84")
        origin_geo = document.get("origin_wgs84")
        anchor_raw = request.get("anchor_position_m")
        vessel_raw = request.get("vessel_position_m")
        if any(not isinstance(v, list) or len(v) != count for v, count in ((anchor_geo,2),(origin_geo,2),(anchor_raw,3),(vessel_raw,3))):
            raise ValueError("equilibrium planning requires complete geographic and local boundaries")
        options = {"anchor": {"longitude": anchor_geo[0], "latitude": anchor_geo[1], "z_model_m": anchor_raw[2]},
                   "vessel_z_m": vessel_raw[2], **{k: deepcopy(request[k]) for k in ("natural_length_m", "rest_lengths_m", "solver") if k in request}}
        nodes = _integer(source, "nodes", 16, 6, 80)
        material_length = initial_equilibrium_length(options, nodes)
        if abs(material_length-length) > max(1e-8, abs(material_length)*1e-12):
            raise ValueError("equilibrium planning initial inventory does not match declared natural length")
        if abs(top-origin-material_length) > max(1e-7, abs(material_length)*1e-10):
            raise ValueError("equilibrium planning manufacturing origin/initial inventory/top station do not balance")
        original = frame.get("original_grid")
        if not isinstance(original, dict) or frame.get("original_grid_sha256") != frame_digest(original):
            raise ValueError("equilibrium planning original bed provenance mismatch")
        actual_frame = PlanBathymetryFrame(original, {"longitude": origin_geo[0], "latitude": origin_geo[1]}, options, nodes)
        expected_frame = actual_frame.metadata()
        for key in ("method", "horizontal_crs", "original_origin_projected_m", "origin_projected_m", "translation_from_original_local_m", "vertical_datum", "vertical_translation_m"):
            if frame.get(key) != expected_frame[key]:
                raise ValueError("equilibrium planning terrain frame mismatch: "+key)
        if frame_digest(actual_frame.grid) != frame_digest(source["seabed_grid"]):
            raise ValueError("equilibrium planning grid was not translated from its actual original")
        if document.get("local_crs") != CRS.from_user_input(actual_frame.crs).to_string():
            raise ValueError("equilibrium planning local CRS no longer matches bed frame")
        def point(value, name):
            if not isinstance(value, list) or len(value) != 3:
                raise ValueError(name+" requires three finite coordinates")
            return np.array([finite_number(v,name,minimum=-1e7,maximum=1e7) for v in value])
        if not np.allclose(point(vessel_raw,"vessel"), actual_frame.request["vessel_position_m"], rtol=0, atol=1e-9) or not np.allclose(point(anchor_raw,"anchor"), actual_frame.request["anchor_position_m"], rtol=0, atol=1e-9):
            raise ValueError("equilibrium planning geographic/local endpoint mismatch")
        saved_anchor = document.get("initial_anchor_xy_m")
        if not isinstance(saved_anchor, list) or len(saved_anchor) != 2 or any(finite_number(v,"initial_anchor_xy_m") != anchor_raw[i] for i,v in enumerate(saved_anchor)):
            raise ValueError("equilibrium planning initial anchor is not the actual fixed boundary")
        target = document.get("planned_start_touchdown_xy_m")
        if not isinstance(target, list) or len(target) != 2:
            raise ValueError("equilibrium planning requires its planned target footprint")
        target = [finite_number(v,"planned_start_touchdown_xy_m",minimum=-1e7,maximum=1e7) for v in target]
        target_geo = document.get("planned_start_touchdown_wgs84")
        if not isinstance(target_geo,list) or len(target_geo)!=2 or not np.allclose(target,actual_frame.project(*target_geo),rtol=0,atol=1e-8):
            raise ValueError("equilibrium planning target geographic/local footprint mismatch")
        residual = _num(document,"initial_target_touchdown_residual_m",0,0,1e8)
        if not math.isclose(residual, math.dist(saved_anchor,target),rel_tol=1e-12,abs_tol=1e-8):
            raise ValueError("equilibrium planning target residual disagrees with actual fixed anchor and planned footprint")
        preparation = document.get("initial_equilibrium_preparation")
        if not isinstance(preparation, dict) or not isinstance(preparation.get("provenance"), dict):
            raise ValueError("equilibrium planning requires its actual preparation proof")
        proof = preparation["provenance"]
        validate_initialization_provenance(proof, source)
        if preparation.get("solver") != proof["solver"] or preparation.get("estimated_work_units") != proof["solver"]["estimated_work_units"]:
            raise ValueError("equilibrium planning preparation diagnostics disagree with verified proof")
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
    if document["schema_version"] == 2:
        plan_start = _num(document,"source_plan_start_s",0,0,1e9)
        plan_end = _num(document,"source_plan_end_s",0,plan_start,1e9,strict=True)
        final_top = _num(document,"final_manufacturing_top_m",0,0,1e12)
        if abs(plan_end-plan_start-previous_time)>1e-7 or abs(final_top-previous_material)>1e-7 or abs(_num(source,"ship_plan_horizon_s",0,0,1e9)-previous_time)>1e-7:
            raise ValueError("equilibrium planning final time/manufacturing station disagree with actual window")
        controls = source.get("ship_plan")
        if not isinstance(controls,list) or len(controls)!=len(rows)+1 or any(not isinstance(r,dict) for r in controls):
            raise ValueError("equilibrium planning requires its actual instruction controls")
        previous_xy = np.array([0.,0.])
        actual_paid = 0.
        def xy(value):
            if not isinstance(value,list) or len(value)!=2:
                raise ValueError("equilibrium planning vessel XY requires two finite metres")
            return np.array([finite_number(v,"vessel_xy_m",minimum=-1e7,maximum=1e7) for v in value])
        for row, control in zip(rows,controls):
            a,b = row["local_start_s"],row["local_end_s"]
            if abs(_num(control,"time_s",0,0,1e9)-a)>1e-7 or abs(_num(row,"plan_start_s",0,0,1e9)-plan_start-a)>1e-7 or abs(_num(row,"plan_end_s",0,0,1e9)-plan_start-b)>1e-7:
                raise ValueError("equilibrium planning instruction times disagree with actual controls")
            speed = _num(control,"speed_m_s",0,0,50)
            heading = math.radians(_num(control,"heading_deg",0,-1e6,1e6))
            expected_xy = previous_xy+(b-a)*speed*np.array([math.sin(heading),math.cos(heading)])
            if not np.allclose(xy(row.get("vessel_start_xy_m")),previous_xy,rtol=0,atol=1e-7) or not np.allclose(xy(row.get("vessel_end_xy_m")),expected_xy,rtol=0,atol=1e-7):
                raise ValueError("equilibrium planning vessel geometry disagrees with actual controls")
            paid = (b-a)*_num(control,"payout_m_s",0,0,100)
            if abs(paid-(row["manufacturing_end_m"]-row["manufacturing_start_m"]))>max(1e-7,paid*1e-10):
                raise ValueError("equilibrium planning instruction inventory disagrees with actual payout")
            actual_paid += paid
            previous_xy = expected_xy
        stop = controls[-1]
        if abs(_num(stop,"time_s",0,0,1e9)-previous_time)>1e-7 or _num(stop,"speed_m_s",0,0,50)!=0 or _num(stop,"payout_m_s",0,0,100)!=0:
            raise ValueError("equilibrium planning controls lack the actual terminal stop")
        actual_balance = abs(actual_paid-(final_top-document["initial_manufacturing_top_m"]))
        if not math.isclose(_num(document,"payout_balance_residual_m",0,0,1e7),actual_balance,rel_tol=1e-8,abs_tol=1e-8):
            raise ValueError("equilibrium planning payout balance diagnostics disagree with actual controls")
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
    allowed = {"plan", "simulation", "voyage", "start_time_s", "duration_s", "max_track_span_m", "projection_tolerance_m", "seabed_grid", "equilibrium_start"}
    if set(c)-allowed:
        raise ValueError("unknown plan-voyage configuration: " + ", ".join(sorted(set(c)-allowed)))
    equilibrium_mode = "equilibrium_start" in c or "seabed_grid" in c
    if equilibrium_mode and not {"seabed_grid", "equilibrium_start"} <= c.keys():
        raise ValueError("equilibrium planning requires both seabed_grid and equilibrium_start")
    analysis = analyze_project(project)
    simulation = deepcopy(_config(c.get("simulation", {})))
    optional_physics = {"nodes", "bottom_tension_n", "water_density_kg_m3", "drag_coefficient", "current_x_m_s", "current_y_m_s",
                        "current_profile", "added_mass_coefficient", "damping_ratio", "seabed_friction", "heave_amplitude_m",
                        "heave_period_s", "max_tension_n", "min_bend_radius_m", "internal_dt_s", "dt_s", "solver_iterations",
                        "vessel_motion_series", "wave_kinematics", "max_work_units"}
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
    if np.min(depths) < .001 or (not equilibrium_mode and np.ptp(depths) > 1e-5):
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
    nodes = _integer(simulation, "nodes", 16, 6, 80 if equilibrium_mode else 100)
    if equilibrium_mode:
        initial_length = initial_equilibrium_length(c["equilibrium_start"], nodes)
        initial = None
    else:
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
    initial_keys = {"wet_weight_n_m", "ea_n", "ei_n_m2"} if equilibrium_mode else physical_keys
    if not initial_rows or any(any(row[k] != first[k] for k in initial_keys) for row in initial_rows):
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
    frame = PlanBathymetryFrame(c["seabed_grid"], at_start, c["equilibrium_start"], nodes) if equilibrium_mode else None
    local = CRS.from_user_input(frame.crs) if frame is not None else CRS.from_proj4(f"+proj=aeqd +lat_0={at_start['latitude']} +lon_0={at_start['longitude']} +datum=WGS84 +units=m")
    forward = Transformer.from_crs("EPSG:4326", local, always_xy=True)
    project_position = frame.project if frame is not None else forward.transform
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
        ax, ay = project_position(aa["longitude"], aa["latitude"])
        bx, by = project_position(bb["longitude"], bb["latitude"])
        if max(math.hypot(ax, ay), math.hypot(bx, by)) > maximum_span:
            raise ValueError("mapped vessel track exceeds its declared local projection span")
        distance = math.hypot(bx-ax, by-ay)
        heading = math.degrees(math.atan2(bx-ax, by-ay)) % 360 if distance > 1e-9 else instruction["heading_deg"]
        middle = _at(plan, (a+b)/2)
        mx, my = project_position(middle["longitude"], middle["latitude"])
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
    equilibrium_preview = None
    if frame is not None:
        simulation["seabed_grid"] = deepcopy(frame.grid)
        simulation["initial_equilibrium"] = deepcopy(frame.request)
        simulation["depth_m"] = -float(frame.field.evaluate(np.array([[0., 0.]]), gradient=False)[0])
        from .initial_equilibrium import resolve_initial_equilibrium
        equilibrium_preview = resolve_initial_equilibrium(project, simulation)
        if abs(equilibrium_preview["initial_material_length_m"]-initial_length) > max(1e-8, initial_length*1e-12):
            raise ValueError("verified equilibrium material length no longer matches planning inventory")
        # This is the actual bed depth at the declared anchor footprint used by
        # the physical checkpoint, not a flat replacement for the whole field.
        simulation["depth_m"] = equilibrium_preview["reference_depth_m"]
    material_model = _MaterialModel(simulation, _environment(simulation), first["ea_n"], first["ei_n_m2"], first["mass_kg_m"], simulation.get("added_mass_coefficient", 1))
    material_model.validate_coverage(at_end["material_m"])
    voyage = deepcopy(_config(c.get("voyage", {})))
    if set(voyage) & {"simulation", "resume_state", "duration_s"}:
        raise ValueError("prepare-plan owns voyage duration and initial simulation state")
    voyage.update({"duration_s": duration, "simulation": simulation})
    context = {"schema": "oceanroute.plan-voyage-mapping", "schema_version": 2 if equilibrium_mode else 1,
               "route_signature": analysis["route_signature"], "project_sha256": _digest(project),
               "source_plan_start_s": start, "source_plan_end_s": start+duration,
               "manufacturing_origin_m": max(0., origin_material), "initial_manufacturing_top_m": at_start["material_m"],
               "initial_natural_length_m": initial_length, "final_manufacturing_top_m": at_end["material_m"],
               "local_crs": local.to_string(), "origin_wgs84": [at_start["longitude"], at_start["latitude"]],
               "initial_state": "verified-discrete-equilibrium-zero-velocity" if equilibrium_mode else "analytical-homogeneous-catenary-zero-velocity", "instructions": mapping,
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
    target_xy = list(map(float, project_position(target_lon, target_lat)))
    if frame is not None:
        anchor_xy = frame.request["anchor_position_m"][:2]
        context["terrain_frame"] = frame.metadata()
        context["initial_equilibrium_preparation"] = {"solver": deepcopy(equilibrium_preview["solver"]),
                                                     "estimated_work_units": equilibrium_preview["estimated_work_units"],
                                                     "provenance": deepcopy(equilibrium_preview["provenance"])}
    else:
        angle = math.radians(controls[0]["heading_deg"])
        layback = initial["summary"]["layback_m"]
        anchor_xy = [-layback*math.sin(angle), -layback*math.cos(angle)]
    context["initial_anchor_xy_m"] = anchor_xy
    context["planned_start_touchdown_xy_m"] = target_xy
    if frame is not None:
        context["planned_start_touchdown_wgs84"] = [target_lon,target_lat]
    context["initial_target_touchdown_residual_m"] = math.dist(anchor_xy, target_xy)
    context["checksum_sha256"] = _digest(context)
    read_plan_mapping(context, simulation=simulation)
    voyage["plan_mapping"] = deepcopy(context)
    if frame is not None:
        return {"model": "explicit-manufacturing-geographic-equilibrium-plan-voyage-preparation-v2",
                "validation_status": "research", "config": voyage, "mapping": context, "plan": plan,
                "warnings": plan["warnings"] + frame.warnings + [
                    {"code": "EQUILIBRIUM_START_NOT_SURVEY_RECONSTRUCTION", "severity": "warning",
                     "message": "Initial cable was actually solved and independently checked in the explicit bed frame; it is a zero-velocity equilibrium before actuation, not a measured installation history. Initial manufacturing inventory is not paid out again."},
                    {"code": "PLAN_OFFSETS_REMAIN_FLAT_LOCAL_FIRST_CUT", "severity": "warning",
                     "message": "The existing ship-plan offsets remain homogeneous local flat-bottom first cuts. The verified variable-bed initial state and subsequent real dynamics do not certify the planned moving ship trajectory or planned touchdown."}],
                "assumptions": ["The complete known bilinear bed is used without flattening or extension. All horizontal axes/endpoints/seeds receive the same real translation in its projected metre CRS.",
                                "The explicit fixed anchor may be off bed and is not forced to the planned route touchdown; its geographic residual is reported separately.",
                                "Natural active inventory may include both suspended and bed-contact cable; route KP, manufacturing station, inventory and new payout remain distinct.",
                                "Initial equilibrium requires homogeneous wet weight/EA, zero bending, no initial bodies/flow/waves; the real initializer enforces these scope and force checks.",
                                "Preparation and fresh voyage startup each solve/check the declared raw initial inputs and report initialization budgets. Checkpoint resume preserves the validated physical state without solving initialization again."]}
    return {"model": "explicit-manufacturing-geographic-plan-voyage-preparation-v1", "validation_status": "research",
            "config": voyage, "mapping": context, "plan": plan,
            "warnings": plan["warnings"] + [{"code": "ANALYTICAL_START_NOT_SURVEY_RECONSTRUCTION", "severity": "warning",
                "message": "Initial cable is an analytical catenary with zero velocity, not a reconstructed installation state. Earlier plan payout occupies manufacturing stock and is not simulated again; the anchor is not forced to the planned geographic route."}],
            "assumptions": ["This preparation requires a known flat positive-depth bed and a homogeneous initial suspended interval.",
                            "Local vessel controls integrate projected instruction endpoints; route KP, manufacturing station, initial inventory and newly paid cable remain distinct.",
                            "Positive-length bodies require a rod/carrier model; point body loads are mapped only from declared measured physical data.",
                            "No past laying history, initial velocity, anchor survey or original-engine accuracy is inferred."]}
