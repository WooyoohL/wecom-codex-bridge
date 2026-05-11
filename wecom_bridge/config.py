import os
from dataclasses import dataclass


DEFAULT_SLASH_COMMANDS_URL = (
    "https://raw.githubusercontent.com/openai/codex/main/"
    "codex-rs/tui/src/slash_command.rs"
)


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

    @classmethod
    def from_env(cls) -> "Config":
        workdir = os.environ.get("CODEX_WORKDIR") or None
        return cls(
            corp_id=require_env("WECOM_CORP_ID"),
            corp_secret=require_env("WECOM_CORP_SECRET"),
            agent_id=int(require_env("WECOM_AGENT_ID")),
            token=require_env("WECOM_TOKEN"),
            encoding_aes_key=require_env("WECOM_ENCODING_AES_KEY"),
            to_user=require_env("WECOM_TO_USER"),
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
            codex_remote_approval_policy=os.environ.get("CODEX_REMOTE_APPROVAL_POLICY", "never"),
            codex_remote_sandbox=os.environ.get("CODEX_REMOTE_SANDBOX", "danger-full-access"),
            codex_thread_list_limit=int(os.environ.get("CODEX_THREAD_LIST_LIMIT", "20")),
            codex_slash_commands_url=os.environ.get(
                "CODEX_SLASH_COMMANDS_URL", DEFAULT_SLASH_COMMANDS_URL
            ),
            codex_slash_commands_cache_seconds=int(
                os.environ.get("CODEX_SLASH_COMMANDS_CACHE_SECONDS", "21600")
            ),
            bridge_command_prefix=os.environ.get("BRIDGE_COMMAND_PREFIX", "!"),
        )
