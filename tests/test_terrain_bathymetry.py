from copy import deepcopy
import json

from fastapi.testclient import TestClient
import numpy as np
from pyproj import Transformer
import pytest

from oceanroute.api import create_app
from oceanroute.bathymetry import BathymetryGrid
from oceanroute.storage import ProjectStore
from oceanroute.terrain_bathymetry import bathymetry_from_sources
from oceanroute.terrain_sources import example_sources


def config(**changes):
    return {'origin':[118,22], 'bounds_m':[-40,-40,40,40], 'nx':5, 'ny':5,
            'vertical_datum':'synthetic-demo-datum', 'sea_surface_height_m':1.25, **changes}


def low_project():
    project = example_sources()['project']
    project['terrain_sources'] = [project['terrain_sources'][0]]
    return project


def test_actual_library_nodes_projection_height_offset_provenance_no_mutation():
    project = low_project(); before = deepcopy(project)
    result = bathymetry_from_sources(project, config())
    assert project == before and result['can_apply'] and result['validation_status'] == 'research'
    grid = result['seabed_grid']; field = BathymetryGrid(grid)
    assert grid['x_m'] == [-40,-20,0,20,40] and grid['y_m'] == [-40,-20,0,20,40]
    assert np.array(grid['z_m']) == pytest.approx(np.full((5,5), -101.25), abs=1e-10)
    assert field.evaluate(np.array([[0.,0.], [20,-20]]), gradient=False) == pytest.approx([-101.25]*2)
    center = result['samples'][12]
    assert center['longitude'] == pytest.approx(118) and center['latitude'] == pytest.approx(22)
    assert center['source_id'] == 'example-background' and center['row_index'] == center['column_index'] == 2
    inv = Transformer.from_crs(grid['source']['horizontal_crs'],4326,always_xy=True)
    assert (result['samples'][0]['longitude'],result['samples'][0]['latitude']) == pytest.approx(inv.transform(-40,-40))
    assert result['derivation']['vertical_translation'] == 'z_model_m = -depth_m - sea_surface_height_m'
    assert result['derivation']['sea_surface_height_m'] == 1.25
    assert result['derivation']['missing_nodes'] == 0
    assert grid['source']['sha256'] == result['derivation']['library_signature']
    assert len(result['derivation']['grid_sha256']) == 64
    json.dumps(result, allow_nan=False)


def test_real_hole_fallback_and_missing_are_preserved_not_zero_filled():
    project = example_sources()['project']
    result = bathymetry_from_sources(project, config(bounds_m=[-8000,-8000,8000,8000],nx=5,ny=5,sea_surface_height_m=0))
    assert result['samples'][12]['fallback'] and result['samples'][12]['depth_m'] == pytest.approx(100)
    assert result['seabed_grid']['z_m'][0][0] is None
    assert result['derivation']['missing_nodes'] > 0 and not result['can_apply']
    assert any(w['code'] == 'BATHYMETRY_GRID_UNUSABLE' for w in result['warnings'])


def test_explicit_datum_selects_only_matching_sources_and_cannot_infer_alignment():
    project = low_project()
    result = bathymetry_from_sources(project, config(vertical_datum='different-datum'))
    assert result['derivation']['quality']['excluded_datum_source_ids'] == ['example-background']
    assert all(v is None for row in result['seabed_grid']['z_m'] for v in row)
    assert not result['can_apply']
    incomplete = config(); incomplete.pop('sea_surface_height_m')
    with pytest.raises(ValueError,match='sea_surface_height_m'): bathymetry_from_sources(project,incomplete)


def test_known_zero_or_above_model_sea_surface_is_not_clamped_to_fake_depth():
    project = low_project()
    source = project['terrain_sources'][0]
    source['text'] = source['text'].replace('100', '0')
    for key in ('fingerprint','content_sha256','byte_count'): source.pop(key,None)
    result = bathymetry_from_sources(project,config(sea_surface_height_m=0))
    assert all(v == 0 for row in result['seabed_grid']['z_m'] for v in row)
    assert not result['can_apply'] and 'outside its bounds' in result['validation']['error']


@pytest.mark.parametrize('changes', [{'bounds_m':[-1,-1,0,0],'nx':1000,'ny':1000}, {'bounds_m':[1,-1,2,1]},
                                     {'bounds_m':[-100001,-1,1,1]}, {'bounds_m':[0,0,1,0]},
                                     {'nx':True}, {'ny':3.5}, {'origin':[0,90]}, {'origin':[181,0]},
                                     {'sea_surface_height_m':None}, {'sea_surface_height_m':101},
                                     {'vertical_datum':''}, {'query_config':{'vertical_datum':'override'}},
                                     {'bounds_m':[-.0001,-.0001,.0001,.0001]}])
def test_grid_limits_and_explicit_metadata_rejected(changes):
    with pytest.raises(ValueError): bathymetry_from_sources(low_project(),config(**changes))


def test_explicit_query_budget_is_enforced_without_partial_grid():
    with pytest.raises(ValueError,match='预算'):
        bathymetry_from_sources(low_project(),config(query_config={'max_work_units':1}))


def test_actual_dynamic_uses_generated_grid_and_v2_checkpoint():
    from oceanroute.checkpoints import read_checkpoint
    from oceanroute.simulation import simulate_lay
    project = low_project(); source = project['terrain_sources'][0]
    source['text'] = source['text'].replace('100', '12')
    for key in ('fingerprint','content_sha256','byte_count'): source.pop(key,None)
    result = bathymetry_from_sources(project, config(sea_surface_height_m=0))
    assert result['can_apply']
    simulated = simulate_lay({}, {'seabed_grid':result['seabed_grid'], 'bottom_tension_n':10, 'wet_weight_n_m':4,
                                  'nodes':12,'duration_s':.5,'dt_s':.25,'internal_dt_s':.025,
                                  'ship_speed_m_s':.2,'payout_m_s':.4,'heading_deg':90,'solver_iterations':24})
    saved = read_checkpoint(simulated['checkpoint'])
    assert simulated['checkpoint']['schema_version'] == 2
    assert saved['config']['seabed_grid'] == result['seabed_grid']
    assert simulated['summary']['material_balance_residual_m'] < 1e-7
    assert saved['time_s'] == pytest.approx(.5)


def test_actual_http_generated_grid(tmp_path):
    with TestClient(create_app(ProjectStore(tmp_path/'bathymetry-api.sqlite3'))) as client:
        response = client.post('/api/terrain/bathymetry',json={'project':low_project(),'config':config()})
        assert response.status_code == 200,response.text
        assert response.json()['can_apply'] and len(response.json()['samples']) == 25
        wrong = client.post('/api/terrain/bathymetry',json={'project':low_project(),'config':{}})
        assert wrong.status_code == 422
