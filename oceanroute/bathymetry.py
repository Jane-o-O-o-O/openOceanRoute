"""Strict local metre height field and bounded nonlinear normal projection.

No projection, datum conversion, missing-data fill or outside extrapolation is
performed here. ``z_m[y][x]`` is height relative to model sea surface z=0.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re

import numpy as np
from pyproj import CRS
from scipy.optimize import brentq

SCHEMA = "oceanroute.bathymetry.v1"


def _number(value, name, lo=-1e7, hi=1e7):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"seabed_grid {name} must be a finite numeric JSON value")
    try:
        value = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"seabed_grid {name} must be a finite numeric JSON value") from error
    if not math.isfinite(value):
        raise ValueError(f"seabed_grid {name} must be a finite numeric JSON value")
    if not lo <= value <= hi:
        raise ValueError(f"seabed_grid {name} is outside its bounds")
    return float(value)


class BathymetryGrid:
    """Bilinear height field. Normals are the exact gradient in the chosen cell.

    Cell selection is right/up at interior grid knots, left/down at the upper
    boundaries. A gradient requires all four corners: no normals are invented
    next to a missing corner, even when its height interpolation weight is zero.
    """

    def __init__(self, document):
        if not isinstance(document, dict) or document.get("schema") != SCHEMA:
            raise ValueError("seabed_grid requires schema oceanroute.bathymetry.v1")
        if set(document) != {"schema", "x_m", "y_m", "z_m", "source"}:
            raise ValueError("seabed_grid requires exactly schema, x_m, y_m, z_m and source")
        axes = []
        for key in ("x_m", "y_m"):
            row = document[key]
            if not isinstance(row, list) or not 2 <= len(row) <= 1000:
                raise ValueError(f"seabed_grid {key} requires 2 to 1000 coordinates")
            axis = np.array([_number(v, key) for v in row])
            if np.any(np.diff(axis) < .001) or np.ptp(axis) > 1e6:
                raise ValueError(f"seabed_grid {key} must strictly increase with >= .001 m spacing and <= 1000 km extent")
            axes.append(axis)
        self.x, self.y = axes
        if len(self.x)*len(self.y) > 10000:
            raise ValueError("seabed_grid exceeds its 10000-node computation/storage limit")
        rows = document["z_m"]
        if not isinstance(rows, list) or len(rows) != len(self.y):
            raise ValueError("seabed_grid z_m rows must match ascending y_m")
        checked = []
        for row in rows:
            if not isinstance(row, list) or len(row) != len(self.x):
                raise ValueError("seabed_grid z_m columns must match ascending x_m")
            checked.append([np.nan if v is None else _number(v, "z_m", -12000, -.001) for v in row])
        self.z = np.array(checked)
        self.valid_cells = (np.isfinite(self.z[:-1, :-1]) & np.isfinite(self.z[:-1, 1:]) &
                            np.isfinite(self.z[1:, :-1]) & np.isfinite(self.z[1:, 1:]))
        if not np.any(self.valid_cells):
            raise ValueError("seabed_grid has no complete known bilinear cell")
        # Height-map contact is deliberately bounded away from vertical cliffs.
        for dx in (np.diff(self.z, axis=1)[:-1]/np.diff(self.x), np.diff(self.z, axis=1)[1:]/np.diff(self.x)):
            for dy in (np.diff(self.z, axis=0)[:, :-1]/np.diff(self.y)[:, None], np.diff(self.z, axis=0)[:, 1:]/np.diff(self.y)[:, None]):
                if np.any(np.hypot(dx, dy)[self.valid_cells] > 5):
                    raise ValueError("seabed_grid slopes exceed the supported height-map gradient magnitude 5")
        source = document["source"]
        if not isinstance(source, dict) or not {"name", "horizontal_crs", "origin_projected_m", "vertical_datum"} <= source.keys():
            raise ValueError("seabed_grid source requires name, horizontal_crs, origin_projected_m and vertical_datum")
        if set(source)-{"name", "horizontal_crs", "origin_projected_m", "vertical_datum", "sha256"}:
            raise ValueError("seabed_grid source contains unsupported provenance fields")
        for key in ("name", "horizontal_crs", "vertical_datum"):
            if not isinstance(source[key], str) or not 1 <= len(source[key].strip()) <= 1000:
                raise ValueError(f"seabed_grid source.{key} must be a nonempty string of at most 1000 characters")
        origin = source["origin_projected_m"]
        if not isinstance(origin, list) or len(origin) != 2:
            raise ValueError("seabed_grid source.origin_projected_m must be [easting, northing] in metres")
        for value in origin:
            _number(value, "source.origin_projected_m", -1e9, 1e9)
        if source["horizontal_crs"] != "LOCAL_CARTESIAN_METRES":
            try:
                crs = CRS.from_user_input(source["horizontal_crs"])
            except Exception as error:
                raise ValueError("seabed_grid source.horizontal_crs is invalid") from error
            if not crs.is_projected or len(crs.axis_info) != 2 or any(abs(axis.unit_conversion_factor-1) > 1e-12 for axis in crs.axis_info):
                raise ValueError("seabed_grid source CRS must be a horizontal projected CRS in metres, not degrees/feet")
            if {axis.direction for axis in crs.axis_info} != {"east", "north"}:
                raise ValueError("seabed_grid projected source requires east/north horizontal axes")
        elif origin != [0, 0]:
            raise ValueError("LOCAL_CARTESIAN_METRES source uses origin_projected_m [0,0]")
        if "sha256" in source and (not isinstance(source["sha256"], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", source["sha256"])):
            raise ValueError("seabed_grid source.sha256 must contain 64 hexadecimal characters")
        self.document = deepcopy(document)
        self.queried_nodes = 0

    def evaluate(self, xy, *, gradient=True):
        points = np.asarray(xy, dtype=float)
        if points.ndim != 2 or points.shape[1] < 2 or not np.all(np.isfinite(points)):
            raise ValueError("bathymetry sample requires finite local points [x,y] or [x,y,z]")
        px, py = points[:, 0], points[:, 1]
        if np.any((px < self.x[0]) | (px > self.x[-1]) | (py < self.y[0]) | (py > self.y[-1])):
            raise ValueError("seabed_grid coverage: a ship/cable/sample point is outside the declared grid; no extrapolation")
        ix = np.clip(np.searchsorted(self.x, px, side="right")-1, 0, len(self.x)-2)
        iy = np.clip(np.searchsorted(self.y, py, side="right")-1, 0, len(self.y)-2)
        dx, dy = self.x[ix+1]-self.x[ix], self.y[iy+1]-self.y[iy]
        tx, ty = (px-self.x[ix])/dx, (py-self.y[iy])/dy
        corners = np.column_stack((self.z[iy, ix], self.z[iy, ix+1], self.z[iy+1, ix], self.z[iy+1, ix+1]))
        weights = np.column_stack(((1-tx)*(1-ty), tx*(1-ty), (1-tx)*ty, tx*ty))
        required = np.ones_like(weights, dtype=bool) if gradient else weights != 0
        if np.any(required & ~np.isfinite(corners)):
            raise ValueError("seabed_grid coverage: a sample uses a NoData corner; missing bathymetry is not filled")
        self.queried_nodes += len(points)
        safe = np.where(np.isfinite(corners), corners, 0)
        height = np.sum(weights*safe, axis=1)
        if not gradient:
            return height
        dzdx = ((1-ty)*(corners[:, 1]-corners[:, 0]) + ty*(corners[:, 3]-corners[:, 2]))/dx
        dzdy = ((1-tx)*(corners[:, 2]-corners[:, 0]) + tx*(corners[:, 3]-corners[:, 1]))/dy
        return height, np.column_stack((dzdx, dzdy))

    def surface(self, points):
        height, gradient = self.evaluate(points)
        normals = np.column_stack((-gradient, np.ones(len(height))))
        normals /= np.linalg.norm(normals, axis=1)[:, None]
        return height, normals

    def project(self, positions, inverse_mass, multipliers, *, max_iterations=16):
        """Unilateral nonlinear point contact; accumulated unit-normal multiplier.

        Multipliers have mass*length units. Changing piecewise cell normals make
        this a local iterative approximation, not a global closest-point solve.
        """
        if isinstance(max_iterations, bool) or not isinstance(max_iterations, int) or not 1 <= max_iterations <= 64:
            raise ValueError("bathymetry normal projection iteration budget must be 1 to 64")
        corrections = np.zeros_like(positions)
        for iteration in range(max_iterations):
            bed, normal = self.surface(positions)
            signed = (positions[:, 2]-bed)*normal[:, 2]
            free = inverse_mass > 0
            active = free & ((signed < -1e-10) | (multipliers > 0))
            if not np.any(active):
                return iteration+1, corrections
            delta = np.zeros(len(positions))
            delta[active] = np.maximum(multipliers[active]-signed[active]/inverse_mass[active], 0)-multipliers[active]
            move = inverse_mass[:, None]*delta[:, None]*normal
            positions += move
            corrections += delta[:, None]*normal
            multipliers += delta
            next_bed, next_normal = self.surface(positions)
            gap = (positions[:, 2]-next_bed)*next_normal[:, 2]
            if np.max(np.abs(delta)*inverse_mass) < 1e-10 and np.min(gap[free], initial=0) >= -1e-9:
                return iteration+1, corrections
        raise ValueError("seabed_grid normal projection did not converge across bilinear cells; refine internal_dt_s/mesh or terrain")

    def initial_depth(self, direction, a):
        """First known catenary horizontal-touchdown root along the back ray.

        Every traversed bilinear cell is checked, including cells containing no
        sampled cable node. This does not infer slope-equilibrium cable geometry.
        """
        self.surface(np.array([[0., 0., 0.]]))
        if a == 0:
            return -float(self.evaluate(np.array([[0., 0.]]), gradient=False)[0])
        ray = -np.asarray(direction[:2])
        limits = [(axis[-1]/component if component > 1e-14 else axis[0]/component)
                  for axis, component in ((self.x, ray[0]), (self.y, ray[1])) if abs(component) > 1e-14]
        bound = min(limits)
        if bound <= 0:
            raise ValueError("seabed_grid cannot contain an initial suspended catenary back ray")
        knots = [0., float(bound)]
        for axis, component in ((self.x, ray[0]), (self.y, ray[1])):
            if abs(component) > 1e-14:
                knots.extend(float(q) for q in axis/component if 0 < q < bound)
        knots = sorted(set(knots))
        probes = sorted(set(knots + [(.5*(l+r)) for l, r in zip(knots[:-1], knots[1:])]))
        def residual(q):
            point = (q*ray)[None, :]
            depth = -float(self.surface(point)[0][0])
            return 2*a*math.asinh(math.sqrt(depth/(2*a)))-q
        previous, value = 0., residual(0.)
        for q in probes[1:]:
            now = residual(q)
            if now <= 0:
                root = brentq(residual, previous, q, xtol=1e-10, maxiter=100)
                return -float(self.surface((root*ray)[None, :])[0][0])
            previous, value = q, now
        raise ValueError("seabed_grid has insufficient known extent for the initial horizontal-touchdown catenary")

    def metadata(self):
        return {"grid": deepcopy(self.document), "interpolation": "bilinear",
                "coverage_policy": "reject_outside_or_missing_cell", "normal_policy": "cell gradient; right/up cell at interior knots",
                "grid_nodes": int(self.z.size), "known_cells": int(np.count_nonzero(self.valid_cells)),
                "bounds_m": [float(self.x[0]), float(self.y[0]), float(self.x[-1]), float(self.y[-1])],
                "vertical_reference": "input elevations already aligned to model sea surface z=0; source datum is provenance only"}
