"""Independent, explicitly bounded static cable research on local bathymetry.

This module does not change the existing laying initialization or its guards.
Submerged weight is per unstretched material metre; all heights are relative
to model sea surface z=0. See docs/STATIC_BATHYMETRY_NOTES.md.
"""
from __future__ import annotations

import math
from copy import deepcopy

import numpy as np
from numpy.polynomial.legendre import leggauss
from scipy.optimize import brentq, minimize

from .bathymetry import BathymetryGrid
from .simulation import _base, _config, _heading, _integer, _num, _warning


_GAUSS_X, _GAUSS_W = leggauss(12)
_GAUSS_T, _GAUSS_WEIGHTS = (_GAUSS_X + 1) / 2, _GAUSS_W / 2


def _point(value, name):
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{name} must be [east_m, north_m, z_m]")
    return np.array([_num({"v": v}, "v", 0, -1e7, 1e7) for v in value])


def _affine_plane(grid, tolerance):
    """Fit and verify every grid sample, with scaled coordinates for stability."""
    if not np.all(np.isfinite(grid.z)):
        raise ValueError("slope_catenary requires a complete affine plane; NoData is not filled")
    xx, yy = np.meshgrid(grid.x, grid.y)
    center = np.array([np.mean(grid.x), np.mean(grid.y)])
    scales = np.array([np.ptp(grid.x), np.ptp(grid.y)])
    matrix = np.column_stack(((xx.ravel()-center[0])/scales[0],
                              (yy.ravel()-center[1])/scales[1], np.ones(xx.size)))
    fitted, _, rank, _ = np.linalg.lstsq(matrix, grid.z.ravel(), rcond=None)
    if rank != 3:
        raise ValueError("seabed plane is not identifiable from the declared grid")
    error = float(np.max(np.abs(matrix@fitted-grid.z.ravel())))
    if error > tolerance:
        raise ValueError("slope_catenary requires an affine plane across all grid nodes; use an explicitly bounded 2D static model")
    return center, float(fitted[2]), fitted[:2]/scales, error


def _asinh_difference(first, increment):
    """Stable asinh(first+increment)-asinh(first), without small subtraction."""
    last = first+increment
    rf, rl = np.hypot(1., first), np.hypot(1., last)
    sinh_difference = increment*(rf-first*(last+first)/(rl+rf))
    return np.arcsinh(sinh_difference)


def _plane_curve(material, horizontal, vertical_bottom, weight, inverse_ea):
    s = np.asarray(material, dtype=float)
    vertical = vertical_bottom+weight*s
    bottom = math.hypot(horizontal, vertical_bottom)
    tension = np.hypot(horizontal, vertical)
    difference = _asinh_difference(vertical_bottom/horizontal, weight*s/horizontal)
    along = horizontal/weight*difference+horizontal*inverse_ea*s
    # Rationalized T-T0 remains accurate when a high bottom tension is specified.
    height = s*(vertical+vertical_bottom)/(tension+bottom)
    height += inverse_ea*(vertical_bottom*s+.5*weight*s*s)
    return along, height, tension, vertical


def _normal_rise(length, horizontal, vertical_bottom, weight, inverse_ea):
    """z(s)-m*x(s): strictly increasing for s>0 from a tangent touchdown."""
    if weight*length/horizontal < .02:
        samples = length*_GAUSS_T
        tension = np.hypot(horizontal, vertical_bottom+weight*samples)
        # Integral of w*s/T, evaluated where the algebraic expression cancels.
        return float(weight*length*length*np.dot(_GAUSS_WEIGHTS, _GAUSS_T/tension)
                     + .5*weight*inverse_ea*length*length)
    x, z, _, _ = _plane_curve(length, horizontal, vertical_bottom, weight, inverse_ea)
    return float(z-(vertical_bottom/horizontal)*x)


def _stretched_length(length, horizontal, vertical_bottom, weight, inverse_ea):
    if inverse_ea == 0:
        return length
    if weight*length/horizontal < .02:
        mean_t = np.dot(_GAUSS_WEIGHTS, np.hypot(horizontal, vertical_bottom+weight*length*_GAUSS_T))
        return float(length*(1+inverse_ea*mean_t))
    end = vertical_bottom+weight*length
    def primitive(v):
        return .5*(v*math.hypot(horizontal, v)+horizontal**2*math.asinh(v/horizontal))
    return float(length+inverse_ea*(primitive(end)-primitive(vertical_bottom))/weight)


def slope_catenary(config: dict) -> dict:
    """Elastic suspended cable tangent to a verified affine 2D seabed plane.

    The touchdown *total tension magnitude* and the horizontal direction toward
    the vessel are boundary conditions. No seabed tail/friction is reconstructed.
    ``ea_n=None`` explicitly requests the inextensible limit.
    """
    c = _config(config)
    if any(not isinstance(k, str) for k in c):
        raise ValueError("slope_catenary config keys must be strings")
    allowed = {"seabed_grid", "vessel_position_m", "heading_deg", "bottom_tension_n",
               "wet_weight_n_m", "ea_n", "nodes", "plane_tolerance_m",
               "max_natural_length_m", "max_root_iterations", "max_work_units"}
    if set(c)-allowed:
        raise ValueError("slope_catenary unsupported inputs: "+", ".join(sorted(set(c)-allowed)))
    if "seabed_grid" not in c:
        raise ValueError("slope_catenary requires an explicit canonical seabed_grid")
    grid = BathymetryGrid(c["seabed_grid"])
    vessel = _point(c.get("vessel_position_m", [0., 0., 0.]), "vessel_position_m")
    if vessel[2] > 0:
        raise ValueError("vessel_position_m.z must not exceed model sea surface zero")
    heading = _num(c, "heading_deg", 90, -36000, 36000)
    bottom = _num(c, "bottom_tension_n", 1000, 1e-6, 1e9)
    weight = _num(c, "wet_weight_n_m", 4, 1e-6, 20000)
    ea = None if c.get("ea_n", 1e8) is None else _num(c, "ea_n", 1e8, 100, 1e12)
    inverse_ea = 0. if ea is None else 1/ea
    n = _integer(c, "nodes", 64, 3, 1000)
    tolerance = _num(c, "plane_tolerance_m", 1e-7, 1e-10, 1e-5)
    maximum_length = _num(c, "max_natural_length_m", 1e6, .001, 1e6)
    maximum_iterations = _integer(c, "max_root_iterations", 100, 1, 200)
    budget = _integer(c, "max_work_units", 250000, 1, 1000000)
    # Each root evaluation uses at most twelve scalar quadrature samples.
    estimated_work = int(grid.z.size+12*(maximum_iterations+4)+8*n+24)
    if estimated_work > budget:
        raise ValueError("slope_catenary declared computation exceeds max_work_units")
    center, reference_z, gradient, plane_error = _affine_plane(grid, tolerance)
    vessel_bed, _ = grid.evaluate(vessel[None, :])
    fitted_bed = reference_z+float(gradient@(vessel[:2]-center))
    depth_above_bed = float(vessel[2]-fitted_bed)
    if depth_above_bed < .001 or vessel[2] <= vessel_bed[0]:
        raise ValueError("vessel must be at least .001 m above its known seabed")
    direction = _heading(heading)
    grade = float(gradient@direction[:2])
    horizontal = bottom/math.sqrt(1+grade*grade)
    vertical_bottom = horizontal*grade
    evaluations = 0
    def residual(s):
        nonlocal evaluations
        evaluations += 1
        if evaluations > maximum_iterations+4:
            raise ValueError("slope_catenary exhausted its root evaluation budget")
        return _normal_rise(s, horizontal, vertical_bottom, weight, inverse_ea)-depth_above_bed
    upper_residual = residual(maximum_length)
    reach_roundoff_tolerance = 32*np.finfo(float).eps*max(1., depth_above_bed, abs(upper_residual+depth_above_bed))
    at_length_limit = abs(upper_residual) <= reach_roundoff_tolerance
    if upper_residual < -reach_roundoff_tolerance:
        raise ValueError("slope_catenary cannot reach the vessel within max_natural_length_m")
    if at_length_limit:
        # Some scipy versions leave RootResults.iterations undefined when a
        # bracket endpoint is already a root. Do not expose that memory value.
        length, root_converged, root_iterations = maximum_length, True, 0
    else:
        try:
            length, solved = brentq(residual, 0., maximum_length, maxiter=maximum_iterations,
                                    xtol=1e-10, rtol=4*np.finfo(float).eps, full_output=True)
            root_converged, root_iterations = bool(solved.converged), int(solved.iterations)
        except (RuntimeError, ValueError) as error:
            raise ValueError("slope_catenary root did not converge within its declared budget") from error
    material = np.linspace(length, 0., n)
    along, height, tension, vertical = _plane_curve(material, horizontal, vertical_bottom, weight, inverse_ea)
    top_x, top_z = float(along[0]), float(height[0])
    touchdown = vessel-direction*top_x
    touchdown[2] = vessel[2]-top_z
    positions = touchdown[None, :]+along[:, None]*direction[None, :]
    positions[:, 2] = touchdown[2]+height
    positions[0] = vessel
    # No post-solve projection. The original full field remains the coverage and
    # non-penetration authority, including all queried cable footprints.
    bed, normals = grid.surface(positions)
    gaps = positions[:, 2]-bed
    vectors = horizontal*direction[None, :]+np.zeros_like(positions)
    vectors[:, 2] = vertical
    top_force, bottom_force = vectors[0].copy(), -vectors[-1].copy()
    total_weight = weight*length
    total_force = top_force+bottom_force+np.array([0., 0., -total_weight])
    root_error = abs(_normal_rise(length, horizontal, vertical_bottom, weight, inverse_ea)-depth_above_bed)
    tangent_error = abs(float(np.dot(vectors[-1]/bottom, normals[-1])))
    touchdown_error = abs(float(gaps[-1]))
    penetration = max(0., -float(np.min(gaps)))
    geometry_tolerance = tolerance+1e-8
    force_error = float(np.linalg.norm(total_force))
    accepted = bool(root_converged and root_error <= geometry_tolerance and
                    touchdown_error <= geometry_tolerance and penetration <= geometry_tolerance and
                    tangent_error <= 1e-7 and force_error <= max(1e-7, total_weight*1e-12))
    if not accepted:
        raise ValueError("slope_catenary failed its force/tangency/coverage/non-penetration verification")
    stretched = _stretched_length(length, horizontal, vertical_bottom, weight, inverse_ea)
    result = _base("affine-seabed-elastic-tangent-catenary-v1", [
        "Uniform positive submerged weight per unstretched material metre; linear axial elasticity or explicit inextensible limit.",
        "All seabed grid samples must represent one affine plane within the declared absolute tolerance; every cable footprint has complete known coverage.",
        "Touchdown total tension and horizontal touchdown-to-vessel heading are prescribed; the touchdown tangent is in the seabed plane.",
        "The suspended segment is in static force balance; the prescribed touchdown traction does not reconstruct an attached seabed tail or friction history.",
        "No current, bending/torsion stiffness, mixed materials, inline bodies, soil deformation or dynamic/steady laying motion is included."])
    if plane_error > 1e-10:
        result["warnings"].append(_warning("AFFINE_FIT_TOLERANCE", "The accepted affine fit differs slightly from the input grid; both the fit error and original-grid clearance are reported."))
    if np.max(tension)*inverse_ea > .05:
        result["warnings"].append(_warning("LARGE_LINEAR_AXIAL_STRAIN", "Axial strain exceeds 5%; the assumed linear elastic law needs an independent material-validity check."))
    result.update({"accepted": accepted, "seabed": grid.metadata(),
        "nodes": positions.tolist(), "node_material_m": material.tolist(),
        "node_tension_n": tension.tolist(), "node_tension_vectors_n": vectors.tolist(),
        "node_seabed_z_m": bed.tolist(), "node_seabed_normal": normals.tolist(),
        "node_clearance_m": gaps.tolist(),
        "frames": [{"time_s": 0., "ship": vessel.tolist(), "nodes": positions.tolist(),
                    "node_material_m": material.tolist(), "top_tension_n": float(tension[0]),
                    "bottom_tension_n": bottom, "touchdown": touchdown.tolist()}],
        "boundary": {"vessel_position_m": vessel.tolist(), "touchdown_position_m": touchdown.tolist(),
                     "touchdown_heading_deg": heading, "bottom_tension_is_total_magnitude": True,
                     "seabed_tail_solved": False},
        "end_forces_on_cable_n": {"vessel": top_force.tolist(), "touchdown": bottom_force.tolist()},
        "summary": {"natural_length_m": length, "stretched_arc_length_m": stretched,
                    "geometric_chord_length_m": float(np.sum(np.linalg.norm(np.diff(positions, axis=0), axis=1))),
                    "layback_m": top_x, "touchdown_depth_below_model_sea_m": float(-touchdown[2]),
                    "vessel_clearance_above_bed_m": depth_above_bed,
                    "wet_weight_total_n": total_weight, "top_tension_n": float(tension[0]),
                    "bottom_tension_n": bottom, "horizontal_tension_n": horizontal,
                    "bottom_vertical_tension_n": vertical_bottom,
                    "top_vertical_tension_n": float(vertical[0]),
                    "bottom_angle_from_horizontal_deg": math.degrees(math.atan(grade)),
                    "top_angle_from_horizontal_deg": math.degrees(math.atan2(vertical[0], horizontal)),
                    "maximum_axial_strain": float(np.max(tension)*inverse_ea),
                    "total_force_balance_n": total_force.tolist()},
        "plane": {"reference_xy_m": center.tolist(), "z_at_reference_m": reference_z,
                  "gradient_xy": gradient.tolist(), "grade_toward_vessel": grade,
                  "all_grid_node_fit_error_m": plane_error, "tolerance_m": tolerance},
        "solver": {"converged": root_converged, "accepted": accepted,
                   "method": "monotone tangent-plane elastic catenary root; no bed projection",
                   "iterations": root_iterations, "function_evaluations": evaluations,
                   "root_at_length_limit": bool(at_length_limit), "reach_roundoff_tolerance_m": float(reach_roundoff_tolerance),
                   "root_residual_m": root_error, "touchdown_bed_residual_m": touchdown_error,
                   "touchdown_normal_tangent_dot": tangent_error, "max_penetration_m": penetration,
                   "force_balance_residual_n": force_error,
                   "estimated_work_units": estimated_work, "max_work_units": budget,
                   "work_basis": "grid nodes + worst-case 12 quadrature samples per bounded root evaluation + bounded output samples; normalized units, not FLOPs"}})
    return result


class _StaticBudget(Exception):
    pass


def _energy_forces(positions, rest, ea, nodal_weight):
    """Actual Hooke tension and energy, including exact equal/opposite nodal loads."""
    delta = np.diff(positions, axis=0)
    length = np.linalg.norm(delta, axis=1)
    extension = np.maximum(length-rest, 0.)
    tension = ea*extension/rest
    segment_force = tension[:, None]*delta/np.maximum(length[:, None], 1e-30)
    internal = np.zeros_like(positions)
    internal[:-1] += segment_force
    internal[1:] -= segment_force
    external = np.zeros_like(positions)
    external[:, 2] = -nodal_weight
    elastic = float(np.sum(.5*ea/rest*extension**2))
    potential = float(nodal_weight@positions[:, 2])
    return elastic+potential, internal+external, tension, internal, external, elastic, potential, length


def _segment_bed_clearance(grid, positions, maximum_samples):
    """Exact clearance minima of each straight chord over bilinear grid cells.

    NoData intervals are rejected. Within a cell the restriction of a bilinear
    height to a straight chord is quadratic, so endpoints and its stationary
    point suffice; no decorative fixed-rate sampling claims full coverage.
    """
    minimum = math.inf
    worst_segment = None
    samples = 0
    intervals = 0
    def query(points, gradient=False):
        nonlocal samples
        samples += len(points)
        if samples > maximum_samples:
            raise _StaticBudget("straight-segment coverage verification exceeded max_segment_samples")
        return grid.evaluate(points, gradient=gradient)
    for i, (a, b) in enumerate(zip(positions[:-1], positions[1:])):
        delta = b-a
        crossings = [0., 1.]
        for axis, component in ((grid.x, 0), (grid.y, 1)):
            if abs(delta[component]) > 1e-14:
                times = (axis-a[component])/delta[component]
                crossings.extend(times[(times>0)&(times<1)].tolist())
        cuts = np.unique(np.array(crossings))
        for start, finish in zip(cuts[:-1], cuts[1:]):
            if finish-start < 1e-14:
                continue
            intervals += 1
            points = a[None, :]+np.array([start, (start+finish)/2, finish])[:, None]*delta[None, :]
            # The midpoint enforces all four cell corners, including zero-weight
            # corners ignored by a height-only query at the exact cell boundary.
            query(points[1:2], gradient=True)
            heights = query(points)
            gaps = points[:, 2]-heights
            q2 = 2*(gaps[0]+gaps[2]-2*gaps[1])
            q1 = gaps[2]-gaps[0]-q2
            candidate = float(min(gaps[0], gaps[2]))
            if q2 > 0:
                stationary = -q1/(2*q2)
                if 0 < stationary < 1:
                    t = start+(finish-start)*stationary
                    point = a+t*delta
                    actual = float(point[2]-query(point[None, :])[0])
                    candidate = min(candidate, actual)
            if candidate < minimum:
                minimum, worst_segment = candidate, i
    return {"minimum_clearance_m": float(minimum), "worst_segment_index": worst_segment,
            "samples": samples, "cell_intervals": intervals,
            "method": "per-cell exact quadratic straight-chord gap extrema; unknown intervals rejected"}


def static_equilibrium(config: dict) -> dict:
    """Fixed-end, fixed-natural-length tension-only statics over a 2D height field.

    Contact at free nodes is frictionless. Optional explicitly prescribed
    sticking nodes are accepted only if their actual required support belongs
    to the nonnegative normal/Coulomb cone. This is a conditional equilibrium,
    not an inferred as-laid friction history or a steady translating cable.
    """
    return _static_equilibrium_core(config)


def _static_equilibrium_core(config: dict, *, segment_ea_n=None,
                             node_wet_weight_n=None, load_scale_n=None) -> dict:
    """Internal material statics; the public scalar input contract is unchanged.

    Coefficients are supplied only by the independently validated natural
    material operator, never by an accepted-result JSON or a public request.
    """
    c = _config(config)
    if any(not isinstance(k, str) for k in c):
        raise ValueError("static_equilibrium config keys must be strings")
    allowed = {"seabed_grid", "vessel_position_m", "anchor_position_m", "natural_length_m",
               "rest_lengths_m", "nodes", "wet_weight_n_m", "ea_n", "initial_positions_m",
               "contact_policy", "seabed_friction", "sticking_nodes", "force_tolerance_n",
               "relative_force_tolerance", "contact_tolerance_m", "max_solver_iterations",
               "max_function_evaluations", "max_segment_samples", "max_work_units"}
    if set(c)-allowed:
        raise ValueError("static_equilibrium unsupported inputs: "+", ".join(sorted(set(c)-allowed)))
    for key in ("seabed_grid", "vessel_position_m", "anchor_position_m"):
        if key not in c:
            raise ValueError("static_equilibrium requires "+key)
    grid = BathymetryGrid(c["seabed_grid"])
    vessel = _point(c["vessel_position_m"], "vessel_position_m")
    anchor = _point(c["anchor_position_m"], "anchor_position_m")
    for point in (vessel, anchor):
        if point[2] > 0:
            raise ValueError("static boundary heights must not exceed model sea surface zero")
        floor = grid.surface(point[None, :])[0][0]
        if point[2] < floor-1e-9:
            raise ValueError("static fixed boundary is below its declared seabed")
    if np.linalg.norm(vessel-anchor) < .001:
        raise ValueError("static endpoints require at least .001 m separation")
    length_fields = [k for k in ("natural_length_m", "rest_lengths_m") if k in c]
    if len(length_fields) != 1:
        raise ValueError("provide exactly one natural_length_m or rest_lengths_m")
    if "rest_lengths_m" in c:
        values = c["rest_lengths_m"]
        if not isinstance(values, list) or not 5 <= len(values) <= 79:
            raise ValueError("rest_lengths_m requires 5 to 79 positive segment lengths")
        rest = np.array([_num({"v":v}, "v", 1., 1e-4, 1e6) for v in values])
        n = len(rest)+1
        if "nodes" in c and _integer(c, "nodes", n, 6, 80) != n:
            raise ValueError("nodes must match rest_lengths_m length + 1")
    else:
        n = _integer(c, "nodes", 24, 6, 80)
        rest = np.full(n-1, _num(c, "natural_length_m", 1., .001, 1e6)/(n-1))
        if np.min(rest) < 1e-4:
            raise ValueError("each natural segment must be at least .0001 m")
    natural_length = float(rest.sum())
    if natural_length > 1e6:
        raise ValueError("total static natural length exceeds 1000000 m")
    weight = _num(c, "wet_weight_n_m", 4., 1e-6, 20000)
    ea = _num(c, "ea_n", 1e6, 100, 1e12)
    nodal_weight = np.r_[rest[0]/2, (rest[:-1]+rest[1:])/2, rest[-1]/2]*weight
    material_loading = segment_ea_n is not None or node_wet_weight_n is not None or load_scale_n is not None
    if material_loading:
        if segment_ea_n is None or node_wet_weight_n is None or load_scale_n is None:
            raise ValueError("internal static material core requires EA, nodal weight and absolute load scale together")
        ea = np.asarray(segment_ea_n, dtype=float)
        nodal_weight = np.asarray(node_wet_weight_n, dtype=float)
        if ea.shape != (n-1,) or not np.isfinite(ea).all() or np.any(ea < 100) or np.any(ea > 1e12*(1+1e-10)):
            raise ValueError("internal static segment EA must be finite positive declared material stiffness")
        if nodal_weight.shape != (n,) or not np.isfinite(nodal_weight).all():
            raise ValueError("internal static nodal wet weight must be a finite signed load")
        load_scale = _num({"v":load_scale_n}, "v", 1., 0., 2.2e10)
        if load_scale + 1e-7 < float(np.sum(np.abs(nodal_weight))):
            raise ValueError("internal static absolute load scale cannot hide signed load cancellation")
    else:
        load_scale = weight*natural_length
    policy = c.get("contact_policy", "frictionless")
    if policy not in ("frictionless", "prescribed_stick"):
        raise ValueError("contact_policy must be frictionless or prescribed_stick")
    friction = _num(c, "seabed_friction", 0., 0, 2)
    raw_sticking = c.get("sticking_nodes", [])
    if not isinstance(raw_sticking, list) or len(raw_sticking) > n-2:
        raise ValueError("sticking_nodes must be a bounded list of interior node declarations")
    if policy == "frictionless" and (friction != 0 or raw_sticking):
        raise ValueError("frictionless policy requires zero seabed_friction and no sticking_nodes")
    if policy == "prescribed_stick" and (friction <= 0 or not raw_sticking):
        raise ValueError("prescribed_stick requires positive seabed_friction and explicit sticking_nodes history")
    sticking = {}
    for row in raw_sticking:
        if not isinstance(row, dict) or set(row) != {"node_index", "position_m"}:
            raise ValueError("sticking_nodes entries require exactly node_index and position_m")
        i = _integer(row, "node_index", 0, 1, n-2)
        if i in sticking:
            raise ValueError("sticking node indices must be unique")
        point = _point(row["position_m"], "sticking_nodes.position_m")
        floor = grid.surface(point[None, :])[0][0]
        if abs(point[2]-floor) > 1e-8:
            raise ValueError("prescribed sticking positions must be on the known seabed, not projected there")
        sticking[i] = point
    force_absolute = _num(c, "force_tolerance_n", .01, 1e-6, 1)
    force_relative = _num(c, "relative_force_tolerance", 1e-5, 1e-9, 1e-3)
    force_tolerance = max(force_absolute, force_relative*max(1., load_scale))
    contact_tolerance = _num(c, "contact_tolerance_m", 1e-6, 1e-9, 1e-4)
    max_iterations = _integer(c, "max_solver_iterations", 300, 1, 600)
    max_evaluations = _integer(c, "max_function_evaluations", 2000, 1, 10000)
    max_segment_samples = _integer(c, "max_segment_samples", 20000, 10, 200000)
    max_work = _integer(c, "max_work_units", 200000000, 1, 2000000000)
    free = np.array([i for i in range(1, n-1) if i not in sticking], dtype=int)
    variables = max(1, 3*len(free))
    estimated_work = int(grid.z.size+max_iterations*variables**3+
                         max_evaluations*(variables**2+4*n)+5*max_segment_samples+20*n)
    if estimated_work > max_work:
        raise ValueError("static_equilibrium declared dense-solver computation exceeds max_work_units; reduce nodes/iteration caps")
    material = np.r_[np.cumsum(rest[::-1])[::-1], 0.]
    fractions = (natural_length-material)/natural_length
    positions = vessel[None, :]+fractions[:, None]*(anchor-vessel)[None, :]
    initial_kind = "straight fixed-end shape with vertical feasible interior seed; not equilibrium"
    if "initial_positions_m" in c:
        rows = c["initial_positions_m"]
        if not isinstance(rows, list) or len(rows) != n:
            raise ValueError("initial_positions_m must contain exactly nodes finite positions")
        positions = np.array([_point(row, "initial_positions_m") for row in rows])
        if not np.allclose(positions[[0, -1]], np.stack((vessel, anchor)), rtol=0, atol=1e-9):
            raise ValueError("initial shape endpoints must match fixed boundaries")
        initial_kind = "user supplied feasible numerical starting shape; not equilibrium"
    positions[0], positions[-1] = vessel, anchor
    for i, point in sticking.items():
        if "initial_positions_m" in c and np.linalg.norm(positions[i]-point) > 1e-9:
            raise ValueError("initial shape must preserve the declared sticking positions")
        positions[i] = point
    seed_bed = grid.surface(positions)[0]
    if "initial_positions_m" in c:
        if np.min(positions[:, 2]-seed_bed) < -contact_tolerance:
            raise ValueError("user initial shape penetrates the known seabed")
    else:
        positions[free, 2] = np.maximum(positions[free, 2], seed_bed[free])
    scale = max(natural_length, float(np.linalg.norm(vessel-anchor)), .001)
    energy_scale = max(load_scale*scale, 1e-6)
    template = positions.copy()
    evaluations = 0
    cache = None
    best = None
    def state(x):
        nonlocal cache, evaluations, best
        if cache is not None and np.array_equal(x, cache[0]):
            return cache
        if evaluations >= max_evaluations:
            raise _StaticBudget("static optimizer exceeded max_function_evaluations")
        evaluations += 1
        p = template.copy()
        p[free] = vessel[None, :]+np.asarray(x).reshape(-1, 3)*scale
        bed, gradient = grid.evaluate(p)
        energy, forces, *physical = _energy_forces(p, rest, ea, nodal_weight)
        cache = (np.array(x).copy(), p, bed, gradient, energy, forces, physical)
        if np.min(p[:, 2]-bed) >= -contact_tolerance:
            if best is None or energy < best[4]:
                best = cache
        return cache
    def objective(x):
        return state(x)[4]/energy_scale
    def jacobian(x):
        return -state(x)[5][free].ravel()*scale/energy_scale
    def gaps(x):
        st = state(x)
        return (st[1][free, 2]-st[2][free])/scale
    def gap_jacobian(x):
        gradient = state(x)[3][free]
        matrix = np.zeros((len(free), 3*len(free)))
        for j, grad in enumerate(gradient):
            matrix[j, 3*j:3*j+3] = [-grad[0], -grad[1], 1.]
        return matrix
    x0 = ((positions[free]-vessel[None, :])/scale).ravel()
    converged, iterations, message, budget_exhausted = False, 0, "", False
    if len(free):
        lower = (np.array([grid.x[0], grid.y[0], -12000.])-vessel)/scale
        upper = (np.array([grid.x[-1], grid.y[-1], 0.])-vessel)/scale
        bounds = list(zip(np.tile(lower, len(free)), np.tile(upper, len(free))))
        state(x0)
        try:
            optimization = minimize(objective, x0, jac=jacobian, method="SLSQP", bounds=bounds,
                                    constraints=[{"type": "ineq", "fun": gaps, "jac": gap_jacobian}],
                                    options={"maxiter": max_iterations, "ftol": 1e-12, "disp": False})
            final_state = state(optimization.x)
            positions = final_state[1].copy()
            converged, iterations, message = bool(optimization.success), int(optimization.nit), str(optimization.message)
        except _StaticBudget as error:
            budget_exhausted, message = True, str(error)
            positions = (best if best is not None else cache)[1].copy()
    else:
        converged, message = True, "All interior positions supplied as conditional sticking history; independent force feasibility still required"
    bed, normals = grid.surface(positions)
    energy, forces, tension, internal, external, elastic, potential, lengths = _energy_forces(positions, rest, ea, nodal_weight)
    clearance = positions[:, 2]-bed
    contact = clearance <= contact_tolerance
    normal_force = np.zeros(n)
    friction_force = np.zeros((n, 3))
    required_stick_friction = np.zeros((n, 3))
    required_stick_normal = np.zeros(n)
    stick_margins = []
    for i in free:
        if contact[i]:
            normal_force[i] = max(0., -float(forces[i]@normals[i]))
    for i in sticking:
        required = -forces[i]
        rn = float(required@normals[i])
        tangential = required-rn*normals[i]
        magnitude = float(np.linalg.norm(tangential))
        required_stick_normal[i] = rn
        required_stick_friction[i] = tangential
        normal_force[i] = max(0., rn)
        capacity = float(friction*normal_force[i])
        friction_force[i] = tangential*min(1., capacity/max(magnitude, 1e-30))
        stick_margins.append({"node_index": int(i), "required_normal_n": rn,
                             "required_tangential_n": magnitude, "coulomb_capacity_n": capacity,
                             "margin_n": capacity-magnitude, "normal_margin_n": rn})
    contact_force = normal_force[:, None]*normals+friction_force
    boundary_force = np.zeros_like(positions)
    boundary_force[[0, -1]] = -forces[[0, -1]]
    force_residual = forces+contact_force+boundary_force
    residual_max = float(np.max(np.linalg.norm(force_residual, axis=1)))
    penetration = max(0., -float(np.min(clearance)))
    complementarity = float(np.max(np.abs(normal_force*clearance*normals[:, 2])))
    complementarity_tolerance = float(contact_tolerance*max(1., float(normal_force.max())))
    verification = None
    try:
        verification = _segment_bed_clearance(grid, positions, max_segment_samples)
    except _StaticBudget as error:
        budget_exhausted, message = True, message+"; "+str(error)
    stick_valid = all(v["normal_margin_n"] >= -force_tolerance and v["margin_n"] >= -force_tolerance for v in stick_margins)
    segment_valid = verification is not None and verification["minimum_clearance_m"] >= -contact_tolerance
    collapsed = bool(np.min(lengths) < 1e-6)
    total_balance = np.sum(external+contact_force+boundary_force, axis=0)
    global_force_residual = float(np.linalg.norm(total_balance))
    global_force_tolerance = (n-2)*force_tolerance
    accepted = bool(converged and not budget_exhausted and residual_max <= force_tolerance and
                    global_force_residual <= global_force_tolerance and
                    complementarity <= complementarity_tolerance and
                    penetration <= contact_tolerance and stick_valid and segment_valid and not collapsed)
    reason_codes = []
    if not converged: reason_codes.append("OPTIMIZER_NOT_CONVERGED")
    if budget_exhausted: reason_codes.append("COMPUTATION_BUDGET_EXHAUSTED")
    if residual_max > force_tolerance: reason_codes.append("FORCE_BALANCE_NOT_CONVERGED")
    if global_force_residual > global_force_tolerance: reason_codes.append("GLOBAL_FORCE_BALANCE_NOT_CONVERGED")
    if complementarity > complementarity_tolerance: reason_codes.append("CONTACT_COMPLEMENTARITY_NOT_CONVERGED")
    if penetration > contact_tolerance: reason_codes.append("NODE_BED_PENETRATION")
    if not stick_valid: reason_codes.append("PRESCRIBED_STICK_CAPACITY_EXCEEDED")
    if verification is not None and not segment_valid: reason_codes.append("STRAIGHT_SEGMENT_BED_INTERSECTION")
    if collapsed: reason_codes.append("COLLAPSED_SEGMENT_UNSUPPORTED")
    node_tension = np.r_[tension[0], (tension[:-1]+tension[1:])/2, tension[-1]]
    result = _base("2d-seabed-tension-only-discrete-static-v1", [
        "Both endpoint positions and every unstretched material segment length are prescribed; endpoint reactions are solved rather than bottom tension imposed.",
        "Uniform positive wet weight per natural metre, linear tensile-only axial elasticity, zero bending/torsion, no current or moving laying boundary.",
        "Free-node contact is frictionless hard unilateral contact; explicitly prescribed sticking positions condition a static Coulomb feasibility test, not an inferred loading history.",
        "Only a local stationary equilibrium is sought; uniqueness, global energy minimum and engineering validation are not proved.",
        "The cable is a straight-chord nodal model; per-cell chord clearance is verified, and unresolved between-node bed contact is rejected rather than silently projected.",
        "Endpoint supports are separate external boundary forces, including discretized endpoint weight; they are not reported as seabed friction."])
    if not accepted:
        result["warnings"].append(_warning("STATIC_EQUILIBRIUM_NOT_ACCEPTED", "The best available finite shape failed an independent equilibrium/geometry/budget check; it must not initialize a claimed balanced installation.", "error"))
    if np.max(tension/ea) > .05:
        result["warnings"].append(_warning("LARGE_LINEAR_AXIAL_STRAIN", "Axial strain exceeds 5%; the assumed linear elastic law needs an independent material-validity check."))
    frame = {"time_s": 0., "ship": vessel.tolist(), "nodes": positions.tolist(),
             "node_material_m": material.tolist(), "node_contact_mask": contact.tolist(),
             "top_tension_n": float(tension[0]), "bottom_tension_n": float(tension[-1])}
    result.update({"accepted": accepted, "seabed": grid.metadata(), "nodes": positions.tolist(),
        "node_material_m": material.tolist(), "rest_lengths_m": rest.tolist(),
        "segment_tension_n": tension.tolist(), "node_tension_n": node_tension.tolist(),
        "node_wet_weight_n": nodal_weight.tolist(), "node_internal_force_n": internal.tolist(),
        "node_external_force_n": external.tolist(), "node_contact_force_n": contact_force.tolist(),
        "node_contact_normal_force_n": normal_force.tolist(), "node_contact_friction_force_n": friction_force.tolist(),
        "node_required_stick_normal_force_n": required_stick_normal.tolist(),
        "node_required_stick_friction_force_n": required_stick_friction.tolist(),
        "node_boundary_force_n": boundary_force.tolist(), "node_force_residual_n": force_residual.tolist(),
        "node_seabed_z_m": bed.tolist(), "node_seabed_normal": normals.tolist(),
        "node_clearance_m": clearance.tolist(), "node_contact_mask": contact.tolist(),
        "frames": [frame], "initialization": {"method": initial_kind, "physical_equilibrium_claim": False},
        "boundary": {"vessel_position_m": vessel.tolist(), "anchor_position_m": anchor.tolist(),
                     "contact_policy": policy, "seabed_friction": friction,
                     "sticking_nodes": deepcopy(raw_sticking), "boundary_forces_include_endpoint_half_segment_weight": True},
        "end_forces_on_cable_n": {"vessel": boundary_force[0].tolist(), "anchor": boundary_force[-1].tolist()},
        "stick_feasibility": stick_margins, "segment_clearance": verification,
        "summary": {"natural_length_m": natural_length, "geometric_chord_length_m": float(lengths.sum()),
                    "wet_weight_total_n": float(nodal_weight.sum()), "top_tension_n": float(tension[0]),
                    "bottom_tension_n": float(tension[-1]), "max_tension_n": float(tension.max()),
                    "maximum_axial_strain": float(np.max(tension/ea)), "contact_nodes": int(np.sum(contact)),
                    "max_normal_contact_force_n": float(normal_force.max()),
                    "axial_elastic_energy_j": elastic, "submerged_gravity_potential_j": potential,
                    "total_potential_energy_j": energy, "total_force_balance_n": total_balance.tolist()},
        "solver": {"converged": converged, "accepted": accepted, "method": "scaled constrained tension-only potential energy SLSQP; independent nodal-force and chord-coverage verification",
                   "iterations": iterations, "function_evaluations": evaluations,
                   "message": message, "rejection_codes": reason_codes,
                   "max_node_force_residual_n": residual_max, "force_tolerance_n": force_tolerance,
                   "max_penetration_m": penetration, "contact_tolerance_m": contact_tolerance,
                   "max_complementarity_n_m": complementarity,
                   "complementarity_tolerance_n_m": complementarity_tolerance,
                   "global_force_balance_residual_n": global_force_residual,
                   "global_force_tolerance_n": global_force_tolerance,
                   "estimated_work_units": estimated_work, "max_work_units": max_work,
                   "max_solver_iterations": max_iterations, "max_function_evaluations": max_evaluations,
                   "max_segment_samples": max_segment_samples,
                   "bathymetry_node_queries": int(grid.queried_nodes),
                   "work_basis": "bounded dense-solver iteration times free variables cubed, evaluation times variables squared plus nodal operations, and final chord samples; normalized units, not literal FLOPs"}})
    if material_loading:
        result["model"] = "2d-seabed-material-tension-only-discrete-static-v2"
        result["assumptions"][1] = "Actual segment harmonic EA and signed nodal material/point weights; linear tensile-only axial elasticity, zero bending/torsion, no current or moving boundary."
        result["segment_ea_n"] = ea.tolist()
        result["summary"]["absolute_load_scale_n"] = load_scale
        result["summary"]["positive_nodal_wet_load_n"] = float(np.sum(np.maximum(nodal_weight,0)))
        result["summary"]["negative_nodal_wet_load_n"] = float(np.sum(np.minimum(nodal_weight,0)))
        result["solver"]["absolute_load_scale_n"] = load_scale
    return result
