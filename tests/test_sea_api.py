"""Sea workflows must survive API serialization and checkpoint reuse."""
import json

from fastapi.testclient import TestClient
import pytest

from oceanroute.api import create_app
from oceanroute.storage import ProjectStore

SIM={"depth_m":30,"duration_s":2,"nodes":8,"dt_s":.25,"internal_dt_s":.05,
     "bottom_tension_n":100,"ship_speed_m_s":0,"payout_m_s":0}
SEA={"spectrum":"regular","hs_m":.2,"tp_s":4,"wave_direction_deg":90,
     "heave_rao":[{"frequency_hz":.25,"amplitude_m_m":1,"phase_deg":0}]}


@pytest.fixture
def api(tmp_path):
    return TestClient(create_app(ProjectStore(tmp_path/"sea-api.sqlite3")))


@pytest.mark.parametrize("kind",["generate","simulate","montecarlo"])
def test_sea_endpoints_emit_finite_json_for_actual_results(api,kind):
    project=api.get("/api/sample").json()
    config={**SEA,"depth_m":30,"duration_s":2,"ship_speed_m_s":0} if kind=="generate" else {"simulation":SIM,"sea_state":SEA}
    if kind=="montecarlo":
        config.update({"runs":3,"seed":42,"vary_wave_phases":True,"include_frames":True,
            "uncertainties":[{"scope":"simulation","parameter":"current_y_m_s","distribution":"uniform","lower":-.3,"upper":.3}]})
    response=api.post(f"/api/sea/{kind}",json={"project":project,"config":config})
    assert response.status_code==200,response.text
    result=response.json()
    json.dumps(result,allow_nan=False)
    assert result["validation_status"]=="research"
    if kind=="generate":
        assert result["vessel_motion_series"][0]["heave_m"]==0
    elif kind=="simulate":
        assert result["simulation"]["frames"][-1]["ship"][2]==pytest.approx(-.2,abs=1e-8)
        assert result["simulation"]["checkpoint"]["time_s"]==2
    else:
        assert len(result["trials"])==3
        assert len({t["config"]["sea_state"]["phase_deg"] for t in result["trials"]})==3
        assert all(len(t["simulation"]["frames"])>1 for t in result["trials"])


def test_sea_api_resumes_saved_json_state_with_absolute_clock(api):
    project=api.get("/api/sample").json()
    first=api.post("/api/sea/simulate",json={"project":project,"config":{"simulation":SIM,"sea_state":SEA}})
    assert first.status_code==200,first.text
    state=json.loads(json.dumps(first.json()["simulation"]["checkpoint"]))
    second=api.post("/api/sea/simulate",json={"project":project,"config":{
        "simulation":{"resume_state":state,"duration_s":2},"sea_state":SEA}})
    assert second.status_code==200,second.text
    result=second.json()
    assert result["simulation"]["frames"][0]["time_s"]==2
    assert result["simulation"]["frames"][0]["node_velocity_m_s"]==state["state"]["velocities"]
    assert result["simulation"]["frames"][-1]["time_s"]==4
    json.dumps(result,allow_nan=False)


@pytest.mark.parametrize("kind,config",[
    ("simulate",{"simulation":SIM,"sea_state":{"hs_m":.1}}),
    ("generate",{"spectrum":[]}),
    ("montecarlo",{"simulation":SIM,"sea_state":SEA,"uncertainties":[{"scope":[],"parameter":"hs_m"}]}),
])
def test_sea_api_rejects_missing_response_or_malformed_enums(api,kind,config):
    project=api.get("/api/sample").json()
    response=api.post(f"/api/sea/{kind}",json={"project":project,"config":config})
    assert response.status_code==422,response.text
    assert response.json()["detail"]
