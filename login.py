# =============================================================================
# login.py — Fyers Login
# =============================================================================
#
# WHAT THIS FILE DOES:
#   Logs you into Fyers and saves an access token to access_token.txt.
#   You run this ONCE each morning before starting the dashboard.
#   The token lasts the whole trading day and expires at midnight.
#
# HOW TO RUN:
#   cd ~/fyers_bot
#   python login.py
#
#   It will open a browser window. Log in with your Fyers credentials.
#   After login, come back to the terminal — it saves the token automatically.
#
# =============================================================================

import os
import sys
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from fyers_apiv3 import fyersModel
from config import CLIENT_ID, SECRET_KEY, REDIRECT_URI, ACCESS_TOKEN_FILE

# We will capture the redirect URL in this variable
captured_auth_code = None


class RedirectHandler(BaseHTTPRequestHandler):
    """
    A tiny local web server that catches the redirect from Fyers after login.

    When you log in to Fyers in the browser, Fyers redirects your browser to
    http://127.0.0.1:5000/?auth_code=XXXXXXXX
    This handler catches that redirect and extracts the auth_code.
    """

    def do_GET(self):
        global captured_auth_code

        # Parse the URL to extract auth_code from query string
        query = parse_qs(urlparse(self.path).query)
        auth_code = query.get("auth_code", [None])[0]

        if auth_code:
            captured_auth_code = auth_code
            # Send a simple success page to the browser
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"""
                <html><body style="font-family:sans-serif;text-align:center;padding:60px;background:#0a0e14;color:#00d4aa">
                <h2>Login successful!</h2>
                <p style="color:#e8edf5">You can close this browser tab and return to Terminal.</p>
                </body></html>
            """)
        else:
            self.send_response(400)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress default request logs (keeps terminal clean)
        pass


def login():
    """
    Runs the complete Fyers OAuth login flow.

    Step 1: Generate a login URL
    Step 2: Open it in your browser
    Step 3: You log in → Fyers redirects to localhost:5000 with an auth_code
    Step 4: We catch the auth_code and exchange it for an access_token
    Step 5: Save the access_token to access_token.txt
    """

    print("\n" + "="*55)
    print("  OPTIONS RADAR — Fyers Login")
    print("="*55)

    # --- Step 1: Generate the login URL ---
    session = fyersModel.SessionModel(
        client_id=CLIENT_ID,
        secret_key=SECRET_KEY,
        redirect_uri=REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code",
    )
    login_url = session.generate_authcode()

    # --- Step 2: Open the browser ---
    print(f"\nOpening browser for Fyers login...")
    print(f"If it doesn't open automatically, visit this URL:\n{login_url}\n")
    webbrowser.open(login_url)

    # --- Step 3: Start local server to catch the redirect ---
    print("Waiting for you to log in...")
    port = int(REDIRECT_URI.split(":")[-1].split("/")[0])
    server = HTTPServer(("127.0.0.1", port), RedirectHandler)

    # Handle requests until we get the auth_code
    while captured_auth_code is None:
        server.handle_request()

    print(f"Auth code received.")

    # --- Step 4: Exchange auth_code for access_token ---
    session.set_token(captured_auth_code)
    response = session.generate_token()

    if "access_token" not in response:
        print(f"\n[ERROR] Could not get access token: {response}")
        sys.exit(1)

    access_token = response["access_token"]

    # --- Step 5: Save token to file ---
    with open(ACCESS_TOKEN_FILE, "w") as f:
        f.write(access_token)

    print(f"\nAccess token saved to: {ACCESS_TOKEN_FILE}")
    print("You can now run:  python main.py\n")
    return access_token


if __name__ == "__main__":
    login()
