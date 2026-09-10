"""Opt-in, bounded LX search/resolve smoke against a separately started test service.

Supply a keyword for audio you are authorized to access. URLs and response bodies
are deliberately omitted from the report. This is not proof of full-track playback.
"""
import argparse
import datetime
import json
import time
import urllib.error
import urllib.parse
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8773")
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--sources", default="kg,wy,mg,tx,kw")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        parser.error("base URL must be HTTP(S) without credentials")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, params):
        url = base + path + "?" + urllib.parse.urlencode(params)
        with opener.open(url, timeout=20) as response:
            return json.loads(response.read(1024 * 1024))

    for source in args.sources.split(","):
        source = source.strip()
        if source not in {"kg", "wy", "mg", "tx", "kw"}:
            parser.error("unknown LX source")
        started = time.monotonic()
        report = {"time": datetime.datetime.now(datetime.timezone.utc).isoformat(), "source": source,
                  "search": "fail", "resolve": "not_run", "full_track_verified": False}
        try:
            result = request("/api/v1/search", {"keyword": args.keyword, "sources": source, "limit": 1})
            items = result.get("items") or []
            report["search"] = "results" if items else "empty"
            report["count"] = len(items)
            if items:
                resolved = request("/api/v1/track/url", {"id": items[0]["id"], "quality": "standard"})
                data = resolved.get("data") or {}
                report["resolve"] = "url_returned" if data.get("url") else "no_url"
                report["validation_status"] = data.get("validation_status", "unspecified")
                report["completeness"] = data.get("completeness", "unknown")
                report["actual_tier"] = data.get("actual_tier", "unknown")
        except (OSError, ValueError, KeyError) as exc:
            report["error_type"] = type(exc).__name__
        report["elapsed_s"] = round(time.monotonic() - started, 3)
        print(json.dumps(report, ensure_ascii=False), flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main()
