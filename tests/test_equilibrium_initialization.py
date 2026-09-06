"""Physical/material acceptance of verified statics feeding actual dynamics."""
from copy import deepcopy
import json

import numpy as np
import pytest

from oceanroute.checkpoints import _digest, read_checkpoint
from oceanroute.initial_equilibrium import resolve_initial_equilibrium, estimate_initial_equilibrium_work
from oceanroute.simulation import simulate_lay
from oceanroute.voyage import run_voyage


def bed(curved=False):
    axis=[-100.,0.,100.]
    return {"schema":"oceanroute.bathymetry.v1","x_m":axis,"y_m":axis,
            "z_m":[[(-40+.1*x+.05*y+.0003*x*y) if curved else -100. for x in axis] for y in axis],
            "source":{"name":"explicit synthetic curved bed" if curved else "explicit synthetic deep flat bed",
                      "horizontal_crs":"LOCAL_CARTESIAN_METRES","origin_projected_m":[0,0],
                      "vertical_datum":"already model sea surface z=0"}}


def analytic_config(**changes):
    # Independent discrete free-cable balance: segment vertical force equals
    # bottom reaction plus the half-segment-weight material midpoint above it.
    rest=np.array([12.,2.,3.,1.,4.,2.,3.])
    middle=np.cumsum(rest[::-1])[::-1]-rest/2
    f=np.column_stack((np.full(7,150.),np.zeros(7),15+4*middle))
    tension=np.linalg.norm(f,axis=1)
    vessel=np.array([3.,-4.,-2.])
    segments=rest[:,None]*(1+tension[:,None]/1e4)*f/tension[:,None]
    positions=vessel-np.vstack((np.zeros(3),np.cumsum(segments,axis=0)))
    c={"seabed_grid":bed(),"nodes":8,"wet_weight_n_m":4.,"ea_n":1e4,"seabed_friction":.7,
       "initial_suspended_material_m":37.,"ship_speed_m_s":0.,"payout_m_s":0.,
       "duration_s":.1,"dt_s":.05,"internal_dt_s":.004,"solver_iterations":24,
       "initial_equilibrium":{"schema":"oceanroute.dynamic.initial-equilibrium.v1",
                              "vessel_position_m":vessel.tolist(),"anchor_position_m":positions[-1].tolist(),
                              "rest_lengths_m":rest.tolist(),"initial_positions_m":positions.tolist()}}
    c.update(changes)
    return c,positions,rest,tension


def curved_config(**changes):
    c={"seabed_grid":bed(True),"nodes":18,"wet_weight_n_m":4.,"ea_n":1e4,"seabed_friction":.5,
       "ship_speed_m_s":0.,"payout_m_s":0.,"duration_s":.2,"dt_s":.1,"internal_dt_s":.01,"solver_iterations":24,
       "initial_suspended_material_m":125.,"initial_equilibrium":{"schema":"oceanroute.dynamic.initial-equilibrium.v1",
       "vessel_position_m":[0,0,0],"anchor_position_m":[-60,-10,-46.32],"natural_length_m":80}}
    c.update(changes);return c


def test_actual_curved_bed_equilibrium_maps_full_material_and_independent_force_balance():
    c=curved_config();r=simulate_lay({},c);p=np.array(r['frames'][0]['nodes'])
    state=r['initialization']['initial_snapshot'];rest=np.array(state['rest_lengths_m'])
    length=np.linalg.norm(np.diff(p,axis=0),axis=1);t=1e4*np.maximum(length/rest-1,0)
    internal=np.zeros_like(p);sf=t[:,None]*np.diff(p,axis=0)/length[:,None]
    internal[:-1]+=sf;internal[1:]-=sf
    nodalweight=4*np.r_[rest[0]/2,(rest[:-1]+rest[1:])/2,rest[-1]/2]
    normal=np.array(r['frames'][0]['node_seabed_normal']);forces=internal.copy();forces[:,2]-=nodalweight
    forces+=np.array(state['node_contact_normal_force_n'])[:,None]*normal
    forces+=np.array(state['node_boundary_force_n'])
    assert np.max(np.linalg.norm(forces,axis=1))<.01
    assert rest.sum()==pytest.approx(80)
    assert state['node_wet_weight_n']==pytest.approx(nodalweight)
    assert state['node_material_m'][0]==pytest.approx(205)
    assert state['node_material_m'][-1]==pytest.approx(125)
    assert r['summary']['paid_out_m']==0
    assert r['summary']['material_balance_residual_m']<1e-10
    assert r['checkpoint']['schema_version']==3
    assert r['checkpoint']['model']=='material-lumped-mass-xpbd-cable-lay-v4'
    assert r['checkpoint']['numerical']['scheme']=='implicit-compliant-material-nodes-equilibrium-prestress-v4'
    assert all(x==0 for x in r['frames'][0]['node_contact_normal_impulse_n_s'])
    assert r['initialization']['verification']['segment_clearance']['minimum_clearance_m']>=-1e-8
    assert r['solver']['charged_normalized_work_units']==r['solver']['estimated_work_units']+r['solver']['initialization_work']['estimated_work_units_this_run']
    assert 'INITIAL_BATHYMETRY_APPROXIMATION' not in {w['code'] for w in r['warnings']}
    json.dumps(r,allow_nan=False)


def test_nonuniform_natural_elements_and_prestress_are_exact_fixed_point_without_actuation():
    c,p,rest,tension=analytic_config();r=simulate_lay({},c)
    for f in r['frames']:
        assert np.max(np.linalg.norm(np.array(f['nodes'])-p,axis=1))<1e-9
        assert np.max(np.linalg.norm(f['node_velocity_m_s'],axis=1))<1e-8
        assert f['segment_tension_n']==pytest.approx(tension,abs=1e-7)
        assert f['touchdown'] is None and f['bottom_tension_n'] is None
        assert f['touchdown_detected'] is False
        assert f['anchor_position_m']==pytest.approx(p[-1])
        assert f['anchor_segment_tension_n']==pytest.approx(tension[-1],abs=1e-7)
    assert r['checkpoint']['state']['rest_lengths_m']==rest.tolist()
    assert r['checkpoint']['state']['segment_target_m']==12
    assert len(r['frames'][-1]['nodes'])==8  # no zero-feed subdivision
    assert r['frames'][-1]['ship']==pytest.approx([3,-4,-2])


def test_inertia_and_diameter_may_vary_when_true_initial_force_law_is_uniform():
    c,_,rest,_=analytic_config(added_mass_coefficient=0)
    c['material_segments']=[{'start_m':0,'end_m':50,'wet_weight_n_m':4,'ea_n':1e4,'mass_kg_m':1.,'diameter_m':.01},
                            {'start_m':50,'end_m':200,'wet_weight_n_m':4,'ea_n':1e4,'mass_kg_m':2.,'diameter_m':.04}]
    r=simulate_lay({},c);initial=r['initialization']['initial_snapshot']
    assert sum(initial['node_dry_mass_kg'])==pytest.approx(13*1+14*2)
    assert sum(initial['node_mass_kg'])==pytest.approx(41)
    assert sum(initial['node_wet_weight_n'])==pytest.approx(4*rest.sum())
    assert r['solver']['converged']


@pytest.mark.parametrize('change,match',[
    ({'current_x_m_s':.01},'zero initial current'),
    ({'current_profile':[{'depth_m':0,'x_m_s':0},{'depth_m':100,'x_m_s':.01}]},'every current_profile'),
    ({'ei_n_m2':1},'zero EI'),
    ({'inline_bodies':[{'material_m':45,'length_m':1,'mass_kg':1,'wet_weight_n':1}]},'finite'),
    ({'material_segments':[{'start_m':0,'end_m':50,'wet_weight_n_m':4,'ea_n':1e4},{'start_m':50,'end_m':200,'wet_weight_n_m':5,'ea_n':1e4,'ei_n_m2':1}]},'zero EI'),
    ({'material_segments':[{'start_m':0,'end_m':50,'wet_weight_n_m':4,'ea_n':1e4,'ei_n_m2':1},{'start_m':50,'end_m':200,'wet_weight_n_m':4,'ea_n':2e4}]},'zero EI'),
])
def test_unsupported_initial_force_laws_are_rejected_without_flattening(change,match):
    c,*_=analytic_config(**change)
    with pytest.raises(ValueError,match=match):simulate_lay({},c)


def test_future_mixed_material_and_body_are_actually_paid_once_after_verified_initial_snapshot():
    c,_,rest,_=analytic_config(duration_s=.2,dt_s=.05,internal_dt_s=.01,
        ship_plan=[{'time_s':.02,'speed_m_s':.1,'payout_m_s':2}])
    c['material_segments']=[{'start_m':0,'end_m':64,'wet_weight_n_m':4,'ea_n':1e4},
                            {'start_m':64,'end_m':200,'wet_weight_n_m':7,'ea_n':2e4}]
    c['inline_bodies']=[{'material_m':64.15,'mass_kg':5,'wet_weight_n':20}]
    r=simulate_lay({},c);initial=r['frames'][0];last=r['frames'][-1]
    assert initial['paid_out_m']==0 and initial['node_material_m'][0]==pytest.approx(64)
    assert sum(initial['node_wet_weight_n'])==pytest.approx(108)
    assert last['paid_out_m']==pytest.approx(.36)
    assert last['node_material_m'][0]==pytest.approx(64.36)
    assert sum(last['node_wet_weight_n'])==pytest.approx(108+7*.36+20)
    assert r['summary']['material_balance_residual_m']<1e-10
    assert np.linalg.norm(np.array(last['nodes'])-np.array(initial['nodes']))>1e-6


def test_dynamic_budget_is_checked_before_expensive_static_optimizer(monkeypatch):
    import oceanroute.static_bathymetry as module
    calls=[];original=module.static_equilibrium
    def tracked(c):calls.append(c);return original(c)
    monkeypatch.setattr(module,'static_equilibrium',tracked)
    c,*_=analytic_config(max_work_units=1)
    with pytest.raises(ValueError,match='simulation exceeds computation limit'):simulate_lay({},c)
    assert calls==[]


def test_nonconvergence_and_raw_accepted_result_cannot_enter_initial_state():
    c=curved_config();c['initial_equilibrium']['solver']={'max_solver_iterations':1}
    with pytest.raises(ValueError,match='static solve was not accepted'):simulate_lay({},c)
    c,*_=analytic_config();c['initial_equilibrium']['accepted']=True
    with pytest.raises(ValueError,match='unsupported inputs'):simulate_lay({},c)
    c,*_=analytic_config();c['initial_equilibrium']['solver']={'contact_tolerance_m':1e-6}
    with pytest.raises(ValueError,match='contact_tolerance_m'):simulate_lay({},c)


def test_serialized_midflight_checkpoint_resumes_actual_state_without_static_optimizer(monkeypatch):
    import oceanroute.static_bathymetry as module
    c=curved_config(duration_s=.4,ship_plan=[{'time_s':.1,'speed_m_s':.1,'payout_m_s':.12}],ship_plan_horizon_s=.4)
    direct=simulate_lay({},c)
    first=simulate_lay({},dict(c,duration_s=.2))
    def no_reoptimization(_):raise AssertionError('a real checkpoint must not rerun the static optimizer')
    monkeypatch.setattr(module,'static_equilibrium',no_reoptimization)
    restored=simulate_lay({}, {'resume_state':json.loads(json.dumps(first['checkpoint'])),'duration_s':.2})
    assert np.array(restored['frames'][-1]['nodes'])==pytest.approx(np.array(direct['frames'][-1]['nodes']),abs=1e-12)
    assert np.array(restored['frames'][-1]['node_velocity_m_s'])==pytest.approx(np.array(direct['frames'][-1]['node_velocity_m_s']),abs=1e-11)
    assert restored['checkpoint']['state']['rest_lengths_m']==pytest.approx(direct['checkpoint']['state']['rest_lengths_m'],abs=1e-13)
    assert restored['frames'][-1]['segment_tension_n']==pytest.approx(direct['frames'][-1]['segment_tension_n'],abs=1e-9)
    assert restored['initialization']==first['initialization']
    assert restored['solver']['initialization_work']['static_optimizer_run_this_call'] is False
    assert restored['solver']['initialization_work']['estimated_work_units_this_run']==0
    assert restored['solver']['initialization_work']['checkpoint_proof_verification_work_this_run']>0


@pytest.mark.parametrize('mutate',[
    lambda cp:cp['state']['initialization_provenance']['verification'].__setitem__('max_node_force_residual_n',0.),
    lambda cp:cp['state']['initialization_provenance']['initial_snapshot']['positions'][3].__setitem__(2,-120.),
    lambda cp:cp['state']['initialization_provenance']['initial_snapshot']['node_mass_kg'].__setitem__(3,1.),
    lambda cp:cp['state']['initialization_provenance']['initial_snapshot']['segment_tension_n'].__setitem__(3,0.),
    lambda cp:cp['state'].__setitem__('initial_material_length_m',30.),
    lambda cp:cp.__setitem__('schema_version',2),
])
def test_checksum_recomputed_false_physical_provenance_still_rejected(mutate):
    c=curved_config();cp=simulate_lay({},c)['checkpoint'];mutate(cp);cp['checksum_sha256']=_digest(cp)
    with pytest.raises(ValueError):read_checkpoint(cp)


def test_initial_geometry_no_data_or_coverage_fails_instead_of_flat_fallback():
    c=curved_config();c['seabed_grid']['z_m'][0][0]=None
    with pytest.raises(ValueError,match='NoData'):simulate_lay({},c)
    c=curved_config();c['initial_equilibrium']['anchor_position_m']=[-101,-10,-46.32]
    with pytest.raises(ValueError,match='outside'):simulate_lay({},c)


def test_voyage_charges_one_actual_static_initialization_and_explicitly_rejects_insufficient_total(monkeypatch):
    import oceanroute.static_bathymetry as module
    original=module.static_equilibrium;calls=[]
    def tracked(c):calls.append(c);return original(c)
    monkeypatch.setattr(module,'static_equilibrium',tracked)
    c,*_=analytic_config()
    est=estimate_initial_equilibrium_work({},c)['estimated_work_units']
    with pytest.raises(ValueError,match='initial equilibrium work exceeds'):
        run_voyage({}, {'simulation':c,'duration_s':.3,'chunk_duration_s':.1,'max_total_work_units':est-1})
    assert calls==[]
    r=run_voyage({}, {'simulation':c,'duration_s':.3,'chunk_duration_s':.1,'max_total_work_units':1e7,'adaptive_mesh':{'enabled':False}})
    assert len(calls)==1 and r['status']=='completed'
    assert r['summary']['estimated_work_units']>=est
    assert r['checkpoint']['physical_checkpoint']['schema_version']==3
    json.dumps(r,allow_nan=False)


@pytest.mark.parametrize('field,value',[('heave_offset_z_m',0.),('heave_phase_origin_s',1.),('last_contact_step_s',.01)])
def test_time_zero_checkpoint_cannot_fake_completed_contact_or_shift_heave_baseline(field,value):
    c,*_=analytic_config(save_checkpoints=True)
    cp=simulate_lay({},c)['checkpoints'][0]
    cp['state'][field]=value;cp['checksum_sha256']=_digest(cp)
    with pytest.raises(ValueError):read_checkpoint(cp)
