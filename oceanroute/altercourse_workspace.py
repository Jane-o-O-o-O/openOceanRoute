"""Read-only altercourse candidates admitted through complete workspace rules."""
from __future__ import annotations

from copy import deepcopy
import json

from .workspace import _materialize, _validate, workspace_action


def preview_altercourse_workspace(workspace: dict, path_id: str, kind: str, config: dict) -> dict:
    """Return an unsaved complete candidate, retaining normal inventory policy.

    Flexible cable quantities may change. Shared manufacturing inventories still
    require the existing explicit handling rather than a silent mode conversion.
    The caller applies only the selected candidate path to its current document.
    """
    if kind not in {"split", "radius"}:
        raise ValueError("ALTERCOURSE_KIND: kind须为split或radius")
    if not isinstance(path_id, str) or not path_id:
        raise ValueError("ALTERCOURSE_PATH: path_id须为实际路径ID")
    if not isinstance(config, dict):
        raise ValueError("ALTERCOURSE_CONFIG: config须为对象")
    source, _, _ = _validate(workspace)
    path = next((item for item in source["paths"] if item["id"] == path_id), None)
    if path is None:
        raise ValueError("ALTERCOURSE_PATH: 指定路径不存在")
    from .altercourse import radius_altercourse, split_altercourse
    function = split_altercourse if kind == "split" else radius_altercourse
    tool = function(_materialize(source, path), deepcopy(config))
    if tool["project"].get("id") != path_id:
        raise ValueError("ALTERCOURSE_PATH: 工具结果路径身份改变")
    candidate = workspace_action(source, {
        "action": "update_path", "path_id": path_id,
        "project": tool["project"], "assembly_policy": "auto_exclusive",
    })
    report = deepcopy(tool["report"])
    selection = report["result_selection_point_id"]
    if selection not in {point["id"] for point in tool["project"]["route"]["points"]}:
        raise ValueError("ALTERCOURSE_SELECTION: 结果选点不属于实际候选路线")
    candidate["tool_report"] = report
    candidate["operation"] = {"kind": kind, "path_id": path_id, "config": deepcopy(report["config"])}
    candidate["result_selection_point_id"] = selection
    candidate["warnings"] = deepcopy(tool.get("warnings", [])) + candidate["warnings"]
    candidate["scope"] = "read-only complete workspace candidate; apply one path to the current document; not saved"
    size = len(json.dumps(candidate, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
    if size > 64 * 1024**2:
        raise ValueError("ALTERCOURSE_HTTP_OUTPUT_LIMIT: 完整转角候选超过64MiB，不截断")
    return candidate
