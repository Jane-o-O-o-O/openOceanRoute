"""Independent reconciliation of declared cable observations and a planned route.

Observed coordinates/depths are never installed into the planning project. This
is a sampled geodetic comparison, not contact inference or a Makai native method.
"""
from __future__ import annotations

import bisect
from copy import deepcopy
import csv
import io
import math

import numpy as np

from .core import analyze_project
from .geodesy import GEOD, coordinate, densify, finite_number, interpolate, inverse, split_antimeridian

EPS = 1e-7


class SurveyError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _warning(code, message, severity="warning", **extra):
    return {"code":code,"message":message,"severity":severity,**extra}


def _integer(value, name, low, high):
    value=finite_number(value,name,minimum=low,maximum=high)
    if not value.is_integer():
        raise ValueError(f"{name} 必须为整数")
    return int(value)


def parse_observations(text, config=None):
    """Decimal-degree WGS84 CSV/TSV, metre depth and physical cable stations."""
    config=config or {}
    if not isinstance(text,str) or len(text)>2_000_000:
        raise SurveyError("SURVEY_TEXT_LIMIT","观测文本须为字符串，最多2 MB")
    delimiter=config.get("delimiter")
    if delimiter is None:
        try:delimiter=csv.Sniffer().sniff(text[:8192],delimiters=",\t;").delimiter
        except csv.Error:delimiter="\t" if "\t" in next(iter(text.splitlines()),"") else ","
    if delimiter not in (",","\t",";"):
        raise ValueError("delimiter 只支持逗号、Tab或分号")
    reader=csv.DictReader(io.StringIO(text.lstrip("\ufeff")),delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError("测量文本缺少表头")
    reader.fieldnames=[h.strip().lower() for h in reader.fieldnames]
    if len(set(reader.fieldnames))!=len(reader.fieldnames) or not {"longitude","latitude"}<=set(reader.fieldnames):
        raise ValueError("表头须有唯一 longitude、latitude；可选depth_m、cable_kp_m、time_s、id、break_before")
    values=[]
    for number,row in enumerate(reader,2):
        if all(not str(v or "").strip() for v in row.values()):
            continue
        try:
            if None in row:
                raise ValueError("列数超过表头")
            value={"longitude":float(row["longitude"]),"latitude":float(row["latitude"])}
            for key in ("depth_m","cable_kp_m","time_s"):
                value[key]=float(row[key]) if str(row.get(key) or "").strip() else None
            if str(row.get("id") or "").strip():value["id"]=row["id"].strip()
            marker=str(row.get("break_before") or "").strip().lower()
            if marker not in ("","0","false","no","1","true","yes"):
                raise ValueError("break_before须0/1或false/true")
            value["break_before"]=marker in ("1","true","yes")
            values.append(value)
        except (TypeError,ValueError) as error:
            raise ValueError(f"测量文本第{number}行: {error}") from error
        if len(values)>5000:
            raise SurveyError("SURVEY_SAMPLE_LIMIT","观测最多5,000点，请按测量区段拆分")
    return values


def _observations(raw):
    if not isinstance(raw,list) or not 1<=len(raw)<=5000:
        raise SurveyError("SURVEY_SAMPLE_LIMIT","observations须含1～5,000个观测点")
    result=[];seen=set();last_station=None;last_time=None
    for i,item in enumerate(raw):
        if not isinstance(item,dict):raise ValueError("observations内每项须为对象")
        identifier=str(item.get("id",f"obs-{i+1}"))
        if not identifier or identifier in seen:raise ValueError("观测id须非空且唯一")
        seen.add(identifier)
        lon,lat=coordinate(item.get("longitude"),item.get("latitude"))
        depth=None if item.get("depth_m") is None else finite_number(item["depth_m"],"depth_m",minimum=0,maximum=20000)
        station=None if item.get("cable_kp_m") is None else finite_number(item["cable_kp_m"],"cable_kp_m",minimum=0,maximum=1e9)
        if station is not None:
            if last_station is not None and station-last_station<=EPS:
                raise SurveyError("SURVEY_CABLE_STATION_ORDER","非缺测实物缆KP须严格递增；重复/逆序站位不能用于余缆计算")
            last_station=station
        time=None if item.get("time_s") is None else finite_number(item["time_s"],"time_s",minimum=0,maximum=1e12)
        if time is not None:
            if last_time is not None and time<last_time:raise ValueError("观测时间须按输入顺序非递减")
            last_time=time
        marker=item.get("break_before",False)
        if not isinstance(marker,bool):raise ValueError("break_before须为布尔值")
        result.append({"id":identifier,"index":i,"longitude":lon,"latitude":lat,"depth_m":depth,
                       "cable_kp_m":station,"time_s":time,"break_before":marker,"note":str(item.get("note",""))})
    return result


class _NearestCurve:
    def __init__(self, project, analysis, config):
        self.points=project["route"]["points"]
        self.legs=analysis["legs"]
        self.curve=project["route"].get("curve","rhumb")
        self.step=finite_number(config.get("route_sample_step_m",1000),"route_sample_step_m",minimum=25,maximum=20000)
        self.tolerance=finite_number(config.get("station_tolerance_m",.01),"station_tolerance_m",minimum=.001,maximum=10)
        self.ambiguity=finite_number(config.get("ambiguity_distance_m",.1),"ambiguity_distance_m",minimum=.001,maximum=100)
        self.separation=finite_number(config.get("ambiguity_kp_separation_m",100),"ambiguity_kp_separation_m",minimum=1,maximum=1e7)
        self.budget=_integer(config.get("max_work_evaluations",2_000_000),"max_work_evaluations",1,10_000_000)
        self.evaluations=0;self.cells=[];self.nodes=[]
        for i,leg in enumerate(self.legs):
            length=leg["surface_length_m"]
            if length<=EPS:continue
            count=max(1,math.ceil(length/self.step))
            if len(self.cells)+count>20000:
                raise SurveyError("SURVEY_ROUTE_GRID_LIMIT","规划曲线超过20,000个搜索区间，请增大route_sample_step_m或分海区")
            for j in range(count):
                fractions=(j/count,(j+.5)/count,(j+1)/count)
                indices=[]
                for fraction in fractions:
                    position=self.at(i,fraction)
                    indices.append(len(self.nodes));self.nodes.append((i,fraction,position))
                self.cells.append((i,fractions[0],fractions[2],length/count,indices))
        if not self.cells:raise SurveyError("SURVEY_EMPTY_ROUTE","规划路线没有正长度区段，无法定义横向偏差")
        self.xy=np.array([n[2] for n in self.nodes])

    def at(self, leg_index, fraction):
        a,b=self.points[leg_index:leg_index+2]
        return interpolate(a["longitude"],a["latitude"],b["longitude"],b["latitude"],fraction,self.curve)

    def charge(self,amount):
        self.evaluations+=int(amount)
        if self.evaluations>self.budget:
            raise SurveyError("SURVEY_WORK_LIMIT","最近曲线查询超过工作预算；请减少观测点、增大搜索间距或分段")

    def distance(self,lon,lat,leg,fraction):
        self.charge(1)
        position=self.at(leg,fraction)
        return float(GEOD.inv(*position,lon,lat)[2])

    def nearest(self,lon,lat):
        self.charge(len(self.nodes))
        distances=np.asarray(GEOD.inv(self.xy[:,0],self.xy[:,1],np.full(len(self.nodes),lon),np.full(len(self.nodes),lat))[2])
        initial=int(np.argmin(distances))
        li,frac,_=self.nodes[initial]
        candidates=[(float(distances[initial]),self.legs[li]["start_kp_m"]+frac*self.legs[li]["surface_length_m"],li,frac)]
        best=float(distances[initial])
        # A midpoint-distance minus half the surface arc is a valid geodesic
        # triangle-inequality lower bound even when the curve is a rhumb line.
        bounds=sorted((max(0,float(distances[cell[4][1]])-cell[3]/2),i) for i,cell in enumerate(self.cells))
        ratio=(math.sqrt(5)-1)/2
        for bound,ci in bounds:
            if bound>best+self.ambiguity:break
            leg,lo,hi,length,indices=self.cells[ci]
            left,right=lo,hi
            c=right-ratio*(right-left);d=left+ratio*(right-left)
            fc=self.distance(lon,lat,leg,c);fd=self.distance(lon,lat,leg,d)
            while (right-left)*self.legs[leg]["surface_length_m"]>self.tolerance:
                if fc<fd:
                    right,d,fd=d,c,fc;c=right-ratio*(right-left);fc=self.distance(lon,lat,leg,c)
                else:
                    left,c,fc=c,d,fd;d=left+ratio*(right-left);fd=self.distance(lon,lat,leg,d)
            options=[(float(distances[indices[0]]),lo),(float(distances[indices[2]]),hi),(fc,c),(fd,d)]
            value,fraction=min(options,key=lambda p:p[0])
            kp=self.legs[leg]["start_kp_m"]+fraction*self.legs[leg]["surface_length_m"]
            candidates.append((value,kp,leg,fraction));best=min(best,value)
        # Exact-distance ties select the first planned KP deterministically.
        closest=min((c for c in candidates if c[0]<=best+min(.001,self.tolerance)),key=lambda c:(c[1],c[2]))
        distance,kp,leg,fraction=closest
        ambiguous=any(c[0]<=best+self.ambiguity and abs(c[1]-kp)>=self.separation for c in candidates)
        position=self.at(leg,fraction)
        a,b=self.points[leg:leg+2]
        length,bearing=inverse(a["longitude"],a["latitude"],b["longitude"],b["latitude"],self.curve)
        if self.curve=="geodesic":
            _,_,bearing=GEOD.fwd(a["longitude"],a["latitude"],bearing,length*fraction,return_back_azimuth=False)
        residual_bearing=GEOD.inv(*position,lon,lat)[0]
        difference=math.radians((residual_bearing-bearing+180)%360-180)
        cross=distance*math.sin(difference) if distance>1e-5 else 0.0
        along=distance*math.cos(difference) if distance>1e-5 else 0.0
        endpoint=fraction*length<=self.tolerance or (1-fraction)*length<=self.tolerance
        internal_vertex=endpoint and (leg>0 if fraction<.5 else leg<len(self.legs)-1)
        return {"planned_kp_m":kp,"nearest_distance_m":distance,"signed_cross_track_m":cross,
                "along_track_residual_m":along,"nearest_longitude":position[0],"nearest_latitude":position[1],
                "planned_leg_index":leg,"leg_fraction":fraction,"route_bearing_deg":bearing%360,
                "ambiguous_match":ambiguous,"endpoint_projection":endpoint,
                "tangent_ambiguous":internal_vertex and distance>self.tolerance}


def _depth(profile,kp):
    keys=[p["kp_m"] for p in profile]
    i=bisect.bisect_left(keys,kp)
    if i<len(profile) and abs(keys[i]-kp)<=EPS:return profile[i]["depth_m"]
    if i==0 or i==len(profile):return None
    a,b=profile[i-1:i+1]
    if a["depth_m"] is None or b["depth_m"] is None:return None
    return a["depth_m"]+(kp-a["kp_m"])/(b["kp_m"]-a["kp_m"])*(b["depth_m"]-a["depth_m"])


def _bottom(profile,lo,hi):
    selected=[{"kp_m":lo,"depth_m":_depth(profile,lo)}]+[p for p in profile if lo+EPS<p["kp_m"]<hi-EPS]+[{"kp_m":hi,"depth_m":_depth(profile,hi)}]
    if any(p["depth_m"] is None for p in selected):return None
    return sum(math.hypot(b["kp_m"]-a["kp_m"],b["depth_m"]-a["depth_m"]) for a,b in zip(selected,selected[1:]))


def _cable_station(project,analysis):
    keys=[r["kp_m"] for r in analysis["rpl"]]
    base=[0.0]
    for leg in analysis["legs"]:base.append(base[-1]+leg["cable_length_m"])
    inserts=[(b["kp_m"],b["length_m"]) for b in analysis["bodies"] if b.get("length_mode")=="additional"]
    inserts.extend((min(keys[-1],a.get("kp_m",0)),a.get("length_m",0)) for a in project["route"].get("allowances",project.get("allowances",[])))
    inserts.extend((keys[i+1],opt["allowance_m"]) for i,opt in enumerate(project["route"].get("legs",[])) if opt.get("allowance_m",0))
    def at(kp):
        if kp>=keys[-1]:value=base[-1]
        else:
            i=max(0,min(len(keys)-2,bisect.bisect_right(keys,kp)-1))
            leg=analysis["legs"][i]
            value=base[i]+(kp-keys[i])/leg["surface_length_m"]*leg["cable_length_m"] if leg["surface_length_m"]>0 else base[i]
        return value+sum(length for key,length in inserts if key<=kp+1e-8)
    return at


def _line(coords,spacing):
    dense=[]
    for a,b in zip(coords,coords[1:]):
        count=max(1,math.ceil(inverse(*a,*b,"geodesic")[0]/spacing))
        points=densify(*a,*b,"geodesic",spacing,count)
        dense.extend(points if not dense else points[1:])
    pieces=split_antimeridian(dense,"geodesic")
    return {"type":"LineString","coordinates":pieces[0]} if len(pieces)==1 else {"type":"MultiLineString","coordinates":pieces}


def reconcile_survey(project,config=None):
    config={} if config is None else config
    if not isinstance(config,dict):raise ValueError("survey config须为对象")
    analysis=analyze_project(project)
    raw=config.get("observations")
    if raw is None and "text" in config:raw=parse_observations(config["text"],config)
    observed=_observations(raw)
    gap=finite_number(config.get("max_gap_m",10000),"max_gap_m",minimum=.01,maximum=1e6)
    deviation=finite_number(config.get("max_deviation_m",50),"max_deviation_m",minimum=0,maximum=1e6)
    match_radius=finite_number(config.get("max_match_distance_m",10000),"max_match_distance_m",minimum=.01,maximum=1e6)
    geometry_step=finite_number(config.get("geometry_step_m",1000),"geometry_step_m",minimum=10,maximum=20000)
    vertex_limit=_integer(config.get("max_output_vertices",100000),"max_output_vertices",10,250000)
    kind=config.get("observation_kind","cable_position_survey")
    if kind not in ("cable_position_survey","reported_touchdown","vessel_track"):
        raise ValueError("observation_kind须cable_position_survey/reported_touchdown/vessel_track")
    aligned=config.get("depth_datums_aligned",False)
    if not isinstance(aligned,bool):raise ValueError("depth_datums_aligned须为布尔值")
    offset=None if config.get("cable_kp_offset_m") is None else finite_number(config["cable_kp_offset_m"],"cable_kp_offset_m",minimum=-1e9,maximum=1e9)
    nearest=_NearestCurve(project,analysis,config)
    cable_at=_cable_station(project,analysis)
    warnings=[{**deepcopy(w),"scope":"planned_route"} for w in analysis["warnings"]
              if w["code"].startswith("PROFILE_") or w["code"]=="WAYPOINT_DEPTH_APPROXIMATION"];rows=[]
    for obs in observed:
        result=nearest.nearest(obs["longitude"],obs["latitude"])
        matched=result["nearest_distance_m"]<=match_radius
        planned_depth=_depth(analysis["profile"],result["planned_kp_m"]) if matched else None
        planned_cable=cable_at(result["planned_kp_m"]) if matched else None
        row={**obs,**result,"matched":matched,"planned_depth_m":planned_depth,"planned_cable_kp_m":planned_cable,
             "route_kp_m":result["planned_kp_m"],"cross_track_m":result["signed_cross_track_m"],
             "observed_depth_m":obs["depth_m"],"ambiguity":result["ambiguous_match"],
             "depth_difference_m":obs["depth_m"]-planned_depth if aligned and obs["depth_m"] is not None and planned_depth is not None else None,
             "aligned_observed_cable_kp_m":obs["cable_kp_m"]+offset if offset is not None and obs["cable_kp_m"] is not None else None,
             "cable_kp_difference_m":obs["cable_kp_m"]+offset-planned_cable if offset is not None and obs["cable_kp_m"] is not None and planned_cable is not None else None,
             "deviation_exceeded":matched and abs(result["signed_cross_track_m"])>deviation}
        if not matched:warnings.append(_warning("SURVEY_OUTSIDE_MATCH_RADIUS","观测距规划路线超过匹配半径；保留观测但不做规划区间对照",observation_id=obs["id"]))
        elif result["ambiguous_match"]:warnings.append(_warning("SURVEY_AMBIGUOUS_KP","回环／邻近路线存在多个近似等距KP，选择最早KP仅供展示；不自动计区间对照",observation_id=obs["id"]))
        if row["deviation_exceeded"]:warnings.append(_warning("SURVEY_DEVIATION_LIMIT","带方向横偏超过用户阈值",observation_id=obs["id"],signed_cross_track_m=result["signed_cross_track_m"]))
        if result["tangent_ambiguous"]:warnings.append(_warning("SURVEY_TURN_TANGENT","最近位置在转折点，法向不唯一；偏差分量采用已标明区段方向",observation_id=obs["id"]))
        rows.append(row)
    segments=[];chains=0;surface_cumulative=0.0;bottom_cumulative=0.0 if rows[0]["depth_m"] is not None and kind!="vessel_track" else None
    rows[0].update(chain_index=0,chain_surface_kp_m=0.0,observed_surface_kp_m=0.0,observed_bottom_kp_m=bottom_cumulative)
    chain_surface=0.0
    for i,(a,b) in enumerate(zip(rows,rows[1:])):
        horizontal=inverse(a["longitude"],a["latitude"],b["longitude"],b["latitude"],"geodesic")[0]
        connected=not b["break_before"] and horizontal<=gap
        if not connected:
            chains+=1;chain_surface=0.0;surface_cumulative=None;bottom_cumulative=None
            warnings.append(_warning("SURVEY_GAP","观测存在显式断开或间距超过max_gap_m；不跨空档插值",start_id=a["id"],end_id=b["id"]))
        else:
            chain_surface+=horizontal
            if surface_cumulative is not None:surface_cumulative+=horizontal
        bottom=math.hypot(horizontal,b["depth_m"]-a["depth_m"]) if connected and a["depth_m"] is not None and b["depth_m"] is not None and kind!="vessel_track" else None
        cable=b["cable_kp_m"]-a["cable_kp_m"] if connected and a["cable_kp_m"] is not None and b["cable_kp_m"] is not None and kind!="vessel_track" else None
        if bottom_cumulative is not None:bottom_cumulative=bottom_cumulative+bottom if bottom is not None else None
        b.update(chain_index=chains,chain_surface_kp_m=chain_surface,observed_surface_kp_m=surface_cumulative,observed_bottom_kp_m=bottom_cumulative)
        pair=connected and a["matched"] and b["matched"] and not a["ambiguous_match"] and not b["ambiguous_match"]
        ordered=b["planned_kp_m"]-a["planned_kp_m"]>EPS
        if pair and not ordered:
            warnings.append(_warning("SURVEY_KP_NONMONOTONIC","最近规划KP未沿正敷设方向增加；此观测段不自动与规划区间配对",start_id=a["id"],end_id=b["id"]))
        comparable=pair and ordered
        planned_horizontal=b["planned_kp_m"]-a["planned_kp_m"] if comparable else None
        planned_bottom=_bottom(analysis["profile"],a["planned_kp_m"],b["planned_kp_m"]) if comparable else None
        planned_cable=b["planned_cable_kp_m"]-a["planned_cable_kp_m"] if comparable else None
        measured_slack=100*(cable/horizontal-1) if cable is not None and horizontal>EPS else None
        measured_bottom_slack=100*(cable/bottom-1) if cable is not None and bottom is not None and bottom>EPS else None
        planned_slack=100*(planned_cable/planned_horizontal-1) if planned_cable is not None and planned_horizontal>EPS else None
        planned_bottom_slack=100*(planned_cable/planned_bottom-1) if planned_cable is not None and planned_bottom is not None and planned_bottom>EPS else None
        segments.append({"index":i,"start_id":a["id"],"end_id":b["id"],"connected":connected,"comparable":comparable,
            "observed_chord_distance_m":horizontal,"measured_surface_length_m":horizontal if connected else None,
            "measured_bottom_length_m":bottom,"measured_cable_length_m":cable,
            "measured_surface_slack_pct":measured_slack,"measured_bottom_slack_pct":measured_bottom_slack,
            "planned_start_kp_m":a["planned_kp_m"] if comparable else None,"planned_end_kp_m":b["planned_kp_m"] if comparable else None,
            "planned_surface_length_m":planned_horizontal,"planned_bottom_length_m":planned_bottom,"planned_cable_length_m":planned_cable,
            "planned_surface_slack_pct":planned_slack,"planned_bottom_slack_pct":planned_bottom_slack,
            "surface_length_difference_m":horizontal-planned_horizontal if comparable else None,
            "bottom_length_difference_m":bottom-planned_bottom if bottom is not None and planned_bottom is not None else None,
            "cable_length_difference_m":cable-planned_cable if cable is not None and planned_cable is not None else None,
            "surface_slack_difference_pct":measured_slack-planned_slack if measured_slack is not None and planned_slack is not None else None,
            "bottom_slack_difference_pct":measured_bottom_slack-planned_bottom_slack if measured_bottom_slack is not None and planned_bottom_slack is not None else None})
    point_features=[];line_features=[];residual_features=[];vertices=0
    def geometry(coords):
        nonlocal vertices
        estimate=1+sum(max(1,math.ceil(inverse(*a,*b,"geodesic")[0]/geometry_step)) for a,b in zip(coords,coords[1:]))
        if vertices+estimate>vertex_limit:raise SurveyError("SURVEY_OUTPUT_LIMIT","地图加密顶点超过上限，请增大geometry_step_m或分测区")
        result=_line(coords,geometry_step)
        vertices+=len(result["coordinates"]) if result["type"]=="LineString" else sum(len(p) for p in result["coordinates"])
        if vertices>vertex_limit:raise SurveyError("SURVEY_OUTPUT_LIMIT","地图加密顶点超过上限，请增大geometry_step_m或分测区")
        return result
    for row in rows:
        vertices+=1
        if vertices>vertex_limit:raise SurveyError("SURVEY_OUTPUT_LIMIT","地图加密顶点超过上限，请增大geometry_step_m或分测区")
        point_features.append({"type":"Feature","id":row["id"],"properties":{"feature_role":"observed_point",**deepcopy(row)},
                               "geometry":{"type":"Point","coordinates":[row["longitude"],row["latitude"]]}})
        if row["matched"]:
            residual_features.append({"type":"Feature","id":f"residual-{row['id']}",
                "properties":{"feature_role":"residual_vector","observation_id":row["id"],"route_kp_m":row["route_kp_m"],"cross_track_m":row["cross_track_m"],
                              "planned_kp_m":row["planned_kp_m"],"signed_cross_track_m":row["signed_cross_track_m"],
                              "nearest_distance_m":row["nearest_distance_m"],"ambiguous_match":row["ambiguous_match"]},
                "geometry":geometry([(row["nearest_longitude"],row["nearest_latitude"]),(row["longitude"],row["latitude"])])})
    for segment,a,b in zip(segments,rows,rows[1:]):
        if segment["connected"]:
            line_features.append({"type":"Feature","id":f"measured-{segment['index']}","properties":{"feature_role":"measured_line",**deepcopy(segment)},
                                  "geometry":geometry([(a["longitude"],a["latitude"]),(b["longitude"],b["latitude"])])})
    deviations=[r["signed_cross_track_m"] for r in rows if r["matched"] and not r["ambiguous_match"] and not r["tangent_ambiguous"]]
    complete=bool(segments) and all(s["connected"] for s in segments)
    bottom_complete=complete and all(s["measured_bottom_length_m"] is not None for s in segments)
    cable_complete=complete and all(s["measured_cable_length_m"] is not None for s in segments)
    paired=[s for s in segments if s["comparable"]]
    if not aligned and any(r["depth_m"] is not None and r["planned_depth_m"] is not None for r in rows):
        warnings.append(_warning("SURVEY_DEPTH_DATUM_UNALIGNED","未声明测量与规划垂直基准对齐；显示两种水深，不计算点水深残差","info"))
    if kind=="vessel_track":warnings.append(_warning("SURVEY_VESSEL_TRACK","船位轨迹不等于海床实敷缆位置；仅做几何对照，不计算实测海底缆长或余缆"))
    summary={"observation_count":len(rows),"matched_count":sum(r["matched"] for r in rows),"unmatched_count":sum(not r["matched"] for r in rows),
        "ambiguous_count":sum(r["ambiguous_match"] for r in rows),"deviation_exceeded_count":sum(r["deviation_exceeded"] for r in rows),
        "deviation_stat_count":len(deviations),"mean_signed_cross_track_m":float(np.mean(deviations)) if deviations else None,
        "rms_cross_track_m":float(np.sqrt(np.mean(np.square(deviations)))) if deviations else None,
        "max_abs_cross_track_m":max(map(abs,deviations)) if deviations else None,
        "q95_abs_cross_track_m":float(np.quantile(np.abs(deviations),.95)) if deviations else None,
        "continuous_chain_count":chains+1,"complete_observation_chain":complete,"complete_observed_depth":bottom_complete,
        "complete_observed_cable_stations":cable_complete,
        "known_measured_surface_length_m":sum(s["measured_surface_length_m"] or 0 for s in segments),
        "known_measured_bottom_length_m":sum(s["measured_bottom_length_m"] or 0 for s in segments),
        "known_measured_cable_length_m":sum(s["measured_cable_length_m"] or 0 for s in segments),
        "measured_surface_length_m":sum(s["measured_surface_length_m"] for s in segments) if complete else None,
        "measured_bottom_length_m":sum(s["measured_bottom_length_m"] for s in segments) if bottom_complete else None,
        "measured_cable_length_m":sum(s["measured_cable_length_m"] for s in segments) if cable_complete else None,
        "comparable_segment_count":len(paired),"work_evaluations":nearest.evaluations,"route_search_cells":len(nearest.cells),"output_vertices":vertices}
    fc=lambda features:{"type":"FeatureCollection","features":features}
    return {"observations":rows,"segments":segments,"summary":summary,
        "comparison":{"paired_segment_count":len(paired),"paired_planned_surface_length_m":sum(s["planned_surface_length_m"] for s in paired),
                      "paired_measured_surface_length_m":sum(s["measured_surface_length_m"] for s in paired),
                      "planned_route_signature":analysis["route_signature"],"planned_profile_metadata":deepcopy(analysis["profile_metadata"])},
        "geojson":fc(point_features+line_features+residual_features),
        "geojson_layers":{"observed_points":fc(point_features),"measured_line":fc(line_features),"residual_vectors":fc(residual_features)},
        "warnings":warnings,"source":{"name":str(config.get("source","user_observations")),"observation_kind":kind,
            "horizontal_crs":"EPSG:4326","depth_positive":"down","vertical_datum":str(config.get("vertical_datum","unspecified")),
            "depth_datums_aligned":aligned,"cable_kp_offset_m":offset},
        "model":{"identity":"independent-wgs84-sampled-as-laid-reconciliation-v1","validation_status":"research",
            "nearest_curve":analysis["model"]["geometry"],"residual_distance":"WGS84_geodesic",
            "cross_track_sign":"right_of_planned_lay_direction_is_positive","route_sample_step_m":nearest.step,
            "station_tolerance_m":nearest.tolerance,"ambiguity_distance_m":nearest.ambiguity,
            "ambiguity_kp_separation_m":nearest.separation,"max_gap_m":gap,"max_deviation_m":deviation,
            "max_match_distance_m":match_radius,"geometry_step_m":geometry_step,"max_work_evaluations":nearest.budget},
        "assumptions":["输入位置由用户声明为实敷缆测量或报告触地点；几何比较本身不证明海床接触、张力或埋深。",
            "最近点在实际恒向／测地曲线上以分段距离下界筛选和一维细化求解；容差为数值站位容差，不是测量精度或原厂兼容声明。",
            "横偏是最近点到观测的WGS84测地残差在局部路线法向上的有符号分量；端点还有沿向残差，转折点法向不唯一。",
            "观测线由相邻有效观测间的测地线表示，底距为水平测地距离与水深差的分段线性三维近似；未知细部地形不补造。",
            "显式断开、过大间距、缺水深或缺实物缆KP不外推；空档之后完整累计KP保持未知，连续局部链独立记账。",
            "点水深差需用户声明垂直基准对齐，绝对实物缆KP差需明确offset；区间制造长度差不依赖共同里程原点。",
            "观测原顺序必须沿正敷设方向；回环KP歧义、未匹配或反序投影不自动生成规划区间对照。"]}
