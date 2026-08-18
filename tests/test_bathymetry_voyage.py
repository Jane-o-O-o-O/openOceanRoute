"""Actual 2D contact remains complete through chunking and durable jobs."""
from copy import deepcopy
import json
import time

import numpy as np
import pytest

from oceanroute.checkpoints import read_checkpoint
from oceanroute.simulation import simulate_lay
from oceanroute.voyage import coarsen_checkpoint, read_voyage_checkpoint, run_voyage
from oceanroute.voyage_jobs import VoyageJobs


def settings():
    axes = [-40, 0, 40]
    return {'seabed_grid': {'schema':'oceanroute.bathymetry.v1', 'x_m':axes, 'y_m':axes,
            'z_m':[[-12+.02*x-.02*y for x in axes] for y in axes],
            'source':{'name':'explicit synthetic 2D slope', 'horizontal_crs':'LOCAL_CARTESIAN_METRES',
                      'origin_projected_m':[0,0], 'vertical_datum':'aligned model sea surface z=0'}},
            'depth_m':12,'bottom_tension_n':10,'wet_weight_n_m':4,'nodes':12,
            'ship_speed_m_s':.2,'payout_m_s':.4,'heading_deg':90,
            'internal_dt_s':.025,'dt_s':.5,'solver_iterations':24}


def request(duration=4):
    return {'duration_s':duration,'chunk_duration_s':1,'simulation':settings(),'adaptive_mesh':{'enabled':False}}


def compare(a,b):
    for key in ('positions','velocities','rest_lengths_m','node_material_m','node_seabed_normal',
                'node_contact_normal_impulse_n_s','node_contact_friction_impulse_n_s'):
        np.testing.assert_allclose(a[key],b[key],atol=3e-8,rtol=1e-9)
    assert a['paid_out_m'] == pytest.approx(b['paid_out_m'],abs=1e-9)
    assert a['contact_mask'] == b['contact_mask']


def test_real_2d_chunks_and_serialized_resume_equal_uninterrupted_with_source():
    chunks = run_voyage({},request())
    uninterrupted = simulate_lay({},dict(settings(),duration_s=4))
    assert chunks['status'] == 'completed' and chunks['summary']['computed_duration_s'] == 4
    physical = chunks['checkpoint']['physical_checkpoint']
    assert physical['schema_version'] == 2
    compare(physical['state'],uninterrupted['checkpoint']['state'])
    first = run_voyage({},request(2))
    saved = json.loads(json.dumps(first['checkpoint'],allow_nan=False))
    assert read_voyage_checkpoint(saved)['physical_checkpoint']['config']['seabed_grid'] == settings()['seabed_grid']
    continued = run_voyage({}, {'resume_state':saved,'duration_s':2,'chunk_duration_s':1})
    compare(continued['checkpoint']['physical_checkpoint']['state'],physical['state'])
    assert continued['summary']['end_time_s'] == 4


def test_2d_grid_cannot_enter_old_flat_bed_coarsening_even_when_growing_mesh():
    # Fast feed creates enough genuine new nodes to enter the coarsening gate.
    config = dict(settings(),payout_m_s=8,duration_s=2)
    first = simulate_lay({},dict(config,duration_s=1))['checkpoint']
    later = simulate_lay({}, {'resume_state':first,'duration_s':1})['checkpoint']
    assert len(later['state']['positions']) > 12
    reduced = coarsen_checkpoint({},later,first,{'enabled':True,'target_nodes':12})
    assert not reduced['accepted'] and reduced['reason'] == 'flat_bed_required'
    assert reduced['checkpoint'] == later and reduced['transfers'] == []


def test_actual_2d_background_job_close_reopen_resume_preserves_grid_and_contacts(tmp_path):
    def wait(jobs,identifier):
        until=time.monotonic()+10
        while time.monotonic()<until:
            row=jobs.get(identifier)
            if row['status'] in ('completed','failed','stopped','cancelled'): return row
            time.sleep(.01)
        raise AssertionError('real local 2D job did not finish')
    directory=tmp_path/'jobs'; jobs=VoyageJobs(directory)
    try:
        row=jobs.submit({},request(2))
        assert wait(jobs,row['id'])['status'] == 'completed'
        first=jobs.result(row['id'])
    finally: jobs.close()
    reopened=VoyageJobs(directory)
    try:
        child=reopened.resume(row['id'],{'duration_s':2,'chunk_duration_s':1})
        assert wait(reopened,child['id'])['status'] == 'completed'
        result=reopened.result(child['id'])
        physical=result['checkpoint']['physical_checkpoint']
        assert read_checkpoint(physical)['config']['seabed_grid'] == settings()['seabed_grid']
        assert result['summary']['end_time_s'] == 4
        assert result['summary']['material_balance_residual_m'] < 1e-7
        compare(physical['state'],run_voyage({},request())['checkpoint']['physical_checkpoint']['state'])
    finally: reopened.close()
