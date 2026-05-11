from dataclasses import dataclass
from typing import Any

from wecom_bridge.codex.client import CodexAppServerClient


@dataclass(frozen=True)
class CodexThreadSummary:
    thread_id: str
    cwd: str
    preview: str
    name: str
    status: str

    @classmethod
    def from_remote(cls, raw: dict[str, Any]) -> "CodexThreadSummary | None":
        thread_id = str(raw.get("id") or "").strip()
        if not thread_id:
            return None
        status = raw.get("status") or ""
        if isinstance(status, dict):
            status = str(status.get("type") or "")
        else:
            status = str(status or "")
        return cls(
            thread_id=thread_id,
            cwd=str(raw.get("cwd") or ""),
            preview=" ".join(str(raw.get("preview") or "").split()),
            name=str(raw.get("name") or ""),
            status=status,
        )

    @property
    def short_id(self) -> str:
        return self.thread_id[:8]

    @property
    def cwd_tail(self) -> str:
        cwd = self.cwd.rstrip("/")
        return "/".join(cwd.split("/")[-2:]) if cwd else ""

    @property
    def menu_label(self) -> str:
        return self.name or self.short_id

    @property
    def menu_description(self) -> str:
        parts = []
        if self.name:
            parts.append(self.short_id)
        if self.cwd_tail:
            parts.append(self.cwd_tail)
        if self.status and self.status != "idle":
            parts.append(f"status={self.status}")
        if self.preview:
            parts.append(self.preview[:70])
        return " | ".join(parts)


class CodexThreadService:
    def __init__(self, client: CodexAppServerClient) -> None:
        self.client = client

    @property
    def thread_id(self) -> str | None:
        return self.client.thread_id

    @property
    def workdir(self) -> str:
        return self.client.workdir

    def list_recent(self, limit: int) -> list[CodexThreadSummary]:
        summaries: list[CodexThreadSummary] = []
        for raw in self.client.list_threads(limit=limit):
            summary = CodexThreadSummary.from_remote(raw)
            if summary:
                summaries.append(summary)
        return summaries

    def read_current(self) -> dict[str, Any]:
        return self.client.read_thread()

    def bind(self, thread_id: str) -> str:
        self.client.bind(thread_id)
        return thread_id

    def new(self) -> str:
        return self.client.new_thread()

    def fork_current(self) -> str:
        return self.client.fork_thread()

    def rename_current(self, name: str) -> str:
        return self.client.rename_thread(name)

    def archive_current(self) -> str:
        return self.client.archive_thread()

    def unarchive(self, thread_id: str) -> str:
        return self.client.unarchive_thread(thread_id)

    def rollback_current(self, num_turns: int) -> str:
        return self.client.rollback_thread(num_turns)

    def set_workdir(self, path: str) -> str:
        return self.client.set_workdir(path)

    def reset_workdir(self) -> str:
        return self.client.reset_workdir()

    def recent_history(self, limit: int) -> str:
        return self.client.recent_thread_history(limit)
