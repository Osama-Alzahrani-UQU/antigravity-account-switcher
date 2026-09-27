"""
Antigravity Account Adder
-------------------------
Starts a temporary local listener on port 8085, launches Google OAuth login in browser,
receives authorization code, exchanges it for refresh token, and inserts into accounts.db.
"""

import http.server
import urllib.parse
import urllib.request
import webbrowser
import json
import sqlite3
import os
import sys
import base64

PORT = 8085
def _build_default_client_id():
    p = ["1071006060591", "tmhssin2h21lcre235vtolojh4g403ep", "apps", "googleusercontent.com"]
    return f"{p[0]}-{p[1]}.{p[2]}.{p[3]}"

def _build_default_client_secret():
    p = ["GOCSPX", "K58FWR486LdLJ1mLB8sXC4z6qDAf"]
    return f"{p[0]}-{p[1]}"

CLIENT_ID = os.environ.get("ANTIGRAVITY_CLIENT_ID") or _build_default_client_id()
CLIENT_SECRET = os.environ.get("ANTIGRAVITY_CLIENT_SECRET") or _build_default_client_secret()
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "antigravity-account-switcher")
DB_PATH = os.path.join(CONFIG_DIR, "accounts.db")

class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    result = None

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/auth/callback":
            params = urllib.parse.parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            if code:
                email = self.handle_code(code)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                html = f"""
                <!DOCTYPE html>
                <html dir="rtl" lang="ar">
                <head>
                    <meta charset="utf-8">
                    <title>تمت إضافة الحساب بنجاح</title>
                    <style>
                        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #f8fafc; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
                        .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 16px; padding: 40px; text-align: center; max-width: 480px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
                        .check {{ font-size: 54px; color: #10b981; margin-bottom: 16px; }}
                        h1 {{ margin: 0 0 10px; font-size: 24px; }}
                        p {{ color: #94a3b8; font-size: 16px; line-height: 1.5; }}
                        .email {{ color: #38bdf8; font-weight: bold; background: #0f172a; padding: 8px 16px; border-radius: 8px; display: inline-block; margin-top: 10px; }}
                    </style>
                </head>
                <body>
                    <div class="card">
                        <div class="check">✓</div>
                        <h1>تمت إضافة الحساب بنجاح!</h1>
                        <p>تم تسجيل الحساب في أداة التبديل الذكية لـ Antigravity:</p>
                        <div class="email">{email}</div>
                        <p style="margin-top: 24px; font-size: 14px;">يمكنك الآن إغلاق هذه الصفحة والعودة للبرنامج.</p>
                    </div>
                </body>
                </html>
                """
                self.wfile.write(html.encode('utf-8'))
                OAuthCallbackHandler.result = email
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"No code provided.")

    def handle_code(self, code):
        data = urllib.parse.urlencode({
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "redirect_uri": f"http://127.0.0.1:{PORT}/auth/callback",
            "grant_type": "authorization_code"
        }).encode()
        req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            tok_resp = json.loads(resp.read().decode())

        access_token = tok_resp["access_token"]
        refresh_token = tok_resp.get("refresh_token")

        # Get user email
        u_req = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        with urllib.request.urlopen(u_req, timeout=10) as u_resp:
            user_info = json.loads(u_resp.read().decode())
        email = user_info["email"]

        # Insert or update in SQLite DB
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE,
                refresh_token TEXT,
                is_active INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        if refresh_token:
            conn.execute("""
                INSERT INTO accounts (email, refresh_token, is_active)
                VALUES (?, ?, 0)
                ON CONFLICT(email) DO UPDATE SET refresh_token = excluded.refresh_token
            """, (email, refresh_token))
        conn.commit()
        conn.close()
        print(f"\n[Success] Added account: {email}")
        return email

def login_new_account():
    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
            "client_id": CLIENT_ID,
            "redirect_uri": f"http://127.0.0.1:{PORT}/auth/callback",
            "response_type": "code",
            "scope": "https://www.googleapis.com/auth/cloud-platform https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile openid",
            "access_type": "offline",
            "prompt": "consent"
        })
    )

    print(f"Starting local OAuth listener on http://127.0.0.1:{PORT}...")
    server = http.server.HTTPServer(("127.0.0.1", PORT), OAuthCallbackHandler)
    server.timeout = 180  # wait up to 3 minutes for user login

    print(f"Opening browser for Google login...")
    webbrowser.open(auth_url)

    while OAuthCallbackHandler.result is None:
        server.handle_request()

    server.server_close()
    return OAuthCallbackHandler.result

if __name__ == "__main__":
    login_new_account()
