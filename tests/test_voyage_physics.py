"""Independent numerical acceptance of continued lay and conservative remeshing.

The baseline keeps every original/payout node. Transfer checks below integrate
the actual saved arrays rather than accepting the module's audit numbers.
Manufactured perturbations are explicitly unit benchmarks, not field data.
"""
from copy import deepcopy
import hashlib
import json

import numpy as np
import pytest

from oceanroute.checkpoints import pack_checkpoint, read_checkpoint
from oceanroute.simulation import simulate_lay
from oceanroute.voyage import (_finite_json, _material, _policy, _transfer_one,
                              coarsen_checkpoint, read_voyage_checkpoint,
                              run_voyage)


BASE = {"depth_m": 20, "bottom_tension_n": 50, "wet_weight_n_m": 4,
        "ea_n": 1e6, "ei_n_m2": 0, "nodes": 12, "ship_speed_m_s": .5,
        "payout_m_s": .6, "internal_dt_s": .05, "dt_s": 1,
        "solver_iterations": 24, "seabed_friction": .5}


def independent_integrals(checkpoint):
    """Integrals of actual lumped arrays; does not call voyage._metrics."""
    a = read_checkpoint(checkpoint)["arrays"]
    x, u, mass = a["positions"], a["velocities"], a["node_mass_kg"]
    strain = np.maximum(np.linalg.norm(np.diff(x, axis=0), axis=1)
                        / a["rest_lengths_m"] - 1, 0)
    return {
        "length": np.sum(a["rest_lengths_m"]),
        "dry_mass": np.sum(a["node_dry_mass_kg"]),
        "mass": np.sum(mass), "weight": np.sum(a["node_wet_weight_n"]),
        "momentum": (mass[:, None] * u).sum(axis=0),
        "angular_momentum": np.cross(x, mass[:, None] * u).sum(axis=0),
        "center": (mass[:, None] * x).sum(axis=0) / mass.sum(),
        "kinetic": .5 * np.sum(mass[:, None] * u * u),
        "elastic": .5 * np.sum(a["segment_ea_n"]
                                * a["rest_lengths_m"] * strain * strain),
    }


def actual_trace_error(reduced, baseline):
    originals = {f["time_s"]: f for f in baseline["frames"]}
    errors = {"position": 0., "reconstructed_position": 0., "velocity": 0., "touchdown": 0.,
              "tension": 0., "material_length": 0.}
    for f in reduced["frames"]:
        exact = originals[f["time_s"]]
        coords = np.asarray(exact["node_material_m"])
        for field, name in (("nodes", "position"),
                            ("node_velocity_m_s", "velocity")):
            source = np.asarray(exact[field])
            mapped = np.column_stack([
                np.interp(f["node_material_m"], coords[::-1], source[::-1, k])
                for k in range(3)])
            errors[name] = max(errors[name], float(np.max(
                np.linalg.norm(mapped - np.asarray(f[field]), axis=1))))
        # A retained-node comparison alone misses displacement of the material
        # markers removed by remeshing. Compare the entire original domain too.
        source = np.asarray(f["nodes"])
        mapped = np.column_stack([
            np.interp(exact["node_material_m"],
                      np.asarray(f["node_material_m"])[::-1], source[::-1, k])
            for k in range(3)])
        errors["reconstructed_position"] = max(errors["reconstructed_position"],
            float(np.max(np.linalg.norm(mapped-np.asarray(exact["nodes"]), axis=1))))
        errors["touchdown"] = max(errors["touchdown"], float(np.linalg.norm(
            np.asarray(f["touchdown"]) - exact["touchdown"])))
        errors["tension"] = max(errors["tension"],
            abs(f["top_tension_n"] - exact["top_tension_n"]),
            abs(f["bottom_tension_n"] - exact["bottom_tension_n"]))
        errors["material_length"] = max(errors["material_length"],
            abs(f["node_material_m"][0] - exact["node_material_m"][0]))
    return errors


@pytest.fixture(scope="module", params=[0., .02, .1])
def traces(request):
    simulation = {**BASE, "current_y_m_s": request.param}
    reduced = run_voyage({}, {"simulation": simulation, "duration_s": 120,
        "chunk_duration_s": 10, "adaptive_mesh": {"target_nodes": 12},
        "max_output_frames": 256})
    baseline = simulate_lay({}, {**simulation, "duration_s": 120})
    return request.param, reduced, baseline


def test_batch_actual_full_traces_remain_close_to_uncoarsened_baseline(traces):
    current, reduced, baseline = traces
    assert reduced["status"] == "completed"
    assert all(c["solver"]["converged"] for c in reduced["chunks"])
    assert baseline["solver"]["converged"]
    assert len(reduced["frames"]) == len(baseline["frames"]) == 121
    if current == 0:
        assert reduced["checkpoint"]["mesh_history"]
    if reduced["checkpoint"]["mesh_history"]:
        assert reduced["summary"]["final_nodes"] < len(baseline["checkpoint"]["state"]["positions"])
    else:
        # Conservative rejection is a valid outcome, not a reason to loosen
        # the strain/material-location guards just to produce a small mesh.
        assert reduced["summary"]["final_nodes"] == len(baseline["checkpoint"]["state"]["positions"])
        for key in ("positions", "velocities", "rest_lengths_m"):
            np.testing.assert_allclose(
                reduced["checkpoint"]["physical_checkpoint"]["state"][key],
                baseline["checkpoint"]["state"][key], atol=1e-9, rtol=1e-10)
    error = actual_trace_error(reduced, baseline)
    # Empirical whole-interval criteria, distinct from the .25 s probe guard.
    assert error["position"] < (1e-8 if current == 0 else .006)
    assert error["reconstructed_position"] < .02
    assert error["velocity"] < 1e-8
    assert error["tension"] < 1e-6
    assert error["touchdown"] < 1e-8
    assert error["material_length"] < 1e-9
    assert reduced["summary"]["paid_out_m"] == pytest.approx(.6*120, abs=1e-8)
    assert reduced["summary"]["material_balance_residual_m"] < 1e-8


def test_all_accepted_transfers_conserve_actual_integrated_material_and_momentum(traces):
    _, reduced, _ = traces
    totals = {key: 0. for key in ("length", "dry_mass", "mass", "weight")}
    momentum_defect = np.zeros(3)
    center_defect_sum = 0.
    for record in reduced["checkpoint"]["mesh_history"]:
        before, after = record["before_checkpoint"], record["candidate_checkpoint"]
        old, new = independent_integrals(before), independent_integrals(after)
        for key in totals:
            defect = new[key] - old[key]
            totals[key] += defect
            assert abs(defect) < 1e-8
        momentum_defect += new["momentum"] - old["momentum"]
        assert np.linalg.norm(new["momentum"] - old["momentum"]) < 1e-8
        center_defect_sum += np.linalg.norm(new["center"] - old["center"])
        reported_center_bound = sum(t["center_of_mass_shift_m"] for t in record["transfers"])
        assert np.linalg.norm(new["center"] - old["center"]) <= reported_center_bound + 1e-10
        for key in ("anchor", "ship", "paid_out_m", "initial_material_length_m",
                    "segment_target_m", "plan", "heave_phase_origin_s"):
            assert before["state"][key] == after["state"][key]
        assert before["time_s"] == after["time_s"]
        assert before["numerical"] == after["numerical"]
        assert after["state"]["node_material_m"][-1] == before["state"]["node_material_m"][-1]
        assert after["state"]["node_material_m"][0] == pytest.approx(
            before["state"]["node_material_m"][0], abs=1e-10, rel=0)
        assert record["probe"]["original_converged"]
        assert record["probe"]["reduced_converged"]
        assert record["probe"]["retained_contact_masks_equal"]
    assert all(abs(x) < 1e-7 for x in totals.values())
    assert np.linalg.norm(momentum_defect) < 1e-7
    # The policy is per deletion; do not silently interpret it as a global
    # centroid bound. Track the accumulated defect independently.
    count = sum(len(r["transfers"]) for r in reduced["checkpoint"]["mesh_history"])
    assert center_defect_sum <= count*.01+1e-10
    json.dumps(reduced, allow_nan=False)


def observed_interval(config, start, finish):
    stable, samples, actual_end = None, 0, start
    def observe(value):
        nonlocal stable, samples, actual_end
        if value["time_s"] < start-1e-8:
            return
        points = {round(q, 7) for q, speed, contact in zip(
            value["node_material_m"], value["node_speed_m_s"], value["contact_mask"])
            if contact and speed <= .01}
        stable = points if stable is None else stable & points
        samples += 1
        actual_end = value["time_s"]
    result = simulate_lay({}, {**config, "duration_s": finish,
                              "checkpoint_times_s": [start, finish]},
                          state_observer=observe)
    return result, {"interval_start_s": start, "interval_end_s": actual_end,
                    "internal_samples": samples,
                    "stable_material_coordinates_m": sorted(stable or [])}


@pytest.fixture(scope="module")
def actual_pair():
    result, evidence = observed_interval(BASE, 50, 60)
    previous, current = result["checkpoints"]
    return previous, current, evidence


def eligible_transfer(actual_pair):
    previous, current = map(read_checkpoint, actual_pair[:2])
    material, policy = _material(current["config"]), _policy({"target_nodes": 12})
    for j in range(len(current["arrays"]["positions"])-3, 2, -1):
        # Unit tests of a single transfer declare an already stable material
        # set. Production evidence is separately collected at every time step.
        stable = {round(float(q), 7) for q in actual_pair[2]["stable_material_coordinates_m"]}
        result = _transfer_one(current, previous, material, j, policy, stable)
        if result is not None:
            return j, previous, current, material, policy, stable
    pytest.fail("the actual stable flat-bed benchmark has no eligible element")


def test_nonzero_velocity_transfer_preserves_linear_momentum_not_just_zero_states(actual_pair):
    j, previous, current, material, policy, stable = eligible_transfer(actual_pair)
    # A small, declared perturbation of an actual settled mesh is a transfer
    # unit benchmark; it is not advertised as a dynamically equilibrated sea.
    old_state, new_state = deepcopy(previous["state"]), deepcopy(current["state"])
    for k, velocity in zip((j-1, j, j+1),
                           ([.002, -.001, 0], [-.003, .004, 0], [.001, .002, 0])):
        q = int(np.argmin(abs(previous["arrays"]["node_material_m"]
                             - current["arrays"]["node_material_m"][k])))
        old_state["velocities"][q] = velocity
        new_state["velocities"][k] = velocity
    previous = read_checkpoint(pack_checkpoint(previous["config"], old_state,
                                previous["time_s"], previous["numerical"]))
    document = pack_checkpoint(current["config"], new_state,
                               current["time_s"], current["numerical"])
    current = read_checkpoint(document)
    candidate, record = _transfer_one(current, previous, material, j, policy, stable)
    before, after = independent_integrals(document), independent_integrals(candidate)
    assert np.linalg.norm(before["momentum"]) > 1
    np.testing.assert_allclose(after["momentum"], before["momentum"], atol=1e-10, rtol=0)
    for key in ("length", "mass", "dry_mass", "weight"):
        assert after[key] == pytest.approx(before[key], abs=1e-10)
    assert after["kinetic"] <= before["kinetic"] + 1e-10
    assert np.linalg.norm(after["center"]-before["center"]) < .01
    assert record["angular_momentum_error_kg_m2_s"] == pytest.approx(
        np.linalg.norm(after["angular_momentum"]-before["angular_momentum"]), abs=1e-10)
    # Removing one interior node retains the initial real anchor and every
    # surviving material coordinate; no new fixed touchdown is introduced.
    assert candidate["state"]["anchor"] == document["state"]["anchor"]
    assert candidate["state"]["positions"] == np.delete(
        current["arrays"]["positions"], j, axis=0).tolist()


@pytest.mark.parametrize("hazard", ["bend_stiffness", "moving", "not_contact", "kink",
                                  "material_mapping", "unequal_compression",
                                  "body", "material_boundary"])
def test_specific_physical_hazards_reject_a_known_eligible_transfer(actual_pair, hazard):
    j, previous, current, material, policy, stable = eligible_transfer(actual_pair)
    current, previous = deepcopy(current), deepcopy(previous)
    if hazard == "bend_stiffness":
        current["arrays"]["segment_ei_n_m2"][j] = 1
    elif hazard == "moving":
        current["arrays"]["velocities"][j] = [.1, 0, 0]
    elif hazard == "not_contact":
        current["state"]["contact_mask"][j] = False
    elif hazard == "kink":
        current["arrays"]["positions"][j, 1] += .1
        old = int(np.argmin(abs(previous["arrays"]["node_material_m"]
                               - current["arrays"]["node_material_m"][j])))
        previous["arrays"]["positions"][old, 1] += .1
    elif hazard in {"material_mapping", "unequal_compression"}:
        points = current["arrays"]["positions"]
        axis = points[j+1]-points[j-1]
        delta = axis/np.linalg.norm(axis)*(.002 if hazard == "material_mapping" else .0002)
        points[j] += delta
        old = int(np.argmin(abs(previous["arrays"]["node_material_m"]
                               - current["arrays"]["node_material_m"][j])))
        previous["arrays"]["positions"][old] += delta
        if hazard == "material_mapping":
            policy = {**policy, "max_strain_change": .001}
        # The three points remain collinear. Geometry-only perpendicular
        # deviation therefore cannot reject these lost-material-coordinate
        # and unequal-compression defects.
        assert np.linalg.norm(np.cross(points[j]-points[j-1], axis)) < 1e-10
    elif hazard == "body":
        material = deepcopy(material)
        material.bodies = [{"material_m": current["arrays"]["node_material_m"][j],
                            "length_m": 0}]
    else:
        material = deepcopy(material)
        material.ends = np.r_[current["arrays"]["node_material_m"][j], 1e6]
    assert _transfer_one(current, previous, material, j, policy, stable) is None


def test_actual_unconverged_twin_solvers_never_accept_coarsening():
    result, evidence = observed_interval({**BASE, "internal_dt_s": .1,
                                         "solver_iterations": 2}, 30, 40)
    assert not result["solver"]["converged"]
    previous, current = result["checkpoints"]
    report = coarsen_checkpoint({}, current, previous, {"target_nodes": 12},
                                settlement_evidence=evidence)
    assert not report["accepted"]
    assert report["reason"] in {"probe_rejected", "no_settled_safe_elements"}
    assert report["checkpoint"] == current
    if report["reason"] == "probe_rejected":
        assert not (report["probe"]["original_converged"]
                    and report["probe"]["reduced_converged"])


def test_a_probe_without_future_prescribed_motion_retains_unmodified_state():
    config = {**BASE, "duration_s": 60, "checkpoint_times_s": [50, 60],
              "vessel_motion_series": [{"time_s": 0, "heave_m": 0},
                                       {"time_s": 60, "heave_m": 0}]}
    result, evidence = observed_interval(config, 50, 60)
    previous, current = result["checkpoints"]
    report = coarsen_checkpoint({}, current, previous, {"target_nodes": 12},
                                settlement_evidence=evidence)
    assert not report["accepted"]
    assert report["reason"] == "probe_unavailable"
    assert "does not cover" in report["detail"]
    assert report["checkpoint"] == current


def test_two_saved_endpoints_do_not_substitute_for_internal_settlement_evidence(actual_pair):
    previous, current, _ = actual_pair
    report = coarsen_checkpoint({}, current, previous, {"target_nodes": 12})
    assert not report["accepted"]
    assert report["reason"] == "internal_settlement_evidence_required"
    assert report["checkpoint"] == current


def test_removed_material_point_must_be_stable_at_every_observed_internal_step(actual_pair):
    j, previous, current, material, policy, stable = eligible_transfer(actual_pair)
    stable.remove(round(float(current["arrays"]["node_material_m"][j]), 7))
    # Both actual endpoint checkpoints still have this node in bed contact;
    # the absent internal evidence is nevertheless a hard rejection.
    assert current["state"]["contact_mask"][j]
    assert _transfer_one(current, previous, material, j, policy, stable) is None


def test_policy_minimum_settled_interval_is_applied_before_any_mesh_transfer(actual_pair):
    previous, current, evidence = actual_pair
    report = coarsen_checkpoint({}, current, previous,
        {"target_nodes": 12, "min_settled_duration_s": 11},
        settlement_evidence=evidence)
    assert not report["accepted"]
    assert report["reason"] == "internal_settlement_evidence_required"
    assert not report["transfers"]


def test_internal_observer_receives_every_step_but_cannot_mutate_integrator_arrays():
    samples = []
    def tamper(sample):
        samples.append(sample["time_s"])
        sample["node_material_m"][:] = [1e9]
        sample["node_speed_m_s"][:] = [1e9]
        sample["contact_mask"][:] = [False]
    config = {**BASE, "duration_s": 1}
    observed = simulate_lay({}, config, state_observer=tamper)
    reference = simulate_lay({}, config)
    assert len(samples) == observed["solver"]["steps_this_run"]+1
    assert samples[0] == 0 and samples[-1] == pytest.approx(1)
    assert all(b > a for a, b in zip(samples, samples[1:]))
    assert observed["checkpoint"] == reference["checkpoint"]


def test_reaching_requested_end_is_completed_even_when_audit_capacity_is_full():
    result = run_voyage({}, {"simulation": BASE, "duration_s": 30,
        "chunk_duration_s": 10, "adaptive_mesh": {"target_nodes": 12},
        "max_mesh_records": 1})
    assert result["summary"]["accepted_mesh_records"] == 1
    assert result["summary"]["end_time_s"] == 30
    assert result["status"] == "completed"
    assert result["stop_reason"] is None


def test_wave_preflight_and_probe_charge_fit_the_true_declared_work_budget():
    config = {**BASE, "wave_kinematics": {"depth_m": 20,
        "components": [{"frequency_hz": .1, "amplitude_m": .001,
                         "direction_deg": d} for d in (0, 90, 180)]}}
    result = run_voyage({}, {"simulation": config, "duration_s": 120,
        "chunk_duration_s": 10, "adaptive_mesh": {"target_nodes": 12},
        "max_total_work_units": 1_000_000})
    assert result["summary"]["chunks_this_run"] > 0
    assert result["status"] == "stopped"
    assert result["summary"]["estimated_work_units_this_run"] <= 1_000_000
    assert result["summary"]["end_time_s"] < 120
    restored = read_voyage_checkpoint(result["checkpoint"])
    assert restored["physical_checkpoint"]["time_s"] == result["summary"]["end_time_s"]


def test_resume_coarsened_actual_state_retains_clock_controls_and_material_through_later_merges():
    first = run_voyage({}, {"simulation": BASE, "duration_s": 50,
        "chunk_duration_s": 10, "adaptive_mesh": {"target_nodes": 12}})
    assert first["checkpoint"]["mesh_history"]
    second = run_voyage({}, {"resume_state": json.loads(json.dumps(first["checkpoint"])),
        "duration_s": 70, "chunk_duration_s": 10, "max_output_frames": 256})
    baseline = simulate_lay({}, {**BASE, "duration_s": 120})
    assert second["status"] == "completed"
    assert second["summary"]["start_time_s"] == 50
    assert second["summary"]["end_time_s"] == 120
    assert second["checkpoint"]["completed_chunks"] == 12
    assert second["summary"]["paid_out_m"] == pytest.approx(72, abs=1e-8)
    assert second["summary"]["material_balance_residual_m"] < 1e-8
    physical = second["checkpoint"]["physical_checkpoint"]
    assert physical["state"]["anchor"] == baseline["checkpoint"]["state"]["anchor"]
    assert physical["numerical"] == baseline["checkpoint"]["numerical"]
    assert actual_trace_error(second, baseline)["reconstructed_position"] < .02
    read_voyage_checkpoint(second["checkpoint"])


def test_voyage_version_rejects_boolean_even_with_valid_recomputed_checksum(actual_pair):
    physical = actual_pair[1]
    from oceanroute.voyage import _pack_voyage
    checkpoint = _pack_voyage(physical, _policy({}), 1, [], 0, 1)
    checkpoint["schema_version"] = True
    payload = {k: v for k, v in checkpoint.items() if k != "checksum_sha256"}
    checkpoint["checksum_sha256"] = hashlib.sha256(_finite_json(payload)).hexdigest()
    with pytest.raises(ValueError, match="schema"):
        read_voyage_checkpoint(checkpoint)


def test_historical_payout_does_not_block_a_current_stopped_feed_near_node_capacity(actual_pair):
    from oceanroute.voyage import FIELDS, _pack_voyage
    saved = read_checkpoint(actual_pair[1])
    a, s, config = saved["arrays"], deepcopy(saved["state"]), deepcopy(saved["config"])
    # Manufactured capacity benchmark: subdivide the actual material geometry
    # into 253 active nodes. This is an admissible mesh, not a field observation
    # or a claim that the old numerical trajectory had these extra nodes.
    length = np.sum(a["rest_lengths_m"])
    rest = np.full(252, length/252)
    loads = _material(config).loads(rest)
    coords = loads["coordinates"]
    positions = np.column_stack([np.interp(coords,
        a["node_material_m"][::-1], a["positions"][::-1, k]) for k in range(3)])
    s.update(positions=positions.tolist(), velocities=np.zeros_like(positions).tolist(),
             rest_lengths_m=rest.tolist(),
             contact_mask=(positions[:, 2] <= -config["depth_m"]+1e-8).tolist(),
             last_segment_tensions_n=(loads["ea"]*np.maximum(
                 np.linalg.norm(np.diff(positions, axis=0), axis=1)/rest-1, 0)).tolist(),
             plan=[{"time_s": 0., "speed_m_s": .5, "heading_deg": 90., "payout_m_s": .6},
                   {"time_s": 60., "speed_m_s": .5, "heading_deg": 90., "payout_m_s": 0.}],
             plan_index=1)
    for field, key in FIELDS:
        s[field] = loads[key].tolist()
    physical = pack_checkpoint(config, s, saved["time_s"], saved["numerical"])
    read_checkpoint(physical)
    direct = simulate_lay({}, {"resume_state": physical, "duration_s": .1})
    envelope = _pack_voyage(physical, _policy({"enabled": False}), 1, [], 0, 1)
    continued = run_voyage({}, {"resume_state": envelope, "duration_s": .1})
    assert continued["status"] == "completed"
    assert continued["summary"]["final_nodes"] == 253
    assert continued["summary"]["paid_out_m"] == s["paid_out_m"]
    np.testing.assert_allclose(continued["checkpoint"]["physical_checkpoint"]["state"]["positions"],
                               direct["checkpoint"]["state"]["positions"], atol=1e-10)
