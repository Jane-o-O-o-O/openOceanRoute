"""Geographic planning in the actual metric frame of an explicit bed grid.

Only a horizontal change of local origin is performed. Heights remain in the
caller's declared model sea-surface frame; no vertical datum is inferred.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math

import numpy as np

from .bathymetry import BathymetryGrid
from .coordinate_transforms import transform_coordinates
from .geodesy import coordinate, finite_number


def _digest(value):
    def canonical(item):
        if isinstance(item, float) and math.isfinite(item) and item.is_integer():
            return int(item)
        if isinstance(item, list):
            return [canonical(v) for v in item]
        if isinstance(item, dict):
            return {k: canonical(v) for k, v in item.items()}
        return item
    return hashlib.sha256(json.dumps(canonical(value), sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def initial_equilibrium_length(options, nodes):
    """Read declared material length, never estimate it from drawn geometry."""
    if not isinstance(options, dict):
        raise ValueError("equilibrium_start must be an object")
    allowed = {"anchor", "vessel_z_m", "natural_length_m", "rest_lengths_m",
               "initial_positions_m", "solver"}
    if set(options)-allowed or not {"anchor", "vessel_z_m"} <= options.keys():
        raise ValueError("equilibrium_start requires anchor/vessel_z_m and only declared material/solver inputs")
    if isinstance(nodes, bool) or not isinstance(nodes, int) or not 6 <= nodes <= 80:
        raise ValueError("equilibrium planning requires 6 to 80 material nodes")
    fields = [key for key in ("natural_length_m", "rest_lengths_m") if key in options]
    if len(fields) != 1:
        raise ValueError("equilibrium_start requires exactly one natural_length_m or rest_lengths_m")
    if fields[0] == "rest_lengths_m":
        values = options[fields[0]]
        if not isinstance(values, list) or len(values) != nodes-1:
            raise ValueError("equilibrium rest_lengths_m must match nodes - 1")
        length = math.fsum(finite_number(v, "rest_lengths_m", minimum=1e-4, maximum=1e6) for v in values)
    else:
        length = finite_number(options[fields[0]], "natural_length_m", minimum=.001, maximum=1e6)
    if not .001 <= length <= 1e6:
        raise ValueError("equilibrium initial material length is outside .001 to 1e6 metres")
    return length


class PlanBathymetryFrame:
    """Convert real geographic points and rebase every horizontal input alike."""

    def __init__(self, document, start, options, nodes):
        initial_equilibrium_length(options, nodes)
        field = BathymetryGrid(document)
        self.original = deepcopy(field.document)
        source = self.original["source"]
        self.crs = source["horizontal_crs"]
        if self.crs == "LOCAL_CARTESIAN_METRES":
            raise ValueError("geographic equilibrium planning needs a real projected metric CRS, not unbound LOCAL_CARTESIAN_METRES")
        self.original_origin = np.array(source["origin_projected_m"], dtype=float)
        self.operations = []
        self.warnings = []
        self.queries = 0
        self._cache = {}
        self.absolute_origin = np.array(self._absolute(start["longitude"], start["latitude"]), dtype=float)
        self.shift = self.absolute_origin-self.original_origin
        self.grid = deepcopy(self.original)
        self.grid["x_m"] = (field.x-self.shift[0]).tolist()
        self.grid["y_m"] = (field.y-self.shift[1]).tolist()
        self.grid["source"]["origin_projected_m"] = self.absolute_origin.tolist()
        # Validate actual arrays and complete ship cell after rebasing. NoData
        # is retained; there is no resize, extrapolation, or flat-bed fallback.
        self.field = BathymetryGrid(self.grid)
        self.field.surface(np.array([[0., 0.]]))
        anchor = options["anchor"]
        if not isinstance(anchor, dict) or set(anchor) != {"longitude", "latitude", "z_model_m"}:
            raise ValueError("equilibrium anchor requires longitude/latitude/z_model_m")
        anchor_xy = self.project(anchor["longitude"], anchor["latitude"])
        anchor_z = finite_number(anchor["z_model_m"], "anchor.z_model_m", minimum=-12000, maximum=0)
        vessel_z = finite_number(options["vessel_z_m"], "vessel_z_m", minimum=-12000, maximum=0)
        self.request = {"schema": "oceanroute.dynamic.initial-equilibrium.v1",
                        "vessel_position_m": [0., 0., vessel_z],
                        "anchor_position_m": [*anchor_xy, anchor_z]}
        self.anchor_wgs84 = [anchor["longitude"], anchor["latitude"]]
        for key in ("natural_length_m", "rest_lengths_m", "solver"):
            if key in options:
                self.request[key] = deepcopy(options[key])
        if "initial_positions_m" in options:
            seed = options["initial_positions_m"]
            if not isinstance(seed, list) or len(seed) != nodes or any(not isinstance(row, list) or len(row) != 3 for row in seed):
                raise ValueError("equilibrium seed positions must be finite N x 3")
            try:
                positions = np.array([[finite_number(v, "initial_positions_m", minimum=-1e7, maximum=1e7)
                                       for v in row] for row in seed], dtype=float)
            except (ValueError, TypeError, OverflowError) as error:
                raise ValueError("equilibrium seed positions must be finite N x 3") from error
            if positions.shape != (nodes, 3) or not np.all(np.isfinite(positions)):
                raise ValueError("equilibrium seed positions must be finite N x 3")
            positions[:, :2] -= self.shift
            self.request["initial_positions_m"] = positions.tolist()

    def _absolute(self, longitude, latitude):
        longitude, latitude = coordinate(longitude, latitude)
        key = (longitude, latitude)
        if key in self._cache:
            return self._cache[key]
        if self.queries >= 10000:
            raise ValueError("planning bed frame exceeds 10000 geographic conversions")
        self.queries += 1
        result = transform_coordinates({"source_crs": "EPSG:4326", "target_crs": self.crs,
                                        "points": [{"id": "plan", "x": longitude, "y": latitude}],
                                        "error_policy": "reject"})
        if not result["can_apply"]:
            raise ValueError("planning point has no usable non-ballpark bed-frame operation")
        output = result["points"][0]["output"]
        xy = [output["x"], output["y"]]
        self._cache[key] = xy
        for operation in result.get("operations", []):
            if operation not in self.operations:
                self.operations.append(deepcopy(operation))
        for warning in result.get("warnings", []):
            if warning not in self.warnings:
                self.warnings.append(deepcopy(warning))
        return xy

    def project(self, longitude, latitude):
        return (np.array(self._absolute(longitude, latitude))-self.absolute_origin).tolist()

    def metadata(self):
        return {"method": "actual geographic transform and common horizontal translation",
                "original_grid": deepcopy(self.original), "anchor_wgs84": self.anchor_wgs84,
                "horizontal_crs": self.crs, "original_origin_projected_m": self.original_origin.tolist(),
                "origin_projected_m": self.absolute_origin.tolist(), "translation_from_original_local_m": self.shift.tolist(),
                "original_grid_sha256": _digest(self.original), "rebased_grid_sha256": _digest(self.grid),
                "vertical_datum": self.grid["source"]["vertical_datum"],
                "vertical_translation_m": 0., "geographic_point_conversions": self.queries,
                "operations": deepcopy(self.operations), "warnings": deepcopy(self.warnings),
                "assumptions": ["All bed axes, numerical seeds and endpoints receive the same actual translation.",
                                "Model heights are preserved; geographic anchor height is explicitly z_model_m, not ellipsoid height.",
                                "The original bed stays finite and missing; only complete known cells may support mechanics.",
                                "Plan controls use this projected metre plane, not spherical vessel/cable dynamics."]}
