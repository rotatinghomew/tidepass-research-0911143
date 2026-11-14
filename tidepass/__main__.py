import argparse
from pathlib import Path

from .service import Application, make_server


def main():
    parser = argparse.ArgumentParser(description="经营研究资料服务")
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--state", type=Path, default=Path("state"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    arguments = parser.parse_args()
    server = make_server(Application(arguments.data, arguments.state), arguments.host, arguments.port)
    print(f"资料服务已启动：{arguments.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
