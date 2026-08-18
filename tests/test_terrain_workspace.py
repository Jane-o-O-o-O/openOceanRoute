"""Shared sources remain real, revisioned and stale-safe through full workspaces."""
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest

from oceanroute.api import create_app
from oceanroute.core import analyze_project
from oceanroute.storage import ProjectStore
from oceanroute.terrain_sources import example_sources, normalize_sources, profile_from_sources
from oceanroute.workspace import (WorkspaceError, analyze_workspace, export_workspace, import_workspace,
                                 materialize_path, migrate_project, workspace_action)
from oceanroute.workspace_storage import WorkspaceRevisionConflict, WorkspaceStore


def profiled():
    data = example_sources()
    p = data['project']
    # Distinct incidental waypoint depths must never rescue an invalidated survey.
    for row in p['route']['points']: row['depth_m'] = 55
    return profile_from_sources(p, data['config'])['project']


def warning_codes(result):
    return {w['code'] for w in result['warnings']}


def changed(project, kind):
    p = deepcopy(project)
    source = p['terrain_sources'][0]
    if kind == 'priority': source['priority'] += 1000
    elif kind == 'enabled': source['enabled'] = False
    elif kind == 'deleted': p['terrain_sources'].pop()
    elif kind == 'content':
        source['text'] = source['text'].replace('100', '101')
        for key in ('content_sha256', 'fingerprint', 'byte_count'): source.pop(key, None)
    elif kind == 'units':
        source['depth_units'] = 'ft'; source.pop('fingerprint', None)
    elif kind == 'datum':
        source['vertical_datum'] = 'new-declared-datum'; source.pop('fingerprint', None)
    else: raise AssertionError(kind)
    p['terrain_sources'] = normalize_sources(p['terrain_sources'])
    return p


def test_schema1_to_shared_workspace_source_bytes_and_profile_survive():
    p = profiled(); original = deepcopy(p)
    expected = analyze_project(p)
    result = migrate_project(p)
    ws, path = result['workspace'], result['project']
    assert p == original
    assert ws['terrain_sources'] == path['terrain_sources'] == p['terrain_sources']
    assert 'terrain_sources' not in ws['paths'][0]['project']
    assert path['profile'] == p['profile']
    assert analyze_project(path)['summary']['bottom_length_m'] == pytest.approx(expected['summary']['bottom_length_m'])
    assert path['terrain_sources'][1]['data_base64'] == p['terrain_sources'][1]['data_base64']


@pytest.mark.parametrize('kind', ['priority', 'enabled', 'deleted', 'content', 'units', 'datum'])
def test_each_source_mutation_invalidates_same_geometry_without_waypoint_rescue(kind):
    p = profiled(); altered = changed(p, kind)
    result = analyze_project(altered)
    assert 'TERRAIN_LIBRARY_STALE' in warning_codes(result)
    assert 'PROFILE_STALE' not in warning_codes(result)  # Geometry remains unchanged.
    assert result['summary']['bottom_length_m'] is None
    assert not result['profile_metadata']['imported_profile_valid']
    assert all(row['depth_m'] is None for row in result['profile'])


def test_library_reordering_and_name_edits_preserve_sampling_signature():
    p = profiled(); p['terrain_sources'].reverse(); p['terrain_sources'][0]['name'] = '改名资料'
    result = analyze_project(p)
    assert 'TERRAIN_LIBRARY_STALE' not in warning_codes(result)
    assert result['profile_metadata']['imported_profile_valid']
    assert result['summary']['bottom_length_m'] is not None


def test_shared_change_invalidates_all_paths_and_unshared_edit_is_rejected():
    initial = migrate_project(profiled())['workspace']
    copied = workspace_action(initial, {'action': 'copy_path', 'assembly_policy': 'alternative'})['workspace']
    active = materialize_path(copied)
    edited = changed(active, 'priority')
    with pytest.raises(WorkspaceError, match='WORKSPACE_SHARED_EDIT_REQUIRED'):
        workspace_action(copied, {'action': 'update_path', 'project': edited})
    updated = workspace_action(copied, {'action': 'update_path', 'project': edited, 'update_shared': True})
    assert len(updated['workspace']['paths']) == 2
    assert all(row['summary']['bottom_length_m'] is None for row in updated['analysis']['paths'])
    assert {w['path_id'] for w in updated['warnings'] if w['code'] == 'TERRAIN_LIBRARY_STALE'} == {p['id'] for p in copied['paths']}
    assert all('terrain_sources' not in p['project'] for p in updated['workspace']['paths'])
    assert updated['analysis']['summary']['procurement_cost'] == pytest.approx(analyze_workspace(copied)['summary']['procurement_cost'])


def test_update_shared_and_profile_resampling_recover_only_explicitly_selected_path():
    ws = migrate_project(profiled())['workspace']
    first = ws['active_path_id']
    ws = workspace_action(ws, {'action': 'copy_path', 'assembly_policy': 'alternative'})['workspace']
    replacement = changed(materialize_path(ws), 'priority')['terrain_sources']
    ws = workspace_action(ws, {'action': 'update_shared', 'terrain_sources': replacement})['workspace']
    new = profile_from_sources(materialize_path(ws), {'spacing_m': 200})['project']
    recovered = workspace_action(ws, {'action': 'update_path', 'project': new})
    rows = {p['path_id']: p for p in recovered['analysis']['paths']}
    assert rows[first]['summary']['bottom_length_m'] is None
    assert rows[ws['active_path_id']]['summary']['bottom_length_m'] is not None


def test_full_storage_restart_restore_revision_and_export_keep_source_data(tmp_path):
    db = tmp_path/'shared-terrain.sqlite3'; store = WorkspaceStore(db)
    ws = store.save(migrate_project(profiled())['workspace'])['workspace']
    first = deepcopy(ws)
    changed_ws = workspace_action(ws, {'action': 'update_shared', 'terrain_sources': changed(materialize_path(ws), 'enabled')['terrain_sources']})['workspace']
    second = store.save(changed_ws)['workspace']
    reopened = WorkspaceStore(db).get(ws['id'])
    assert reopened == second
    assert analyze_workspace(reopened)['summary']['deployment_bottom_length_m'] is None
    with pytest.raises(WorkspaceRevisionConflict): store.save(first)
    restored = WorkspaceStore(db).restore(ws['id'], 1, expected_revision=2)
    assert restored['saved_revision'] == 3 and restored['terrain_sources'] == first['terrain_sources']
    assert analyze_workspace(restored)['summary']['deployment_bottom_length_m'] is not None
    exported = export_workspace(restored)
    imported = import_workspace(exported)['workspace']
    assert imported['id'] != restored['id'] and imported['terrain_sources'] == first['terrain_sources']
    assert analyze_workspace(imported)['summary']['deployment_bottom_length_m'] is not None
    assert 'saved_revision' not in imported


def test_old_workspace_without_sources_is_compatible_and_child_private_copy_is_rejected():
    p = profiled(); p.pop('terrain_sources'); p.pop('profile')
    ws = migrate_project(p)['workspace']; ws.pop('terrain_sources')
    assert materialize_path(ws)['terrain_sources'] == []
    ws['paths'][0]['project']['terrain_sources'] = []
    with pytest.raises(WorkspaceError, match='WORKSPACE_SHARED_RESOURCE'): analyze_workspace(ws)


def test_actual_http_full_query_profile_library_stale_and_save_roundtrip(tmp_path):
    store = ProjectStore(tmp_path/'terrain-http.sqlite3')
    with TestClient(create_app(store)) as client:
        data = client.get('/api/terrain/sources/example').json()
        normalized = client.post('/api/terrain/sources/normalize', json={'sources': data['sources']})
        assert normalized.status_code == 200, normalized.text
        query = client.post('/api/terrain/query', json={'sources': data['sources'], 'points': data['points']})
        assert query.status_code == 200, query.text
        assert query.json()['samples'][1]['depth_m'] == pytest.approx(100)
        assert query.json()['samples'][1]['fallback'] and query.json()['samples'][3]['depth_m'] is None
        assert query.json()['quality']['library_signature'] == normalized.json()['library_signature']
        sampled = client.post('/api/terrain/profile', json={'project': data['project'], 'config': data['config']})
        assert sampled.status_code == 200, sampled.text
        p = sampled.json()['project']
        attached = client.post('/api/analyze', json=p)
        assert attached.status_code == 200 and attached.json()['summary']['bottom_length_m'] is not None
        stale = client.post('/api/analyze', json=changed(p, 'priority')).json()
        assert 'TERRAIN_LIBRARY_STALE' in warning_codes(stale) and stale['summary']['bottom_length_m'] is None
        invalid = client.post('/api/terrain/sources/normalize', json={'sources': data['sources'], 'path': '/anything'})
        assert invalid.status_code == 422
        saved = client.post('/api/projects', json=p)
        assert saved.status_code == 200, saved.text
        reloaded = client.get('/api/projects/'+saved.json()['project']['id']).json()
        assert reloaded['terrain_sources'] == p['terrain_sources']
        assert reloaded['profile'] == p['profile']
        json.dumps(query.json(), allow_nan=False)


def test_reverse_split_merge_preserve_source_binding_and_invalidate_after_mutation():
    from oceanroute.tools import merge_projects, reverse_project, split_project
    p = profiled()
    reversed_p = reverse_project(p)['project']
    assert reversed_p['profile']['samples'][-1]['source_id'] == p['profile']['samples'][0]['source_id']
    assert reversed_p['profile']['samples'][-1]['source_fingerprint'] == p['profile']['samples'][0]['source_fingerprint']
    assert 'TERRAIN_LIBRARY_STALE' in warning_codes(analyze_project(changed(reversed_p, 'priority')))
    total = analyze_project(p)['summary']['surface_length_m']
    parts = split_project(p, {'kp_m': total/2})['projects']
    for part in parts:
        assert analyze_project(part)['profile_metadata']['imported_profile_valid']
        assert 'TERRAIN_LIBRARY_STALE' in warning_codes(analyze_project(changed(part, 'priority')))
    merged = merge_projects(parts)['project']
    assert merged['profile']['metadata']['model'] == 'terrain-library-derived-profile-v1'
    assert analyze_project(merged)['profile_metadata']['imported_profile_valid']
    assert merged['profile']['samples'][0]['source_id'] == p['profile']['samples'][0]['source_id']
    stale = analyze_project(changed(merged, 'priority'))
    assert 'TERRAIN_LIBRARY_STALE' in warning_codes(stale) and stale['summary']['bottom_length_m'] is None
    with pytest.raises(ValueError, match='MERGE_TERRAIN_LIBRARY_CONFLICT'):
        merge_projects([parts[0], changed(parts[1], 'priority')])


def test_interpolated_split_station_is_marked_as_derived_not_fabricated_source_query():
    from oceanroute.tools import split_project
    p = profiled()
    # This KP is deliberately not one of the source query stations.
    kp = 123.456
    assert all(abs(row['kp_m']-kp) > .001 for row in p['profile']['samples'])
    parts = split_project(p, {'kp_m': kp})['projects']
    for row in (parts[0]['profile']['samples'][-1], parts[1]['profile']['samples'][0]):
        assert row['provenance_interpolated'] is True
        assert row['source_id'] is None and row['source_fingerprint'] is None
        assert row['depth_m'] is not None


def test_route_search_retained_profile_keeps_library_binding_for_later_edits():
    from pyproj import CRS, Transformer
    from oceanroute.routing import search_route
    p = profiled()
    crs = CRS.from_proj4('+proj=aeqd +lat_0=22 +lon_0=118 +datum=WGS84 +units=m')
    to_geo = Transformer.from_crs(crs,4326,always_xy=True)
    ring = [list(to_geo.transform(x,y)) for x,y in [(-100,-100),(100,-100),(100,100),(-100,100),(-100,-100)]]
    p['layers'] = [{'id':'blocked','kind':'restricted','geojson':{'type':'FeatureCollection','features':[{'type':'Feature','properties':{},'geometry':{'type':'Polygon','coordinates':[ring]}}]}}]
    found = search_route(p,{'grid_spacing_m':100,'padding_m':500,'clearance_m':100})['project']
    assert found['profile']['metadata']['model'] == 'terrain-library-derived-profile-v1'
    assert found['profile']['metadata']['terrain_library_signature'] == p['profile']['metadata']['terrain_library_signature']
    assert 'TERRAIN_LIBRARY_STALE' in warning_codes(analyze_project(changed(found,'priority')))
