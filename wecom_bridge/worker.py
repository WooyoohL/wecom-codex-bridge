import os
import queue
import re
import threading
import time
from dataclasses import replace
from typing import Callable

from wecom_bridge.audit import audit_event
from wecom_bridge.codex.client import CodexAppServerClient
from wecom_bridge.codex.formatters import tail_text
from wecom_bridge.codex.slash import CodexSlashCommandProvider
from wecom_bridge.codex.threads import CodexThreadService
from wecom_bridge.config import Config
from wecom_bridge.models import (
    ActiveTurn,
    IncomingMessage,
    MenuOption,
    PendingConfirmation,
    PendingMenu,
    RecentOutgoing,
    format_active_turn_status,
)
from wecom_bridge.wecom.sender import WeComSender


DANGEROUS_BRIDGE_COMMANDS = {
    "archive",
    "bind",
    "cd",
    "fork",
    "new",
    "rename",
    "rollback",
    "unarchive",
}


class MessageWorker:
    def __init__(self, config: Config, sender: WeComSender) -> None:
        self.config = config
        self.sender = sender
        self.queue: queue.Queue[IncomingMessage] = queue.Queue()
        self.seen_msg_ids: set[str] = set()
        self._lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._recent_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self.recent_outgoing: list[RecentOutgoing] = []
        self.pending_menu: PendingMenu | None = None
        self.pending_confirmation: PendingConfirmation | None = None
        self.active_turn: ActiveTurn | None = None
        self.remote_client: CodexAppServerClient | None = None
        if config.codex_backend == "codex_remote_control":
            self.remote_client = CodexAppServerClient(config)
        elif config.codex_backend != "disabled":
            raise SystemExit(f"unsupported CODEX_BACKEND: {config.codex_backend}")
        self.slash_commands = CodexSlashCommandProvider(config)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, message: IncomingMessage) -> None:
        if message.msg_id:
            with self._lock:
                if message.msg_id in self.seen_msg_ids:
                    print(f"skip duplicate message: msg_id={message.msg_id}", flush=True)
                    return
                self.seen_msg_ids.add(message.msg_id)
                if len(self.seen_msg_ids) > 1024:
                    self.seen_msg_ids = set(list(self.seen_msg_ids)[-512:])
        self.queue.put(message)

    def _run(self) -> None:
        while True:
            message = self.queue.get()
            try:
                after_send: Callable[[], None] | None = None
                print(
                    f"handle message: from={message.from_user} "
                    f"type={message.msg_type} content={message.content[:80]!r}",
                    flush=True,
                )
                if message.msg_type != "text":
                    reply = f"unsupported message type: {message.msg_type}"
                elif self._is_bridge_command(message.content):
                    bridge_result = self._handle_bridge_command(
                        message.content,
                        self.config.to_user or message.from_user,
                    )
                    if isinstance(bridge_result, tuple):
                        reply, after_send = bridge_result
                    else:
                        reply = bridge_result
                elif (
                    pending_confirm := self._handle_pending_confirmation(
                        message.content,
                        self.config.to_user or message.from_user,
                    )
                ) is not None:
                    reply = pending_confirm
                elif (pending_reply := self._handle_pending_menu(message.content)) is not None:
                    reply = pending_reply
                elif message.content.strip() == "/":
                    reply = self._codex_slash_help()
                elif message.content.strip().startswith("/"):
                    reply = self._handle_codex_slash_command(message.content)
                elif self.remote_client:
                    target = self.config.to_user or message.from_user
                    reply, after_send = self._handle_remote_text(message.content, target)
                else:
                    reply = "codex remote-control backend is not available"
                try:
                    target = self.config.to_user or message.from_user
                    if reply:
                        self._send_and_remember(
                            reply,
                            target,
                            kind="reply",
                            remember=not self._is_tail_command(message.content),
                        )
                finally:
                    if after_send:
                        after_send()
            except Exception as exc:
                print(f"message handling failed: {exc}", flush=True)
                try:
                    self._send_and_remember(
                        f"bridge error: {exc}",
                        self.config.to_user,
                        kind="error",
                    )
                except Exception as nested:
                    print(f"failed to send error message: {nested}", flush=True)
            finally:
                self.queue.task_done()

    def _is_bridge_command(self, text: str) -> bool:
        stripped = text.strip()
        prefix = self.config.bridge_command_prefix
        return bool(prefix) and stripped.startswith(prefix)

    def _is_tail_command(self, text: str) -> bool:
        stripped = text.strip()
        prefix = self.config.bridge_command_prefix
        if not prefix or not stripped.startswith(prefix):
            return False
        command = stripped[len(prefix) :].strip().split(maxsplit=1)
        return bool(command) and command[0].lower() == "tail"

    def _send_and_remember(
        self,
        content: str,
        target: str,
        *,
        kind: str,
        remember: bool = True,
    ) -> None:
        if remember:
            self._remember_outgoing(kind, target, content)
        self.sender.send_text(content, to_user=target)

    def _remember_outgoing(self, kind: str, target: str, content: str) -> None:
        item = RecentOutgoing(
            created_at=time.time(),
            kind=kind,
            target=target,
            content=content,
        )
        with self._recent_lock:
            self.recent_outgoing.append(item)
            if len(self.recent_outgoing) > 50:
                self.recent_outgoing = self.recent_outgoing[-50:]

    def _recent_tail(self, limit: int) -> str:
        limit = max(1, min(limit, 50))
        with self._recent_lock:
            items = list(self.recent_outgoing[-limit:])
        if not items:
            return "(no recent outgoing messages)"
        lines = []
        for item in items:
            stamp = time.strftime("%H:%M:%S", time.localtime(item.created_at))
            preview = tail_text(item.content.strip(), 800)
            lines.append(f"[{stamp}] {item.kind} -> {item.target}\n{preview}")
        return "\n\n".join(lines)

    def _set_pending_menu(self, menu: PendingMenu) -> None:
        with self._pending_lock:
            self.pending_menu = menu

    def _clear_pending_menu(self) -> None:
        with self._pending_lock:
            self.pending_menu = None

    def _pending_snapshot(self) -> PendingMenu | None:
        with self._pending_lock:
            menu = self.pending_menu
            if menu and time.time() - menu.created_at > 300:
                self.pending_menu = None
                return None
            return menu

    def _set_pending_confirmation(self, confirmation: PendingConfirmation) -> None:
        with self._pending_lock:
            self.pending_confirmation = confirmation

    def _clear_pending_confirmation(self) -> None:
        with self._pending_lock:
            self.pending_confirmation = None

    def _pending_confirmation_snapshot(self) -> PendingConfirmation | None:
        with self._pending_lock:
            confirmation = self.pending_confirmation
            if confirmation and time.time() - confirmation.created_at > 300:
                self.pending_confirmation = None
                return None
            return confirmation

    def _handle_pending_confirmation(self, text: str, target: str) -> str | None:
        confirmation = self._pending_confirmation_snapshot()
        if not confirmation:
            return None
        stripped = text.strip()
        if stripped in {"确认", "同意", "yes", "y"}:
            return self._confirm_bridge_command(confirmation.confirmation_id, target)
        if stripped in {"取消", "拒绝", "no", "n"}:
            self._clear_pending_confirmation()
            audit_event(
                self.config,
                "bridge_command_denied",
                user=target,
                command=confirmation.command_line,
            )
            return f"cancelled: {confirmation.command_line}"
        return None

    def _handle_pending_menu(self, text: str) -> str | None:
        menu = self._pending_snapshot()
        if not menu:
            return None

        stripped = text.strip()
        if stripped.lower() in {"cancel", "q", "quit", "exit", "取消", "退出"}:
            self._clear_pending_menu()
            return f"{menu.title} cancelled"

        if self._is_bridge_command(stripped):
            return None

        option = self._match_menu_option(menu, stripped)
        if not option:
            if stripped.startswith("/"):
                self._clear_pending_menu()
                return None
            self._clear_pending_menu()
            return None

        self._clear_pending_menu()
        if menu.kind == "slash":
            return self._handle_codex_slash_command("/" + option.value)
        if menu.kind == "model":
            return self._select_model(option)
        if menu.kind == "reasoning":
            return self._select_reasoning_effort(option)
        if menu.kind == "thread":
            return self._select_thread(option)
        return None

    def _match_menu_option(self, menu: PendingMenu, text: str) -> MenuOption | None:
        candidate = text.strip()
        if not candidate:
            return None
        candidate = candidate.removeprefix("/").strip()
        if re.fullmatch(r"\d+", candidate):
            index = int(candidate) - 1
            if 0 <= index < len(menu.options):
                return menu.options[index]
            return None

        normalized = candidate.casefold()
        for option in menu.options:
            values = {
                option.value.casefold(),
                option.label.casefold(),
                f"{option.label} ({option.value})".casefold(),
            }
            if normalized in values:
                return option
        return None

    def _format_menu(self, menu: PendingMenu, *, extra: str = "") -> str:
        self._set_pending_menu(menu)
        lines = [menu.title]
        if extra:
            lines.append(extra)
        for index, option in enumerate(menu.options, start=1):
            suffix = f" - {option.description}" if option.description else ""
            lines.append(f"{index}. {option.label}{suffix}")
        lines.append("")
        lines.append("Reply with a number to select, or send 取消 to cancel.")
        return "\n".join(lines)

    def _active_snapshot(self) -> ActiveTurn | None:
        with self._active_lock:
            return self.active_turn

    def _set_active_turn(self, turn: ActiveTurn | None) -> None:
        with self._active_lock:
            self.active_turn = turn

    def _clear_active_turn(self, turn_id: str) -> None:
        with self._active_lock:
            if self.active_turn and self.active_turn.turn_id == turn_id:
                self.active_turn = None

    def _clear_active_thread(self, thread_id: str) -> None:
        with self._active_lock:
            if self.active_turn and self.active_turn.thread_id == thread_id:
                self.active_turn = None

    def _replace_active_turn_id(self, old_turn_id: str, new_turn_id: str) -> ActiveTurn | None:
        with self._active_lock:
            if not self.active_turn or self.active_turn.turn_id != old_turn_id:
                return self.active_turn
            if old_turn_id != new_turn_id:
                self.active_turn = replace(self.active_turn, turn_id=new_turn_id)
            return self.active_turn

    def _mark_active_stopping(self, turn_id: str) -> None:
        with self._active_lock:
            if self.active_turn and self.active_turn.turn_id == turn_id:
                self.active_turn = replace(self.active_turn, stopping_at=time.time())

    def _active_is_stopping(self, turn_id: str) -> bool:
        with self._active_lock:
            return bool(
                self.active_turn
                and self.active_turn.turn_id == turn_id
                and self.active_turn.stopping_at
            )

    def _active_thread_is_stopping(self, thread_id: str) -> bool:
        with self._active_lock:
            return bool(
                self.active_turn
                and self.active_turn.thread_id == thread_id
                and self.active_turn.stopping_at
            )

    def _handle_remote_text(self, text: str, target: str) -> tuple[str, Callable[[], None] | None]:
        assert self.remote_client is not None
        active = self._active_snapshot()
        if active:
            if active.stopping_at:
                return (
                    "Codex is interrupting the current turn; send this message again after "
                    "the interruption finishes.",
                    None,
                )
            next_turn_id = self.remote_client.steer_turn(active.thread_id, active.turn_id, text)
            self._replace_active_turn_id(active.turn_id, next_turn_id)
            return f"inserted into current Codex turn\nturn={next_turn_id}", None

        thread_id, turn_id = self.remote_client.start_turn(text)
        active_turn = ActiveTurn(
            thread_id=thread_id,
            turn_id=turn_id,
            target=target,
            started_at=time.time(),
            original_text=text,
        )
        self._set_active_turn(active_turn)
        return (
            "",
            lambda: threading.Thread(
                target=self._wait_active_turn,
                args=(active_turn,),
                daemon=True,
            ).start(),
        )

    def _wait_active_turn(self, active: ActiveTurn) -> None:
        assert self.remote_client is not None
        try:
            reply = self.remote_client.wait_for_turn(
                active.thread_id,
                active.turn_id,
                on_message=lambda text: self._send_progress(text, active.target),
            )
            if self._active_thread_is_stopping(active.thread_id):
                current = self._active_snapshot()
                turn_id = current.turn_id if current else active.turn_id
                reply = f"Codex interrupted\nturn={turn_id}"
            self._send_and_remember(reply, active.target, kind="final")
        except Exception as exc:
            print(f"active turn failed: {exc}", flush=True)
            try:
                self._send_and_remember(f"bridge error: {exc}", active.target, kind="error")
            except Exception as nested:
                print(f"failed to send active turn error: {nested}", flush=True)
        finally:
            self._clear_active_thread(active.thread_id)

    def _handle_bridge_command(
        self,
        text: str,
        target: str | None = None,
        *,
        confirmed: bool = False,
    ) -> str | tuple[str, Callable[[], None] | None]:
        stripped = text.strip()
        prefix = self.config.bridge_command_prefix
        command_line = stripped[len(prefix) :].strip()
        if not command_line:
            return self._bridge_help()
        command, _, arg = command_line.partition(" ")
        command = command.lower()
        target_user = target or self.config.to_user
        if command in ("help", "h", "?"):
            return self._bridge_help()
        if command == "confirm":
            return self._confirm_bridge_command(arg.strip(), target_user)
        if command == "deny":
            return self._deny_bridge_command(arg.strip(), target_user)
        if self.config.allowed_bridge_commands and command not in self.config.allowed_bridge_commands:
            audit_event(
                self.config,
                "bridge_command_blocked",
                user=target_user,
                command=command,
            )
            return f"bridge command is not allowed: {prefix}{command}"
        if self._requires_confirmation(command, confirmed):
            return self._request_bridge_command_confirmation(command_line, target_user)
        if command == "status":
            extra = ""
            if self.remote_client:
                active = self._active_snapshot()
                extra = f"\nthread={self.remote_client.thread_id or '(not started)'}"
                extra += f"\nmodel_override={self.remote_client.model_override or ''}"
                extra += (
                    "\nreasoning_effort_override="
                    f"{self.remote_client.reasoning_effort_override or ''}"
                )
                extra += "\n" + format_active_turn_status(active)
                extra += f"\nqueue_size={self.queue.qsize()}"
                extra += f"\nrecent_outgoing={len(self.recent_outgoing)}"
                extra += f"\ncwd={self.remote_client.workdir}"
            return (
                f"bridge ok\nbackend={self.config.codex_backend}"
                f"\ncommand_prefix={prefix}"
                f"\nsecurity_profile={self.config.bridge_security_profile}"
                f"\nallowed_users={len(self.config.allowed_wecom_users)}"
                f"\nconfirmation_required={self.config.dangerous_commands_require_confirmation}"
                f"{extra}"
            )
        if command == "tail":
            limit = 10
            if arg.strip():
                try:
                    limit = int(arg.strip())
                except ValueError:
                    return f"usage: {prefix}tail [1-50]"
            return self._recent_tail(limit)
        if not self.remote_client:
            return "remote-control backend is not enabled"
        if command == "cwd":
            return self._bridge_cwd()
        if command == "cd":
            return self._bridge_cd(arg)
        if command in ("continue", "cont", "resume"):
            prompt = arg.strip() or "继续"
            return self._handle_remote_text(prompt, target_user)
        if command in ("stop", "interrupt"):
            active = self._active_snapshot()
            if not active:
                return "no active Codex turn"
            interrupted_turn_id = self.remote_client.interrupt_turn(active.thread_id, active.turn_id)
            self._replace_active_turn_id(active.turn_id, interrupted_turn_id)
            self._mark_active_stopping(interrupted_turn_id)
            return f"interrupt sent\nturn={interrupted_turn_id}"
        if command == "queue":
            active = self._active_snapshot()
            return f"queue_size={self.queue.qsize()}\n{format_active_turn_status(active)}"
        if command == "last":
            limit = 3
            if arg.strip():
                try:
                    limit = int(arg.strip())
                except ValueError:
                    return f"usage: {prefix}last [1-10]"
            return self._threads().recent_history(limit)
        if command == "thread":
            return f"thread={self._threads().thread_id or '(not started)'}"
        if command == "threads":
            return self._codex_threads()
        if command == "bind":
            if self._active_snapshot():
                return f"current Codex turn is active; send {prefix}stop before binding a thread"
            thread_id = arg.strip()
            if not thread_id:
                return f"usage: {prefix}bind <thread_id>"
            self._threads().bind(thread_id)
            return f"bound thread: {thread_id}"
        if command == "new":
            if self._active_snapshot():
                return f"current Codex turn is active; send {prefix}stop before starting a new thread"
            thread_id = self._threads().new()
            return f"new thread: {thread_id}"
        if command == "fork":
            if self._active_snapshot():
                return f"current Codex turn is active; send {prefix}stop before forking a thread"
            thread_id = self._threads().fork_current()
            return f"forked thread\nthread={thread_id}"
        if command == "rename":
            name = arg.strip()
            if not name:
                return f"usage: {prefix}rename <name>"
            thread_id = self._threads().rename_current(name)
            return f"renamed thread\nthread={thread_id}\nname={name}"
        if command == "archive":
            if self._active_snapshot():
                return f"current Codex turn is active; send {prefix}stop before archiving a thread"
            thread_id = self._threads().archive_current()
            return f"archived thread\nthread={thread_id}\ncurrent thread cleared"
        if command == "unarchive":
            thread_id = arg.strip()
            if not thread_id:
                return f"usage: {prefix}unarchive <thread_id>"
            self._threads().unarchive(thread_id)
            return f"unarchived and bound thread\nthread={thread_id}"
        if command == "rollback":
            if self._active_snapshot():
                return f"current Codex turn is active; send {prefix}stop before rolling back a thread"
            num_turns = self._parse_num_turns(arg, prefix + "rollback")
            if isinstance(num_turns, str):
                return num_turns
            thread_id = self._threads().rollback_current(num_turns)
            return f"rolled back thread\nthread={thread_id}\nnum_turns={num_turns}"
        return f"unknown bridge command: {prefix}{command}\n\n{self._bridge_help()}"

    def _requires_confirmation(self, command: str, confirmed: bool) -> bool:
        return (
            self.config.dangerous_commands_require_confirmation
            and not confirmed
            and command in DANGEROUS_BRIDGE_COMMANDS
        )

    def _request_bridge_command_confirmation(self, command_line: str, target: str) -> str:
        confirmation_id = str(int(time.time()))
        self._set_pending_confirmation(
            PendingConfirmation(
                confirmation_id=confirmation_id,
                command_line=command_line,
                user_id=target,
                created_at=time.time(),
            )
        )
        audit_event(
            self.config,
            "bridge_command_confirmation_requested",
            user=target,
            command=command_line,
            confirmation_id=confirmation_id,
        )
        prefix = self.config.bridge_command_prefix
        return (
            "Bridge command requires confirmation\n"
            f"id={confirmation_id}\n"
            f"command={prefix}{command_line}\n\n"
            f"Reply 确认 or {prefix}confirm {confirmation_id} to run it.\n"
            f"Reply 取消 or {prefix}deny {confirmation_id} to cancel."
        )

    def _confirm_bridge_command(self, confirmation_id: str, target: str) -> str | tuple[str, Callable[[], None] | None]:
        confirmation = self._pending_confirmation_snapshot()
        if not confirmation:
            return "no pending bridge command confirmation"
        if confirmation_id and confirmation_id != confirmation.confirmation_id:
            return f"confirmation id mismatch; expected {confirmation.confirmation_id}"
        self._clear_pending_confirmation()
        audit_event(
            self.config,
            "bridge_command_confirmed",
            user=target,
            command=confirmation.command_line,
            confirmation_id=confirmation.confirmation_id,
        )
        return self._handle_bridge_command(
            self.config.bridge_command_prefix + confirmation.command_line,
            target,
            confirmed=True,
        )

    def _deny_bridge_command(self, confirmation_id: str, target: str) -> str:
        confirmation = self._pending_confirmation_snapshot()
        if not confirmation:
            return "no pending bridge command confirmation"
        if confirmation_id and confirmation_id != confirmation.confirmation_id:
            return f"confirmation id mismatch; expected {confirmation.confirmation_id}"
        self._clear_pending_confirmation()
        audit_event(
            self.config,
            "bridge_command_denied",
            user=target,
            command=confirmation.command_line,
            confirmation_id=confirmation.confirmation_id,
        )
        return f"cancelled: {self.config.bridge_command_prefix}{confirmation.command_line}"

    def _bridge_help(self) -> str:
        prefix = self.config.bridge_command_prefix
        return (
            f"{prefix}status\n"
            f"{prefix}thread\n"
            f"{prefix}threads\n"
            f"{prefix}bind <thread_id>\n"
            f"{prefix}new\n"
            f"{prefix}fork\n"
            f"{prefix}rename <name>\n"
            f"{prefix}archive\n"
            f"{prefix}unarchive <thread_id>\n"
            f"{prefix}rollback [n]\n"
            f"{prefix}confirm <id>\n"
            f"{prefix}deny <id>\n"
            f"{prefix}cwd\n"
            f"{prefix}cd <path>\n"
            f"{prefix}stop\n"
            f"{prefix}continue [text]\n"
            f"{prefix}queue\n"
            f"{prefix}last [n]\n"
            f"{prefix}tail [n]"
        )

    def _codex_slash_help(self) -> str:
        try:
            commands = self.slash_commands.list_commands()
        except Exception as exc:
            return (
                "Could not read Codex slash commands from "
                f"{self.config.codex_slash_commands_url}: {exc}"
            )
        source = self.slash_commands.source or self.config.codex_slash_commands_url
        options = [
            MenuOption(
                value=item.command,
                label=f"/{item.command}",
                description=item.description,
            )
            for item in commands
        ]
        return self._format_menu(
            PendingMenu(
                kind="slash",
                title="Codex slash commands",
                created_at=time.time(),
                options=options,
            ),
            extra="source: " + source,
        )

    def _handle_codex_slash_command(self, text: str) -> str:
        stripped = text.strip()
        command_line = stripped[1:].strip()
        command, _, arg = command_line.partition(" ")
        command = command.lower()

        commands_error = ""
        try:
            commands = {item.command: item for item in self.slash_commands.list_commands()}
        except Exception as exc:
            commands = {}
            commands_error = str(exc)

        if command in ("", "help", "?"):
            return self._codex_slash_help()
        implemented = {
            "status",
            "new",
            "compact",
            "model",
            "resume",
            "goal",
            "fork",
            "rename",
            "archive",
            "unarchive",
            "rollback",
        }
        if command not in commands and command not in implemented:
            if commands_error:
                return (
                    "Could not read Codex slash commands from "
                    f"{self.config.codex_slash_commands_url}: {commands_error}"
                )
            return f"not in current Codex slash list: /{command}\n\n{self._codex_slash_help()}"

        if not self.remote_client:
            return (
                f"/{command} exists in Codex, but remote-control backend is not enabled. "
                "Use normal text for chat, or enable CODEX_BACKEND=codex_remote_control."
            )

        if command == "status":
            return self._codex_status()
        if command == "new":
            if self._active_snapshot():
                return "current Codex turn is active; send !stop before starting a new thread"
            thread_id = self._threads().new()
            return f"Codex new thread: {thread_id}"
        if command == "fork":
            if self._active_snapshot():
                return "current Codex turn is active; send !stop before forking a thread"
            thread_id = self._threads().fork_current()
            return f"Codex forked thread\nthread={thread_id}"
        if command == "rename":
            name = arg.strip()
            if not name:
                return "usage: /rename <name>"
            thread_id = self._threads().rename_current(name)
            return f"Codex renamed thread\nthread={thread_id}\nname={name}"
        if command == "archive":
            if self._active_snapshot():
                return "current Codex turn is active; send !stop before archiving a thread"
            thread_id = self._threads().archive_current()
            return f"Codex archived thread\nthread={thread_id}\ncurrent thread cleared"
        if command == "unarchive":
            thread_id = arg.strip()
            if not thread_id:
                return "usage: /unarchive <thread_id>"
            self._threads().unarchive(thread_id)
            return f"Codex unarchived and bound thread\nthread={thread_id}"
        if command == "rollback":
            if self._active_snapshot():
                return "current Codex turn is active; send !stop before rolling back a thread"
            num_turns = self._parse_num_turns(arg, "/rollback")
            if isinstance(num_turns, str):
                return num_turns
            thread_id = self._threads().rollback_current(num_turns)
            return f"Codex rolled back thread\nthread={thread_id}\nnum_turns={num_turns}"
        if command == "compact":
            thread_id = self.remote_client.compact_thread()
            return f"Codex compact started for thread: {thread_id}"
        if command == "goal":
            return self._codex_goal(arg)
        if command == "model":
            return self._codex_models()
        if command == "resume":
            return self._codex_threads()

        description = commands[command].description if command in commands else ""
        return (
            f"/{command} is in Codex slash commands"
            + (f": {description}" if description else "")
            + "\n\nThis WeCom bridge can list it, but has not implemented that TUI action yet."
        )

    def _codex_status(self) -> str:
        assert self.remote_client is not None
        thread = self.remote_client.read_thread()
        status = thread.get("status") or {}
        status_type = status.get("type") if isinstance(status, dict) else status
        active = self._active_snapshot()
        return (
            "Codex status\n"
            f"thread={self.remote_client.thread_id or '(not started)'}\n"
            f"cwd={thread.get('cwd') or self.config.codex_workdir or ''}\n"
            f"model={thread.get('model') or ''}\n"
            f"model_override={self.remote_client.model_override or ''}\n"
            f"reasoning_effort_override={self.remote_client.reasoning_effort_override or ''}\n"
            f"model_provider={thread.get('modelProvider') or ''}\n"
            f"status={status_type or ''}\n"
            f"{format_active_turn_status(active)}\n"
            f"approval_policy={self.config.codex_remote_approval_policy}\n"
            f"sandbox={self.config.codex_remote_sandbox}"
        )

    def _codex_goal(self, arg: str) -> str:
        assert self.remote_client is not None
        objective = arg.strip()
        if objective.lower() in {"clear", "reset", "delete", "取消"}:
            cleared = self.remote_client.clear_goal()
            return "Codex goal cleared" if cleared else "Codex goal was already empty"
        if objective:
            goal = self.remote_client.set_goal(objective)
            return self._format_goal(goal, title="Codex goal set")

        goal = self.remote_client.get_goal()
        if not goal:
            return "Codex goal\n(no goal set)"
        return self._format_goal(goal, title="Codex goal")

    def _format_goal(self, goal: dict[str, object], *, title: str) -> str:
        if not goal:
            return title
        lines = [
            title,
            f"objective={goal.get('objective') or ''}",
            f"status={goal.get('status') or ''}",
        ]
        token_budget = goal.get("tokenBudget")
        if token_budget not in (None, ""):
            lines.append(f"token_budget={token_budget}")
        tokens_used = goal.get("tokensUsed")
        if tokens_used not in (None, ""):
            lines.append(f"tokens_used={tokens_used}")
        time_used = goal.get("timeUsedSeconds")
        if time_used not in (None, ""):
            lines.append(f"time_used_seconds={time_used}")
        return "\n".join(lines)

    def _codex_models(self) -> str:
        assert self.remote_client is not None
        models = self.remote_client.list_models(limit=20)
        options = [
            MenuOption(
                value="__codex_default__",
                label="Codex default",
                description="clear bridge model override",
            )
        ]
        for model in models:
            display = model.get("displayName") or model.get("model") or model.get("id")
            model_id = model.get("model") or model.get("id") or ""
            if not model_id:
                continue
            label = f"{display} ({model_id})" if display != model_id else model_id
            description = "default" if model.get("isDefault") else ""
            options.append(MenuOption(value=model_id, label=label, description=description))
        if not options:
            return "Codex models\n(no models)"
        current_model = self.remote_client.model_override or "(Codex default)"
        current_effort = self.remote_client.reasoning_effort_override or "(Codex default)"
        return self._format_menu(
            PendingMenu(
                kind="model",
                title="Codex models",
                created_at=time.time(),
                options=options,
            ),
            extra=f"current_model={current_model}\ncurrent_reasoning_effort={current_effort}",
        )

    def _codex_threads(self) -> str:
        assert self.remote_client is not None
        prefix = self.config.bridge_command_prefix
        if self._active_snapshot():
            return f"current Codex turn is active; send {prefix}stop before choosing a thread"
        limit = max(1, min(self.config.codex_thread_list_limit, 50))
        threads = self._threads().list_recent(limit)
        options: list[MenuOption] = []
        for thread in threads:
            options.append(
                MenuOption(
                    value=thread.thread_id,
                    label=thread.menu_label,
                    description=thread.menu_description,
                )
            )
        if not options:
            return "Codex threads\n(no threads)"
        return self._format_menu(
            PendingMenu(
                kind="thread",
                title="Codex threads",
                created_at=time.time(),
                options=options,
            ),
            extra=f"showing_latest={limit}\nReply with a number to bind that thread.",
        )

    def _select_thread(self, option: MenuOption) -> str:
        assert self.remote_client is not None
        prefix = self.config.bridge_command_prefix
        if self._active_snapshot():
            return f"current Codex turn is active; send {prefix}stop before binding a thread"
        self._threads().bind(option.value)
        detail = f"\n{option.description}" if option.description else ""
        return f"bound thread\nthread={option.value}{detail}"

    def _bridge_cwd(self) -> str:
        assert self.remote_client is not None
        thread_cwd = ""
        try:
            thread = self._threads().read_current()
            thread_cwd = str(thread.get("cwd") or "")
        except Exception as exc:
            thread_cwd = f"(unavailable: {exc})"
        return (
            "Codex cwd\n"
            f"current_thread_cwd={thread_cwd}\n"
            f"default_new_thread_cwd={self.remote_client.workdir}"
        )

    def _bridge_cd(self, arg: str) -> str:
        assert self.remote_client is not None
        prefix = self.config.bridge_command_prefix
        if self._active_snapshot():
            return f"current Codex turn is active; send {prefix}stop before changing cwd"

        raw_path = arg.strip()
        if not raw_path:
            return self._bridge_cwd()
        if raw_path in {"--reset", "reset", "default"}:
            thread_id = self._threads().reset_workdir()
            return (
                "Codex cwd reset\n"
                f"cwd={self.remote_client.workdir}\n"
                f"new_thread={thread_id}"
            )

        base = self.remote_client.workdir or self.config.codex_workdir or os.getcwd()
        path = os.path.expandvars(os.path.expanduser(raw_path))
        if not os.path.isabs(path):
            path = os.path.abspath(os.path.join(base, path))
        if not os.path.isdir(path):
            return f"not a directory: {path}"

        thread_id = self._threads().set_workdir(path)
        return (
            "Codex cwd changed\n"
            f"cwd={path}\n"
            f"new_thread={thread_id}"
        )

    def _threads(self) -> CodexThreadService:
        assert self.remote_client is not None
        return CodexThreadService(self.remote_client)

    def _parse_num_turns(self, arg: str, command_name: str) -> int | str:
        raw = arg.strip()
        if not raw:
            return 1
        try:
            value = int(raw)
        except ValueError:
            return f"usage: {command_name} [n]"
        if value < 1 or value > 20:
            return f"usage: {command_name} [1-20]"
        return value

    def _select_model(self, option: MenuOption) -> str:
        assert self.remote_client is not None
        if option.value == "__codex_default__":
            self.remote_client.set_model_override(None)
            self.remote_client.set_reasoning_effort_override(None)
            return (
                "Codex model override cleared\n"
                "Future Codex turns will use Codex default model and reasoning effort."
            )

        model_info = self._find_model(option.value)
        efforts = self._reasoning_effort_options(model_info)
        default_effort = self._model_default_reasoning_effort(model_info)
        if efforts:
            return self._format_menu(
                PendingMenu(
                    kind="reasoning",
                    title="Codex reasoning effort",
                    created_at=time.time(),
                    options=[
                        MenuOption(
                            value=f"{option.value}\t{effort}",
                            label=effort,
                            description=self._reasoning_effort_description(
                                effort,
                                description,
                                default_effort,
                            ),
                        )
                        for effort, description in efforts
                    ],
                ),
                extra=f"model={option.label}\ndefault={default_effort or '(not reported)'}",
            )

        self.remote_client.set_model_override(option.value)
        self.remote_client.set_reasoning_effort_override(None)
        active_note = ""
        if self._active_snapshot():
            active_note = "\nCurrent turn is already running; this applies to future turns."
        return (
            "Codex model selected\n"
            f"model={option.value}\n"
            f"label={option.label}\n"
            "reasoning_effort=(Codex default)\n"
            "Applies to future Codex turns."
            f"{active_note}"
        )

    def _select_reasoning_effort(self, option: MenuOption) -> str:
        assert self.remote_client is not None
        model, _, effort = option.value.partition("\t")
        if not model or not effort:
            return "invalid Codex reasoning effort selection"
        self.remote_client.set_model_override(model)
        self.remote_client.set_reasoning_effort_override(effort)
        active_note = ""
        if self._active_snapshot():
            active_note = "\nCurrent turn is already running; this applies to future turns."
        return (
            "Codex model selected\n"
            f"model={model}\n"
            f"reasoning_effort={effort}\n"
            "Applies to future Codex turns."
            f"{active_note}"
        )

    def _find_model(self, model_id: str) -> dict[str, object]:
        assert self.remote_client is not None
        for model in self.remote_client.list_models(limit=50):
            if model_id in {str(model.get("model") or ""), str(model.get("id") or "")}:
                return model
        return {}

    def _reasoning_effort_options(self, model: dict[str, object]) -> list[tuple[str, str]]:
        raw_options = (
            model.get("supportedReasoningEfforts")
            or model.get("supported_reasoning_efforts")
            or model.get("supported_reasoning_levels")
            or []
        )
        options: list[tuple[str, str]] = []
        if not isinstance(raw_options, list):
            return options
        for raw in raw_options:
            if isinstance(raw, str):
                effort = raw
                description = ""
            elif isinstance(raw, dict):
                effort = str(
                    raw.get("reasoningEffort")
                    or raw.get("effort")
                    or raw.get("reasoning_effort")
                    or ""
                )
                description = str(raw.get("description") or "")
            else:
                continue
            if effort:
                options.append((effort, description))
        return options

    def _model_default_reasoning_effort(self, model: dict[str, object]) -> str:
        return str(
            model.get("defaultReasoningEffort")
            or model.get("default_reasoning_effort")
            or model.get("default_reasoning_level")
            or ""
        )

    def _reasoning_effort_description(
        self,
        effort: str,
        description: str,
        default_effort: str,
    ) -> str:
        parts: list[str] = []
        if default_effort and effort == default_effort:
            parts.append("default")
        if description:
            parts.append(description)
        return " - ".join(parts)

    def _send_progress(self, text: str, target: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        try:
            self._send_and_remember(cleaned, target, kind="progress")
        except Exception as exc:
            print(f"failed to send progress message: {exc}", flush=True)
