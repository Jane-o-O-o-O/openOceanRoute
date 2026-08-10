"""Declarative, bounded RPL text templates; never executes template content.

This is OceanRoute's open JSON schema, not a Makai format-file decoder.
Character offsets count Unicode code points after removing a leading BOM.
"""
from __future__ import annotations

from copy import deepcopy
import csv
import io
import json
import math
import re
from uuid import uuid4

SCHEMA = "oceanroute.rpl-template"
MAX_TEXT_BYTES = 32 * 1024 * 1024
MAX_TEMPLATE_BYTES = 128 * 1024
MAX_RECORDS = 10_000
MAX_LINES = 500_000
MAX_COLUMNS = 512
MAX_LINE_CHARS = 1_000_000

COORDINATES = {"longitude", "latitude"} | {
    f"{axis}_{part}" for axis in ("longitude", "latitude")
    for part in ("degrees", "minutes", "seconds", "hemisphere")}
FIELDS = COORDINATES | {"label", "note", "depth_m", "kp_m", "cable_kp_m", "cable_type_id",
    "slack_pct", "slack_basis", "mode", "fixed_cable_length_m", "burial", "stop_hours", "extra_cost"}
LENGTH_FACTORS = {"m": 1.0, "km": 1000.0, "ft": .3048, "nm": 1852.0}
UNIT_FACTORS = {k: LENGTH_FACTORS for k in ("depth_m", "kp_m", "cable_kp_m", "fixed_cable_length_m")}
UNIT_FACTORS.update(slack_pct={"percent": 1.0, "fraction": 100.0},
    stop_hours={"h": 1.0, "min": 1/60, "s": 1/3600})
DEFAULT_UNITS = {k: "m" for k in ("depth_m", "kp_m", "cable_kp_m", "fixed_cable_length_m")}
DEFAULT_UNITS.update(slack_pct="percent", stop_hours="h")


def _json(value, limit):
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("模板/结果必须为有限JSON，不能含NaN、Infinity或可执行对象") from exc
    if len(encoded) > limit:
        raise ValueError(f"JSON超过体积上限 {limit} 字节")
    return encoded


def _integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name}须为{low}至{high}之间的整数")
    return value


def _number(value, name, low=None, *, strict=False):
    if isinstance(value, bool):
        raise ValueError(f"{name}须为有限数值")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{name}须为有限数值") from exc
    if not math.isfinite(result) or (low is not None and (result <= low if strict else result < low)):
        raise ValueError(f"{name}须为有限数值" + (f"且{'大于' if strict else '不小于'}{low}" if low is not None else ""))
    return result


def parse_coordinate(value, *, latitude):
    """Decimal degrees or DMS; textual -0 preserves a south/west sign."""
    value = str(value).strip()
    # csv.writer/float legitimately emits scientific notation near zero. A
    # complete scalar is unambiguous; exponents mixed into DMS are not.
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?",value):
        scalar_value=_number(value,"坐标")
        if abs(scalar_value)>(90 if latitude else 180): raise ValueError("坐标超出有效范围")
        return scalar_value
    value = value.upper().translate(str.maketrans({"北": "N", "南": "S", "东": "E", "西": "W"}))
    if not value or re.search(r"[^NSEW0-9+\-.\s°º'\"′″:]", value):
        raise ValueError("坐标包含无法识别的字符")
    hemispheres = re.findall(r"[NSEW]", value)
    if len(hemispheres) > 1 or any(h not in ("NS" if latitude else "EW") for h in hemispheres):
        raise ValueError("坐标半球与字段不一致")
    if hemispheres and value.find(hemispheres[0]) not in (0,len(value)-1):
        raise ValueError("半球标识须位于坐标首尾，不能混合科学计数与度分秒")
    stripped = re.sub(r"[NSEW°º'\"′″:]", " ", value)
    tokens = stripped.split()
    scalar = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
    if not 1 <= len(tokens) <= 3 or any(not scalar.fullmatch(token) for token in tokens):
        raise ValueError("坐标需为十进制度或以空格/度分秒符号分隔的度分秒")
    if any(token.startswith(("+", "-")) for token in tokens[1:]):
        raise ValueError("分/秒不能带独立正负号")
    if len(tokens) > 1 and any("." in token for token in tokens[:-1]):
        raise ValueError("度分秒仅最后一项可含小数，不能重复定义小数单位")
    parts = [_number(token, "坐标") for token in tokens]
    if any(not 0 <= p < 60 for p in parts[1:]):
        raise ValueError("分和秒应在0到60之间")
    negative = tokens[0].startswith("-")
    sign = -1 if negative else 1
    if hemispheres:
        hemisphere_sign = -1 if hemispheres[0] in "SW" else 1
        if negative and hemisphere_sign > 0:
            raise ValueError("负号与半球标识矛盾")
        sign = hemisphere_sign
    result = sign * (abs(parts[0]) + (parts[1]/60 if len(parts)>1 else 0) + (parts[2]/3600 if len(parts)>2 else 0))
    if abs(result) > (90 if latitude else 180):
        raise ValueError("坐标超出有效范围")
    return result


def validate_template(template):
    if not isinstance(template, dict):
        raise ValueError("template须为JSON对象")
    _json(template, MAX_TEMPLATE_BYTES)
    allowed = {"schema", "schema_version", "name", "format", "index_base", "lines_per_record", "header_lines",
        "skip_blank_lines", "comment_prefixes", "error_policy", "leg_assignment", "delimiter", "quotechar",
        "fields", "defaults", "units", "cable_type_map", "depth_positive", "max_records", "column_units"}
    if set(template)-allowed:
        raise ValueError("template含不支持的字段："+", ".join(sorted(set(template)-allowed)))
    result = deepcopy(template)
    if result.get("schema") != SCHEMA or type(result.get("schema_version")) is not int or result.get("schema_version") != 1:
        raise ValueError("不支持的RPL模板schema/version；不是原厂格式文件")
    result.setdefault("name", "RPL格式模板")
    if not isinstance(result["name"], str) or len(result["name"]) > 256:
        raise ValueError("template.name须为不超过256字符的文本")
    if result.get("format") not in ("fixed_width", "delimited"):
        raise ValueError("template.format须为fixed_width或delimited")
    if result.setdefault("column_units","unicode_code_points")!="unicode_code_points":
        raise ValueError("column_units仅支持unicode_code_points，不支持字节或屏幕格列")
    base = _integer(result.setdefault("index_base", 1), "index_base", 0, 1)
    lines = _integer(result.setdefault("lines_per_record", 1), "lines_per_record", 1, 32)
    _integer(result.setdefault("header_lines", 0), "header_lines", 0, MAX_LINES)
    _integer(result.setdefault("max_records", MAX_RECORDS), "max_records", 2, MAX_RECORDS)
    if not isinstance(result.setdefault("skip_blank_lines", True), bool):
        raise ValueError("skip_blank_lines须为布尔值")
    prefixes = result.setdefault("comment_prefixes", ["#"])
    if not isinstance(prefixes, list) or len(prefixes)>16 or any(not isinstance(p,str) or not p or len(p)>32 or "\n" in p or "\r" in p for p in prefixes):
        raise ValueError("comment_prefixes须为最多16个非空单行文本前缀")
    if result.setdefault("error_policy", "collect") not in ("collect", "skip", "reject"):
        raise ValueError("error_policy须为collect、skip或reject")
    if result.setdefault("leg_assignment", "outgoing") not in ("outgoing", "incoming"):
        raise ValueError("leg_assignment须为outgoing或incoming")
    if result.setdefault("depth_positive", "down") not in ("down", "up"):
        raise ValueError("depth_positive须为down或up")
    if result["format"] == "delimited":
        for key, default in (("delimiter", ","), ("quotechar", '"')):
            item = result.setdefault(key, default)
            if not isinstance(item, str) or len(item)!=1 or item in "\r\n\0":
                raise ValueError(f"{key}须为单个非换行字符（制表符使用JSON的\\t）")
        if result["delimiter"] == result["quotechar"]:
            raise ValueError("delimiter与quotechar不能相同")
    elif "delimiter" in result or "quotechar" in result:
        raise ValueError("fixed_width模板不使用delimiter/quotechar")
    fields = result.get("fields")
    if not isinstance(fields, dict) or not fields or set(fields)-FIELDS:
        raise ValueError("fields须为支持的RPL字段位置对象")
    for name, spec in fields.items():
        options = {"line", "start", "width", "required", "trim"} if result["format"] == "fixed_width" else {"line", "column", "required", "trim"}
        if not isinstance(spec,dict) or set(spec)-options:
            raise ValueError(f"fields.{name}含无效位置/可执行字段")
        _integer(spec.setdefault("line", base), f"fields.{name}.line", base, base+lines-1)
        if result["format"] == "fixed_width":
            _integer(spec.get("start"), f"fields.{name}.start", base, base+MAX_LINE_CHARS-1)
            _integer(spec.get("width"), f"fields.{name}.width", 1, MAX_LINE_CHARS)
            if spec["start"]-base+spec["width"] > MAX_LINE_CHARS:
                raise ValueError(f"fields.{name}超过行字符预算")
        else:
            _integer(spec.get("column"), f"fields.{name}.column", base, base+MAX_COLUMNS-1)
        for key, default in (("required", name in ("longitude", "latitude", "longitude_degrees", "latitude_degrees", "cable_kp_m")), ("trim", True)):
            if not isinstance(spec.setdefault(key,default),bool):
                raise ValueError(f"fields.{name}.{key}须为布尔值")
    for axis in ("longitude", "latitude"):
        combined = axis in fields
        parts = [k for k in fields if k.startswith(axis+"_")]
        if combined and parts or (not combined and axis+"_degrees" not in fields):
            raise ValueError(f"{axis}须仅使用完整坐标字段或degrees与可选分/秒/半球字段")
    if "cable_kp_m" in fields and ("fixed_cable_length_m" in fields or "mode" in fields):
        raise ValueError("累计cable_kp_m是固定制造量来源，不能同时声明mode或fixed_cable_length_m字段")
    units = result.setdefault("units", {})
    if not isinstance(units,dict) or set(units)-UNIT_FACTORS.keys():
        raise ValueError("units含不支持的字段")
    for name, unit in units.items():
        if not isinstance(unit,str) or unit not in UNIT_FACTORS[name]:
            raise ValueError(f"units.{name}不支持单位{unit}")
    defaults = result.setdefault("defaults", {})
    if not isinstance(defaults,dict) or set(defaults)-{"curve","mode","slack_basis","slack_pct","cable_type_id","burial","stop_hours","extra_cost"}:
        raise ValueError("defaults含不支持的字段")
    for key, default, options in (("curve","rhumb",("rhumb","geodesic")),("mode","flexible",("flexible","fixed")),("slack_basis","surface",("surface","bottom"))):
        if defaults.setdefault(key, default) not in options:
            raise ValueError(f"defaults.{key}值无效")
    defaults["slack_pct"] = _number(defaults.get("slack_pct",0), "defaults.slack_pct", -100, strict=True)
    if "cable_type_id" in defaults and (not isinstance(defaults["cable_type_id"],str) or not defaults["cable_type_id"].strip() or len(defaults["cable_type_id"])>128):
        raise ValueError("defaults.cable_type_id须为非空文本")
    if "burial" in defaults and not isinstance(defaults["burial"],bool):
        raise ValueError("defaults.burial须为布尔值")
    for name in ("stop_hours", "extra_cost"):
        if name in defaults:
            defaults[name] = _number(defaults[name], "defaults."+name, 0)
    mapping = result.setdefault("cable_type_map", {})
    if not isinstance(mapping,dict) or len(mapping)>1000 or any(not isinstance(k,str) or not isinstance(v,str) or not k or not v or len(k)>128 or len(v)>128 for k,v in mapping.items()):
        raise ValueError("cable_type_map须为最多1000项的非空文本到缆型ID映射")
    _json(result, MAX_TEMPLATE_BYTES)
    return result


def dump_template(template):
    return json.dumps(validate_template(template), ensure_ascii=False, allow_nan=False, indent=2)


def load_template(text):
    if not isinstance(text,str) or len(text.encode("utf-8"))>MAX_TEMPLATE_BYTES:
        raise ValueError("模板JSON须为不超过128KiB的文本")
    try:
        obj = json.loads(text, parse_constant=lambda s: (_ for _ in ()).throw(ValueError("模板不能含"+s)))
    except (json.JSONDecodeError,RecursionError) as exc:
        raise ValueError("RPL模板JSON语法错误") from exc
    return validate_template(obj)


def _physical_lines(text):
    if not isinstance(text,str) or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("RPL文本超过32MiB或不是文本")
    lines = text.removeprefix("\ufeff").splitlines()
    if len(lines)>MAX_LINES or any(len(line)>MAX_LINE_CHARS for line in lines):
        raise ValueError("RPL文本超过物理行/单行字符预算")
    return lines


class _CSVLines:
    """Skip comments only before a CSV logical row, never inside quotes."""
    def __init__(self, lines, header_lines, skip_blank_lines, comment_prefixes):
        self.lines, self.cursor = lines, header_lines
        self.skip_blank, self.prefixes = skip_blank_lines, tuple(comment_prefixes)
        self.active, self.used = False, []

    def __iter__(self): return self

    def __next__(self):
        while self.cursor < len(self.lines):
            number=self.cursor+1; line=self.lines[self.cursor]; self.cursor+=1
            if not self.active and ((self.skip_blank and not line.strip()) or (self.prefixes and line.lstrip().startswith(self.prefixes))):
                continue
            self.active=True; self.used.append(number)
            return line+"\n"
        raise StopIteration


def iter_csv_records(text, delimiter=",", *, quotechar='"', header_lines=0, skip_blank_lines=True, comment_prefixes=("#",)):
    lines=_physical_lines(text)
    source=_CSVLines(lines,header_lines,skip_blank_lines,comment_prefixes)
    reader=csv.reader(source,delimiter=delimiter,quotechar=quotechar,strict=True)
    while True:
        source.active=False; source.used=[]
        try:
            row=next(reader)
        except StopIteration:
            return
        except csv.Error as exc:
            where=source.used[0] if source.used else source.cursor+1
            raise _RecordError(f"CSV语法错误，第{where}行：{exc}",line=where,line_end=source.used[-1] if source.used else where) from exc
        if len(row)>MAX_COLUMNS:
            raise _BudgetError(f"第{source.used[0]}行超过{MAX_COLUMNS}列预算")
        yield row, source.used[0], source.used[-1]


class _RecordError(ValueError):
    def __init__(self,message,field=None,line=None,line_end=None):
        super().__init__(message); self.field=field; self.line=line; self.line_end=line_end or line


class _BudgetError(ValueError):
    pass


def _extract(record, spec, template, field, allow_missing_optional_columns=False):
    offset=spec["line"]-template["index_base"]
    if offset>=len(record):
        raise _RecordError("记录行数不足",field,record[-1][2] if record else None)
    data,start,end=record[offset]
    if template["format"]=="fixed_width":
        first=spec["start"]-template["index_base"]; last=first+spec["width"]
        if last>len(data):
            raise _RecordError(f"短行：需要至少{last}个Unicode字符，实际{len(data)}",field,start)
        value=data[first:last]
    else:
        index=spec["column"]-template["index_base"]
        if index>=len(data):
            if allow_missing_optional_columns and not spec["required"]: return ""
            raise _RecordError(f"列数不足：需要第{index+1}列，实际{len(data)}列",field,start)
        value=data[index]
    value=value.strip() if spec["trim"] else value
    if not value and spec["required"]:
        raise _RecordError("必填字段为空",field,start)
    return value


def _parse_record(record, template, allow_missing_optional_columns=False):
    fields=template["fields"]; values={name:_extract(record,spec,template,name,allow_missing_optional_columns) for name,spec in fields.items()}
    typed={}; source=dict(values)
    def number(name,low=None,strict=False):
        raw=values.get(name,"")
        if not raw: return None
        try:
            result=_number(raw,name)
            result*=UNIT_FACTORS.get(name,{}).get(template["units"].get(name,DEFAULT_UNITS.get(name)),1)
            if name=="depth_m" and template["depth_positive"]=="up": result=-result
            return _number(result,name,low,strict=strict)
        except ValueError as exc:
            raise _RecordError(str(exc),name,record[fields[name]["line"]-template["index_base"]][1]) from exc
    for axis in ("longitude","latitude"):
        if axis in values:
            raw=values[axis]
        else:
            parts=[values[axis+"_degrees"]]
            minutes=values.get(axis+"_minutes","")
            seconds=values.get(axis+"_seconds","")
            if minutes or seconds: parts.append(minutes or "0")
            if seconds: parts.append(seconds)
            hemisphere=values.get(axis+"_hemisphere","").upper()
            if hemisphere and hemisphere not in (("N","S","北","南") if axis=="latitude" else ("E","W","东","西")):
                raise _RecordError("分列半球字段须为该坐标的一枚半球字符",axis+"_hemisphere",record[fields[axis+"_hemisphere"]["line"]-template["index_base"]][1])
            if any(not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)",p) for p in parts):
                raise _RecordError("分列度/分/秒须为独立数值",axis+"_degrees",record[fields[axis+"_degrees"]["line"]-template["index_base"]][1])
            raw=" ".join(parts+[hemisphere])
        try: typed[axis]=parse_coordinate(raw,latitude=axis=="latitude")
        except ValueError as exc:
            field=axis if axis in fields else axis+"_degrees"
            raise _RecordError(str(exc),field,record[fields[field]["line"]-template["index_base"]][1]) from exc
    typed["depth_m"]=number("depth_m",0)
    for name in ("kp_m","cable_kp_m","fixed_cable_length_m","stop_hours","extra_cost"):
        if name in fields: typed[name]=number(name,0)
    if "cable_kp_m" in fields and typed["cable_kp_m"] is None:
        raise _RecordError("累计缆里程每个有效记录均须填写","cable_kp_m",record[fields["cable_kp_m"]["line"]-template["index_base"]][1])
    if "slack_pct" in fields: typed["slack_pct"]=number("slack_pct",-100,strict=True)
    for name,options in (("mode",("flexible","fixed")),("slack_basis",("surface","bottom"))):
        if values.get(name):
            if values[name] not in options: raise _RecordError(f"{name}值无效",name,record[fields[name]["line"]-template["index_base"]][1])
            typed[name]=values[name]
    if values.get("burial"):
        word=values["burial"].casefold()
        if word not in ("true","false","1","0","yes","no","是","否"):
            raise _RecordError("burial须为true/false、1/0、yes/no或是/否","burial",record[fields["burial"]["line"]-template["index_base"]][1])
        typed["burial"]=word in ("true","1","yes","是")
    if values.get("cable_type_id"):
        raw=values["cable_type_id"]
        typed["cable_type_id"]=template["cable_type_map"].get(raw,raw)
        if len(typed["cable_type_id"])>128: raise _RecordError("缆型ID超过128字符","cable_type_id",record[0][1])
    for name in ("label","note"):
        typed[name]=values.get(name,"")
        if len(typed[name])>4096: raise _RecordError(f"{name}超过4096字符",name,record[0][1])
    return typed,source


def parse_rpl(text, template, *, expected_columns=None):
    template=validate_template(template)
    lines=_physical_lines(text)
    if template["header_lines"]>len(lines): raise ValueError("header_lines超过文件实际物理行数")
    if template["format"]=="fixed_width":
        prefixes=tuple(template["comment_prefixes"])
        data=((line,n,n) for n,line in enumerate(lines,1) if n>template["header_lines"]
              and not (template["skip_blank_lines"] and not line.strip())
              and not (prefixes and line.lstrip().startswith(prefixes)))
    else:
        data=iter_csv_records(text,template["delimiter"],quotechar=template["quotechar"],header_lines=template["header_lines"],
                             skip_blank_lines=template["skip_blank_lines"],comment_prefixes=template["comment_prefixes"])
    points=[]; records=[]; accepted=[]; warnings=[]; errors=[]; count=0; structural_error=False
    def problem(exc,record,index,code="RPL_RECORD_INVALID"):
        item={"code":code,"severity":"error","record":index,"row":getattr(exc,"line",None) or (record[0][1] if record else None),
              "line_start":record[0][1] if record else getattr(exc,"line",None),"line_end":max(record[-1][2],getattr(exc,"line_end",None) or record[-1][2]) if record else getattr(exc,"line_end",None),
              "field":getattr(exc,"field",None),"message":f"记录{index}，第{getattr(exc,'line',None) or (record[0][1] if record else '?')}行：{exc}"}
        if template["error_policy"]=="reject": raise ValueError(item["message"]) from exc
        warnings.append(item); errors.append(item)
        records.append({"record":index,"line_start":item["line_start"],"line_end":item["line_end"],"accepted":False,"error":item})
    iterator=iter(data)
    while True:
        record=[]
        try:
            for _ in range(template["lines_per_record"]): record.append(next(iterator))
        except StopIteration:
            if not record: break
        except _BudgetError:
            raise
        except ValueError as exc:
            count+=1
            if count>template["max_records"]: raise ValueError(f"RPL记录超过{template['max_records']}个预算") from exc
            structural_error=True; problem(exc,record,count,"RPL_CSV_INVALID"); break
        count+=1
        if count>template["max_records"]: raise ValueError(f"RPL记录超过{template['max_records']}个预算")
        if len(record)!=template["lines_per_record"]:
            problem(_RecordError(f"末尾记录不完整，需要{template['lines_per_record']}行，实际{len(record)}行",line=record[-1][2]),record,count); break
        try:
            if expected_columns is not None and any(len(row[0])>expected_columns for row in record):
                raise _RecordError("列数多于表头，请检查引号和分隔符",line=record[0][1])
            typed,source=_parse_record(record,template,allow_missing_optional_columns=expected_columns is not None)
            previous=accepted[-1][0] if accepted else None
            for name in ("kp_m","cable_kp_m"):
                if previous and typed.get(name) is not None and previous.get(name) is not None and typed[name]<previous[name]-1e-7:
                    spec=template["fields"][name]
                    raise _RecordError(f"{name}不能下降",name,record[spec["line"]-template["index_base"]][1])
            point={"id":str(uuid4()),"label":typed["label"] or f"P{len(points)+1:02d}","longitude":typed["longitude"],
                   "latitude":typed["latitude"],"depth_m":typed["depth_m"],"note":typed["note"],"kind":"rigid"}
            details={"record":count,"line_start":record[0][1],"line_end":record[-1][2],"accepted":True,"point_id":point["id"],
                     "source_kp_m":typed.get("kp_m"),"source_cable_kp_m":typed.get("cable_kp_m"),"source_slack_pct":typed.get("slack_pct")}
            points.append(point); records.append(details); accepted.append((typed,details))
        except _RecordError as exc: problem(exc,record,count)
    legs=[]; kps=[0.0]; engineering_errors=[]; defaults=template["defaults"]
    from .geodesy import inverse
    for index,(a,b) in enumerate(zip(accepted,accepted[1:])):
        first,ainfo=a; second,binfo=b
        owner,info=(a if template["leg_assignment"]=="outgoing" else b)
        leg={key:value for key,value in defaults.items() if key!="curve"}
        leg.update({key:value for key,value in owner.items() if key in {"cable_type_id","slack_pct","slack_basis","mode","fixed_cable_length_m","burial","stop_hours","extra_cost"} and value is not None})
        try:
            distance,_=inverse(first["longitude"],first["latitude"],second["longitude"],second["latitude"],defaults["curve"])
            kps.append(kps[-1]+distance if kps[-1] is not None else None)
            if "cable_kp_m" in template["fields"]:
                leg.update(mode="fixed",fixed_cable_length_m=max(0.0,second["cable_kp_m"]-first["cable_kp_m"]))
                if distance<1e-7 and leg["fixed_cable_length_m"]>0:
                    warnings.append({"code":"RPL_ZERO_KP_CABLE_JUMP","severity":"info","record":binfo["record"],"row":binfo["line_start"],"message":"重复坐标的累计缆里程跳跃已保留为零平面长度的固定制造段；余缆百分比不适用"})
            elif leg.get("fixed_cable_length_m") is not None:
                if owner.get("mode")=="flexible": raise ValueError("flexible字段与固定缆长冲突")
                leg["mode"]="fixed"
            if leg["mode"]=="fixed" and leg.get("fixed_cable_length_m") is None:
                raise ValueError("固定段缺少fixed_cable_length_m")
            if leg["mode"]=="flexible" and leg["slack_basis"]=="bottom" and (first["depth_m"] is None or second["depth_m"] is None):
                raise ValueError("导入底余缆区间缺少完整端点深度，不能以平面余缆替代")
            if ainfo["record"]+1!=binfo["record"]:
                warnings.append({"code":"RPL_REJECTED_RECORD_BRIDGE","severity":"warning","record":binfo["record"],"row":binfo["line_start"],"message":f"有效记录{ainfo['record']}与{binfo['record']}之间存在被拒记录，当前几何将直接连接；必须显式接受跳过策略"})
        except ValueError as exc:
            if len(kps)<index+2: kps.append(None)
            item={"code":"RPL_LEG_INVALID","severity":"error","record":info["record"],"row":info["line_start"],"leg_index":index,"message":str(exc)}
            if template["error_policy"]=="reject": raise ValueError(f"记录{info['record']}第{info['line_start']}行区间：{exc}") from exc
            warnings.append(item); engineering_errors.append(item)
        legs.append(leg)
    origin=accepted[0][0].get("kp_m") if accepted else None
    for index,(_,info) in enumerate(accepted):
        if index<len(kps): info["computed_route_kp_m"]=kps[index]
        if origin is not None and info["source_kp_m"] is not None and index<len(kps) and kps[index] is not None and abs(info["source_kp_m"]-origin-kps[index])>max(1.0,kps[index]*1e-5):
            warnings.append({"code":"RPL_SOURCE_KP_DIFFERS","severity":"warning","record":info["record"],"row":info["line_start"],"message":"原始KP与WGS84路线计算KP不同；已保留为来源信息，未替代几何里程"})
    cable_origin=accepted[0][0].get("cable_kp_m") if accepted else None
    if cable_origin:
        warnings.append({"code":"RPL_CABLE_ORIGIN_OFFSET","severity":"info","record":accepted[0][1]["record"],"row":accepted[0][1]["line_start"],"message":f"原始累计缆里程起点{cable_origin:g}m仅作来源，导入制造从该值归零并保留各段差额"})
    change_records=[]; last_slack=None
    for typed,info in accepted:
        slack=typed.get("slack_pct")
        if slack is not None and slack!=last_slack: change_records.append(info["record"])
        if slack is not None: last_slack=slack
    result={"points":points,"legs":legs,"route_options":{"curve":defaults["curve"],"mode":"flexible","slack_basis":defaults["slack_basis"],"slack_pct":defaults["slack_pct"]},
        "warnings":warnings,"errors":errors+engineering_errors,"records":records,"accepted_rows":len(points),"rejected_rows":sum(not r["accepted"] for r in records),
        "can_apply":len(points)>=2 and not engineering_errors and not structural_error and (not errors or template["error_policy"]=="skip"),"template":template,
        "metadata":{"schema":SCHEMA,"coordinate_crs":"EPSG:4326","column_units":"unicode_code_points","depth_positive":"down","internal_length_units":"m",
            "leg_assignment":template["leg_assignment"],"cable_distance_origin_m":cable_origin,"source_kp_origin_m":origin,"slack_change_records":change_records,
            "cable_distance_policy":"fixed_segment_differences" if "cable_kp_m" in template["fields"] else "explicit_leg_fields",
            "depth_source":"waypoint_depths_only_not_measured_profile","cable_type_ids":sorted({l["cable_type_id"] for l in legs if l.get("cable_type_id")})}}
    _json(result,MAX_TEXT_BYTES)
    return result


def example_templates():
    """Actual text/templates, suitable for the UI without invented outputs."""
    fixed={"schema":SCHEMA,"schema_version":1,"name":"两行定宽 Unicode / 累计缆公里", "format":"fixed_width","index_base":1,
        "lines_per_record":2,"header_lines":1,"comment_prefixes":["#",";"],"defaults":{"cable_type_id":"LW"},"units":{"cable_kp_m":"km"},
        "fields":{"label":{"line":1,"start":1,"width":6},"longitude":{"line":1,"start":7,"width":14},"latitude":{"line":1,"start":21,"width":14},
                  "depth_m":{"line":2,"start":1,"width":8},"cable_kp_m":{"line":2,"start":9,"width":10},"cable_type_id":{"line":2,"start":19,"width":6},
                  "slack_pct":{"line":2,"start":25,"width":8}}}
    text="示例：字符列以Unicode代码点计数\n"+"\n".join(f"{label:<6}{lon:<14}{lat:<14}\n{depth:<8}{cable:<10}{kind:<6}{slack:<8}" for label,lon,lat,depth,cable,kind,slack in [
        ("起点","118 00 E","22 00 N","30","5.000","LW","1.5"),("转点","118 01 E","22 00 N","45","6.750","LW","1.5"),("终点","118 02 E","22 00 N","60","8.500","LW","2")])
    split={"schema":SCHEMA,"schema_version":1,"name":"CSV分列度分秒 / 出发段余缆","format":"delimited","index_base":0,"header_lines":1,
        "defaults":{"cable_type_id":"LW"},"fields":{"label":{"column":0},"longitude_degrees":{"column":1},"longitude_minutes":{"column":2},
        "longitude_seconds":{"column":3},"longitude_hemisphere":{"column":4},"latitude_degrees":{"column":5},"latitude_minutes":{"column":6},
        "latitude_seconds":{"column":7},"latitude_hemisphere":{"column":8},"slack_pct":{"column":9},"note":{"column":10}}}
    csv_text='label,lon_deg,lon_min,lon_sec,EW,lat_deg,lat_min,lat_sec,NS,slack,note\n"起点,甲",118,0,0,E,22,0,0,N,1.5,"中文,备注"\n终点,118,1,0,E,22,1,0,N,2,终点\n'
    return [{"template":validate_template(fixed),"text":text},{"template":validate_template(split),"text":csv_text}]
