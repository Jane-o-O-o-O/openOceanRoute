"""Read-only complete-workspace admission for explicit intrinsic-arc edits."""
from copy import deepcopy
import json

from .arc_edit import ArcEditError, edit_arc_project
from .workspace import _materialize, _validate, workspace_action


def preview_arc_edit_workspace(workspace, path_id, config):
    if not isinstance(path_id, str) or not path_id:
        raise ArcEditError("ARC_EDIT_PATH", "path_id须为实际路径ID")
    if not isinstance(config, dict):
        raise ValueError("arc-edit config须为对象")
    source, _, _ = _validate(workspace)
    path = next((p for p in source["paths"] if p["id"] == path_id), None)
    if path is None:
        raise ArcEditError("ARC_EDIT_PATH", "指定路径不存在")
    tool = edit_arc_project(_materialize(source, path), deepcopy(config))
    candidate = workspace_action(source, {"action":"update_path", "path_id":path_id,
        "project":tool["project"], "assembly_policy":"auto_exclusive"})
    candidate["tool_report"] = deepcopy(tool["report"])
    candidate["operation"] = {"kind":"arc_edit", "path_id":path_id, "config":deepcopy(tool["report"]["config"])}
    candidate["result_selection_point_id"] = tool["report"]["result_selection_point_id"]
    candidate["warnings"] = deepcopy(tool["warnings"]) + candidate["warnings"]
    candidate["scope"] = "read-only complete workspace candidate; normal exclusive/shared inventory policy; not saved"
    size = len(json.dumps(candidate,ensure_ascii=False,allow_nan=False,separators=(",", ":")).encode("utf-8"))
    if size > 64*1024**2:
        raise ArcEditError("ARC_EDIT_HTTP_OUTPUT_LIMIT", "完整工作区候选超过64MiB；不截断")
    return candidate
