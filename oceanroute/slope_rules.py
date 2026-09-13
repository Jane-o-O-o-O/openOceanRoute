"""Read-only KP-range slope checks over declared, current sampled terrain.

This is an independent interpretation of the public planning rules.  A passing
check describes the sampled data, never unsurveyed seabed between stations.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from copy import deepcopy
import hashlib
import json
import math

from .geodesy import finite_number, inverse

MODEL = "kp-slope-rules-v1"
SIDE_MODEL = "route-side-slopes-v1"
EPS_M = 1e-7
MAX_RULES = 512
MAX_SAMPLES = 100_000
MAX_SIDE_SAMPLES = 50_000
MAX_TRANSECT_POINTS = 50_000


class SlopeRuleError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _error(code, message):
    raise SlopeRuleError(code, message)


def _integer(value, field, minimum, maximum):
    value = finite_number(value, field, minimum=minimum, maximum=maximum)
    if value != int(value):
        _error("SLOPE_RULE_CONFIG", f"{field} 须为整数")
    return int(value)


def _text(value, field, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _error("SLOPE_RULE_SCHEMA", f"{field} 须为1..{maximum}字符的非空文字")
    return value.strip()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _config(config):
    config = {} if config is None else config
    allowed = {"rules", "max_rules", "max_work_units", "max_output_bytes"}
    if not isinstance(config, dict) or set(config)-allowed:
        _error("SLOPE_RULE_CONFIG", "config 含不支持的字段")
    return {**config,
            "max_rules": _integer(config.get("max_rules", 128), "max_rules", 1, MAX_RULES),
            "max_work_units": _integer(config.get("max_work_units", 5_000_000), "max_work_units", 1, 20_000_000),
            "max_output_bytes": _integer(config.get("max_output_bytes", 16*1024**2), "max_output_bytes", 1024, 32*1024**2)}


def normalize_slope_rules(rules, *, max_rules=128):
    """Normalize declarative rules; no route, terrain or inventory is changed."""
    limit = _integer(max_rules, "max_rules", 1, MAX_RULES)
    if not isinstance(rules, list) or len(rules) > limit:
        _error("SLOPE_RULE_LIMIT", f"slope_rules 须为最多{limit}项的数组，不自动截断")
    allowed = {"id", "name", "enabled", "kind", "start_kp_m", "end_kp_m", "max_inline_slope_deg", "max_side_slope_deg"}
    normalized, seen = [], set()
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict) or set(rule)-allowed:
            _error("SLOPE_RULE_SCHEMA", f"第{i+1}条规则含不支持的字段")
        ident = _text(rule.get("id"), "rule.id", 128)
        if ident in seen:
            _error("SLOPE_RULE_DUPLICATE_ID", "规则id须唯一")
        seen.add(ident)
        kind = rule.get("kind")
        if not isinstance(kind,str) or kind not in {"inline", "side", "both"}:
            _error("SLOPE_RULE_SCHEMA", "kind 须为 inline、side 或 both")
        enabled = rule.get("enabled", True)
        if not isinstance(enabled, bool):
            _error("SLOPE_RULE_SCHEMA", "enabled 须为布尔值")
        start = finite_number(rule.get("start_kp_m", 0), "start_kp_m", minimum=0)
        if start == 0:start = 0.0
        end = rule.get("end_kp_m")
        end = None if end is None else finite_number(end, "end_kp_m", minimum=0)
        if end is not None and end <= start:
            _error("SLOPE_RULE_RANGE", "end_kp_m 须大于 start_kp_m")
        row = {"id": ident, "name": _text(rule.get("name", ident), "rule.name", 256),
               "enabled": enabled, "kind": kind, "start_kp_m": start, "end_kp_m": end}
        for component, field in (("inline", "max_inline_slope_deg"), ("side", "max_side_slope_deg")):
            needed = kind in {component, "both"}
            if not needed and field in rule:
                _error("SLOPE_RULE_SCHEMA", f"{field} 与规则kind不符")
            if needed:
                angle = finite_number(rule.get(field), field, minimum=0, maximum=90)
                if angle == 90:
                    _error("SLOPE_RULE_ANGLE", "阈值须小于90°，不是含90°的范围")
                if angle == 0:angle = 0.0
                row[field] = angle
        normalized.append(row)
    return normalized


def _merged(ranges):
    result = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if result and start <= result[-1][1]+EPS_M:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def _coverage(start, end, ranges):
    valid = _merged((max(start, a), min(end, b)) for a, b in ranges if b > start and a < end)
    unknown, cursor = [], start
    for a, b in valid:
        if a > cursor+EPS_M:
            unknown.append([cursor, a])
        cursor = max(cursor, b)
    if end > cursor+EPS_M:
        unknown.append([cursor, end])
    return {"requested_range_m": [start, end], "covered_ranges_m": valid,
            "unknown_ranges_m": unknown, "covered_length_m": sum(b-a for a, b in valid),
            "unknown_length_m": sum(b-a for a, b in unknown), "complete": not unknown and end > start}


def _diagnostic(code, message, **extra):
    return {"code": code, "message": message, **extra}


class _OutputBudget:
    """Charge retained result structures before accumulating unbounded lists."""
    def __init__(self, maximum):
        self.maximum, self.used = maximum, 0

    def charge(self, count):
        self.used += count
        if self.used > self.maximum:
            _error("SLOPE_RULE_OUTPUT_BUDGET", "完整规则结果超过max_output_bytes，不截断违例或诊断")

    def violation(self, value):
        # Each violation is also retained in a warning with its rule name.
        self.charge(2*len(_json(value))+1536)


def _angle(value, field, *, absolute=False):
    if value is None:
        return None
    return finite_number(value, field, minimum=0 if absolute else -90, maximum=90)


def _exceeds(angle, limit):
    # A route's independently rounded geodesic endpoint can change atan2 by a
    # few floating ULPs. This is numerical equality handling, not an engineering
    # angular tolerance; a nonzero angle is still above a zero-degree limit.
    magnitude=abs(angle)
    if limit==0:
        return magnitude>0
    return magnitude-limit > 8*max(math.ulp(magnitude),math.ulp(limit))


def _side_data(project, signature, terrain_signature):
    """Read saved measurements and validate their arithmetic, not re-sample them."""
    original = project.get("side_slopes")
    if original is None:
        return [], None, [_diagnostic("SIDE_SLOPES_MISSING", "未提供路线横坡采样，不能用纵坡或点水深替代")]
    if not isinstance(original, dict):
        _error("SIDE_SLOPES_SCHEMA", "side_slopes 须为对象")
    raw = original.get("samples", [])
    metadata = original.get("metadata", {})
    if not isinstance(raw, list) or len(raw) > MAX_SIDE_SAMPLES or not isinstance(metadata, dict):
        _error("SIDE_SLOPES_SCHEMA", "横坡samples/metadata结构或数量无效")
    previous = -1.
    for item in raw:
        if not isinstance(item, dict):
            _error("SIDE_SLOPES_SCHEMA", "横坡采样须为对象")
        kp = finite_number(item.get("kp_m"), "side_slopes.kp_m", minimum=0)
        if kp <= previous:
            _error("SIDE_SLOPES_SCHEMA", "横坡KP须严格递增，不能重复或乱序")
        previous = kp
        for field in ("port_slope_deg", "starboard_slope_deg", "side_slope_deg", "max_sampled_abs_slope_deg"):
            _angle(item.get(field), field, absolute=field=="max_sampled_abs_slope_deg")
        if not isinstance(item.get("complete"), bool) or not isinstance(item.get("source_boundary"), bool):
            _error("SIDE_SLOPES_SCHEMA", "complete/source_boundary 须为布尔值")
        transect = item.get("transect")
        if not isinstance(transect, list) or len(transect) < 3:
            _error("SIDE_SLOPES_SCHEMA", "横坡站须保留至少3点的原始transect")
        previous_offset = None
        for point in transect:
            if not isinstance(point, dict):
                _error("SIDE_SLOPES_SCHEMA", "transect点须为对象")
            offset = finite_number(point.get("offset_m"), "transect.offset_m")
            if previous_offset is not None and offset <= previous_offset:
                _error("SIDE_SLOPES_SCHEMA", "横截面offset须严格递增")
            previous_offset = offset
            if point.get("depth_m") is not None:
                finite_number(point["depth_m"], "transect.depth_m", minimum=0)
    diagnostics = []
    if original.get("model") != SIDE_MODEL or metadata.get("model") != SIDE_MODEL or type(original.get("schema_version")) is not int or original.get("schema_version") != 1:
        diagnostics.append(_diagnostic("SIDE_SLOPES_MODEL_UNSUPPORTED", "横坡模型或schema版本不受支持"))
    if original.get("route_signature") != signature:
        diagnostics.append(_diagnostic("SIDE_SLOPES_ROUTE_STALE", "横坡未绑定当前路线；旧横坡已停用"))
    if not metadata.get("terrain_library_signature") or metadata.get("terrain_library_signature") != terrain_signature:
        diagnostics.append(_diagnostic("SIDE_SLOPES_TERRAIN_STALE", "横坡未绑定当前地形源库；旧横坡已停用"))
    if diagnostics:
        return [], None, diagnostics
    start = finite_number(metadata.get("start_kp_m"), "side_slopes.start_kp_m", minimum=0)
    end = finite_number(metadata.get("end_kp_m"), "side_slopes.end_kp_m", minimum=0)
    spacing = finite_number(metadata.get("spacing_m"), "side_slopes.spacing_m", minimum=1, maximum=100_000)
    half_width = finite_number(metadata.get("half_width_m"), "side_slopes.half_width_m", minimum=0)
    cross_spacing = finite_number(metadata.get("cross_spacing_m"), "side_slopes.cross_spacing_m", minimum=0)
    if end < start or half_width == 0 or cross_spacing == 0:
        _error("SIDE_SLOPES_SCHEMA", "横坡范围不得倒置；半宽和横向站距须为正")
    samples = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            _error("SIDE_SLOPES_SCHEMA", "横坡采样须为对象")
        kp = finite_number(item.get("kp_m"), "side_slopes.kp_m", minimum=0)
        if samples and kp <= samples[-1]["kp_m"]:
            _error("SIDE_SLOPES_SCHEMA", "横坡KP须严格递增，不能重复或乱序")
        if kp < start-EPS_M or kp > end+EPS_M:
            _error("SIDE_SLOPES_SCHEMA", "横坡站点超出声明采样范围")
        complete, boundary = item.get("complete"), item.get("source_boundary")
        if not isinstance(complete, bool) or not isinstance(boundary, bool):
            _error("SIDE_SLOPES_SCHEMA", "complete/source_boundary 须为布尔值")
        angles = {key: _angle(item.get(key), key, absolute=key=="max_sampled_abs_slope_deg")
                  for key in ("port_slope_deg", "starboard_slope_deg", "side_slope_deg", "max_sampled_abs_slope_deg")}
        if complete and any(value is None for value in angles.values()):
            _error("SIDE_SLOPES_SCHEMA", "完整横坡站不能包含未知坡度")
        maximum = angles["max_sampled_abs_slope_deg"]
        if maximum is not None and any(value is not None and abs(value) > maximum+1e-8 for value in angles.values()):
            _error("SIDE_SLOPES_SCHEMA", "最大相邻采样坡度小于已记录割线角")
        transect = item.get("transect")
        if not isinstance(transect, list) or len(transect) < 3:
            _error("SIDE_SLOPES_SCHEMA", "横坡站须保留至少3点的原始transect")
        positions, depths, boundaries = [], [], []
        for point in transect:
            if not isinstance(point, dict):
                _error("SIDE_SLOPES_SCHEMA", "transect点须为对象")
            offset = finite_number(point.get("offset_m"), "transect.offset_m")
            depth = None if point.get("depth_m") is None else finite_number(point["depth_m"], "transect.depth_m", minimum=0)
            if positions and offset <= positions[-1]:
                _error("SIDE_SLOPES_SCHEMA", "横截面offset须严格递增")
            flag = point.get("source_boundary_to_next", False)
            if not isinstance(flag, bool):
                _error("SIDE_SLOPES_SCHEMA", "source_boundary_to_next 须为布尔值")
            positions.append(offset); depths.append(depth); boundaries.append(flag)
            if depth is not None:
                for field in ("source_id", "source_fingerprint"):
                    _text(point.get(field), f"transect.{field}", 128)
        if abs(positions[0]+half_width)>EPS_M or abs(positions[-1]-half_width)>EPS_M or not any(abs(q)<=EPS_M for q in positions):
            _error("SIDE_SLOPES_SCHEMA", "横截面须包含声明的左右端点和零偏移中心")
        if complete != all(value is not None for value in depths):
            _error("SIDE_SLOPES_SCHEMA", "横截面实测缺测与complete不一致")
        centre = next(j for j,q in enumerate(positions) if abs(q)<=EPS_M)
        secants = {
            "port_slope_deg": (0,centre,depths[:centre+1]),
            "starboard_slope_deg": (centre,len(depths)-1,depths[centre:]),
            "side_slope_deg": (0,len(depths)-1,depths),
        }
        for field,(a,b,values) in secants.items():
            expected_angle = None if any(v is None for v in values) else math.degrees(math.atan2(depths[a]-depths[b],positions[b]-positions[a]))
            value = angles[field]
            if (expected_angle is None)!=(value is None) or (expected_angle is not None and abs(expected_angle-value)>1e-8):
                _error("SIDE_SLOPES_SCHEMA", f"{field} 与原始有效测深割线或缺测语义不符")
        calculated = [abs(math.degrees(math.atan2(a-b, y-x))) for x,y,a,b in zip(positions,positions[1:],depths,depths[1:]) if a is not None and b is not None]
        expected = max(calculated) if calculated else None
        if (expected is None) != (maximum is None) or (expected is not None and abs(expected-maximum)>1e-8):
            _error("SIDE_SLOPES_SCHEMA", "max_sampled_abs_slope_deg 与原始相邻测深割线不符")
        # Classification uses the retained observations, not a rounded summary.
        angles["max_sampled_abs_slope_deg"] = expected
        valid_sources = {(p.get("source_id"),p.get("source_fingerprint"))
                         for p,depth in zip(transect,depths) if depth is not None}
        changed_source = len(valid_sources)>1
        if (any(boundaries) or changed_source) and not boundary:
            _error("SIDE_SLOPES_SCHEMA", "原始来源边界不能被站点source_boundary隐藏")
        samples.append({"kp_m":kp, "sample_index":i, "complete":complete, "source_boundary":boundary, **angles})
    return samples, {"start_kp_m":start,"end_kp_m":end,"spacing_m":spacing}, diagnostics


def _inline(rule, profile, metadata, route_length, budget):
    start, end = rule["start_kp_m"], rule["end_kp_m"]
    ranges, violations = [], []
    for i, (a, b) in enumerate(zip(profile, profile[1:])):
        left, right = max(start,a["kp_m"],0), min(end,b["kp_m"],route_length)
        if right <= left or a["depth_m"] is None or b["depth_m"] is None:
            continue
        angle = math.degrees(math.atan2(b["depth_m"]-a["depth_m"],b["kp_m"]-a["kp_m"]))
        ranges.append([left,right])
        if _exceeds(angle,rule["max_inline_slope_deg"]):
            violation={"kind":"inline","start_kp_m":left,"end_kp_m":right,
                       "value_deg":angle,"limit_deg":rule["max_inline_slope_deg"],
                       "profile_interval_index":i,"uncertain":False}
            budget.violation(violation);violations.append(violation)
    coverage = _coverage(start,end,ranges)
    diagnostics = []
    source = metadata.get("source", "unknown")
    if "stale" in str(source) or "unbound" in str(source):
        diagnostics.append(_diagnostic("INLINE_PROFILE_STALE", "纵坡剖面未绑定当前路线或来源库，旧数据停用"))
    if not coverage["complete"]:
        diagnostics.append(_diagnostic("INLINE_COVERAGE_INCOMPLETE", "规则KP范围含未覆盖或缺测区间；不跨缺测推断纵坡"))
    status = "violations" if violations else "sampled_pass" if coverage["complete"] else "incomplete" if ranges else "unknown"
    return {"status":status,"coverage":coverage,"model":"piecewise_linear_kp_depth",
            "profile_source":source,"waypoint_approximation":source=="waypoint_linear_approximation",
            "sampling_only":True}, violations, diagnostics


def _side(rule, samples, metadata, stale, route_length, budget):
    start, end = rule["start_kp_m"], rule["end_kp_m"]
    keys = [s["kp_m"] for s in samples]
    lo, hi = bisect_left(keys,start-EPS_M),bisect_right(keys,end+EPS_M)
    chosen = samples[lo:hi]
    supporting = samples[max(0,lo-1):min(len(samples),hi+1)]
    budget.charge(len(supporting)*96+1024)
    ranges, gaps = [], []
    if metadata is not None:
        for a,b in zip(supporting,supporting[1:]):
            left,right = max(start,a["kp_m"],0),min(end,b["kp_m"],route_length)
            if right<=left:continue
            gap=b["kp_m"]-a["kp_m"]
            if gap>metadata["spacing_m"]+EPS_M:
                gaps.append([left,right]);continue
            if a["complete"] and b["complete"] and not a["source_boundary"] and not b["source_boundary"]:
                ranges.append([left,right])
    coverage = _coverage(start,end,ranges)
    coverage.update(data_range_m=None if metadata is None else [metadata["start_kp_m"],metadata["end_kp_m"]],
                    sampled_kps_m=[s["kp_m"] for s in chosen],supporting_kps_m=[s["kp_m"] for s in supporting],
                    missing_kps_m=[s["kp_m"] for s in supporting if not s["complete"]],
                    uncertain_kps_m=[s["kp_m"] for s in supporting if s["source_boundary"]],
                    sampling_gaps_m=gaps,sampling_only=True,continuous_bed_verified=False)
    if not chosen:
        coverage["complete"] = False
    violations=[]
    for s in chosen:
        if s["kp_m"]>route_length+EPS_M or s["max_sampled_abs_slope_deg"] is None or not _exceeds(s["max_sampled_abs_slope_deg"],rule["max_side_slope_deg"]):continue
        violation={"kind":"side","start_kp_m":s["kp_m"],"end_kp_m":s["kp_m"],"kp_m":s["kp_m"],
                   "value_deg":s["max_sampled_abs_slope_deg"],"limit_deg":rule["max_side_slope_deg"],
                   "sample_index":s["sample_index"],"uncertain":s["source_boundary"] or not s["complete"]}
        budget.violation(violation);violations.append(violation)
    diagnostics=deepcopy(stale)
    if not chosen and not stale:
        diagnostics.append(_diagnostic("SIDE_NO_STATIONS_IN_RANGE", "规则KP范围内没有实际横坡站，不插值生成横坡"))
    if coverage["uncertain_kps_m"]:
        diagnostics.append(_diagnostic("SIDE_SOURCE_BOUNDARY_UNCERTAIN", "横截面跨地形来源边界，其角度不能作为已核实连续床面坡度"))
    if not coverage["complete"] and not stale:
        diagnostics.append(_diagnostic("SIDE_COVERAGE_INCOMPLETE", "规则KP范围超出采样域、有缺测/来源边界或站距缺口"))
    known = any(s["max_sampled_abs_slope_deg"] is not None for s in chosen)
    status="violations" if violations else "sampled_pass" if coverage["complete"] else "incomplete" if known else "unknown"
    return {"status":status,"coverage":coverage,"model":SIDE_MODEL,"sampling_only":True},violations,diagnostics


def check_slope_rules(project, config=None, *, analysis=None, terrain_signature=None):
    """Evaluate independent rules without updating route, profile or inventory.

    Optional keyword analysis is a current core result.  Without it, only the
    central profile reader is reused; the manufacturing solver is never called.
    """
    if not isinstance(project,dict) or not isinstance(project.get("route"),dict):
        _error("SLOPE_RULE_PROJECT", "需要包含route的schema1路径投影")
    config=_config(config)
    if analysis is not None and not isinstance(analysis,dict):
        _error("SLOPE_RULE_ANALYSIS_STALE", "analysis 须为当前core结果对象")
    profile_object=project.get("profile") or {}
    if not isinstance(profile_object,dict) or not isinstance(profile_object.get("metadata",{}) or {},dict):
        _error("SLOPE_RULE_PROFILE", "profile和metadata须为对象")
    rules=normalize_slope_rules(config.get("rules",project.get("slope_rules",[])),max_rules=config["max_rules"])
    enabled=[r for r in rules if r["enabled"]]
    need_inline=any(r["kind"] in {"inline","both"} for r in enabled)
    need_side=any(r["kind"] in {"side","both"} for r in enabled)
    route=project["route"];raw_points=route.get("points",[])
    if not isinstance(raw_points,list) or not 2<=len(raw_points)<=10_000:
        _error("SLOPE_RULE_PROJECT", "路线须有2..10,000点")
    raw_profile=(analysis or {}).get("profile",[]) if analysis is not None else profile_object.get("samples",[])
    raw_side=(project.get("side_slopes") or {}).get("samples",[]) if need_side and isinstance(project.get("side_slopes"),dict) else []
    if not isinstance(raw_profile,list) or len(raw_profile)>MAX_SAMPLES+(10_000 if analysis is not None else 0) or not isinstance(raw_side,list) or len(raw_side)>MAX_SIDE_SAMPLES:
        _error("SLOPE_RULE_LIMIT", "剖面/横坡采样数量超过预算")
    transect_count=sum(len(s.get("transect",[])) for s in raw_side if isinstance(s,dict) and isinstance(s.get("transect",[]),list))
    if transect_count>MAX_TRANSECT_POINTS:
        _error("SLOPE_RULE_LIMIT", "横截面累计原始探点超过50,000，不自动截断")
    n_inline=sum(r["kind"] in {"inline","both"} for r in enabled)
    n_side=sum(r["kind"] in {"side","both"} for r in enabled)
    work=len(raw_points)*8+(len(raw_profile)+len(raw_points))*4+transect_count*8+len(raw_side)*8+n_inline*(len(raw_profile)+len(raw_points))*8+n_side*len(raw_side)*8+len(rules)*32
    if work>config["max_work_units"]:
        _error("SLOPE_RULE_WORK_BUDGET", "逻辑工作预估超过max_work_units，未开始来源解析或规则检查")
    output_budget=_OutputBudget(config["max_output_bytes"])
    output_budget.charge(len(_json(rules))+512+len(enabled)*1024)
    from .core import _profiles, _validate_points, route_signature
    points=_validate_points(route);keys=[0.]
    for a,b in zip(points,points[1:]):keys.append(keys[-1]+inverse(a["longitude"],a["latitude"],b["longitude"],b["latitude"],route.get("curve","rhumb"))[0])
    length=keys[-1];signature=route_signature(project)
    if length<=EPS_M:_error("SLOPE_RULE_ROUTE_EMPTY", "全部路线点重合，规则没有正长度KP域")
    profile_model=(profile_object.get("metadata") or {}).get("model")
    if profile_model is not None and not isinstance(profile_model,str):
        _error("SLOPE_RULE_PROFILE", "profile.metadata.model 须为文字")
    bound_profile=profile_model in {"priority-terrain-library-v1","terrain-library-derived-profile-v1"}
    if terrain_signature is None and (need_side and project.get("side_slopes") is not None or need_inline and bound_profile):
        from .terrain_sources import terrain_library_signature
        terrain_signature=terrain_library_signature(project.get("terrain_sources",[]))
    warnings=[];profile=[];profile_metadata={}
    if need_inline:
        if analysis is not None:
            if not isinstance(analysis,dict) or analysis.get("route_signature")!=signature:
                _error("SLOPE_RULE_ANALYSIS_STALE", "传入analysis不是当前路线结果")
            profile=analysis.get("profile",[]);profile_metadata=analysis.get("profile_metadata",{})
            if not isinstance(profile_metadata,dict):
                _error("SLOPE_RULE_PROFILE", "analysis.profile_metadata须为对象")
            # The current analysis may correctly mark an OLD imported profile
            # unavailable. Its resolved-library binding must be current; the
            # original profile binding is allowed to remain stale and unknown.
            if bound_profile and profile_metadata.get("terrain_library_signature")!=terrain_signature:
                _error("SLOPE_RULE_ANALYSIS_STALE", "传入analysis的地形来源已过期")
        else:
            profile,_,profile_metadata=_profiles(project,points,keys,signature,warnings,terrain_signature)
        previous=-1.
        for item in profile:
            if not isinstance(item,dict):_error("SLOPE_RULE_PROFILE", "profile采样须为对象")
            kp=finite_number(item.get("kp_m"),"profile.kp_m",minimum=0)
            if kp<=previous:_error("SLOPE_RULE_PROFILE", "profile KP须严格递增")
            previous=kp
            if item.get("depth_m") is not None:finite_number(item["depth_m"],"profile.depth_m",minimum=0)
    side_samples,side_metadata,side_diagnostics=_side_data(project,signature,terrain_signature) if need_side else ([],None,[])
    results=[]
    for original in rules:
        rule={**original,"end_kp_m":length if original["end_kp_m"] is None else original["end_kp_m"]}
        row={key:rule[key] for key in ("id","name","enabled","kind","start_kp_m","end_kp_m")}
        row.update(requested_range_m=[original["start_kp_m"],original["end_kp_m"]],
                   status="disabled",components={},violations=[],diagnostics=[])
        if rule["enabled"]:
            empty = rule["end_kp_m"]<=rule["start_kp_m"]
            for kind,checker,args in (("inline",_inline,([] if empty else profile,profile_metadata,length,output_budget)),("side",_side,([] if empty else side_samples,side_metadata,side_diagnostics,length,output_budget))):
                if rule["kind"] not in {kind,"both"}:continue
                component,violations,diagnostics=checker(rule,*args)
                row["components"][kind]=component;row["violations"].extend(violations);row["diagnostics"].extend(diagnostics)
            states=[c["status"] for c in row["components"].values()]
            row["status"]="violations" if row["violations"] else "unknown" if "unknown" in states else "incomplete" if "incomplete" in states else "sampled_pass"
            if empty:
                row["status"]="unknown"
                row["diagnostics"].append(_diagnostic("RULE_RANGE_EMPTY", "规则起点已达到或超过当前路线终点；声明保留，没有可检查的正长度域"))
        results.append(row)
    incomplete=sum(any(c["status"]=="incomplete" or not c["coverage"]["complete"] for c in r["components"].values()) for r in results if r["enabled"])
    unknown=sum(any(c["status"]=="unknown" for c in r["components"].values()) for r in results if r["enabled"])
    violations=sum(len(r["violations"]) for r in results)
    available=all(c["coverage"]["complete"] for r in results if r["enabled"] for c in r["components"].values())
    status="not_configured" if not rules else "disabled" if not enabled else "violations" if violations else "unknown" if unknown else "incomplete" if incomplete else "sampled_pass"
    for row in results:
        if not row["enabled"]:continue
        for item in row["diagnostics"]:warnings.append({**item,"severity":"warning","rule_id":row["id"]})
        for item in row["violations"]:warnings.append({"code":"SLOPE_RULE_VIOLATION","severity":"warning","rule_id":row["id"],"message":f"规则 {row['name']} 的{item['kind']}采样坡度 {abs(item['value_deg']):g}° 超过 {item['limit_deg']:g}°",**item})
    output={"model":MODEL,"validation_status":"research","rules":rules,"results":results,
            "summary":{"status":status,"enabled_rules":len(enabled),"disabled_rules":len(rules)-len(enabled),"violation_count":violations,"violating_rules":sum(bool(r["violations"]) for r in results),"incomplete_rules":incomplete,"unknown_rules":unknown,"all_requested_data_available":available,"continuous_bed_verified":False},
            "metadata":{"route_signature":signature,"terrain_library_signature":terrain_signature,"rules_signature":hashlib.sha256(_json(rules)).hexdigest()},
            "warnings":warnings,"budget":{"estimated_work_units":work,"max_work_units":config["max_work_units"],"max_rules":config["max_rules"],"max_output_bytes":config["max_output_bytes"],"work_basis":"bounded logical parsing and rule/sample comparisons; not CPU FLOPs, hard runtime or RSS"},
            "assumptions":["规则彼此独立；比较坡度绝对值，等于阈值不算超限。","纵坡按有效相邻KP/深度分段及规则区间重叠检查；不跨缺测。","横坡只检查域内真实站点的最大相邻采样绝对坡度，不插值补站，不推断站间连续床面。","来源边界、缺测、过期或不完整覆盖不能判为通过；真实超限仍完整列出。","只读检查不更改路线、剖面、地形源、制造库存或固定约束；非原厂算法等效及海试精度证明。"]}
    if len(_json(output))>config["max_output_bytes"]:
        _error("SLOPE_RULE_OUTPUT_BUDGET", "完整规则结果超过max_output_bytes，不截断违例或诊断")
    return output
