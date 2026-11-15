import json
import tempfile
import threading
from urllib.request import Request, urlopen

from tidepass.service import Application, make_server


def main():
    with tempfile.TemporaryDirectory() as state:
        server = make_server(Application("data", state), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            for path in ("/health", "/tables", "/view",
                         "/transmission/dimensions",
                         "/transmission/series?route_id=R0&evidence=1"):
                with urlopen(base + path, timeout=5) as response:
                    print(path, json.dumps(json.load(response), ensure_ascii=False)[:240])
            analysis = Request(
                base + "/transmission/analyze",
                data=json.dumps({"route_ids": ["R0"], "max_lag": 2,
                                 "bootstrap_replications": 40,
                                 "models": ["controlled"], "windows": ["full"]},
                                ensure_ascii=False).encode(),
                headers={"Content-Type": "application/json"})
            with urlopen(analysis, timeout=20) as response:
                result = json.load(response)
            assert result["routes_analyzed"] == 1
            assert result["results"][0]["windows"][0]["models"][0]["cumulative"]
            assert result["interpretation"]
            print("传导分析完成", result["results"][0]["route"]["id"],
                  "自助区间",
                  result["results"][0]["windows"][0]["models"][0]
                  ["bootstrap_cumulative"]["profiles"][-1])
            request = Request(base + "/cases", data=json.dumps({"label": "月度经营讨论", "options": {"period": "2026-09"}}, ensure_ascii=False).encode(), headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=5) as response:
                created = json.load(response)
            with urlopen(base + "/cases/" + created["id"], timeout=5) as response:
                restored = json.load(response)
            assert created == restored
            print("研究记录已回读", restored["id"], restored["source_fingerprint"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
