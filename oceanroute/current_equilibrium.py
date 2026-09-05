"""Bounded nonconservative stationary cable/point equilibrium on actual 2D bed.

Positions and normal reactions are solved together. Drag is recomputed at
every candidate; no drag potential or frozen-fluid equilibrium is substituted.
"""
from __future__ import annotations

import math
import numpy as np
from scipy.optimize import least_squares

from .bathymetry import BathymetryGrid
from .hydrodynamics import HydrodynamicField, _array
from .static_bathymetry import _point, _energy_forces, _segment_bed_clearance, _StaticBudget
from .simulation import _base, _config, _num, _integer, _warning

_ALLOWED = {"seabed_grid", "vessel_position_m", "anchor_position_m", "natural_length_m",
            "rest_lengths_m", "nodes", "wet_weight_n_m", "ea_n", "initial_positions_m",
            "contact_policy", "seabed_friction", "sticking_nodes", "force_tolerance_n",
            "relative_force_tolerance", "contact_tolerance_m", "max_solver_iterations",
            "max_function_evaluations", "max_segment_samples", "max_work_units"}
_WORK_BASIS = ("grid samples + bounded 4NF dense Jacobian factorizations times variables cubed "
               "+ every force/finite-difference evaluation times (variables squared + 32N + 500) "
               "+ 5 times bounded whole-chord samples + final nodal verification; normalized charged units, not FLOPs/CPU time")


def _parameters(static):
    c = _config(static)
    if any(not isinstance(k, str) for k in c) or set(c)-_ALLOWED:
        raise ValueError("current_equilibrium unsupported static input fields")
    if any(key not in c for key in ("seabed_grid", "vessel_position_m", "anchor_position_m")):
        raise ValueError("current_equilibrium requires explicit grid/vessel/anchor")
    if c.get("contact_policy", "frictionless") != "frictionless" or c.get("sticking_nodes", []) != []:
        raise ValueError("current_equilibrium supports frictionless free-node contact, not prescribed sticking history")
    if _num(c, "seabed_friction", 0, 0, 2) != 0:
        raise ValueError("current_equilibrium static seabed_friction must be zero")
    grid = BathymetryGrid(c["seabed_grid"])
    vessel, anchor = _point(c["vessel_position_m"], "vessel_position_m"), _point(c["anchor_position_m"], "anchor_position_m")
    for point in (vessel, anchor):
        if point[2] > 0 or point[2] < -12000:
            raise ValueError("current fixed-end heights must lie between model sea zero and -12000 m")
        floor = grid.surface(point[None, :])[0][0]
        if point[2] < floor-1e-9:
            raise ValueError("current fixed endpoint is below its known seabed")
    if np.linalg.norm(vessel-anchor) < .001:
        raise ValueError("current fixed endpoints require .001 m separation")
    if ("rest_lengths_m" in c)+( "natural_length_m" in c) != 1:
        raise ValueError("provide exactly one natural_length_m or rest_lengths_m")
    if "rest_lengths_m" in c:
        raw = c["rest_lengths_m"]
        if not isinstance(raw, list) or not 5 <= len(raw) <= 79:
            raise ValueError("rest_lengths_m requires 5 to 79 natural segments")
        rest = np.array([_num({"v":x}, "v", 1, 1e-4, 1e6) for x in raw])
        n = len(rest)+1
        if "nodes" in c and _integer(c, "nodes", n, 6, 80) != n:
            raise ValueError("nodes must match rest_lengths_m")
    else:
        n = _integer(c, "nodes", 24, 6, 80)
        rest = np.full(n-1, _num(c, "natural_length_m", 1, .001, 1e6)/(n-1))
    if np.min(rest) < 1e-4 or rest.sum() > 1e6:
        raise ValueError("current natural lengths exceed the bounded material domain")
    # Scalar declarations remain valid syntax, but supplied arrays determine loads.
    _num(c, "wet_weight_n_m", 4, 1e-6, 20000)
    _num(c, "ea_n", 1e6, 100, 1e12)
    absolute = _num(c, "force_tolerance_n", .01, 1e-6, 1)
    relative = _num(c, "relative_force_tolerance", 1e-5, 1e-9, 1e-3)
    contact = _num(c, "contact_tolerance_m", 1e-6, 1e-9, 1e-4)
    iterations = _integer(c, "max_solver_iterations", 300, 1, 600)
    evaluations = _integer(c, "max_function_evaluations", 2000, 1, 10000)
    samples = _integer(c, "max_segment_samples", 20000, 10, 200000)
    maximum = _integer(c, "max_work_units", 200000000, 1, 2000000000)
    variables = 4*(n-2)
    components = {"grid_parse": int(grid.z.size), "solver_dense": int(iterations*variables**3),
                  "residual_evaluations": int(evaluations*(variables**2+32*n+500)),
                  "final_verification": int(5*samples+40*n+500)}
    estimated = int(sum(components.values()))
    if estimated > maximum:
        raise ValueError("current_equilibrium declared 4NF dense-solver computation exceeds max_work_units; explicitly reduce caps/nodes or declare a sufficient budget")
    budget = {"estimated_work_units": estimated, "max_work_units": maximum, "components": components,
              "variables": variables, "max_solver_iterations": iterations,
              "max_function_evaluations": evaluations, "max_segment_samples": samples,
              "work_basis": _WORK_BASIS}
    return c, grid, vessel, anchor, rest, absolute, relative, contact, budget


def estimate_current_equilibrium_work(static: dict) -> dict:
    """Strict pure preflight; estimates 4NF and all finite-difference calls.

    The cap is not silently enlarged. A 24-node default with 300 dense iterations
    exceeds 200M; explicitly reduce iterations or increase declared work.
    """
    return _parameters(static)[-1]


def solve_current_equilibrium(static: dict, *, segment_ea_n, node_wet_weight_n,
                              cable_drag_factor, body_drag_factor,
                              initial_fluid, load_scale_n) -> dict:
    """True zero-velocity fixed-end balance with p-dependent steady fluid drag.

    Coefficients are independently resolved natural material/point values. This
    internal core does not interpret an accepted-result JSON as material input.
    """
    c, grid, vessel, anchor, rest, absolute, relative, contact_tolerance, budget = _parameters(static)
    n, nf = len(rest)+1, len(rest)-1
    ea = _array(segment_ea_n, (n-1,), "current segment EA", True)
    if np.any(ea < 100) or np.any(ea > 1e12*(1+1e-10)):
        raise ValueError("current segment EA exceeds declared finite material stiffness")
    weight = _array(node_wet_weight_n, (n,), "current signed nodal wet weight")
    cable = _array(cable_drag_factor, (n,), "current cable drag factor", True)
    body = _array(body_drag_factor, (n,), "current body drag factor", True)
    load_scale = _num({"v":load_scale_n}, "v", 1, 0, 2.2e10)
    if load_scale+1e-7 < float(np.sum(np.abs(weight))):
        raise ValueError("current absolute load scale hides signed load cancellation")
    field = HydrodynamicField(initial_fluid)
    fluid = field.fluid
    rows = fluid["current_profile"]
    velocities = ([fluid["current_m_s"]] if rows is None else [[r["x_m_s"], r["y_m_s"], 0] for r in rows])
    speed_squared = max(float(np.dot(u, u)) for u in velocities)
    with np.errstate(over="ignore", invalid="ignore"):
        drag_bound = float(speed_squared*(np.sum(cable)+np.sum(body)))
    if not math.isfinite(drag_bound) or drag_bound > 1e18:
        raise ValueError("current drag bound exceeds the explicit 1e18 N finite force-scale domain")
    force_scale = max(1., load_scale+drag_bound)
    force_tolerance = max(absolute, relative*force_scale)
    natural = float(np.sum(rest))
    scale = max(natural, float(np.linalg.norm(vessel-anchor)), .001)
    material = np.r_[np.cumsum(rest[::-1])[::-1], 0.]
    fractions = (natural-material)/natural
    positions = vessel+fractions[:, None]*(anchor-vessel)
    kind = "straight fixed-end feasible numerical seed; not equilibrium"
    if "initial_positions_m" in c:
        raw = c["initial_positions_m"]
        if not isinstance(raw, list) or len(raw) != n:
            raise ValueError("current initial_positions_m must contain exactly nodes positions")
        positions = np.array([_point(p, "initial_positions_m") for p in raw])
        if not np.allclose(positions[[0,-1]], np.stack((vessel,anchor)), rtol=0, atol=1e-9):
            raise ValueError("current numerical seed endpoints differ from fixed boundaries")
        kind = "user supplied feasible numerical seed; not equilibrium"
    positions[0], positions[-1] = vessel, anchor
    seed_bed = grid.surface(positions)[0]
    if "initial_positions_m" not in c:
        positions[1:-1,2] = np.maximum(positions[1:-1,2], seed_bed[1:-1])
    elif np.min(positions[:,2]-seed_bed) < -contact_tolerance:
        raise ValueError("current numerical seed penetrates the known seabed")
    if np.any(positions[:,2] > 0) or np.any(positions[:,2] < -12000):
        raise ValueError("current numerical seed exceeds the model vertical domain")
    local = {"drag": cable, "body_drag": body}
    zero_velocity = np.zeros_like(positions)
    # This is a real candidate evaluation, never a no-current auxiliary solve.
    initial_hydro = field.loads(positions, zero_velocity, local)
    initial_physical = _energy_forces(positions, rest, ea, weight)
    initial_force = initial_physical[1]+initial_hydro["node_total_drag_force_n"]
    _, initial_normal = grid.surface(positions)
    normal_seed = np.where(positions[1:-1,2]-seed_bed[1:-1] <= contact_tolerance,
                           np.maximum(-np.sum(initial_force[1:-1]*initial_normal[1:-1],axis=1), 0), 0)
    x0 = np.r_[((positions[1:-1]-vessel)/scale).ravel(), normal_seed/force_scale]
    lower_position = (np.array([grid.x[0],grid.y[0],-12000.])-vessel)/scale
    upper_position = (np.array([grid.x[-1],grid.y[-1],0.])-vessel)/scale
    # FB itself enforces a,b>=0 at a root. Leaving intermediate reaction
    # iterates signed avoids the strictly-interior TRF lower-bound stagnation
    # at exactly zero off-bed reaction; final physical reactions are rechecked.
    lower = np.r_[np.tile(lower_position,nf),np.full(nf,-np.inf)]
    upper = np.r_[np.tile(upper_position,nf),np.full(nf,np.inf)]
    evaluations, jacobians, fd_evaluations = 1, 0, 0
    initial_reaction = np.r_[0.,normal_seed,0.]
    initial_gap = positions[:,2]-seed_bed
    a0,b0 = initial_gap[1:-1]/scale,normal_seed/force_scale
    r0 = np.r_[((initial_force+initial_reaction[:,None]*initial_normal)[1:-1]/force_scale).ravel(),
               np.hypot(a0,b0)-a0-b0]
    seed_state = (x0.copy(),positions.copy(),seed_bed.copy(),initial_normal.copy(),initial_physical,
                  initial_hydro,initial_force,initial_reaction,initial_gap,r0)
    invalid_candidates, cache, best = 0, seed_state, (float(np.dot(r0,r0)),seed_state)
    template = positions.copy()
    evaluation_units = budget["variables"]**2+32*n+500
    def actual_work(final_samples=0):
        return int(grid.z.size+jacobians*budget["variables"]**3+evaluations*evaluation_units+5*final_samples+40*n+500)
    def evaluate(x, finite_difference=False):
        nonlocal evaluations, fd_evaluations, cache, best, invalid_candidates
        if cache is not None and np.array_equal(cache[0],x):
            return cache
        if evaluations >= budget["max_function_evaluations"] or actual_work()+evaluation_units > budget["max_work_units"]:
            raise _StaticBudget("current optimizer exhausted actual force/finite-difference evaluation budget")
        evaluations += 1
        if finite_difference:
            fd_evaluations += 1
        p = template.copy()
        p[1:-1] = vessel+np.asarray(x[:3*nf]).reshape(nf,3)*scale
        try:
            bed, normals = grid.surface(p)
            physical = _energy_forces(p,rest,ea,weight)
            hydro = field.loads(p,zero_velocity,local)
        except ValueError:
            invalid_candidates += 1
            raise
        force = physical[1]+hydro["node_total_drag_force_n"]
        normal_force = np.r_[0.,np.asarray(x[3*nf:])*force_scale,0.]
        gap = p[:,2]-bed
        balanced = force+normal_force[:,None]*normals
        a,b = gap[1:-1]/scale, normal_force[1:-1]/force_scale
        complementarity = np.hypot(a,b)-a-b
        residual = np.r_[(balanced[1:-1]/force_scale).ravel(),complementarity]
        if not np.isfinite(residual).all():
            raise ValueError("current equilibrium candidate produced nonfinite residual")
        st = (np.array(x).copy(),p,bed,normals,physical,hydro,force,normal_force,gap,residual)
        cache = st
        merit = float(np.dot(residual,residual))
        if not finite_difference and (best is None or merit < best[0]):
            best = (merit,st)
        return st
    def residual(x):
        return evaluate(x)[-1]
    def jacobian(x):
        nonlocal jacobians
        if jacobians >= budget["max_solver_iterations"]:
            raise _StaticBudget("current optimizer exhausted dense Jacobian/iteration budget")
        if actual_work()+budget["variables"]**3 > budget["max_work_units"]:
            raise _StaticBudget("current optimizer exhausted normalized work budget")
        jacobians += 1
        st = evaluate(x)
        baseline = st[-1].copy()
        matrix = np.zeros((4*nf,4*nf))
        for j in range(3*nf):
            step = 1e-7*max(1.,abs(float(x[j])))
            if x[j]+step >= upper[j]:
                step = -step
            if x[j]+step <= lower[j]:
                step = (upper[j]-lower[j])*1e-7
            y = np.array(x).copy()
            y[j] += step
            matrix[:,j] = (evaluate(y,True)[-1]-baseline)/step
        # Normal-reaction columns are exact; geometric/flow dependence remains
        # in the position finite differences, including real bilinear normals.
        a,b = st[8][1:-1]/scale, st[7][1:-1]/force_scale
        radius = np.hypot(a,b)
        derivative = np.divide(b,radius,out=np.zeros_like(b),where=radius>0)-1
        for j in range(nf):
            matrix[3*j:3*j+3,3*nf+j] = st[3][j+1]
            matrix[3*nf+j,3*nf+j] = derivative[j]
        return matrix
    converged, message, budget_exhausted, invalid_geometry = False,"",False,False
    optimum = None
    # Reserve final verification even on failed solves. At least one real
    # residual+Jacobian solve is attempted; an exact seed is not trusted blindly.
    try:
        evaluate(x0)
        optimum = least_squares(residual,x0,jac=jacobian,bounds=(lower,upper),method="trf",
                                x_scale="jac",ftol=1e-13,xtol=1e-13,gtol=1e-13,
                                max_nfev=budget["max_function_evaluations"])
        st = evaluate(optimum.x)
        converged, message = bool(optimum.success),str(optimum.message)
    except _StaticBudget as error:
        budget_exhausted,message = True,str(error)
        st = best[1]
    except ValueError as error:
        # Invalid raw seeds were checked before entering this try. An invalid
        # trial is a failed local search, never a replacement flat/no-flow bed.
        invalid_geometry,message = True,str(error)
        st = best[1] if best is not None else None
    if st is None:
        raise ValueError("current solver has no finite covered candidate")
    positions,raw_normal_force = st[1].copy(),st[7].copy()
    normal_force = np.maximum(raw_normal_force,0.)
    # Independent final physical evaluation, separate from cached optimizer
    # residual. Normal forces must come from the actual solved unknowns.
    bed,normals = grid.surface(positions)
    physical = _energy_forces(positions,rest,ea,weight)
    _,unused,tension,internal,gravity,elastic,potential,lengths = physical
    hydro = field.loads(positions,zero_velocity,local)
    external = gravity+hydro["node_total_drag_force_n"]
    physical_force_scale = max(1.,load_scale+float(np.sum(np.linalg.norm(hydro["node_cable_drag_force_n"],axis=1)))+
                               float(np.sum(np.linalg.norm(hydro["node_body_drag_force_n"],axis=1))))
    force_tolerance = max(absolute,relative*physical_force_scale)
    force = internal+external
    gap = positions[:,2]-bed
    contact_force = normal_force[:,None]*normals
    boundary = np.zeros_like(positions)
    boundary[[0,-1]] = -force[[0,-1]]
    force_residual = force+contact_force+boundary
    maximum_residual = float(np.max(np.linalg.norm(force_residual,axis=1)))
    penetration = max(0.,-float(np.min(gap)))
    complementarity = float(np.max(np.abs(normal_force*gap*normals[:,2])))
    complementarity_tolerance = contact_tolerance*max(1.,float(np.max(normal_force)))
    total_balance = np.sum(external+contact_force+boundary,axis=0)
    global_residual = float(np.linalg.norm(total_balance))
    global_tolerance = nf*force_tolerance
    segment = None
    try:
        segment = _segment_bed_clearance(grid,positions,budget["max_segment_samples"])
    except _StaticBudget as error:
        budget_exhausted,message = True,message+"; "+str(error)
    except ValueError as error:
        invalid_geometry,message = True,message+"; "+str(error)
    samples = segment["samples"] if segment is not None else budget["max_segment_samples"]
    work = actual_work(samples)
    if work > budget["max_work_units"]:
        budget_exhausted = True
    rejected = []
    if not converged: rejected.append("OPTIMIZER_NOT_CONVERGED")
    if budget_exhausted: rejected.append("COMPUTATION_BUDGET_EXHAUSTED")
    if invalid_geometry: rejected.append("INVALID_CANDIDATE_COVERAGE_OR_TANGENT")
    if maximum_residual > force_tolerance: rejected.append("FORCE_BALANCE_NOT_CONVERGED")
    if global_residual > global_tolerance: rejected.append("GLOBAL_FORCE_BALANCE_NOT_CONVERGED")
    if complementarity > complementarity_tolerance: rejected.append("CONTACT_COMPLEMENTARITY_NOT_CONVERGED")
    if penetration > contact_tolerance: rejected.append("NODE_BED_PENETRATION")
    if np.min(raw_normal_force) < -force_tolerance: rejected.append("NEGATIVE_NORMAL_REACTION")
    if segment is None or segment["minimum_clearance_m"] < -contact_tolerance:
        rejected.append("STRAIGHT_SEGMENT_BED_INTERSECTION_OR_UNKNOWN")
    if np.min(lengths) < 1e-6: rejected.append("COLLAPSED_SEGMENT_UNSUPPORTED")
    # Nonzero reaction away from contact is unacceptable independently of the
    # scaled optimizer stopping criterion (and especially at force-scale extremes).
    if np.any((gap>contact_tolerance)&(normal_force>force_tolerance)):
        rejected.append("CONTACT_REACTION_AWAY_FROM_BED")
    accepted = not rejected
    contact = gap <= contact_tolerance
    node_tension = np.r_[tension[0],(tension[:-1]+tension[1:])/2,tension[-1]]
    result = _base("2d-seabed-material-current-tension-only-static-v1",[
        "Both endpoints and each natural material segment are fixed; actual segment EA, signed nodal cable/point wet load and resolved drag coefficients are used.",
        "Steady horizontal constant/depth-profile current, zero node velocity; node-index secant normal cable drag and isotropic node-lumped point drag are recomputed at every candidate.",
        "Free-node frictionless unilateral contact on the actual bilinear grid; no inferred sticking history, bed flattening or unknown-height fill.",
        "Nonconservative force/complementarity equations are solved locally; uniqueness, stability and global energy minimization are not asserted.",
        "Straight-chord geometry is checked over every crossed bilinear cell; continuous rod/body rotation, bending, continuous entity collision and moving quasi-steady boundaries are absent.",
        "Endpoint support includes endpoint gravity and hydrodynamic load; it is separate from segment tension or seabed friction."])
    if not accepted:
        result["warnings"].append(_warning("CURRENT_EQUILIBRIUM_NOT_ACCEPTED","The finite candidate failed actual force/contact/coverage/budget verification and cannot initialize a balanced state.","error"))
    if np.max(tension/ea) > .05:
        result["warnings"].append(_warning("LARGE_LINEAR_AXIAL_STRAIN","The linear tensile law exceeds 5% strain; material validity is not established."))
    result.update({"accepted":bool(accepted),"seabed":grid.metadata(),"initial_fluid":fluid,
        "nodes":positions.tolist(),"rest_lengths_m":rest.tolist(),"node_material_m":material.tolist(),
        "segment_ea_n":ea.tolist(),"segment_tension_n":tension.tolist(),"node_tension_n":node_tension.tolist(),
        "node_wet_weight_n":weight.tolist(),"node_internal_force_n":internal.tolist(),"node_external_force_n":external.tolist(),
        "node_contact_force_n":contact_force.tolist(),"node_contact_normal_force_n":normal_force.tolist(),
        "node_contact_friction_force_n":np.zeros_like(positions).tolist(),
        "node_required_stick_normal_force_n":np.zeros(n).tolist(),"node_required_stick_friction_force_n":np.zeros_like(positions).tolist(),
        "node_boundary_force_n":boundary.tolist(),"node_force_residual_n":force_residual.tolist(),
        "node_seabed_z_m":bed.tolist(),"node_seabed_normal":normals.tolist(),"node_clearance_m":gap.tolist(),"node_contact_mask":contact.tolist(),
        "node_cable_drag_factor":cable.tolist(),"node_body_drag_factor":body.tolist(),
        **{key:value.tolist() for key,value in hydro.items()},
        "frames":[{"time_s":0.,"ship":vessel.tolist(),"nodes":positions.tolist(),"node_material_m":material.tolist(),
                   "node_contact_mask":contact.tolist(),"top_tension_n":float(tension[0]),"bottom_tension_n":float(tension[-1])}],
        "initialization":{"method":kind,"physical_equilibrium_claim":False,"auxiliary_no_current_solves":0},
        "boundary":{"vessel_position_m":vessel.tolist(),"anchor_position_m":anchor.tolist(),"contact_policy":"frictionless","seabed_friction":0.,
                    "sticking_nodes":[],"boundary_forces_include_endpoint_half_segment_weight":True,"boundary_forces_include_endpoint_drag":True},
        "end_forces_on_cable_n":{"vessel":boundary[0].tolist(),"anchor":boundary[-1].tolist()},"stick_feasibility":[],"segment_clearance":segment,
        "summary":{"natural_length_m":natural,"geometric_chord_length_m":float(np.sum(lengths)),"wet_weight_total_n":float(np.sum(weight)),
                   "absolute_load_scale_n":load_scale,"hydrodynamic_force_bound_n":drag_bound,
                   "top_tension_n":float(tension[0]),"bottom_tension_n":float(tension[-1]),"max_tension_n":float(np.max(tension)),
                   "maximum_axial_strain":float(np.max(tension/ea)),"contact_nodes":int(np.sum(contact)),"max_normal_contact_force_n":float(np.max(normal_force)),
                   "axial_elastic_energy_j":elastic,"submerged_gravity_potential_j":potential,"drag_potential_exists":False,
                   "total_force_balance_n":total_balance.tolist(),"positive_nodal_wet_load_n":float(np.sum(np.maximum(weight,0))),
                   "negative_nodal_wet_load_n":float(np.sum(np.minimum(weight,0)))},
        "solver":{"converged":converged,"accepted":bool(accepted),"method":"bounded nonconservative force plus Fischer-Burmeister normal-contact least squares; per-candidate actual flow/tangent/bed",
                  "iterations":jacobians,"function_evaluations":evaluations,"finite_difference_evaluations":fd_evaluations,"jacobian_evaluations":jacobians,
                  "invalid_candidate_evaluations":invalid_candidates,"message":message,"rejection_codes":rejected,
                  "max_node_force_residual_n":maximum_residual,"force_tolerance_n":force_tolerance,
                  "max_penetration_m":penetration,"contact_tolerance_m":contact_tolerance,
                  "max_complementarity_n_m":complementarity,"complementarity_tolerance_n_m":complementarity_tolerance,
                  "global_force_balance_residual_n":global_residual,"global_force_tolerance_n":global_tolerance,
                  "absolute_load_scale_n":load_scale,"force_scale_n":physical_force_scale,"optimization_force_scale_n":force_scale,"hydrodynamic_force_bound_n":drag_bound,
                  **budget,"actual_work_units":work,"charged_work_units":work,"bathymetry_node_queries":int(grid.queried_nodes),
                  "final_verification_evaluations":1,"raw_minimum_normal_reaction_n":float(np.min(raw_normal_force)),
                  "fluid_profile_rows_parsed":field.profile_rows,"minimum_tangent_secant_m":1e-10}})
    return result
