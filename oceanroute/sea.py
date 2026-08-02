"""Independent linear sea-state synthesis and bounded dynamic error budgets.

No vessel RAO, equipment accuracy or engineering calibration is invented here.
Directions are propagation bearings clockwise from north; RAO phase is lag.
"""
from __future__ import annotations

from copy import deepcopy
import math

import numpy as np
from scipy.optimize import brentq
from scipy.stats import truncnorm

from .checkpoints import merge_resume_config
from .simulation import (G, _config, _environment, _heading, _integer, _num,
                         _resumed_plan, _ship_plan, _warning, simulate_lay)
from .shipplan import _dynamic_config, _metrics


def _boolean(c,key,default=False):
    value=c.get(key,default)
    if not isinstance(value,bool): raise ValueError(f"{key} must be boolean")
    return value


def _result(model,assumptions):
    return {"model":model,"validation_status":"research","assumptions":assumptions,"warnings":[]}


def _dispersion(frequency,depth):
    omega=2*math.pi*frequency
    upper=2*(omega*omega/G+omega/math.sqrt(G*depth))
    return brentq(lambda k:G*k*math.tanh(k*depth)-omega*omega,1e-14,upper,xtol=1e-13)


class MotionSeries:
    """Prescribed vertical cable-fairlead displacement, with a bounded time span."""
    def __init__(self,rows):
        if not isinstance(rows,list) or not 2<=len(rows)<=10001:
            raise ValueError("vessel_motion_series needs 2 to 10001 ordered time/heave samples")
        if any(not isinstance(row,dict) or not {"time_s","heave_m"}<=row.keys() for row in rows):
            raise ValueError("vessel_motion_series samples require time_s and heave_m")
        self.times=np.array([_num(row,"time_s",0,0,1e9) for row in rows])
        self.values=np.array([_num(row,"heave_m",0,-20,20) for row in rows])
        if self.times[0]!=0 or abs(self.values[0])>1e-8 or np.any(np.diff(self.times)<=0):
            raise ValueError("vessel_motion_series must start at time 0/displacement 0 and have increasing times")
        self.step_cap=float(np.min(np.diff(self.times)))

    def displacement(self,time):
        if time< -1e-8 or time>self.times[-1]+1e-8:
            raise ValueError("vessel_motion_series does not cover the requested continued time")
        return float(np.interp(time,self.times,self.values))


class AiryField:
    """Stable finite-depth linear particle velocities, no acceleration force."""
    def __init__(self,config):
        if not isinstance(config,dict): raise ValueError("wave_kinematics must be an object")
        self.depth=_num(config,"depth_m",100,.01,12000)
        self.time_origin=_num(config,"time_origin_s",0,0,1e9)
        origin=config.get("spatial_origin_xy_m",[0,0])
        if not isinstance(origin,list) or len(origin)!=2:
            raise ValueError("wave spatial_origin_xy_m requires two coordinates")
        self.origin=np.array([_num({"v":v},"v",0,-1e9,1e9) for v in origin])
        rows=config.get("components",[])
        if not isinstance(rows,list) or len(rows)>128:
            raise ValueError("wave components must be an array of at most 128 entries")
        frequencies=[]; amplitudes=[]; phases=[]; directions=[]
        for row in rows:
            if not isinstance(row,dict) or not {"frequency_hz","amplitude_m"}<=row.keys():
                raise ValueError("wave components require frequency_hz and amplitude_m")
            frequencies.append(_num(row,"frequency_hz",.1,.005,2))
            amplitudes.append(_num(row,"amplitude_m",0,0,10))
            phases.append(math.radians(_num(row,"phase_deg",0,-36000,36000)))
            directions.append(_heading(_num(row,"direction_deg",90,-36000,36000))[:2])
        self.omega=2*math.pi*np.array(frequencies)
        self.amplitudes=np.array(amplitudes)
        self.phases=np.array(phases)
        self.directions=np.array(directions).reshape(-1,2)
        self.k=np.array([_dispersion(f,self.depth) for f in frequencies])
        if np.sum(self.amplitudes)>30:
            raise ValueError("sum of wave amplitudes exceeds the linear research field limit")
        active_frequencies=np.array(frequencies)[self.amplitudes>0]
        self.step_cap=1/(40*float(np.max(active_frequencies))) if len(active_frequencies) else .25

    def velocity(self,positions,time):
        positions=np.asarray(positions,dtype=float)
        if not np.any(self.amplitudes): return np.zeros_like(positions)
        z=np.clip(positions[:,2],-self.depth,0)[:,None]
        denominator=-np.expm1(-2*self.k*self.depth)
        positive=np.exp(self.k*z)
        reflected=np.exp(-self.k*(2*self.depth+z))
        horizontal=(positive+reflected)/denominator
        vertical=(positive-reflected)/denominator
        phase=self.omega*(time-self.time_origin)+self.phases-((positions[:,:2]-self.origin)@self.directions.T)*self.k
        amplitude=self.amplitudes*self.omega
        xy=(horizontal*amplitude*np.cos(phase))@self.directions
        vz=np.sum(-vertical*amplitude*np.sin(phase),axis=1)
        return np.column_stack([xy,vz])

    def elevation(self,positions_xy,time):
        p=np.asarray(positions_xy,dtype=float).reshape(-1,2)
        phase=self.omega*(time-self.time_origin)+self.phases-((p-self.origin)@self.directions.T)*self.k
        return np.sum(self.amplitudes*np.cos(phase),axis=1)


def _rao(rows,frequencies,scale):
    if not isinstance(rows,list) or not 1<=len(rows)<=500:
        raise ValueError("heave_rao needs 1 to 500 frequency/amplitude/phase rows")
    if any(not isinstance(row,dict) or not {"frequency_hz","amplitude_m_m","phase_deg"}<=row.keys() for row in rows):
        raise ValueError("heave_rao rows require frequency_hz, amplitude_m_m and phase_deg")
    grid=np.array([_num(row,"frequency_hz",.1,.005,2) for row in rows])
    amplitudes=np.array([_num(row,"amplitude_m_m",0,0,10) for row in rows])*scale
    phases=np.radians([_num(row,"phase_deg",0,-36000,36000) for row in rows])
    if np.any(np.diff(grid)<=0): raise ValueError("heave_rao frequencies must strictly increase")
    if len(frequencies) and (np.min(frequencies)<grid[0]-1e-12 or np.max(frequencies)>grid[-1]+1e-12):
        raise ValueError("heave_rao must cover all active wave frequencies; extrapolation is not allowed")
    complex_response=amplitudes*np.exp(-1j*phases)
    return np.interp(frequencies,grid,complex_response.real)+1j*np.interp(frequencies,grid,complex_response.imag)


def _track(config,times):
    rows=_ship_plan(config,_environment(config),float(times[-1]))
    start=config.get("vessel_start_xy_m",[0,0])
    if not isinstance(start,list) or len(start)!=2: raise ValueError("vessel_start_xy_m needs two numbers")
    start=np.array([_num({"v":v},"v",0,-1e9,1e9) for v in start])
    output=np.broadcast_to(start,(len(times),2)).copy()
    for i,row in enumerate(rows):
        end=rows[i+1]["time_s"] if i+1<len(rows) else times[-1]
        elapsed=np.maximum(np.minimum(times,end)-row["time_s"],0)
        output+=elapsed[:,None]*row["speed_m_s"]*_heading(row["heading_deg"])[:2]
    return output,rows


def generate_sea_state(config:dict)->dict:
    """Synthesize a finite spectral realization and user-defined heave response."""
    c=deepcopy(_config(config))
    duration=_num(c,"duration_s",30,0,7200,strict=True)
    interval=_num(c,"sample_dt_s",.1,.002,10)
    absolute_start=_num(c,"absolute_start_time_s",0,0,1e9)
    origin=_num(c,"time_origin_s",0,0,1e9)
    seed=_integer(c,"seed",2026,0,2**32-1)
    kind=c.get("spectrum","jonswap")
    if not isinstance(kind,str) or kind not in {"jonswap","pm","regular","custom"}:
        raise ValueError("spectrum must be jonswap, pm, regular or custom")
    direction=_num(c,"wave_direction_deg",90,-36000,36000)
    depth=_num(c,"depth_m",100,.01,12000)
    rows=[]; spectrum_rows=[]
    rng=np.random.default_rng(seed)
    if kind=="custom":
        raw=c.get("components")
        if not isinstance(raw,list) or not 1<=len(raw)<=128:
            raise ValueError("custom components require 1 to 128 wave entries")
        rows=deepcopy(raw)
        for row in rows:
            if isinstance(row,dict): row.setdefault("direction_deg",direction)
    else:
        height=_num(c,"hs_m",1,0,15)
        period=_num(c,"tp_s",8,.5,60)
        peak=1/period
        if kind=="regular":
            rows=[{"frequency_hz":peak,"amplitude_m":height/2,
                   "phase_deg":_num(c,"phase_deg",0,-36000,36000),"direction_deg":direction}]
        else:
            count=_integer(c,"component_count",32,4,128)
            low=_num(c,"frequency_min_hz",max(.005,.3*peak),.005,2)
            high=_num(c,"frequency_max_hz",min(2,5*peak),.005,2)
            if not low<peak<high: raise ValueError("wave frequency band must contain the spectral peak")
            frequency=np.linspace(low,high,count+1)
            widths=np.diff(frequency)
            frequency=(frequency[:-1]+frequency[1:])/2
            gamma=1. if kind=="pm" else _num(c,"gamma",3.3,1,10)
            sigma=np.where(frequency<=peak,.07,.09)
            log_shape=-5*np.log(frequency)-1.25*(peak/frequency)**4+np.log(gamma)*np.exp(-.5*((frequency/peak-1)/sigma)**2)
            shape=np.exp(log_shape-np.max(log_shape))
            density=shape*((height/4)**2/np.sum(shape*widths))
            amplitude=np.sqrt(2*density*widths)
            phases=rng.uniform(0,360,count)
            rows=[{"frequency_hz":float(f),"amplitude_m":float(a),"phase_deg":float(p),"direction_deg":direction}
                  for f,a,p in zip(frequency,amplitude,phases)]
            spectrum_rows=[{"frequency_hz":float(f),"density_m2_hz":float(s),"bandwidth_hz":float(df)}
                           for f,s,df in zip(frequency,density,widths)]
    wave_config={"depth_m":depth,"time_origin_s":origin,"components":rows,
                 "spatial_origin_xy_m":c.get("spatial_origin_xy_m",[0,0])}
    field=AiryField(wave_config)
    active=field.amplitudes>0
    active_frequencies=field.omega[active]/(2*math.pi)
    fixed=_boolean(c,"fixed_vessel_heave")
    scale=_num(c,"rao_scale",1,0,10)
    response=None
    if fixed:
        response=np.zeros(np.count_nonzero(active),dtype=complex)
    elif "heave_rao" in c:
        response=_rao(c["heave_rao"],active_frequencies,scale)
    elif not np.any(active):
        response=np.zeros(np.count_nonzero(active),dtype=complex)
    # Include vessel encounter frequencies when choosing a motion sampling grid.
    plan=_ship_plan(c,_environment(c),duration)
    max_speed=max(row["speed_m_s"] for row in plan)
    encounter=(field.omega[active]+field.k[active]*max_speed)/(2*math.pi)
    if len(encounter): interval=min(interval,1/(40*float(np.max(encounter))))
    count=math.ceil(duration/interval)
    if count>10000 or count*max(1,len(rows))>1500000:
        raise ValueError("sea-state synthesis exceeds sample/component budget; shorten duration or frequency band")
    times=np.linspace(0,duration,count+1)
    track,plan=_track(c,times)
    phase=(absolute_start+times[:,None]-origin)*field.omega[active]+field.phases[active]-(track-field.origin)@field.directions[active].T*field.k[active]
    eta=np.sum(field.amplitudes[active]*np.cos(phase),axis=1)
    heave=None
    if response is not None:
        heave=np.real(np.sum(field.amplitudes[active]*response*np.exp(1j*phase),axis=1))
        heave-=heave[0]
        if np.max(np.abs(heave))>20: raise ValueError("RAO motion exceeds 20 m research displacement limit")
    result=_result("linear-discrete-spectrum-user-rao-v1",[
        "JONSWAP/PM shapes are normalized on the finite band to Hs=4sqrt(m0); regular hs_m is crest-to-trough height.",
        "Random phases use an explicit seed. Finite records do not have exactly the ensemble variance.",
        "User heave RAOs are complex-linearly interpolated by incident frequency, amplitude per wave amplitude and positive phase lag.",
        "Wave propagation and vessel commands set encounter phase; one supplied RAO table is used for all headings and speeds.",
        "Heave is prescribed at the cable fairlead; the initial displacement is shifted to zero. No vessel response is calibrated here."])
    result["warnings"].append(_warning("LINEAR_UNCALIBRATED_SEA","Small-amplitude, constant-depth, unidirectional/explicit-component research waves; no breaking, directional RAO interpolation, radiation, diffraction or vessel dynamics."))
    if response is None:
        result["warnings"].append(_warning("VESSEL_RAO_REQUIRED","No vessel heave response was supplied. Enter a heave_rao table or explicitly choose fixed_vessel_heave before dynamic simulation."))
    if np.max(field.k*field.amplitudes,initial=0)>.1 or np.sum(field.amplitudes)/depth>.1:
        result["warnings"].append(_warning("LINEAR_WAVE_RANGE","Component steepness or wave/depth ratio is outside a conservative small-amplitude range; linear kinematics may be inappropriate."))
    result.update({"seed":seed,"spectrum":spectrum_rows,"components":rows,"wave_kinematics":wave_config,
        "vessel_motion_series":[{"time_s":float(t),"heave_m":float(z)} for t,z in zip(times,heave)] if heave is not None else None,
        "samples":[{"time_s":float(t),"absolute_time_s":float(t+absolute_start),"surface_elevation_m":float(y),
                    "heave_m":float(heave[i]) if heave is not None else None,"vessel_xy_m":track[i].tolist()} for i,(t,y) in enumerate(zip(times,eta))],
        "summary":{"duration_s":duration,"absolute_start_time_s":absolute_start,"sample_dt_s":float(times[1]-times[0]),
            "component_count":len(rows),"spectral_m0_m2":float(np.sum(field.amplitudes**2)/2),
            "spectral_hm0_m":float(4*np.sqrt(np.sum(field.amplitudes**2)/2)),
            "sample_surface_std_m":float(np.std(eta)),"peak_heave_displacement_m":float(np.max(np.abs(heave))) if heave is not None else None,
            "rao_phase_convention":"positive phase_deg is lag relative to local wave crest; complex transfer amplitude*exp(-i phase)",
            "initial_heave_dc_shift_m":float(np.real(np.sum(field.amplitudes[active]*response*np.exp(1j*phase[0])))) if response is not None else None}})
    return result


def simulate_sea(project:dict,config:dict)->dict:
    c=_config(config)
    raw=deepcopy(_config(c.get("simulation",{})))
    merged,saved=merge_resume_config(raw)
    sea=deepcopy(_config(c.get("sea_state",{})))
    duration=_num(merged,"duration_s",30,0,180,strict=True)
    merged["duration_s"]=duration
    if saved:
        plan,_=_resumed_plan(raw,merged,saved,_environment(merged),duration)
        start=saved["time_s"]
        index=np.searchsorted([row["time_s"] for row in plan],start+1e-10,side="right")-1
        active=plan[max(0,index)]
        sea["ship_plan"]=[{**active,"time_s":0.}]+[{**row,"time_s":row["time_s"]-start} for row in plan if start+1e-10<row["time_s"]<=start+duration]
        sea["vessel_start_xy_m"]=saved["state"]["ship"][:2]
        sea["absolute_start_time_s"]=start
    else:
        for key in ("ship_plan","ship_speed_m_s","heading_deg","ship_plan_horizon_s"):
            if key in merged: sea[key]=deepcopy(merged[key])
    sea["duration_s"]=duration
    sea["depth_m"]=_num(merged,"depth_m",1000,.01,12000)
    generated=generate_sea_state(sea)
    if generated["vessel_motion_series"] is None:
        raise ValueError("sea dynamic simulation requires user heave_rao or explicit fixed_vessel_heave")
    raw["duration_s"]=duration
    raw["vessel_motion_series"]=generated["vessel_motion_series"]
    raw["heave_amplitude_m"]=0.
    if _boolean(sea,"include_fluid_kinematics",True): raw["wave_kinematics"]=generated["wave_kinematics"]
    else: raw["wave_kinematics"]=None
    simulation=simulate_lay(project,raw)
    window=_num(c,"evaluation_window_s",duration/4,0,duration,strict=True)
    result=_result("linear-sea-driven-material-dynamics-v1",[
        "Actual cable dynamics are driven by sampled user-RAO fairlead heave and optional linear Airy particle velocities.",
        "Water particle velocities enter relative quadratic drag only; wave acceleration/Froude-Krylov/inertia loads are absent."])
    result.update({"sea_state":generated,"simulation":simulation,"metrics":_metrics(simulation,window),
                   "effective_simulation_config":raw,"warnings":generated["warnings"]+simulation["warnings"]})
    return result


def monte_carlo(project:dict,config:dict)->dict:
    """Sample declared independent input distributions, then actually simulate."""
    c=deepcopy(_config(config))
    runs=_integer(c,"runs",8,2,24)
    seed=_integer(c,"seed",2026,0,2**32-1)
    vary=_boolean(c,"vary_wave_phases")
    include=_boolean(c,"include_frames")
    if include and runs>8: raise ValueError("Monte Carlo with full frames is limited to 8 trials")
    sim=_dynamic_config(project,c.get("simulation",{}),runs+1)
    c["simulation"]=sim
    sea_input=deepcopy(_config(c.get("sea_state",{})))
    c["sea_state"]=sea_input
    declarations=c.get("uncertainties",[])
    if not isinstance(declarations,list) or len(declarations)>8:
        raise ValueError("uncertainties must be an array of at most 8 independent distributions")
    allowed={"simulation":{"ship_speed_m_s","payout_m_s","heading_deg","current_x_m_s","current_y_m_s"},
             "sea_state":{"hs_m","tp_s","gamma","wave_direction_deg","rao_scale"}}
    bounds={"ship_speed_m_s":(0,20),"payout_m_s":(0,25),"heading_deg":(-36000,36000),
        "current_x_m_s":(-20,20),"current_y_m_s":(-20,20),"hs_m":(0,15),"tp_s":(.5,60),
        "gamma":(1,10),"wave_direction_deg":(-36000,36000),"rao_scale":(0,10)}
    validated=[]; names=set()
    for item in declarations:
        if not isinstance(item,dict): raise ValueError("uncertainty entries must be objects")
        scope=item.get("scope"); parameter=item.get("parameter"); kind=item.get("distribution","uniform")
        if not isinstance(scope,str) or not isinstance(parameter,str) or scope not in allowed or parameter not in allowed[scope] or (scope,parameter) in names:
            raise ValueError("uncertainty parameter is unsupported or repeated")
        names.add((scope,parameter))
        low=_num(item,"lower",0,-36000,36000); high=_num(item,"upper",0,-36000,36000)
        if high<low or "lower" not in item or "upper" not in item:
            raise ValueError("uncertainties require lower<=upper")
        if low<bounds[parameter][0] or high>bounds[parameter][1]:
            raise ValueError("uncertainty bounds exceed the supported physical parameter range")
        spectrum=sea_input.get("spectrum","jonswap")
        if not isinstance(spectrum,str): raise ValueError("spectrum must be a string")
        if scope=="sea_state" and ((spectrum=="custom" and parameter in {"hs_m","tp_s","gamma","wave_direction_deg"}) or (spectrum in {"regular","pm"} and parameter=="gamma")):
            raise ValueError("uncertainty does not affect the selected spectrum; choose a meaningful parameter")
        if scope=="sea_state" and parameter=="rao_scale" and _boolean(sea_input,"fixed_vessel_heave"):
            raise ValueError("RAO uncertainty does not affect fixed_vessel_heave")
        if kind=="normal":
            mean=_num(item,"mean",(low+high)/2,low,high)
            std=_num(item,"std",(high-low)/4,0,36000)
            if high>low and std<=0: raise ValueError("bounded normal uncertainty needs positive std")
        elif kind=="uniform": mean=std=None
        else: raise ValueError("uncertainty distribution must be uniform or bounded normal")
        if scope=="simulation" and parameter in {"ship_speed_m_s","payout_m_s","heading_deg"} and sim.get("ship_plan"):
            raise ValueError("scalar control uncertainty with an explicit ship_plan is ambiguous; use scalar controls or current/sea uncertainties")
        validated.append((scope,parameter,kind,low,high,mean,std))
    budget=12000000.
    c["simulation"]["max_work_units"]=budget/(runs+1)
    baseline=simulate_sea(project,c)
    base=baseline["simulation"]["solver"]["estimated_work_units"]
    if base*(runs+1)>12000000:
        raise ValueError("Monte Carlo aggregate sea/dynamic computation exceeds its limit; shorten duration or reduce runs/nodes/components")
    rng=np.random.default_rng(seed)
    budget-=base
    trials=[]; failures=[]
    for i in range(runs):
        trial={"simulation":deepcopy(sim),"sea_state":deepcopy(c.get("sea_state",{}))}
        trial["simulation"]["max_work_units"]=max(1,budget/(runs-i))
        values={}
        for scope,parameter,kind,low,high,mean,std in validated:
            if high==low: value=low
            elif kind=="uniform": value=float(rng.uniform(low,high))
            else: value=float(truncnorm.ppf(rng.uniform(),(low-mean)/std,(high-mean)/std,loc=mean,scale=std))
            trial[scope][parameter]=value
            values[f"{scope}.{parameter}"]=value
        if vary:
            trial["sea_state"]["seed"]=int(rng.integers(0,2**32-1))
            # Regular/custom waves have explicit phases, not seeded phases.
            # The requested phase experiment must alter those actual inputs.
            if trial["sea_state"].get("spectrum","jonswap")=="regular":
                trial["sea_state"]["phase_deg"]=float(rng.uniform(0,360))
            elif trial["sea_state"].get("spectrum")=="custom":
                for component in trial["sea_state"].get("components",[]):
                    component["phase_deg"]=float(rng.uniform(0,360))
        trial["evaluation_window_s"]=c.get("evaluation_window_s",sim["duration_s"]/4)
        try:
            output=simulate_sea(project,trial)
            budget-=output["simulation"]["solver"]["estimated_work_units"]
            record={"trial":i+1,"parameters":values,"config":trial,"metrics":output["metrics"],
                    "solver":output["simulation"]["solver"],"warnings":output["warnings"]}
            if include: record["simulation"]=output["simulation"]
            trials.append(record)
        except ValueError as error:
            failures.append({"trial":i+1,"parameters":values,"error":str(error)})
    converged=[trial for trial in trials if trial["solver"]["converged"]]
    distributions={}
    for name in ("mean_bottom_tension_n","peak_top_tension_n","peak_segment_tension_n","minimum_bend_radius_m","paid_out_m"):
        values=np.array([trial["metrics"][name] for trial in converged if trial["metrics"][name] is not None])
        distributions[name]={"count":len(values),"mean":float(np.mean(values)) if len(values) else None,
            "standard_deviation":float(np.std(values,ddof=1)) if len(values)>1 else None,
            "q05":float(np.quantile(values,.05)) if len(values) else None,
            "q50":float(np.quantile(values,.5)) if len(values) else None,"q95":float(np.quantile(values,.95)) if len(values) else None}
    touchdown=np.array([trial["metrics"]["final_touchdown"] for trial in converged])
    if len(touchdown):
        offset=touchdown-np.array(baseline["metrics"]["final_touchdown"])
        radius=np.linalg.norm(offset[:,:2],axis=1)
        distributions["touchdown_shift_m"]={"count":len(radius),"mean":float(np.mean(radius)),
            "standard_deviation":float(np.std(radius,ddof=1)) if len(radius)>1 else None,
            "q05":float(np.quantile(radius,.05)),"q50":float(np.quantile(radius,.5)),"q95":float(np.quantile(radius,.95))}
    result=_result("bounded-real-dynamic-monte-carlo-v1",[
        "Every trial runs the actual material-node cable model; input distributions and independent sampling are user assumptions.",
        "Uncertainty values are absolute scalar parameters. Bounded normal distributions use exact inverse-CDF sampling without clipping.",
        "Wave phase variation is optional and uses declared seeds; no equipment accuracy, sensor filtering or feedback control model is inferred.",
        "Quantiles describe the converged sampled model outputs, not calibrated risk probabilities or confidence intervals of the true vessel."])
    result["warnings"].append(_warning("RESEARCH_MONTE_CARLO","Small bounded trial counts are exploratory. Distribution choice, correlations, rare tails and physical calibration require independent validation."))
    if failures or len(converged)!=len(trials):
        result["warnings"].append(_warning("INCOMPLETE_MONTE_CARLO","Some trials failed or did not converge; statistics are conditional on converged trials. Inspect every trial/failure."))
    result.update({"seed":seed,"baseline_metrics":baseline["metrics"],"trials":trials,"failures":failures,
        "distributions":distributions,"summary":{"requested_runs":runs,"successful_runs":len(trials),
            "converged_runs":len(converged),"failed_runs":len(failures),"vary_wave_phases":vary,
            "complete":len(converged)==runs,"estimated_baseline_work_units":base}})
    return result
