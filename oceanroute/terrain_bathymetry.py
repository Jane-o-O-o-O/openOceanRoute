"""Explicit source-library resampling into a local dynamic height field.

A user-supplied model sea-surface height performs only a declared vertical
translation. It is not a tidal/vertical datum model or a route-to-lay controller.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np
from pyproj import CRS, Transformer

from .bathymetry import BathymetryGrid, SCHEMA
from .core import route_signature
from .geodesy import coordinate, finite_number
from .terrain_sources import query_terrain


def _integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or not 2 <= value <= 1000:
        raise ValueError(f'{name}须为2..1000整数')
    return value


def bathymetry_from_sources(project, config):
    if not isinstance(project, dict):
        raise ValueError('project须为工程对象')
    allowed = {'origin', 'bounds_m', 'nx', 'ny', 'vertical_datum', 'sea_surface_height_m', 'query_config'}
    required = allowed-{'query_config'}
    if not isinstance(config, dict) or not required <= config.keys() or set(config)-allowed:
        raise ValueError('二维海底转换须明确origin、bounds_m、nx/ny、vertical_datum和sea_surface_height_m')
    origin = config['origin']
    if not isinstance(origin, list) or len(origin) != 2:
        raise ValueError('origin须为明确WGS84 [longitude,latitude]')
    longitude, latitude = coordinate(*origin)
    if abs(latitude) >= 89:
        raise ValueError('局部AEQD动力网格尚不支持纬度绝对值>=89°')
    bounds = config['bounds_m']
    if not isinstance(bounds, list) or len(bounds) != 4:
        raise ValueError('bounds_m须为局部米制[xmin,ymin,xmax,ymax]')
    xmin, ymin, xmax, ymax = [finite_number(v, 'bounds_m', minimum=-100_000, maximum=100_000) for v in bounds]
    if not xmin < xmax or not ymin < ymax or not xmin <= 0 <= xmax or not ymin <= 0 <= ymax:
        raise ValueError('bounds_m须严格升序，并覆盖明确的船位局部原点[0,0]')
    nx, ny = _integer(config['nx'], 'nx'), _integer(config['ny'], 'ny')
    if nx*ny > 10_000:
        raise ValueError('二维动力网格最多10,000节点，不自动删点或缩小分辨率')
    if min((xmax-xmin)/(nx-1), (ymax-ymin)/(ny-1)) < .001:
        raise ValueError('二维动力网格间距须至少.001m')
    datum = config['vertical_datum']
    if not isinstance(datum, str) or not datum.strip() or len(datum) > 128:
        raise ValueError('须明确声明1..128字符测深vertical_datum')
    sea_height = finite_number(config['sea_surface_height_m'], 'sea_surface_height_m', minimum=-100, maximum=100)
    query_config = config.get('query_config', {})
    if not isinstance(query_config, dict) or set(query_config)-{'max_work_units', 'max_output_bytes'}:
        raise ValueError('query_config只支持max_work_units和max_output_bytes预算')
    crs = CRS.from_proj4(f'+proj=aeqd +lat_0={latitude:.15g} +lon_0={longitude:.15g} +datum=WGS84 +units=m +type=crs')
    inverse = Transformer.from_crs(crs, 4326, always_xy=True, allow_ballpark=False)
    x, y = np.linspace(xmin, xmax, nx), np.linspace(ymin, ymax, ny)
    xx, yy = np.meshgrid(x, y)
    lon, lat = inverse.transform(xx.ravel(), yy.ravel(), errcheck=True)
    points = [[float(a), float(b)] for a, b in zip(lon, lat)]
    query = query_terrain(project.get('terrain_sources', []), points,
                          {**query_config, 'vertical_datum': datum, 'max_query_points': 10_000})
    z = [None if row['depth_m'] is None else -row['depth_m']-sea_height for row in query['samples']]
    model_datum = f'MODEL_SEA_SURFACE_Z0 ({datum}; sea_surface_height_m={sea_height:g})'
    grid = {'schema': SCHEMA, 'x_m': x.tolist(), 'y_m': y.tolist(),
            'z_m': [z[i*nx:(i+1)*nx] for i in range(ny)],
            'source': {'name': '共享地形源库显式局部重采样', 'horizontal_crs': crs.to_string(),
                       'origin_projected_m': [0, 0], 'vertical_datum': model_datum,
                       'sha256': query['quality']['library_signature']}}
    # Preserve every real node and missing value even when the mechanical grid
    # cannot be used. This validation does not promise future cable coverage.
    warnings = list(query['warnings'])
    try:
        field = BathymetryGrid(grid)
        field.surface(np.array([[0., 0.]]))
        validation = {'can_apply': True, 'error': None, 'known_cells': int(np.count_nonzero(field.valid_cells))}
    except ValueError as error:
        validation = {'can_apply': False, 'error': str(error), 'known_cells': None}
        warnings.append({'code': 'BATHYMETRY_GRID_UNUSABLE', 'severity': 'warning', 'message': str(error)})
    compact = [{key: row[key] for key in ('longitude', 'latitude', 'depth_m', 'source_id', 'source_fingerprint', 'fallback', 'fallback_count')}
               for row in query['samples']]
    for index, row in enumerate(compact):
        row.update(row_index=index//nx, column_index=index%nx, z_model_m=z[index])
    return {'seabed_grid': grid, 'samples': compact, 'validation': validation,
            'can_apply': validation['can_apply'], 'validation_status': 'research',
            'model': 'source-library-to-local-bathymetry-v1', 'warnings': warnings,
            'derivation': {'library_signature': query['quality']['library_signature'],
                           'route_signature': route_signature(project), 'origin_wgs84': [longitude, latitude],
                           'source_vertical_datum': datum, 'sea_surface_height_m': sea_height,
                           'vertical_translation': 'z_model_m = -depth_m - sea_surface_height_m',
                           'grid_sha256': hashlib.sha256(json.dumps(grid, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
                           'quality': query['quality'], 'budget': query['budget'], 'source_metadata': query['sources'],
                           'missing_nodes': query['quality']['missing_count']},
            'assumptions': ['No tidal or vertical datum transformation is inferred; the user declares model sea-surface height in the selected source datum.',
                            'Local AEQD X/Y metres define the cable/ship plane; spherical/geodesic dynamics are not modeled.',
                            'NoData is preserved; contact needs all four cell corners for a normal and future trajectories must remain covered.',
                            'This grid does not make the flat-bed ShipPlan-to-voyage initialization valid on variable seabed.',
                            'Node interpolation and source selection are explicit approximations, without field accuracy validation.']}
