import json
import os
import time
from typing import Any

from wecom_bridge.config import Config


def audit_event(config: Config, event: str, **fields: Any) -> None:
    path = config.bridge_audit_log_file
    if not path:
        return
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "event": event,
        **fields,
    }
    directory = os.path.dirname(path)
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"audit log write failed: {exc}", flush=True)
