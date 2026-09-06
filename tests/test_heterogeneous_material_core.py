"""Material integrals, signed potential and real heterogeneous initialization.

Expected geometry is constructed from independent nodal force recurrence;
neither the static optimizer nor the production material operator supplies it.
"""
from copy import deepcopy
import json
import math
from pathlib import Path

import numpy as np
import pytest

from oceanroute.checkpoints import _digest, read_checkpoint
from oceanroute.initial_equilibrium import resolve_initial_equilibrium
from oceanroute.simulation import _MaterialModel, _environment, simulate_lay
from oceanroute.static_bathymetry import _energy_forces, static_equilibrium


def bed():
    return {"schema":"oceanroute.bathymetry.v1","x_m":[-100.,0.,100.],"y_m":[-100.,0.,100.],
            "z_m":[[-100.]*3 for _ in range(3)],"source":{"name":"synthetic material-core test bed",
            "horizontal_crs":"LOCAL_CARTESIAN_METRES","origin_projected_m":[0.,0.],
            "vertical_datum":"synthetic model sea surface zero"}}


def fixture(**changes):
    rest=np.array([2.,3.,4.,1.,5.,2.]); q=100+np.r_[np.cumsum(rest[::-1])[::-1],0.]
    rows=[{"start_m":0.,"end_m":104.5,"wet_weight_n_m":2.,"ea_n":8000.,"mass_kg_m":1.,"diameter_m":.02},
          {"start_m":104.5,"end_m":110.25,"wet_weight_n_m":6.,"ea_n":20000.,"mass_kg_m":2.,"diameter_m":.025},
          {"start_m":110.25,"end_m":300.,"wet_weight_n_m":3.,"ea_n":12000.,"mass_kg_m":1.,"diameter_m":.015}]
    bodies=[{"id":"buoyant","material_m":103.25,"length_m":0.,"mass_kg":2.,"wet_weight_n":-15.},
            {"id":"positive","material_m":108.75,"length_m":0.,"mass_kg":2.,"wet_weight_n":10.},
            {"id":"top","material_m":117.,"length_m":0.,"mass_kg":2.,"wet_weight_n":4.}]
    wet=[];compliance=[];dry=[]
    for low,high in zip(q[1:],q[:-1]):
        overlap=np.array([max(0.,min(high,r['end_m'])-max(low,r['start_m'])) for r in rows])
        wet.append(sum(overlap[i]*r['wet_weight_n_m'] for i,r in enumerate(rows)))
        compliance.append(sum(overlap[i]/r['ea_n'] for i,r in enumerate(rows)))
        dry.append(sum(overlap[i]*r['mass_kg_m'] for i,r in enumerate(rows)))
    def half(x):return np.r_[x[0]/2,(np.array(x[:-1])+x[1:])/2,x[-1]/2]
    weight=half(wet);bodyweight=np.zeros(7)
    for b in bodies:
        for i in range(6):
            if q[i+1]<=b['material_m']<=q[i]:
                f=(b['material_m']-q[i+1])/rest[i]
                bodyweight[i:i+2]+=np.array([f,1-f])*b['wet_weight_n'];break
    weight+=bodyweight;ea=rest/np.array(compliance)
    vertical=np.full(6,40.)
    for i in range(4,-1,-1):vertical[i]=vertical[i+1]+weight[i+1]
    force=np.column_stack([np.full(6,120.),np.zeros(6),vertical]);tension=np.linalg.norm(force,axis=1)
    vectors=(rest+np.array(compliance)*tension)[:,None]*force/tension[:,None]
    vessel=np.array([2.,-3.,-1.]);p=np.vstack([vessel,vessel-np.cumsum(vectors,axis=0)])
    c={"nodes":7,"seabed_grid":bed(),"initial_suspended_material_m":100.,"material_segments":rows,
       "inline_bodies":bodies,"ea_n":10000.,"wet_weight_n_m":4.,"mass_kg_m":1.,"ei_n_m2":0.,
       "ship_speed_m_s":0.,"payout_m_s":0.,"heading_deg":90.,"duration_s":.04,"dt_s":.02,
       "internal_dt_s":.002,"damping_ratio":0.,"solver_iterations":24,"seabed_friction":.6,
       "initial_equilibrium":{"schema":"oceanroute.dynamic.initial-equilibrium.v1",
         "vessel_position_m":vessel.tolist(),"anchor_position_m":p[-1].tolist(),
         "rest_lengths_m":rest.tolist(),"initial_positions_m":p.tolist()}}
    c.update(changes)
    return c,p,rest,ea,weight,np.array(wet),np.array(compliance),tension,bodyweight


def test_large_origin_local_integrals_do_not_subtract_predecessor_totals():
    origin=2**26;rest=np.full(5,1/1024)
    c={"initial_suspended_material_m":origin,"wet_weight_n_m":4,"diameter_m":.02,
       "material_segments":[{"start_m":0,"end_m":origin,"wet_weight_n_m":20000.,"mass_kg_m":3000.,"ea_n":100.},
                            {"start_m":origin,"end_m":origin+1,"wet_weight_n_m":1e-6,"mass_kg_m":.01,
                             "diameter_m":1e-4,"ea_n":1e12}]}
    material=_MaterialModel(c,_environment(c),1e4,0.,1.,.7);actual=material.loads(rest)
    assert actual['ea']==pytest.approx(np.full(5,1e12),rel=1e-14)
    assert actual['segment_compliance_m_n']==pytest.approx(rest/1e12,rel=1e-14,abs=0)
    assert sum(actual['weight'])==pytest.approx(5/1024*1e-6,rel=1e-14,abs=0)
    assert sum(actual['dry_mass'])==pytest.approx(5/1024*.01,rel=1e-14,abs=0)
    assert np.isfinite(actual['mass']).all()


def test_local_mixed_compliance_and_signed_point_loading_equal_independent_integrals():
    c,p,rest,ea,weight,wet,compliance,tension,bodyweight=fixture()
    result=resolve_initial_equilibrium({},c);s=result['provenance']['initial_snapshot'];parts=result['provenance']['material_loading']
    assert np.array(result['positions'])==pytest.approx(p,abs=1e-10)
    assert s['segment_ea_n']==pytest.approx(ea,rel=1e-12)
    assert s['node_wet_weight_n']==pytest.approx(weight,abs=1e-11)
    assert s['segment_tension_n']==pytest.approx(tension,abs=1e-8)
    assert parts['segment_cable_wet_weight_n']==pytest.approx(wet,abs=1e-12)
    assert parts['segment_compliance_m_n']==pytest.approx(compliance,rel=1e-12,abs=0)
    assert parts['node_body_wet_weight_n']==pytest.approx(bodyweight,abs=1e-12)
    assert parts['absolute_load_scale_n']==pytest.approx(sum(wet)+29.)
    assert s['node_boundary_force_n'][0]==pytest.approx([120,0,tension[0]*((p[0,2]-p[1,2])/np.linalg.norm(p[0]-p[1]))+weight[0]],abs=1e-8)
    assert np.sum(s['node_boundary_force_n'],axis=0)==pytest.approx([0,0,sum(weight)],abs=1e-8)
    json.dumps(result,allow_nan=False)


def test_signed_heterogeneous_energy_gradient_matches_actual_nodal_force():
    _,p,rest,ea,weight,*_=fixture()
    # Perturb a tensile state away from the stationary point, so a zero-force
    # comparison cannot conceal a missing load or wrong energy derivative.
    p=p.copy();p[2]+=[.03,-.02,.04]
    direction=np.sin(np.arange(p.size)+.3).reshape(p.shape)
    step=2e-6
    energy,force,*_= _energy_forces(p,rest,ea,weight)
    plus=_energy_forces(p+step*direction,rest,ea,weight)[0]
    minus=_energy_forces(p-step*direction,rest,ea,weight)[0]
    assert (plus-minus)/(2*step)==pytest.approx(-np.sum(force*direction),rel=3e-7,abs=2e-5)
    assert math.isfinite(energy)


@pytest.mark.parametrize('step',[.002,.004,.008])
def test_real_prestress_heterogeneous_point_state_is_a_fixed_point(step):
    c,p,rest,_,_,_,_,tension,_=fixture(duration_s=.08,internal_dt_s=step)
    r=simulate_lay({},c);last=r['frames'][-1]
    assert np.array(last['nodes'])==pytest.approx(p,abs=1e-8)
    assert np.max(np.linalg.norm(last['node_velocity_m_s'],axis=1))<1e-7
    assert last['segment_tension_n']==pytest.approx(tension,abs=1e-7)
    assert r['checkpoint']['state']['rest_lengths_m']==rest.tolist()
    assert r['checkpoint']['state']['segment_target_m']==5
    assert last['touchdown'] is None and last['bottom_tension_n'] is None
    assert r['initialization']['schema'].endswith('provenance.v2')


def test_actuation_and_json_resume_keep_real_loads_and_initial_stock(monkeypatch):
    c,p,*_=fixture(duration_s=.08,ship_speed_m_s=.2,payout_m_s=.3)
    direct=simulate_lay({},c);first=simulate_lay({},dict(c,duration_s=.04))
    import oceanroute.static_bathymetry as module
    monkeypatch.setattr(module,'_static_equilibrium_core',lambda *a,**k:pytest.fail('resume optimized again'))
    resumed=simulate_lay({}, {'resume_state':json.loads(json.dumps(first['checkpoint'])), 'duration_s':.04})
    assert np.array(resumed['frames'][-1]['nodes'])==pytest.approx(np.array(direct['frames'][-1]['nodes']),abs=1e-10)
    assert np.max(np.linalg.norm(np.array(direct['frames'][-1]['nodes'])[1:-1]-p[1:-1],axis=1))>1e-7
    assert direct['summary']['paid_out_m']==pytest.approx(.024)
    assert resumed['solver']['initialization_work']['static_optimizer_run_this_call'] is False
    assert resumed['checkpoint']['state']['initialization_provenance']==first['checkpoint']['state']['initialization_provenance']


@pytest.mark.parametrize('field',['segment_compliance_m_n','node_body_wet_weight_n','node_cable_wet_weight_n'])
def test_rechecks_component_loads_even_after_public_checksum_is_recomputed(field):
    c,*_=fixture();r=simulate_lay({},c);cp=deepcopy(r['checkpoint'])
    cp['state']['initialization_provenance']['material_loading'][field][0]*=2
    cp['state']['initialization_provenance']['material_loading'][field][0]+=.001
    cp['checksum_sha256']=_digest(cp)
    with pytest.raises(ValueError,match='material/loading evidence'):read_checkpoint(cp)


def test_cannot_downgrade_extended_proof_to_uniform_v1():
    c,*_=fixture();cp=deepcopy(simulate_lay({},c)['checkpoint'])
    cp['state']['initialization_provenance']['schema']='oceanroute.dynamic.initial-equilibrium.provenance.v1'
    cp['state']['initialization_provenance'].pop('material_loading');cp['checksum_sha256']=_digest(cp)
    with pytest.raises(ValueError,match='provenance v1'):read_checkpoint(cp)


@pytest.mark.parametrize('field',['node_wet_weight_n','node_mass_kg','segment_ea_n','node_material_m'])
def test_public_reader_rechecks_current_loads_after_actuation(field):
    c,*_=fixture(ship_speed_m_s=.1,payout_m_s=.2)
    cp=deepcopy(simulate_lay({},c)['checkpoint']);assert cp['time_s']>0
    cp['state'][field][2]+=1
    cp['checksum_sha256']=_digest(cp)
    with pytest.raises(ValueError,match='current material/loading'):read_checkpoint(cp)


def test_public_static_contract_still_rejects_user_injected_material_force_arrays():
    c,p,rest,*_=fixture()
    request={"seabed_grid":c['seabed_grid'],"vessel_position_m":p[0].tolist(),"anchor_position_m":p[-1].tolist(),
             "rest_lengths_m":rest.tolist(),"segment_ea_n":[1e4]*6}
    with pytest.raises(ValueError,match='unsupported inputs'):static_equilibrium(request)


def test_deployed_finite_body_and_active_ei_cannot_be_collapsed_into_new_point_model():
    c,*_=fixture();c['inline_bodies'][0]['length_m']=.1
    with pytest.raises(ValueError,match='finite-length'):resolve_initial_equilibrium({},c)
    c,*_=fixture();c['material_segments'][1]['ei_n_m2']=1.
    with pytest.raises(ValueError,match='zero EI'):resolve_initial_equilibrium({},c)


def test_initializer_work_guard_precedes_optimizer(monkeypatch):
    import oceanroute.static_bathymetry as module
    monkeypatch.setattr(module,'_static_equilibrium_core',lambda *a,**k:pytest.fail('ran beyond work cap'))
    c,*_=fixture();c['initial_equilibrium']['solver']={'max_work_units':1}
    with pytest.raises(ValueError,match='dense-solver computation'):resolve_initial_equilibrium({},c)


@pytest.mark.parametrize('identifier_bytes,duration,save_all,match',[(1010000,.02,False,'standalone checkpoint JSON volume'),
                                                                 (80000,2.,True,'checkpoint JSON volume exceeds 16 MB'),
                                                                 (800000,2.,False,'dynamic response JSON volume exceeds 64 MB')])
def test_real_point_metadata_bytes_are_preflighted_before_static_solve(monkeypatch,identifier_bytes,duration,save_all,match):
    import oceanroute.static_bathymetry as module
    monkeypatch.setattr(module,'_static_equilibrium_core',lambda *a,**k:pytest.fail('optimized before capacity guard'))
    c,*_=fixture(duration_s=duration,save_checkpoints=save_all)
    c['inline_bodies'][0]['id']='x'*identifier_bytes
    with pytest.raises(ValueError,match=match):simulate_lay({},c)


def test_actual_frozen_06_checkpoints_survive_stable_integral_change_but_not_forged_proof():
    # These states were produced by the separately frozen wheel, not by the
    # module under test. Current-source v1 roundtrips alone miss this regression.
    evidence=json.loads((Path(__file__).resolve().parents[1]/'resources/validation/development_0.7_legacy_checkpoint_inputs.json').read_text())
    assert 'oceanroute-0.6.0-py3-none-any.whl/oceanroute/simulation.py' in evidence['imported_source']
    for case in evidence['cases']:
        cp=case['checkpoint'];assert read_checkpoint(cp)['state']['initialization_provenance']['schema'].endswith('provenance.v1')
        bad=deepcopy(cp);bad['state']['initialization_provenance']['verification']['max_node_force_residual_n']+=.001
        bad['checksum_sha256']=_digest(bad)
        with pytest.raises(ValueError,match='acceptance/tolerance'):read_checkpoint(bad)
