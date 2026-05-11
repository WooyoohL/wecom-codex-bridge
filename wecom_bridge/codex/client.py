import itertools
import json
import os
import queue
import re
import subprocess
import threading
import time
from typing import Any, Callable

from wecom_bridge.config import Config
from wecom_bridge.codex.formatters import (
    format_diff_summary,
    format_history_items,
    format_item_progress,
    format_plan_update,
    format_status_value,
    format_timestamp,
    tail_text,
)


class CodexAppServerClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.workdir = self._read_state_workdir() or config.codex_workdir or os.getcwd()
        if not os.path.isdir(self.workdir):
            print(f"saved Codex workdir does not exist, falling back: {self.workdir}", flush=True)
            self.workdir = config.codex_workdir or os.getcwd()
        self.process = subprocess.Popen(
            ["codex", "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=self.workdir or None,
        )
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._turn_state_lock = threading.Lock()
        self._responses: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._notifications: queue.Queue[dict[str, Any]] = queue.Queue()
        self._current_turn_ids: dict[str, str] = {}
        self.thread_id = config.codex_remote_thread_id or self._read_state_thread_id()
        self.model_override: str | None = self._read_state_model_override()
        self.reasoning_effort_override: str | None = self._read_state_reasoning_effort()
        self._thread_loaded = False
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "wecom-bridge",
                    "title": "WeCom Bridge",
                    "version": "0.1",
                },
                "capabilities": {"experimentalApi": True},
            },
            timeout=15,
        )

    def list_threads(self, limit: int = 5) -> list[dict[str, Any]]:
        response = self.request(
            "thread/list",
            {"limit": limit, "useStateDbOnly": True},
            timeout=20,
        )
        return list(response.get("data", []))

    def read_thread(self) -> dict[str, Any]:
        thread_id = self._ensure_thread()
        response = self.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": False},
            timeout=20,
        )
        return dict(response.get("thread", {}))

    def compact_thread(self) -> str:
        thread_id = self._ensure_thread()
        self.request(
            "thread/compact/start",
            {"threadId": thread_id},
            timeout=30,
        )
        return thread_id

    def get_goal(self) -> dict[str, Any] | None:
        thread_id = self._ensure_thread()
        response = self.request(
            "thread/goal/get",
            {"threadId": thread_id},
            timeout=20,
        )
        goal = response.get("goal")
        return dict(goal) if isinstance(goal, dict) else None

    def set_goal(self, objective: str) -> dict[str, Any]:
        thread_id = self._ensure_thread()
        response = self.request(
            "thread/goal/set",
            {"threadId": thread_id, "objective": objective},
            timeout=20,
        )
        goal = response.get("goal")
        return dict(goal) if isinstance(goal, dict) else {}

    def clear_goal(self) -> bool:
        thread_id = self._ensure_thread()
        response = self.request(
            "thread/goal/clear",
            {"threadId": thread_id},
            timeout=20,
        )
        return bool(response.get("cleared"))

    def list_models(self, limit: int = 20) -> list[dict[str, Any]]:
        response = self.request(
            "model/list",
            {"limit": limit, "includeHidden": False},
            timeout=20,
        )
        return list(response.get("data", []))

    def recent_thread_history(self, limit: int = 3) -> str:
        thread_id = self._ensure_thread()
        limit = max(1, min(limit, 10))
        response = self.request(
            "thread/turns/list",
            {
                "threadId": thread_id,
                "limit": limit,
                "sortDirection": "desc",
                "itemsView": "full",
            },
            timeout=30,
        )
        turns = list(response.get("data", []))
        if not turns:
            return "(no recent Codex turns)"

        lines: list[str] = []
        for turn in reversed(turns):
            turn_id = turn.get("id") or ""
            status = format_status_value(turn.get("status"))
            started_at = format_timestamp(turn.get("startedAt"))
            lines.append(f"[turn {turn_id} {started_at} status={status}]".strip())

            items = list(turn.get("items") or [])
            if not items:
                items = self._list_turn_items(thread_id, str(turn_id), limit=30)
            item_lines = format_history_items(items)
            lines.append(item_lines or "(no persisted items)")
        return "\n\n".join(lines)

    def _list_turn_items(self, thread_id: str, turn_id: str, limit: int = 30) -> list[dict[str, Any]]:
        response = self.request(
            "thread/turns/items/list",
            {
                "threadId": thread_id,
                "turnId": turn_id,
                "limit": limit,
                "sortDirection": "asc",
            },
            timeout=30,
        )
        return list(response.get("data", []))

    def new_thread(self) -> str:
        previous_thread_id = self.thread_id
        previous_loaded = self._thread_loaded
        self.thread_id = None
        self._thread_loaded = False
        try:
            return self._ensure_thread()
        except Exception:
            self.thread_id = previous_thread_id
            self._thread_loaded = previous_loaded
            raise

    def set_workdir(self, path: str) -> str:
        previous_workdir = self.workdir
        previous_thread_id = self.thread_id
        previous_loaded = self._thread_loaded
        self.workdir = path
        self.thread_id = None
        self._thread_loaded = False
        try:
            thread_id = self._ensure_thread()
        except Exception:
            self.workdir = previous_workdir
            self.thread_id = previous_thread_id
            self._thread_loaded = previous_loaded
            raise
        self._write_state_workdir(path)
        return thread_id

    def reset_workdir(self) -> str:
        previous_workdir = self.workdir
        previous_thread_id = self.thread_id
        previous_loaded = self._thread_loaded
        self.workdir = self.config.codex_workdir or os.getcwd()
        self.thread_id = None
        self._thread_loaded = False
        try:
            thread_id = self._ensure_thread()
        except Exception:
            self.workdir = previous_workdir
            self.thread_id = previous_thread_id
            self._thread_loaded = previous_loaded
            raise
        self._delete_state_workdir()
        return thread_id

    def bind(self, thread_id: str) -> None:
        previous_thread_id = self.thread_id
        previous_loaded = self._thread_loaded
        self.thread_id = thread_id
        self._thread_loaded = False
        try:
            self._ensure_thread()
        except Exception:
            self.thread_id = previous_thread_id
            self._thread_loaded = previous_loaded
            raise
        else:
            self._write_state_thread_id(thread_id)

    def fork_thread(self) -> str:
        source_thread_id = self._ensure_thread()
        response = self.request(
            "thread/fork",
            {"threadId": source_thread_id},
            timeout=60,
        )
        forked_thread_id = self._extract_thread_id(response)
        if not forked_thread_id:
            raise RuntimeError(f"thread/fork did not return a thread id: {response}")
        self.thread_id = forked_thread_id
        self._thread_loaded = True
        self._write_state_thread_id(forked_thread_id)
        return forked_thread_id

    def rename_thread(self, name: str) -> str:
        thread_id = self._ensure_thread()
        self.request(
            "thread/name/set",
            {"threadId": thread_id, "name": name},
            timeout=30,
        )
        return thread_id

    def archive_thread(self) -> str:
        thread_id = self._ensure_thread()
        self.request(
            "thread/archive",
            {"threadId": thread_id},
            timeout=30,
        )
        self._clear_thread_binding()
        return thread_id

    def unarchive_thread(self, thread_id: str) -> str:
        self.request(
            "thread/unarchive",
            {"threadId": thread_id},
            timeout=30,
        )
        self.bind(thread_id)
        return thread_id

    def rollback_thread(self, num_turns: int) -> str:
        thread_id = self._ensure_thread()
        response = self.request(
            "thread/rollback",
            {"threadId": thread_id, "numTurns": num_turns},
            timeout=60,
        )
        return self._extract_thread_id(response) or thread_id

    def start_turn(self, text: str) -> tuple[str, str]:
        thread_id = self._ensure_thread()
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text, "text_elements": []}],
            "approvalPolicy": self.config.codex_remote_approval_policy,
        }
        if self.model_override:
            params["model"] = self.model_override
        if self.reasoning_effort_override:
            params["effort"] = self.reasoning_effort_override
        response = self.request("turn/start", params, timeout=30)
        turn_id = response["turn"]["id"]
        self._set_current_turn_id(thread_id, turn_id)
        return thread_id, turn_id

    def set_model_override(self, model: str | None) -> None:
        self.model_override = model
        self._write_state_model_override(model)

    def set_reasoning_effort_override(self, effort: str | None) -> None:
        self.reasoning_effort_override = effort
        self._write_state_reasoning_effort(effort)

    def steer_turn(self, thread_id: str, turn_id: str, text: str) -> str:
        response = self.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": text, "text_elements": []}],
            },
            timeout=30,
        )
        next_turn_id = response.get("turnId", turn_id)
        self._set_current_turn_id(thread_id, next_turn_id)
        return next_turn_id

    def interrupt_turn(self, thread_id: str, turn_id: str) -> str:
        current_turn_id = self._get_current_turn_id(thread_id) or turn_id
        try:
            self.request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": current_turn_id},
                timeout=30,
            )
            return current_turn_id
        except RuntimeError as exc:
            actual_turn_id = self._parse_actual_active_turn_id(str(exc))
            if not actual_turn_id or actual_turn_id == current_turn_id:
                raise
            self._set_current_turn_id(thread_id, actual_turn_id)
            self.request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": actual_turn_id},
                timeout=30,
            )
            return actual_turn_id

    def wait_for_turn(
        self,
        thread_id: str,
        turn_id: str,
        on_message: Callable[[str], None] | None = None,
    ) -> str:
        return self._wait_for_turn(
            thread_id,
            turn_id,
            self.config.codex_timeout_seconds,
            on_message=on_message,
        )

    def send_turn(self, text: str, on_message: Callable[[str], None] | None = None) -> str:
        thread_id, turn_id = self.start_turn(text)
        return self.wait_for_turn(thread_id, turn_id, on_message=on_message)

    def request(self, method: str, params: dict[str, Any] | None, timeout: int) -> dict[str, Any]:
        if self.process.poll() is not None:
            raise RuntimeError("codex app-server is not running")
        request_id = next(self._ids)
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._lock:
            self._responses[request_id] = response_queue
            assert self.process.stdin is not None
            self.process.stdin.write(
                json.dumps({"id": request_id, "method": method, "params": params}, ensure_ascii=False)
                + "\n"
            )
            self.process.stdin.flush()
        try:
            response = response_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(f"app-server request timed out: {method}") from exc
        finally:
            self._responses.pop(request_id, None)
        if "error" in response:
            error = response["error"]
            raise RuntimeError(f"{method} failed: {error.get('message', error)}")
        return response.get("result", {})

    def _ensure_thread(self) -> str:
        if self.thread_id and self._thread_loaded:
            return self.thread_id
        if self.thread_id:
            self.request(
                "thread/resume",
                {
                    "threadId": self.thread_id,
                    "approvalPolicy": self.config.codex_remote_approval_policy,
                    "sandbox": self.config.codex_remote_sandbox,
                    "persistExtendedHistory": False,
                },
                timeout=60,
            )
            self._thread_loaded = True
            return self.thread_id

        params: dict[str, Any] = {
            "cwd": self.workdir,
            "approvalPolicy": self.config.codex_remote_approval_policy,
            "sandbox": self.config.codex_remote_sandbox,
            "experimentalRawEvents": False,
            "persistExtendedHistory": False,
        }
        if self.model_override:
            params["model"] = self.model_override
        response = self.request("thread/start", params, timeout=60)
        self.thread_id = response["thread"]["id"]
        self._thread_loaded = True
        self._write_state_thread_id(self.thread_id)
        print(f"codex remote thread started: {self.thread_id}", flush=True)
        return self.thread_id

    def _clear_thread_binding(self) -> None:
        self.thread_id = None
        self._thread_loaded = False
        self._delete_state_thread_id()

    def _extract_thread_id(self, response: dict[str, Any]) -> str | None:
        for key in ("thread", "data"):
            value = response.get(key)
            if isinstance(value, dict):
                thread_id = value.get("id") or value.get("threadId")
                if thread_id:
                    return str(thread_id)
        for key in ("threadId", "id"):
            value = response.get(key)
            if value:
                return str(value)
        return None

    def _set_current_turn_id(self, thread_id: str, turn_id: str) -> None:
        self._ensure_turn_state()
        with self._turn_state_lock:
            self._current_turn_ids[thread_id] = turn_id

    def _get_current_turn_id(self, thread_id: str) -> str | None:
        self._ensure_turn_state()
        with self._turn_state_lock:
            return self._current_turn_ids.get(thread_id)

    def _clear_current_turn_id(self, thread_id: str, turn_id: str) -> None:
        self._ensure_turn_state()
        with self._turn_state_lock:
            if self._current_turn_ids.get(thread_id) == turn_id:
                self._current_turn_ids.pop(thread_id, None)

    def _ensure_turn_state(self) -> None:
        if not hasattr(self, "_turn_state_lock"):
            self._turn_state_lock = threading.Lock()
        if not hasattr(self, "_current_turn_ids"):
            self._current_turn_ids = {}

    def _parse_actual_active_turn_id(self, message: str) -> str | None:
        match = re.search(r"but found ([0-9a-fA-F-]{36}|[0-9a-fA-F-]{8,})", message)
        return match.group(1) if match else None

    def _wait_for_turn(
        self,
        thread_id: str,
        turn_id: str,
        timeout: int,
        *,
        on_message: Callable[[str], None] | None,
    ) -> str:
        chunks: list[str] = []
        completed_text = ""
        sent_progress_item_ids: set[str] = set()
        last_plan_text = ""
        latest_diff = ""
        idle_deadline = time.time() + timeout
        last_active_check = 0.0
        if not self._get_current_turn_id(thread_id):
            self._set_current_turn_id(thread_id, turn_id)
        while True:
            remaining = idle_deadline - time.time()
            if remaining <= 0:
                last_active_check = time.time()
                if self._thread_is_active(thread_id):
                    idle_deadline = last_active_check + timeout
                    continue
                raise TimeoutError(
                    "Codex remote turn idle timed out after "
                    f"{timeout} seconds without events or active thread status"
                )
            try:
                notification = self._notifications.get(timeout=min(1.0, remaining))
            except queue.Empty:
                now = time.time()
                if now - last_active_check >= 30:
                    last_active_check = now
                    if self._thread_is_active(thread_id):
                        idle_deadline = now + timeout
                continue
            method = notification.get("method")
            params = notification.get("params") or {}
            if params.get("threadId") != thread_id:
                continue
            notification_turn_id = self._notification_turn_id(params)
            current_turn_id = self._get_current_turn_id(thread_id) or turn_id
            accepted_turn_ids = {turn_id, current_turn_id}
            if method == "turn/started" and notification_turn_id:
                self._set_current_turn_id(thread_id, notification_turn_id)
                current_turn_id = notification_turn_id
                accepted_turn_ids.add(notification_turn_id)
            if notification_turn_id and notification_turn_id not in accepted_turn_ids:
                continue
            idle_deadline = time.time() + timeout
            if method == "item/agentMessage/delta":
                chunks.append(params.get("delta", ""))
            elif method == "turn/plan/updated":
                text = format_plan_update(params)
                if text and text != last_plan_text and on_message:
                    last_plan_text = text
                    on_message(text)
            elif method == "turn/diff/updated":
                latest_diff = params.get("diff") or latest_diff
            elif method == "model/rerouted":
                if on_message:
                    on_message(
                        "[模型切换]\n"
                        f"{params.get('fromModel', '')} -> {params.get('toModel', '')}\n"
                        f"reason={params.get('reason', '')}"
                    )
            elif method in ("warning", "guardianWarning"):
                message = params.get("message")
                if message and on_message:
                    on_message(f"[警告]\n{message}")
            elif method == "item/started":
                item = params.get("item") or {}
                text = None
                if self._bridge_option("bridge_forward_tool_progress", False):
                    text = format_item_progress(item, sent_progress_item_ids)
                if text and on_message:
                    on_message(text)
            elif method == "item/completed":
                item = params.get("item") or {}
                text = self._format_completed_progress(item, sent_progress_item_ids)
                if text and on_message:
                    on_message(text)
                if item.get("type") == "agentMessage" and item.get("text"):
                    text = item["text"]
                    if item.get("phase") == "commentary":
                        summary = self._format_thought_summary(text)
                        if summary and on_message:
                            on_message(summary)
                    else:
                        completed_text = text
            elif method == "error":
                error = params.get("error") or {}
                return f"Codex error: {error}"
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                completed_turn_id = str(turn.get("id") or notification_turn_id or "")
                current_turn_id = self._get_current_turn_id(thread_id) or turn_id
                if completed_turn_id and completed_turn_id != current_turn_id:
                    continue
                if turn.get("status") == "failed":
                    self._clear_current_turn_id(thread_id, current_turn_id)
                    return f"Codex turn failed: {turn.get('error')}"
                diff_summary = (
                    format_diff_summary(latest_diff)
                    if self._bridge_option("bridge_forward_file_changes", False)
                    else None
                )
                if diff_summary and on_message:
                    on_message(diff_summary)
                self._clear_current_turn_id(thread_id, current_turn_id)
                return completed_text or "".join(chunks).strip() or "(Codex completed with no output)"

    def _format_completed_progress(
        self,
        item: dict[str, Any],
        sent_progress_item_ids: set[str],
    ) -> str | None:
        item_type = item.get("type")
        if item_type == "fileChange" and not self._bridge_option("bridge_forward_file_changes", False):
            return None
        if self._bridge_option("bridge_forward_tool_progress", False):
            return format_item_progress(item, sent_progress_item_ids)
        if item_type == "commandExecution":
            status = item.get("status")
            exit_code = item.get("exitCode")
            if status in ("failed", "error") or (exit_code not in (None, 0)):
                return format_item_progress(item, sent_progress_item_ids)
        if item_type in ("mcpToolCall", "dynamicToolCall") and item.get("status") == "failed":
            return format_item_progress(item, sent_progress_item_ids)
        return None

    def _format_thought_summary(self, text: str) -> str | None:
        if not self._bridge_option("bridge_forward_thought_summary", True):
            return None
        stripped = text.strip()
        if not stripped:
            return None
        return "[思考摘要]\n" + tail_text(stripped, 1200)

    def _bridge_option(self, name: str, default: bool) -> bool:
        config = getattr(self, "config", None)
        if config is None:
            return default
        return bool(getattr(config, name, default))

    def _notification_turn_id(self, params: dict[str, Any]) -> str | None:
        turn_id = params.get("turnId")
        if turn_id:
            return str(turn_id)
        turn = params.get("turn")
        if isinstance(turn, dict) and turn.get("id"):
            return str(turn["id"])
        return None

    def _thread_is_active(self, thread_id: str) -> bool:
        try:
            response = self.request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": False},
                timeout=10,
            )
        except Exception as exc:
            print(f"failed to poll thread status: {exc}", flush=True)
            return False
        thread = response.get("thread") or {}
        status = thread.get("status") or {}
        if isinstance(status, dict):
            return status.get("type") == "active"
        return status == "active"

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                print(f"codex app-server non-json stdout: {line}", flush=True)
                continue
            request_id = message.get("id")
            if request_id in self._responses:
                self._responses[request_id].put(message)
            else:
                self._notifications.put(message)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            line = line.strip()
            if line:
                print(f"codex app-server stderr: {line}", flush=True)

    def _read_state_thread_id(self) -> str | None:
        path = self.config.codex_remote_state_file
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = handle.read().strip()
                return value or None
        except OSError:
            return None

    def _write_state_thread_id(self, thread_id: str) -> None:
        path = self.config.codex_remote_state_file
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(thread_id + "\n")
        except OSError as exc:
            print(f"failed to write remote thread id: {exc}", flush=True)

    def _delete_state_thread_id(self) -> None:
        try:
            os.remove(self.config.codex_remote_state_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"failed to delete remote thread id: {exc}", flush=True)

    def _read_state_workdir(self) -> str | None:
        path = self.config.codex_remote_workdir_state_file
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = handle.read().strip()
                return value or None
        except OSError:
            return None

    def _write_state_workdir(self, path_value: str) -> None:
        path = self.config.codex_remote_workdir_state_file
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(path_value + "\n")
        except OSError as exc:
            print(f"failed to write remote workdir override: {exc}", flush=True)

    def _delete_state_workdir(self) -> None:
        try:
            os.remove(self.config.codex_remote_workdir_state_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"failed to delete remote workdir override: {exc}", flush=True)

    def _read_state_model_override(self) -> str | None:
        path = self.config.codex_remote_model_state_file
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = handle.read().strip()
                return value or None
        except OSError:
            return None

    def _write_state_model_override(self, model: str | None) -> None:
        path = self.config.codex_remote_model_state_file
        try:
            if model:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(model + "\n")
            else:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
        except OSError as exc:
            print(f"failed to write remote model override: {exc}", flush=True)

    def _read_state_reasoning_effort(self) -> str | None:
        path = self.config.codex_remote_reasoning_state_file
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = handle.read().strip()
                return value or None
        except OSError:
            return None

    def _write_state_reasoning_effort(self, effort: str | None) -> None:
        path = self.config.codex_remote_reasoning_state_file
        try:
            if effort:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(effort + "\n")
            else:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
        except OSError as exc:
            print(f"failed to write remote reasoning effort override: {exc}", flush=True)
