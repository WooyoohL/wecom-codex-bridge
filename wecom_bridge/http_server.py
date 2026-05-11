import urllib.parse
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler

from wecom_bridge.audit import audit_event
from wecom_bridge.config import Config
from wecom_bridge.models import IncomingMessage, format_active_turn_status
from wecom_bridge.wecom.crypto import WeComCrypto
from wecom_bridge.wecom.sender import WeComSender
from wecom_bridge.worker import MessageWorker


def parse_xml(text: str) -> dict[str, str]:
    root = ET.fromstring(text)
    return {child.tag: child.text or "" for child in root}

class BridgeState:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.crypto = WeComCrypto(config.token, config.encoding_aes_key, config.corp_id)
        self.sender = WeComSender(config)
        self.worker = MessageWorker(config, self.sender)


class Handler(BaseHTTPRequestHandler):
    state: BridgeState

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            worker = self.state.worker
            active = worker._active_snapshot()
            model_override = ""
            reasoning_effort_override = ""
            workdir = ""
            if worker.remote_client:
                model_override = worker.remote_client.model_override or ""
                reasoning_effort_override = worker.remote_client.reasoning_effort_override or ""
                workdir = worker.remote_client.workdir
            self.respond_text(
                200,
                "ok\n"
                f"backend={self.state.config.codex_backend}\n"
                f"command_prefix={self.state.config.bridge_command_prefix}\n"
                f"security_profile={self.state.config.bridge_security_profile}\n"
                f"allowed_users={len(self.state.config.allowed_wecom_users)}\n"
                "confirmation_required="
                f"{self.state.config.dangerous_commands_require_confirmation}\n"
                f"queue_size={worker.queue.qsize()}\n"
                f"recent_outgoing={len(worker.recent_outgoing)}\n"
                f"model_override={model_override}\n"
                f"reasoning_effort_override={reasoning_effort_override}\n"
                f"cwd={workdir}\n"
                f"{format_active_turn_status(active)}\n",
            )
            return
        if parsed.path != "/wecom/callback":
            self.send_error(404)
            return

        query = urllib.parse.parse_qs(parsed.query)
        try:
            signature = one(query, "msg_signature")
            timestamp = one(query, "timestamp")
            nonce = one(query, "nonce")
            echostr = one(query, "echostr")
            self.state.crypto.verify_signature(signature, timestamp, nonce, echostr)
            plaintext = self.state.crypto.decrypt(echostr)
            self.respond_text(200, plaintext)
        except Exception as exc:
            self.respond_text(400, f"bad wecom callback verification: {exc}\n")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/wecom/callback":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        query = urllib.parse.parse_qs(parsed.query)
        try:
            encrypted = parse_xml(body)["Encrypt"]
            self.state.crypto.verify_signature(
                one(query, "msg_signature"),
                one(query, "timestamp"),
                one(query, "nonce"),
                encrypted,
            )
            message = parse_xml(self.state.crypto.decrypt(encrypted))
            incoming = IncomingMessage(
                from_user=message.get("FromUserName", ""),
                msg_type=message.get("MsgType", ""),
                content=message.get("Content", ""),
                msg_id=message.get("MsgId", ""),
            )
            if not self.state.config.is_user_allowed(incoming.from_user):
                audit_event(
                    self.state.config,
                    "unauthorized_message",
                    user=incoming.from_user,
                    msg_type=incoming.msg_type,
                    msg_id=incoming.msg_id,
                )
                print(
                    f"skip unauthorized wecom message: from={incoming.from_user} "
                    f"type={incoming.msg_type} msg_id={incoming.msg_id}",
                    flush=True,
                )
                self.respond_text(200, "success")
                return
            print(
                f"received wecom message: from={incoming.from_user} "
                f"type={incoming.msg_type} msg_id={incoming.msg_id}",
                flush=True,
            )
            audit_event(
                self.state.config,
                "message_received",
                user=incoming.from_user,
                msg_type=incoming.msg_type,
                msg_id=incoming.msg_id,
            )
            self.state.worker.submit(incoming)
            self.respond_text(200, "success")
        except Exception as exc:
            self.respond_text(400, f"bad wecom message: {exc}\n")

    def respond_text(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.client_address[0]} - {fmt % args}", flush=True)


def one(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key)
    if not values:
        raise ValueError(f"missing {key}")
    return values[0]
