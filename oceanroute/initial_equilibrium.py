"""Verified static material state for the dynamic research integrator.

This is an explicit initialization solve, not a checkpoint importer. The initial
proof is independently rechecked on continuation without rerunning optimization.
"""
from __future__ import annotations

from copy import deepcopy
import math
import numpy as np

from .bathymetry import BathymetryGrid
from .checkpoints import _digest, _array

SCHEMA = "oceanroute.dynamic.initial-equilibrium.v1"
PROVENANCE = "oceanroute.dynamic.initial-equilibrium.provenance.v1"
SOLVER_KEYS = {"force_tolerance_n", "relative_force_tolerance", "contact_tolerance_m",
               "max_solver_iterations", "max_function_evaluations", "max_segment_samples", "max_work_units"}
METHOD = "fresh fixed-end natural-length discrete equilibrium; independent dynamic force/material/chord verification"
MOTION_POLICY = "zero initial node velocity before prescribed actuation; commands start with the first real internal step"
SNAPSHOT_FIELDS = {"positions", "velocities", "rest_lengths_m", "node_material_m", "node_mass_kg",
                   "node_dry_mass_kg", "node_wet_weight_n", "segment_ea_n", "segment_tension_n",
                   "node_contact_normal_force_n", "node_boundary_force_n", "node_force_residual_n"}


def _prepare(project, config, *, continuation=False):
    # Lazy imports keep the static solver's existing numerical helper imports
    # independent of the integrator's module import order.
    from .simulation import _config, _cable_defaults, _environment, _integer, _num, _MaterialModel, _current_profile, G
    from .static_bathymetry import _point
    c = _cable_defaults(project, _config(config))
    raw = c.get("initial_equilibrium")
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise ValueError("initial_equilibrium requires schema "+SCHEMA)
    allowed = {"schema", "vessel_position_m", "anchor_position_m", "natural_length_m", "rest_lengths_m", "initial_positions_m", "solver"}
    if any(not isinstance(key,str) for key in raw):
        raise ValueError("initial_equilibrium keys must be strings")
    if set(raw)-allowed:
        raise ValueError("initial_equilibrium unsupported inputs: "+", ".join(sorted(set(raw)-allowed)))
    if "seabed_grid" not in c or "seabed_profile" in c:
        raise ValueError("initial_equilibrium requires an explicit seabed_grid and no seabed_profile")
    if any(k not in raw for k in ("vessel_position_m", "anchor_position_m")):
        raise ValueError("initial_equilibrium requires actual vessel_position_m and anchor_position_m")
    grid = BathymetryGrid(c["seabed_grid"])
    n = _integer(c, "nodes", 24, 6, 80)
    lengths = [k for k in ("natural_length_m", "rest_lengths_m") if k in raw]
    if len(lengths) != 1:
        raise ValueError("initial_equilibrium requires exactly one natural_length_m or rest_lengths_m")
    if "rest_lengths_m" in raw:
        if not isinstance(raw["rest_lengths_m"], list) or len(raw["rest_lengths_m"]) != n-1:
            raise ValueError("initial_equilibrium rest_lengths_m must contain exactly nodes-1 natural lengths")
        rest = np.array([_num({"v":x}, "v", 1, 1e-4, 1e6) for x in raw["rest_lengths_m"]])
    else:
        rest = np.full(n-1, _num(raw, "natural_length_m", 1, .001, 1e6)/(n-1))
        if rest.min() < 1e-4:
            raise ValueError("initial_equilibrium each natural segment must be at least .0001 m")
    if rest.sum() > 1e6:
        raise ValueError("initial_equilibrium total natural length exceeds 1000000 m")
    e = _environment(c)
    ea = _num(c, "ea_n", 1e8, 100, 1e12)
    ei = _num(c, "ei_n_m2", 0, 0, 1e10)
    added = _num(c, "added_mass_coefficient", 1, 0, 10)
    dry = _num(c, "mass_kg_m", e["weight"]/G+e["rho"]*math.pi*e["diameter"]**2/4, 0, 50000, strict=True)
    if dry*G <= e["weight"]:
        raise ValueError("initial_equilibrium mass_kg_m implies nonpositive displaced volume")
    material = _MaterialModel(c, e, ea, ei, dry, added)
    material.validate_coverage(material.origin+float(rest.sum()))
    local = material.loads(rest)
    active = [r for r in material.rows if r["end_m"] > material.origin and r["start_m"] < material.origin+float(rest.sum())]
    weight, stiffness = active[0]["wet_weight_n_m"], active[0]["ea_n"]
    if any(r["wet_weight_n_m"] != weight or r["ea_n"] != stiffness or r["ei_n_m2"] != 0 for r in active):
        raise ValueError("initial_equilibrium currently requires uniform wet weight/EA and zero EI throughout the initial active material interval")
    if any(np.any(shares) for shares in local["body_weights"]):
        raise ValueError("initial_equilibrium does not support an initially loaded inline body; future undeployed bodies remain allowed")
    if not continuation:
        _current_profile(c, e)  # Validate the actual current-table contract first.
        if np.any(e["current"]):
            raise ValueError("initial_equilibrium currently requires zero initial current")
        for row in c.get("current_profile", []):
            if _num(row, "x_m_s", 0, -20, 20) != 0 or _num(row, "y_m_s", 0, -20, 20) != 0:
                raise ValueError("initial_equilibrium requires every current_profile sample to be zero")
    if c.get("wave_kinematics") is not None:
        raise ValueError("initial_equilibrium does not support wave_kinematics")
    vessel, anchor = _point(raw["vessel_position_m"], "initial_equilibrium.vessel_position_m"), _point(raw["anchor_position_m"], "initial_equilibrium.anchor_position_m")
    floor = grid.surface(np.stack((vessel, anchor)))[0]
    if np.any(np.stack((vessel, anchor))[:,2] > 0) or np.any(np.stack((vessel, anchor))[:,2] < floor-1e-9):
        raise ValueError("initial_equilibrium endpoints must be at/below model sea surface and on/above their actual known seabed")
    if np.linalg.norm(vessel-anchor) < .001:
        raise ValueError("initial_equilibrium endpoints require at least .001 m separation")
    solver = raw.get("solver", {})
    if not isinstance(solver, dict) or any(not isinstance(k,str) for k in solver) or set(solver)-SOLVER_KEYS:
        raise ValueError("initial_equilibrium solver contains unsupported inputs")
    resolved = {
        "force_tolerance_n": _num(solver, "force_tolerance_n", .01, 1e-6, .01),
        "relative_force_tolerance": _num(solver, "relative_force_tolerance", 1e-7, 1e-9, 1e-7),
        "contact_tolerance_m": _num(solver, "contact_tolerance_m", 1e-8, 1e-9, 1e-8),
        "max_solver_iterations": _integer(solver, "max_solver_iterations", 300, 1, 600),
        "max_function_evaluations": _integer(solver, "max_function_evaluations", 2000, 1, 10000),
        "max_segment_samples": _integer(solver, "max_segment_samples", 20000, 10, 200000),
        "max_work_units": _integer(solver, "max_work_units", 200000000, 1, 2000000000)}
    static = {"seabed_grid": deepcopy(c["seabed_grid"]), "vessel_position_m": vessel.tolist(), "anchor_position_m": anchor.tolist(),
              "rest_lengths_m": rest.tolist(), "nodes": n, "wet_weight_n_m": weight, "ea_n": stiffness,
              "contact_policy": "frictionless", "seabed_friction": 0., **resolved}
    if "initial_positions_m" in raw:
        static["initial_positions_m"] = deepcopy(raw["initial_positions_m"])
    variables = 3*(n-2)
    static_work = int(grid.z.size+resolved["max_solver_iterations"]*variables**3+
               resolved["max_function_evaluations"]*(variables**2+4*n)+5*resolved["max_segment_samples"]+20*n)
    mapping_work = int(5*resolved["max_segment_samples"]+3*grid.z.size+3*n*(len(material.rows)+len(material.bodies)+10))
    work = static_work+mapping_work
    components = {"static_solver_estimated_work_units":static_work,"independent_mapping_work_upper_bound":mapping_work}
    if work > resolved["max_work_units"]:
        raise ValueError("initial_equilibrium dense-solver computation exceeds its declared max_work_units")
    return c, raw, grid, material, local, static, work, -float(floor[1]), components


def estimate_initial_equilibrium_work(project: dict, config: dict) -> dict:
    """Validate the material/boundary contract and preflight without optimizing."""
    _, _, _, _, _, static, work, depth, components = _prepare(project, config)
    rest = static["rest_lengths_m"]
    return {"estimated_work_units": work, "max_work_units": static["max_work_units"], "work_components":components,
            "rest_lengths_m": deepcopy(rest), "initial_material_length_m": float(sum(rest)),
            "segment_target_m": max(rest), "vessel_position_m": static["vessel_position_m"],
            "anchor_position_m": static["anchor_position_m"], "reference_depth_m": depth,
            "work_basis": "declared dense static iteration*free_variables^3 + evaluations*(free_variables^2+4N) + final chord sample bounds; normalized units, not FLOPs or CPU time"}


def _verification(grid, positions, rest, local, static):
    from .static_bathymetry import _segment_bed_clearance, _StaticBudget
    length = np.linalg.norm(np.diff(positions, axis=0), axis=1)
    if length.min() < 1e-6:
        raise ValueError("initial_equilibrium mapping has a collapsed physical segment")
    tension = local["ea"]*np.maximum(length/rest-1, 0)
    directions = np.diff(positions, axis=0)/length[:,None]
    segment_force = tension[:,None]*directions
    internal = np.zeros_like(positions)
    internal[:-1] += segment_force; internal[1:] -= segment_force
    external = np.zeros_like(positions); external[:,2] = -local["weight"]
    force = internal+external
    bed, normals = grid.surface(positions)
    gap = positions[:,2]-bed
    # The dynamic contact/mask threshold is stricter than legacy static defaults.
    contact = gap <= 1e-8
    normal_force = np.maximum(-np.sum(force*normals,axis=1), 0)*contact
    normal_force[[0,-1]] = 0
    reaction = normal_force[:,None]*normals
    boundary = np.zeros_like(positions); boundary[[0,-1]] = -force[[0,-1]]
    residual = force+reaction+boundary
    tolerance = max(static["force_tolerance_n"], static["relative_force_tolerance"]*max(1.,float(local["weight"].sum())))
    maxres = float(np.max(np.linalg.norm(residual,axis=1)))
    try:
        chord = _segment_bed_clearance(grid, positions, static["max_segment_samples"])
    except _StaticBudget as error:
        raise ValueError("initial_equilibrium mapping chord verification exceeded its declared sample budget") from error
    complementarity = float(np.max(np.abs(normal_force*gap*normals[:,2])))
    comp_limit = static["contact_tolerance_m"]*max(1., float(normal_force.max()))
    global_balance = np.sum(external+reaction+boundary,axis=0)
    rejected=[]
    if maxres > tolerance: rejected.append("MAPPED_FORCE_BALANCE_NOT_CONVERGED")
    if np.min(gap) < -static["contact_tolerance_m"]: rejected.append("MAPPED_NODE_BED_PENETRATION")
    if chord["minimum_clearance_m"] < -static["contact_tolerance_m"]: rejected.append("MAPPED_STRAIGHT_SEGMENT_BED_INTERSECTION")
    if complementarity > comp_limit: rejected.append("MAPPED_CONTACT_COMPLEMENTARITY_NOT_CONVERGED")
    if np.linalg.norm(global_balance) > (len(rest)-1)*tolerance: rejected.append("MAPPED_GLOBAL_FORCE_BALANCE_NOT_CONVERGED")
    return {"accepted":not rejected,"rejection_codes":rejected,"max_node_force_residual_n":maxres,
            "force_tolerance_n":tolerance,"contact_tolerance_m":static["contact_tolerance_m"],
            "minimum_node_clearance_m":float(gap.min()),"segment_clearance":chord,
            "max_complementarity_n_m":complementarity,"complementarity_tolerance_n_m":comp_limit,
            "total_force_balance_n":global_balance.tolist(),"normal_force_n":normal_force,
            "boundary_force_n":boundary,"residual_force_n":residual,"segment_tension_n":tension}


def resolve_initial_equilibrium(project: dict, config: dict) -> dict:
    """Actually solve and independently verify the same state used by dynamics.

    Returns finite JSON data; preparation can call this, but actual dynamic
    startup still solves again from the raw request rather than trusting it.
    """
    from .static_bathymetry import static_equilibrium
    c, raw, grid, material, local, static, work, depth, components = _prepare(project, config)
    result = static_equilibrium(static)
    if not result["accepted"]:
        raise ValueError("initial_equilibrium static solve was not accepted: "+", ".join(result["solver"]["rejection_codes"]))
    p = np.asarray(result["nodes"],dtype=float); rest = np.asarray(static["rest_lengths_m"])
    proof = _verification(grid,p,rest,local,static)
    if not proof["accepted"]:
        raise ValueError("initial_equilibrium independent mapping was not accepted: "+", ".join(proof["rejection_codes"]))
    if not np.allclose(proof["segment_tension_n"],result["segment_tension_n"],rtol=1e-9,atol=1e-7):
        raise ValueError("initial_equilibrium static/dynamic axial force laws disagree")
    snapshot={"positions":p.tolist(), "velocities":np.zeros_like(p).tolist(), "rest_lengths_m":rest.tolist(),
              "node_material_m":local["coordinates"].tolist(), "node_mass_kg":local["mass"].tolist(),
              "node_dry_mass_kg":local["dry_mass"].tolist(), "node_wet_weight_n":local["weight"].tolist(),
              "segment_ea_n":local["ea"].tolist(), "segment_tension_n":proof["segment_tension_n"].tolist(),
              "node_contact_normal_force_n":proof["normal_force_n"].tolist(),
              "node_boundary_force_n":proof["boundary_force_n"].tolist(), "node_force_residual_n":proof["residual_force_n"].tolist()}
    for name,values in snapshot.items():
        shape=np.asarray(values).shape
        _array(values,shape,"initial_equilibrium."+name)
    publicproof={k:v for k,v in proof.items() if k not in ("normal_force_n","boundary_force_n","residual_force_n","segment_tension_n")}
    provenance={"schema":PROVENANCE,"source":"oceanroute.static_bathymetry.static_equilibrium",
                "validation_status":"research", "method":METHOD,
                "initial_time_s":0., "initial_paid_out_m":0.,"initial_suspended_material_m":material.origin,
                "initial_material_length_m":float(rest.sum()),"segment_target_m":float(rest.max()),
                "request_sha256":_digest(raw),"seabed_grid_sha256":_digest(c["seabed_grid"]),
                "initial_conditions":{"fluid_velocity_m_s":[0.,0.,0.],"frictional_support_force_n":0.,
                                      "motion_policy":MOTION_POLICY},
                "initial_snapshot":snapshot,"verification":publicproof,"solver":deepcopy(result["solver"]),
                "slope_equilibrium":True,"loading_history_reconstructed":False}
    provenance["solver"].update(components)
    provenance["solver"]["estimated_work_units"] = work
    provenance["solver"]["work_basis"] += "; plus independently bounded material/force/chord mapping verification"
    provenance["solver"]["independent_mapping_bathymetry_node_queries"] = int(grid.queried_nodes)
    return {"positions":p.tolist(),"rest_lengths_m":rest.tolist(),"segment_tension_n":proof["segment_tension_n"].tolist(),
            "initial_material_length_m":float(rest.sum()),"segment_target_m":float(rest.max()),"reference_depth_m":depth,
            "provenance":provenance,"solver":deepcopy(provenance["solver"]),"estimated_work_units":work}


def validate_initialization_provenance(provenance: dict, config: dict) -> dict:
    """Reconstruct the original proof; never infer it from current/final frames."""
    c, raw, grid, material, local, static, work, _, components = _prepare({},config,continuation=True)
    required={"schema","source","validation_status","method","initial_time_s","initial_paid_out_m",
              "initial_suspended_material_m","initial_material_length_m","segment_target_m","request_sha256",
              "seabed_grid_sha256","initial_conditions","initial_snapshot","verification","solver",
              "slope_equilibrium","loading_history_reconstructed"}
    if not isinstance(provenance,dict) or set(provenance)!=required:
        raise ValueError("checkpoint initial equilibrium provenance is missing/incompatible")
    if provenance["method"]!=METHOD or provenance["schema"]!=PROVENANCE or provenance["source"]!="oceanroute.static_bathymetry.static_equilibrium" or provenance["validation_status"]!="research" or provenance["slope_equilibrium"] is not True or provenance["loading_history_reconstructed"] is not False:
        raise ValueError("checkpoint initial equilibrium provenance source/model is incompatible")
    if provenance["request_sha256"]!=_digest(raw) or provenance["seabed_grid_sha256"]!=_digest(c["seabed_grid"]):
        raise ValueError("checkpoint initial equilibrium request/grid provenance mismatch")
    rest=np.asarray(static["rest_lengths_m"]);n=len(rest)+1
    expected={"initial_time_s":0.,"initial_paid_out_m":0.,"initial_suspended_material_m":material.origin,
              "initial_material_length_m":float(rest.sum()),"segment_target_m":float(rest.max())}
    if any(provenance[k]!=v for k,v in expected.items()):
        raise ValueError("checkpoint initial equilibrium manufacturing/reference state mismatch")
    cond=provenance["initial_conditions"]
    if cond!={"fluid_velocity_m_s":[0.,0.,0.],"frictional_support_force_n":0.,"motion_policy":MOTION_POLICY}:
        raise ValueError("checkpoint initial equilibrium unsupported initial fluid/friction loading")
    snap=provenance["initial_snapshot"]
    if not isinstance(snap,dict) or set(snap)!=SNAPSHOT_FIELDS:
        raise ValueError("checkpoint initial equilibrium complete initial snapshot is required")
    arrays={}
    for name in SNAPSHOT_FIELDS:
        shape=(n,3) if name in ("positions","velocities","node_boundary_force_n","node_force_residual_n") else ((n-1,) if name in ("rest_lengths_m","segment_ea_n","segment_tension_n") else (n,))
        arrays[name]=_array(snap[name],shape,"initial_equilibrium."+name)
    if np.max(np.abs(arrays["velocities"]))!=0 or not np.array_equal(arrays["rest_lengths_m"],rest):
        raise ValueError("checkpoint initial equilibrium initial velocity/natural lengths mismatch")
    if not np.allclose(arrays["positions"][[0,-1]],np.array([static["vessel_position_m"],static["anchor_position_m"]]),rtol=0,atol=1e-9):
        raise ValueError("checkpoint initial equilibrium fixed boundaries mismatch")
    for key,actual in (("node_material_m",local["coordinates"]),("node_mass_kg",local["mass"]),("node_dry_mass_kg",local["dry_mass"]),("node_wet_weight_n",local["weight"]),("segment_ea_n",local["ea"])):
        if not np.allclose(arrays[key],actual,rtol=1e-10,atol=1e-8):
            raise ValueError("checkpoint initial equilibrium material proof mismatch: "+key)
    proof=_verification(grid,arrays["positions"],rest,local,static)
    if not proof["accepted"]:
        raise ValueError("checkpoint initial equilibrium force/geometry proof failed: "+", ".join(proof["rejection_codes"]))
    for key,actual in (("segment_tension_n",proof["segment_tension_n"]),("node_contact_normal_force_n",proof["normal_force_n"]),("node_boundary_force_n",proof["boundary_force_n"]),("node_force_residual_n",proof["residual_force_n"])):
        if not np.allclose(arrays[key],actual,rtol=1e-9,atol=1e-7):
            raise ValueError("checkpoint initial equilibrium force evidence mismatch: "+key)
    recorded=provenance["verification"]
    publicproof={k:v for k,v in proof.items() if k not in ("normal_force_n","boundary_force_n","residual_force_n","segment_tension_n")}
    if not isinstance(recorded,dict) or set(recorded)!=set(publicproof) or _digest(recorded)!=_digest(publicproof):
        raise ValueError("checkpoint initial equilibrium acceptance/tolerance evidence mismatch")
    solver=provenance["solver"]
    if not isinstance(solver,dict) or solver.get("accepted") is not True or solver.get("converged") is not True or solver.get("estimated_work_units")!=work:
        raise ValueError("checkpoint initial equilibrium solver provenance mismatch")

    from .simulation import _integer
    if any(solver.get(k)!=v for k,v in components.items()) or solver.get("max_work_units")!=static["max_work_units"] or solver.get("rejection_codes")!=[]:
        raise ValueError("checkpoint initial equilibrium work/cap provenance mismatch")
    physical_diagnostics={"max_node_force_residual_n":proof["max_node_force_residual_n"],
        "force_tolerance_n":proof["force_tolerance_n"],"max_penetration_m":max(0.,-proof["minimum_node_clearance_m"]),
        "contact_tolerance_m":static["contact_tolerance_m"],"max_complementarity_n_m":proof["max_complementarity_n_m"],
        "complementarity_tolerance_n_m":proof["complementarity_tolerance_n_m"],
        "global_force_balance_residual_n":float(np.linalg.norm(proof["total_force_balance_n"])),
        "global_force_tolerance_n":(n-2)*proof["force_tolerance_n"]}
    from .simulation import _num
    for key,expected in physical_diagnostics.items():
        actual=_num(solver,key,-1.,0,1e15)
        if not math.isclose(actual,expected,rel_tol=1e-8,abs_tol=1e-7):
            raise ValueError("checkpoint initial equilibrium solver force diagnostic mismatch: "+key)
    _integer(solver,"iterations",0,0,static["max_solver_iterations"])
    _integer(solver,"function_evaluations",0,0,static["max_function_evaluations"])
    return {"estimated_work_units":components["independent_mapping_work_upper_bound"],
            "actual_bathymetry_node_queries":int(grid.queried_nodes),
            "actual_segment_samples":proof["segment_clearance"]["samples"]}
