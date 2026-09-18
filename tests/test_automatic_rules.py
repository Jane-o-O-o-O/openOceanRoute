"""Independent geographic-rule cases over actual WGS84 curves/native GIS.

Expected metric locations come from elementary equatorial geometry or an
independent Geod calculation; no JSON is substituted for a terrain query.
"""
from copy import deepcopy
import json
import math

from pyproj import Geod
import pytest

from oceanroute.automatic_rules import (AutomaticRuleEvaluationError,
    automatic_rule_catalog, check_automatic_rules, export_automatic_rules,
    import_automatic_rules, normalize_automatic_rules)
from oceanroute.core import route_signature
from oceanroute.workspace import migrate_project, validate_workspace

G=Geod(ellps='WGS84')


def workspace(points=None, geometries=None, bodies=None, cable_types=None):
    points=points or [(0,0),(.02,0)]
    cables=cable_types or [{'id':'A','name':'A','cost_per_m':1}]
    project={'schema_version':1,'id':'independent-rules','name':'Synthetic independent rules','crs':'EPSG:4326',
             'route':{'id':'route','curve':'geodesic','mode':'flexible','slack_pct':0,'slack_basis':'surface',
                      'points':[{'id':f'p{i}','longitude':p[0],'latitude':p[1],'depth_m':50} for i,p in enumerate(points)],
                      'legs':[{'cable_type_id':cables[min(i,len(cables)-1)]['id']} for i in range(len(points)-1)]},
             'cable_types':cables,'bodies':bodies or [],'layers':[{'id':'L','name':'Synthetic GIS','kind':'reference','visible':False,
             'geojson':{'type':'FeatureCollection','features':[{'type':'Feature','id':i,'properties':{},'geometry':geo} for i,geo in enumerate(geometries or [])]}}],
             'costs':{'currency':'CNY'}}
    return migrate_project(project)['workspace']


def crossing(ws,**extra):
    return {'id':'r','name':'Independent crossing','path_id':ws['active_path_id'],'kind':'crossing',
            'selectors':[{'layer_id':'L','feature_ids':None}],**extra}


def proximity(ws,**extra):
    return {'id':'r','path_id':ws['active_path_id'],'kind':'proximity','around':'path','targets':['gis'],
            'selectors':[{'layer_id':'L','feature_ids':None}],'distance_m':50,**extra}


def check(ws,rule,**config):
    before=deepcopy(ws)
    report=check_automatic_rules(ws,{'rules':[rule],**config})
    assert ws==before
    json.dumps(report,allow_nan=False)
    return report['results'][0]


def line(coords):return {'type':'LineString','coordinates':coords}
def point(x,y):return {'type':'Point','coordinates':[x,y]}


def test_equator_actual_location_angle_and_kp_range_end_overlap():
    ws=workspace(geometries=[line([[.01,-.001],[.01,.001]])])
    expected=6378137*math.radians(.01)
    row=check(ws,crossing(ws,start_kp_m=expected-1,end_kp_m=expected+1))
    assert row['status']=='violations' and len(row['violations'])==1
    hit=row['violations'][0]
    assert hit['kp_m']==pytest.approx(expected,abs=1e-6)
    assert hit['location']['longitude']==pytest.approx(.01,abs=1e-12)
    assert hit['values']['angle_deg']==pytest.approx(90,abs=1e-8)
    assert hit['location']['depth_m']==50


def test_polygon_hole_and_entry_exit_are_full_intervals():
    geo={'type':'Polygon','coordinates':[[[.002,-.002],[.018,-.002],[.018,.002],[.002,.002],[.002,-.002]],
                                      [[.007,-.001],[.007,.001],[.013,.001],[.013,-.001],[.007,-.001]]]}
    ws=workspace(geometries=[geo])
    row=check(ws,crossing(ws))
    assert [v['event'] for v in row['violations']]==['area_entry','area_exit','area_entry','area_exit']
    assert [v['location']['longitude'] for v in row['violations']]==pytest.approx([.002,.007,.013,.018],abs=1e-10)
    hole=workspace(points=[(.008,0),(.012,0)],geometries=[geo])
    assert check(hole,proximity(hole,distance_m=5))['status']=='clear'
    contained=workspace(points=[(.003,0),(.006,0)],geometries=[geo])
    assert check(contained,crossing(contained))['violations'][0]['event']=='containment'
    assert check(contained,proximity(contained,distance_m=0))['status']=='violations'


def test_recursive_geometry_collection_preserves_polygon_kind_and_path():
    geo={'type':'GeometryCollection','geometries':[point(.004,.01),{'type':'GeometryCollection','geometries':[
        {'type':'Polygon','coordinates':[[[.009,-.001],[.011,-.001],[.011,.001],[.009,.001],[.009,-.001]]]}]}]}
    ws=workspace(geometries=[geo])
    row=check(ws,crossing(ws))
    hits=row['violations']
    assert [h['event'] for h in hits]==['area_entry','area_exit']
    assert all(h['source']['primitive_path']==[1,0] for h in hits)


def test_v_vertex_touch_does_not_invent_zero_crossing_angle():
    ws=workspace(geometries=[line([[.009,.001],[.01,0],[.011,.001]])])
    row=check(ws,crossing(ws,conditions=[{'field':'angle_deg','comparison':'lt','value':35}]))
    assert row['status']=='unknown' and not row['violations']
    assert row['diagnostics'][0]['predicates'][0]['actual'] is None
    all_contacts=check(ws,crossing(ws))
    assert all_contacts['status']=='violations'
    assert all_contacts['violations'][0]['event']=='touch'


def test_overlap_angle_unknown_and_known_depth_can_trigger_any():
    ws=workspace(geometries=[line([[.004,0],[.012,0]])])
    cond=[{'field':'angle_deg','comparison':'lt','value':10},{'field':'depth_m','comparison':'gt','value':40}]
    assert check(ws,crossing(ws,conditions=cond))['status']=='unknown'
    row=check(ws,crossing(ws,conditions=cond,match_mode='any'))
    assert row['status']=='violations' and all(h['event']=='overlap' for h in row['violations'])


def test_date_line_short_route_and_native_long_edge_keep_distinct_semantics():
    short=workspace(points=[(179.99,0),(-179.99,0)],geometries=[line([[180,-.002],[180,.002]])])
    row=check(short,crossing(short))
    assert row['status']=='violations'
    assert row['violations'][0]['kp_m']==pytest.approx(6378137*math.radians(.01),abs=1e-5)
    # Native179 -> -179 is geographic-linear through0, not a short date-line edge.
    long=workspace(geometries=[line([[179,-.001],[-179,.001]])])
    row=check(long,crossing(long))
    assert row['violations'] and row['violations'][0]['location']['longitude']==pytest.approx(0,abs=1e-7)


def test_polar_overlay_explicit_unknown_while_continuous_point_distance_works():
    ws=workspace(points=[(0,86),(.01,86)],geometries=[line([[.005,85.9],[.005,86.1]])])
    row=check(ws,crossing(ws))
    assert row['status']=='unknown' and any(d['code']=='GEOMETRY_POLAR_SCOPE' for d in row['diagnostics'])


def test_true_same_geodesic_midpoint_is_not_altercourse_at_high_latitude():
    length=G.inv(0,70,90,70)[2]; bearing=G.inv(0,70,90,70)[0]
    lon,lat,_=G.fwd(0,70,bearing,length/2)
    ws=workspace(points=[(0,70),(lon,lat),(90,70)],geometries=[point(lon,lat)])
    row=check(ws,proximity(ws,around='altercourses',targets=['gis'],distance_m=1))
    assert row['status']=='clear' and row['coverage']['subject_count']==0


def test_whole_path_distance_uses_segments_not_only_control_or_sample_vertices():
    ws=workspace(geometries=[point(.00973,.0002)])
    row=check(ws,proximity(ws,distance_m=23))
    assert row['status']=='violations'
    hit=row['violations'][0]
    exact=G.inv(.00973,0,.00973,.0002)[2]
    assert hit['distance_m']==pytest.approx(exact,abs=.25)
    assert hit['distance_bounds_m'][0]<=exact<=hit['distance_bounds_m'][1]+1e-8
    assert hit['location']['longitude']==pytest.approx(.00973,abs=.000002)
    assert check(ws,proximity(ws,distance_m=21))['status']=='clear'


def test_parallel_line_nearest_bounds_and_near_threshold_are_not_fake_clear():
    ws=workspace(geometries=[line([[.002,.0002],[.018,.0002]])])
    row=check(ws,proximity(ws,distance_m=G.inv(.01,0,.01,.0002)[2]))
    assert row['status'] in {'unknown','violations'}
    assert check(ws,proximity(ws,distance_m=23))['status']=='violations'


def test_missing_native_typed_id_is_not_index_collision_and_ambiguity_is_saved():
    ws=workspace(geometries=[point(.01,.0001),point(.01,.0002),point(.01,.0003)])
    features=ws['layers'][0]['geojson']['features']
    features[0]['id']=1;features[1]['id']='1';features[2]['id']='index:0'
    catalog=automatic_rule_catalog(ws)['layers'][0]['features']
    assert catalog[0]['selector']['feature_ids']==[1]
    assert catalog[1]['selector']['feature_ids']==['1']
    assert catalog[2]['selector']['feature_ids']==['index:0']
    row=check(ws,proximity(ws,selectors=[{'layer_id':'L','feature_indexes':[0]}],distance_m=12))
    assert row['violations'][0]['target']['feature_index']==0
    features[1]['id']=1
    row=check(ws,proximity(ws,selectors=[{'layer_id':'L','feature_ids':[1]}]))
    assert row['status']=='reference_error' and row['diagnostics'][0]['code']=='FEATURE_REFERENCE_AMBIGUOUS'


def test_missing_refs_keep_valid_selection_real_evidence_and_diagnostic():
    ws=workspace(geometries=[line([[.01,-.001],[.01,.001]])])
    row=check(ws,crossing(ws,selectors=[{'layer_id':'L','feature_ids':[0]},{'layer_id':'missing','feature_ids':None}]))
    assert row['status']=='reference_error' and row['violations']
    assert any(d['code']=='LAYER_REFERENCE_MISSING' for d in row['diagnostics'])


def test_body_physical_station_maps_through_allowance_and_slack():
    ws=workspace(bodies=[{'id':'physical','cable_kp_m':600,'length_m':0,'cost':0}])
    project=ws['paths'][0]['project']
    # Rebuild from schema1 before migration so assembly associations remain exact.
    project={'schema_version':1,'id':'material-point','name':'material-point','crs':'EPSG:4326',
             'route':deepcopy(project['route']),'cable_types':deepcopy(ws['cable_types']),
             'bodies':[{'id':'physical','cable_kp_m':600,'length_m':0}], 'layers':[], 'costs':{'currency':'CNY'}}
    project['route']['slack_pct']=20
    project['route']['allowances']=[{'id':'reserve','kp_m':200,'length_m':120}]
    # 600 physical -120 allowance =480 ordinary /1.2 =400 surface KP.
    lon=math.degrees(400/6378137)
    project['layers']=[{'id':'L','kind':'reference','geojson':{'type':'FeatureCollection','features':[{'type':'Feature','id':0,'properties':{},'geometry':point(lon,0)}]}}]
    ws=migrate_project(project)['workspace']
    row=check(ws,proximity(ws,around='bodies',distance_m=.01))
    assert row['status']=='violations'
    assert row['violations'][0]['kp_m']==pytest.approx(400,abs=1e-6)


def test_compound_groups_and_self_exclusion_keep_distinct_colocated_entities():
    ws=workspace(points=[(0,0),(.01,0),(.01,.01)],bodies=[{'id':'a','kp_m':1000,'length_m':0},{'id':'b','kp_m':1000,'length_m':0}],
                 cable_types=[{'id':'A','cost_per_m':1},{'id':'B','cost_per_m':2}],geometries=[point(.01,0)])
    row=check(ws,proximity(ws,around='bodies',targets=['bodies','altercourses','transitions','gis'],distance_m=120))
    targets=[v['target'].get('kind','gis') for v in row['violations']]
    assert set(targets)=={'bodies','altercourses','transitions','gis'}
    pairs=[(v['subject']['id'],v['target'].get('id')) for v in row['violations'] if v['target'].get('kind')=='bodies']
    assert set(pairs)=={('a','b'),('b','a')}


def test_missing_depth_range_does_not_fill_zero_or_bridge_gap():
    ws=workspace(geometries=[point(.01,.0001)])
    project=ws['paths'][0]['project'];project['profile']={'route_signature':route_signature(project),'samples':[
        {'kp_m':0,'depth_m':50},{'kp_m':1000,'depth_m':None},{'kp_m':3000,'depth_m':50}]}
    row=check(ws,proximity(ws))
    assert row['status']=='incomplete' and not row['violations']
    assert row['coverage']['unknown_depth_ranges_m']


def test_water_depth_filter_clips_piecewise_profile_not_only_nearest_station():
    ws=workspace(geometries=[point(.001,.0001),point(.019,.0001)])
    project=ws['paths'][0]['project'];length=6378137*math.radians(.02)
    project['profile']={'route_signature':route_signature(project),'samples':[{'kp_m':0,'depth_m':0},{'kp_m':length,'depth_m':100}]}
    row=check(ws,proximity(ws,water_depth_m={'min_m':80,'max_m':None},distance_m=20))
    assert row['status']=='violations' and len(row['violations'])==1
    assert row['violations'][0]['target']['feature_index']==1
    assert row['coverage']['depth_eligible_ranges_m'][0][0]==pytest.approx(length*.8)


def test_null_end_is_dynamic_numeric_out_of_range_cannot_pass():
    ws=workspace(geometries=[point(.01,.01)])
    row=check(ws,crossing(ws,start_kp_m=3000,end_kp_m=None))
    assert row['status']=='unknown'
    assert row['requested_range_m']==[3000,None]
    row=check(ws,crossing(ws,end_kp_m=3000))
    assert row['status']=='incomplete'
    assert normalize_automatic_rules([crossing(ws)])[0]['end_kp_m'] is None


def test_saved_inline_side_missing_stale_slope_group_delegation():
    ws=workspace()
    rule={'id':'s','path_id':ws['active_path_id'],'kind':'slope','slope_basis':'both','max_inline_slope_deg':1,'max_side_slope_deg':1}
    row=check(ws,rule)
    assert row['status'] in {'unknown','incomplete'}
    assert row['components']['inline']['status']=='sampled_pass'
    assert row['components']['side']['status']=='unknown'


def terrain_plane(ws, gradient=(.1,.2)):
    center_lon=math.degrees(500/6378137)
    ws=deepcopy(ws)
    ws['terrain_sources']=[{'id':'plane','name':'Independent projected plane','kind':'xyz','enabled':True,'priority':1,
       'source_crs':f'+proj=aeqd +lat_0=0 +lon_0={center_lon} +datum=WGS84 +units=m +type=crs',
       'depth_positive':'down','depth_units':'m','vertical_datum':'synthetic-plane',
       'sampling':{'method':'linear','max_gap_m':1000},
       'text':'\n'.join(f'{x} {y} {1000+gradient[0]*x+gradient[1]*y}' for y in (-200,0,200) for x in (-200,0,200))}]
    return validate_workspace(ws)


def test_body_slopes_actual_2d_gradient_witness_and_radius_center():
    ws=terrain_plane(workspace(bodies=[{'id':'body','kp_m':500,'length_m':0}]))
    rule=proximity(ws,around='bodies',targets=['slopes'],selectors=None,distance_m=50,slope_threshold_deg=10,slope_probe_spacing_m=50,slope_vertical_datum='synthetic-plane')
    rule.pop('selectors')
    row=check(ws,rule)
    assert row['status']=='violations'
    expected=math.degrees(math.atan(math.hypot(.1,.2)))
    assert all(v['slope_deg']==pytest.approx(expected,abs=1e-5) for v in row['violations'])
    for violation in row['violations']:
        assert violation['location']['depth_m'] is None
        assert violation['sampled_witness']['depth_m'] is not None
        assert violation['source_fingerprint']==violation['sampled_witness']['source_fingerprint']
        assert violation['location']['radius_center']['longitude']==pytest.approx(math.degrees(500/6378137),abs=1e-12)
    assert row['coverage']['terrain_sampling_only'] and not row['coverage']['continuous_bed_verified']


@pytest.mark.parametrize('change',[
    {'kind':'other'},{'enabled':1},{'start_kp_m':True},{'end_kp_m':0},
    {'conditions':[{'field':'angle_deg','comparison':'lt','value':91}]},
    {'conditions':[{'field':'depth_m','comparison':'eq','value':1}]},
    {'selectors':[{'layer_id':'L','feature_ids':[True]}]},
    {'selectors':[{'layer_id':'L','feature_ids':[1,1.0]}]},
    {'selectors':[{'layer_id':'L','feature_ids':None,'feature_indexes':[0]}]},
    {'unknown':1}])
def test_strict_schema_invalid_declarations_reject_whole(change):
    ws=workspace()
    with pytest.raises(ValueError): normalize_automatic_rules([crossing(ws,**change)])


@pytest.mark.parametrize('change',[
    {'targets':[{}]},{'targets':['bodies']},{'around':'transitions','targets':['slopes']},
    {'water_depth_m':{'min_m':None,'max_m':None}},{'distance_m':-1},
    {'around':'bodies','targets':['slopes'],'distance_m':0,'slope_threshold_deg':10},
    {'slope_threshold_deg':1}])
def test_strict_proximity_matrix_and_nonfinite_rejected(change):
    ws=workspace()
    with pytest.raises(ValueError):normalize_automatic_rules([proximity(ws,**change)])


@pytest.mark.parametrize('config',[{'max_work_units':1},{'max_vertices':1},{'max_pairs':1},{'max_output_bytes':1024}])
def test_budgets_raise_typed_error_not_truncated_clear(config):
    ws=workspace(geometries=[line([[.01,-.001],[.01,.001]]),line([[.015,-.001],[.015,.001]])])
    with pytest.raises(AutomaticRuleEvaluationError):check(ws,crossing(ws),**config)


def test_import_export_and_duplicate_policy_preserve_complete_revision():
    ws=workspace(geometries=[line([[.01,-.001],[.01,.001]])]);ws['saved_revision']=7
    ws['automatic_rules']=[crossing(ws)]
    package=export_automatic_rules(ws)['package']
    original=deepcopy(ws)
    with pytest.raises(ValueError):import_automatic_rules(ws,package)
    result=import_automatic_rules(ws,json.dumps(package),{'duplicate_ids':'rename'})
    assert result['workspace']['saved_revision']==7 and len(result['rules'])==2
    assert result['report']['renamed_ids']==[{'from':'r','to':'r-2'}]
    assert {k:v for k,v in result['workspace'].items() if k!='automatic_rules'}=={k:v for k,v in ws.items() if k!='automatic_rules'}
    assert ws==original
    assert json.loads(export_automatic_rules(result['workspace'])['text'])['rules']==result['rules']


def test_512_disabled_rules_persist_and_default128_rejects_without_truncation():
    ws=workspace()
    rules=[crossing(ws,id=f'r{i}',enabled=False) for i in range(512)]
    ws['automatic_rules']=normalize_automatic_rules(rules)
    with pytest.raises(ValueError):check_automatic_rules(ws)
    result=check_automatic_rules(ws,{'max_rules':512})
    assert result['summary']['disabled_rules']==512 and len(result['results'])==512
    with pytest.raises(ValueError):normalize_automatic_rules(rules+[crossing(ws,id='r513')])


def test_closed_single_depth_filter_includes_true_intermediate_point():
    ws=workspace(points=[(0,0),(.01,0)],geometries=[point(.005,.0001)])
    project=ws['paths'][0]['project'];length=6378137*math.radians(.01)
    project['profile']={'route_signature':route_signature(project),'samples':[{'kp_m':0,'depth_m':0},{'kp_m':length,'depth_m':100}]}
    row=check(ws,proximity(ws,distance_m=100,water_depth_m={'min_m':50,'max_m':50}))
    assert row['status']=='violations' and len(row['violations'])==1
    assert row['violations'][0]['kp_m']==pytest.approx(length/2,abs=1e-6)
    assert row['violations'][0]['location']['depth_m']==pytest.approx(50)


def test_repeated_vertices_never_create_two_virtual_turns_or_transitions():
    ws=workspace(points=[(0,0),(.005,0),(.005,0),(.005,.005)],geometries=[point(.005,0)],
                 cable_types=[{'id':'A','cost_per_m':1},{'id':'B','cost_per_m':1},{'id':'C','cost_per_m':1}])
    for kind in ('altercourses','transitions'):
        row=check(ws,proximity(ws,around=kind,distance_m=1))
        assert not row['violations'] and row['coverage']['subject_count']==0


def test_exact_zero_radius_path_line_contact_is_not_replaced_with_sparse_nearness():
    ws=workspace(geometries=[line([[.01003,-.002],[.01003,.002]])])
    row=check(ws,proximity(ws,distance_m=0))
    assert row['status']=='violations' and row['violations'][0]['distance_m']==0


def test_geometry_signature_excludes_display_but_input_keeps_material_changes():
    ws=workspace(geometries=[point(.01,.01)])
    first=check_automatic_rules(ws,{'rules':[crossing(ws)]})
    ws['layers'][0].update(visible=True,opacity=.1,display_order=9)
    second=check_automatic_rules(ws,{'rules':[crossing(ws)]})
    assert first['metadata']['geometry_signature']==second['metadata']['geometry_signature']
    assert first['metadata']['input_signature']==second['metadata']['input_signature']
    ws['paths'][0]['project']['name']='Changed editorial label'
    third=check_automatic_rules(ws,{'rules':[crossing(ws)]})
    assert second['metadata']['geometry_signature']==third['metadata']['geometry_signature']
    assert second['metadata']['input_signature']!=third['metadata']['input_signature']
    assert third['budget']['admission_exclusions']==['workspace_json_size_checks_and_canonical_serialization','workspace_shared_source_normalization','core_route_densification_and_profile_material_manufacture_currency_relationship_validation','pure_source_gis_admission_before_checker']
    assert third['budget']['output_bytes']==len(json.dumps(third,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode())


def test_null_native_id_uses_explicit_index_without_invented_typed_id():
    ws=workspace(geometries=[point(.01,.0001)])
    ws['layers'][0]['geojson']['features'][0]['id']=None
    catalog=automatic_rule_catalog(ws)
    assert catalog['layers'][0]['features'][0]['selector']=={'layer_id':'L','feature_indexes':[0]}
    assert check(ws,proximity(ws,selectors=[{'layer_id':'L','feature_indexes':[0]}],distance_m=12))['status']=='violations'


def test_large_distinct_native_integer_ids_never_collapse_through_float():
    ws=workspace(geometries=[point(.01,.0001),point(.01,.001)])
    for feature,value in zip(ws['layers'][0]['geojson']['features'],(2**53,2**53+1)):feature['id']=value
    catalog=automatic_rule_catalog(ws)['layers'][0]['features']
    assert all(row['id_unique'] for row in catalog)
    row=check(ws,proximity(ws,selectors=[{'layer_id':'L','feature_ids':[2**53+1]}],distance_m=20))
    assert row['status']=='clear' and not row['violations']


@pytest.mark.parametrize('field,value',[('kind',[]),('match_mode',{}),('body_distance_mode',[]),('around',[])])
def test_non_scalar_enum_values_reject_as_valueerror_not_internal_typeerror(field,value):
    ws=workspace()
    rule=proximity(ws,**{field:value}) if field=='around' else crossing(ws,**{field:value})
    with pytest.raises(ValueError):normalize_automatic_rules([rule])


def test_curve_chunk_budget_has_real_effect_and_old_projection_field_rejected():
    ws=workspace(points=[(0,0),(.05,0)],geometries=[line([[.021,-.001],[.021,.001]])])
    wide=check_automatic_rules(ws,{'rules':[crossing(ws)],'max_curve_chunk_m':100000})
    narrow=check_automatic_rules(ws,{'rules':[crossing(ws)],'max_curve_chunk_m':1000})
    assert narrow['budget']['pairs']>wide['budget']['pairs']
    assert narrow['results'][0]['violations'][0]['kp_m']==pytest.approx(wide['results'][0]['violations'][0]['kp_m'],abs=1e-6)
    with pytest.raises(ValueError):check_automatic_rules(ws,{'rules':[crossing(ws)],'max_projection_radius_m':100000})


def slope_window(ws,threshold=10):
    return {'id':'window','path_id':ws['active_path_id'],'kind':'proximity','around':'bodies','targets':['slopes'],
            'distance_m':50,'slope_threshold_deg':threshold,'slope_probe_spacing_m':50,'slope_vertical_datum':'synthetic-plane'}


def test_actual_2d_nodata_cannot_be_replaced_with_complete_flat_route_profile():
    ws=terrain_plane(workspace(bodies=[{'id':'body','kp_m':500,'length_m':0}]),(.001,.002))
    ws['terrain_sources'][0]['text']='\n'.join(f'{x} {y} {1000+.001*x+.002*y}' for y in (-20,0,20) for x in (-20,0,20))
    for source in ws['terrain_sources']:
        for key in ('content_sha256','byte_count','fingerprint'):source.pop(key,None)
    row=check(ws,slope_window(ws))
    assert row['status']=='incomplete' and not row['violations']
    quality=row['coverage']['slope_neighborhoods'][0]['quality']
    assert not quality['sample_complete'] and quality['missing_triangle_count']>0
    assert any(d['code']=='TERRAIN_SLOPE_WINDOW_INCOMPLETE' for d in row['diagnostics'])


def test_actual_2d_source_boundary_retains_uncertainty_without_bridging_faces():
    ws=terrain_plane(workspace(bodies=[{'id':'body','kp_m':500,'length_m':0}]),(.001,.002))
    upper=deepcopy(ws['terrain_sources'][0]);upper.update(id='half',priority=2)
    upper['text']='\n'.join(f'{x} {y} {1000+.001*x+.002*y}' for y in (-200,0,200) for x in (-200,-100,0))
    ws['terrain_sources'].append(upper)
    for source in ws['terrain_sources']:
        for key in ('content_sha256','byte_count','fingerprint'):source.pop(key,None)
    row=check(ws,slope_window(ws))
    assert row['status']=='incomplete' and not row['violations']
    quality=row['coverage']['slope_neighborhoods'][0]['quality']
    assert quality['source_boundary'] and quality['source_boundary_triangle_count']>0
    assert quality['sample_complete'] and not quality['triangle_complete']


def test_all_rule_terrain_work_is_preflighted_before_any_actual_query(monkeypatch):
    from oceanroute.terrain_slope_neighborhoods import SlopeNeighborhoodSampler
    ws=terrain_plane(workspace(bodies=[{'id':'body','kp_m':500,'length_m':0}]))
    called=[]
    original=SlopeNeighborhoodSampler.sample_many
    def observe(self,windows):
        called.append(len(windows));return original(self,windows)
    monkeypatch.setattr(SlopeNeighborhoodSampler,'sample_many',observe)
    with pytest.raises(AutomaticRuleEvaluationError,match='TERRAIN_BUDGET'):
        check(ws,slope_window(ws),max_work_units=100)
    assert called==[]


def test_two_rule_windows_charge_real_queries_and_cache_separately():
    ws=terrain_plane(workspace(bodies=[{'id':'body','kp_m':500,'length_m':0}]))
    first=slope_window(ws);second={**first,'id':'second','slope_threshold_deg':89}
    report=check_automatic_rules(ws,{'rules':[first,second]})
    assert report['results'][0]['status']=='violations'
    assert report['results'][1]['status']=='sampled_pass'
    assert report['budget']['terrain_query_count']>0
    assert report['results'][0]['coverage']['slope_neighborhoods'][0]['quality']['continuous_bed_verified'] is False


def test_whole_rules_terrain_window_limit_rejects_instead_of_truncating():
    ws=terrain_plane(workspace(bodies=[{'id':f'body{i}','kp_m':500,'length_m':0} for i in range(201)]))
    with pytest.raises(AutomaticRuleEvaluationError,match='TERRAIN_WINDOW_LIMIT'):
        check(ws,slope_window(ws))


def test_delegated_slope_budget_uses_typed_unavailability_without_fake_clear():
    ws=workspace()
    rule={'id':'s','path_id':ws['active_path_id'],'kind':'slope','slope_basis':'inline','max_inline_slope_deg':1}
    with pytest.raises(AutomaticRuleEvaluationError,match='SLOPE_RULE_WORK_BUDGET'):
        check(ws,rule,max_work_units=95)


def test_reported_triangle_gradient_rebuilds_from_three_actual_queries():
    ws=terrain_plane(workspace(bodies=[{'id':'body','kp_m':500,'length_m':0}]))
    row=check(ws,slope_window(ws))
    for violation in row['violations']:
        a,b,c=violation['queried_vertices']
        dx1,dy1=b['x_m']-a['x_m'],b['y_m']-a['y_m']
        dx2,dy2=c['x_m']-a['x_m'],c['y_m']-a['y_m']
        dz1,dz2=a['depth_m']-b['depth_m'],a['depth_m']-c['depth_m']
        det=dx1*dy2-dx2*dy1
        gx=(dz1*dy2-dz2*dy1)/det;gy=(dx1*dz2-dx2*dz1)/det
        assert violation['gradient_height']==pytest.approx([gx,gy],abs=1e-10)
        assert violation['slope_deg']==pytest.approx(math.degrees(math.atan(math.hypot(gx,gy))),abs=1e-10)
        assert [v['index'] for v in violation['queried_vertices']]==violation['vertex_indices']
        assert all(v['depth_m'] is not None and v['source_id']=='plane' for v in violation['queried_vertices'])


def test_unselected_invalid_geographic_coordinates_are_not_admitted_by_skip_screen():
    ws=workspace(geometries=[point(.01,.01)])
    ws['layers'][0]['geojson']['features'][0]['geometry']['coordinates']=[181,0]
    with pytest.raises(ValueError):check_automatic_rules(ws,{'rules':[]})


def test_invalid_collection_child_keeps_valid_sibling_violation_and_unknown():
    ws=workspace(geometries=[{'type':'GeometryCollection','geometries':[
        line([[.01,-.001],[.01,.001]]),
        {'type':'Polygon','coordinates':[[[.005,-.002],[.015,.002],[.005,.002],[.015,-.002],[.005,-.002]]]}]}])
    row=check(ws,crossing(ws))
    assert row['status']=='violations' and row['violations'][0]['source']['primitive_path']==[0]
    assert row['diagnostics'][0]['code']=='GIS_GEOMETRY_UNAVAILABLE'
    assert row['diagnostics'][0]['primitive_path']==[1]
