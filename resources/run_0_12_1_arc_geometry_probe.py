"""Independent constructed-center probes; not a whole-suite or field gate."""
from __future__ import annotations
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import time

from oceanroute.arc_edit_geometry import rebuild_arc_endpoints
from oceanroute.geodesy import GEOD
from oceanroute.route_geometry import segment_from_leg


def forward(point, angle, radius):
    return tuple(GEOD.fwd(*point, angle, radius)[:2])


def main():
    started = time.monotonic()
    source = Path('oceanroute/arc_edit_geometry.py')
    original = source.read_bytes()
    rows = []
    for center in [(118., 22.), (-179.98, 0.), (179.99, 68.), (37., 89.5), (-122., -80.), (0., 90.)]:
        for radius in [.001, 10., 300., 500000., 1000000.]:
            for sweep in [60., -60., 270., -270.]:
                a = forward(center, 30., radius)
                old_b = forward(center, 30.+sweep, radius)
                old_g = {'type':'circular_arc', 'schema_version':1, 'center':list(center),
                         'radius_m':radius, 'start_azimuth_deg':30., 'sweep_deg':sweep}
                # Known solution built before the solver: rotate the circle
                # center about the FIXED old start and build the other endpoint
                # on that independent new circle. No solver output constructs
                # this target or the expected center.
                radial = GEOD.inv(*a, *center)[0]
                expected_center = forward(a, radial+7., radius)
                azi0 = GEOD.inv(*expected_center, *a)[0]
                b = forward(expected_center, azi0+sweep, radius)
                frozen_g = deepcopy(old_g)
                result = rebuild_arc_endpoints(a, b, old_g,
                    config={'original_endpoints':[a, old_b]})
                assert old_g == frozen_g
                assert result['accepted'], (center, radius, sweep, result['rejection_codes'])
                g = result['geometry']
                center_error = GEOD.inv(*expected_center, *g['center'])[2]
                assert center_error <= max(1e-6, radius*1e-7), (center, radius, sweep, center_error)
                assert g['radius_m'] == radius and g['sweep_deg']*sweep > 0
                assert (abs(g['sweep_deg'])>180) == (abs(sweep)>180)
                segment = segment_from_leg(a, b, {'geometry':g})
                radial_errors = []
                for fraction in [0., .1, .25, .5, .75, .9, 1.]:
                    point = segment.point_at_fraction(fraction)
                    radial_errors.append(abs(GEOD.inv(*g['center'], *point)[2]-radius))
                tolerance = result['evidence']['effective_radius_tolerance_m']
                assert max(radial_errors) <= max(1e-8, tolerance), (center,radius,sweep,radial_errors,tolerance)
                assert result['budget']['work_units'] <= result['budget']['max_work_units']
                rows.append({'old_center':list(center),'radius_m':radius,'signed_sweep_deg':sweep,
                             'expected_center':list(expected_center),'actual_center':g['center'],
                             'expected_center_error_m':center_error,
                             'physical_fraction_sample_radius_errors_m':radial_errors,
                             'effective_radius_tolerance_m':tolerance,'budget':result['budget'],'status':'passed'})
    assert source.read_bytes() == original, 'Geometry changed during the actual probe'
    report={'version':'0.12.1','status':'passed','recorded_at_utc':datetime.now(timezone.utc).isoformat(),
            'scope':'120 independently constructed known-center edits: six latitudes/longitudes including the pole and date line, five radii from1mm to1000km, four directed minor/major sweeps. Actual native GEOD construction and inverse checks plus seven true physical-fraction positions per result. Not an original-product golden or global floating-point/safety proof; this is not a full-suite/API/browser/installation gate.',
            'cases':len(rows),'actual_physical_fraction_positions':7*len(rows),'wall_time_s':time.monotonic()-started,
            'source':{'path':source.as_posix(),'bytes':len(original),'sha256':hashlib.sha256(original).hexdigest()},
            'all_inputs_unchanged':True,'results':rows}
    target=Path('resources/validation/development_0.12.1_arc_geometry_independent_probes.json')
    with target.open('x',encoding='utf-8') as stream:stream.write(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='results'},ensure_ascii=False))


if __name__=='__main__':
    main()
