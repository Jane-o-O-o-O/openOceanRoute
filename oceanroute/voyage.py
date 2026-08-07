"""Continuous research lay runs with conservative, checked seabed coarsening.

The original bottom anchor and every material interval stay in the dynamic
model. Only nearly straight, persistently settled flat-bed elements may be
merged. This is adaptive discretization, not a newly fixed touchdown boundary.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import time

import numpy as np

from .checkpoints import pack_checkpoint, read_checkpoint
from .simulation import (_MaterialModel, _environment, _integer, _num, _config,
                         simulate_lay)

SCHEMA = "oceanroute.voyage.checkpoint"
MESH_METHOD = "flat-bed-internal-step-conservative-material-map-v2"
FIELDS = (("node_material_m", "coordinates"), ("node_mass_kg", "mass"),
          ("node_dry_mass_kg", "dry_mass"), ("node_wet_weight_n", "weight"),
          ("segment_ea_n", "ea"), ("segment_wet_weight_n_m", "segment_weight"),
          ("segment_diameter_m", "segment_diameter"), ("segment_ei_n_m2", "segment_ei"),
          ("node_cable_drag_factor", "drag"), ("node_body_drag_factor", "body_drag"))


def _finite_json(value, limit=16_000_000):
    def canonical(v):
        if isinstance(v, float) and math.isfinite(v) and v.is_integer():
            return int(v)
        if isinstance(v, dict):
            return {k: canonical(x) for k, x in v.items()}
        if isinstance(v, list):
            return [canonical(x) for x in v]
        return v
    try:
        data = json.dumps(canonical(value), ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError, RecursionError, OverflowError) as error:
        raise ValueError("voyage state must contain finite serializable JSON") from error
    if len(data) > limit:
        raise ValueError("voyage state exceeds its declared JSON volume limit")
    return data


def _policy(raw):
    c = _config(raw)
    enabled = c.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("adaptive_mesh.enabled must be boolean")
    return {"enabled": enabled,
            "target_nodes": _integer(c, "target_nodes", 96, 12, 220),
            "max_merges_per_chunk": _integer(c, "max_merges_per_chunk", 24, 1, 64),
            "contact_guard_nodes": _integer(c, "contact_guard_nodes", 3, 2, 20),
            "max_angle_deg": _num(c, "max_angle_deg", .5, 0, 5),
            "max_chord_deviation_m": _num(c, "max_chord_deviation_m", .01, 0, 1),
            "max_material_position_shift_m": _num(c, "max_material_position_shift_m", .001, 0, 1),
            "max_velocity_m_s": _num(c, "max_velocity_m_s", .01, 0, .1),
            "min_settled_duration_s": _num(c, "min_settled_duration_s", 2, .02, 600),
            "max_segment_length_m": _num(c, "max_segment_length_m", 500, .01, 100000),
            "max_tension_change_n": _num(c, "max_tension_change_n", 2, 0, 100),
            "max_relative_tension_change": _num(c, "max_relative_tension_change", .02, 0, .1),
            "max_strain_change": _num(c, "max_strain_change", 1e-5, 0, .001),
            "max_center_of_mass_shift_m": _num(c, "max_center_of_mass_shift_m", .01, 0, 1),
            "max_relative_energy_change": _num(c, "max_relative_energy_change", .01, 0, .1),
            "max_absolute_energy_change_j": _num(c, "max_absolute_energy_change_j", .001, 0, 1),
            "probe_duration_s": _num(c, "probe_duration_s", .25, .02, 2),
            "probe_position_tolerance_m": _num(c, "probe_position_tolerance_m", .02, 0, 1),
            "probe_velocity_tolerance_m_s": _num(c, "probe_velocity_tolerance_m_s", .02, 0, 1),
            "probe_tension_tolerance_n": _num(c, "probe_tension_tolerance_n", 5, 0, 1000),
            "probe_relative_tension_tolerance": _num(c, "probe_relative_tension_tolerance", .02, 0, .1)}


def _material(config):
    e = _environment(config)
    return _MaterialModel(config, e, config["ea_n"], config["ei_n_m2"],
                          config["mass_kg_m"], config["added_mass_coefficient"])


def _metrics(p, v, rest, loads):
    chord = np.linalg.norm(np.diff(p, axis=0), axis=1)
    extension = np.maximum(chord-rest, 0)
    mass = loads["mass"]
    return {"natural_length_m": float(np.sum(rest)), "dry_mass_kg": float(np.sum(loads["dry_mass"])),
            "effective_mass_kg": float(np.sum(mass)), "wet_weight_n": float(np.sum(loads["weight"])),
            "linear_momentum_kg_m_s": np.sum(mass[:, None]*v, axis=0).tolist(),
            "angular_momentum_kg_m2_s": np.sum(np.cross(p, mass[:, None]*v), axis=0).tolist(),
            "center_of_mass_m": (mass@p/np.sum(mass)).tolist(),
            "kinetic_energy_j": float(.5*np.sum(mass*np.sum(v*v, axis=1))),
            "axial_elastic_energy_j": float(.5*np.sum(loads["ea"]/rest*extension**2))}


def _settled_triplet(saved, previous, j, policy, stable_material):
    a, old = saved["arrays"], previous["arrays"]
    old_coords = old["node_material_m"]
    ids = []
    for k in (j-1, j, j+1):
        if round(float(a["node_material_m"][k]), 7) not in stable_material:
            return False
        candidates = np.flatnonzero(np.abs(old_coords-a["node_material_m"][k]) <= 1e-7)
        if len(candidates) != 1:
            return False
        q = int(candidates[0]); ids.append(q)
        if not saved["state"]["contact_mask"][k] or not previous["state"]["contact_mask"][q]:
            return False
        if max(np.linalg.norm(a["velocities"][k]), np.linalg.norm(old["velocities"][q])) > policy["max_velocity_m_s"]:
            return False
        elapsed = saved["time_s"]-previous["time_s"]
        if elapsed <= 0 or np.linalg.norm(a["positions"][k]-old["positions"][q]) > policy["max_velocity_m_s"]*elapsed+1e-8:
            return False
    return True


def _transfer_one(saved, previous, material, j, policy, stable_material):
    a = saved["arrays"]
    p, v, rest = a["positions"], a["velocities"], a["rest_lengths_m"]
    n = len(p)
    if j <= 2 or j >= n-2 or n <= saved["config"]["nodes"]:
        return None
    if not _settled_triplet(saved, previous, j, policy, stable_material):
        return None
    low, high = a["node_material_m"][j+1], a["node_material_m"][j-1]
    # One homogeneous material row and no body influence across this element.
    if any(low-1e-7 <= b <= high+1e-7 for b in material.ends[:-1]):
        return None
    guard = 2*saved["state"]["segment_target_m"]
    if any(low-guard <= b["material_m"]+b["length_m"] and b["material_m"] <= high+guard for b in material.bodies):
        return None
    if np.any(a["segment_ei_n_m2"][j-1:j+1] > 0):
        return None
    lengths = np.linalg.norm(np.diff(p[j-1:j+2], axis=0), axis=1)
    if np.any(lengths < 1e-8):
        return None
    directions = np.diff(p[j-1:j+2], axis=0)/lengths[:, None]
    angle = math.degrees(math.acos(float(np.clip(directions[0]@directions[1], -1, 1))))
    axis = p[j+1]-p[j-1]
    chord = float(np.linalg.norm(axis))
    if chord < 1e-8:
        return None
    fraction = np.clip((p[j]-p[j-1])@axis/(chord*chord), 0, 1)
    deviation = float(np.linalg.norm(p[j]-p[j-1]-fraction*axis))
    merged_rest = float(rest[j-1]+rest[j])
    if angle > policy["max_angle_deg"] or deviation > policy["max_chord_deviation_m"] or merged_rest > policy["max_segment_length_m"]:
        return None
    next_rest = np.r_[rest[:j-1], merged_rest, rest[j+1:]]
    next_p, next_v = np.delete(p, j, axis=0), np.delete(v, j, axis=0)
    old_loads, loads = material.loads(rest), material.loads(next_rest)
    old_signed_strain = lengths/rest[j-1:j+1]-1
    new_signed_strain = chord/merged_rest-1
    new_strain = max(new_signed_strain, 0)
    material_position_shift = float(np.linalg.norm(p[j]-(p[j-1]+rest[j-1]/merged_rest*axis)))
    tension = float(loads["ea"][j-1]*new_strain)
    old_tensions = a["last_segment_tensions_n"][j-1:j+1]
    force_tolerance = policy["max_tension_change_n"]+policy["max_relative_tension_change"]*float(np.max(old_tensions))
    if (material_position_shift > policy["max_material_position_shift_m"]
            or np.max(np.abs(new_signed_strain-old_signed_strain)) > policy["max_strain_change"]
            or np.max(np.abs(tension-old_tensions)) > force_tolerance):
        return None
    # Actual new lumped mass increments, not arbitrary node averages.
    for old_index, new_index in ((j-1, j-1), (j+1, j)):
        delta = loads["mass"][new_index]-old_loads["mass"][old_index]
        if delta < -1e-8:
            return None
        next_v[new_index] = (old_loads["mass"][old_index]*v[old_index]+delta*v[j])/loads["mass"][new_index]
    before, after = _metrics(p, v, rest, old_loads), _metrics(next_p, next_v, next_rest, loads)
    for key in ("natural_length_m", "dry_mass_kg", "effective_mass_kg", "wet_weight_n"):
        if abs(before[key]-after[key]) > max(1e-8, abs(before[key])*1e-10):
            return None
    momentum_error = float(np.linalg.norm(np.array(before["linear_momentum_kg_m_s"])-after["linear_momentum_kg_m_s"]))
    center_error = float(np.linalg.norm(np.array(before["center_of_mass_m"])-after["center_of_mass_m"]))
    if momentum_error > max(1e-8, np.linalg.norm(before["linear_momentum_kg_m_s"])*1e-10) or center_error > policy["max_center_of_mass_shift_m"]:
        return None
    for key in ("kinetic_energy_j", "axial_elastic_energy_j"):
        if abs(before[key]-after[key]) > policy["max_absolute_energy_change_j"]+policy["max_relative_energy_change"]*before[key]:
            return None
    state = deepcopy(saved["state"])
    state.update(positions=next_p.tolist(), velocities=next_v.tolist(), rest_lengths_m=next_rest.tolist(),
                 contact_mask=[True if i >= j-1 and i <= j else x for i, x in enumerate(np.delete(state["contact_mask"], j).tolist())],
                 last_segment_tensions_n=np.r_[a["last_segment_tensions_n"][:j-1], tension,
                                               a["last_segment_tensions_n"][j+1:]].tolist())
    for field, key in FIELDS:
        state[field] = loads[key].tolist()
    document = pack_checkpoint(saved["config"], state, saved["time_s"], saved["numerical"])
    return document, {"removed_material_m": float(a["node_material_m"][j]), "merged_interval_m": [float(low), float(high)],
                      "angle_deg": angle, "chord_deviation_m": deviation,
                      "geometric_length_loss_m": float(np.sum(lengths)-chord),
                      "material_position_shift_m": material_position_shift,
                      "old_signed_axial_strain": old_signed_strain.tolist(), "merged_signed_axial_strain": float(new_signed_strain),
                      "merged_tension_n": tension, "before": before, "after": after,
                      "linear_momentum_error_kg_m_s": momentum_error,
                      "center_of_mass_shift_m": center_error,
                      "angular_momentum_error_kg_m2_s": float(np.linalg.norm(np.array(before["angular_momentum_kg_m2_s"])-after["angular_momentum_kg_m2_s"]))}


def coarsen_checkpoint(project, document, previous_document, policy=None, *, max_probe_work_units=12_000_000, settlement_evidence=None):
    """Return a candidate only after conservative transfer and a true twin solve."""
    limits = _policy(policy or {})
    saved, previous = read_checkpoint(document), read_checkpoint(previous_document)
    if not limits["enabled"] or len(saved["arrays"]["positions"]) <= max(limits["target_nodes"], saved["config"]["nodes"]):
        return {"checkpoint": deepcopy(document), "accepted": False, "reason": "not_needed", "transfers": []}
    if saved["config"].get("seabed_profile") is not None:
        return {"checkpoint": deepcopy(document), "accepted": False, "reason": "flat_bed_required", "transfers": []}
    evidence = settlement_evidence
    if (not isinstance(evidence, dict) or evidence.get("interval_start_s") != previous["time_s"]
            or abs(evidence.get("interval_end_s", -1)-saved["time_s"]) > 1e-8
            or evidence.get("internal_samples", 0) < 2
            or saved["time_s"]-previous["time_s"] < limits["min_settled_duration_s"]
            or not isinstance(evidence.get("stable_material_coordinates_m"), list)):
        return {"checkpoint": deepcopy(document), "accepted": False, "reason": "internal_settlement_evidence_required", "transfers": []}
    stable_material = {round(float(q), 7) for q in evidence["stable_material_coordinates_m"]}
    material = _material(saved["config"])
    candidate, transfers = deepcopy(document), []
    while len(saved["arrays"]["positions"]) > max(limits["target_nodes"], saved["config"]["nodes"]) and len(transfers) < limits["max_merges_per_chunk"]:
        contact = np.flatnonzero(saved["state"]["contact_mask"][1:])+1
        touchdown = int(contact[0]) if len(contact) else len(saved["arrays"]["positions"])-1
        found = False
        for j in range(len(saved["arrays"]["positions"])-3, touchdown+limits["contact_guard_nodes"], -1):
            value = _transfer_one(saved, previous, material, j, limits, stable_material)
            if value is not None:
                candidate, report = value; transfers.append(report)
                saved = read_checkpoint(candidate); found = True; break
        if not found:
            break
    if not transfers:
        return {"checkpoint": deepcopy(document), "accepted": False, "reason": "no_settled_safe_elements", "transfers": []}
    original = None
    probe_cap = _num({"limit": max_probe_work_units}, "limit", 12_000_000, 1, 12_000_000)
    try:
        original = simulate_lay(project, {"resume_state": document, "duration_s": limits["probe_duration_s"], "max_work_units": probe_cap})
        reduced = simulate_lay(project, {"resume_state": candidate, "duration_s": limits["probe_duration_s"], "max_work_units": probe_cap})
    except ValueError as error:
        return {"checkpoint": deepcopy(document), "accepted": False, "reason": "probe_unavailable",
                "detail": str(error), "transfers": transfers, "before_checkpoint": deepcopy(document),
                "candidate_checkpoint": candidate,
                "probe": {"estimated_work_units": (original["solver"]["estimated_work_units"] if original else 0)+probe_cap,
                          "failed_probe_budget_charge": probe_cap,
                          "solver_steps": original["solver"]["steps_this_run"] if original else 0}}
    a, b = original["frames"][-1], reduced["frames"][-1]
    coords = np.asarray(a["node_material_m"])
    mapped = np.column_stack([np.interp(b["node_material_m"], coords[::-1], np.asarray(a["nodes"])[::-1, k]) for k in range(3)])
    position_error = float(np.max(np.linalg.norm(mapped-np.asarray(b["nodes"]), axis=1)))
    mapped_velocity = np.column_stack([np.interp(b["node_material_m"], coords[::-1], np.asarray(a["node_velocity_m_s"])[::-1, k]) for k in range(3)])
    velocity_error = float(np.max(np.linalg.norm(mapped_velocity-np.asarray(b["node_velocity_m_s"]), axis=1)))
    reduced_coords = np.asarray(b["node_material_m"])
    reconstructed = np.column_stack([np.interp(coords, reduced_coords[::-1], np.asarray(b["nodes"])[::-1, k]) for k in range(3)])
    reconstructed_velocity = np.column_stack([np.interp(coords, reduced_coords[::-1], np.asarray(b["node_velocity_m_s"])[::-1, k]) for k in range(3)])
    full_material_error = float(np.max(np.linalg.norm(reconstructed-np.asarray(a["nodes"]), axis=1)))
    full_velocity_error = float(np.max(np.linalg.norm(reconstructed_velocity-np.asarray(a["node_velocity_m_s"]), axis=1)))
    td_error = float(np.linalg.norm(np.asarray(a["touchdown"])-b["touchdown"]))
    tension_error = max(abs(a["top_tension_n"]-b["top_tension_n"]), abs(a["bottom_tension_n"]-b["bottom_tension_n"]))
    tension_limit = limits["probe_tension_tolerance_n"]+limits["probe_relative_tension_tolerance"]*max(a["top_tension_n"], a["bottom_tension_n"])
    peak_error = abs(original["summary"]["interval_max_tension_n"]-reduced["summary"]["interval_max_tension_n"])
    converged = original["solver"]["converged"] and reduced["solver"]["converged"]
    kept_ids = [int(np.argmin(np.abs(coords-q))) for q in b["node_material_m"]]
    contact_equal = all(original["checkpoint"]["state"]["contact_mask"][q] == reduced["checkpoint"]["state"]["contact_mask"][j] for j, q in enumerate(kept_ids))
    accepted = (converged and contact_equal and max(position_error, full_material_error, td_error) <= limits["probe_position_tolerance_m"]
                and max(velocity_error, full_velocity_error) <= limits["probe_velocity_tolerance_m_s"] and max(tension_error, peak_error) <= tension_limit)
    return {"checkpoint": candidate if accepted else deepcopy(document), "accepted": accepted,
            "reason": "probe_passed" if accepted else "probe_rejected", "transfers": transfers,
            "settlement_evidence": deepcopy(evidence),
            "before_checkpoint": deepcopy(document), "candidate_checkpoint": candidate,
            "probe": {"duration_s": limits["probe_duration_s"], "position_error_m": position_error,
                      "touchdown_error_m": td_error, "tension_error_n": tension_error,
                      "velocity_error_m_s": velocity_error, "internal_peak_tension_error_n": peak_error,
                      "full_material_position_error_m": full_material_error,
                      "full_material_velocity_error_m_s": full_velocity_error,
                      "original_converged": original["solver"]["converged"], "reduced_converged": reduced["solver"]["converged"],
                      "retained_contact_masks_equal": contact_equal,
                      "tension_limit_n": tension_limit, "original_contact_nodes": a["contact_nodes"],
                      "reduced_contact_nodes": b["contact_nodes"],
                      "solver_steps": original["solver"]["steps_this_run"]+reduced["solver"]["steps_this_run"],
                      "estimated_work_units": original["solver"]["estimated_work_units"]+reduced["solver"]["estimated_work_units"]}}


def _pack_voyage(physical, policy, chunks, history, origin_time, work):
    document = {"schema": SCHEMA, "schema_version": 1, "validation_status": "research",
                "mesh_method": MESH_METHOD,
                "physical_checkpoint": deepcopy(physical), "adaptive_mesh": deepcopy(policy),
                "completed_chunks": chunks, "mesh_history": deepcopy(history),
                "origin_time_s": origin_time, "estimated_work_units": work}
    document["checksum_sha256"] = hashlib.sha256(_finite_json(document)).hexdigest()
    return document


def read_voyage_checkpoint(document):
    if not isinstance(document, dict) or document.get("schema") != SCHEMA or document.get("schema_version") != 1 or isinstance(document.get("schema_version"), bool) or document.get("validation_status") != "research":
        raise ValueError("unsupported voyage checkpoint schema")
    if document.get("mesh_method") != MESH_METHOD:
        raise ValueError("voyage checkpoint mesh method is incompatible; rerun the development benchmark")
    payload = {k: v for k, v in document.items() if k != "checksum_sha256"}
    if document.get("checksum_sha256") != hashlib.sha256(_finite_json(payload)).hexdigest():
        raise ValueError("voyage checkpoint checksum mismatch")
    for key in ("physical_checkpoint", "adaptive_mesh", "completed_chunks", "mesh_history", "origin_time_s", "estimated_work_units"):
        if key not in document:
            raise ValueError("voyage checkpoint is missing actual state fields")
    read_checkpoint(document["physical_checkpoint"])
    _policy(document["adaptive_mesh"])
    _integer(document, "completed_chunks", 0, 0, 1000000)
    _num(document, "origin_time_s", 0, 0, document["physical_checkpoint"]["time_s"])
    _num(document, "estimated_work_units", 0, 0, 1e15)
    if not isinstance(document["mesh_history"], list) or len(document["mesh_history"]) > 128:
        raise ValueError("voyage checkpoint mesh history exceeds its bound")
    for record in document["mesh_history"]:
        if not isinstance(record, dict) or not record.get("accepted") or not record.get("transfers"):
            raise ValueError("voyage mesh history must describe actual accepted transfers")
        read_checkpoint(record["before_checkpoint"])
        read_checkpoint(record["candidate_checkpoint"])
    _finite_json(document["mesh_history"], 12_000_000)
    return deepcopy(document)


def run_voyage(project, config, *, on_chunk=None, should_cancel=None):
    """Actual continued state; bounded workload and explicit partial-stop status.

    Callbacks receive only completed states, so cancellation never fabricates
    partial-step results. A caller may persist each chunk before proceeding.
    """
    c = _config(config); _finite_json(c)
    duration = _num(c, "duration_s", 3600, 0, 30*86400, strict=True)
    chunk_cap = _num(c, "chunk_duration_s", 30, .02, 600)
    max_chunks = _integer(c, "max_chunks", 2000, 1, 10000)
    max_frames = _integer(c, "max_output_frames", 512, 2, 4096)
    max_work = _num(c, "max_total_work_units", 120000000, 1, 2000000000)
    max_history = _integer(c, "max_mesh_records", 64, 1, 128)
    policy = _policy(c.get("adaptive_mesh", {}))
    base = deepcopy(_config(c.get("simulation", {})))
    if any(k in base for k in ("resume_state", "save_checkpoints", "checkpoint_times_s")):
        raise ValueError("voyage owns continuation/checkpoint timing; provide resume_state at the voyage level")
    base.pop("duration_s", None)
    saved = c.get("resume_state")
    if saved is not None:
        restored = read_voyage_checkpoint(saved)
        if "adaptive_mesh" in c and policy != restored["adaptive_mesh"]:
            raise ValueError("resumed voyage must preserve its adaptive mesh policy")
        if base:
            raise ValueError("resumed voyage restores physical controls; start a separate branch to change simulation configuration")
        physical = restored["physical_checkpoint"]
        policy = restored["adaptive_mesh"]
        origin_time = restored["origin_time_s"]
        chunks, history, work = restored["completed_chunks"], restored["mesh_history"], restored["estimated_work_units"]
        base = {}
    else:
        physical, origin_time, chunks, history, work = None, 0., 0, [], 0.
    start = physical["time_s"] if physical else 0.
    end = start+duration
    if end > 1e9:
        raise ValueError("voyage absolute end exceeds 1e9 seconds")
    if not saved:
        base["ship_plan_horizon_s"] = max(end, _num(base, "ship_plan_horizon_s", end, 0, 1e9))
    frames, reports, warnings = [], [], {}
    status, reason, last = "completed", None, None
    chunk_count = 0; started_wall = time.monotonic()
    interval_work = 0.
    while (physical["time_s"] if physical else 0.) < end-1e-9:
        now = physical["time_s"] if physical else 0.
        if should_cancel is not None and should_cancel():
            status, reason = "cancelled", "cancel_requested"; break
        if chunk_count >= max_chunks:
            status, reason = "stopped", "chunk_budget"; break
        if interval_work >= max_work or max_work-interval_work < 1:
            status, reason = "stopped", "work_budget"; break
        step = min(chunk_cap, end-now)
        if physical:
            parsed = read_checkpoint(physical)
            a, settings = parsed["arrays"], parsed["config"]
            current_count = len(a["positions"])
            plan = parsed["state"]["plan"]
            index = parsed["state"]["plan_index"]
            while index+1 < len(plan) and plan[index+1]["time_s"] <= now+1e-10:
                index += 1
            max_payout = max([plan[index]["payout_m_s"]]+[row["payout_m_s"] for row in plan[index+1:] if now < row["time_s"] < end])
            available = 253-current_count
            if max_payout > 0:
                if available <= 0:
                    status, reason = "stopped", "mesh_capacity"; break
                step = min(step, available*parsed["state"]["segment_target_m"]/max_payout*.9)
            # A conservative work bound includes insertion, outputs, and bodies.
            iterations = settings["solver_iterations"]
            dt = min(settings["internal_dt_s"], settings["dt_s"])
            estimated_nodes = min(256, current_count+math.ceil(max_payout*step/parsed["state"]["segment_target_m"])+2)
            step = min(step, 10_000_000*dt/(estimated_nodes*(iterations+len(settings.get("inline_bodies", []))+1)),
                       25000*dt, 1900*settings["dt_s"])
            request = {"resume_state": physical, "duration_s": step}
        else:
            # Fresh state still uses the bounded original solver's validators.
            step = min(step, 30.)
            request = {**base, "duration_s": step}
        if step < .02-1e-12:
            status, reason = "stopped", "minimum_chunk_or_mesh_capacity"; break
        previous = physical
        stable_material = None
        internal_samples = 0
        observed_end = now
        def observe(value):
            nonlocal stable_material, internal_samples, observed_end
            stable = {round(q, 7) for q, speed, contact in zip(value["node_material_m"], value["node_speed_m_s"], value["contact_mask"])
                      if contact and speed <= policy["max_velocity_m_s"]}
            stable_material = stable if stable_material is None else stable_material & stable
            internal_samples += 1; observed_end = value["time_s"]
        while True:
            request["max_work_units"] = min(12_000_000, max_work-interval_work)
            try:
                result = simulate_lay(project, request, state_observer=observe if policy["enabled"] else None)
                break
            except ValueError as error:
                if "simulation exceeds computation limit" in str(error) or "payout would exceed 256" in str(error):
                    step /= 2
                    if step < .02:
                        status, reason = "stopped", "work_or_mesh_budget"; result = None; break
                    request["duration_s"] = step
                elif previous is not None and "cover the entire initial cable and all requested payout" in str(error):
                    status, reason = "stopped", "material_coverage"; result = None; break
                else:
                    raise
        if result is None:
            break
        last, physical = result, result["checkpoint"]
        chunk_count += 1; chunks += 1
        chunk_work = result["solver"]["estimated_work_units"]
        work += chunk_work; interval_work += chunk_work
        for warning in result["warnings"]:
            warnings[warning["code"]] = warning
        reports.append({"chunk": chunks, "start_time_s": now, "end_time_s": physical["time_s"],
                        "nodes": len(physical["state"]["positions"]), "solver": result["solver"],
                        "summary": result["summary"]})
        new_frames = result["frames"] if not frames else result["frames"][1:]
        frames.extend(new_frames)
        # Keep real samples and both endpoints. Never synthesize missing frames.
        if len(frames) > max_frames:
            ids = np.unique(np.linspace(0, len(frames)-1, max_frames, dtype=int))
            frames = [frames[i] for i in ids]
        probe_cap = min(12_000_000, (max_work-interval_work)/2)
        if previous is not None and physical["time_s"] < end-1e-9 and policy["enabled"] and len(history) < max_history and probe_cap >= 1:
            evidence = {"interval_start_s": now, "interval_end_s": observed_end, "internal_samples": internal_samples,
                        "stable_material_coordinates_m": sorted(stable_material or [])}
            mesh = coarsen_checkpoint(project, physical, previous, policy, max_probe_work_units=probe_cap, settlement_evidence=evidence)
            if "probe" in mesh:
                probe_work = mesh["probe"]["estimated_work_units"]
                work += probe_work; interval_work += probe_work
            reports[-1]["mesh_reason"] = mesh["reason"]
            reports[-1]["mesh_diagnostics"] = {"accepted": mesh["accepted"], "reason": mesh["reason"],
                "attempted_merges": len(mesh["transfers"]), "probe": deepcopy(mesh.get("probe")), "detail": mesh.get("detail")}
            if mesh["accepted"]:
                proposed = mesh.pop("checkpoint")
                try:
                    _finite_json(history+[mesh], 12_000_000)
                except ValueError:
                    status, reason = "stopped", "mesh_audit_volume"
                else:
                    physical = proposed
                    history.append(mesh)
                    reports[-1]["nodes_after_transfer"] = len(physical["state"]["positions"])
                    reports[-1]["merged_elements"] = len(mesh["transfers"])
        elif physical["time_s"] < end-1e-9 and policy["enabled"] and len(history) >= max_history and len(physical["state"]["positions"]) > policy["target_nodes"]:
            status, reason = "stopped", "mesh_history_budget"
        envelope = _pack_voyage(physical, policy, chunks, history, origin_time, work)
        if on_chunk is not None:
            on_chunk({"checkpoint": envelope, "chunk": deepcopy(reports[-1]),
                      "requested_end_time_s": end, "elapsed_wall_s": time.monotonic()-started_wall})
        if reason is not None:
            break
    checkpoint = _pack_voyage(physical, policy, chunks, history, origin_time, work) if physical else saved
    actual_end = physical["time_s"] if physical else start
    state = physical["state"] if physical else None
    if reason:
        warnings["VOYAGE_PARTIAL_STOP"] = {"code": "VOYAGE_PARTIAL_STOP", "severity": "warning", "message": "Continuous run stopped at a completed state: "+reason}
    return {"model": "continuous-material-lay-with-checked-flat-bed-coarsening-v1", "validation_status": "research",
            "status": status, "stop_reason": reason, "checkpoint": checkpoint, "frames": frames, "chunks": reports,
            "warnings": list(warnings.values()), "assumptions": [
                "All cable material and original anchor remain active; no settled tail is deleted or fixed at a new point.",
                "Coarsening applies only to persistently settled nearly straight homogeneous flat-bed elements with zero bending stiffness and no nearby bodies.",
                "Natural length, integrated masses, weight and linear momentum are conserved; geometric, energy and centroid defects are bounded and reported.",
                "Each proposed transfer is tested by actual original/reduced short dynamic solves before acceptance.",
                "Longer elapsed time does not imply full-voyage engineering accuracy; spherical route/2D terrain and device control are not inferred.",
                "Frames are retained actual samples; all chunk intervals and final exact state are provided."],
            "summary": {"requested_duration_s": duration, "start_time_s": start, "end_time_s": actual_end,
                        "computed_duration_s": actual_end-start, "requested_end_time_s": end,
                        "chunks_this_run": chunk_count, "completed_chunks": chunks,
                        "accepted_mesh_records": len(history), "final_nodes": len(state["positions"]) if state else None,
                        "paid_out_m": state["paid_out_m"] if state else None,
                        "natural_length_m": float(sum(state["rest_lengths_m"])) if state else None,
                        "material_balance_residual_m": abs(sum(state["rest_lengths_m"])-state["initial_material_length_m"]-state["paid_out_m"]) if state else None,
                        "estimated_work_units_this_run": interval_work, "estimated_work_units": work,
                        "max_tension_n": state["statistics"]["max_force_n"] if state else None,
                        "elapsed_wall_s": time.monotonic()-started_wall},
            "last_solver_summary": last["summary"] if last else None}
