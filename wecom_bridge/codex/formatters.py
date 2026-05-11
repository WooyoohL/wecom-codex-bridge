import json
import re
import shlex
import time
from typing import Any


def format_item_progress(item: dict[str, Any], sent_progress_item_ids: set[str]) -> str | None:
    item_type = item.get("type")

    if item_type == "commandExecution":
        command = item.get("command")
        if not command:
            return None

        item_id = str(item.get("id") or item.get("itemId") or command)
        if item_id not in sent_progress_item_ids:
            sent_progress_item_ids.add(item_id)
            return "[直接执行]\n" + summarize_command(command)

        status = item.get("status")
        exit_code = item.get("exitCode")
        output = item.get("aggregatedOutput") or item.get("output") or ""
        if status in ("failed", "error") or (exit_code not in (None, 0)):
            return (
                "[直接执行失败]\n"
                f"{summarize_command(command)}\n"
                f"exit_code={exit_code}\n"
                f"{tail_text(str(output), 1200)}"
            ).strip()
        return None

    item_id = str(item.get("id") or item.get("itemId") or json.dumps(item, sort_keys=True))
    if item_id not in sent_progress_item_ids:
        sent_progress_item_ids.add(item_id)
        if item_type == "webSearch":
            return "[网络搜索]\n" + str(item.get("query") or "").strip()
        if item_type in ("mcpToolCall", "dynamicToolCall"):
            name = format_tool_name(item)
            if item.get("status") == "inProgress":
                return "[工具调用]\n" + name
        if item_type == "fileChange":
            text = format_file_change_item(item)
            if text:
                return text

    if item_type in ("mcpToolCall", "dynamicToolCall") and item.get("status") == "failed":
        error = item.get("error") or ""
        return "[工具调用失败]\n" + format_tool_name(item) + ("\n" + str(error) if error else "")
    if item_type == "fileChange" and item.get("status") in ("failed", "declined"):
        text = format_file_change_item(item)
        return "[文件修改失败]\n" + text.removeprefix("[文件修改]\n") if text else "[文件修改失败]"
    return None


def tail_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return "...(truncated)\n" + text[-limit:]


def summarize_command(command: str) -> str:
    stripped = command.strip()
    if not stripped:
        return "(empty command)"

    if _is_short_single_line_command(stripped):
        return stripped

    first_line = stripped.splitlines()[0].strip()
    program = _first_shell_word(first_line)
    if "\n" in stripped or "<<" in stripped:
        return f"{program} (multi-line command, {len(stripped)} chars)"
    return f"{program} (long command, {len(stripped)} chars)"


def _is_short_single_line_command(command: str) -> bool:
    return "\n" not in command and "<<" not in command and len(command) <= 120


def _first_shell_word(line: str) -> str:
    try:
        parts = shlex.split(line)
    except ValueError:
        parts = line.split()
    if not parts:
        return "shell"
    return parts[0].split("/")[-1] or "shell"


def format_tool_name(item: dict[str, Any]) -> str:
    if item.get("type") == "mcpToolCall":
        return f"{item.get('server', '')}/{item.get('tool', '')}".strip("/")
    namespace = item.get("namespace")
    tool = item.get("tool") or ""
    return f"{namespace}.{tool}" if namespace else str(tool)


def format_file_change_item(item: dict[str, Any]) -> str | None:
    changes = item.get("changes") or []
    lines = []
    for change in changes[:12]:
        kind = change.get("kind") or {}
        kind_type = kind.get("type") if isinstance(kind, dict) else str(kind)
        path = change.get("path") or ""
        move_path = kind.get("move_path") if isinstance(kind, dict) else None
        if move_path:
            lines.append(f"{kind_type} {path} -> {move_path}")
        else:
            lines.append(f"{kind_type} {path}".strip())
    if len(changes) > 12:
        lines.append(f"...还有 {len(changes) - 12} 个文件")
    if not lines:
        return None
    return "[文件修改]\n" + "\n".join(lines)


def format_plan_update(params: dict[str, Any]) -> str | None:
    plan = params.get("plan") or []
    if not plan:
        return None
    labels = {"pending": "待做", "inProgress": "进行中", "completed": "完成"}
    lines = ["[计划更新]"]
    explanation = (params.get("explanation") or "").strip()
    if explanation:
        lines.append(explanation)
    for step in plan[:12]:
        status = labels.get(step.get("status"), step.get("status") or "")
        text = (step.get("step") or "").strip()
        if text:
            lines.append(f"- {status}: {text}")
    if len(plan) > 12:
        lines.append(f"...还有 {len(plan) - 12} 步")
    return "\n".join(lines)


def format_diff_summary(diff: str) -> str | None:
    if not diff.strip():
        return None

    files: dict[str, list[int]] = {}
    current_path: str | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.*?) b/(.*)", line)
            if match:
                current_path = match.group(2)
                files.setdefault(current_path, [0, 0])
            continue
        if line.startswith("+++ ") and current_path is None:
            path = line[4:].strip()
            if path != "/dev/null":
                current_path = path.removeprefix("b/")
                files.setdefault(current_path, [0, 0])
            continue
        if not current_path:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            files[current_path][0] += 1
        elif line.startswith("-") and not line.startswith("---"):
            files[current_path][1] += 1

    if not files:
        return None

    lines = []
    for path, counts in list(files.items())[:12]:
        lines.append(f"{path} (+{counts[0]} -{counts[1]})")
    if len(files) > 12:
        lines.append(f"...还有 {len(files) - 12} 个文件")
    return "[文件变更]\n" + "\n".join(lines)


def format_timestamp(value: Any) -> str:
    if not value:
        return ""
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(value)))
    except (TypeError, ValueError, OSError):
        return str(value)


def format_status_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("type") or value)
    return str(value or "")


def format_history_items(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in items:
        item_type = item.get("type")
        if item_type == "userMessage":
            text = format_user_inputs(item.get("content") or [])
            if text:
                lines.append("user: " + tail_text(text, 800))
        elif item_type == "agentMessage":
            text = (item.get("text") or "").strip()
            if text:
                phase = item.get("phase") or "final"
                lines.append(f"assistant/{phase}: " + tail_text(text, 1200))
        elif item_type == "plan":
            text = (item.get("text") or "").strip()
            if text:
                lines.append("plan: " + tail_text(text, 800))
        elif item_type == "commandExecution":
            command = item.get("command") or ""
            status = item.get("status") or ""
            exit_code = item.get("exitCode")
            if command:
                exit_text = "" if exit_code in (None, "") else f" exit={exit_code}"
                lines.append(f"cmd/{status}{exit_text}: {summarize_command(command)}")
        elif item_type == "fileChange":
            text = format_file_change_item(item)
            if text:
                lines.append(text)
        elif item_type in ("mcpToolCall", "dynamicToolCall"):
            name = format_tool_name(item)
            status = item.get("status") or ""
            if name:
                lines.append(f"tool/{status}: {name}")
    return "\n".join(lines)


def format_user_inputs(inputs: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in inputs:
        item_type = item.get("type")
        if item_type == "text":
            parts.append(str(item.get("text") or ""))
        elif item_type == "image":
            parts.append("[image] " + str(item.get("url") or ""))
        elif item_type == "localImage":
            parts.append("[local image] " + str(item.get("path") or ""))
        elif item_type == "mention":
            name = item.get("name") or ""
            path = item.get("path") or ""
            parts.append(f"[mention] {name} {path}".strip())
        elif item_type == "skill":
            name = item.get("name") or ""
            path = item.get("path") or ""
            parts.append(f"[skill] {name} {path}".strip())
    return "\n".join(part for part in parts if part)
