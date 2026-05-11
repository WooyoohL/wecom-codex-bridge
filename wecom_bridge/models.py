import time
from dataclasses import dataclass


@dataclass(frozen=True)
class SlashCommandInfo:
    command: str
    description: str


@dataclass(frozen=True)
class ActiveTurn:
    thread_id: str
    turn_id: str
    target: str
    started_at: float
    original_text: str
    stopping_at: float | None = None


@dataclass(frozen=True)
class RecentOutgoing:
    created_at: float
    kind: str
    target: str
    content: str


@dataclass(frozen=True)
class MenuOption:
    value: str
    label: str
    description: str = ""


@dataclass(frozen=True)
class PendingMenu:
    kind: str
    title: str
    created_at: float
    options: list[MenuOption]


def format_active_turn_status(active: ActiveTurn | None) -> str:
    if not active:
        return "active_turn=(none)"
    elapsed = int(time.time() - active.started_at)
    preview = " ".join(active.original_text.split())[:120]
    lines = [
        f"active_turn={active.turn_id}",
        f"active_thread={active.thread_id}",
        f"active_state={'interrupting' if active.stopping_at else 'running'}",
        f"elapsed_seconds={elapsed}",
    ]
    if active.stopping_at:
        lines.append(f"interrupting_seconds={int(time.time() - active.stopping_at)}")
    if preview:
        lines.append(f"active_input={preview}")
    return "\n".join(lines)


@dataclass(frozen=True)
class IncomingMessage:
    from_user: str
    msg_type: str
    content: str
    msg_id: str
