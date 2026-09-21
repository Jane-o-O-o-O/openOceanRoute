"""Explicit geographic Split AC / Radius AC candidate route tools.

The public manual describes equal turn angles/equal internal KP steps and a
radius arc tangent to the original inbound/outbound paths. This module solves
those constraints on WGS84. It does not reproduce an unpublished native solver.
"""
from __future__ import annotations

from copy import deepcopy
import math

from scipy.optimize import least_squares

from .core import analyze_project, route_signature
from .constraints import ConstraintError, _rigid_path, reconcile_route_structure
from .geodesy import GEOD, WGS84_A, WGS84_E2, finite_number, inverse, _isometric_delta_degrees, _rhumb_psi_per_meridian, wrap_longitude
from .route_geometry import segment_from_leg

POSITION_TOLERANCE_M = 1e-4
ANGLE_TOLERANCE_DEG = 1e-7


class AltercourseError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _angle(value):
    return (value+180.) % 360.-180.


def _integer(value, name, minimum, maximum):
    if isinstance(value,bool) or not isinstance(value,int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须为 {minimum}..{maximum} 的整数")
    return value


def _config(config, operation):
    required = {"point_id", "max_turn_angle_deg", "min_turn_distance_m"} if operation=="split" else {"point_id", "radius_m"}
    optional = {"max_solver_evaluations", "max_work_units"}
    if operation=="split": optional.add("max_generated_turns")
    if not isinstance(config,dict) or set(config)-required-optional or required-set(config):
        raise ValueError("转角工具配置包含未知字段或缺少必需字段")
    pid=config["point_id"]
    if not isinstance(pid,str) or not pid or len(pid)>256:
        raise ValueError("point_id 必须为非空字符串，最多256字符")
    result={"point_id":pid}
    if operation=="split":
        angle=finite_number(config["max_turn_angle_deg"],"max_turn_angle_deg",minimum=0,maximum=180)
        if not 0 < angle < 180:
            raise ValueError("max_turn_angle_deg 必须严格在 0..180 度之间")
        result.update(max_turn_angle_deg=angle,min_turn_distance_m=finite_number(config["min_turn_distance_m"],"min_turn_distance_m",minimum=.001,maximum=1e6))
    else:
        result["radius_m"]=finite_number(config["radius_m"],"radius_m",minimum=.001,maximum=1e6)
    for key,default,maximum in (("max_solver_evaluations",200,2000),("max_generated_turns",128,512),("max_work_units",200000,2000000)):
        if key=="max_generated_turns" and operation!="split": continue
        result[key]=_integer(config.get(key,default),key,1,maximum)
    return result


class _Work:
    def __init__(self, maximum): self.maximum,self.used=maximum,0
    def charge(self,count=1):
        if self.used+count>self.maximum:
            raise AltercourseError("ALTERCOURSE_WORK_BUDGET","实际地理求值超过工作预算；未返回部分整形")
        self.used+=count


def _direct(point, heading, distance, curve, work):
    work.charge()
    lon,lat=point
    if curve=="geodesic":
        x,y,_=GEOD.fwd(lon,lat,heading,distance)
        return x,y
    if abs(lat)>=90:
        raise AltercourseError("ALTERCOURSE_POLE_DOMAIN","恒向线不能从极点定义航向")
    angle=math.radians(heading)
    north=distance*math.cos(angle)
    limit=GEOD.inv(lon,lat,lon,90 if north>=0 else -90)[2]
    if abs(north)>=limit-1e-7:
        raise AltercourseError("ALTERCOURSE_POLE_DOMAIN","等距恒向航段越过极点")
    if abs(math.cos(angle))<1e-10:
        p=math.radians(lat)
        radius=WGS84_A*math.cos(p)/math.sqrt(1-WGS84_E2*math.sin(p)**2)
        return wrap_longitude(lon+math.degrees(distance*math.sin(angle)/radius)),lat
    _,y,_=GEOD.fwd(lon,lat,0 if north>=0 else 180,abs(north))
    phi1,phi2=math.radians(lat),math.radians(y)
    dp=math.radians(y-lat);mid=(phi1+phi2)/2
    if abs(dp)<.001*abs(math.cos(mid)):
        # Near an east/west heading, the rounded meridian endpoint cannot be
        # multiplied by tan(heading). Use the smooth metric divided difference
        # for the actual requested east component instead.
        dl=distance*math.sin(angle)*_rhumb_psi_per_meridian(phi1,phi2,latitude1=lat,latitude2=y)
    else:
        dl=math.tan(angle)*_isometric_delta_degrees(lat,y)
    return wrap_longitude(lon+math.degrees(dl)),y


def _setup(project, config):
    before=analyze_project(project)
    points=project["route"]["points"]
    selected=next((p for p in points if p["id"]==config["point_id"]),None)
    if selected is None:
        raise AltercourseError("ALTERCOURSE_POINT_REFERENCE","选定 point_id 不存在")
    if selected.get("constraint","rigid")!="rigid":
        raise ConstraintError("CONSTRAINT_ALTERCOURSE_POINT_TYPE","Split/Radius 必须明确编辑 Rigid 转角；不能解锁 Clamped/Sliding")
    rigid,segments,keys=_rigid_path(project)
    index=next(i for i,p in enumerate(rigid) if p["id"]==selected["id"])
    if not 0 < index < len(rigid)-1:
        raise AltercourseError("ALTERCOURSE_ENDPOINT","路线端点没有两侧航段，不能整形")
    incoming,outgoing=segments[index-1:index+1]
    if min(incoming.length_m,outgoing.length_m)<=1e-6:
        raise AltercourseError("ALTERCOURSE_ZERO_LEG","邻接零长航段无法定义转角")
    turn=_angle(outgoing.tangent_at_fraction(0)-incoming.tangent_at_fraction(1))
    if abs(turn)>=180-1e-8:
        raise AltercourseError("ALTERCOURSE_REVERSAL","180度折返不存在有限内切整形，未裁剪")
    return before,rigid,segments,index,turn


def _candidate(project, rigid, segments, index, chain, replacement):
    result=deepcopy(project)
    result["route"]["points"]=deepcopy(rigid[:index])+chain+deepcopy(rigid[index+1:])
    actual=segments[:index-1]+replacement+segments[index+1:]
    result["route"]["legs"]=[{"geometry":s.geometry} if s.geometry is not None else {} for s in actual]
    return result


def _new_points(rigid, index, coordinates):
    ids={p["id"] for p in rigid}
    chain=[]
    for j,(lon,lat) in enumerate(coordinates):
        if j==len(coordinates)-1:
            point=deepcopy(rigid[index])
        else:
            pid=f"altercourse-{rigid[index]['id']}-{j+1}"
            while pid in ids: pid+="_"
            ids.add(pid)
            point={"id":pid,"constraint":"rigid","label":"转角整形切点","note":"WGS84真实几何求解；水深未重新采样"}
        point.update(longitude=lon,latitude=lat,depth_m=None)
        chain.append(point)
    return chain


def _manufacturing(before, after, mode):
    a={m["cable_type_id"]:m["length_m"] for m in before["materials"]}
    b={m["cable_type_id"]:m["length_m"] for m in after["materials"]}
    basea=sum(l["cable_length_m"] for l in before["legs"])
    baseb=sum(l["cable_length_m"] for l in after["legs"])
    return {"mode":mode,"base_length_before_m":basea,"base_length_after_m":baseb,"base_delta_m":baseb-basea,
            "physical_length_before_m":before["summary"]["cable_length_m"],"physical_length_after_m":after["summary"]["cable_length_m"],
            "physical_delta_m":after["summary"]["cable_length_m"]-before["summary"]["cable_length_m"],
            "by_cable_type":[{"cable_type_id":t,"before_m":a.get(t,0),"after_m":b.get(t,0),"delta_m":b.get(t,0)-a.get(t,0)} for t in sorted(set(a)|set(b))]}


def _result(project, result, before, config, operation, inserted, evidence, reconcile=None, changed=True):
    after=analyze_project(result)
    profile_invalidation={"present":bool(project.get("profile")),"invalidated":changed and bool(project.get("profile")),
                          "old_route_signature":route_signature(project),"new_route_signature":route_signature(result),
                          "policy":"old_signature_retained_no_depth_recertification"}
    side_invalidation={"present":bool(project.get("side_slopes")),"invalidated":changed and bool(project.get("side_slopes")),
                       "policy":"old_signature_retained"}
    modes={l["mode"] for l in before["legs"]}
    report={"operation":operation,"config":config,"changed":changed,"selected_point_id":config["point_id"],
            "result_selection_point_id":config["point_id"],"inserted_point_ids":inserted,
            "before":{"surface_length_m":before["summary"]["surface_length_m"],"point_count":len(project["route"]["points"])},
            "after":{"surface_length_m":after["summary"]["surface_length_m"],"point_count":len(result["route"]["points"])},
            "geometry_evidence":evidence,"manufacturing":_manufacturing(before,after,next(iter(modes)) if len(modes)==1 else "mixed"),
            "profile_invalidation":profile_invalidation,"side_slopes_invalidation":side_invalidation,
            "constraint_reconciliation":None if reconcile is None else reconcile["report"]}
    warnings=[] if reconcile is None else reconcile["warnings"]
    if changed:
        warnings=warnings+[{"code":"ALTERCOURSE_DEPTH_REQUIRES_RESAMPLING","severity":"warning","message":"移动／新增点水深为空；原剖面和侧坡未重新认证，须实际重采样"}]
    return {"project":result,"report":report,"warnings":warnings}


def split_altercourse(project, config):
    config=_config(config,"split")
    before,rigid,segments,index,turn=_setup(project,config)
    limit=config["max_turn_angle_deg"]
    if abs(turn)<=limit:
        return _result(project,deepcopy(project),before,config,"split",[],{"original_turn_deg":turn,"turns_deg":[turn],"internal_lengths_m":[],"solver":{"evaluations":0,"work_units":0}},changed=False)
    # A valid positive subnormal angle must be rejected by the declared count
    # budget before division can overflow to infinity (and become HTTP500).
    if abs(turn)>limit*config["max_generated_turns"]:
        raise AltercourseError("ALTERCOURSE_POINT_BUDGET","所需等转角数量超预算；未自动放宽角度")
    count=math.ceil(abs(turn)/limit)
    if count>config["max_generated_turns"] or len(rigid)+count-1>10000:
        raise AltercourseError("ALTERCOURSE_POINT_BUDGET","所需等转角数量超预算；未自动放宽角度")
    incoming,outgoing=segments[index-1:index+1]
    curve=project["route"].get("curve","rhumb")
    distance=config["min_turn_distance_m"]
    sign=math.copysign(1.,turn)
    work=_Work(config["max_work_units"])
    scale=max(distance,1.)
    evaluations=0
    solution=None
    # Curvature can change the actual common turn by a small amount. If the
    # minimum count is insufficient, solve the next count explicitly rather
    # than certifying a turn above the user's maximum.
    for n in range(count,config["max_generated_turns"]+1):
        def build(values):
            nonlocal evaluations
            evaluations+=1
            if evaluations>config["max_solver_evaluations"]:
                raise AltercourseError("ALTERCOURSE_SOLVER_BUDGET","根求解实际评估次数超预算")
            ti,to,delta=values
            work.charge(2)
            start=incoming.point_at_distance(incoming.length_m-ti)
            heading=incoming.tangent_at_distance(incoming.length_m-ti)
            vertices=[start];parts=[]
            for j in range(n-1):
                target=_direct(vertices[-1],heading+sign*delta,distance,curve,work)
                part=segment_from_leg(vertices[-1],target,curve=curve)
                work.charge(2)
                heading=part.tangent_at_fraction(1)
                vertices.append(target);parts.append(part)
            end=outgoing.point_at_distance(to)
            work.charge(2)
            az,_,gap=GEOD.inv(*vertices[-1],*end)
            angle=float(_angle(heading+sign*delta-outgoing.tangent_at_distance(to)))
            return [gap*math.sin(math.radians(az))/scale,gap*math.cos(math.radians(az))/scale,math.radians(angle)],vertices,parts,angle,gap
        theta=math.radians(abs(turn));delta=theta/n
        guess=distance*math.sin((n-1)*delta/2)/(2*math.sin(delta/2)*math.cos(theta/2))
        epsilon=max(1e-7,min(incoming.length_m,outgoing.length_m)*1e-12)
        upper=[incoming.length_m-epsilon,outgoing.length_m-epsilon, min(179.999999,limit+1.)]
        # A planar construction is only a starting guess. In particular it is
        # not a proof that a geodesic/curved neighbour's finite domain is empty.
        initial=[min(max(guess,epsilon*2),upper[0]*.9),min(max(guess,epsilon*2),upper[1]*.9),math.degrees(delta)]
        solved=least_squares(lambda v:build(v)[0],initial,bounds=([epsilon,epsilon,1e-10],upper),
                             xtol=1e-14,ftol=1e-14,gtol=1e-15,max_nfev=config["max_solver_evaluations"],diff_step=1e-5)
        residual,vertices,parts,last_angle,gap=build(solved.x)
        ti,to,delta=map(float,solved.x)
        if gap<=POSITION_TOLERANCE_M and abs(last_angle)<=ANGLE_TOLERANCE_DEG and delta<=limit+ANGLE_TOLERANCE_DEG:
            solution=(ti,to,delta,vertices,parts,gap,last_angle,n);break
        if gap>POSITION_TOLERANCE_M and max(ti/upper[0],to/upper[1])>1-1e-7:
            raise AltercourseError("ALTERCOURSE_SHORT_LEG","根求解触及有限邻腿边界仍有位置残差；未缩短内部距离")
    if solution is None:
        raise AltercourseError("ALTERCOURSE_UNREACHABLE","等转角／等KP距离约束在声明有限邻腿域内未求得可验收根")
    ti,to,delta,vertices,parts,gap,last_angle,n=solution
    # Preserve the exact original outgoing endpoint of the root, then verify
    # all real angles and lengths again after this sub-millimetre adjustment.
    vertices[-1]=outgoing.point_at_distance(to)
    parts=[segment_from_leg(a,b,curve=curve) for a,b in zip(vertices,vertices[1:])]
    replacement=[incoming.subsegment(0.,incoming.length_m-ti)]+parts+[outgoing.subsegment(to,outgoing.length_m)]
    turns=[_angle(b.tangent_at_fraction(0)-a.tangent_at_fraction(1)) for a,b in zip(replacement,replacement[1:])]
    lengths=[p.length_m for p in parts]
    if max(abs(v-sign*delta) for v in turns)>ANGLE_TOLERANCE_DEG*2 or max(abs(v-distance) for v in lengths)>POSITION_TOLERANCE_M or max(map(abs,turns))>limit+ANGLE_TOLERANCE_DEG*2:
        raise AltercourseError("ALTERCOURSE_GEOMETRY_VERIFICATION","实际航段未通过等角／等距独立几何复核")
    chain=_new_points(rigid,index,vertices)
    candidate=_candidate(project,rigid,segments,index,chain,replacement)
    reconciled=reconcile_route_structure(project,candidate)
    evidence={"model":"wgs84-equal-turn-equal-internal-kp-v1","original_turn_deg":turn,"turns_deg":turns,
              "internal_lengths_m":lengths,"trim_in_m":ti,"trim_out_m":to,"common_turn_deg":sign*delta,
              "endpoint_residual_m":gap,"terminal_tangent_residual_deg":last_angle,
              "position_tolerance_m":POSITION_TOLERANCE_M,"angle_tolerance_deg":ANGLE_TOLERANCE_DEG,
              "solver":{"evaluations":evaluations,"work_units":work.used,"max_work_units":work.maximum,"success":True,"finite_trim_domain_m":[incoming.length_m,outgoing.length_m]}}
    evidence["solver"].update(work_basis="counted geographic primitive calls during bounded root evaluations; nested arc integration has separate intrinsic-segment limits; not FLOPs or a wall-time cap",
                              input_segment_solvers=[incoming.solver,outgoing.solver])
    return _result(project,reconciled["project"],before,config,"split",[p["id"] for p in chain[:-1]],evidence,reconciled)


def radius_altercourse(project, config):
    config=_config(config,"radius")
    before,rigid,segments,index,turn=_setup(project,config)
    if abs(turn)<=ANGLE_TOLERANCE_DEG:
        raise AltercourseError("ALTERCOURSE_NO_TURN","零转角不能定义非零圆弧；原路线保持不变")
    incoming,outgoing=segments[index-1:index+1]
    radius=config["radius_m"]
    sign=math.copysign(1.,turn)
    guess=radius*math.tan(math.radians(abs(turn))/2)
    work=_Work(config["max_work_units"]);evaluations=0
    def geometry(values):
        nonlocal evaluations
        evaluations+=1
        if evaluations>config["max_solver_evaluations"]:
            raise AltercourseError("ALTERCOURSE_SOLVER_BUDGET","根求解实际评估次数超预算")
        ti,to=values
        work.charge(6)
        p=incoming.point_at_distance(incoming.length_m-ti)
        h=incoming.tangent_at_distance(incoming.length_m-ti)
        cx,cy,_=GEOD.fwd(*p,h+sign*90.,radius)
        q=outgoing.point_at_distance(to)
        alpha,back,r=GEOD.inv(cx,cy,*q)
        angle=_angle(back+180.+sign*90.-outgoing.tangent_at_distance(to))
        return [(r-radius)/max(radius,1.),math.radians(angle)],p,q,(cx,cy),angle,r
    epsilon=max(1e-7,min(incoming.length_m,outgoing.length_m)*1e-12)
    upper=[incoming.length_m-epsilon,outgoing.length_m-epsilon]
    initial=[min(max(guess,epsilon*2),u*.9) for u in upper]
    solved=least_squares(lambda v:geometry(v)[0],initial,bounds=([epsilon,epsilon],upper),
                         xtol=1e-14,ftol=1e-14,gtol=1e-15,max_nfev=config["max_solver_evaluations"],diff_step=1e-5)
    _,p,q,center,angle,r=geometry(solved.x)
    ti,to=map(float,solved.x)
    if abs(r-radius)>POSITION_TOLERANCE_M or abs(angle)>ANGLE_TOLERANCE_DEG:
        if max(ti/upper[0],to/upper[1])>1-1e-7:
            raise AltercourseError("ALTERCOURSE_SHORT_LEG","根求解触及有限邻腿边界仍不满足半径／切线；未缩小半径")
        raise AltercourseError("ALTERCOURSE_UNREACHABLE","声明有限邻腿域内未求得通过半径和切线验收的根")
    alpha=GEOD.inv(*center,*p)[0]%360.
    beta=GEOD.inv(*center,*q)[0]%360.
    sweep=(beta-alpha)%360. if sign>0 else -((alpha-beta)%360.)
    if not 0<abs(sweep)<180+1e-6:
        raise AltercourseError("ALTERCOURSE_ARC_BRANCH","未求得短内切圆弧分支；未采用长绕弧")
    arc=segment_from_leg(p,q,{"geometry":{"type":"circular_arc","schema_version":1,"center":list(center),"radius_m":radius,"start_azimuth_deg":alpha,"sweep_deg":sweep}},project["route"].get("curve","rhumb"))
    replacement=[incoming.subsegment(0.,incoming.length_m-ti),arc,outgoing.subsegment(to,outgoing.length_m)]
    tangent_residuals=[_angle(b.tangent_at_fraction(0)-a.tangent_at_fraction(1)) for a,b in zip(replacement,replacement[1:])]
    if max(map(abs,tangent_residuals))>ANGLE_TOLERANCE_DEG:
        raise AltercourseError("ALTERCOURSE_GEOMETRY_VERIFICATION","圆弧实际径向切线未与原入出航迹相切")
    chain=_new_points(rigid,index,[p,q])
    reconciled=reconcile_route_structure(project,_candidate(project,rigid,segments,index,chain,replacement))
    evidence={"model":"wgs84-geodesic-radius-circle-v1","original_turn_deg":turn,"center":list(center),"radius_m":radius,
              "sweep_deg":sweep,"arc_length_m":arc.length_m,"turns_deg":tangent_residuals,"internal_lengths_m":[arc.length_m],
              "trim_in_m":ti,"trim_out_m":to,"radius_residual_m":abs(r-radius),"tangent_residuals_deg":tangent_residuals,
              "position_tolerance_m":POSITION_TOLERANCE_M,"angle_tolerance_deg":ANGLE_TOLERANCE_DEG,"arc_solver":arc.solver,
              "solver":{"evaluations":evaluations,"work_units":work.used,"max_work_units":work.maximum,"success":True,"finite_trim_domain_m":[incoming.length_m,outgoing.length_m]}}
    evidence["solver"].update(work_basis="counted geographic primitive calls during bounded root evaluations; nested arc integration has separate intrinsic-segment limits; not FLOPs or a wall-time cap",
                              input_segment_solvers=[incoming.solver,outgoing.solver])
    return _result(project,reconciled["project"],before,config,"radius",[chain[0]["id"]],evidence,reconciled)
