"""Read-only TLS/ngrok diagnostics. Never creates a job or prints capability URLs.

--base probes only public root and a deliberately INVALID manifest path.
--command-file can probe a real signed command obtained separately from Django.
This is a PC probe, not ESP32 TLS/OTA validation and never writes flash.
"""
import argparse
import json
import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener
from http.client import HTTPException


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe(url, role):
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("An HTTPS URL without credentials/query/fragment is required")
    opener = build_opener(HTTPSHandler(context=ssl.create_default_context()), NoRedirect())
    request = Request(url, headers={"ngrok-skip-browser-warning": "1", "Accept-Encoding": "identity",
                                   "Accept": "application/json", "User-Agent": "SmartCharge-OTA/1"})
    try:
        try:
            response = opener.open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            prefix = response.read(1024)
            code = re.search(rb"ERR_NGROK_[0-9]+", prefix)
            result = dict(role=role, hostname=parts.hostname, tls_verified=True,
                          status=response.code, content_type=response.headers.get("Content-Type", ""),
                          content_length=response.headers.get("Content-Length", ""),
                          content_encoding=response.headers.get("Content-Encoding", "identity"),
                          transfer_encoding=response.headers.get("Transfer-Encoding", ""),
                          html_prefix=prefix.lstrip().lower().startswith((b"<!doctype html", b"<html")),
                          json_prefix=prefix.lstrip().startswith(b"{"),
                          ngrok_error=code.group().decode() if code else None)
            return result
    except URLError as error:
        return dict(role=role, hostname=parts.hostname, tls_verified=False, error=type(error.reason).__name__)
    except (TimeoutError, ssl.SSLError, OSError, ValueError, HTTPException) as error:
        return dict(role=role, hostname=parts.hostname, tls_verified=False, error=type(error).__name__)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base")
    p.add_argument("--command-file", help="Local command JSON; capability URL is never printed")
    a = p.parse_args()
    if bool(a.base) == bool(a.command_file):
        p.error("Choose --base or --command-file")
    if a.base:
        base = a.base.rstrip("/")
        results = [probe(base + "/", "public_root"),
                   probe(base + "/firmware/manifest/probe-only/no-capability/", "invalid_manifest_expected_denial")]
    else:
        with open(a.command_file, encoding="utf-8-sig") as f:
            command = json.load(f)
        results = [probe(command["manifest_url"], "manifest_headers_only")]
    print(json.dumps(results, indent=2))
    usable = all(r.get("tls_verified") and not r.get("ngrok_error") for r in results)
    if a.base:
        usable = usable and results[0].get("status", 500)<500
    else:
        r=results[0]
        length=r.get("content_length", "")
        usable = usable and r.get("status")==200 and r.get("json_prefix") and not r.get("html_prefix") and \
            r.get("content_type", "").split(";",1)[0].lower()=="application/json" and \
            length.isdigit() and 0<int(length)<=2048 and not r.get("transfer_encoding") and \
            r.get("content_encoding")=="identity"
    return 0 if usable else 1


if __name__ == "__main__":
    raise SystemExit(main())
