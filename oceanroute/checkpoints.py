"""Serializable, checksummed exact state for the research dynamic integrator.

This checksum detects accidental changes; it is not a security signature.
No executable objects, pickles, or frame-derived velocity reconstruction exist.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math

import numpy as np

SCHEMA = "oceanroute.dynamic.checkpoint"
SCHEMA_VERSION = 1
MODEL = "material-lumped-mass-xpbd-cable-lay-v2"
MODEL_2D = "material-lumped-mass-xpbd-cable-lay-v3"
CONTACT_FIELDS = {"node_seabed_normal", "node_contact_normal_impulse_n_s",
                  "node_contact_friction_impulse_n_s", "last_contact_step_s"}
CONTACT_STATS = {"max_contact_normal_force_n", "max_contact_penetration_m",
                 "max_inward_contact_velocity_m_s", "friction_dissipation_j"}

FROZEN_FIELDS = {
    "depth_m", "wet_weight_n_m", "diameter_m", "water_density_kg_m3", "drag_coefficient", "bottom_tension_n",
    "nodes", "dt_s", "internal_dt_s", "solver_iterations", "ea_n", "ei_n_m2", "mass_kg_m",
    "added_mass_coefficient", "damping_ratio", "seabed_friction", "seabed_profile", "seabed_grid",
    "initial_suspended_material_m", "material_segments", "inline_bodies", "cable_type_id",
}


def _digest(document):
    payload = {k: v for k, v in document.items() if k != "checksum_sha256"}
    def normalize(value):
        # JSON/JavaScript has one numeric type: 1.0 becomes 1 after parsing and
        # stringifying. Canonicalize equal integer-valued numbers, including -0.
        if isinstance(value,float) and math.isfinite(value) and value.is_integer():
            return int(value)
        if isinstance(value,dict):
            return {key:normalize(item) for key,item in value.items()}
        if isinstance(value,list):
            return [normalize(item) for item in value]
        return value
    try:
        encoded = json.dumps(normalize(payload), sort_keys=True, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, OverflowError, RecursionError) as error:
        raise ValueError("checkpoint must contain finite, serializable JSON data") from error
    if len(encoded) > 2_000_000:
        raise ValueError("checkpoint exceeds the 2 MB serialized limit")
    return hashlib.sha256(encoded).hexdigest()


def pack_checkpoint(config, state, time, numerical):
    is_2d = "seabed_grid" in config
    document = {"schema": SCHEMA, "schema_version": 2 if is_2d else SCHEMA_VERSION, "model": MODEL_2D if is_2d else MODEL,
                "validation_status": "research", "source": "oceanroute.simulation.simulate_lay",
                "time_s": float(time), "config": deepcopy(config), "state": deepcopy(state),
                "numerical": deepcopy(numerical)}
    document["checksum_sha256"] = _digest(document)
    return document


def _number(value, field, low=0., high=1e15):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"checkpoint {field} must be a finite number")
    try:
        value = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"checkpoint {field} must be a finite number") from error
    if not math.isfinite(value):
        raise ValueError(f"checkpoint {field} must be a finite number")
    if not low <= value <= high:
        raise ValueError(f"checkpoint {field} is outside its bounds")
    return float(value)


def _array(value, shape, field, limit=1e15):
    if not isinstance(value, list):
        raise ValueError(f"checkpoint {field} must be an array")
    try:
        array = np.asarray(value, dtype=float)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"checkpoint {field} has invalid numeric values") from error
    if array.shape != shape or not np.all(np.isfinite(array)) or np.any(np.abs(array)>limit):
        raise ValueError(f"checkpoint {field} has invalid dimensions or values")
    # np.asarray would otherwise silently accept booleans/string numbers.
    def strict_numbers(row):
        return all(strict_numbers(v) if isinstance(v,list) else
                   isinstance(v,(int,float)) and not isinstance(v,bool) for v in row)
    if not strict_numbers(value):
        raise ValueError(f"checkpoint {field} must contain numeric JSON values")
    return array


def read_checkpoint(document):
    if not isinstance(document, dict):
        raise ValueError("resume_state must be a checkpoint object")
    required = {"schema", "schema_version", "model", "time_s", "config", "state", "numerical", "checksum_sha256", "source", "validation_status"}
    if not required <= document.keys():
        raise ValueError("checkpoint is missing required schema/state/provenance fields")
    if document["schema"] != SCHEMA or not isinstance(document["schema_version"],int) or document["schema_version"] not in (1, 2) or isinstance(document["schema_version"],bool):
        raise ValueError("unsupported checkpoint schema/version")
    is_2d = document["schema_version"] == 2
    if document["model"] != (MODEL_2D if is_2d else MODEL) or document["source"] != "oceanroute.simulation.simulate_lay" or document["validation_status"]!="research":
        raise ValueError("checkpoint model or source is incompatible")
    if document["checksum_sha256"] != _digest(document):
        raise ValueError("checkpoint checksum mismatch; preserve the complete original saved state")
    time = _number(document["time_s"], "time_s", 0, 1e9)
    c, s, numerical = document["config"], document["state"], document["numerical"]
    if not isinstance(c,dict) or not isinstance(s,dict) or not isinstance(numerical,dict):
        raise ValueError("checkpoint config/state/numerical must be objects")
    if is_2d != ("seabed_grid" in c):
        raise ValueError("checkpoint schema/model and seabed_grid presence are incompatible")
    if is_2d:
        from .bathymetry import BathymetryGrid
        grid = BathymetryGrid(c["seabed_grid"])
        if "seabed_profile" in c or c.get("wave_kinematics") is not None:
            raise ValueError("2D checkpoint cannot combine a profile or constant-depth wave kinematics")
    configuration_fields = {"duration_s","dt_s","internal_dt_s","nodes","solver_iterations","depth_m",
        "wet_weight_n_m","diameter_m","water_density_kg_m3","drag_coefficient","bottom_tension_n",
        "ea_n","ei_n_m2","mass_kg_m","added_mass_coefficient","damping_ratio","seabed_friction",
        "initial_suspended_material_m","ship_speed_m_s","payout_m_s","heading_deg",
        "current_x_m_s","current_y_m_s","heave_amplitude_m","heave_period_s","max_tension_n","min_bend_radius_m"}
    if not configuration_fields<=c.keys():
        raise ValueError("checkpoint is missing resolved physical/control configuration fields")
    keys = {"positions", "velocities", "rest_lengths_m", "node_material_m", "node_mass_kg", "node_dry_mass_kg",
            "node_wet_weight_n", "segment_ea_n", "segment_wet_weight_n_m", "segment_diameter_m",
            "segment_ei_n_m2", "node_cable_drag_factor", "node_body_drag_factor",
            "ship", "anchor", "paid_out_m", "initial_material_length_m", "segment_target_m",
            "last_segment_tensions_n", "plan", "plan_index", "contact_mask", "heave_phase_origin_s",
            "heave_offset_z_m", "statistics"}
    if not keys <= s.keys():
        raise ValueError("checkpoint is missing actual integrator state fields")
    if is_2d and not CONTACT_FIELDS <= s.keys():
        raise ValueError("2D checkpoint is missing actual contact diagnostic state")
    if not {"output_grid_origin_s", "internal_dt_s", "output_dt_s", "solver_iterations", "scheme"} <= numerical.keys():
        raise ValueError("checkpoint is missing numerical time-grid parameters")
    if numerical["scheme"] != ("implicit-compliant-material-nodes-2d-contact-v3" if is_2d else "implicit-compliant-material-nodes-v2"):
        raise ValueError("checkpoint integration scheme is incompatible")
    _number(numerical["internal_dt_s"], "numerical.internal_dt_s", .002, .25)
    _number(numerical["output_dt_s"], "numerical.output_dt_s", .02, 60)
    solver_iterations = _number(numerical["solver_iterations"], "numerical.solver_iterations", 2, 40)
    if solver_iterations != int(solver_iterations):
        raise ValueError("checkpoint solver_iterations must be an integer")
    positions = s["positions"]
    if not isinstance(positions,list) or not 6 <= len(positions) <= 256:
        raise ValueError("checkpoint needs 6 to 256 material nodes")
    n = len(positions)
    initial_nodes = _number(c["nodes"],"config.nodes",6,100)
    if initial_nodes!=int(initial_nodes) or initial_nodes>n:
        raise ValueError("checkpoint material node count is incompatible with the original mesh")
    arrays = {}
    for field, shape in (("positions",(n,3)),("velocities",(n,3)),("rest_lengths_m",(n-1,)),
                         ("node_material_m",(n,)),("node_mass_kg",(n,)),("node_dry_mass_kg",(n,)),
                         ("node_wet_weight_n",(n,)),("segment_ea_n",(n-1,)),
                         ("segment_wet_weight_n_m",(n-1,)),("segment_diameter_m",(n-1,)),
                         ("segment_ei_n_m2",(n-1,)),("node_cable_drag_factor",(n,)),("node_body_drag_factor",(n,)),
                         ("last_segment_tensions_n",(n-1,)),("ship",(3,)),("anchor",(3,))):
        arrays[field] = _array(s[field],shape,field)
    if np.min(arrays["rest_lengths_m"]) < 1e-6 or np.min(arrays["node_mass_kg"]) <= 0 or np.min(arrays["segment_ea_n"]) <= 0:
        raise ValueError("checkpoint has nonpositive material length, mass or stiffness")
    if np.min(arrays["last_segment_tensions_n"]) < -1e-9:
        raise ValueError("checkpoint tension-only model cannot have negative tension")
    if np.min(arrays["segment_diameter_m"])<=0 or np.min(arrays["segment_ei_n_m2"])<0 or np.min(arrays["node_cable_drag_factor"])<0 or np.min(arrays["node_body_drag_factor"])<0:
        raise ValueError("checkpoint contains invalid material diameter, bending stiffness or drag")
    if not np.allclose(arrays["positions"][0], arrays["ship"],rtol=0,atol=1e-9) or not np.allclose(arrays["positions"][-1],arrays["anchor"],rtol=0,atol=1e-9):
        raise ValueError("checkpoint ship/anchor boundary does not match actual nodes")
    if np.linalg.norm(arrays["velocities"][-1]) > 1e-9:
        raise ValueError("checkpoint fixed anchor must have zero velocity")
    for field in ("paid_out_m","initial_material_length_m","segment_target_m","heave_phase_origin_s"):
        _number(s[field],field,0,1e9)
    _number(s["heave_offset_z_m"],"heave_offset_z_m",-12000,100)
    if s["segment_target_m"] <= 0 or s["initial_material_length_m"] <= 0:
        raise ValueError("checkpoint reference material lengths must be positive")
    if abs(np.sum(arrays["rest_lengths_m"])-s["initial_material_length_m"]-s["paid_out_m"]) > max(1e-7,np.sum(arrays["rest_lengths_m"])*1e-10):
        raise ValueError("checkpoint cumulative payout/material balance is inconsistent")
    mask = s["contact_mask"]
    if not isinstance(mask,list) or len(mask)!=n or any(not isinstance(v,bool) for v in mask):
        raise ValueError("checkpoint contact mask must match all material nodes")
    if is_2d:
        for field, shape in (("node_seabed_normal", (n,3)), ("node_contact_normal_impulse_n_s", (n,)),
                             ("node_contact_friction_impulse_n_s", (n,3))):
            arrays[field] = _array(s[field], shape, field)
        h = _number(s["last_contact_step_s"], "last_contact_step_s", 0, .25)
        bed, normals = grid.surface(arrays["positions"])
        gap = arrays["positions"][:, 2]-bed
        if np.min(gap) < -1e-7 or mask != (gap <= 1e-8).tolist():
            raise ValueError("2D checkpoint contact mask/penetration does not match actual bathymetry")
        if not np.allclose(arrays["node_seabed_normal"], normals, rtol=0, atol=1e-9):
            raise ValueError("2D checkpoint normals do not match actual bathymetry")
        jn, jt = arrays["node_contact_normal_impulse_n_s"], arrays["node_contact_friction_impulse_n_s"]
        mu = _number(c["seabed_friction"], "seabed_friction", 0, 2)
        if np.min(jn) < 0 or np.any(np.linalg.norm(jt,axis=1) > mu*jn+1e-8) or np.max(np.abs(np.sum(jt*normals, axis=1))) > 1e-8:
            raise ValueError("2D checkpoint friction impulse violates its tangent/Coulomb bound")
        if np.any(jn[np.logical_not(mask)] > 1e-9):
            raise ValueError("2D checkpoint noncontact node cannot carry a solved contact impulse")
        if np.any(jn[[0,-1]] > 1e-9) or np.max(np.abs(jt[[0,-1]])) > 1e-9:
            raise ValueError("2D checkpoint kinematic ship/anchor cannot carry solved free-node contact impulses")
        if h == 0 and (np.any(jn) or np.any(jt)):
            raise ValueError("2D checkpoint nonzero impulse requires a positive contact step")
        if not isinstance(s["statistics"],dict) or not CONTACT_STATS <= s["statistics"].keys():
            raise ValueError("2D checkpoint contact statistics are incomplete")
        for field in CONTACT_STATS:
            _number(s["statistics"][field], "statistics."+field, 0, 1e15)
        if h > numerical["internal_dt_s"]+1e-10 or (h == 0 and s["statistics"].get("steps",0) != 0):
            raise ValueError("2D checkpoint last contact step is incompatible with the actual numerical clock")
    plan = s["plan"]
    if not isinstance(plan,list) or not 1 <= len(plan) <= 1000:
        raise ValueError("checkpoint ship command list is invalid")
    previous = -1.
    for row in plan:
        if not isinstance(row,dict) or not {"time_s","speed_m_s","heading_deg","payout_m_s"} <= row.keys():
            raise ValueError("checkpoint ship commands are incomplete")
        t = _number(row["time_s"],"plan.time_s",0,1e9)
        if t <= previous:
            raise ValueError("checkpoint ship commands must increase in absolute time")
        _number(row["speed_m_s"],"plan.speed_m_s",0,20)
        _number(row["payout_m_s"],"plan.payout_m_s",0,25)
        _number(row["heading_deg"],"plan.heading_deg",-36000,36000)
        previous = t
    index = s["plan_index"]
    if isinstance(index,bool) or not isinstance(index,int) or not 0 <= index < len(plan) or plan[index]["time_s"] > time+1e-8:
        raise ValueError("checkpoint ship-command cursor is invalid")
    if index+1 < len(plan) and plan[index+1]["time_s"] < time-1e-8:
        raise ValueError("checkpoint ship-command cursor skipped an earlier event")
    if not isinstance(s["statistics"],dict) or not {"steps","max_force_n","max_strain","worst_residual_m","minimum_radius_m","max_output_top_tension_n","max_internal_top_tension_n"} <= s["statistics"].keys():
        raise ValueError("checkpoint numerical statistics are incomplete")
    for field in ("steps","max_force_n","max_strain","worst_residual_m","max_output_top_tension_n","max_internal_top_tension_n"):
        _number(s["statistics"][field],"statistics."+field,0,1e15)
    if isinstance(s["statistics"]["steps"],bool) or not isinstance(s["statistics"]["steps"],int):
        raise ValueError("checkpoint cumulative step count must be an integer")
    if s["statistics"]["minimum_radius_m"] is not None:
        _number(s["statistics"]["minimum_radius_m"],"statistics.minimum_radius_m",0,1e15)
    for key, config_key in (("internal_dt_s","internal_dt_s"),("output_dt_s","dt_s"),("solver_iterations","solver_iterations")):
        if numerical[key] != c.get(config_key):
            raise ValueError("checkpoint numerical settings do not match its configuration")
    _number(numerical["output_grid_origin_s"],"output_grid_origin_s",0,time)
    return {"document":deepcopy(document),"config":deepcopy(c),"state":deepcopy(s),
            "arrays":arrays,"time_s":time,"numerical":deepcopy(numerical)}


def merge_resume_config(config):
    """Read/verify a state, merge allowed controls and reject model changes."""
    if "resume_state" not in config:
        return deepcopy(config), None
    saved = read_checkpoint(config["resume_state"])
    original = saved["config"]
    for key in FROZEN_FIELDS:
        if key in config and config[key] != original.get(key):
            raise ValueError(f"checkpoint resume cannot change {key}; preserve material, boundaries and numerical settings")
    merged = {**original, **deepcopy(config)}
    return merged, saved
