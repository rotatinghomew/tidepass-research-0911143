import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .cases import CaseStore
from .catalog import Catalog
from .domain import overview
from .profile import numeric_profiles
from .transmission.analysis import run_analysis
from .transmission.series import (
    build_series,
    known_dimensions,
    select_routes,
    series_overview,
    trade_evidence,
)


class Application:
    def __init__(self, data, state):
        self.catalog = Catalog(data)
        self.cases = CaseStore(state)

    def _transmission_series(self, query):
        def single(name, default=None):
            return query[name][0] if name in query else default

        def plural(name):
            return query[name] if name in query else None

        route_ids = plural("route_id")
        routes = select_routes(
            self.catalog, species=single("species"), grade=single("grade"),
            region=single("region"), route_ids=route_ids)
        quote_kind = single("quote_kind", "挂牌")
        match = single("match", "same_day")
        try:
            window = int(single("window", "2"))
        except ValueError as error:
            raise ValueError("窗口半径需为整数") from error
        if not 1 <= window <= 7:
            raise ValueError("窗口匹配半径需在一至七天之间")
        if quote_kind not in ("挂牌", "询价", "any"):
            raise ValueError("报价类型需为挂牌、询价或 any")
        if match not in ("same_day", "window", "trade_day"):
            raise ValueError("未知匹配方式：" + match)
        output = []
        for route in routes:
            series = build_series(self.catalog, route, quote_kind=quote_kind,
                                  match=match, window=window)
            entry = series_overview(self.catalog, series)
            if single("evidence") == "1":
                entry["trade_evidence"] = trade_evidence(self.catalog, route, series["paired_days"])
            output.append(entry)
        return {"source_fingerprint": self.catalog.fingerprint,
                "quote_kind": quote_kind, "match": match, "window": window,
                "series": output}

    def dispatch(self, method, target, body=None):
        parsed = urlsplit(target)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return 200, {"status": "ok", "source_fingerprint": self.catalog.fingerprint}
            if method == "GET" and path == "/tables":
                return 200, {"tables": self.catalog.describe()}
            if method == "GET" and path.startswith("/tables/"):
                name = path.split("/")[-1]
                offset = int(query.get("offset", ["0"])[0])
                limit = int(query.get("limit", ["50"])[0])
                if offset < 0 or not 1 <= limit <= 500:
                    raise ValueError("分页范围无效")
                records = self.catalog.rows(name)
                return 200, {"total": len(records), "offset": offset, "rows": records[offset:offset + limit]}
            if method == "GET" and path == "/view":
                return 200, {"source_fingerprint": self.catalog.fingerprint, "view": overview(self.catalog)}
            if method == "GET" and path == "/profiles":
                return 200, {"source_fingerprint": self.catalog.fingerprint, "profiles": numeric_profiles(self.catalog)}
            if method == "GET" and path == "/transmission/dimensions":
                return 200, {"source_fingerprint": self.catalog.fingerprint,
                             "dimensions": known_dimensions(self.catalog)}
            if method == "GET" and path == "/transmission/series":
                return 200, self._transmission_series(query)
            if method == "POST" and path == "/transmission/analyze":
                if not isinstance(body, dict):
                    raise ValueError("请求内容应为对象")
                result = run_analysis(self.catalog, body)
                return 200, result
            if method == "GET" and path == "/cases":
                return 200, {"cases": self.cases.list()}
            if method == "GET" and path.startswith("/cases/"):
                return 200, self.cases.read(path.split("/")[-1])
            if method == "POST" and path == "/cases":
                if not isinstance(body, dict):
                    raise ValueError("请求内容应为对象")
                if set(body) - {"label", "options", "notes"}:
                    raise ValueError("请求包含未知字段")
                return 201, self.cases.create(self.catalog, body.get("label"), body.get("options"), body.get("notes", ""))
        except KeyError:
            return 404, {"error": "记录不存在"}
        except (TypeError, ValueError) as error:
            return 400, {"error": str(error)}
        return 404, {"error": "接口不存在"}


def make_server(application, host="127.0.0.1", port=8080):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond("GET")

        def do_POST(self):
            self.respond("POST")

        def respond(self, method):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 262144:
                    raise ValueError("请求体过大")
                body = json.loads(self.rfile.read(length)) if length else None
                status, content = application.dispatch(method, self.path, body)
            except (ValueError, UnicodeDecodeError):
                status, content = 400, {"error": "请求格式无效"}
            encoded = json.dumps(content, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format, *args):
            pass

    return ThreadingHTTPServer((host, port), Handler)
