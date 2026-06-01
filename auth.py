"""
LinkedIn OAuth 2.0 3-legged flow.

Opens the browser to the LinkedIn authorization URL, runs a local callback
listener on localhost:8000, exchanges the auth code for an access token,
saves the token to token.json, and prints it to stdout.
"""
import http.server
import json
import socketserver
import sys
import threading
import time
import urllib.parse
import webbrowser

import requests

import config

CALLBACK_PORT = 8000
CALLBACK_PATH = "/callback"


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the OAuth authorization code from the redirect URL."""

    auth_code = None
    auth_error = None

    def do_GET(self):  # noqa: N802 (http.server naming)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        if "error" in params:
            _CallbackHandler.auth_error = params.get(
                "error_description", ["unknown"]
            )[0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                f"<html><body><h1>Authorization failed</h1>"
                f"<p>{_CallbackHandler.auth_error}</p>"
                f"<p>You can close this tab.</p></body></html>".encode()
            )
            return

        code = params.get("code", [None])[0]
        if code:
            _CallbackHandler.auth_code = code
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h1>Authorization complete</h1>"
                b"<p>You can close this tab and return to the terminal.</p>"
                b"</body></html>"
            )
        else:
            _CallbackHandler.auth_error = "no code in callback"
            self.send_response(400)
            self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 (silence server logs)
        return


def _build_auth_url():
    params = {
        "response_type": "code",
        "client_id": config.CLIENT_ID,
        "redirect_uri": config.REDIRECT_URI,
        "scope": config.OAUTH_SCOPES,
        "state": "linkedin_bid_optimizer",
    }
    return f"{config.AUTH_URL}?{urllib.parse.urlencode(params)}"


def _exchange_code_for_token(code):
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.REDIRECT_URI,
        "client_id": config.CLIENT_ID,
        "client_secret": config.CLIENT_SECRET,
    }
    response = requests.post(
        config.TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Token exchange failed: {response.status_code} {response.text}"
        )
    return response.json()


def run_oauth():
    """Drive the full 3-legged OAuth flow end to end."""
    config.ensure_dirs()

    server = socketserver.TCPServer(("localhost", CALLBACK_PORT), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    auth_url = _build_auth_url()
    print(f"Opening browser to: {auth_url}")
    print(f"If the browser does not open, paste the URL manually.")
    webbrowser.open(auth_url)

    print("Waiting for authorization callback on http://localhost:8000/callback ...")
    # Poll for the callback handler to fill in the code
    import time

    timeout_s = 300
    start = time.time()
    while (
        _CallbackHandler.auth_code is None
        and _CallbackHandler.auth_error is None
    ):
        if time.time() - start > timeout_s:
            server.shutdown()
            raise TimeoutError("OAuth authorization timed out after 5 minutes")
        time.sleep(0.25)

    server.shutdown()

    if _CallbackHandler.auth_error:
        raise RuntimeError(f"OAuth error: {_CallbackHandler.auth_error}")

    print("Authorization code received. Exchanging for access token ...")
    token_payload = _exchange_code_for_token(_CallbackHandler.auth_code)

    # Record issue time so we can track expiry accurately later
    issued_at = time.time()
    token_payload["issued_at_epoch"] = issued_at
    if token_payload.get("expires_in"):
        token_payload["expires_at_epoch"] = issued_at + token_payload["expires_in"]

    with open(config.TOKEN_FILE, "w") as f:
        json.dump(token_payload, f, indent=2)

    print(f"Access token saved to {config.TOKEN_FILE}")
    print(f"Access token: {token_payload.get('access_token')}")
    expires_in = token_payload.get("expires_in")
    if expires_in:
        print(f"Expires in:   {expires_in} seconds ({expires_in // 86400} days)")
    return token_payload


if __name__ == "__main__":
    try:
        run_oauth()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
