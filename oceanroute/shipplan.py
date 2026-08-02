"""Research ship-plan construction, scenario analysis and payout search.

Geographic planning and the local material-node dynamics are deliberately
separate: a long mixed-cable route is not silently simulated as one uniform line.
"""
from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
import math

import numpy as np

from .core import analyze_project
from .geodesy import GEOD, interpolate
from .checkpoints import merge_resume_config
from .simulation import (_cable_defaults, _config, _integer, _num, _warning,
                         _environment, _resumed_plan, simulate_lay, steady_state)


def _result(model, assumptions):
    return {"model": model, "validation_status": "research", "assumptions": assumptions, "warnings": []}


def _positive(c, key, default, hi):
    return _num(c, key, default, 0, hi, strict=True)


def build_ship_plan(project: dict, config: dict) -> dict:
    """Build a complete, time-indexed preliminary plan from route and assembly.

    Offsets are steady-state first cuts. Moving legs are geographic straight
    segments between offset samples, with explicitly timed transitions at turns.
    Additional physical cable length is fed at its declared route KP.
    """
    c = _config(config)
    analysis = analyze_project(project)
    spacing = _positive(c, "sample_spacing_m", 5000, 1e6)
    maximum = _integer(c, "max_samples", 500, 5, 2000)
    body_pause = _num(c, "body_pause_s", 0, 0, 86400)
    feed_rate = _positive(c, "event_payout_m_s", .2, 25)
    transition_speed = _positive(c, "transition_speed_m_s", .5, 20)
    base_tension = _positive(c, "bottom_tension_n", 1000, 1e9)
    override_speed = _positive(c, "ship_speed_m_s", 1, 20) if "ship_speed_m_s" in c else None
    result = _result("geographic-steady-offset-ship-plan-v1", [
        "Geographic route is sampled and vessel positions offset using homogeneous, flat-bottom steady cable solutions.",
        "Each moving instruction follows a WGS84 geodesic between successive vessel samples; route KP remains separate.",
        "Route corners and cable changes include timed vessel-offset repositioning with zero payout; dynamic verification is required.",
        "Mixed cable types change steady properties at declared route boundaries; transitions and inline-body dynamics are not solved.",
        "Additional cable/body/allowance length is fed while stationary at its route KP; replacement-body length stays within the base route budget."])
    result["warnings"] = deepcopy(analysis["warnings"])
    legs = analysis["legs"]
    if not legs or analysis["summary"]["surface_length_m"] <= 1e-7:
        raise ValueError("ship plan requires a route with positive horizontal length")
    types = {str(x["id"]): x for x in project.get("cable_types", [])}
    points = project["route"]["points"]
    curve = project["route"].get("curve", "rhumb")
    rpl = analysis["rpl"]
    total_kp = analysis["summary"]["surface_length_m"]
    if any(not isinstance(x, dict) for x in project.get("route", {}).get("legs", [])):
        raise ValueError("route leg options must be objects")
    events = []
    event_number = 0

    def add_event(kp, name, kind, duration=0., material=0., rate=0., body_id=None):
        nonlocal event_number
        if not duration and not material:
            return
        events.append({"id": f"event-{event_number}", "kp_m": kp, "name": name, "kind": kind,
                       "duration_s": duration, "material_length_m": material,
                       "payout_m_s": rate, "body_id": body_id})
        event_number += 1

    for body in analysis.get("bodies", []):
        kp = float(body["kp_m"])
        pause = _num(body, "deployment_pause_s", body_pause, 0, 86400)
        if "stop_hours" in body:
            pause += 3600*_num(body, "stop_hours", 0, 0, 24)
        add_event(kp, body.get("name", "Inline body deployment"), "body_pause", pause, body_id=body["id"])
        if body.get("length_mode") == "additional" and body["length_m"] > 0:
            length = float(body["length_m"])
            add_event(kp, body.get("name", "Additional body"), "additional_body_feed", length/feed_rate,
                      length, feed_rate, body["id"])
    route = project["route"]
    for item in route.get("allowances", project.get("allowances", [])):
        length = _num(item, "length_m", 0, 0, 1e7)
        add_event(_num(item, "kp_m", 0, 0, total_kp), item.get("name", "Cable allowance"), "allowance_feed",
                  length/feed_rate, length, feed_rate)
    for i, item in enumerate(route.get("legs", [])):
        stop = 3600*_num(item, "stop_hours", 0, 0, 1000)
        add_event(legs[i]["end_kp_m"], "Leg-end pause", "leg_pause", stop)
        length = _num(item, "allowance_m", 0, 0, 1e7)
        add_event(legs[i]["end_kp_m"], "Leg-end cable allowance", "allowance_feed", length/feed_rate, length, feed_rate)
    raw_events = c.get("events", [])
    if not isinstance(raw_events, list) or len(raw_events) > 1000:
        raise ValueError("events must be an array of at most 1000 entries")
    for item in raw_events:
        if not isinstance(item, dict) or "kp_m" not in item or "duration_s" not in item:
            raise ValueError("events require kp_m and duration_s")
        kp = _num(item, "kp_m", 0, 0, total_kp)
        duration = _num(item, "duration_s", 0, 0, 86400)
        rate = _num(item, "payout_m_s", 0, 0, 25)
        add_event(kp, str(item.get("name", "Operator event")), "operator_pause", duration, duration*rate, rate)
    if len(events) > 3000:
        raise ValueError("too many ship-plan events")
    events.sort(key=lambda x: (x["kp_m"], x["id"]))
    mandatory = {round(float(p["kp_m"]), 8) for p in rpl}
    mandatory.update(round(e["kp_m"], 8) for e in events)
    # Two samples at each noninitial leg boundary may be needed for offset change.
    available = maximum - len(mandatory) - len(legs)
    if available < 1:
        raise ValueError("route boundaries/events exceed max_samples; increase the sample budget")
    effective_spacing = max(spacing, total_kp/available)
    if effective_spacing > spacing*(1+1e-9):
        result["warnings"].append(_warning("SHIP_PLAN_COARSENED", "Sample spacing increased to remain inside max_samples; turn and event positions are retained."))
    profile = analysis.get("profile", [])
    profile_kps = [p["kp_m"] for p in profile]
    kps = [p["kp_m"] for p in rpl]
    cache = {}
    unknown_depth = False

    def depth_at(kp):
        if len(profile) < 2:
            return None
        j = min(len(profile)-2, max(0, bisect_right(profile_kps, kp)-1))
        p0, p1 = profile[j], profile[j+1]
        if p0.get("depth_m") is None or p1.get("depth_m") is None:
            return None
        fraction = (kp-p0["kp_m"])/(p1["kp_m"]-p0["kp_m"]) if p1["kp_m"] != p0["kp_m"] else 0
        return float(p0["depth_m"]+fraction*(p1["depth_m"]-p0["depth_m"]))

    def sample(kp, index):
        nonlocal unknown_depth
        leg = legs[index]
        first, last = points[index], points[index+1]
        fraction = (kp-leg["start_kp_m"])/leg["surface_length_m"] if leg["surface_length_m"] > 1e-7 else 0.
        fraction = min(1, max(0, fraction))
        lon, lat = interpolate(first["longitude"], first["latitude"], last["longitude"], last["latitude"], fraction, curve)
        cable = types.get(leg["cable_type_id"], {})
        speed = override_speed or _positive(cable, "lay_speed_m_s", 1, 20)
        heading = leg["bearing_deg"] or 0.
        depth = depth_at(kp)
        offset = None
        steady_summary = None
        if depth is not None and depth >= .001:
            physics = {key: c[key] for key in ("current_x_m_s", "current_y_m_s", "drag_coefficient", "water_density_kg_m3") if key in c}
            for key, default in (("wet_weight_n_m", 4), ("diameter_m", .02)):
                physics[key] = c.get(key, cable.get(key, default))
            physics.update({"depth_m": depth, "bottom_tension_n": base_tension,
                            "ship_speed_m_s": speed, "heading_deg": heading, "payout_m_s": speed, "nodes": 12})
            signature = tuple(sorted(physics.items()))
            if signature not in cache:
                cache[signature] = steady_state(physics)
            steady_summary = cache[signature]["summary"]
            offset = steady_summary["recommended_ship_offset_m"]
            azimuth = math.degrees(math.atan2(offset[0], offset[1])) % 360
            vessel_lon, vessel_lat, _ = GEOD.fwd(lon, lat, azimuth, math.hypot(*offset))
        else:
            unknown_depth = True
            vessel_lon, vessel_lat = lon, lat
        return {"kp_m": float(kp), "longitude": lon, "latitude": lat, "depth_m": depth,
                "vessel_longitude": float(vessel_lon), "vessel_latitude": float(vessel_lat),
                "ship_offset_m": offset, "steady_summary": steady_summary,
                "leg_index": index, "cable_type_id": leg["cable_type_id"],
                "route_heading_deg": heading, "requested_speed_m_s": speed}

    samples = []
    for index, leg in enumerate(legs):
        if leg["surface_length_m"] <= 1e-7:
            continue
        count = max(1, math.ceil(leg["surface_length_m"]/effective_spacing))
        locations = set(np.linspace(leg["start_kp_m"], leg["end_kp_m"], count+1).tolist())
        locations.update(e["kp_m"] for e in events if leg["start_kp_m"] <= e["kp_m"] <= leg["end_kp_m"])
        samples.extend(sample(kp, index) for kp in sorted(locations))
    if len(samples) > maximum + len(legs):
        raise ValueError("ship plan sampling exceeded its bounded sample budget")
    instructions, timed_points = [], []
    elapsed = 0.
    cable_paid = 0.
    track_length = 0.
    consumed = set()

    def stationary_events(at):
        nonlocal elapsed, cable_paid
        for event in events:
            if event["id"] in consumed or abs(event["kp_m"]-at["kp_m"]) > 1e-6:
                continue
            instructions.append({"time_s": elapsed, "end_time_s": elapsed+event["duration_s"],
                                 "duration_s": event["duration_s"], "speed_m_s": 0.,
                                 "heading_deg": at["route_heading_deg"], "payout_m_s": event["payout_m_s"],
                                 "kind": event["kind"], "name": event["name"], "kp_start_m": at["kp_m"],
                                 "kp_end_m": at["kp_m"], "cable_start_m": cable_paid,
                                 "cable_end_m": cable_paid+event["material_length_m"],
                                 "cable_type_id": at["cable_type_id"], "body_id": event["body_id"],
                                 "vessel_start": [at["vessel_longitude"], at["vessel_latitude"]],
                                 "vessel_end": [at["vessel_longitude"], at["vessel_latitude"]]})
            elapsed += event["duration_s"]
            cable_paid += event["material_length_m"]
            consumed.add(event["id"])
            timed_points.append({**at, "time_s": elapsed, "cable_paid_m": cable_paid})

    timed_points.append({**samples[0], "time_s": elapsed, "cable_paid_m": cable_paid})
    stationary_events(samples[0])
    for previous, at in zip(samples, samples[1:]):
        azimuth, _, distance = GEOD.inv(previous["vessel_longitude"], previous["vessel_latitude"],
                                        at["vessel_longitude"], at["vessel_latitude"])
        transition = previous["leg_index"] != at["leg_index"]
        kp_delta = at["kp_m"]-previous["kp_m"]
        if transition and abs(kp_delta) > 1e-5:
            raise ValueError("ship-plan internal route-boundary inconsistency")
        speed = transition_speed if transition else at["requested_speed_m_s"]
        duration = float(distance)/speed
        leg = legs[at["leg_index"]]
        material = 0. if transition else leg["cable_length_m"]*kp_delta/leg["surface_length_m"]
        if material > 1e-7 and duration <= 1e-9:
            raise ValueError("steady vessel offsets collapse a material-laying segment; refine the plan")
        payout = material/duration if duration else 0.
        if payout > 25:
            raise ValueError("calculated plan payout exceeds 25 m/s; reduce speed or revise cable budget")
        if duration > 1e-9:
            instructions.append({"time_s": elapsed, "end_time_s": elapsed+duration, "duration_s": duration,
                                 "speed_m_s": speed, "heading_deg": azimuth % 360, "payout_m_s": payout,
                                 "kind": "offset_transition" if transition else "lay",
                                 "name": "Vessel offset transition" if transition else f"Lay leg {at['leg_index']+1}",
                                 "kp_start_m": previous["kp_m"], "kp_end_m": at["kp_m"],
                                 "cable_start_m": cable_paid, "cable_end_m": cable_paid+material,
                                 "cable_type_id": at["cable_type_id"],
                                 "vessel_start": [previous["vessel_longitude"], previous["vessel_latitude"]],
                                 "vessel_end": [at["vessel_longitude"], at["vessel_latitude"]]})
            elapsed += duration
            track_length += float(distance)
            cable_paid += material
        timed_points.append({**at, "time_s": elapsed, "cable_paid_m": cable_paid})
        stationary_events(at)
    if len(consumed) != len(events):
        raise ValueError("some event KP positions could not be scheduled on the positive-length route")
    # Terminal zero rates make the schedule safe to extend for local analysis.
    terminal = {"time_s": elapsed, "speed_m_s": 0., "heading_deg": samples[-1]["route_heading_deg"], "payout_m_s": 0.}
    compatible = [{key: row[key] for key in ("time_s", "speed_m_s", "heading_deg", "payout_m_s")} for row in instructions]
    compatible.append(terminal)
    operator_extra = sum(e["material_length_m"] for e in events if e["kind"] == "operator_pause")
    expected = analysis["summary"]["cable_length_m"] + operator_extra
    if unknown_depth:
        result["warnings"].append(_warning("SHIP_OFFSET_UNAVAILABLE", "Unknown or shore-level water depth has no suspended-cable solution; those vessel samples lie on the route provisionally with null offsets."))
    if len({leg["cable_type_id"] for leg in legs}) > 1:
        result["warnings"].append(_warning("MIXED_PLAN_NOT_MIXED_DYNAMICS", "Mixed cable types are included in the plan; local dynamics still requires separate homogeneous scenarios."))
    if analysis.get("bodies"):
        result["warnings"].append(_warning("BODY_EVENT_APPROXIMATION", "Inline bodies have budgeted lengths and event pauses; their mass, hydrodynamics and engine handling are not simulated."))
    if operator_extra:
        result["warnings"].append(_warning("OPERATOR_EXTRA_CABLE", "Operator pause payout adds material beyond the planned assembly length; update the manufacturing budget."))
    residual = abs(cable_paid-expected)
    if residual > max(1e-6, expected*1e-9):
        raise ValueError("ship-plan payout balance does not match route/assembly material")
    result.update({"instructions": instructions, "ship_plan": compatible, "vessel_waypoints": timed_points,
                   "target_route": [{"kp_m": s["kp_m"], "longitude": s["longitude"], "latitude": s["latitude"], "depth_m": s["depth_m"]} for s in samples],
                   "events": events, "route_signature": analysis["route_signature"],
                   "summary": {"duration_s": elapsed, "vessel_track_length_m": track_length, "route_length_m": total_kp,
                               "assembly_length_m": analysis["summary"]["cable_length_m"], "paid_out_m": cable_paid,
                               "operator_extra_cable_m": operator_extra, "material_balance_residual_m": residual,
                               "instruction_count": len(instructions), "sample_count": len(samples),
                               "effective_sample_spacing_m": effective_spacing,
                               "pause_duration_s": sum(row["duration_s"] for row in instructions if row["speed_m_s"] == 0),
                               "transition_duration_s": sum(row["duration_s"] for row in instructions if row["kind"] == "offset_transition")}})
    return result


def _dynamic_config(project, config, runs):
    raw = _config(config)
    merged, saved = merge_resume_config(raw)
    c = _cable_defaults(project, merged)
    c.setdefault("duration_s", 30)
    c.setdefault("dt_s", 1)
    c.setdefault("nodes", 16)
    c.setdefault("internal_dt_s", .1)
    c.setdefault("solver_iterations", 16)
    duration = _positive(c, "duration_s", 30, 180)
    interval = _positive(c, "dt_s", 1, 60)
    nodes = _integer(c, "nodes", 16, 6, 100 if saved else 48)
    if saved: nodes=len(saved["arrays"]["positions"])
    iterations = _integer(c, "solver_iterations", 16, 2, 40 if saved else 24)
    internal = _num(c, "internal_dt_s", .1, .002 if saved else .025, .25)
    if _num(c, "heave_amplitude_m", 0, 0, 20) > 0:
        period = _positive(c, "heave_period_s", 10, 1000)
        internal = min(internal, period/40)
    if math.ceil(duration/interval) > 1000:
        raise ValueError("scenario output grid exceeds 1000 intervals")
    if math.ceil(duration/min(internal, interval))*nodes*iterations*runs > 6000000:
        raise ValueError("scenario/search aggregate computation exceeds its limit; reduce duration, nodes or evaluations")
    c["max_work_units"]=min(_num(c,"max_work_units",12000000,1,12000000),6000000/runs)
    if saved:
        # Only controls explicitly supplied by this call may overwrite the
        # active command. Inherited defaults do not restart the original plan.
        for key in ("ship_speed_m_s","payout_m_s","heading_deg","ship_plan"):
            if key not in raw:
                c.pop(key,None)
    return c


def _boolean(c, key, default=False):
    value = c.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be boolean")
    return value


def _metrics(simulation, window):
    frames = simulation["frames"]
    times = np.array([f["time_s"] for f in frames], dtype=float)
    bottom = np.array([f["bottom_tension_n"] for f in frames], dtype=float)
    finish = times[-1]
    start = max(float(times[0]), finish-window)
    interior = (times > start) & (times < finish)
    grid = np.r_[start, times[interior], finish]
    values = np.interp(grid, times, bottom)
    # Trapezoids integrate a piecewise-linear signal, including exact window edges.
    mean = float(np.sum((values[:-1]+values[1:])/2*np.diff(grid))/(finish-start))
    variance = float(np.sum((values[:-1]**2+values[:-1]*values[1:]+values[1:]**2)/3*np.diff(grid))/(finish-start)-mean*mean)
    summary = simulation["summary"]
    return {"evaluation_start_s": start, "evaluation_end_s": float(finish),
            "mean_bottom_tension_n": mean, "bottom_tension_std_n": math.sqrt(max(0., variance)),
            "final_bottom_tension_n": float(bottom[-1]),
            "peak_top_tension_n": summary.get("interval_max_internal_top_tension_n",summary["max_top_tension_n"]),
            "peak_segment_tension_n": summary.get("interval_max_tension_n",summary["max_tension_n"]),
            "minimum_bend_radius_m": summary.get("interval_minimum_bend_radius_m",summary["minimum_bend_radius_m"]),
            "final_touchdown": summary["final_touchdown"],
            "paid_out_m": summary.get("interval_paid_out_m",summary["paid_out_m"]),
            "cumulative_paid_out_m": summary["paid_out_m"],
            "solver_converged": simulation["solver"]["converged"]}


def look_ahead(project: dict, config: dict, scenarios: list) -> dict:
    """Compare local scenarios from identical material-node initial state."""
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 6:
        raise ValueError("look-ahead requires 1 to 6 scenarios")
    c = _dynamic_config(project, config, len(scenarios))
    include = _boolean(c, "include_frames")
    window = _positive(c, "evaluation_window_s", c["duration_s"]/4, c["duration_s"])
    allowed = {"ship_speed_m_s", "heading_deg", "payout_m_s", "current_x_m_s", "current_y_m_s", "current_profile", "ship_plan", "heave_amplitude_m", "heave_period_s", "vessel_motion_series", "wave_kinematics"}
    prepared = []
    names = set()
    sample_interval = c["dt_s"]
    for i, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            raise ValueError("each scenario must be an object")
        name = str(scenario.get("name", f"Scenario {i+1}"))
        if not name or name in names or len(name) > 200:
            raise ValueError("scenario names must be unique nonempty strings of at most 200 characters")
        names.add(name)
        overrides = scenario.get("overrides", {})
        if not isinstance(overrides, dict) or set(overrides)-allowed:
            raise ValueError("scenario overrides may change controls/current/heave only; initial material, depth and mesh stay fixed")
        candidate = {**deepcopy(c), **deepcopy(overrides)}
        if _num(candidate, "heave_amplitude_m", 0, 0, 20) > 0:
            period = _positive(candidate, "heave_period_s", 10, 1000)
            if "resume_state" not in c:
                sample_interval = min(sample_interval, period/20)
        prepared.append((name, candidate, deepcopy(overrides)))
    c["dt_s"] = sample_interval
    _dynamic_config(project, c, len(scenarios))
    result = _result("common-initial-state-dynamic-look-ahead-v1", [
        "Each branch independently uses the same material properties, inline-body loading and actual initial material-node state.",
        "A supplied checkpoint restores actual positions, velocities, natural lengths, contact, boundaries and absolute ship-command clock; otherwise branches start from the analytical initial geometry.",
        "All branches share the output time grid and the final evaluation window; bottom-tension means are time-weighted.",
        "Heave is prescribed vessel displacement, without wave particle kinematics or a vessel response model."])
    if "resume_state" in c and any(candidate.get("heave_amplitude_m",0)>0 and candidate.get("heave_period_s",10)/20<sample_interval for _,candidate,_ in prepared):
        result["warnings"].append(_warning("CHECKPOINT_OUTPUT_GRID_PRESERVED", "The checkpoint output grid is preserved. Reported peak tensions use internal solver steps; refine the original simulation grid for waveforms."))
    branches = []
    initial_nodes = None
    initial_velocities = None
    initial_material = None
    reference_times = None
    for name, candidate, overrides in prepared:
        candidate["dt_s"] = sample_interval
        simulation = simulate_lay(project, candidate)
        initial = np.array(simulation["frames"][0]["nodes"])
        velocities = np.array(simulation["frames"][0]["node_velocity_m_s"])
        material = np.array(simulation["frames"][0]["node_material_m"])
        times = np.array([f["time_s"] for f in simulation["frames"]])
        if initial_nodes is None:
            initial_nodes, reference_times = initial, times
            initial_velocities,initial_material = velocities,material
        elif initial.shape != initial_nodes.shape or not np.allclose(initial, initial_nodes, atol=1e-9, rtol=0):
            raise ValueError("scenarios alter initial cable geometry; keep the time-zero ship heading and initial conditions identical")
        elif not np.array_equal(times, reference_times):
            raise ValueError("scenario comparison has inconsistent output sampling")
        if not np.array_equal(velocities,initial_velocities) or not np.array_equal(material,initial_material):
            raise ValueError("scenarios alter the actual initial velocity or material coordinate state")
        branch = {"name": name, "overrides": overrides, "config": candidate,
                  "metrics": _metrics(simulation, window), "solver": simulation["solver"],
                  "warnings": simulation["warnings"],"checkpoint":simulation["checkpoint"]}
        if include:
            branch["simulation"] = simulation
        branches.append(branch)
    baseline = branches[0]["metrics"]
    for branch in branches:
        m = branch["metrics"]
        m["bottom_tension_change_n"] = m["mean_bottom_tension_n"]-baseline["mean_bottom_tension_n"]
        m["touchdown_shift_m"] = float(np.linalg.norm(np.array(m["final_touchdown"])-np.array(baseline["final_touchdown"])))
    result.update({"scenarios": branches, "initial_nodes": initial_nodes.tolist(),
                   "initial_velocities_m_s":initial_velocities.tolist(),"initial_material_m":initial_material.tolist(),
                   "summary": {"scenario_count": len(branches), "duration_s": c["duration_s"],
                               "start_time_s":float(reference_times[0]),"end_time_s":float(reference_times[-1]),
                               "output_interval_s": sample_interval, "evaluation_window_s": window,
                               "all_solvers_converged": all(b["metrics"]["solver_converged"] for b in branches)}})
    return result


def optimize_tension(project: dict, config: dict) -> dict:
    """Bounded derivative-free search of one payout rate using real dynamics.

    The objective is final-window mean touchdown-adjacent tension. This is a
    finite parameter search, not a closed-loop auto-tension controller.
    """
    raw = _config(config)
    if "target_bottom_tension_n" not in raw:
        raise ValueError("target_bottom_tension_n is required")
    target = _num(raw, "target_bottom_tension_n", 1000, 0, 1e8)
    maximum = _integer(raw, "max_evaluations", 9, 3, 16)
    c = _dynamic_config(project, raw, maximum)
    if c["duration_s"] > 120:
        raise ValueError("optimization duration_s is limited to 120 seconds per local window")
    window = _positive(c, "evaluation_window_s", c["duration_s"]/4, c["duration_s"])
    tolerance = _positive(c, "tension_tolerance_n", max(10., target*.05), 1e8)
    future_controls = None
    active = None
    if "resume_state" in c:
        _,saved = merge_resume_config(c)
        plan,_ = _resumed_plan(raw,c,saved,_environment({**saved["config"],**c}),c["duration_s"])
        time = saved["time_s"]
        active = plan[max(0,bisect_right([r["time_s"] for r in plan],time+1e-10)-1)]
        future_controls = [{**active,"time_s":0.}]+[
            {**row,"time_s":row["time_s"]-time} for row in plan
            if time+1e-10<row["time_s"]<=time+c["duration_s"]]
    speed = _num(c, "ship_speed_m_s", active["speed_m_s"] if active else 1.5, 0, 20)
    nominal = _num(c, "payout_m_s", active["payout_m_s"] if active else speed, 0, 25)
    low = _num(c, "payout_min_m_s", 0, 0, 25)
    high = _num(c, "payout_max_m_s", min(25., max(speed*1.5, nominal*1.5, .1)), 0, 25)
    if high <= low:
        raise ValueError("payout_max_m_s must exceed payout_min_m_s")
    include = _boolean(c, "include_frames")
    records = []
    cache = {}
    best_simulation = None
    best_record = None
    initial_nodes = None
    search_failures = []

    def candidate_config(rate):
        candidate = deepcopy(c)
        candidate["payout_m_s"] = rate
        if future_controls is not None:
            candidate["ship_plan"] = deepcopy(future_controls)
        if "ship_plan" in candidate:
            if not isinstance(candidate["ship_plan"], list):
                raise ValueError("ship_plan must be an array")
            for row in candidate["ship_plan"]:
                if not isinstance(row, dict):
                    raise ValueError("ship_plan instructions must be objects")
                if "payout_m_s" in row:
                    original_rate = _num(row, "payout_m_s", 0, 0, 25)
                    if original_rate:
                        row["payout_m_s"] = rate
        return candidate

    def evaluate(rate):
        nonlocal best_record, best_simulation, initial_nodes
        rate = float(rate)
        key = round(rate, 12)
        if key in cache or len(records)+len(search_failures) >= maximum:
            return
        candidate = candidate_config(rate)
        try:
            simulation = simulate_lay(project, candidate)
        except ValueError as error:
            search_failures.append({"payout_m_s": rate, "error": str(error)})
            cache[key] = None
            return
        initial = np.array(simulation["frames"][0]["nodes"])
        if initial_nodes is None:
            initial_nodes = initial
        elif initial.shape != initial_nodes.shape or not np.allclose(initial, initial_nodes, atol=1e-9, rtol=0):
            raise ValueError("payout search changed its initial cable state")
        metrics = _metrics(simulation, window)
        error = metrics["mean_bottom_tension_n"]-target
        record = {"evaluation": len(records)+len(search_failures)+1, "payout_m_s": rate,
                  "achieved_bottom_tension_n": metrics["mean_bottom_tension_n"], "error_n": error,
                  "rms_error_n": math.hypot(error, metrics["bottom_tension_std_n"]),
                  "solver_converged": metrics["solver_converged"], "metrics": metrics,
                  "warnings": simulation["warnings"]}
        records.append(record)
        cache[key] = record
        if metrics["solver_converged"] and (best_record is None or abs(error) < abs(best_record["error_n"])):
            best_record, best_simulation = record, simulation

    # A bracket and baseline are evaluated before adaptive refinement. The
    # objective may be nonmonotonic, so no monotonicity is asserted.
    for seed in (low, high, min(high, max(low, nominal)), (low+high)/2):
        evaluate(seed)
    while len(records)+len(search_failures) < maximum:
        points = sorted({low, high, *(r["payout_m_s"] for r in records), *(r["payout_m_s"] for r in search_failures)})
        anchor = best_record["payout_m_s"] if best_record else (low+high)/2
        adjacent = [(a,b) for a,b in zip(points, points[1:]) if a-1e-12 <= anchor <= b+1e-12]
        if not adjacent:
            break
        a, b = max(adjacent, key=lambda interval: interval[1]-interval[0])
        if b-a < 1e-6:
            break
        evaluate((a+b)/2)
    if best_record is None:
        cause = f"; first rejection: {search_failures[0]['error']}" if search_failures else "; all computed candidates exceeded the numerical residual tolerance"
        raise ValueError("no payout-search candidate produced a converged dynamic solution; refine numerical settings or revise the payout range" + cause)
    achieved = best_record["achieved_bottom_tension_n"]
    met = abs(achieved-target) <= tolerance
    result = _result("bounded-dynamic-payout-parameter-search-v1", [
        "One constant positive-payout rate is searched; explicitly zero-payout pauses remain zero.",
        "All candidates use the same initial cable state, duration, output time grid and time-weighted evaluation window.",
        "Objective is mean segment tension immediately above discrete touchdown; fluctuations and peak force are separately reported.",
        "No monotonicity, global optimum, continuous controller or original Auto-Tension equivalence is assumed."])
    if not met:
        result["warnings"].append(_warning("TENSION_TARGET_NOT_MET", "No evaluated converged candidate met the target tolerance; the closest candidate is reported."))
    if search_failures:
        result["warnings"].append(_warning("SEARCH_CANDIDATE_ERRORS", "Some candidate simulations were rejected; inspect search_failures."))
    result["warnings"].extend(deepcopy(best_record["warnings"]))
    result.update({"target_bottom_tension_n": target, "target_met": met,
                   "payout_m_s": best_record["payout_m_s"], "achieved_bottom_tension_n": achieved,
                   "error_n": achieved-target, "rms_error_n": best_record["rms_error_n"],
                   "tension_tolerance_n": tolerance, "best_metrics": best_record["metrics"],
                   "optimized_config": candidate_config(best_record["payout_m_s"]),
                   "evaluations": records, "search_failures": search_failures,
                   "summary": {"evaluation_count": len(records)+len(search_failures), "valid_evaluations": len(records),
                               "max_evaluations": maximum, "evaluation_window_s": window,
                               "payout_min_m_s": low, "payout_max_m_s": high}})
    if include:
        result["simulation"] = best_simulation
    return result
