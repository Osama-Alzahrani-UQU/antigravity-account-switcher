"""
Antigravity Zero-Interruption Smart Quota Proxy & Auto-Rotation Daemon
----------------------------------------------------------------------
1. Transparent high-performance reverse proxy for Google Cloud Code API (daily-cloudcode-pa.googleapis.com).
2. Attaches to running Antigravity language_server.exe via SetCloudCodeURL without restarting any processes.
3. Automatically rotates between accounts when 5-hour limit or weekly limit is depleted or on HTTP 429/403.
4. Switches active accounts with ZERO interruption to ongoing agent tasks.
5. Emits authentic Antigravity Hub User-Agent headers for 100% reliable quota polling and model streaming.
"""

import http.server
import http.client
import urllib.parse
import urllib.request
import threading
import ssl
import json
import sqlite3
import subprocess
import psutil
import re
import time
import os
import sys
import gzip
import base64
import signal
import ctypes
import ctypes.wintypes as wintypes
from datetime import datetime, timezone, timedelta

# UTF-8 stdout configuration for Windows CP1256 safety
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "daemon.log")

class DaemonLogger:
    def __init__(self, filepath):
        self.terminal = sys.stdout
        self.filepath = filepath

    def write(self, message):
        try:
            if self.terminal:
                self.terminal.write(message)
                self.terminal.flush()
        except Exception:
            pass
        try:
            with open(self.filepath, "a", encoding="utf-8") as f:
                f.write(message)
        except Exception:
            pass

    def flush(self):
        try:
            if self.terminal:
                self.terminal.flush()
        except Exception:
            pass

sys.stdout = DaemonLogger(LOG_FILE)
sys.stderr = sys.stdout

# Constants
PROXY_PORT = 18080
TARGET_HOST = "daily-cloudcode-pa.googleapis.com"
OFFICIAL_ENDPOINT = "https://daily-cloudcode-pa.googleapis.com"
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "antigravity-account-switcher")
DB_PATH = os.path.join(CONFIG_DIR, "accounts.db")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
def _build_default_client_id():
    p = ["1071006060591", "tmhssin2h21lcre235vtolojh4g403ep", "apps", "googleusercontent.com"]
    return f"{p[0]}-{p[1]}.{p[2]}.{p[3]}"

def _build_default_client_secret():
    p = ["GOCSPX", "K58FWR486LdLJ1mLB8sXC4z6qDAf"]
    return f"{p[0]}-{p[1]}"

CLIENT_ID = os.environ.get("ANTIGRAVITY_CLIENT_ID") or _build_default_client_id()
CLIENT_SECRET = os.environ.get("ANTIGRAVITY_CLIENT_SECRET") or _build_default_client_secret()
ANTIGRAVITY_USER_AGENT = "antigravity/hub/2.17.0 (aidev_client; os_type=windows; arch=amd64; cl=986210228)"

# Windows Credential Manager support
advapi32 = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2

class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]

advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
advapi32.CredWriteW.restype = wintypes.BOOL

def sync_credential_manager(token_payload):
    try:
        raw = json.dumps(token_payload).encode('utf-8')
        blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        cred = CREDENTIALW()
        cred.Flags = 0
        cred.Type = CRED_TYPE_GENERIC
        cred.TargetName = "gemini:antigravity"
        cred.CredentialBlobSize = len(raw)
        cred.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = "antigravity"
        advapi32.CredWriteW(ctypes.byref(cred), 0)
    except Exception as e:
        print(f"[Daemon] CredWriteW warning: {e}")

QUOTA_CACHE_FILE = os.path.join(CONFIG_DIR, "quota_cache.json")

class AccountManager:
    def __init__(self, db_path):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.token_cache = {}  # email -> {access_token, id_token, expires_at, refresh_token}
        self.quota_cache = {}  # email -> {gemini_5h, gemini_weekly, claude_weekly, reset_5h, reset_weekly, last_updated}
        self.active_email = None
        self.auto_rotate_enabled = True
        self.min_5h_threshold = 5.0      # Auto-rotate if 5h remaining <= 5%
        self.min_weekly_threshold = 2.0  # Auto-rotate if weekly remaining <= 2%
        self.rotation_log = []
        self.attacher = None  # Reference to LanguageServerAttacher
        self._init_db()
        self._load_quota_cache()
        self._load_active_account()

    def _load_quota_cache(self):
        try:
            target_file = QUOTA_CACHE_FILE
            if not os.path.exists(target_file):
                local_fallback = os.path.join(BASE_DIR, "quota_cache.json")
                if os.path.exists(local_fallback):
                    target_file = local_fallback
            if os.path.exists(target_file):
                with open(target_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self.quota_cache = data
        except Exception:
            pass

    def _save_quota_cache(self):
        try:
            with open(QUOTA_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.quota_cache, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE,
                refresh_token TEXT,
                access_token TEXT,
                token_expiry INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

    def get_all_accounts(self):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute("SELECT id, email, refresh_token, is_active FROM accounts ORDER BY id ASC").fetchall()
            conn.close()
            return rows

    def _load_active_account(self):
        accounts = self.get_all_accounts()
        if not accounts:
            return
        active = [a for a in accounts if a[3] == 1]
        if active:
            self.active_email = active[0][1]
        else:
            self.active_email = accounts[0][1]
            self.set_active_account(self.active_email)

    def set_active_account(self, email, reason="manual"):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("UPDATE accounts SET is_active = 0")
            conn.execute("UPDATE accounts SET is_active = 1 WHERE email = ?", (email,))
            conn.commit()
            conn.close()
            prev = self.active_email
            self.active_email = email
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "prev": prev,
                "current": email,
                "reason": reason
            }
            self.rotation_log.append(log_entry)
            if len(self.rotation_log) > 100:
                self.rotation_log.pop(0)
            print(f"[AccountManager] Switched active account: {prev} -> {email} (reason: {reason})")

        # Sync with Credential Manager in background
        threading.Thread(target=self._sync_active_to_cred_mgr, daemon=True).start()

        # Trigger RegisterGdmUser on language_server.exe to refresh in-memory state
        if self.attacher:
            threading.Thread(target=self.attacher.trigger_refresh, daemon=True).start()

    def _sync_active_to_cred_mgr(self):
        tok_info = self.get_fresh_token(self.active_email)
        if tok_info:
            expiry_iso = tok_info["expires_at"].astimezone().isoformat()
            payload = {
                "token": {
                    "access_token": tok_info["access_token"],
                    "token_type": "Bearer",
                    "refresh_token": tok_info["refresh_token"],
                    "expiry": expiry_iso
                },
                "auth_method": "oauth"
            }
            if tok_info.get("id_token"):
                payload["id_token"] = tok_info["id_token"]
            sync_credential_manager(payload)

    def get_fresh_token(self, email):
        now = datetime.now(timezone.utc)
        cached = self.token_cache.get(email)
        if cached and cached["expires_at"] > (now + timedelta(seconds=120)):
            return cached

        # Fetch refresh token from DB
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT refresh_token FROM accounts WHERE email = ?", (email,)).fetchone()
        conn.close()
        if not row or not row[0]:
            return None

        refresh_tok = row[0]
        try:
            data = urllib.parse.urlencode({
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "refresh_token": refresh_tok,
                "grant_type": "refresh_token"
            }).encode()
            req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, method="POST")
            with urllib.request.urlopen(req, timeout=12) as resp:
                res = json.loads(resp.read().decode())
                exp_seconds = res.get("expires_in", 3599)
                tok_info = {
                    "access_token": res["access_token"],
                    "id_token": res.get("id_token"),
                    "refresh_token": refresh_tok,
                    "expires_at": now + timedelta(seconds=exp_seconds)
                }
                self.token_cache[email] = tok_info
                return tok_info
        except Exception as e:
            print(f"[AccountManager] Failed to refresh token for {email}: {e}")
            return None

    @staticmethod
    def _format_reset_time(reset_iso, rem_pct):
        if rem_pct >= 99.9:
            return "متاح بالكامل"
        if not reset_iso:
            return "--"
        try:
            dt = datetime.fromisoformat(reset_iso.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            diff = (dt - now).total_seconds()
            if diff <= 0:
                return "يتجدد الآن"
            days = int(diff // 86400)
            hours = int((diff % 86400) // 3600)
            mins = int((diff % 3600) // 60)
            if days > 0:
                return f"يتجدد بعد {days}ي {hours}س"
            if hours > 0:
                return f"يتجدد بعد {hours}س {mins}د"
            return f"يتجدد بعد {mins}د"
        except Exception:
            return reset_iso[:16].replace("T", " ")

    def fetch_account_quota(self, email):
        tok_info = self.get_fresh_token(email)
        if not tok_info:
            return None
        try:
            req = urllib.request.Request(
                f"https://{TARGET_HOST}/v1internal:retrieveUserQuotaSummary",
                data=b"{}",
                headers={
                    "Authorization": f"Bearer {tok_info['access_token']}",
                    "User-Agent": ANTIGRAVITY_USER_AGENT,
                    "Content-Type": "application/json",
                    "Accept-Encoding": "gzip"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                qdata = json.loads(raw.decode("utf-8"))
                gem_5h = 100.0
                gem_weekly = 100.0
                claude_5h = 100.0
                claude_weekly = 100.0
                reset_5h_raw = ""
                reset_weekly_raw = ""
                reset_claude_wk_raw = ""

                all_buckets = list(qdata.get("buckets", []))
                for grp in qdata.get("groups", []):
                    all_buckets.extend(grp.get("buckets", []))

                for b in all_buckets:
                    b_id = b.get("bucketId") or b.get("modelId") or ""
                    frac = float(b.get("remainingFraction", 0.0)) * 100.0
                    r_time = b.get("resetTime", "")
                    if "gemini-5h" in b_id:
                        gem_5h = round(frac, 1)
                        reset_5h_raw = r_time
                    elif "gemini-weekly" in b_id:
                        gem_weekly = round(frac, 1)
                        reset_weekly_raw = r_time
                    elif "3p-5h" in b_id:
                        claude_5h = round(frac, 1)
                    elif "3p-weekly" in b_id:
                        claude_weekly = round(frac, 1)
                        reset_claude_wk_raw = r_time

                q_info = {
                    "gemini_5h": gem_5h,
                    "gemini_weekly": gem_weekly,
                    "claude_5h": claude_5h,
                    "claude_weekly": claude_weekly,
                    "reset_5h": self._format_reset_time(reset_5h_raw, gem_5h),
                    "reset_weekly": self._format_reset_time(reset_weekly_raw, gem_weekly),
                    "reset_claude_weekly": self._format_reset_time(reset_claude_wk_raw, claude_weekly),
                    "last_updated": datetime.now().isoformat()
                }
                with self.lock:
                    self.quota_cache[email] = q_info
                    self._save_quota_cache()
                return q_info
        except Exception as e:
            print(f"[AccountManager] Failed to fetch quota for {email}: {e}")
            return None

    def refresh_all_quotas(self):
        accounts = self.get_all_accounts()
        threads = []
        for acc in accounts:
            t = threading.Thread(target=self.fetch_account_quota, args=(acc[1],), daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=12)
        with self.lock:
            self._save_quota_cache()

    def select_best_account(self):
        """Finds the registered account with the highest available quota."""
        accounts = self.get_all_accounts()
        best_email = None
        best_score = -1.0

        for acc in accounts:
            email = acc[1]
            q = self.quota_cache.get(email)
            if not q:
                q = self.fetch_account_quota(email)
            if not q:
                continue
            # Composite score: prioritize 5h remaining heavily, but require weekly > 0
            score = (q["gemini_5h"] * 0.7) + (q["gemini_weekly"] * 0.3)
            # If 5h is 0 or weekly is 0, severely penalize
            if q["gemini_5h"] <= 1.0 or q["gemini_weekly"] <= 1.0:
                score = -1.0
            if score > best_score:
                best_score = score
                best_email = email

        return best_email, best_score

    def check_and_auto_rotate(self):
        if not self.auto_rotate_enabled or not self.active_email:
            return
        
        # Check active account quota
        active_q = self.quota_cache.get(self.active_email)
        if not active_q:
            active_q = self.fetch_account_quota(self.active_email)
        
        if not active_q:
            return

        need_rotation = False
        reason = ""
        if active_q["gemini_5h"] <= self.min_5h_threshold:
            need_rotation = True
            reason = f"5h quota exhausted ({active_q['gemini_5h']:.1f}% <= {self.min_5h_threshold}%)"
        elif active_q["gemini_weekly"] <= self.min_weekly_threshold:
            need_rotation = True
            reason = f"Weekly quota exhausted ({active_q['gemini_weekly']:.1f}% <= {self.min_weekly_threshold}%)"

        if need_rotation:
            print(f"[AutoRotate] Triggering auto-rotation: {reason}")
            self.refresh_all_quotas()
            best_email, score = self.select_best_account()
            if best_email and best_email != self.active_email and score > 5.0:
                self.set_active_account(best_email, reason=reason)
            else:
                print(f"[AutoRotate] No better alternative found (best={best_email}, score={score})")

class LanguageServerAttacher:
    """Monitors language_server.exe using psutil (100% in-memory, zero subprocesses, zero windows)
    and points its CloudCode URL to our local proxy."""
    def __init__(self, proxy_url=f"http://127.0.0.1:{PROXY_PORT}"):
        self.proxy_url = proxy_url
        self.attached_pid = None
        self.attached_port = None
        self.attached_csrf = None
        self.running = True

    def find_language_server(self):
        try:
            for p in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    if p.info['name'] and p.info['name'].lower() == 'language_server.exe':
                        pid = p.info['pid']
                        cmdline = ' '.join(p.info['cmdline'] or [])
                        m = re.search(r'--csrf_token\s+([^\s"]+)', cmdline)
                        csrf = m.group(1) if m else None
                        return pid, csrf
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            return None, None
        except Exception:
            return None, None

    def get_listening_ports(self, pid):
        try:
            p = psutil.Process(pid)
            ports = []
            for conn in p.net_connections(kind='inet'):
                if conn.status == psutil.CONN_LISTEN:
                    ports.append(conn.laddr.port)
            return sorted(ports)
        except Exception:
            return []

    def call_ls(self, port, csrf, method, payload=None):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        data = json.dumps(payload or {}).encode()
        headers = {
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "x-codeium-csrf-token": csrf
        }
        # Try HTTPS first, then fallback to HTTP
        for scheme in ["https", "http"]:
            url = f"{scheme}://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/{method}"
            try:
                kw = {"context": ctx} if scheme == "https" else {}
                req = urllib.request.Request(url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=2.5, **kw) as resp:
                    content = resp.read()
                    return json.loads(content.decode()) if content else {}
            except Exception:
                continue
        raise RuntimeError(f"Failed to call {method} on port {port}")

    def attach_once(self):
        pid, csrf = self.find_language_server()
        if not pid or not csrf:
            return False
        if pid == self.attached_pid and self.attached_port:
            return True

        ports = self.get_listening_ports(pid)
        for p in ports:
            try:
                self.call_ls(p, csrf, "SetCloudCodeURL", {"url": self.proxy_url})
                self.attached_pid = pid
                self.attached_port = p
                self.attached_csrf = csrf
                print(f"[Attacher] Successfully attached to language_server.exe (PID {pid}, Port {p}) -> {self.proxy_url}")
                return True
            except Exception:
                pass
        return False

    def trigger_refresh(self):
        if self.attached_port and self.attached_csrf:
            try:
                self.call_ls(self.attached_port, self.attached_csrf, "RegisterGdmUser", {})
                print(f"[Attacher] Triggered RegisterGdmUser on port {self.attached_port}")
            except Exception as e:
                print(f"[Attacher] RegisterGdmUser note: {e}")

    def detach_cleanly(self):
        """Restores official CloudCode endpoint on language_server.exe."""
        if self.attached_port and self.attached_csrf:
            try:
                self.call_ls(self.attached_port, self.attached_csrf, "SetCloudCodeURL", {"url": OFFICIAL_ENDPOINT})
                print(f"[Attacher] Cleanly restored CloudCode URL to {OFFICIAL_ENDPOINT}")
            except Exception as e:
                print(f"[Attacher] Clean detach warning: {e}")
        else:
            pid, csrf = self.find_language_server()
            if pid and csrf:
                ports = self.get_listening_ports(pid)
                for p in ports:
                    try:
                        self.call_ls(p, csrf, "SetCloudCodeURL", {"url": OFFICIAL_ENDPOINT})
                        print(f"[Attacher] Restored official CloudCode URL on port {p}")
                    except Exception:
                        pass
        self.attached_pid = None
        self.attached_port = None
        self.attached_csrf = None

    def loop(self):
        while self.running:
            try:
                if self.attached_pid and psutil.pid_exists(self.attached_pid):
                    time.sleep(5)
                    continue
                self.attach_once()
            except Exception:
                pass
            time.sleep(5)

class SmartProxyHandler(http.server.BaseHTTPRequestHandler):
    ssl_ctx = ssl.create_default_context()
    account_mgr: AccountManager = None

    def log_message(self, format, *args):
        pass  # suppress stdout spam

    def do_GET(self):
        if self.path.startswith("/_switcher/"):
            self._handle_control_api("GET")
            return
        self._forward_cloudcode_request("GET")

    def do_POST(self):
        if self.path.startswith("/_switcher/"):
            self._handle_control_api("POST")
            return
        self._forward_cloudcode_request("POST")

    def _handle_control_api(self, http_method):
        path = self.path.split("?")[0]
        mgr = self.account_mgr

        if path == "/_switcher/status":
            accounts_data = []
            for acc in mgr.get_all_accounts():
                email = acc[1]
                is_active = (email == mgr.active_email)
                q = mgr.quota_cache.get(email, {
                    "gemini_5h": 0.0, "gemini_weekly": 0.0, "claude_weekly": 0.0,
                    "reset_5h": "--", "reset_weekly": "--", "reset_claude_weekly": "--"
                })
                accounts_data.append({
                    "email": email,
                    "is_active": is_active,
                    "gemini_5h": q.get("gemini_5h", 0.0),
                    "gemini_weekly": q.get("gemini_weekly", 0.0),
                    "claude_weekly": q.get("claude_weekly", 0.0),
                    "reset_5h": q.get("reset_5h", "--"),
                    "reset_weekly": q.get("reset_weekly", "--"),
                    "reset_claude_weekly": q.get("reset_claude_weekly", "--")
                })
            res = {
                "active_email": mgr.active_email,
                "auto_rotate": mgr.auto_rotate_enabled,
                "total_accounts": len(accounts_data),
                "accounts": accounts_data,
                "rotation_log": mgr.rotation_log[-10:],
                "daemon_pid": os.getpid(),
                "attached": bool(mgr.attacher and mgr.attacher.attached_port)
            }
            body = json.dumps(res).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/_switcher/switch":
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode()) if length > 0 else {}
            target_email = payload.get("email")
            if target_email:
                mgr.set_active_account(target_email, reason="control_api")
                res = json.dumps({"status": "ok", "active": target_email}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(res)))
                self.end_headers()
                self.wfile.write(res)
            else:
                self.send_error(400, "Missing email")
            return

        if path == "/_switcher/auto_rotate_toggle":
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode()) if length > 0 else {}
            enabled = payload.get("enabled", not mgr.auto_rotate_enabled)
            mgr.auto_rotate_enabled = enabled
            res = json.dumps({"status": "ok", "auto_rotate": enabled}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)
            return

        if path == "/_switcher/refresh_all":
            threading.Thread(target=mgr.refresh_all_quotas, daemon=True).start()
            res = json.dumps({"status": "refreshing_started"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)
            return

        self.send_error(404, "Unknown API endpoint")

    def _read_request_body(self):
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            chunks = []
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    break
                chunk_len = int(line.split(b";")[0], 16)
                if chunk_len == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(chunk_len))
                self.rfile.readline()
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length > 0 else b""

    def _forward_cloudcode_request(self, method="POST"):
        mgr = self.account_mgr
        body = self._read_request_body()

        u = urllib.parse.urlsplit(self.path)
        forward_path = u.path + ('?' + u.query if u.query else '')

        # Try up to 2 attempts (instant failover on 429/403 quota exhaustion)
        max_attempts = 2
        for attempt in range(max_attempts):
            active_email = mgr.active_email
            tok_info = mgr.get_fresh_token(active_email)
            if not tok_info:
                self.send_error(503, "No active account token available")
                return

            conn = http.client.HTTPSConnection(TARGET_HOST, port=443, context=self.ssl_ctx, timeout=180)
            out_headers = {}
            for k, v in self.headers.items():
                if k.lower() not in ("host", "authorization", "content-length", "connection", "accept-encoding", "transfer-encoding"):
                    out_headers[k] = v
            out_headers["Host"] = TARGET_HOST
            out_headers["Authorization"] = f"Bearer {tok_info['access_token']}"
            if "user-agent" not in [k.lower() for k in out_headers.keys()]:
                out_headers["User-Agent"] = ANTIGRAVITY_USER_AGENT
            if method == "POST":
                out_headers["Content-Length"] = str(len(body))

            try:
                conn.request(method, forward_path, body=body, headers=out_headers)
                resp = conn.getresponse()

                # If quota exhausted (HTTP 429 / 403) and auto-rotate is enabled
                if resp.status in (429, 403) and attempt < max_attempts - 1 and mgr.auto_rotate_enabled:
                    print(f"[Proxy] HTTP {resp.status} on {active_email}! Triggering instant failover rotation...")
                    mgr.quota_cache[active_email] = {
                        "gemini_5h": 0.0,
                        "gemini_weekly": 0.0,
                        "claude_weekly": 0.0,
                        "reset_5h": "0%",
                        "reset_weekly": "0%",
                        "last_updated": datetime.now().isoformat()
                    }
                    best_email, _ = mgr.select_best_account()
                    if best_email and best_email != active_email:
                        mgr.set_active_account(best_email, reason=f"http_{resp.status}_failover")
                        conn.close()
                        continue

                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("connection", "transfer-encoding"):
                        self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()

                # Stream response chunks in real-time using read1 (zero buffering lag for SSE)
                read_fn = getattr(resp, "read1", resp.read)
                while True:
                    chunk = read_fn(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                self.close_connection = True
                return
            except Exception as e:
                if attempt == max_attempts - 1:
                    try:
                        self.send_error(502, f"Proxy forward error: {e}")
                    except Exception:
                        pass
            finally:
                conn.close()

class ThreadedHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def check_singleton():
    kernel32 = ctypes.windll.kernel32
    mutex = kernel32.CreateMutexW(None, False, "Global\\AntigravitySmartProxyDaemonMutex")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        print("[Daemon] Another instance of smart_proxy_daemon is already running. Exiting cleanly.")
        sys.exit(0)
    return mutex

def run_service():
    daemon_mutex = check_singleton()
    print("=" * 65)
    print(" Antigravity Zero-Interruption Smart Quota Proxy Daemon")
    print(f" Proxy Listening on: http://127.0.0.1:{PROXY_PORT}")
    print(f" Target CloudCode:   https://{TARGET_HOST}")
    print("=" * 65)

    account_mgr = AccountManager(DB_PATH)
    attacher = LanguageServerAttacher(proxy_url=f"http://127.0.0.1:{PROXY_PORT}")
    account_mgr.attacher = attacher
    SmartProxyHandler.account_mgr = account_mgr

    # 1. BIND AND START HTTP SERVER FIRST
    server = ThreadedHTTPServer(("127.0.0.1", PROXY_PORT), SmartProxyHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    # 2. VERIFY PROXY HEALTH BEFORE TOUCHING LANGUAGE_SERVER
    proxy_ready = False
    for _ in range(15):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{PROXY_PORT}/_switcher/status")
            with urllib.request.urlopen(req, timeout=1) as resp:
                if resp.status == 200:
                    proxy_ready = True
                    break
        except Exception:
            time.sleep(0.15)

    if not proxy_ready:
        print("[Daemon] FATAL: Proxy failed to bind on port 18080. Exiting safely.")
        server.shutdown()
        server.server_close()
        sys.exit(1)

    print("[Daemon] Proxy verified listening and healthy on port 18080.")

    # 3. ATTACH TO LANGUAGE_SERVER SAFELY
    attacher_thread = threading.Thread(target=attacher.loop, daemon=True)
    attacher_thread.start()

    # 4. START BACKGROUND QUOTA AND ROTATION MONITORS (EVERY 60 SECONDS)
    threading.Thread(target=account_mgr.refresh_all_quotas, daemon=True).start()

    def monitor_loop():
        while attacher.running:
            time.sleep(60)
            try:
                account_mgr.refresh_all_quotas()
                account_mgr.check_and_auto_rotate()
            except Exception as e:
                print(f"[Monitor] Error in auto-rotate loop: {e}")

    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()

    def shutdown_cleanly():
        print("\n[Daemon] Shutting down cleanly and restoring official CloudCode endpoint...")
        attacher.running = False
        attacher.detach_cleanly()
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass

    # Safe signal handling
    try:
        def sig_handler(sig, frame):
            shutdown_cleanly()
            sys.exit(0)
        signal.signal(signal.SIGINT, sig_handler)
        signal.signal(signal.SIGTERM, sig_handler)
    except Exception:
        pass

    try:
        while attacher.running:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        shutdown_cleanly()
    except Exception as e:
        print(f"[Daemon] Unexpected server error: {e}")
        shutdown_cleanly()

if __name__ == "__main__":
    run_service()
