"""A local fake of Anthropic's plan-usage endpoint (GET /api/oauth/usage), for the usage tests.

It listens on 127.0.0.1 on a free port, in a background thread, and never touches the network. It can answer with
a configurable JSON reply, a sequence of replies (one per request, the last one repeated, e.g. numbers that rise
between calls), an error status, a body that isn't JSON, or a delay longer than the client's time-out. Every request
it gets is recorded (method, path, headers with lower-case names) so tests can check what was sent.

Importable from tests/ and tests/e2e/ (the e2e tests add the tests folder to sys.path).

    with FakeUsageServer(reply=usage_reply(37, 82)) as srv:
        env["REGISTA_USAGE_URL"] = srv.url
        ...
        assert srv.requests[0]["headers"]["authorization"] == "Bearer " + FAKE_TOKEN
"""
import http.server
import json
import os
import socket
import threading
import time

FAKE_TOKEN = "sk-ant-oat-FAKE-TOKEN-123"

# 6 November 2026 is a Friday; 05:00 UTC is 07:00 in Bucharest (EET, UTC+2, in November).
WEEK_RESETS_AT = "2026-11-06T05:00:00+00:00"
WEEK_RESETS_TEXT_BUCHAREST = "Friday 6 November, 07:00"
# 3 November 2026 is a Tuesday; 15:00 UTC is 17:00 in Bucharest.
SESSION_RESETS_AT = "2026-11-03T15:00:00+00:00"
SESSION_RESETS_TEXT_BUCHAREST = "Tuesday 3 November, 17:00"


def usage_reply(session_pct=37.0, week_pct=82.0, session_resets=SESSION_RESETS_AT, week_resets=WEEK_RESETS_AT,
                five_hour=True, seven_day=True, limits=False):
    """A reply shaped like the real endpoint's. five_hour/seven_day: include those objects (utilization,
    resets_at). limits: also include the `limits` list (kind session, weekly_all, weekly_scoped)."""
    out = {}
    if five_hour:
        out["five_hour"] = {"utilization": float(session_pct), "resets_at": session_resets}
    if seven_day:
        out["seven_day"] = {"utilization": float(week_pct), "resets_at": week_resets}
    if limits:
        out["limits"] = limits_list(session_pct, week_pct, session_resets, week_resets)
    return out


def limits_list(session_pct, week_pct, session_resets=SESSION_RESETS_AT, week_resets=WEEK_RESETS_AT,
                scoped_pct=3.0):
    """The `limits` list: a weekly_scoped item comes first so a reader that takes the first weekly item is caught."""
    return [
        {"kind": "weekly_scoped", "percent": float(scoped_pct), "resets_at": week_resets},
        {"kind": "session", "percent": float(session_pct), "resets_at": session_resets},
        {"kind": "weekly_all", "percent": float(week_pct), "resets_at": week_resets},
    ]


def rising_replies(count=5, session_start=10.0, week_start=40.0, session_step=2.0, week_step=0.5):
    """`count` replies whose session and weekly percentages rise by a fixed step from one call to the next."""
    return [usage_reply(session_start + i * session_step, week_start + i * week_step) for i in range(count)]


def write_credentials(path, token=FAKE_TOKEN):
    """Write a Claude Code credentials file (claudeAiOauth.accessToken) at `path`; token None leaves it out."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    oauth = {"refreshToken": "sk-ant-ort-FAKE-REFRESH", "expiresAt": 4102444800000, "scopes": ["user:inference"]}
    if token is not None:
        oauth["accessToken"] = token
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"claudeAiOauth": oauth}, f)
    return path


def closed_port_url():
    """An address on 127.0.0.1 where nothing listens (the port was free a moment ago)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/api/oauth/usage"


class FakeUsageServer:
    """The fake endpoint. Options (all can be changed later with configure()):
    reply    -- the JSON object to send (default usage_reply())
    replies  -- a list of JSON objects, one per request in order; the last is repeated once they run out
    status   -- the HTTP status (default 200)
    body     -- raw text to send instead of JSON (e.g. "not json")
    delay    -- seconds to sleep before answering
    echo_token -- with an error status, put the request's Authorization header in the error body (to check the
                  client never repeats what the server says)
    """

    def __init__(self, reply=None, replies=None, status=200, body=None, delay=0.0, echo_token=False):
        self.requests = []
        self._lock = threading.Lock()
        self._served = 0
        self.configure(reply=reply if reply is not None else usage_reply(), replies=replies, status=status,
                       body=body, delay=delay, echo_token=echo_token)
        self._httpd = None
        self._thread = None

    def configure(self, **opts):
        for k, v in opts.items():
            setattr(self, k, v)
        if "replies" in opts:
            self._served = 0
        return self

    # --- lifecycle

    def start(self):
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self):
                with owner._lock:
                    owner.requests.append({"method": self.command, "path": self.path,
                                           "headers": {k.lower(): v for k, v in self.headers.items()}})
                    if owner.replies:
                        reply = owner.replies[min(owner._served, len(owner.replies) - 1)]
                    else:
                        reply = owner.reply
                    owner._served += 1
                    status, body, delay, echo = owner.status, owner.body, owner.delay, owner.echo_token
                if delay:
                    time.sleep(delay)
                if body is not None:
                    data = body.encode("utf-8")
                elif status >= 400:
                    err = {"type": "error", "error": {"type": "authentication_error", "message": "invalid token"}}
                    if echo:
                        err["error"]["message"] = "invalid token: " + str(self.headers.get("Authorization"))
                    data = json.dumps(err).encode("utf-8")
                else:
                    data = json.dumps(reply).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass  # the client gave up (time-out test)

            do_GET = _answer
            do_POST = _answer

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            block_on_close = False

        self._httpd = Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    @property
    def port(self):
        return self._httpd.server_address[1]

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/api/oauth/usage"

    @property
    def count(self):
        with self._lock:
            return len(self.requests)
