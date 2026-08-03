import numpy as np
import pytest
from oceanroute.units import length_factor
from oceanroute.gis import import_xyz


def test_explicit_imperial_elevation_to_positive_metric_depth():
    xyz = "longitude,latitude,elevation\n118,22,-100\n119,22,-200\n118,23,-300"
    values, warnings = import_xyz(xyz, depth_positive="up", depth_units="ft")
    assert not warnings
    assert values[:, 2] == pytest.approx([30.48, 60.96, 91.44])
    metric, _ = import_xyz("118 22 30.48\n119 22 60.96\n118 23 91.44")
    assert np.allclose(values, metric)
    assert length_factor("fathom") == pytest.approx(6 * .3048)


def test_unknown_units_never_guessed_and_land_not_seabed():
    with pytest.raises(ValueError, match="单位"):
        length_factor("unknown")
    with pytest.raises(ValueError, match="至少"):
        import_xyz("118 22 100\n119 22 200\n118 23 300", depth_positive="up")
