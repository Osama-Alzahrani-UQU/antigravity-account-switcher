"""
CLI tool: Immediately switch Antigravity to the account with the highest remaining quota.
"""
import sys
import os
import urllib.request
import urllib.parse
import json

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from smart_proxy_daemon import DB_PATH, AccountManager, PROXY_PORT

def main():
    print("=" * 60)
    print("  Antigravity Smart Quota Failover - Best Account Switcher")
    print("=" * 60)

    # 1. Check if smart daemon is running on port 18080
    daemon_running = False
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PROXY_PORT}/_switcher/status")
        with urllib.request.urlopen(req, timeout=2) as resp:
            daemon_running = True
    except Exception:
        daemon_running = False

    mgr = AccountManager(DB_PATH)
    print("Refreshing quotas for all accounts...")
    mgr.refresh_all_quotas()

    best_email, score = mgr.select_best_account()
    if not best_email:
        print("Error: No valid accounts found in database.")
        sys.exit(1)

    print(f"\nBest account found: {best_email} (Composite Score: {score:.1f})")
    q = mgr.quota_cache.get(best_email, {})
    print(f"  Gemini 5-Hour Quota:  {q.get('gemini_5h', 0):.1f}%")
    print(f"  Gemini Weekly Quota:  {q.get('gemini_weekly', 0):.1f}%")

    if best_email == mgr.active_email:
        print(f"\nAccount {best_email} is ALREADY the active account!")
    else:
        print(f"\nSwitching active account: {mgr.active_email} -> {best_email}...")
        if daemon_running:
            req = urllib.request.Request(
                f"http://127.0.0.1:{PROXY_PORT}/_switcher/switch",
                data=json.dumps({"email": best_email}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                print("Active account switched in live daemon with ZERO interruption.")
        else:
            mgr.set_active_account(best_email, reason="cli_best_switch")
            print("Active account updated in accounts.db and Windows Credential Manager.")

    if daemon_running:
        print("\nSmart Proxy Daemon status: ACTIVE on port 18080 (Zero-Interruption Enabled)")
    else:
        print("\nNote: Smart Daemon is not currently running. You can run start_daemon.bat or run_gui.bat for live zero-interruption routing.")

    print("Done! Ready to use.")

if __name__ == "__main__":
    main()
