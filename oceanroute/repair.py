"""Uniform-line repair research: recovery snapshots, tow rope and buoy sizing.

There is no hook/contact dynamics, retrieval controller or six-DOF buoy model.
"""
from __future__ import annotations

from copy import deepcopy
import math

import numpy as np
from scipy.integrate import solve_ivp

from .simulation import G, _config, _environment, _heading, _integer, _num, _warning, catenary


def _result(model,assumptions):
    return {"model":model,"validation_status":"research","assumptions":assumptions,"warnings":[]}


def _uniform(config):
    c=deepcopy(_config(config))
    for key in ("material_segments","inline_bodies","seabed_profile","current_profile",
                "wave_kinematics","vessel_motion_series","resume_state","ship_plan"):
        if key in c and c[key] is not None and c[key]!=[]:
            raise ValueError(f"repair uniform quasi-static model does not support {key}")
    if _num(c,"ei_n_m2",0,0,1e10)>0 or _num(c,"heave_amplitude_m",0,0,20)>0:
        raise ValueError("repair quasi-static model omits bending and prescribed wave motion")
    if _num(c,"retrieval_speed_m_s",0,0,20)>0:
        raise ValueError("repair tools are static snapshots, not a retrieval-speed dynamics model")
    return c


def _vector(value,field,n=3):
    if not isinstance(value,list) or len(value)!=n:
        raise ValueError(f"{field} requires {n} finite force components")
    return np.array([_num({"v":v},"v",0,-1e12,1e12) for v in value])


def _forces(top,bottom,weight,drag):
    gravity=np.array([0.,0.,-weight])
    balance=top-bottom+gravity+drag
    return {"required_top_support_n":top.tolist(),"rope_on_bottom_n":bottom.tolist(),
            "cable_on_top_n":(-top).tolist(),"integrated_gravity_n":gravity.tolist(),
            "integrated_drag_n":drag.tolist(),"balance_residual_n":balance.tolist(),
            "balance_residual_norm_n":float(np.linalg.norm(balance))}


def recovery_shape(config:dict)->dict:
    """Exact static snapshots parameterized by remaining suspended arc length."""
    c=_uniform(config)
    depth=_num(c,"depth_m",1000,.001,12000)
    weight=_num(c,"wet_weight_n_m",4,1e-6,20000)
    nodes=_integer(c,"nodes",64,3,500)
    heading=_num(c,"heading_deg",90,-36000,36000)
    if any(key in c for key in ("bottom_tension_n","cable_length_m","layback_m")):
        raise ValueError("recovery_shape length determines tension and layback; do not prescribe additional boundary controls")
    for key in ("ship_speed_m_s","current_x_m_s","current_y_m_s","retrieval_speed_m_s"):
        if _num(c,key,0,-20 if "current" in key else 0,20)!=0:
            raise ValueError("recovery_shape is a no-flow static snapshot; use steady_tow for prescribed tow/current force, not recovery dynamics")
    if "suspended_length_m" in c:
        if "initial_suspended_length_m" in c or "retrieved_length_m" in c:
            raise ValueError("specify suspended_length_m or initial_suspended_length_m/retrieved_length_m, not both")
        length=_num(c,"suspended_length_m",depth,0,1e6,strict=True)
        initial=None; retrieved=None
    elif "initial_suspended_length_m" in c:
        initial=_num(c,"initial_suspended_length_m",depth,depth,1e6)
        retrieved=_num(c,"retrieved_length_m",0,0,1e6)
        length=initial-retrieved
    else:
        raise ValueError("recovery_shape requires suspended_length_m or initial_suspended_length_m")
    if length<depth:
        raise ValueError("remaining suspended length is shorter than depth; this recovery geometry is impossible")
    solved=catenary({"depth_m":depth,"wet_weight_n_m":weight,"nodes":nodes,"heading_deg":heading,"cable_length_m":length})
    summary=solved["summary"]
    if summary["top_tension_n"]>1e12:
        raise ValueError("recovery end force exceeds the research force limit")
    bottom=summary["bottom_tension_n"]*_heading(heading)
    top=bottom+np.array([0.,0.,weight*length])
    result=_result("uniform-quasi-static-recovery-catenary-v1",[
        "Uniform, fully immersed, inextensible, flexible cable with horizontal tangent at a flat seabed touchdown.",
        "Retrieved length is subtracted from the initially suspended arc, not from the entire installed cable route.",
        "Each remaining-length snapshot has a freely shifting touchdown; no fixed bottom anchor or time history is implied.",
        "No current, recovery speed, soil suction, seabed dragging, inertia, elasticity, bending or inline bodies."])
    result["warnings"]=deepcopy(solved["warnings"])
    result.update({"nodes":solved["nodes"],"node_tension_n":solved["node_tension_n"],
        "arc_from_touchdown_m":np.linspace(length,0,nodes).tolist(),"end_forces":_forces(top,bottom,weight*length,np.zeros(3)),
        "summary":{**summary,"initial_suspended_length_m":initial,"retrieved_length_m":retrieved,
            "remaining_suspended_length_m":length,"required_lift_tension_n":float(np.linalg.norm(top)),
            "required_vertical_lift_n":float(top[2]),"required_horizontal_lift_n":float(np.linalg.norm(top[:2]))},
        "solver":solved["solver"]})
    if "max_tension_n" in c and summary["top_tension_n"]>_num(c,"max_tension_n",1e12,0,1e12):
        result["warnings"].append(_warning("RECOVERY_TENSION_LIMIT","Static top tension exceeds the user cable limit; this is not a retrieval safety certification."))
    return result


def steady_tow(config:dict)->dict:
    """Integrate actual 3D static rope equilibrium in the translating frame."""
    c=_uniform(config)
    if any(key in c for key in ("suspended_length_m","initial_suspended_length_m","retrieved_length_m","cable_length_m")):
        raise ValueError("steady_tow prescribes bottom force and solves suspended length; use recovery_shape for length control")
    e=_environment(c)
    n=_integer(c,"nodes",64,3,500)
    heading=_num(c,"bottom_heading_deg",e["heading"],-36000,36000)
    bottom=e["bottom"]*_heading(heading)
    relative=e["current"]-_heading(e["heading"])*e["speed"]
    coefficient=.5*e["rho"]*e["cd"]*e["diameter"]
    calls=0
    if coefficient==0 or np.linalg.norm(relative)<1e-12:
        solved=catenary({"depth_m":e["depth"],"wet_weight_n_m":e["weight"],"bottom_tension_n":e["bottom"],"heading_deg":heading,"nodes":n})
        length=solved["summary"]["suspended_length_m"]
        positions=np.array(solved["nodes"])
        forces=bottom+np.linspace(length,0,n)[:,None]*np.array([0.,0.,e["weight"]])
        top=forces[0]; drag=np.zeros(3); residual=0.
    else:
        if e["bottom"]<=1e-8:
            raise ValueError("flow-loaded steady_tow requires positive bottom horizontal tension")
        estimate=math.sqrt(e["depth"]*(e["depth"]+2*e["bottom"]/e["weight"]))
        limit=min(1e6,max(10*estimate,10*e["depth"]))
        def rhs(s,state):
            nonlocal calls
            calls+=1
            if calls>20000: raise ValueError("steady_tow integration exceeded 20000 force evaluations")
            tension=state[3:6]
            tangent=tension/max(np.linalg.norm(tension),1e-12)
            normal=relative-np.dot(relative,tangent)*tangent
            drag=coefficient*np.linalg.norm(normal)*normal
            return np.r_[tangent,-drag+np.array([0.,0.,e["weight"]]),drag]
        def surface(s,state): return state[2]-e["depth"]
        surface.terminal=True; surface.direction=1
        integration=solve_ivp(rhs,(0,limit),np.r_[np.zeros(3),bottom,np.zeros(3)],
                              events=surface,dense_output=True,rtol=1e-8,atol=1e-8,max_step=limit/400)
        if not integration.success or len(integration.t_events[0])!=1:
            raise ValueError("steady_tow did not reach the surface within the bounded rope length")
        length=float(integration.t_events[0][0])
        states=integration.sol(np.linspace(length,0,n)).T
        positions=states[:,:3]-states[0,:3]
        forces=states[:,3:6]
        top=forces[0]; drag=states[0,6:9]
        residual=abs(float(states[0,2])-e["depth"])
    if length>1e6 or not np.isfinite(positions).all() or not np.isfinite(forces).all():
        raise ValueError("steady_tow geometry exceeds finite research bounds")
    if np.min(positions[:,2])< -e["depth"]-1e-6 or np.max(positions[:,2])>1e-6:
        raise ValueError("steady_tow geometry crosses the declared flat seabed or surface")
    tensions=np.linalg.norm(forces,axis=1)
    if np.max(tensions)>1e12: raise ValueError("steady_tow force exceeds the research limit")
    result=_result("uniform-normal-drag-steady-tow-v1",[
        "Uniform inextensible rope with positive submerged weight, no bending, inertia or axial drag.",
        "Actual steady force integration uses current minus vessel velocity and independently specified bottom-force bearing.",
        "Bottom traction is horizontal at the declared flat seabed, with zero vertical tangent; no seabed cable/contact dynamics."])
    if e["bottom"]==0:
        result["warnings"].append(_warning("ZERO_TOW_FORCE","Zero horizontal bottom force is the vertical static limit, not a driven dragging solution."))
    result.update({"nodes":positions.tolist(),"node_tension_n":tensions.tolist(),
        "node_force_vectors_n":forces.tolist(),"arc_from_touchdown_m":np.linspace(length,0,n).tolist(),
        "end_forces":_forces(top,bottom,e["weight"]*length,drag),
        "summary":{"depth_m":e["depth"],"suspended_length_m":length,
            "layback_m":float(np.linalg.norm(positions[-1,:2])),"touchdown":positions[-1].tolist(),
            "top_tension_n":float(tensions[0]),"maximum_tension_n":float(np.max(tensions)),
            "bottom_tension_n":e["bottom"],"bottom_heading_deg":heading,
            "relative_current_m_s":relative.tolist(),"top_angle_from_horizontal_deg":math.degrees(math.atan2(top[2],np.linalg.norm(top[:2]))),
            "touchdown_tangent":(bottom/e["bottom"]).tolist() if e["bottom"] else [0.,0.,1.],
            "geometry_above_seabed":True},
        "solver":{"converged":True,"function_evaluations":calls,"vertical_residual_m":residual}})
    return result


def estimate_grapnel_rope(config:dict)->dict:
    """Conditional rope length for an ideal sled already sliding on a flat bed."""
    c=_uniform(config)
    if "bottom_tension_n" in c or "bottom_heading_deg" in c:
        raise ValueError("grapnel rope bottom force is derived from body drag and friction; do not prescribe it independently")
    e=_environment(c)
    if "grapnel_wet_weight_n" not in c: raise ValueError("grapnel_wet_weight_n is required")
    weight=_num(c,"grapnel_wet_weight_n",0,0,1e8,strict=True)
    if "grapnel_mass_kg" in c and weight>_num(c,"grapnel_mass_kg",0,0,1e8)*G:
        raise ValueError("grapnel wet weight exceeds dry mass gravity")
    friction=_num(c,"grapnel_friction_coefficient",.5,0,2)
    area=_num(c,"grapnel_drag_area_m2",0,0,10000)
    cd=_num(c,"grapnel_drag_coefficient",1.2,0,10)
    if e["speed"]==0 and friction>0:
        raise ValueError("grapnel kinetic-friction rope estimate requires positive sliding speed; use steady_tow for a stationary boundary")
    direction=_heading(e["heading"])
    relative=e["current"]-e["speed"]*direction
    drag=.5*e["rho"]*cd*area*np.linalg.norm(relative)*relative
    resistance=-friction*weight*direction
    rope=-(drag+resistance)
    horizontal=float(np.linalg.norm(rope[:2]))
    if horizontal>1e9: raise ValueError("grapnel bottom load exceeds steady-tow bounds")
    bearing=math.degrees(math.atan2(rope[0],rope[1])) if horizontal else e["heading"]
    shape=steady_tow({**c,"bottom_tension_n":horizontal,"bottom_heading_deg":bearing})
    length=shape["summary"]["suspended_length_m"]
    extra=_num(c,"extra_rope_m",0,0,1e6)
    required=length+extra
    available=_num(c,"available_rope_length_m",required,0,1e6) if "available_rope_length_m" in c else None
    balance=rope+drag+resistance
    result=_result("conditional-grapnel-rope-steady-estimate-v1",[
        "Grapnel is an ideal bed-supported sled with attachment at seabed level and horizontal rope traction.",
        "User submerged weight, kinetic friction and isotropic body drag set the actual bottom rope force; rope weight/drag are independently integrated.",
        "The geometry assumes existing sliding contact. It does not simulate lowering, landing, hooking, snagging, rotation or soil penetration.",
        "Extra rope is a declared allowance; drag/friction of a seabed tail is not included."])
    result["warnings"]=deepcopy(shape["warnings"])
    if available is not None and available<length:
        result["warnings"].append(_warning("INSUFFICIENT_GRAPNEL_ROPE","Available rope is shorter than the computed suspended arc; the assumed bottom-contact geometry cannot be achieved."))
    result.update({"rope_shape":shape,"nodes":shape["nodes"],"node_tension_n":shape["node_tension_n"],
        "grapnel_forces":{"rope_n":rope.tolist(),"drag_n":drag.tolist(),"seabed_friction_n":resistance.tolist(),
            "seabed_normal_n":weight,"horizontal_balance_residual_n":balance[:2].tolist(),
            "horizontal_balance_residual_norm_n":float(np.linalg.norm(balance[:2]))},
        "summary":{"minimum_suspended_rope_m":length,"extra_rope_m":extra,"required_paid_rope_m":required,
            "available_rope_length_m":available,"rope_shortfall_m":max(0,required-available) if available is not None else None,
            "available_length_reaches_seabed":available>=length if available is not None else None,
            "declared_allowance_met":available>=required if available is not None else None,
            "bottom_contact_assumed":True,"bottom_normal_force_n":weight,
            "bottom_rope_force_n":horizontal,"bottom_rope_bearing_deg":bearing,
            "top_tension_n":shape["summary"]["top_tension_n"],"touchdown":shape["summary"]["touchdown"]}})
    return result


def size_buoy(config:dict)->dict:
    """Vertical-cylinder hydrostatic sizing against real cable endpoint force."""
    c=deepcopy(_config(config))
    rho=_num(c,"water_density_kg_m3",1025,1,2000)
    if "buoy_mass_kg" not in c or "height_m" not in c:
        raise ValueError("buoy_mass_kg and vertical-cylinder height_m are required")
    mass=_num(c,"buoy_mass_kg",0,0,1e8)
    height=_num(c,"height_m",1,.001,1000)
    freeboard=_num(c,"freeboard_m",0,0,height)
    if freeboard>=height: raise ValueError("freeboard_m must be less than buoy height")
    reserve=_num(c,"reserve_pct",20,0,500)/100
    rigging=_num(c,"rigging_wet_weight_n",0,0,1e9)
    payload=_num(c,"above_water_payload_mass_kg",0,0,1e8)
    if ("cable" in c)==("support_force_n" in c):
        raise ValueError("provide exactly one cable model or support_force_n vector")
    shape=None
    if "cable" in c:
        cable=deepcopy(_config(c["cable"]))
        if "water_density_kg_m3" in cable and cable["water_density_kg_m3"]!=rho:
            raise ValueError("buoy and uniform cable water density must agree")
        cable["water_density_kg_m3"]=rho
        method=c.get("cable_model","recovery")
        if method=="recovery": shape=recovery_shape(cable)
        elif method=="tow": shape=steady_tow(cable)
        else: raise ValueError("cable_model must be recovery or tow")
        top=np.array(shape["end_forces"]["required_top_support_n"])
    else: top=_vector(c["support_force_n"],"support_force_n")
    if top[2]<0: raise ValueError("buoy sizing requires a nonnegative supported downward cable load")
    load=float(top[2]+(mass+payload)*G+rigging)
    if load<=0: raise ValueError("buoy sizing requires a positive total supported load")
    designed=load*(1+reserve)
    fraction=1-freeboard/height
    minimum=designed/(rho*G*fraction)
    if minimum>1e6: raise ValueError("required buoy displacement exceeds the research volume limit")
    volume=_num(c,"displacement_volume_m3",minimum,0,1e6,strict=True)
    area=volume/height
    diameter=math.sqrt(4*area/math.pi)
    maximum_buoyancy=rho*G*volume
    capacity_at_freeboard=maximum_buoyancy*fraction
    floats=maximum_buoyancy>=load
    draft=load/(rho*G*area) if floats else None
    actual_freeboard=height-draft if floats else None
    restraint=_vector(c["horizontal_restraint_n"],"horizontal_restraint_n",2) if "horizontal_restraint_n" in c else None
    horizontal=restraint-top[:2] if restraint is not None else -top[:2]
    tolerance=max(1e-6,float(np.linalg.norm(top))*1e-8)
    horizontal_ok=float(np.linalg.norm(horizontal))<=tolerance
    sufficient=capacity_at_freeboard>=designed*(1-1e-12)
    vertical_balance=(rho*G*area*draft-load) if floats else maximum_buoyancy-load
    result=_result("cylinder-archimedes-static-cable-buoy-sizing-v1",[
        "User-defined dry buoy mass and height describe a vertical, constant-section cylinder; fully displaced volume is total hull volume.",
        "Archimedes buoyancy balances cable vertical end force, wet rigging load and above-water/buoy dry weight; total tension magnitude is not used as vertical load.",
        "User reserve is applied to total vertical load at the requested freeboard. This margin is not an engineering safety certification.",
        "Horizontal cable force needs a declared restraint. Buoy drift, mooring geometry, heel/stability, waves, drag and six-DOF dynamics are omitted."])
    if not floats:
        result["warnings"].append(_warning("BUOY_CANNOT_SUPPORT_LOAD","Even full displacement cannot support the vertical load; no floating equilibrium/draft is reported."))
    elif not sufficient:
        result["warnings"].append(_warning("BUOY_RESERVE_OR_FREEBOARD_NOT_MET","Candidate floats but does not meet the requested freeboard and reserve allowance."))
    if not horizontal_ok:
        result["warnings"].append(_warning("HORIZONTAL_BUOY_EQUILIBRIUM_NOT_MET","Vertical sizing alone cannot balance the cable horizontal force; supply a physically justified horizontal restraint or a fuller buoy/mooring model."))
    result.update({"cable_shape":shape,"support_force_n":top.tolist(),
        "summary":{"vertical_cable_load_n":float(top[2]),"horizontal_cable_load_n":float(np.linalg.norm(top[:2])),
            "total_vertical_load_n":load,"design_vertical_load_n":designed,"reserve_pct":reserve*100,
            "minimum_displacement_volume_m3":minimum,"displacement_volume_m3":volume,
            "height_m":height,"diameter_m":diameter,"waterplane_area_m2":area,
            "requested_freeboard_m":freeboard,"actual_freeboard_m":actual_freeboard,"draft_m":draft,
            "actual_displaced_water_volume_m3":area*draft if floats else None,
            "maximum_buoyancy_n":maximum_buoyancy,"capacity_at_requested_freeboard_n":capacity_at_freeboard,
            "reserve_available_at_requested_freeboard_n":capacity_at_freeboard-load,
            "vertical_equilibrium_possible":floats,"design_margin_and_freeboard_met":sufficient,
            "horizontal_equilibrium_met":horizontal_ok,"complete_static_force_balance":floats and horizontal_ok,
            "vertical_force_residual_n":float(vertical_balance),"horizontal_force_residual_n":horizontal.tolist()}})
    return result
