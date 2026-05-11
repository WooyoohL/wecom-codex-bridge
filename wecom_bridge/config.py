import os
from dataclasses import dataclass


DEFAULT_SLASH_COMMANDS_URL = (
    "https://raw.githubusercontent.com/openai/codex/main/"
    "codex-rs/tui/src/slash_command.rs"
)

SECURITY_PROFILES = {
    "safe": ("on-request", "read-only"),
    "dev": ("on-request", "workspace-write"),
    "personal": ("never", "danger-full-access"),
}


def load_env_file(path: str, *, override: bool = True) -> None:
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and (override or key not in os.environ):
                os.environ[key] = value


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def parse_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise SystemExit(f"Invalid boolean value: {value}")


def security_defaults(profile: str) -> tuple[str, str]:
    try:
        return SECURITY_PROFILES[profile]
    except KeyError as exc:
        options = ", ".join(sorted(SECURITY_PROFILES))
        raise SystemExit(f"Invalid BRIDGE_SECURITY_PROFILE: {profile}. Expected one of: {options}") from exc


@dataclass(frozen=True)
class Config:
    corp_id: str
    corp_secret: str
    agent_id: int
    token: str
    encoding_aes_key: str
    to_user: str
    host: str
    port: int
    codex_backend: str
    codex_workdir: str | None
    codex_timeout_seconds: int
    codex_remote_thread_id: str | None
    codex_remote_state_file: str
    codex_remote_model_state_file: str
    codex_remote_reasoning_state_file: str
    codex_remote_workdir_state_file: str
    codex_remote_approval_policy: str
    codex_remote_sandbox: str
    codex_thread_list_limit: int
    codex_slash_commands_url: str
    codex_slash_commands_cache_seconds: int
    bridge_command_prefix: str
    bridge_security_profile: str
    allowed_wecom_users: tuple[str, ...]
    allowed_bridge_commands: tuple[str, ...]
    dangerous_commands_require_confirmation: bool
    bridge_audit_log_file: str
    bridge_forward_thought_summary: bool
    bridge_forward_tool_progress: bool
    bridge_forward_file_changes: bool

    @classmethod
    def from_env(cls) -> "Config":
        workdir = os.environ.get("CODEX_WORKDIR") or None
        security_profile = os.environ.get("BRIDGE_SECURITY_PROFILE", "dev").strip().casefold()
        default_approval_policy, default_sandbox = security_defaults(security_profile)
        to_user = require_env("WECOM_TO_USER")
        allowed_users = parse_csv(os.environ.get("ALLOWED_WECOM_USERS")) or (to_user,)
        return cls(
            corp_id=require_env("WECOM_CORP_ID"),
            corp_secret=require_env("WECOM_CORP_SECRET"),
            agent_id=int(require_env("WECOM_AGENT_ID")),
            token=require_env("WECOM_TOKEN"),
            encoding_aes_key=require_env("WECOM_ENCODING_AES_KEY"),
            to_user=to_user,
            host=os.environ.get("BRIDGE_HOST", "127.0.0.1"),
            port=int(os.environ.get("BRIDGE_PORT", "8000")),
            codex_backend=os.environ.get("CODEX_BACKEND", "codex_remote_control"),
            codex_workdir=workdir,
            codex_timeout_seconds=int(os.environ.get("CODEX_TIMEOUT_SECONDS", "300")),
            codex_remote_thread_id=os.environ.get("CODEX_REMOTE_THREAD_ID") or None,
            codex_remote_state_file=os.environ.get(
                "CODEX_REMOTE_STATE_FILE", ".codex_wecom_thread_id"
            ),
            codex_remote_model_state_file=os.environ.get(
                "CODEX_REMOTE_MODEL_STATE_FILE", ".codex_wecom_model"
            ),
            codex_remote_reasoning_state_file=os.environ.get(
                "CODEX_REMOTE_REASONING_STATE_FILE", ".codex_wecom_reasoning"
            ),
            codex_remote_workdir_state_file=os.environ.get(
                "CODEX_REMOTE_WORKDIR_STATE_FILE", ".codex_wecom_workdir"
            ),
            codex_remote_approval_policy=(
                os.environ.get("CODEX_REMOTE_APPROVAL_POLICY") or default_approval_policy
            ),
            codex_remote_sandbox=os.environ.get("CODEX_REMOTE_SANDBOX") or default_sandbox,
            codex_thread_list_limit=int(os.environ.get("CODEX_THREAD_LIST_LIMIT", "20")),
            codex_slash_commands_url=os.environ.get(
                "CODEX_SLASH_COMMANDS_URL", DEFAULT_SLASH_COMMANDS_URL
            ),
            codex_slash_commands_cache_seconds=int(
                os.environ.get("CODEX_SLASH_COMMANDS_CACHE_SECONDS", "21600")
            ),
            bridge_command_prefix=os.environ.get("BRIDGE_COMMAND_PREFIX", "!"),
            bridge_security_profile=security_profile,
            allowed_wecom_users=allowed_users,
            allowed_bridge_commands=parse_csv(os.environ.get("ALLOWED_COMMANDS")),
            dangerous_commands_require_confirmation=parse_bool(
                os.environ.get("DANGEROUS_COMMANDS_REQUIRE_CONFIRMATION"),
                default=True,
            ),
            bridge_audit_log_file=os.environ.get("BRIDGE_AUDIT_LOG_FILE", "logs/audit.jsonl"),
            bridge_forward_thought_summary=parse_bool(
                os.environ.get("BRIDGE_FORWARD_THOUGHT_SUMMARY"),
                default=True,
            ),
            bridge_forward_tool_progress=parse_bool(
                os.environ.get("BRIDGE_FORWARD_TOOL_PROGRESS"),
                default=False,
            ),
            bridge_forward_file_changes=parse_bool(
                os.environ.get("BRIDGE_FORWARD_FILE_CHANGES"),
                default=False,
            ),
        )

    def is_user_allowed(self, user_id: str) -> bool:
        return not self.allowed_wecom_users or user_id in self.allowed_wecom_users
