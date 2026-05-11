import argparse
from http.server import ThreadingHTTPServer

from wecom_bridge.config import Config, load_env_file
from wecom_bridge.http_server import BridgeState, Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", help="Optional KEY=VALUE env file")
    args = parser.parse_args()
    if args.env_file:
        load_env_file(args.env_file)
    config = Config.from_env()

    Handler.state = BridgeState(config)
    server = ThreadingHTTPServer((config.host, config.port), Handler)
    print(f"listening on http://{config.host}:{config.port}", flush=True)
    print(f"backend={config.codex_backend}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
