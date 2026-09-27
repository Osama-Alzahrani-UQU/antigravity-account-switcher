"""
Antigravity Fast Multi-Account Switcher & Quota Dashboard
---------------------------------------------------------
Interactive Modern Desktop GUI for:
1. Seamless zero-interruption account switching for Google Antigravity.
2. Accurate visual monitoring of Gemini (5-hour & weekly) and Claude/GPT weekly quotas.
3. Flicker-free in-place UI updates every 60 seconds (1 minute).
4. Intelligent auto-rotation when limits approach exhaustion.
5. One-click Google account onboarding (+ Add Account).
"""

import customtkinter as ctk
import urllib.request
import urllib.parse
import json
import threading
import time
import os
import sys
import subprocess
import add_account
from smart_proxy_daemon import AccountManager, DB_PATH

PROXY_API = "http://127.0.0.1:18080/_switcher"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DAEMON_SCRIPT = os.path.join(BASE_DIR, "smart_proxy_daemon.py")
REFRESH_INTERVAL_MS = 60000  # 60 seconds (1 minute)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class SwitcherApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Antigravity Multi-Account Switchboard (Zero-Interruption)")
        self.geometry("920x700")
        self.minsize(820, 580)

        self.card_widgets = {}
        self.empty_lbl = None
        self._poll_timer_id = None
        self._daemon_started = False

        # State cache
        self.daemon_status = {
            "active_email": None,
            "auto_rotate": True,
            "accounts": [],
            "attached": False
        }

        self._build_header()
        self._build_top_controls()
        self._build_accounts_scroll()
        self._build_status_footer()

        # Load cached accounts & quotas immediately so UI is populated on first frame
        self._load_local_snapshot()
        self._render_accounts_list()

        # Ensure background daemon is running and start 60-second periodic polling
        self._ensure_daemon_running()
        self._poll_daemon_status(schedule_next=True)

    def _load_local_snapshot(self):
        try:
            mgr = AccountManager(DB_PATH)
            accounts_data = []
            for acc in mgr.get_all_accounts():
                email = acc[1]
                is_active = (email == mgr.active_email)
                q = mgr.quota_cache.get(email, {})
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
            self.daemon_status["active_email"] = mgr.active_email
            self.daemon_status["accounts"] = accounts_data
        except Exception as e:
            print(f"[GUI] Local snapshot note: {e}")

    def _ensure_daemon_running(self):
        if self._daemon_started:
            return

        def check_or_start():
            try:
                urllib.request.urlopen(f"{PROXY_API}/status", timeout=2)
            except Exception:
                self._daemon_started = True
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0
                subprocess.Popen(
                    ["pythonw", DAEMON_SCRIPT],
                    cwd=os.path.dirname(DAEMON_SCRIPT),
                    creationflags=0x08000000 | 0x00000008,  # CREATE_NO_WINDOW | DETACHED_PROCESS
                    startupinfo=si
                )
                time.sleep(1.5)
                self._poll_daemon_status(schedule_next=False)

        threading.Thread(target=check_or_start, daemon=True).start()

    def _build_header(self):
        header_frame = ctk.CTkFrame(self, height=75, fg_color="#1e293b", corner_radius=0)
        header_frame.pack(fill="x", side="top")

        title_box = ctk.CTkFrame(header_frame, fg_color="transparent")
        title_box.pack(side="left", padx=20, pady=12)

        main_title = ctk.CTkLabel(
            title_box,
            text="Antigravity Account Switchboard ✦",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color="#38bdf8"
        )
        main_title.pack(anchor="w")

        sub_title = ctk.CTkLabel(
            title_box,
            text="نظام إدارة وتبديل الحسابات الفوري بدون إيقاف المهام مع القراءة الدقيقة للأرصدة (تحديث تلقائي كل دقيقة)",
            font=ctk.CTkFont(size=12),
            text_color="#94a3b8"
        )
        sub_title.pack(anchor="w")

        self.attacher_badge = ctk.CTkLabel(
            header_frame,
            text="● جاري الفحص...",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#f59e0b",
            fg_color="#334155",
            corner_radius=8,
            padx=14,
            pady=6
        )
        self.attacher_badge.pack(side="right", padx=20)

    def _build_top_controls(self):
        ctrl_frame = ctk.CTkFrame(self, fg_color="#0f172a", corner_radius=10)
        ctrl_frame.pack(fill="x", padx=18, pady=(12, 6))

        self.auto_rotate_var = ctk.BooleanVar(value=True)
        self.auto_rotate_switch = ctk.CTkSwitch(
            ctrl_frame,
            text="التبديل التلقائي الذكي عند نفاد الرصيد (Auto-Failover on Expiry)",
            font=ctk.CTkFont(size=13, weight="bold"),
            variable=self.auto_rotate_var,
            command=self._on_toggle_auto_rotate,
            progress_color="#10b981"
        )
        self.auto_rotate_switch.pack(side="left", padx=18, pady=12)

        btn_box = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        btn_box.pack(side="right", padx=14, pady=10)

        add_btn = ctk.CTkButton(
            btn_box,
            text="+ إضافة حساب قوقل جديد",
            command=self._on_add_account,
            fg_color="#2563eb",
            hover_color="#1d4ed8",
            font=ctk.CTkFont(size=12, weight="bold"),
            width=165,
            height=32
        )
        add_btn.pack(side="left", padx=6)

        self.refresh_btn = ctk.CTkButton(
            btn_box,
            text="⟳ تحديث الأرصدة الآن",
            command=self._on_refresh_quotas,
            fg_color="#334155",
            hover_color="#475569",
            font=ctk.CTkFont(size=12),
            width=145,
            height=32
        )
        self.refresh_btn.pack(side="left", padx=6)

    def _build_accounts_scroll(self):
        self.notice_banner = ctk.CTkFrame(self, fg_color="#1e293b", corner_radius=8)
        self.notice_banner.pack(fill="x", padx=18, pady=(4, 6))
        self.notice_lbl = ctk.CTkLabel(
            self.notice_banner,
            text="⚡ جاهز للعمل. التبديل يتم فوراً وبدون قطع أي مهام جارية نهائياً (تحديث القائمة كل 60 ثانية بدون وميض).",
            font=ctk.CTkFont(size=12),
            text_color="#94a3b8"
        )
        self.notice_lbl.pack(padx=12, pady=6)

        self.scroll_frame = ctk.CTkScrollableFrame(
            self,
            corner_radius=12,
            fg_color="#0f172a",
            label_text="الحسابات المسجلة وحالة الأرصدة الفعلية (Registered Accounts & Real-Time Quota)",
            label_font=ctk.CTkFont(size=14, weight="bold"),
            label_text_color="#e2e8f0"
        )
        self.scroll_frame.pack(fill="both", expand=True, padx=18, pady=4)

    def _build_status_footer(self):
        footer_frame = ctk.CTkFrame(self, height=36, fg_color="#1e293b", corner_radius=0)
        footer_frame.pack(fill="x", side="bottom")

        self.footer_status_lbl = ctk.CTkLabel(
            footer_frame,
            text="المحول الذكي: 127.0.0.1:18080 | وضع التبديل الفوري (Zero-Interruption) نشط | التحديث التلقائي: كل دقيقة",
            font=ctk.CTkFont(size=11),
            text_color="#10b981",
            padx=16
        )
        self.footer_status_lbl.pack(side="left")

        self.last_update_lbl = ctk.CTkLabel(
            footer_frame,
            text=f"آخر تحديث: {time.strftime('%H:%M:%S')}",
            font=ctk.CTkFont(size=11),
            text_color="#94a3b8",
            padx=16
        )
        self.last_update_lbl.pack(side="right")

    def _poll_daemon_status(self, schedule_next=False):
        if schedule_next:
            if self._poll_timer_id is not None:
                try:
                    self.after_cancel(self._poll_timer_id)
                except Exception:
                    pass
            # Schedule next automatic refresh in 60,000 ms (60 seconds = 1 minute)
            self._poll_timer_id = self.after(REFRESH_INTERVAL_MS, lambda: self._poll_daemon_status(schedule_next=True))

        def worker():
            try:
                req = urllib.request.Request(f"{PROXY_API}/status", headers={"User-Agent": "SwitcherGUI"})
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read().decode())
                    self.daemon_status = data
                    self.after(0, self._apply_daemon_status)
            except Exception:
                self._load_local_snapshot()
                self.after(0, self._apply_daemon_offline)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_daemon_offline(self):
        self.attacher_badge.configure(
            text="● وضع التبديل المباشر جاهز",
            text_color="#38bdf8",
            fg_color="#0f172a"
        )
        self.last_update_lbl.configure(text=f"آخر تحديث: {time.strftime('%H:%M:%S')}")
        self._render_accounts_list()

    def _apply_daemon_status(self):
        data = self.daemon_status
        attached = data.get("attached", False)
        if attached:
            self.attacher_badge.configure(
                text="● متصل بـ Antigravity (PID جاهز)",
                text_color="#10b981",
                fg_color="#064e3b"
            )
        else:
            self.attacher_badge.configure(
                text="● المحول الذكي نشط وجاهز",
                text_color="#38bdf8",
                fg_color="#0c4a6e"
            )

        self.auto_rotate_var.set(data.get("auto_rotate", True))
        self.last_update_lbl.configure(text=f"آخر تحديث: {time.strftime('%H:%M:%S')}")
        self._render_accounts_list()

    def _render_accounts_list(self):
        accounts = self.daemon_status.get("accounts", [])

        if not accounts:
            for w in self.scroll_frame.winfo_children():
                w.destroy()
            self.card_widgets.clear()
            self.empty_lbl = ctk.CTkLabel(
                self.scroll_frame,
                text="لا توجد حسابات مسجلة حالياً. اضغط على '+ إضافة حساب قوقل جديد' للبدء.",
                font=ctk.CTkFont(size=13),
                text_color="#94a3b8"
            )
            self.empty_lbl.pack(pady=40)
            return

        if self.empty_lbl is not None:
            try:
                self.empty_lbl.destroy()
            except Exception:
                pass
            self.empty_lbl = None

        current_emails = [acc.get("email", "") for acc in accounts]
        cached_emails = list(self.card_widgets.keys())

        # Only rebuild card frames if the list of accounts changed; otherwise update in-place (ZERO flicker!)
        if current_emails != cached_emails:
            for w in self.scroll_frame.winfo_children():
                w.destroy()
            self.card_widgets.clear()
            for idx, acc in enumerate(accounts, start=1):
                self._create_account_card(idx, acc)
        else:
            for idx, acc in enumerate(accounts, start=1):
                self._update_account_card(idx, acc)

    @staticmethod
    def _bar_color(pct):
        if pct > 35.0:
            return "#10b981"
        if pct > 10.0:
            return "#f59e0b"
        return "#ef4444"

    def _create_account_card(self, index, acc):
        email = acc.get("email", "")
        card = ctk.CTkFrame(self.scroll_frame, corner_radius=10)
        card.pack(fill="x", padx=10, pady=6)

        # Top row
        top_row = ctk.CTkFrame(card, fg_color="transparent")
        top_row.pack(fill="x", padx=14, pady=(10, 4))

        idx_lbl = ctk.CTkLabel(
            top_row,
            text=f"#{index}",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#94a3b8",
            width=28
        )
        idx_lbl.pack(side="left")

        email_lbl = ctk.CTkLabel(
            top_row,
            text=email,
            font=ctk.CTkFont(size=14, weight="bold")
        )
        email_lbl.pack(side="left", padx=8)

        active_tag = ctk.CTkLabel(
            top_row,
            text="● نشط حالياً (Active)",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#10b981",
            fg_color="#064e3b",
            corner_radius=6,
            padx=10,
            pady=3
        )

        switch_btn = ctk.CTkButton(
            top_row,
            text="تبديل لهذا الحساب فوراً ⚡",
            command=lambda e=email: self._on_switch_account(e),
            fg_color="#0284c7",
            hover_color="#0369a1",
            font=ctk.CTkFont(size=12, weight="bold"),
            width=165,
            height=28
        )

        current_tag = ctk.CTkLabel(
            top_row,
            text="الحساب المستخدم حالياً",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#38bdf8"
        )

        # Quota row (3 columns: Gemini 5h, Gemini Weekly, Claude/GPT Weekly)
        quota_row = ctk.CTkFrame(card, fg_color="#0f172a", corner_radius=8)
        quota_row.pack(fill="x", padx=12, pady=(4, 10))

        # 1. Gemini 5h Column
        col_5h = ctk.CTkFrame(quota_row, fg_color="transparent")
        col_5h.pack(side="left", fill="x", expand=True, padx=10, pady=8)
        col_5h_lbl = ctk.CTkLabel(col_5h, text="", font=ctk.CTkFont(size=11, weight="bold"), text_color="#e2e8f0")
        col_5h_lbl.pack(anchor="w")
        bar_5h = ctk.CTkProgressBar(col_5h, fg_color="#334155", height=10)
        bar_5h.pack(fill="x", pady=(4, 2))

        # 2. Gemini Weekly Column
        col_wk = ctk.CTkFrame(quota_row, fg_color="transparent")
        col_wk.pack(side="left", fill="x", expand=True, padx=10, pady=8)
        col_wk_lbl = ctk.CTkLabel(col_wk, text="", font=ctk.CTkFont(size=11, weight="bold"), text_color="#e2e8f0")
        col_wk_lbl.pack(anchor="w")
        bar_wk = ctk.CTkProgressBar(col_wk, fg_color="#334155", height=10)
        bar_wk.pack(fill="x", pady=(4, 2))

        # 3. Claude & GPT Weekly Column
        col_cl = ctk.CTkFrame(quota_row, fg_color="transparent")
        col_cl.pack(side="left", fill="x", expand=True, padx=10, pady=8)
        col_cl_lbl = ctk.CTkLabel(col_cl, text="", font=ctk.CTkFont(size=11), text_color="#cbd5e1")
        col_cl_lbl.pack(anchor="w")
        bar_cl = ctk.CTkProgressBar(col_cl, fg_color="#334155", height=10)
        bar_cl.pack(fill="x", pady=(4, 2))

        self.card_widgets[email] = {
            "card": card,
            "idx_lbl": idx_lbl,
            "email_lbl": email_lbl,
            "active_tag": active_tag,
            "switch_btn": switch_btn,
            "current_tag": current_tag,
            "col_5h_lbl": col_5h_lbl,
            "bar_5h": bar_5h,
            "col_wk_lbl": col_wk_lbl,
            "bar_wk": bar_wk,
            "col_cl_lbl": col_cl_lbl,
            "bar_cl": bar_cl,
        }
        self._update_account_card(index, acc)

    def _update_account_card(self, index, acc):
        email = acc.get("email", "")
        w = self.card_widgets.get(email)
        if not w:
            return

        is_active = acc.get("is_active", False)
        gem_5h = float(acc.get("gemini_5h", 0.0))
        gem_weekly = float(acc.get("gemini_weekly", 0.0))
        claude_weekly = float(acc.get("claude_weekly", 0.0))
        reset_5h = acc.get("reset_5h", "--")
        reset_weekly = acc.get("reset_weekly", "--")
        reset_claude = acc.get("reset_claude_weekly", "--")

        card_border = "#0284c7" if is_active else "#334155"
        card_bg = "#1e293b" if is_active else "#131d2e"

        w["card"].configure(fg_color=card_bg, border_color=card_border, border_width=2 if is_active else 1)
        w["idx_lbl"].configure(text=f"#{index}")
        w["email_lbl"].configure(text_color="#ffffff" if is_active else "#e2e8f0")

        if is_active:
            w["switch_btn"].pack_forget()
            w["active_tag"].pack(side="left", padx=8)
            w["current_tag"].pack(side="right")
        else:
            w["active_tag"].pack_forget()
            w["current_tag"].pack_forget()
            w["switch_btn"].pack(side="right")

        w["col_5h_lbl"].configure(text=f"Gemini (5 ساعات): {gem_5h:.1f}%  [{reset_5h}]")
        w["bar_5h"].configure(progress_color=self._bar_color(gem_5h))
        w["bar_5h"].set(max(0.01, min(1.0, gem_5h / 100.0)))

        w["col_wk_lbl"].configure(text=f"Gemini (الأسبوعي): {gem_weekly:.1f}%  [{reset_weekly}]")
        w["bar_wk"].configure(progress_color=self._bar_color(gem_weekly))
        w["bar_wk"].set(max(0.01, min(1.0, gem_weekly / 100.0)))

        w["col_cl_lbl"].configure(text=f"Claude/GPT (أسبوعي): {claude_weekly:.1f}%  [{reset_claude}]")
        w["bar_cl"].configure(progress_color=self._bar_color(claude_weekly))
        w["bar_cl"].set(max(0.01, min(1.0, claude_weekly / 100.0)))

    def _on_switch_account(self, email):
        def worker():
            switched_ok = False
            try:
                data = json.dumps({"email": email}).encode()
                req = urllib.request.Request(
                    f"{PROXY_API}/switch",
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    if resp.status == 200:
                        switched_ok = True
            except Exception:
                pass

            try:
                mgr = AccountManager(DB_PATH)
                mgr.set_active_account(email, reason="gui_direct_switch")
                switched_ok = True
            except Exception as e:
                print(f"[GUI] Direct switch error: {e}")

            if switched_ok:
                self._poll_daemon_status(schedule_next=False)
                self.after(0, lambda: self._show_switch_notification(email))

        threading.Thread(target=worker, daemon=True).start()

    def _show_switch_notification(self, email):
        self.notice_banner.configure(fg_color="#064e3b")
        self.notice_lbl.configure(
            text=f"✔ تم تفعيل الحساب: {email} فوراً بدون انقطاع! (لتحديث الإعدادات في Antigravity، اضغط ⟳ بجانب Models & Usage)",
            text_color="#6ee7b7"
        )

    def _on_toggle_auto_rotate(self):
        val = self.auto_rotate_var.get()

        def worker():
            try:
                data = json.dumps({"enabled": val}).encode()
                req = urllib.request.Request(
                    f"{PROXY_API}/auto_rotate_toggle",
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    pass
            except Exception as e:
                print(f"[GUI] Toggle error: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _on_refresh_quotas(self):
        self.last_update_lbl.configure(text="جاري فحص وتحديث الأرصدة من قوقل...")
        self.refresh_btn.configure(state="disabled", text="جاري التحديث...")

        def worker():
            try:
                req = urllib.request.Request(f"{PROXY_API}/refresh_all", method="POST")
                with urllib.request.urlopen(req, timeout=3) as resp:
                    pass
            except Exception:
                pass

            try:
                mgr = AccountManager(DB_PATH)
                mgr.refresh_all_quotas()
            except Exception as e:
                print(f"[GUI] Direct quota refresh error: {e}")

            self._poll_daemon_status(schedule_next=False)
            self.after(0, lambda: self.refresh_btn.configure(state="normal", text="⟳ تحديث الأرصدة الآن"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_add_account(self):
        def run_oauth():
            added_email = add_account.login_new_account()
            if added_email:
                print(f"[GUI] Added account: {added_email}")
                self._on_refresh_quotas()

        threading.Thread(target=run_oauth, daemon=True).start()


if __name__ == "__main__":
    app = SwitcherApp()
    app.mainloop()
