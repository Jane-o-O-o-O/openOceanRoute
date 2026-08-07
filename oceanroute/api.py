"""Local HTTP application, project lifecycle and engineering exchange."""

from __future__ import annotations

from copy import deepcopy
from contextlib import asynccontextmanager
import json
import logging
import math
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .storage import ProjectStore
from . import exchange

ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger(__name__)


def _project(payload: dict) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("route"), dict):
        raise ValueError("工程需要 route 对象")
    points = payload["route"].get("points")
    if not isinstance(points, list) or not 2 <= len(points) <= 10_000:
        raise ValueError("路线必须包含 2 到 10,000 个点")
    if payload.get("schema_version", 1) != 1:
        raise ValueError("不支持的工程格式版本")
    if payload.get("crs", "EPSG:4326") != "EPSG:4326":
        raise ValueError("当前工程内核使用 WGS84 经纬度，请先转换输入坐标")
    json.dumps(payload, allow_nan=False)
    return payload


def _analysis(project: dict) -> dict:
    from .core import analyze_project
    return analyze_project(_project(project))


def _inherit_revision(source: dict, result: dict) -> dict:
    """Same-project tools retain optimistic lock; new branches keep new identity."""
    candidate = result.get("project", result)
    if isinstance(candidate, dict) and candidate.get("id") == source.get("id") and "saved_revision" in source:
        candidate["saved_revision"] = source["saved_revision"]
    return result


def create_app(store: ProjectStore | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifecycle(application):
        application.state.voyage_jobs.start()
        try:
            yield
        finally:
            application.state.voyage_jobs.close()
    app = FastAPI(title="OceanRoute Local API", version=__version__, description="独立海缆规划与研究仿真", lifespan=lifecycle)
    app.state.store = store or ProjectStore()
    from .workspace_storage import WorkspaceStore
    app.state.workspace_store = WorkspaceStore(app.state.store.path)
    from .voyage_jobs import VoyageJobs
    app.state.voyage_jobs = VoyageJobs(Path(app.state.store.path).parent/"voyage_jobs", lazy=True)

    @app.exception_handler(ValueError)
    async def bad_value(request: Request, exc: ValueError):
        content = {"detail": str(exc)}
        if getattr(exc, "code", None):
            content["code"] = exc.code
        return JSONResponse(status_code=422, content=content)

    @app.exception_handler(KeyError)
    async def not_found(request: Request, exc: KeyError):
        return JSONResponse(status_code=404, content={"detail": "工程或修订不存在"})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": "请求格式不正确，请检查输入字段", "errors": str(exc)})

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__, "product": "OceanRoute"}

    @app.get("/api/sample")
    def sample():
        from .core import sample_project
        project = sample_project()
        project["id"] = str(uuid4())
        return project

    @app.get("/api/capabilities")
    def capabilities():
        return {"version": __version__, "model_status": "research", "items": [
            {"id": "route", "name": "路线规划 / RPL / SLD", "status": "implemented", "note": "独立实现，具体精度见模型说明与测试"},
            {"id": "projects", "name": "工程保存 / 修订恢复", "status": "implemented", "note": "本地 SQLite 事务与历史快照"},
            {"id": "workspace", "name": "多路径工程 / 共享制造装配", "status": "implemented", "note": "schema2路径、共享缆库、库存与互斥替代关系；按唯一实物核算采购"},
            {"id": "exchange", "name": "开放格式交换", "status": "implemented", "note": "CSV、GeoJSON、KML、DXF、SVG、HTML 报告"},
            {"id": "tools", "name": "分缆 / 余缆模板 / 拆分合并", "status": "implemented", "note": "实际工程变换、制造量与有限附属体同步"},
            {"id": "constraints", "name": "Rigid / Clamped / Sliding 域约束", "status": "implemented", "note": "独立显式Path Link域，冲突拒绝；不支持的变换需重新配置"},
            {"id": "assembly", "name": "制造清单 / 实制装配回写", "status": "implemented", "note": "独立CSV、明确映射预览，非原厂装配格式"},
            {"id": "routing", "name": "避让 / 地形路由搜索", "status": "implemented", "note": "有限网格A*候选，明确工作量上限及地形来源"},
            {"id": "terrain", "name": "XYZ / GeoTIFF / Surfer 与 DTM", "status": "implemented", "note": "沿线采样、网格、坡向阴影、等深线；有规模上限"},
            {"id": "simulation", "name": "稳态 / 动态 / 跨距模型", "status": "research", "note": "独立数值模型；适用假设与局限随结果输出"},
            {"id": "voyage", "name": "长时连续计算 / 后台恢复", "status": "research", "note": "真实状态分块延续与有误差约束的平床网格粗化；并非已验证全航程模型"},
            {"id": "materials", "name": "混合缆型 / 有限附属体", "status": "research", "note": "材料坐标的局部物性与平移分布载荷，非完整刚体六自由度"},
            {"id": "shipplan", "name": "ShipPlan / Look Ahead / 张力搜索", "status": "research", "note": "真实模型生成初始指令及候选，未通过原厂或海试对照"},
            {"id": "sea", "name": "海况谱 / 用户RAO / Monte Carlo", "status": "research", "note": "实际波面、垂向运动与重复动力求解，非完整疲劳或6DOF"},
            {"id": "survey", "name": "实敷观测 / 规划对账", "status": "research", "note": "实际路线最近KP、横偏和连续观测量；基准与实物KP需明确对齐"},
            {"id": "repair", "name": "回收 / 拖曳 / 抓缆绳 / 浮标", "status": "research", "note": "独立准静态/稳态工具，非钩挂动力或完整浮标6DOF"},
            {"id": "seismic", "name": "地震缆应答器 / 海流反算", "status": "research", "note": "真实稳态缆形观测算子、加权最小二乘、可观测性与局部协方差；非实时动态滤波"},
            {"id": "native", "name": "原厂专有格式", "status": "unverified", "note": "缺少公开格式规范与往返基准"},
            {"id": "instrument", "name": "船载设备与实时控制", "status": "planned", "note": "另需设备协议和现场验证"},
        ]}

    @app.post("/api/analyze")
    def analyze(payload: dict):
        return _analysis(payload)

    @app.get("/api/projects")
    def projects():
        return app.state.store.list()

    @app.post("/api/projects")
    def save_project(payload: dict):
        _analysis(payload)
        return app.state.store.save(payload)

    @app.get("/api/projects/{project_id}")
    def open_project(project_id: str):
        return app.state.store.get(project_id)

    @app.get("/api/projects/{project_id}/revisions")
    def revisions(project_id: str):
        return app.state.store.revisions(project_id)

    @app.post("/api/projects/{project_id}/restore/{revision}")
    def restore(project_id: str, revision: int):
        return app.state.store.restore(project_id, revision)

    @app.post("/api/workspace/migrate")
    def migrate_workspace(payload: dict):
        from .workspace import migrate_project
        return migrate_project(_project(payload.get("project", {})), payload.get("config", {}))

    @app.post("/api/workspace/action")
    def workspace_action(payload: dict):
        from .workspace import workspace_action as action
        return action(payload.get("workspace", {}), payload.get("config", {}))

    @app.post("/api/workspace/analyze")
    def analyze_workspace(payload: dict):
        from .workspace import analyze_workspace as analyze
        return analyze(payload)

    @app.post("/api/workspace/import")
    def import_workspace(payload: dict):
        from .workspace import import_workspace as read
        return read(payload.get("text", ""), payload.get("config", {}))

    @app.post("/api/workspace/export")
    def export_workspace(payload: dict):
        from .workspace import export_workspace as write
        return Response(write(payload), media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="workspace.oceanroute.json"'})

    @app.get("/api/workspaces")
    def workspaces():
        return app.state.workspace_store.list()

    @app.post("/api/workspaces")
    def save_workspace(payload: dict):
        return app.state.workspace_store.save(payload)

    @app.get("/api/workspaces/{workspace_id}")
    def open_workspace(workspace_id: str):
        return app.state.workspace_store.get(workspace_id)

    @app.get("/api/workspaces/{workspace_id}/revisions")
    def workspace_revisions(workspace_id: str):
        return app.state.workspace_store.revisions(workspace_id)

    @app.post("/api/workspaces/{workspace_id}/restore/{revision}")
    def restore_workspace(workspace_id: str, revision: int, payload: dict):
        return app.state.workspace_store.restore(workspace_id, revision,
                                                  expected_revision=payload.get("expected_revision"))

    @app.post("/api/import/rpl")
    def import_rpl(payload: dict):
        return exchange.import_rpl(str(payload.get("text", "")), payload.get("delimiter"), payload.get("mapping"))

    @app.post("/api/import/profile")
    def import_profile(payload: dict):
        result = exchange.import_profile(str(payload.get("text", "")), payload.get("delimiter"))
        if payload.get("project"):
            from .core import route_signature
            project = deepcopy(_project(payload["project"]))
            project["profile"] = {"samples": result["samples"], "route_signature": route_signature(project), "source": "用户剖面文本"}
            result["project"] = project
        return result

    @app.post("/api/profile/attach")
    def attach_profile(payload: dict):
        from .core import route_signature
        project = deepcopy(_project(payload.get("project", {})))
        samples = payload.get("samples", [])
        if not isinstance(samples, list) or len(samples) < 2:
            raise ValueError("至少需要两个剖面采样点")
        project["profile"] = {"samples": samples, "route_signature": route_signature(project), "source": payload.get("source", "用户剖面文本")}
        _analysis(project)
        return project

    @app.post("/api/terrain/xyz")
    def terrain_xyz(payload: dict):
        from .gis import profile_from_xyz
        return profile_from_xyz(_project(payload.get("project", {})), str(payload.get("text", "")),
                                spacing_m=payload.get("spacing_m", 1000), max_gap_m=payload.get("max_gap_m", 5000),
                                method=payload.get("method", "linear"), allow_extrapolation=bool(payload.get("allow_extrapolation", False)),
                                depth_positive=payload.get("depth_positive", "down"), depth_units=payload.get("depth_units", "m"), vertical_datum=payload.get("vertical_datum", "user-unspecified"))

    @app.post("/api/terrain/geotiff")
    async def terrain_geotiff(file: UploadFile = File(...), project_json: str = Form(...), spacing_m: float = Form(1000),
                              depth_positive: str = Form("down"), vertical_datum: str = Form("user-unspecified"), depth_units: str = Form("m")):
        from .gis import profile_from_geotiff
        import asyncio
        data = await file.read(128 * 1024 * 1024 + 1)
        project = _project(json.loads(project_json))
        return await asyncio.to_thread(profile_from_geotiff, project, data, spacing_m=spacing_m,
                                       depth_positive=depth_positive, vertical_datum=vertical_datum, depth_units=depth_units)

    @app.post("/api/terrain/surfer")
    async def terrain_surfer(file: UploadFile = File(...), project_json: str = Form(...), source_crs: str = Form(...),
                             spacing_m: float = Form(1000), depth_positive: str = Form("down"), vertical_datum: str = Form("user-unspecified"), method: str = Form("linear"), depth_units: str = Form("m")):
        from .surfer import profile_from_surfer
        import asyncio
        data = await file.read(128 * 1024 * 1024 + 1)
        return await asyncio.to_thread(profile_from_surfer, _project(json.loads(project_json)), data, source_crs=source_crs,
                                       spacing_m=spacing_m, depth_positive=depth_positive, vertical_datum=vertical_datum, method=method, depth_units=depth_units)

    @app.post("/api/dtm/grid")
    def dtm_grid(payload: dict):
        from .dtm import build_dtm
        return build_dtm(str(payload.get("text", "")),payload.get("config",{}))

    @app.post("/api/routing/search")
    def search_route(payload: dict):
        from .routing import search_route
        project = _project(payload.get("project", {}))
        return _inherit_revision(project, search_route(project, payload.get("config", {})))

    @app.post("/api/import/kml")
    def import_kml(payload: dict):
        from .geoformats import import_kml
        return import_kml(str(payload.get("text", "")), str(payload.get("name", "KML图层")), str(payload.get("kind", "reference")))

    @app.post("/api/import/shapefile")
    async def import_shapefile(file: UploadFile = File(...), name: str = Form("Shapefile图层"), kind: str = Form("reference"),
                               crs_override: str = Form(""), encoding: str = Form(""), source_name: str = Form("")):
        from .geoformats import import_shapefile_zip
        import asyncio
        data = await file.read(128 * 1024 * 1024 + 1)
        return await asyncio.to_thread(import_shapefile_zip, data, name=name, kind=kind, crs_override=crs_override,
                                       encoding=encoding, source_name=source_name)

    @app.post("/api/import/geojson")
    def import_geojson(payload: dict):
        return exchange.import_geojson(str(payload.get("text", "")), str(payload.get("name", "导入图层")), str(payload.get("kind", "survey")))

    @app.post("/api/import/project")
    def import_project(payload: dict):
        doc = json.loads(str(payload.get("text", "")))
        _analysis(doc)
        doc.pop("saved_revision", None)
        if doc.get("id"):
            doc["origin_project_id"] = doc["id"]
        doc["id"] = str(uuid4())
        return doc

    @app.post("/api/assembly/import")
    def assembly_import(payload: dict):
        from .assembly import import_assembly
        project = _project(payload.get("project", {}))
        return _inherit_revision(project, import_assembly(project, str(payload.get("text", "")), payload.get("config", {})))

    @app.post("/api/constraints/{kind}")
    def constraints(kind: str, payload: dict):
        from .constraints import configure_constraints, edit_constrained_project, solve_constraints
        functions = {"configure": configure_constraints, "edit": edit_constrained_project, "solve": solve_constraints}
        if kind not in functions:
            raise HTTPException(404, "未知约束工具")
        project = _project(payload.get("project", {}))
        return _inherit_revision(project, functions[kind](project, payload.get("config", {})))

    @app.post("/api/sea/{kind}")
    def sea(kind: str, payload: dict):
        from .sea import generate_sea_state, simulate_sea, monte_carlo
        config = payload.get("config", {})
        if kind == "generate":
            return generate_sea_state(config)
        project = _project(payload.get("project", {}))
        if kind == "simulate":
            return simulate_sea(project, config)
        if kind == "montecarlo":
            return monte_carlo(project, config)
        raise HTTPException(404, "未知海况研究工具")

    @app.post("/api/survey/reconcile")
    def survey_reconcile(payload: dict):
        from .survey import reconcile_survey
        return reconcile_survey(_project(payload.get("project", {})), payload.get("config", {}))

    @app.post("/api/repair/{kind}")
    def repair(kind: str, payload: dict):
        from .repair import recovery_shape, steady_tow, estimate_grapnel_rope, size_buoy
        functions = {"recovery": recovery_shape, "tow": steady_tow, "rope": estimate_grapnel_rope, "buoy": size_buoy}
        if kind not in functions:
            raise HTTPException(404, "未知维修研究工具")
        return functions[kind](payload.get("config", {}))

    @app.post("/api/seismic/{kind}")
    def seismic(kind: str, payload: dict):
        from .seismic import predict_transponders, estimate_current
        functions = {"predict": predict_transponders, "estimate": estimate_current}
        if kind not in functions:
            raise HTTPException(404, "未知地震缆研究工具")
        return functions[kind](payload.get("config", {}))

    @app.post("/api/voyage/run")
    def voyage_run(payload: dict):
        from .voyage import run_voyage
        project = payload.get("project", {})
        if project:
            _project(project)
        result = run_voyage(project, payload.get("config", {}))
        from .voyage import _finite_json
        _finite_json(result, 64_000_000)
        return result

    @app.get("/api/voyage/jobs")
    def voyage_jobs():
        return app.state.voyage_jobs.list()

    @app.post("/api/voyage/jobs")
    def voyage_submit(payload: dict):
        project = payload.get("project", {})
        if project:
            _project(project)
        return app.state.voyage_jobs.submit(project, payload.get("config", {}))

    @app.get("/api/voyage/jobs/{job_id}")
    def voyage_job(job_id: str):
        return app.state.voyage_jobs.get(job_id)

    @app.post("/api/voyage/jobs/{job_id}/cancel")
    def voyage_cancel(job_id: str):
        return app.state.voyage_jobs.cancel(job_id)

    @app.post("/api/voyage/jobs/{job_id}/resume")
    def voyage_resume(job_id: str, payload: dict):
        return app.state.voyage_jobs.resume(job_id, payload)

    @app.get("/api/voyage/jobs/{job_id}/checkpoint")
    def voyage_checkpoint(job_id: str):
        return app.state.voyage_jobs.checkpoint(job_id)

    @app.get("/api/voyage/jobs/{job_id}/result")
    def voyage_result(job_id: str):
        return app.state.voyage_jobs.result(job_id)

    @app.delete("/api/voyage/jobs/{job_id}")
    def voyage_delete(job_id: str):
        return app.state.voyage_jobs.delete(job_id)

    @app.post("/api/export/{format_name}")
    def export(format_name: str, payload: dict):
        analysis = _analysis(payload)
        exporters = {
            "csv": (exchange.export_csv, "text/csv; charset=utf-8", "rpl.csv"),
            "geojson": (exchange.export_geojson, "application/geo+json", "route.geojson"),
            "kml": (exchange.export_kml, "application/vnd.google-earth.kml+xml", "route.kml"),
            "dxf": (exchange.export_dxf, "application/dxf", "route.dxf"),
            "sld": (exchange.export_sld, "image/svg+xml", "assembly.svg"),
            "report": (exchange.export_report, "text/html; charset=utf-8", "report.html"),
        }
        if format_name == "assembly":
            from .assembly import export_assembly
            return Response(export_assembly(payload), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="manufacturing.csv"'})

        if format_name == "project":
            content = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2)
            return Response(content, media_type="application/json", headers={"Content-Disposition": 'attachment; filename="project.oceanroute.json"'})
        if format_name not in exporters:
            raise HTTPException(404, "不支持的导出格式")
        function, mime, filename = exporters[format_name]
        return Response(function(payload, analysis), media_type=mime,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.post("/api/route/reverse")
    def reverse_route(payload: dict):
        from .tools import reverse_project
        project = _project(payload)
        return _inherit_revision(project, reverse_project(project))["project"]

    @app.post("/api/tools/{kind}")
    def engineering_tool(kind: str, payload: dict):
        from .tools import geodetic, subdivide_project, define_cables_by_depth, apply_slack_template, split_project, merge_projects, reverse_project
        config = payload.get("config", {})
        if kind == "geodetic":
            return geodetic(config)
        if kind == "merge":
            projects = payload.get("projects", config.get("projects", []))
            return merge_projects([_project(p) for p in projects], config)
        project = _project(payload.get("project", {}))
        functions = {"subdivide": subdivide_project, "depth-cables": define_cables_by_depth,
                     "slack-template": apply_slack_template, "split": split_project}
        if kind == "reverse":
            return _inherit_revision(project, reverse_project(project))
        if kind not in functions:
            raise HTTPException(404, "未知工程工具")
        return _inherit_revision(project, functions[kind](project, config))

    @app.post("/api/shipplan/{kind}")
    def ship_plan(kind: str, payload: dict):
        from .shipplan import build_ship_plan, look_ahead, optimize_tension
        project = _project(payload.get("project", {}))
        config = payload.get("config", {})
        if kind == "create":
            return build_ship_plan(project, config)
        if kind == "lookahead":
            return look_ahead(project, config, payload.get("scenarios", []))
        if kind == "optimize":
            return optimize_tension(project, config)
        raise HTTPException(404, "未知施工计划工具")

    @app.post("/api/route/split")
    def split_route(payload: dict):
        from .tools import split_project
        return split_project(_project(payload.get("project", {})),{k:v for k,v in payload.items() if k!="project"})

    @app.post("/api/simulation/{kind}")
    def simulation(kind: str, payload: dict):
        from .simulation import catenary, steady_state, simulate_lay, span_analysis
        config = payload.get("config", {})
        project = payload.get("project")
        if project:
            _project(project)
        functions = {"catenary": catenary, "steady": steady_state, "span": span_analysis}
        if kind == "dynamic":
            return simulate_lay(project or {}, config)
        if kind not in functions:
            raise HTTPException(404, "未知仿真类型")
        return functions[kind](config)

    @app.get("/api/manual")
    def manual():
        path = ROOT / "docs" / "USER_MANUAL.md"
        if not path.exists():
            path = Path(__file__).resolve().parent / "manual.md"
        return {"text": path.read_text(encoding="utf-8") if path.exists() else "用户手册正在与实现同步更新。"}

    frontend = ROOT / "web" / "dist"
    if not frontend.exists():
        frontend = Path(__file__).resolve().parent / "static"
    if frontend.exists():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="application")
    else:
        @app.get("/")
        def index():
            return {"product": "OceanRoute", "message": "运行 npm --prefix web run build 后重启服务以加载界面", "api": "/docs"}
    return app


app = create_app()
