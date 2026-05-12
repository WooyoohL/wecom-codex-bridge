#!/usr/bin/env python3
import os
import queue
import sys
import threading
import time
from dataclasses import replace as replace_config
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wecom_bridge import (  # noqa: E402
    ActiveTurn,
    CodexAppServerClient,
    Config,
    IncomingMessage,
    MenuOption,
    MessageWorker,
    SlashCommandInfo,
    format_diff_summary,
    format_item_progress,
    split_message,
)


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str | None, str]] = []

    def send_text(self, content: str, to_user: str | None = None) -> None:
        self.sent.append((to_user, content))


class FakeRemote:
    def __init__(self) -> None:
        self.thread_id = "thread-1"
        self.workdir = "/tmp/project-a"
        self.model_override: str | None = None
        self.reasoning_effort_override: str | None = None
        self.started: list[str] = []
        self.steered: list[str] = []
        self.interrupted: list[tuple[str, str]] = []
        self.next_steer_turn_id: str | None = None
        self.bound: list[str] = []
        self.renamed: list[tuple[str, str]] = []
        self.archived: list[str] = []
        self.unarchived: list[str] = []
        self.rolled_back: list[tuple[str, int]] = []
        self.wait_reply = "done"
        self.approvals: list[tuple[object, str, dict[str, object], bool]] = []
        self.goal: dict[str, object] | None = None
        self.threads: list[dict[str, object]] = [
            {"id": "thread-abc123", "cwd": "/tmp/project-a", "preview": "recent work"},
            {"id": "thread-def456", "cwd": "/tmp/project-b", "preview": "other work"},
        ]

    def start_turn(self, text: str) -> tuple[str, str]:
        self.started.append(text)
        return self.thread_id, f"turn-{len(self.started)}"

    def steer_turn(self, thread_id: str, turn_id: str, text: str) -> str:
        self.steered.append(text)
        return self.next_steer_turn_id or turn_id

    def interrupt_turn(self, thread_id: str, turn_id: str) -> str:
        self.interrupted.append((thread_id, turn_id))
        return turn_id

    def wait_for_turn(self, thread_id: str, turn_id: str, on_message=None, on_approval=None) -> str:
        return self.wait_reply

    def respond_to_approval_request(
        self,
        request_id: object,
        method: str,
        params: dict[str, object],
        approved: bool,
    ) -> None:
        self.approvals.append((request_id, method, params, approved))

    def list_models(self, limit: int = 20) -> list[dict[str, object]]:
        return [
            {
                "displayName": "GPT-5.5",
                "model": "gpt-5.5",
                "isDefault": True,
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low", "description": "Fast responses"},
                    {"reasoningEffort": "medium", "description": "Balanced"},
                    {"reasoningEffort": "high", "description": "Deeper reasoning"},
                    {"reasoningEffort": "xhigh", "description": "Deepest reasoning"},
                ],
            },
            {
                "displayName": "gpt-5.4",
                "model": "gpt-5.4",
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low", "description": "Fast responses"},
                    {"reasoningEffort": "medium", "description": "Balanced"},
                    {"reasoningEffort": "high", "description": "Deeper reasoning"},
                ],
            },
        ]

    def set_model_override(self, model: str | None) -> None:
        self.model_override = model

    def set_reasoning_effort_override(self, effort: str | None) -> None:
        self.reasoning_effort_override = effort

    def read_thread(self) -> dict[str, object]:
        return {"cwd": self.workdir, "model": self.model_override or "default", "status": {"type": "idle"}}

    def list_threads(self, limit: int = 8) -> list[dict[str, object]]:
        return self.threads[:limit]

    def new_thread(self) -> str:
        self.thread_id = "thread-new"
        return self.thread_id

    def bind(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.bound.append(thread_id)

    def fork_thread(self) -> str:
        self.thread_id = "thread-fork"
        return self.thread_id

    def rename_thread(self, name: str) -> str:
        self.renamed.append((self.thread_id, name))
        return self.thread_id

    def archive_thread(self) -> str:
        thread_id = self.thread_id
        self.archived.append(thread_id)
        self.thread_id = None
        return thread_id

    def unarchive_thread(self, thread_id: str) -> str:
        self.unarchived.append(thread_id)
        self.thread_id = thread_id
        return thread_id

    def rollback_thread(self, num_turns: int) -> str:
        self.rolled_back.append((self.thread_id, num_turns))
        return self.thread_id

    def set_workdir(self, path: str) -> str:
        self.workdir = path
        self.thread_id = "thread-cd"
        return self.thread_id

    def reset_workdir(self) -> str:
        self.workdir = "/tmp/project-a"
        self.thread_id = "thread-reset"
        return self.thread_id

    def recent_thread_history(self, limit: int = 3) -> str:
        return "user: hello\nassistant/final: hi"

    def get_goal(self) -> dict[str, object] | None:
        return self.goal

    def set_goal(self, objective: str) -> dict[str, object]:
        self.goal = {
            "objective": objective,
            "status": "active",
            "tokensUsed": 0,
            "timeUsedSeconds": 0,
        }
        return self.goal

    def clear_goal(self) -> bool:
        had_goal = self.goal is not None
        self.goal = None
        return had_goal


def make_worker() -> tuple[MessageWorker, FakeSender, FakeRemote]:
    config = Config(
        corp_id="corp",
        corp_secret="secret",
        agent_id=1,
        token="token",
        encoding_aes_key="a" * 43,
        to_user="me",
        host="127.0.0.1",
        port=8000,
        codex_backend="disabled",
        codex_workdir=None,
        codex_timeout_seconds=300,
        codex_remote_thread_id=None,
        codex_remote_state_file=".unused_thread",
        codex_remote_model_state_file=".unused_model",
        codex_remote_reasoning_state_file=".unused_reasoning",
        codex_remote_workdir_state_file=".unused_workdir",
        codex_remote_approval_policy="never",
        codex_remote_sandbox="danger-full-access",
        codex_thread_list_limit=20,
        codex_slash_commands_url="unused",
        codex_slash_commands_cache_seconds=1,
        bridge_command_prefix="!",
        bridge_security_profile="personal",
        allowed_wecom_users=("me",),
        allowed_bridge_commands=(),
        dangerous_commands_require_confirmation=False,
        bridge_audit_log_file="",
        bridge_forward_thought_summary=True,
        bridge_forward_tool_progress=False,
        bridge_forward_file_changes=False,
    )
    sender = FakeSender()
    remote = FakeRemote()
    worker = MessageWorker(config, sender)
    worker.remote_client = remote
    worker.slash_commands._cache = [
        SlashCommandInfo("model", "choose model"),
        SlashCommandInfo("resume", "resume a thread"),
        SlashCommandInfo("goal", "set or view the goal for a long-running task"),
        SlashCommandInfo("status", "show status"),
    ]
    worker.slash_commands._cache_at = time.time()
    return worker, sender, remote


def assert_contains(text: str, expected: str) -> None:
    assert expected in text, f"expected {expected!r} in {text!r}"


def test_menu_model_selection() -> None:
    worker, _, remote = make_worker()
    menu = worker._codex_slash_help()
    assert_contains(menu, "1. /model")
    model_menu = worker._handle_pending_menu("1")
    assert model_menu is not None
    assert_contains(model_menu, "Codex models")
    selected = worker._handle_pending_menu("2")
    assert selected is not None
    assert_contains(selected, "Codex reasoning effort")
    assert remote.model_override is None
    selected = worker._handle_pending_menu("4")
    assert selected is not None
    assert_contains(selected, "model=gpt-5.5")
    assert_contains(selected, "reasoning_effort=xhigh")
    assert remote.model_override == "gpt-5.5"
    assert remote.reasoning_effort_override == "xhigh"
    worker._codex_models()
    cleared = worker._handle_pending_menu("1")
    assert cleared is not None
    assert_contains(cleared, "override cleared")
    assert remote.model_override is None
    assert remote.reasoning_effort_override is None


def test_active_turn_steer_interrupt_continue() -> None:
    worker, _, remote = make_worker()
    reply, _ = worker._handle_remote_text("first", "me")
    assert reply == ""
    assert remote.started == ["first"]

    worker._set_active_turn(ActiveTurn("thread-1", "turn-1", "me", time.time(), "first"))
    remote.next_steer_turn_id = "turn-2"
    steer_reply, _ = worker._handle_remote_text("insert", "me")
    assert_contains(steer_reply, "inserted into current Codex turn")
    assert_contains(steer_reply, "turn=turn-2")
    assert remote.steered == ["insert"]
    assert worker._active_snapshot().turn_id == "turn-2"

    stop_reply = worker._handle_bridge_command("!stop", "me")
    assert isinstance(stop_reply, str)
    assert_contains(stop_reply, "interrupt sent")
    assert_contains(stop_reply, "turn=turn-2")
    assert remote.interrupted[-1] == ("thread-1", "turn-2")
    blocked_reply, _ = worker._handle_remote_text("should wait", "me")
    assert_contains(blocked_reply, "interrupting")

    worker._set_active_turn(None)
    remote.next_steer_turn_id = None
    cont_reply, _ = worker._handle_bridge_command("!continue", "me")
    assert cont_reply == ""
    assert remote.started[-1] == "继续"

    worker._set_active_turn(ActiveTurn("thread-1", "turn-9", "me", time.time(), "running"))
    cont_steer, _ = worker._handle_bridge_command("!continue 补充", "me")
    assert_contains(cont_steer, "inserted into current Codex turn")
    assert remote.steered[-1] == "补充"


def test_interrupted_turn_final_is_normalized() -> None:
    worker, sender, remote = make_worker()
    active = ActiveTurn("thread-1", "turn-stop", "me", time.time(), "running", time.time())
    worker._set_active_turn(active)
    remote.wait_reply = "Codex turn failed: interrupted"

    worker._wait_active_turn(active)

    assert sender.sent[-1] == ("me", "Codex interrupted\nturn=turn-stop")
    assert worker._active_snapshot() is None


def test_tail_does_not_remember_itself() -> None:
    worker, sender, _ = make_worker()
    worker.remote_client = None
    worker.submit(IncomingMessage("me", "text", "!status", "1"))
    worker.queue.join()
    assert_contains(sender.sent[-1][1], "bridge ok")
    assert len(worker.recent_outgoing) == 1

    worker.submit(IncomingMessage("me", "text", "!tail", "2"))
    worker.queue.join()
    assert_contains(sender.sent[-1][1], "bridge ok")
    assert len(worker.recent_outgoing) == 1


def test_last_history_command() -> None:
    worker, _, _ = make_worker()
    reply = worker._handle_bridge_command("!last 2", "me")
    assert isinstance(reply, str)
    assert_contains(reply, "assistant/final")


def test_thread_menu_selection() -> None:
    worker, _, remote = make_worker()
    menu = worker._handle_bridge_command("!threads", "me")
    assert isinstance(menu, str)
    assert_contains(menu, "Codex threads")
    assert_contains(menu, "1. thread-a")

    selected = worker._handle_pending_menu("2")
    assert selected is not None
    assert_contains(selected, "bound thread")
    assert remote.bound == ["thread-def456"]


def test_resume_maps_to_thread_menu() -> None:
    worker, _, remote = make_worker()
    menu = worker._handle_codex_slash_command("/resume")
    assert_contains(menu, "Codex threads")
    selected = worker._handle_pending_menu("1")
    assert selected is not None
    assert_contains(selected, "bound thread")
    assert remote.bound == ["thread-abc123"]


def test_thread_remote_control_commands() -> None:
    worker, _, remote = make_worker()

    forked = worker._handle_bridge_command("!fork", "me")
    assert isinstance(forked, str)
    assert_contains(forked, "thread-fork")
    assert remote.thread_id == "thread-fork"

    renamed = worker._handle_bridge_command("!rename 手机线程", "me")
    assert isinstance(renamed, str)
    assert_contains(renamed, "name=手机线程")
    assert remote.renamed[-1] == ("thread-fork", "手机线程")

    rolled_back = worker._handle_bridge_command("!rollback 2", "me")
    assert isinstance(rolled_back, str)
    assert_contains(rolled_back, "num_turns=2")
    assert remote.rolled_back[-1] == ("thread-fork", 2)

    archived = worker._handle_bridge_command("!archive", "me")
    assert isinstance(archived, str)
    assert_contains(archived, "current thread cleared")
    assert remote.archived[-1] == "thread-fork"
    assert remote.thread_id is None

    unarchived = worker._handle_bridge_command("!unarchive thread-abc123", "me")
    assert isinstance(unarchived, str)
    assert_contains(unarchived, "thread-abc123")
    assert remote.unarchived[-1] == "thread-abc123"
    assert remote.thread_id == "thread-abc123"


def test_dangerous_bridge_command_requires_confirmation() -> None:
    worker, _, remote = make_worker()
    worker.config = replace_config(worker.config, dangerous_commands_require_confirmation=True)

    reply = worker._handle_bridge_command("!new", "me")
    assert isinstance(reply, str)
    assert_contains(reply, "requires confirmation")
    assert remote.started == []
    assert remote.thread_id == "thread-1"

    denied = worker._handle_bridge_command("!deny", "me")
    assert isinstance(denied, str)
    assert_contains(denied, "cancelled")
    assert remote.thread_id == "thread-1"

    reply = worker._handle_bridge_command("!new", "me")
    assert isinstance(reply, str)
    assert_contains(reply, "requires confirmation")
    confirmed = worker._handle_pending_confirmation("确认", "me")
    assert isinstance(confirmed, str)
    assert_contains(confirmed, "new thread")
    assert remote.thread_id == "thread-new"


def test_bridge_command_allowlist_blocks_unlisted_command() -> None:
    worker, _, _ = make_worker()
    worker.config = replace_config(worker.config, allowed_bridge_commands=("status",))

    blocked = worker._handle_bridge_command("!tail", "me")
    assert isinstance(blocked, str)
    assert_contains(blocked, "not allowed")

    status = worker._handle_bridge_command("!status", "me")
    assert isinstance(status, str)
    assert_contains(status, "bridge ok")


def test_config_security_profile_and_allowed_users() -> None:
    old_env = dict(os.environ)
    os.environ.clear()
    os.environ.update(
        {
            "WECOM_CORP_ID": "corp",
            "WECOM_CORP_SECRET": "secret",
            "WECOM_AGENT_ID": "1",
            "WECOM_TOKEN": "token",
            "WECOM_ENCODING_AES_KEY": "a" * 43,
            "WECOM_TO_USER": "owner",
            "BRIDGE_SECURITY_PROFILE": "safe",
            "CODEX_REMOTE_APPROVAL_POLICY": "",
            "CODEX_REMOTE_SANDBOX": "",
            "BRIDGE_AUDIT_LOG_FILE": "",
        }
    )
    try:
        config = Config.from_env()
    finally:
        os.environ.clear()
        os.environ.update(old_env)

    assert config.codex_remote_approval_policy == "on-request"
    assert config.codex_remote_sandbox == "read-only"
    assert config.allowed_wecom_users == ("owner",)
    assert config.is_user_allowed("owner")
    assert not config.is_user_allowed("other")


def test_slash_thread_remote_control_commands() -> None:
    worker, _, remote = make_worker()

    renamed = worker._handle_codex_slash_command("/rename slash name")
    assert_contains(renamed, "Codex renamed thread")
    assert remote.renamed[-1] == ("thread-1", "slash name")

    rolled_back = worker._handle_codex_slash_command("/rollback 3")
    assert_contains(rolled_back, "num_turns=3")
    assert remote.rolled_back[-1] == ("thread-1", 3)


def test_cd_switches_workdir_and_new_thread() -> None:
    worker, _, remote = make_worker()
    reply = worker._handle_bridge_command("!cd /tmp", "me")
    assert isinstance(reply, str)
    assert_contains(reply, "Codex cwd changed")
    assert_contains(reply, "new_thread=thread-cd")
    assert remote.workdir == "/tmp"

    cwd = worker._handle_bridge_command("!cwd", "me")
    assert isinstance(cwd, str)
    assert_contains(cwd, "current_thread_cwd=/tmp")


def test_goal_command_set_view_clear() -> None:
    worker, _, remote = make_worker()
    empty = worker._handle_codex_slash_command("/goal")
    assert_contains(empty, "(no goal set)")

    set_reply = worker._handle_codex_slash_command("/goal 你好")
    assert_contains(set_reply, "Codex goal set")
    assert_contains(set_reply, "objective=你好")
    assert remote.goal is not None

    view_reply = worker._handle_codex_slash_command("/goal")
    assert_contains(view_reply, "Codex goal")
    assert_contains(view_reply, "objective=你好")

    clear_reply = worker._handle_codex_slash_command("/goal clear")
    assert_contains(clear_reply, "Codex goal cleared")
    assert remote.goal is None


def test_goal_command_works_when_slash_list_fetch_fails() -> None:
    worker, _, remote = make_worker()

    def fail_list_commands() -> list[SlashCommandInfo]:
        raise RuntimeError("network failed")

    worker.slash_commands.list_commands = fail_list_commands
    reply = worker._handle_codex_slash_command("/goal 仍然设置")

    assert_contains(reply, "Codex goal set")
    assert remote.goal is not None
    assert remote.goal["objective"] == "仍然设置"


def test_new_slash_command_clears_stale_pending_menu() -> None:
    worker, _, remote = make_worker()
    model_menu = worker._handle_codex_slash_command("/model")
    assert_contains(model_menu, "Codex models")
    reply = worker._handle_pending_menu("/goal 新目标")
    assert reply is None

    goal_reply = worker._handle_codex_slash_command("/goal 新目标")
    assert_contains(goal_reply, "Codex goal set")
    assert remote.goal is not None

    stale_number = worker._handle_pending_menu("2")
    assert stale_number is None
    assert remote.model_override is None


def test_progress_formatters() -> None:
    sent: set[str] = set()
    command = {"type": "commandExecution", "id": "cmd-1", "command": "date", "status": "inProgress"}
    assert format_item_progress(command, sent) == "[直接执行]\ndate"
    assert format_item_progress(command, sent) is None
    failed = {
        "type": "commandExecution",
        "id": "cmd-1",
        "command": "date",
        "status": "failed",
        "exitCode": 2,
        "aggregatedOutput": "bad",
    }
    assert_contains(format_item_progress(failed, sent) or "", "[直接执行失败]")
    long_command = {
        "type": "commandExecution",
        "id": "cmd-2",
        "command": "python3 - <<'PY'\nprint('do not send this code')\nPY",
        "status": "inProgress",
    }
    formatted = format_item_progress(long_command, sent) or ""
    assert_contains(formatted, "python3 (multi-line command")
    assert "do not send this code" not in formatted
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n-old\n+new\n+line\n"
    assert format_diff_summary(diff) == "[文件变更]\na.py (+2 -1)"


def test_split_message_keeps_menu_lines_together() -> None:
    menu = "\n".join(
        f"{index}. thread-{index:02d} - {'项目' * 20}"
        for index in range(1, 31)
    )
    chunks = split_message(menu, limit=900)
    assert len(chunks) > 1
    assert all(len(chunk.encode("utf-8")) <= 900 for chunk in chunks)
    for chunk in chunks[:-1]:
        assert chunk.endswith("\n") is False
    joined = "\n".join(chunks)
    assert "17. thread-17" in joined
    assert joined.replace("\n", "") == menu.replace("\n", "")


def test_wait_for_turn_forwards_commentary_summary() -> None:
    client = object.__new__(CodexAppServerClient)
    client.config = replace_config(make_worker()[0].config, bridge_forward_thought_summary=True)
    client._notifications = queue.Queue()
    client._turn_state_lock = threading.Lock()
    client._current_turn_ids = {}
    client._notifications.put(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"type": "agentMessage", "phase": "commentary", "text": "noise"},
            },
        }
    )
    client._notifications.put(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"type": "agentMessage", "phase": "final_answer", "text": "final"},
            },
        }
    )
    client._notifications.put(
        {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "turn": {"status": "completed"}},
        }
    )
    progress: list[str] = []

    reply = CodexAppServerClient._wait_for_turn(
        client,
        "thread-1",
        "turn-1",
        1,
        on_message=progress.append,
    )

    assert progress == ["[思考摘要]\nnoise"]
    assert reply == "final"


def test_wait_for_turn_suppresses_tool_noise_by_default() -> None:
    client = object.__new__(CodexAppServerClient)
    client.config = replace_config(
        make_worker()[0].config,
        bridge_forward_tool_progress=False,
        bridge_forward_file_changes=False,
    )
    client._notifications = queue.Queue()
    client._turn_state_lock = threading.Lock()
    client._current_turn_ids = {}
    client._notifications.put(
        {
            "method": "item/started",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"type": "commandExecution", "id": "cmd-1", "command": "date"},
            },
        }
    )
    client._notifications.put(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "final",
                },
            },
        }
    )
    client._notifications.put(
        {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "turn": {"status": "completed"}},
        }
    )
    progress: list[str] = []

    reply = CodexAppServerClient._wait_for_turn(
        client,
        "thread-1",
        "turn-1",
        1,
        on_message=progress.append,
    )

    assert progress == []
    assert reply == "final"


def test_wait_for_turn_forwards_reasoning_summary_delta() -> None:
    client = object.__new__(CodexAppServerClient)
    client.config = replace_config(make_worker()[0].config, bridge_forward_thought_summary=True)
    client._notifications = queue.Queue()
    client._turn_state_lock = threading.Lock()
    client._current_turn_ids = {}
    client._notifications.put(
        {
            "method": "item/reasoning/summaryTextDelta",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "reasoning-1",
                "summaryIndex": 0,
                "delta": "我会先确认审批协议，再实现手机端回写。",
            },
        }
    )
    client._notifications.put(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "final",
                },
            },
        }
    )
    client._notifications.put(
        {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "turn": {"status": "completed"}},
        }
    )
    progress: list[str] = []

    reply = CodexAppServerClient._wait_for_turn(
        client,
        "thread-1",
        "turn-1",
        1,
        on_message=progress.append,
    )

    assert progress == ["[思考摘要]\n我会先确认审批协议，再实现手机端回写。"]
    assert reply == "final"


def test_wait_for_turn_surfaces_approval_request() -> None:
    client = object.__new__(CodexAppServerClient)
    client.config = make_worker()[0].config
    client._notifications = queue.Queue()
    client._turn_state_lock = threading.Lock()
    client._current_turn_ids = {}
    client._notifications.put(
        {
            "id": "approval-1",
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "cmd-1",
                "command": "pytest",
                "cwd": "/tmp/project-a",
            },
        }
    )
    client._notifications.put(
        {
            "method": "serverRequest/resolved",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "requestId": "approval-1",
            },
        }
    )
    client._notifications.put(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "final",
                },
            },
        }
    )
    client._notifications.put(
        {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "turn": {"status": "completed"}},
        }
    )
    approvals: list[dict[str, object]] = []

    reply = CodexAppServerClient._wait_for_turn(
        client,
        "thread-1",
        "turn-1",
        1,
        on_message=None,
        on_approval=approvals.append,
    )

    assert reply == "final"
    assert len(approvals) == 1
    assert approvals[0]["id"] == "approval-1"


def test_codex_approval_reply_uses_plain_agree_or_reject() -> None:
    worker, sender, remote = make_worker()
    worker._send_codex_approval_request(
        {
            "id": "approval-1",
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "cmd-1",
                "command": "pytest",
                "cwd": "/tmp/project-a",
            },
        },
        "me",
    )

    assert_contains(sender.sent[-1][1], "[审批请求]")
    assert_contains(sender.sent[-1][1], "回复 同意")
    reply = worker._handle_codex_approval_reply("同意", "me")

    assert reply == "已同意 Codex 审批\nid=1"
    assert remote.approvals == [
        (
            "approval-1",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "cmd-1",
                "command": "pytest",
                "cwd": "/tmp/project-a",
            },
            True,
        )
    ]


def test_approval_response_shapes() -> None:
    client = object.__new__(CodexAppServerClient)

    assert CodexAppServerClient._approval_response(
        client,
        "item/commandExecution/requestApproval",
        {},
        True,
    ) == {"decision": "accept"}
    assert CodexAppServerClient._approval_response(
        client,
        "item/fileChange/requestApproval",
        {},
        False,
    ) == {"decision": "decline"}
    permissions = {"network": {"enabled": True}, "fileSystem": None}
    assert CodexAppServerClient._approval_response(
        client,
        "item/permissions/requestApproval",
        {"permissions": permissions},
        True,
    ) == {"permissions": permissions, "scope": "turn", "strictAutoReview": True}
    assert CodexAppServerClient._approval_response(
        client,
        "execCommandApproval",
        {},
        False,
    ) == {"decision": "denied"}


def test_wait_for_turn_renews_timeout_while_thread_active() -> None:
    client = object.__new__(CodexAppServerClient)
    client._notifications = queue.Queue()
    client._thread_is_active = lambda thread_id: True

    def complete_later() -> None:
        client._notifications.put(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "final after quiet active period",
                    },
                },
            }
        )
        client._notifications.put(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "turn": {"status": "completed"},
                },
            }
        )

    timer = threading.Timer(0.35, complete_later)
    timer.start()
    try:
        reply = CodexAppServerClient._wait_for_turn(
            client,
            "thread-1",
            "turn-1",
            0.2,
            on_message=None,
        )
    finally:
        timer.cancel()

    assert reply == "final after quiet active period"


def test_interrupt_retries_with_actual_active_turn_id() -> None:
    client = object.__new__(CodexAppServerClient)
    client._turn_state_lock = threading.Lock()
    client._current_turn_ids = {}
    old_turn = "019e164e-4b79-7781-b339-938fdab4888f"
    actual_turn = "019e1652-9180-7432-a00c-d9c8df7c1bf7"
    calls: list[str] = []

    def fake_request(method: str, params: dict[str, object], timeout: int) -> dict[str, object]:
        assert method == "turn/interrupt"
        turn_id = str(params["turnId"])
        calls.append(turn_id)
        if turn_id == old_turn:
            raise RuntimeError(
                "turn/interrupt failed: expected active turn id "
                f"{old_turn} but found {actual_turn}"
            )
        return {}

    client.request = fake_request
    interrupted = CodexAppServerClient.interrupt_turn(client, "thread-1", old_turn)

    assert interrupted == actual_turn
    assert calls == [old_turn, actual_turn]
    assert client._current_turn_ids["thread-1"] == actual_turn


def test_wait_for_turn_follows_steered_turn_id() -> None:
    client = object.__new__(CodexAppServerClient)
    client._notifications = queue.Queue()
    client._turn_state_lock = threading.Lock()
    client._current_turn_ids = {"thread-1": "turn-new"}
    client._thread_is_active = lambda thread_id: False
    client._notifications.put(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-old",
                "turn": {"id": "turn-old", "status": "completed"},
            },
        }
    )
    client._notifications.put(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-new",
                "item": {
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "new final",
                },
            },
        }
    )
    client._notifications.put(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-new",
                "turn": {"id": "turn-new", "status": "completed"},
            },
        }
    )

    reply = CodexAppServerClient._wait_for_turn(
        client,
        "thread-1",
        "turn-old",
        1,
        on_message=None,
    )

    assert reply == "new final"
    assert "thread-1" not in client._current_turn_ids


def test_start_turn_uses_effort_parameter() -> None:
    worker, _, _ = make_worker()
    client = object.__new__(CodexAppServerClient)
    client.config = worker.config
    client.model_override = "gpt-5.5"
    client.reasoning_effort_override = "low"
    client._ensure_thread = lambda: "thread-1"
    captured: dict[str, object] = {}

    def fake_request(method: str, params: dict[str, object], timeout: int) -> dict[str, object]:
        captured["method"] = method
        captured["params"] = params
        return {"turn": {"id": "turn-1"}}

    client.request = fake_request
    thread_id, turn_id = CodexAppServerClient.start_turn(client, "hello")

    assert thread_id == "thread-1"
    assert turn_id == "turn-1"
    assert captured["method"] == "turn/start"
    params = captured["params"]
    assert isinstance(params, dict)
    assert params["model"] == "gpt-5.5"
    assert params["effort"] == "low"


def main() -> None:
    tests = [
        test_menu_model_selection,
        test_active_turn_steer_interrupt_continue,
        test_interrupted_turn_final_is_normalized,
        test_tail_does_not_remember_itself,
        test_last_history_command,
        test_thread_menu_selection,
        test_resume_maps_to_thread_menu,
        test_thread_remote_control_commands,
        test_dangerous_bridge_command_requires_confirmation,
        test_bridge_command_allowlist_blocks_unlisted_command,
        test_config_security_profile_and_allowed_users,
        test_slash_thread_remote_control_commands,
        test_cd_switches_workdir_and_new_thread,
        test_goal_command_set_view_clear,
        test_goal_command_works_when_slash_list_fetch_fails,
        test_new_slash_command_clears_stale_pending_menu,
        test_progress_formatters,
        test_split_message_keeps_menu_lines_together,
        test_wait_for_turn_forwards_commentary_summary,
        test_wait_for_turn_suppresses_tool_noise_by_default,
        test_wait_for_turn_forwards_reasoning_summary_delta,
        test_wait_for_turn_surfaces_approval_request,
        test_codex_approval_reply_uses_plain_agree_or_reject,
        test_approval_response_shapes,
        test_wait_for_turn_renews_timeout_while_thread_active,
        test_interrupt_retries_with_actual_active_turn_id,
        test_wait_for_turn_follows_steered_turn_id,
        test_start_turn_uses_effort_parameter,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")


if __name__ == "__main__":
    main()
