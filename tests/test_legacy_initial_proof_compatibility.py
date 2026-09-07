"""Read real 0.6 wheel-generated proofs with the current implementation."""
from pathlib import Path
import json

import numpy as np
import pytest

from oceanroute.checkpoints import read_checkpoint
from oceanroute.simulation import simulate_lay


INPUT = Path(__file__).resolve().parents[1]/"resources/validation/development_0.7_legacy_checkpoint_inputs.json"


@pytest.mark.parametrize("case_index", [0, 1])
def test_real_prior_wheel_proof_remains_usable_without_initial_optimization(case_index, monkeypatch):
    cases = json.loads(INPUT.read_text())
    assert cases["source_wheel_sha256"] == "bc8e20bdf26656e6c5453d25fe385cad61238b48e30b5159471d72c2b8c35141"
    assert ".whl/" in cases["imported_source"]
    checkpoint = cases["cases"][case_index]["checkpoint"]
    original_proof = checkpoint["state"]["initialization_provenance"]
    assert original_proof["schema"] == "oceanroute.dynamic.initial-equilibrium.provenance.v1"
    import oceanroute.initial_equilibrium as initializer
    def forbidden(*args, **kwargs):
        raise AssertionError("an actual old checkpoint must resume without initial optimization")
    monkeypatch.setattr(initializer, "resolve_initial_equilibrium", forbidden)
    saved = read_checkpoint(checkpoint)
    assert saved["time_s"] == .04
    result = simulate_lay({}, {"resume_state": checkpoint, "duration_s": .02})
    state = result["checkpoint"]["state"]
    assert state["initialization_provenance"] == original_proof
    assert result["checkpoint"]["time_s"] == pytest.approx(.06, abs=1e-12)
    assert state["paid_out_m"] == 0.
    assert state["rest_lengths_m"] == checkpoint["state"]["rest_lengths_m"]
    assert np.asarray(state["positions"]) == pytest.approx(np.asarray(original_proof["initial_snapshot"]["positions"]), abs=1e-7)
