"""Directional drag-mass predictor and incremental v5 cable constraints.

Quadratic drag magnitudes and the index-secant tangent are frozen at the old
state for each internal step. This is a local semi-implicit split, not a fully
implicit nonlinear fluid/rod solve. All constraint increments use the same
positive directional mass inverse as the predictor.
"""
from __future__ import annotations

import numpy as np
from scipy.linalg import solve_banded


def current_predictor(positions, velocities, rest, local, loading, bed, normals, h):
    """Return velocity, inverse drag-mass blocks and actual old force warmstart.

    The predictor already applies old axial and normal forces. Warm multipliers
    subsequently carry only constraint increments; applying their old kicks
    again would double those forces. Fixed endpoint inverse blocks are zero.
    """
    mass = local["mass"]
    tangent = loading["node_tangent"]
    fluid = loading["node_current_m_s"]
    relative = fluid-velocities
    normal_relative = relative-np.sum(relative*tangent, axis=1)[:, None]*tangent
    cable = local["drag"]*np.linalg.norm(normal_relative, axis=1)
    body = local["body_drag"]*np.linalg.norm(relative, axis=1)
    parallel_mass = mass+h*body
    normal_mass = parallel_mass+h*cable
    if not np.isfinite(normal_mass).all() or np.any(parallel_mass <= 0):
        raise ValueError("current-equilibrium directional drag mass is nonfinite or nonpositive")
    eye = np.eye(3)[None, :, :]
    tt = tangent[:, :, None]*tangent[:, None, :]
    inverse = eye/normal_mass[:, None, None]+(1/parallel_mass-1/normal_mass)[:, None, None]*tt

    delta = np.diff(positions, axis=0)
    lengths = np.linalg.norm(delta, axis=1)
    if np.any(lengths < 1e-6):
        raise ValueError("current-equilibrium predictor has a collapsed physical segment")
    segment_direction = delta/lengths[:, None]
    tension = local["ea"]*np.maximum(lengths/rest-1, 0)
    axial = np.zeros_like(positions)
    axial[:-1] += tension[:, None]*segment_direction
    axial[1:] -= tension[:, None]*segment_direction
    external = loading["node_total_drag_force_n"].copy()
    external[:, 2] -= local["weight"]
    force = axial+external
    support = np.where(positions[:, 2] <= bed+1e-8,
                       np.maximum(-np.sum(force*normals, axis=1), 0), 0)
    support[[0, -1]] = 0
    buoyant_axial = axial.copy()
    buoyant_axial[:, 2] -= local["weight"]
    normal_fluid = fluid-np.sum(fluid*tangent, axis=1)[:, None]*tangent
    rhs = mass[:, None]*velocities+h*(buoyant_axial+support[:, None]*normals
                                     +cable[:, None]*normal_fluid+body[:, None]*fluid)
    inverse[[0, -1]] = 0
    predicted = np.einsum("nij,nj->ni", inverse, rhs)
    if not np.isfinite(predicted).all() or not np.isfinite(inverse).all():
        raise ValueError("current-equilibrium predictor exceeded its finite numerical domain")
    return predicted, inverse, -h*h*tension, h*h*support


def stretch_project_blocks(positions, inverse, rest, ea, h, multipliers):
    """Tension-only banded increments in the predictor's block mass metric."""
    delta = np.diff(positions, axis=0)
    length = np.linalg.norm(delta, axis=1)
    tangent = delta/np.maximum(length[:, None], 1e-12)
    compliance = rest/(ea*h*h)
    constraint = length-rest
    active = (constraint > 0) | (multipliers < 0)
    diagonal = np.einsum("ni,nij,nj->n", tangent, inverse[:-1]+inverse[1:], tangent)+compliance
    off = -np.einsum("ni,nij,nj->n", tangent[:-1], inverse[1:-1], tangent[1:])
    off *= active[:-1] & active[1:]
    band = np.zeros((3, len(rest)))
    band[1] = np.where(active, diagonal, 1.)
    band[0, 1:] = off; band[2, :-1] = off
    rhs = np.where(active, -constraint-compliance*multipliers, 0.)
    change = solve_banded((1, 1), band, rhs, overwrite_ab=True, overwrite_b=True, check_finite=False)
    proposed = np.minimum(multipliers+change, 0.)
    change = proposed-multipliers
    multipliers[:] = proposed
    corrections = change[:, None]*tangent
    positions[:-1] -= np.einsum("nij,nj->ni", inverse[:-1], corrections)
    positions[1:] += np.einsum("nij,nj->ni", inverse[1:], corrections)
    return float(np.max(np.abs(change)))


def bend_project_blocks(positions, inverse, rest, ei, h, multipliers):
    """Existing secant-bending approximation in the directional block metric."""
    for color in range(3):
        j = np.arange(1+color, len(positions)-1, 3)
        j = j[ei[j-1] > 0]
        if not len(j):
            continue
        left, right = rest[j-1], rest[j]
        a, b, d = 1/left, -(1/left+1/right), 1/right
        constraint = a[:, None]*positions[j-1]+b[:, None]*positions[j]+d[:, None]*positions[j+1]
        compliance = (left+right)/(2*ei[j-1]*h*h)
        blocks = (a*a)[:, None, None]*inverse[j-1]+(b*b)[:, None, None]*inverse[j]+(d*d)[:, None, None]*inverse[j+1]
        blocks += compliance[:, None, None]*np.eye(3)[None, :, :]
        rhs = -constraint-compliance[:, None]*multipliers[j-1]
        change = np.linalg.solve(blocks, rhs[..., None])[..., 0]
        multipliers[j-1] += change
        for nodes, factor in ((j-1, a), (j, b), (j+1, d)):
            positions[nodes] += factor[:, None]*np.einsum("nij,nj->ni", inverse[nodes], change)


def project_contact_blocks(grid, positions, inverse, multipliers, *, max_iterations=16):
    """Bounded unit-normal contact increments in the same block mass metric.

    Normals are resampled from complete real bilinear cells after each move.
    This remains the existing local contact approximation; it is not a global
    closest point, segment/solid collision, or friction-history solver.
    """
    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int) or not 1 <= max_iterations <= 64:
        raise ValueError("current contact projection budget must be 1 to 64")
    free = np.trace(inverse, axis1=1, axis2=2) > 0
    corrections = np.zeros_like(positions)
    for iteration in range(max_iterations):
        bed, normal = grid.surface(positions)
        signed = (positions[:, 2]-bed)*normal[:, 2]
        active = free & ((signed < -1e-10) | (multipliers > 0))
        if not np.any(active):
            return iteration+1, corrections
        direction = np.einsum("nij,nj->ni", inverse, normal)
        denominator = np.sum(normal*direction, axis=1)
        if np.any(denominator[active] <= 0) or not np.isfinite(denominator).all():
            raise ValueError("current contact projection has invalid directional mass")
        delta = np.zeros(len(positions))
        delta[active] = np.maximum(multipliers[active]-signed[active]/denominator[active], 0)-multipliers[active]
        move = direction*delta[:, None]
        positions += move
        corrections += delta[:, None]*normal
        multipliers += delta
        next_bed, next_normal = grid.surface(positions)
        gap = (positions[:, 2]-next_bed)*next_normal[:, 2]
        if np.max(np.linalg.norm(move, axis=1)) < 1e-10 and np.min(gap[free], initial=0) >= -1e-9:
            return iteration+1, corrections
    raise ValueError("current-equilibrium bed projection did not converge across real bilinear cells; refine time/mesh or terrain")
