import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from wecom_bridge.config import Config


GET_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
SEND_MESSAGE_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"


class WeComSender:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._token: str | None = None
        self._token_expire_at = 0.0
        self._lock = threading.Lock()

    def send_text(self, content: str, to_user: str | None = None) -> None:
        target = to_user or self.config.to_user
        for chunk in split_message(content):
            self._send_text_chunk(chunk, target)

    def _send_text_chunk(self, chunk: str, target: str) -> None:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            payload = {
                "touser": target,
                "msgtype": "text",
                "agentid": self.config.agent_id,
                "text": {"content": chunk},
                "safe": 0,
            }
            try:
                token = self._access_token()
                query = urllib.parse.urlencode({"access_token": token})
                result = request_json(f"{SEND_MESSAGE_URL}?{query}", method="POST", payload=payload)
                if result.get("errcode") != 0:
                    with self._lock:
                        self._token = None
                        self._token_expire_at = 0.0
                    raise RuntimeError(
                        f"message/send failed: {json.dumps(result, ensure_ascii=False)}"
                    )
                print(f"sent text to {target}: {len(chunk)} chars", flush=True)
                return
            except Exception as exc:
                last_error = exc
                if attempt >= 3:
                    break
                time.sleep(attempt)
        raise RuntimeError(f"message/send failed after retries: {last_error}") from last_error

    def _access_token(self) -> str:
        with self._lock:
            now = time.time()
            if self._token and now < self._token_expire_at - 120:
                return self._token
            query = urllib.parse.urlencode(
                {"corpid": self.config.corp_id, "corpsecret": self.config.corp_secret}
            )
            result = request_json(f"{GET_TOKEN_URL}?{query}")
            if result.get("errcode") != 0:
                raise RuntimeError(f"gettoken failed: {json.dumps(result, ensure_ascii=False)}")
            self._token = result["access_token"]
            self._token_expire_at = now + int(result.get("expires_in", 7200))
            return self._token

def request_json(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error: {exc}") from exc


def split_message(content: str, limit: int = 1800) -> list[str]:
    if not content:
        return ["(empty response)"]

    chunks: list[str] = []
    current = ""
    for line in content.splitlines(keepends=True):
        if _byte_len(line) > limit:
            if current:
                chunks.append(current.rstrip("\n"))
                current = ""
            chunks.extend(_split_long_line(line, limit))
            continue
        if current and _byte_len(current) + _byte_len(line) > limit:
            chunks.append(current.rstrip("\n"))
            current = line
        else:
            current += line

    if current:
        chunks.append(current.rstrip("\n"))
    return chunks or ["(empty response)"]


def _split_long_line(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    current_bytes = 0
    for char in text:
        char_bytes = _byte_len(char)
        if current and current_bytes + char_bytes > limit:
            chunks.append(current)
            current = char
            current_bytes = char_bytes
        else:
            current += char
            current_bytes += char_bytes
    if current:
        chunks.append(current.rstrip("\n"))
    return chunks


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))
