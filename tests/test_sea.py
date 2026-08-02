"""Linear dispersion/RAO identities and actual dynamic uncertainty propagation."""
from copy import deepcopy
import json

import numpy as np
import pytest

from oceanroute.sea import AiryField, generate_sea_state, monte_carlo, simulate_sea
from oceanroute.simulation import G, simulate_lay


SIM={"depth_m":30,"duration_s":2,"nodes":8,"dt_s":.25,"internal_dt_s":.05,
     "bottom_tension_n":100,"ship_speed_m_s":0,"payout_m_s":0}
SEA={"spectrum":"regular","hs_m":.2,"tp_s":4,"wave_direction_deg":90,
     "heave_rao":[{"frequency_hz":.25,"amplitude_m_m":1,"phase_deg":0}]}


def test_regular_stationary_rao_amplitude_and_phase_match_exact_harmonic():
    r=generate_sea_state({**SEA,"hs_m":1,"duration_s":8,"sample_dt_s":.05,
        "ship_speed_m_s":0,"heave_rao":[{"frequency_hz":.25,"amplitude_m_m":2,"phase_deg":90}]})
    times=np.array([s["time_s"] for s in r["samples"]])
    wave=np.array([s["surface_elevation_m"] for s in r["samples"]])
    heave=np.array([s["heave_m"] for s in r["samples"]])
    assert wave==pytest.approx(.5*np.cos(2*np.pi*times/4),abs=1e-12)
    assert heave==pytest.approx(np.sin(2*np.pi*times/4),abs=1e-12)
    assert r["summary"]["spectral_m0_m2"]==pytest.approx(.125)
    assert r["vessel_motion_series"][0]=={"time_s":0.,"heave_m":0.}


def test_complex_rao_interpolation_respects_phase_wrap_without_false_zero():
    r=generate_sea_state({**SEA,"duration_s":4,"ship_speed_m_s":0,"heave_rao":[
        {"frequency_hz":.2,"amplitude_m_m":1,"phase_deg":179},
        {"frequency_hz":.3,"amplitude_m_m":1,"phase_deg":-179}]})
    amplitude=r["summary"]["peak_heave_displacement_m"]
    assert amplitude==pytest.approx(.2*np.cos(np.radians(1)),abs=1e-6)


def test_finite_depth_dispersion_particle_velocity_and_bottom_boundary():
    f=AiryField({"depth_m":10,"components":[{"frequency_hz":.2,"amplitude_m":.3,
        "phase_deg":0,"direction_deg":90}]})
    omega=2*np.pi*.2; k=f.k[0]
    assert omega**2==pytest.approx(G*k*np.tanh(k*10),rel=1e-12)
    velocity=f.velocity(np.array([[0,0,0],[0,0,-10],[0,0,1]]),0)
    assert velocity[0,0]==pytest.approx(.3*omega/np.tanh(k*10),rel=1e-12)
    assert velocity[1,0]==pytest.approx(.3*omega/np.sinh(k*10),rel=1e-12)
    assert velocity[1,2]==0
    assert velocity[2]==pytest.approx(velocity[0],abs=1e-12)
    quarter=f.velocity(np.array([[0,0,0],[0,0,-10]]),1/(4*.2))
    assert quarter[0,2]==pytest.approx(-.3*omega,abs=1e-12)
    assert quarter[1,2]==0


def test_deep_water_hyperbolic_factors_remain_finite_at_large_kd():
    f=AiryField({"depth_m":12000,"components":[{"frequency_hz":2,"amplitude_m":.01}]})
    v=f.velocity(np.array([[0,0,0],[0,0,-100],[0,0,-12000]]),1)
    assert np.isfinite(v).all()
    assert np.linalg.norm(v[-1])==0
    assert np.linalg.norm(v[1])<1e-50


def test_spectrum_variance_normalization_seed_identity_and_pm_gamma_one():
    c={"hs_m":2.5,"tp_s":8,"duration_s":5,"component_count":24,"fixed_vessel_heave":True,"seed":123}
    a=generate_sea_state(c)
    b=generate_sea_state(c)
    assert a==b
    assert sum(s["density_m2_hz"]*s["bandwidth_hz"] for s in a["spectrum"])==pytest.approx((2.5/4)**2,rel=1e-12)
    assert a["summary"]["spectral_hm0_m"]==pytest.approx(2.5)
    pm=generate_sea_state({**c,"spectrum":"pm"})
    gamma=generate_sea_state({**c,"gamma":1})
    assert pm["components"]==gamma["components"]
    altered=generate_sea_state({**c,"seed":124})
    assert a["components"][0]["amplitude_m"]==altered["components"][0]["amplitude_m"]
    assert a["components"][0]["phase_deg"]!=altered["components"][0]["phase_deg"]


def test_forward_speed_has_actual_analytical_encounter_phase():
    r=generate_sea_state({**SEA,"duration_s":4,"ship_speed_m_s":1,"heading_deg":90,
                         "depth_m":30,"sample_dt_s":.05})
    field=AiryField(r["wave_kinematics"])
    times=np.array([s["time_s"] for s in r["samples"]])
    eta=np.array([s["surface_elevation_m"] for s in r["samples"]])
    expected=.1*np.cos((2*np.pi*.25-field.k[0])*times)
    assert eta==pytest.approx(expected,abs=1e-12)


def test_zero_spectrum_and_zero_rao_preserve_still_water_dynamic_state():
    ordinary=simulate_lay({},SIM)
    zero=simulate_sea({}, {"simulation":SIM,"sea_state":{**SEA,"hs_m":0}})
    last=zero["simulation"]["frames"][-1]
    assert np.array(last["nodes"])==pytest.approx(np.array(ordinary["frames"][-1]["nodes"]),abs=1e-8)
    assert last["node_velocity_m_s"]==pytest.approx(np.array(ordinary["frames"][-1]["node_velocity_m_s"]),abs=1e-8)
    assert zero["sea_state"]["summary"]["spectral_m0_m2"]==0
    json.dumps(zero,allow_nan=False)


def test_vessel_rao_heave_and_fluid_velocity_independently_change_cable_dynamics():
    still=simulate_lay({},SIM)
    heave=simulate_sea({}, {"simulation":SIM,"sea_state":{**SEA,"include_fluid_kinematics":False}})
    fluid=simulate_sea({}, {"simulation":SIM,"sea_state":{**SEA,"fixed_vessel_heave":True,
        "heave_rao":[{"frequency_hz":.25,"amplitude_m_m":0,"phase_deg":0}]}})
    assert heave["simulation"]["frames"][-1]["ship"][2]==pytest.approx(-.2,abs=1e-8)
    assert fluid["simulation"]["frames"][-1]["ship"][2]==0
    assert np.linalg.norm(np.array(heave["simulation"]["frames"][-1]["nodes"])-np.array(still["frames"][-1]["nodes"]))>.01
    assert np.linalg.norm(np.array(fluid["simulation"]["frames"][-1]["nodes"])-np.array(still["frames"][-1]["nodes"]))>.001
    repeated=simulate_lay({},heave["effective_simulation_config"])
    assert repeated["frames"][-1]["top_tension_n"]==heave["simulation"]["frames"][-1]["top_tension_n"]


def test_explicit_fixed_heave_mode_overrides_a_retained_user_rao_table():
    fixed=simulate_sea({}, {"simulation":SIM,"sea_state":{**SEA,"fixed_vessel_heave":True}})
    assert all(frame["ship"][2]==0 for frame in fixed["simulation"]["frames"])


def test_motion_and_wave_checkpoint_continuation_retains_true_phase_and_table_origin():
    generated=generate_sea_state({**SEA,"duration_s":4,"ship_speed_m_s":0,"depth_m":30})
    c={**SIM,"duration_s":4,"vessel_motion_series":generated["vessel_motion_series"],"wave_kinematics":generated["wave_kinematics"]}
    continuous=simulate_lay({},c)
    first=simulate_lay({}, {**c,"duration_s":2})
    resumed=simulate_lay({}, {"resume_state":json.loads(json.dumps(first["checkpoint"])),"duration_s":2})
    for key in ("positions","velocities","rest_lengths_m","last_segment_tensions_n"):
        assert np.array(resumed["checkpoint"]["state"][key])==pytest.approx(np.array(continuous["checkpoint"]["state"][key]),abs=1e-8,rel=1e-10)
    with pytest.raises(ValueError,match="does not cover"):
        simulate_lay({}, {"resume_state":resumed["checkpoint"],"duration_s":1})


def test_regenerated_stationary_sea_continuation_matches_same_full_phase_realization():
    full=simulate_sea({}, {"simulation":{**SIM,"duration_s":4},"sea_state":SEA})
    first=simulate_sea({}, {"simulation":SIM,"sea_state":SEA})
    second=simulate_sea({}, {"simulation":{"resume_state":first["simulation"]["checkpoint"],"duration_s":2},"sea_state":SEA})
    assert second["simulation"]["frames"][0]["time_s"]==2
    assert np.array(second["simulation"]["frames"][-1]["nodes"])==pytest.approx(np.array(full["simulation"]["frames"][-1]["nodes"]),abs=1e-7,rel=1e-10)
    assert second["simulation"]["frames"][-1]["ship"][2]==pytest.approx(0,abs=1e-12)


def test_motion_time_resolution_improves_analytical_waveform_accuracy():
    # Midpoint interpolation error of a sampled sine must shrink by ~h^2.
    errors=[]
    for dt in (.1,.05,.025):
        generated=generate_sea_state({**SEA,"duration_s":1,"sample_dt_s":dt,"ship_speed_m_s":0})
        rows=generated["vessel_motion_series"]
        t=np.array([row["time_s"] for row in rows]); z=np.array([row["heave_m"] for row in rows])
        mids=(t[:-1]+t[1:])/2
        interpolated=(z[:-1]+z[1:])/2
        exact=.1*(np.cos(2*np.pi*mids/4)-1)
        errors.append(np.max(np.abs(interpolated-exact)))
    assert errors[1]<errors[0]/3
    assert errors[2]<errors[1]/3


def test_monte_carlo_constant_inputs_give_identical_actual_trials_and_zero_spread():
    c={"simulation":SIM,"sea_state":SEA,"runs":3,"seed":17,"uncertainties":[],"include_frames":True}
    result=monte_carlo({},c)
    assert result["summary"]["complete"]
    values=[trial["metrics"]["mean_bottom_tension_n"] for trial in result["trials"]]
    assert values==[result["baseline_metrics"]["mean_bottom_tension_n"]]*3
    assert result["distributions"]["mean_bottom_tension_n"]["standard_deviation"]==pytest.approx(0,abs=1e-12)
    for trial in result["trials"]:
        assert len(trial["simulation"]["frames"])>1
    assert c["simulation"]==SIM
    json.dumps(result,allow_nan=False)


def test_bounded_distribution_sampling_is_reproducible_and_metrics_independently_recompute():
    c={"simulation":SIM,"sea_state":SEA,"runs":4,"seed":42,"uncertainties":[
        {"scope":"simulation","parameter":"current_y_m_s","distribution":"uniform","lower":-.3,"upper":.3},
        {"scope":"sea_state","parameter":"rao_scale","distribution":"normal","lower":.8,"upper":1.2,"mean":1,"std":.1}]}
    one=monte_carlo({},c); two=monte_carlo({},c)
    assert one==two
    for trial in one["trials"]:
        assert -.3<=trial["parameters"]["simulation.current_y_m_s"]<=.3
        assert .8<=trial["parameters"]["sea_state.rao_scale"]<=1.2
        repeated=simulate_sea({},trial["config"])
        assert repeated["metrics"]["mean_bottom_tension_n"]==trial["metrics"]["mean_bottom_tension_n"]
    values=np.array([trial["metrics"]["mean_bottom_tension_n"] for trial in one["trials"]])
    distribution=one["distributions"]["mean_bottom_tension_n"]
    assert distribution["mean"]==pytest.approx(np.mean(values))
    assert distribution["standard_deviation"]==pytest.approx(np.std(values,ddof=1))
    assert distribution["q95"]==pytest.approx(np.quantile(values,.95))
    assert np.std(values)>.01


def test_wave_phase_variation_changes_actual_trials_and_preserves_seed_reproducibility():
    c={"simulation":{**SIM,"duration_s":1},"sea_state":{"hs_m":.2,"tp_s":4,"component_count":4,
        "heave_rao":[{"frequency_hz":.005,"amplitude_m_m":1,"phase_deg":0},{"frequency_hz":2,"amplitude_m_m":1,"phase_deg":0}]},
        "runs":3,"seed":7,"vary_wave_phases":True}
    one=monte_carlo({},c); two=monte_carlo({},c)
    assert one==two
    assert len({trial["config"]["sea_state"]["seed"] for trial in one["trials"]})==3
    assert len({trial["metrics"]["peak_top_tension_n"] for trial in one["trials"]})==3


def test_regular_wave_phase_variation_changes_explicit_phases_and_real_dynamics():
    result=monte_carlo({}, {"simulation":SIM,"sea_state":SEA,"runs":3,"seed":7,"vary_wave_phases":True})
    assert len({trial["config"]["sea_state"]["phase_deg"] for trial in result["trials"]})==3
    assert len({trial["metrics"]["peak_top_tension_n"] for trial in result["trials"]})==3


def test_failed_trials_are_reported_and_quantiles_do_not_include_fabricated_values():
    c={"simulation":SIM,"sea_state":{**SEA,"rao_scale":2},"runs":4,"seed":1,"uncertainties":[
        {"scope":"sea_state","parameter":"hs_m","lower":8,"upper":14}]}
    result=monte_carlo({},c)
    assert not result["summary"]["complete"]
    assert result["summary"]["failed_runs"]>0
    assert result["summary"]["successful_runs"]>0
    assert result["summary"]["failed_runs"]+result["summary"]["successful_runs"]==4
    assert any(w["code"]=="INCOMPLETE_MONTE_CARLO" for w in result["warnings"])
    assert result["distributions"]["mean_bottom_tension_n"]["count"]==result["summary"]["converged_runs"]


@pytest.mark.parametrize("c",[{"spectrum":"fake"},{"spectrum":[]},{"spectrum":{}},{"hs_m":-1},{"seed":True},{"component_count":3},
    {"frequency_min_hz":.2,"frequency_max_hz":.1},{"duration_s":7200,"tp_s":.5},
    {"heave_rao":[{"frequency_hz":.25,"amplitude_m_m":1,"phase_deg":0}]},
    {"heave_rao":[{"frequency_hz":2,"amplitude_m_m":1,"phase_deg":0},{"frequency_hz":.005,"amplitude_m_m":1,"phase_deg":0}]},
    {"spectrum":"custom","components":[{"frequency_hz":0,"amplitude_m":1}]},
    {"spectrum":"regular","hs_m":15,"heave_rao":[{"frequency_hz":.125,"amplitude_m_m":10,"phase_deg":0}]}])
def test_invalid_spectrum_rao_or_unbounded_synthesis_rejected(c):
    with pytest.raises(ValueError): generate_sea_state(c)


@pytest.mark.parametrize("c",[{"vessel_motion_series":[{"time_s":0,"heave_m":1},{"time_s":2,"heave_m":0}]},
    {"vessel_motion_series":[{"time_s":0,"heave_m":0},{"time_s":1,"heave_m":0}]},
    {"vessel_motion_series":[{"time_s":0,"heave_m":0},{"time_s":2,"heave_m":0}],"heave_amplitude_m":1},
    {"wave_kinematics":{"depth_m":40,"components":[]}},
    {"seabed_profile":[{"x_m":-100,"depth_m":30},{"x_m":100,"depth_m":31}],"wave_kinematics":{"depth_m":30,"components":[]}},
    {"max_work_units":1}])
def test_invalid_dynamic_wave_motion_or_budget_rejected(c):
    with pytest.raises(ValueError): simulate_lay({}, {**SIM,**c})


@pytest.mark.parametrize("c",[{"runs":25},{"runs":9,"include_frames":True},
    {"uncertainties":[{"scope":[],"parameter":"hs_m","lower":1,"upper":2}]},
    {"uncertainties":[{"scope":"sea_state","parameter":{},"lower":1,"upper":2}]},
    {"uncertainties":[{"scope":"simulation","parameter":"depth_m","lower":10,"upper":20}]},
    {"uncertainties":[{"scope":"simulation","parameter":"current_x_m_s","lower":-21,"upper":1}]},
    {"uncertainties":[{"scope":"sea_state","parameter":"gamma","lower":1,"upper":2}]},
    {"uncertainties":[{"scope":"simulation","parameter":"current_x_m_s","distribution":"normal","lower":0,"upper":1,"std":0}]},
    {"simulation":{**SIM,"duration_s":180,"nodes":48,"internal_dt_s":.025},"runs":24}])
def test_invalid_monte_carlo_distributions_and_aggregate_work_rejected(c):
    with pytest.raises(ValueError): monte_carlo({}, {"simulation":SIM,"sea_state":SEA,**c})


def test_wave_generation_never_invents_missing_vessel_rao():
    generated=generate_sea_state({"duration_s":1,"hs_m":.1})
    assert generated["vessel_motion_series"] is None
    assert any(w["code"]=="VESSEL_RAO_REQUIRED" for w in generated["warnings"])
    with pytest.raises(ValueError,match="requires user heave_rao"):
        simulate_sea({}, {"simulation":SIM,"sea_state":{"hs_m":.1}})


def test_repeated_large_motion_tables_in_checkpoints_have_bounded_json_volume():
    motion=[{"time_s":float(t),"heave_m":0} for t in np.linspace(0,1,10001)]
    with pytest.raises(ValueError,match="JSON volume"):
        simulate_lay({}, {**SIM,"duration_s":1,"dt_s":.02,"vessel_motion_series":motion,"save_checkpoints":True})
