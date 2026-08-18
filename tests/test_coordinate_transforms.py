"""Coordinate contracts verified using real PROJ operations and finite HTTP JSON."""
from copy import deepcopy
import json
import math

from fastapi.testclient import TestClient
from pyproj import CRS, Transformer
import pytest

from oceanroute.api import create_app
from oceanroute.coordinate_transforms import transform_coordinates, _geographic_xy
from oceanroute.storage import ProjectStore


def request(source='EPSG:4326', target='EPSG:32650', points=None, **kwargs):
    return {'source_crs': source, 'target_crs': target,
            'points': points or [{'id': 'p1', 'x': 118, 'y': 22}], **kwargs}


def test_real_utm_known_value_and_reverse_with_metadata_no_mutation():
    payload = request(); before = deepcopy(payload)
    result = transform_coordinates(payload)
    assert result['can_apply'] and not result['errors'] and not result['warnings']
    assert payload == before
    point = result['points'][0]
    assert point['output']['x'] == pytest.approx(603224.6404290784, abs=1e-7)
    assert point['output']['y'] == pytest.approx(2433164.428653589, abs=1e-7)
    reverse = transform_coordinates(request('EPSG:32650', 'EPSG:4326',
                                            [{'id': 'p1', **point['output']}]))
    assert reverse['points'][0]['output'] == pytest.approx({'x': 118, 'y': 22}, abs=1e-10)
    assert result['source_crs']['axis_units'][0]['axis_name'] == 'Geodetic longitude'
    assert result['target_crs']['axis_units'][0]['conversion_to_m'] == 1
    assert result['axis_order'] == 'xy'
    assert result['operation']['ballpark'] is False
    assert result['operation']['best_available'] is True
    assert result['operation']['accuracy_m'] is None  # Unknown is never invented as zero.
    assert result['operations'][point['operation_index']] == result['operation']
    json.dumps(result, allow_nan=False)


def test_native_projected_feet_are_not_treated_as_metres():
    crs = CRS.from_proj4('+proj=utm +zone=50 +datum=WGS84 +units=ft +type=crs')
    result = transform_coordinates(request(target=crs.to_wkt()))
    output = result['points'][0]['output']
    assert result['target_crs']['axis_units'][0]['conversion_to_m'] == pytest.approx(.3048)
    assert output['x'] * .3048 == pytest.approx(603224.6404290784, abs=1e-7)
    restored = transform_coordinates(request(crs.to_wkt(), 'EPSG:4326', [{'x': output['x'], 'y': output['y']}]))
    assert restored['points'][0]['output'] == pytest.approx({'x': 118, 'y': 22})


def test_native_angular_grads_are_validated_and_transformed():
    wkt = CRS.from_epsg(4326).to_wkt().replace('ANGLEUNIT["degree",0.0174532925199433]',
                                             'ANGLEUNIT["grad",0.015707963267948967]')
    result = transform_coordinates(request(wkt, 'EPSG:4326', [{'x': 100, 'y': 50}]))
    assert result['can_apply']
    assert result['points'][0]['output'] == pytest.approx({'x': 90, 'y': 45})
    assert result['source_crs']['axis_units'][0]['name'] == 'grad'
    assert not transform_coordinates(request(wkt, 'EPSG:4326', [{'x': 201, 'y': 50}]))['can_apply']


def test_west_south_axis_directions_are_respected_in_area_checks():
    crs = CRS.from_proj4('+proj=longlat +datum=WGS84 +axis=wsu +type=crs')
    result = transform_coordinates(request(crs.to_wkt(), points=[{'x': -118, 'y': -22}]))
    assert result['can_apply'] and not result['warnings']
    assert result['points'][0]['output']['x'] == pytest.approx(603224.6404290784)
    assert result['points'][0]['output']['y'] == pytest.approx(2433164.428653589)


def test_prime_meridian_is_used_in_regional_location():
    # Real EPSG NTF (Paris) declares grads and Paris longitude, not Greenwich.
    crs = CRS.from_epsg(4807)
    assert _geographic_xy(crs, 0, 54) == pytest.approx((2.33722917, 48.6))
    result = transform_coordinates(request('EPSG:4807', 'EPSG:27572', points=[{'x': 0, 'y': 54}]))
    assert result['can_apply'] and not result['warnings']
    assert result['points'][0]['output'] == pytest.approx({'x': 600000, 'y': 2400140.151095362})


@pytest.mark.parametrize('source,target', [('EPSG:4979', 'EPSG:4326'), ('EPSG:4978', 'EPSG:4326'),
                                           ('EPSG:5703', 'EPSG:4326'), ('EPSG:7405', 'EPSG:4326'),
                                           ('EPSG:4326', 'EPSG:4979'), ('invalid_crs', 'EPSG:4326'),
                                           ('', 'EPSG:4326')])
def test_non_horizontal_or_bad_crs_rejected(source, target):
    with pytest.raises(ValueError):
        transform_coordinates(request(source, target))


@pytest.mark.parametrize('point', [None, {'x': True, 'y': 22}, {'x': '118', 'y': 22},
                                   {'x': float('inf'), 'y': 22}, {'x': float('nan'), 'y': 22},
                                   {'x': 1 << 2000, 'y': 22}, {'x': 181, 'y': 22},
                                   {'x': 118, 'y': 91}, {'x': 118},
                                   {'x': 118, 'y': 22, 'z': 30},
                                   {'id': '', 'x': 118, 'y': 22}, {'id': 5, 'x': 118, 'y': 22}])
def test_collect_preserves_invalid_row_and_blocks_partial_apply(point):
    payload = request(points=[{'id': 'good', 'x': 118, 'y': 22}, point])
    result = transform_coordinates(payload)
    assert not result['can_apply'] and len(result['points']) == 2 and len(result['errors']) == 1
    assert result['points'][0]['accepted'] is True
    assert result['points'][1]['accepted'] is False and result['points'][1]['output'] is None
    json.dumps(result, allow_nan=False)
    with pytest.raises(ValueError, match='点2'):
        transform_coordinates({**payload, 'error_policy': 'reject'})


def test_duplicate_ids_are_explicit_errors_but_optional_ids_are_allowed():
    result = transform_coordinates(request(points=[{'id': 'same', 'x': 118, 'y': 22}] * 2))
    assert not result['can_apply'] and result['errors'][0]['index'] == 1
    assert transform_coordinates(request(points=[{'x': 118, 'y': 22}] * 2))['can_apply']


def test_outside_declared_utm_region_warns_without_claiming_accuracy():
    result = transform_coordinates(request(points=[{'id': 'outside', 'x': 121, 'y': 22}]))
    assert result['can_apply'] and result['warnings'][0]['code'] == 'COORDINATE_OUTSIDE_AREA'
    assert result['warnings'][0]['id'] == 'outside'


@pytest.mark.parametrize('change', [{'points': []}, {'points': [{'x': 118, 'y': 22}]*10001},
                                    {'error_policy': 'skip'}, {'depth_m': 10}])
def test_batch_and_fields_have_explicit_limits(change):
    with pytest.raises(ValueError): transform_coordinates({**request(), **change})


def test_missing_best_grid_is_an_error_not_datum_fallback(monkeypatch):
    # PROJ grid availability is installation-specific; force only the unavailable
    # branch while normal tests above retain actual transforms and real values.
    class MissingGroup:
        best_available = False
        transformers = [object()]
    monkeypatch.setattr('oceanroute.coordinate_transforms.TransformerGroup', lambda *args, **kw: MissingGroup())
    result = transform_coordinates(request('EPSG:4267', 'EPSG:4326', [{'x': -75, 'y': 40}]))
    assert not result['can_apply'] and '网格不可用' in result['errors'][0]['message']
    assert not result['operations']


def test_real_http_preview_and_invalid_request_are_finite_and_do_not_save(tmp_path):
    store = ProjectStore(tmp_path/'coordinates.sqlite3')
    with TestClient(create_app(store)) as client:
        response = client.post('/api/coordinates/transform', json=request())
        assert response.status_code == 200, response.text
        assert response.json()['can_apply']
        assert client.get('/api/projects').json() == []
        wrong = client.post('/api/coordinates/transform', json=request(source='EPSG:4979'))
        assert wrong.status_code == 422 and '二维水平' in wrong.json()['detail']
        collected = client.post('/api/coordinates/transform', json=request(points=[{'x': 118, 'y': 95}]))
        assert collected.status_code == 200 and not collected.json()['can_apply']
        assert client.post('/api/coordinates/transform', json=[]).status_code == 422
