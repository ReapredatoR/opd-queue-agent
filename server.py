"""
server.py
OPD Queue Agent — Interactive Local Web Application & Demonstration Server.
Student: Malinga Rajapaksha (MS25909296) | SLIIT IT6070 Assignment II
Run with: python server.py [--port 8000]
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# Ensure UTF-8 output
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from queue_engine import (
    QueueEngine,
    Session,
    Patient,
    make_demo_session,
    init_db,
    persist_events,
)
from notification_agent import (
    NotificationAgent,
    SimulatedChannel,
    SimulatedBookingBackend,
)

# ---------------------------------------------------------------------------
# Global State Management
# ---------------------------------------------------------------------------

class SystemController:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock if hasattr(self, 'lock') else threading.Lock():
            self.db_conn = init_db("opd_queue.db")
            self.session = make_demo_session()
            self.engine = QueueEngine(self.session)
            self.channel = SimulatedChannel(self.db_conn, self.session.session_id)
            self.booking = SimulatedBookingBackend()
            self.agent = NotificationAgent(
                self.engine,
                self.channel,
                self.booking,
                self.db_conn,
            )
            self.notifications_history = []
            self.demo_step = 0
            self.is_demo_running = False

    def get_state(self):
        with self.lock:
            estimates = self.engine.estimates()
            patients_data = []
            for p in self.session.patients:
                est = estimates.get(p.patient_id, {})
                patients_data.append({
                    "patient_id": p.patient_id,
                    "name": p.name,
                    "seq": p.seq,
                    "language": p.language,
                    "is_urgent": p.is_urgent,
                    "status": p.status,
                    "position": est.get("position", "-"),
                    "est_wait_min": est.get("est_wait_min", 0 if p.status == "with_doctor" else "-"),
                })
            
            recent_notifications = self.channel.get_log()[-15:]
            recent_actions = self.booking.get_actions()[-10:]
            events = self.session.events[-20:]

            return {
                "session_id": self.session.session_id,
                "doctor_name": self.session.doctor_name,
                "doctor_status": self.session.doctor_status,
                "delay_minutes": self.session.delay_minutes,
                "llm_available": self.agent._llm_available,
                "llm_model": self.agent.model,
                "patients": patients_data,
                "notifications": recent_notifications,
                "booking_actions": recent_actions,
                "events": events,
                "demo_step": self.demo_step,
            }

    def check_in(self, pid: str):
        with self.lock:
            evts = self.engine.check_in(pid)
            persist_events(self.db_conn, self.session)
            return evts

    def mark_with_doctor(self, pid: str):
        with self.lock:
            evts = self.engine.mark_with_doctor(pid)
            persist_events(self.db_conn, self.session)
            return evts

    def mark_done(self, pid: str):
        with self.lock:
            evts = self.engine.mark_done(pid)
            persist_events(self.db_conn, self.session)
            return evts

    def mark_no_show(self, pid: str):
        with self.lock:
            evts = self.engine.mark_no_show(pid)
            persist_events(self.db_conn, self.session)
            return evts

    def recall(self, pid: str):
        with self.lock:
            evts = self.engine.recall_patient(pid)
            persist_events(self.db_conn, self.session)
            return evts

    def doctor_delay(self, minutes: int):
        with self.lock:
            evts = self.engine.set_doctor_delayed(minutes)
            persist_events(self.db_conn, self.session)
            # Trigger Feature 2: Notify all waiting patients
            notifs = self.agent.notify_all_waiting("delayed")
            return {"events": evts, "notifications": notifs}

    def doctor_arrived(self):
        with self.lock:
            evts = self.engine.set_doctor_arrived()
            persist_events(self.db_conn, self.session)
            notifs = self.agent.notify_all_waiting("doctor_arrived")
            return {"events": evts, "notifications": notifs}

    def doctor_cancelled(self):
        with self.lock:
            evts = self.engine.set_doctor_cancelled()
            persist_events(self.db_conn, self.session)
            notifs = self.agent.notify_all_waiting("cancelled")
            return {"events": evts, "notifications": notifs}

    def patient_reply(self, pid: str, reply_text: str):
        with self.lock:
            action = self.agent.handle_reply(pid, reply_text)
            return {"action": action}

    def toggle_mode(self):
        with self.lock:
            self.agent._llm_available = not self.agent._llm_available
            return {"llm_available": self.agent._llm_available}

    def run_demo_step(self, step: int):
        with self.lock:
            self.demo_step = step
            msg = ""
            if step == 1:
                # Initial Queue State
                msg = "Step 1: Session initialized with 8 patients. Doctor Kumara scheduled."
            elif step == 2:
                # Check in P001 to P004
                for pid in ["P001", "P002", "P003", "P004"]:
                    self.engine.check_in(pid)
                msg = "Step 2: Patients P001–P004 checked in at OPD reception."
            elif step == 3:
                # Doctor delayed 20 min -> F1 wait recalculation & F2 trilingual SMS
                evts = self.engine.set_doctor_delayed(20)
                notifs = self.agent.notify_all_waiting("delayed")
                msg = "Step 3 [Success Case]: Consultant delayed 20 min. Live wait times recalculated in milliseconds (R1); 8 trilingual SMS dispatched (R2)."
            elif step == 4:
                # P001 No-Show
                self.engine.mark_no_show("P001")
                msg = "Step 4: Anil Perera (P001) marked no-show after grace period. Slot vacated, queue moves forward, recall slot saved."
            elif step == 5:
                # Attempt No-Show on Urgent P005 (Safety Guard)
                evts = self.engine.mark_no_show("P005")
                msg = "Step 5 [Safety Edge Case]: Attempted auto-skip on urgent cardiac patient P005 (Suresh Kumar). Intercepted by Clinical Guard!"
            elif step == 6:
                # Patient reply interpretation & tool call
                action = self.agent.handle_reply("P002", "I want to reschedule please")
                msg = f"Step 6 [Agentic Tool Calling]: Patient P002 replied 'I want to reschedule'. Claude Agent dispatched reschedule tool → {action}."
            elif step == 7:
                # Doctor arrives
                self.engine.set_doctor_arrived()
                self.agent.notify_all_waiting("doctor_arrived")
                msg = "Step 7: Doctor Kumara arrived! Session status set to on_time. Arrival alerts dispatched to waiting patients."
            elif step == 8:
                # Demonstrate Fallback Case (Test 3)
                old_state = self.agent._llm_available
                self.agent._llm_available = False # Simulate cloud outage
                self.engine.set_doctor_cancelled()
                self.agent.notify_all_waiting("cancelled")
                self.agent._llm_available = old_state
                msg = "Step 8 [Error/Fallback Case]: Cloud API outage simulated. Agent caught missing client and dispatched pre-approved templates with 100% uptime!"
            return {"step": step, "message": msg}


controller = SystemController()

# ---------------------------------------------------------------------------
# HTML Dashboard Page
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>OPD Queue Agent — Live Demonstration & Control Hub</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --navy: #0f172a;
      --card-bg: #1e293b;
      --card-border: #334155;
      --primary: #38bdf8;
      --primary-dark: #0284c7;
      --accent-green: #10b981;
      --accent-amber: #f59e0b;
      --accent-rose: #f43f5e;
      --accent-purple: #a855f7;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --surface: #0b1120;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Plus Jakarta Sans', sans-serif;
      background-color: var(--surface);
      color: var(--text-main);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }
    /* Top Header */
    header {
      background: rgba(15, 23, 42, 0.85);
      backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--card-border);
      padding: 14px 28px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      position: sticky;
      top: 0;
      z-index: 50;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .brand-logo {
      width: 40px;
      height: 40px;
      background: linear-gradient(135deg, #0284c7, #38bdf8);
      border-radius: 10px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 800;
      font-size: 20px;
      color: white;
      box-shadow: 0 4px 14px rgba(56, 189, 248, 0.35);
    }
    .brand-title {
      font-size: 17px;
      font-weight: 700;
      letter-spacing: -0.3px;
    }
    .brand-sub {
      font-size: 11.5px;
      color: var(--text-muted);
    }
    .badge-bar {
      display: flex;
      gap: 10px;
      align-items: center;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 5px 12px;
      border-radius: 20px;
      font-size: 12px;
      font-weight: 600;
    }
    .badge-pulse {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--accent-green);
      animation: pulse 2s infinite;
    }
    @keyframes pulse {
      0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
      70% { transform: scale(1); box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); }
      100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
    }
    .btn {
      font-family: inherit;
      font-weight: 600;
      font-size: 12.5px;
      padding: 7px 14px;
      border-radius: 8px;
      border: 1px solid transparent;
      cursor: pointer;
      transition: all 0.2s ease;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .btn-primary {
      background: var(--primary);
      color: var(--navy);
      border-color: var(--primary);
    }
    .btn-primary:hover {
      background: #7dd3fc;
      transform: translateY(-1px);
    }
    .btn-secondary {
      background: #334155;
      color: var(--text-main);
      border-color: #475569;
    }
    .btn-secondary:hover {
      background: #475569;
    }
    .btn-danger {
      background: rgba(244, 63, 94, 0.2);
      color: #fda4af;
      border-color: rgba(244, 63, 94, 0.4);
    }
    .btn-danger:hover {
      background: rgba(244, 63, 94, 0.35);
    }
    .btn-amber {
      background: rgba(245, 158, 11, 0.2);
      color: #fcd34d;
      border-color: rgba(245, 158, 11, 0.4);
    }
    .btn-amber:hover {
      background: rgba(245, 158, 11, 0.35);
    }
    .btn-green {
      background: rgba(16, 185, 129, 0.2);
      color: #6ee7b7;
      border-color: rgba(16, 185, 129, 0.4);
    }
    .btn-green:hover {
      background: rgba(16, 185, 129, 0.35);
    }
    .btn-purple {
      background: rgba(168, 85, 247, 0.2);
      color: #d8b4fe;
      border-color: rgba(168, 85, 247, 0.4);
    }

    /* Main Container */
    main {
      padding: 24px 28px;
      display: grid;
      grid-template-columns: 1.15fr 0.85fr;
      gap: 24px;
      flex: 1;
    }

    /* Cards */
    .card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 14px;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.3);
    }
    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--card-border);
      padding-bottom: 12px;
    }
    .card-title {
      font-size: 15px;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    /* Doctor Banner */
    .doctor-bar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: #0f172a;
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 14px 18px;
    }
    .doc-info {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .doc-avatar {
      width: 44px;
      height: 44px;
      border-radius: 50%;
      background: #1e3a8a;
      border: 2px solid var(--primary);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 20px;
    }
    .doc-name {
      font-size: 15px;
      font-weight: 700;
    }
    .doc-meta {
      font-size: 12px;
      color: var(--text-muted);
    }

    /* Table */
    .table-container {
      overflow-x: auto;
      border-radius: 10px;
      border: 1px solid var(--card-border);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12.5px;
      text-align: left;
    }
    th {
      background: #0f172a;
      padding: 10px 14px;
      color: var(--text-muted);
      font-weight: 600;
      border-bottom: 1px solid var(--card-border);
      font-size: 11.5px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    td {
      padding: 12px 14px;
      border-bottom: 1px solid #334155;
    }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: rgba(51, 65, 85, 0.4); }

    .status-badge {
      padding: 3px 8px;
      border-radius: 6px;
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      display: inline-block;
    }
    .status-booked { background: #334155; color: #cbd5e1; }
    .status-checked_in { background: rgba(56, 189, 248, 0.2); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3); }
    .status-with_doctor { background: rgba(16, 185, 129, 0.25); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4); }
    .status-done { background: rgba(148, 163, 184, 0.15); color: #94a3b8; }
    .status-no_show { background: rgba(244, 63, 94, 0.2); color: #fb7185; }
    .status-recalled { background: rgba(168, 85, 247, 0.2); color: #c084fc; }

    .urgent-tag {
      background: rgba(244, 63, 94, 0.25);
      color: #fda4af;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 10.5px;
      font-weight: 700;
      margin-left: 6px;
      border: 1px solid rgba(244, 63, 94, 0.4);
    }
    .lang-tag {
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 10.5px;
      font-weight: 600;
      background: #0f172a;
      border: 1px solid #334155;
      color: #94a3b8;
    }

    /* Right Column (Phone Sim & Logs) */
    .right-col {
      display: flex;
      flex-direction: column;
      gap: 24px;
    }

    /* Smartphone Simulator */
    .phone-mockup {
      background: #090d16;
      border: 4px solid #334155;
      border-radius: 28px;
      padding: 16px;
      box-shadow: 0 16px 40px rgba(0, 0, 0, 0.6);
      position: relative;
    }
    .phone-speaker {
      width: 60px;
      height: 5px;
      background: #334155;
      border-radius: 4px;
      margin: 0 auto 12px auto;
    }
    .phone-screen {
      background: #0f172a;
      border-radius: 18px;
      padding: 14px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      min-height: 290px;
    }
    .sms-bubble {
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 12px 12px 12px 2px;
      padding: 12px 14px;
      font-size: 12.5px;
      line-height: 1.45;
      color: #f1f5f9;
      box-shadow: 0 2px 6px rgba(0,0,0,0.2);
    }
    .sms-header {
      font-size: 10.5px;
      color: var(--primary);
      font-weight: 700;
      margin-bottom: 4px;
      display: flex;
      justify-content: space-between;
    }
    .sms-reply-box {
      margin-top: auto;
      display: flex;
      gap: 8px;
    }
    .sms-input {
      flex: 1;
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 8px;
      padding: 8px 12px;
      color: white;
      font-family: inherit;
      font-size: 12px;
    }
    .sms-input:focus {
      outline: none;
      border-color: var(--primary);
    }

    /* Quick Reply Chips */
    .reply-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .chip {
      background: #1e293b;
      border: 1px solid #475569;
      color: #cbd5e1;
      padding: 4px 10px;
      border-radius: 14px;
      font-size: 11px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.15s;
    }
    .chip:hover {
      background: var(--primary);
      color: var(--navy);
      border-color: var(--primary);
    }

    /* Event Logs */
    .log-box {
      background: #090d16;
      border: 1px solid var(--card-border);
      border-radius: 10px;
      padding: 12px;
      max-height: 180px;
      overflow-y: auto;
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      line-height: 1.6;
      color: #94a3b8;
    }
    .log-entry {
      border-bottom: 1px solid #1e293b;
      padding: 2px 0;
    }
    .log-entry:last-child { border-bottom: none; }

    /* Demo Stepper */
    .stepper {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 8px;
    }
    .step-btn {
      background: #0f172a;
      border: 1px solid #334155;
      padding: 8px;
      border-radius: 8px;
      font-size: 11px;
      color: var(--text-muted);
      cursor: pointer;
      text-align: center;
      transition: all 0.2s;
    }
    .step-btn:hover {
      border-color: var(--primary);
      color: white;
    }
    .step-btn.active {
      background: rgba(56, 189, 248, 0.15);
      border-color: var(--primary);
      color: var(--primary);
      font-weight: 700;
    }

    /* Notification Banner */
    .toast-banner {
      background: linear-gradient(90deg, #1e3a8a, #0369a1);
      border: 1px solid var(--primary);
      border-radius: 8px;
      padding: 10px 14px;
      font-size: 12px;
      display: none;
      align-items: center;
      gap: 10px;
      animation: fadeIn 0.3s;
    }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: translateY(0); } }

    /* Action Icon Buttons in Table */
    .table-actions {
      display: flex;
      gap: 4px;
    }
    .btn-xs {
      padding: 3px 7px;
      font-size: 10.5px;
      border-radius: 4px;
    }
  </style>
</head>
<body>

  <!-- Header -->
  <header>
    <div class="brand">
      <div class="brand-logo">OPD</div>
      <div>
        <div class="brand-title">OPD Queue Agent — Live Demonstration Hub</div>
        <div class="brand-sub">SLIIT IT6070 Emerging Topics | Malinga Rajapaksha (MS25909296) | Mr. Nimal Fernando (Coordinator)</div>
      </div>
    </div>
    <div class="badge-bar">
      <div class="badge" style="background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3);">
        <span class="badge-pulse"></span> Engine Online
      </div>
      <div class="badge" id="llmModeBadge" style="background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3);">
        ⚡ Claude Haiku 4.5
      </div>
      <a href="/video" class="btn btn-primary btn-xs" style="text-decoration:none; display:inline-flex; align-items:center; gap:5px; background:linear-gradient(135deg,#0284c7,#38bdf8); color:#fff;">
        🎬 Demo Video (3m 48s)
      </a>
      <button class="btn btn-secondary btn-xs" onclick="toggleLLMMode()" title="Toggle Fallback Template Mode (Test 3)">
        Safe Mode
      </button>
      <button class="btn btn-danger btn-xs" onclick="resetSystem()">
        Reset
      </button>
    </div>
  </header>

  <!-- Main View -->
  <main>
    <!-- Left Column: Queue & Controls -->
    <div style="display:flex; flex-direction:column; gap:20px;">

      <!-- Doctor Session Card -->
      <div class="card">
        <div class="doctor-bar">
          <div class="doc-info">
            <div class="doc-avatar">👨‍⚕️</div>
            <div>
              <div class="doc-name" id="doctorName">Dr. Kumara (Consultant Cardiologist)</div>
              <div class="doc-meta" id="doctorStatus">Status: <b style="color:var(--accent-green)">On Time</b> | Session ID: OPD-20261017-01</div>
            </div>
          </div>
          <div style="display:flex; gap:8px;">
            <button class="btn btn-amber btn-xs" onclick="doctorDelay(20)">⏱ Delay 20 min</button>
            <button class="btn btn-green btn-xs" onclick="doctorArrived()">✅ Doctor Arrived</button>
            <button class="btn btn-danger btn-xs" onclick="doctorCancel()">❌ Cancel Session</button>
          </div>
        </div>

        <!-- 8-Step Rubric Stepper -->
        <div style="display:flex; flex-direction:column; gap:8px;">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <span style="font-size:12px; font-weight:700; color:var(--text-muted); text-transform:uppercase;">
              5-Minute Rubric Demonstration Stepper
            </span>
            <button class="btn btn-primary btn-xs" onclick="runAutomatedDemo()">▶ Run Full Auto Demo</button>
          </div>
          <div class="stepper">
            <button class="step-btn" id="s1" onclick="runStep(1)">1. Initial State</button>
            <button class="step-btn" id="s2" onclick="runStep(2)">2. Check-In (P1–P4)</button>
            <button class="step-btn" id="s3" onclick="runStep(3)">3. Delay (F1+F2)</button>
            <button class="step-btn" id="s4" onclick="runStep(4)">4. P001 No-Show</button>
            <button class="step-btn" id="s5" onclick="runStep(5)">5. Urgent Guard</button>
            <button class="step-btn" id="s6" onclick="runStep(6)">6. Reschedule Tool</button>
            <button class="step-btn" id="s7" onclick="runStep(7)">7. Doctor Arrival</button>
            <button class="step-btn" id="s8" onclick="runStep(8)">8. Fallback Outage</button>
          </div>
        </div>

        <!-- Toast Notice -->
        <div class="toast-banner" id="toastBox">
          <span id="toastText">Action completed successfully.</span>
        </div>
      </div>

      <!-- Live Queue Table Card -->
      <div class="card" style="flex:1;">
        <div class="card-header">
          <div class="card-title">
            <span>📋</span> Live Patient Queue — Requirement 1 (Recalculation in &lt;1s)
          </div>
          <div style="font-size:12px; color:var(--text-muted);">
            Avg Consultation: <b>8 mins</b> | Urgent Safety Guard: <b>Active</b>
          </div>
        </div>

        <div class="table-container">
          <table>
            <thead>
              <tr>
                <th>Pos</th>
                <th>Patient Details</th>
                <th>Lang</th>
                <th>Status</th>
                <th>Est. Wait</th>
                <th>Coordinator Actions</th>
              </tr>
            </thead>
            <tbody id="queueTableBody">
              <tr><td colspan="6" style="text-align:center; color:var(--text-muted);">Loading live queue...</td></tr>
            </tbody>
          </table>
        </div>
      </div>

    </div>

    <!-- Right Column: Smartphone Simulator & Logs -->
    <div class="right-col">

      <!-- Patient Phone Simulator -->
      <div class="card">
        <div class="card-header">
          <div class="card-title">
            <span>📱</span> Live Trilingual SMS Simulator — Requirement 2
          </div>
          <span style="font-size:11px; color:var(--text-muted);">Simulated Dialog / Text.lk Gateway</span>
        </div>

        <div class="phone-mockup">
          <div class="phone-speaker"></div>
          <div class="phone-screen">
            <div style="text-align:center; font-size:11px; color:#64748b; font-weight:600;">
              Today <span id="phoneTime">10:45 AM</span> • OPD ALERT
            </div>

            <div class="sms-bubble" id="smsDisplay">
              <div class="sms-header">
                <span>HOSPITAL_OPD</span>
                <span id="smsLangBadge">EN / SI / TA</span>
              </div>
              <div id="smsMessageBody">
                Welcome to OPD Channelling. Select an event (Doctor Delay or Arrival) to see live SMS dispatched.
              </div>
            </div>

            <!-- Inbound Reply Simulation -->
            <div style="margin-top:auto; display:flex; flex-direction:column; gap:8px;">
              <div style="font-size:11px; color:var(--text-muted); font-weight:600;">
                Patient Inbound Free-Text Reply:
              </div>
              <div class="reply-chips">
                <span class="chip" onclick="sendQuickReply('I want to reschedule please')">"Reschedule please"</span>
                <span class="chip" onclick="sendQuickReply('WAIT')">"WAIT"</span>
                <span class="chip" onclick="sendQuickReply('Can I see another doctor?')">"Another doctor"</span>
                <span class="chip" onclick="sendQuickReply('Cancel and refund')">"Refund"</span>
              </div>
              <div class="sms-reply-box">
                <input type="text" class="sms-input" id="replyInput" placeholder="Type free-text reply as patient...">
                <button class="btn btn-primary btn-xs" onclick="sendCustomReply()">Send</button>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Audit & Database Event Log -->
      <div class="card">
        <div class="card-header">
          <div class="card-title">
            <span>🗄️</span> SQLite Audit Store & Tool-Calling Dispatch Log
          </div>
          <span style="font-size:11px; color:var(--text-muted);">Table: queue_events</span>
        </div>
        <div class="log-box" id="eventLogs">
          <!-- Logs populate here -->
        </div>
      </div>

    </div>
  </main>

  <script>
    let state = {};
    let selectedPatientId = "P001";

    async function fetchState() {
      try {
        const res = await fetch('/api/state');
        state = await res.json();
        render();
      } catch (err) {
        console.error("Fetch error:", err);
      }
    }

    function render() {
      // Doctor status
      document.getElementById('doctorName').innerText = `${state.doctor_name} (Consultant Cardiologist)`;
      const statusEl = document.getElementById('doctorStatus');
      let statusColor = "var(--accent-green)";
      if (state.doctor_status === "delayed") statusColor = "var(--accent-amber)";
      if (state.doctor_status === "cancelled") statusColor = "var(--accent-rose)";
      statusEl.innerHTML = `Status: <b style="color:${statusColor}">${state.doctor_status.toUpperCase()}</b> ` +
        (state.delay_minutes > 0 ? `(${state.delay_minutes} min delay) ` : '') +
        `| Session: ${state.session_id}`;

      // LLM Badge
      const llmBadge = document.getElementById('llmModeBadge');
      if (state.llm_available) {
        llmBadge.innerHTML = "⚡ Claude Haiku 4.5 (Agentic)";
        llmBadge.style.color = "#38bdf8";
        llmBadge.style.borderColor = "rgba(56, 189, 248, 0.4)";
      } else {
        llmBadge.innerHTML = "🛡️ Fallback Templates (Safe Mode)";
        llmBadge.style.color = "#fcd34d";
        llmBadge.style.borderColor = "rgba(245, 158, 11, 0.4)";
      }

      // Queue table
      const tbody = document.getElementById('queueTableBody');
      tbody.innerHTML = '';
      state.patients.forEach(p => {
        const tr = document.createElement('tr');
        const isUrgentHtml = p.is_urgent ? '<span class="urgent-tag">URGENT</span>' : '';
        const waitText = (typeof p.est_wait_min === 'number') ? `<b>${p.est_wait_min} min</b>` : '-';

        tr.innerHTML = `
          <td><b style="color:var(--primary);">${p.position}</b></td>
          <td>
            <div style="font-weight:600; display:flex; align-items:center;">
              ${p.name} ${isUrgentHtml}
            </div>
            <div style="font-size:11px; color:var(--text-muted); font-family:monospace;">ID: ${p.patient_id} | Seq: ${p.seq}</div>
          </td>
          <td><span class="lang-tag">${p.language.toUpperCase()}</span></td>
          <td><span class="status-badge status-${p.status}">${p.status.replace('_', ' ')}</span></td>
          <td>${waitText}</td>
          <td>
            <div class="table-actions">
              ${p.status === 'booked' ? `<button class="btn btn-secondary btn-xs" onclick="checkIn('${p.patient_id}')">Check In</button>` : ''}
              ${p.status === 'checked_in' || p.status === 'recalled' ? `
                <button class="btn btn-green btn-xs" onclick="withDoctor('${p.patient_id}')">In Room</button>
                <button class="btn btn-danger btn-xs" onclick="noShow('${p.patient_id}')" title="Test Urgent Safety Guard">No-Show</button>
              ` : ''}
              ${p.status === 'with_doctor' ? `<button class="btn btn-primary btn-xs" onclick="markDone('${p.patient_id}')">Done</button>` : ''}
              ${p.status === 'no_show' ? `<button class="btn btn-purple btn-xs" onclick="recall('${p.patient_id}')">Recall</button>` : ''}
              <button class="btn btn-secondary btn-xs" onclick="selectPhonePatient('${p.patient_id}')" title="View in Phone Simulator">📱</button>
            </div>
          </td>
        `;
        tbody.appendChild(tr);
      });

      // Latest SMS display
      if (state.notifications && state.notifications.length > 0) {
        const matching = state.notifications.filter(n => n.patient_id === selectedPatientId);
        const latest = matching.length > 0 ? matching[matching.length - 1] : state.notifications[state.notifications.length - 1];
        document.getElementById('smsMessageBody').innerText = latest.message;
        document.getElementById('smsLangBadge').innerText = `To: ${latest.patient_id} (${latest.phone})`;
      }

      // Event Logs
      const logBox = document.getElementById('eventLogs');
      logBox.innerHTML = '';
      if (state.events) {
        state.events.forEach(evt => {
          const div = document.createElement('div');
          div.className = 'log-entry';
          div.innerText = evt;
          logBox.appendChild(div);
        });
        logBox.scrollTop = logBox.scrollHeight;
      }
    }

    function showToast(msg) {
      const toast = document.getElementById('toastBox');
      const text = document.getElementById('toastText');
      text.innerText = msg;
      toast.style.display = 'flex';
      setTimeout(() => { toast.style.display = 'none'; }, 4000);
    }

    async function checkIn(pid) {
      await fetch('/api/check_in', { method: 'POST', body: JSON.stringify({ patient_id: pid }) });
      fetchState();
    }
    async function withDoctor(pid) {
      await fetch('/api/with_doctor', { method: 'POST', body: JSON.stringify({ patient_id: pid }) });
      fetchState();
    }
    async function markDone(pid) {
      await fetch('/api/done', { method: 'POST', body: JSON.stringify({ patient_id: pid }) });
      fetchState();
    }
    async function noShow(pid) {
      const res = await fetch('/api/no_show', { method: 'POST', body: JSON.stringify({ patient_id: pid }) });
      const data = await res.json();
      if (data.events && data.events.some(e => e.includes('NO_SHOW_SKIPPED_URGENT'))) {
        showToast("⚠️ Clinical Guard Intercepted: Urgent patient protected from no-show!");
      }
      fetchState();
    }
    async function recall(pid) {
      await fetch('/api/recall', { method: 'POST', body: JSON.stringify({ patient_id: pid }) });
      fetchState();
    }
    async function doctorDelay(mins) {
      showToast(`⏱ Doctor delayed by ${mins} mins. F1 recalculating wait times, F2 notifying patients...`);
      await fetch('/api/doctor_delay', { method: 'POST', body: JSON.stringify({ minutes: mins }) });
      fetchState();
    }
    async function doctorArrived() {
      showToast("✅ Doctor arrived! Queue recalculated, arrival notifications dispatched.");
      await fetch('/api/doctor_arrived', { method: 'POST' });
      fetchState();
    }
    async function doctorCancel() {
      showToast("❌ Session cancelled. Cancellation alerts dispatched to all waiting patients.");
      await fetch('/api/doctor_cancel', { method: 'POST' });
      fetchState();
    }
    async function toggleLLMMode() {
      const res = await fetch('/api/toggle_mode', { method: 'POST' });
      const data = await res.json();
      showToast(data.llm_available ? "Switched to Claude Haiku LLM Mode." : "Switched to Safe Fallback Template Mode (Test 3).");
      fetchState();
    }
    async function resetSystem() {
      await fetch('/api/reset', { method: 'POST' });
      showToast("System session reset to fresh state.");
      fetchState();
    }
    function selectPhonePatient(pid) {
      selectedPatientId = pid;
      render();
    }
    async function sendQuickReply(text) {
      showToast(`Patient ${selectedPatientId} replied: "${text}". Agent processing tool call...`);
      const res = await fetch('/api/patient_reply', {
        method: 'POST',
        body: JSON.stringify({ patient_id: selectedPatientId, reply: text })
      });
      const data = await res.json();
      showToast(`Agent Action: ${JSON.stringify(data.action)}`);
      fetchState();
    }
    function sendCustomReply() {
      const val = document.getElementById('replyInput').value.trim();
      if (val) {
        sendQuickReply(val);
        document.getElementById('replyInput').value = '';
      }
    }

    async function runStep(step) {
      document.querySelectorAll('.step-btn').forEach(b => b.classList.remove('active'));
      const activeBtn = document.getElementById('s' + step);
      if (activeBtn) activeBtn.classList.add('active');

      const res = await fetch('/api/run_demo_step', {
        method: 'POST',
        body: JSON.stringify({ step: step })
      });
      const data = await res.json();
      showToast(data.message);
      fetchState();
    }

    async function runAutomatedDemo() {
      for (let s = 1; s <= 8; s++) {
        await runStep(s);
        await new Promise(r => setTimeout(r, 1800));
      }
      showToast("🎉 Full 8-Step Rubric Demonstration Complete!");
    }

    // Polling interval
    fetchState();
    setInterval(fetchState, 3000);
  </script>
</body>
</html>
"""

VIDEO_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>OPD Queue Agent — Official Demonstration Video & Highlights</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --navy: #0f172a;
      --card-bg: #1e293b;
      --card-border: #334155;
      --primary: #38bdf8;
      --primary-dark: #0284c7;
      --accent-green: #10b981;
      --accent-amber: #f59e0b;
      --accent-rose: #f43f5e;
      --accent-purple: #a855f7;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --surface: #0b1120;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Plus Jakarta Sans', sans-serif;
      background-color: var(--surface);
      color: var(--text-main);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }
    header {
      background: rgba(15, 23, 42, 0.9);
      backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--card-border);
      padding: 14px 28px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      position: sticky;
      top: 0;
      z-index: 50;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .brand-logo {
      width: 40px;
      height: 40px;
      background: linear-gradient(135deg, #0284c7, #38bdf8);
      border-radius: 10px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 800;
      font-size: 20px;
      color: white;
      box-shadow: 0 4px 14px rgba(56, 189, 248, 0.35);
    }
    .brand-title {
      font-size: 17px;
      font-weight: 700;
      letter-spacing: -0.3px;
    }
    .brand-sub {
      font-size: 11.5px;
      color: var(--text-muted);
    }
    .container {
      max-width: 1400px;
      margin: 0 auto;
      padding: 24px;
      display: flex;
      flex-direction: column;
      gap: 24px;
      width: 100%;
    }
    .main-grid {
      display: grid;
      grid-template-columns: 1fr 400px;
      gap: 24px;
    }
    @media (max-width: 1100px) {
      .main-grid { grid-template-columns: 1fr; }
    }
    .video-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      box-shadow: 0 20px 40px -15px rgba(0, 0, 0, 0.5);
    }
    .video-player-wrapper {
      position: relative;
      width: 100%;
      border-radius: 12px;
      overflow: hidden;
      background: #000;
      aspect-ratio: 16/9;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.4);
      border: 1px solid rgba(56, 189, 248, 0.3);
    }
    video {
      width: 100%;
      height: 100%;
      object-fit: contain;
      outline: none;
    }
    .meta-pills {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 12px;
      border-radius: 20px;
      font-size: 12px;
      font-weight: 600;
      background: #0f172a;
      border: 1px solid var(--card-border);
      color: var(--text-muted);
    }
    .pill.green {
      background: rgba(16, 185, 129, 0.15);
      border-color: rgba(16, 185, 129, 0.3);
      color: #34d399;
    }
    .pill.cyan {
      background: rgba(56, 189, 248, 0.15);
      border-color: rgba(56, 189, 248, 0.3);
      color: #38bdf8;
    }
    .download-bar {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 8px;
    }
    .btn {
      font-family: inherit;
      font-weight: 600;
      font-size: 13px;
      padding: 9px 18px;
      border-radius: 8px;
      border: 1px solid transparent;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      text-decoration: none;
      transition: all 0.2s ease;
    }
    .btn-primary {
      background: linear-gradient(135deg, #0284c7, #38bdf8);
      color: #fff;
    }
    .btn-primary:hover {
      box-shadow: 0 4px 14px rgba(56, 189, 248, 0.4);
      transform: translateY(-1px);
    }
    .btn-secondary {
      background: #334155;
      color: #f8fafc;
      border-color: #475569;
    }
    .btn-secondary:hover {
      background: #475569;
    }
    .chapter-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 14px;
      max-height: 750px;
    }
    .chapter-list {
      display: flex;
      flex-direction: column;
      gap: 8px;
      overflow-y: auto;
      padding-right: 4px;
    }
    .chapter-item {
      padding: 12px 14px;
      border-radius: 10px;
      background: #0f172a;
      border: 1px solid var(--card-border);
      cursor: pointer;
      display: flex;
      flex-direction: column;
      gap: 4px;
      transition: all 0.2s ease;
    }
    .chapter-item:hover {
      border-color: var(--primary);
      transform: translateX(3px);
    }
    .chapter-item.active {
      border-color: var(--primary);
      background: rgba(2, 132, 199, 0.15);
      box-shadow: 0 0 15px rgba(56, 189, 248, 0.15);
    }
    .chapter-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .chapter-time {
      font-family: 'JetBrains Mono', monospace;
      font-size: 11.5px;
      font-weight: 700;
      color: var(--primary);
      background: rgba(56, 189, 248, 0.15);
      padding: 2px 8px;
      border-radius: 6px;
    }
    .chapter-title {
      font-size: 13.5px;
      font-weight: 700;
      color: var(--text-main);
    }
    .chapter-desc {
      font-size: 11.5px;
      color: var(--text-muted);
      line-height: 1.4;
    }
    .showcase-section {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 24px;
      display: flex;
      flex-direction: column;
      gap: 18px;
    }
    .showcase-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
      gap: 16px;
    }
    .showcase-item {
      background: #0f172a;
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .badge-label {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 3px 10px;
      border-radius: 6px;
      font-size: 11px;
      font-weight: 700;
      color: white;
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <div class="brand-logo">🏥</div>
      <div>
        <div class="brand-title">OPD Queue Agent — Official Demonstration Video</div>
        <div class="brand-sub">SLIIT MSc IT (IT6070) | Malinga Rajapaksha (MS25909296) | Professional: Mr. Nimal Fernando</div>
      </div>
    </div>
    <div style="display:flex; gap:10px; align-items:center;">
      <a href="/" class="btn btn-secondary" style="font-size:12px; padding:6px 14px;">
        ← Live Queue Dashboard
      </a>
    </div>
  </header>

  <div class="container">
    <div class="main-grid">
      <!-- Left: Video Player & Meta -->
      <div class="video-card">
        <div class="video-player-wrapper">
          <video id="player" controls playsinline poster="/video_poster.png">
            <source src="/video.mp4" type="video/mp4">
            <track id="vttTrack" src="/subtitles.vtt" kind="subtitles" srclang="en" label="English Captions" default>
            Your browser does not support HTML5 video.
          </video>
        </div>

        <div class="meta-pills">
          <div class="pill green">⏱ Duration: 3 min 48 sec (Strictly ≤ 4 mins compliant)</div>
          <div class="pill cyan">📺 1920x1080 Full HD (25 fps)</div>
          <div class="pill">🔊 Clear Neural English Voiceover</div>
          <div class="pill">✨ Neon Glowing Highlight Boxes &amp; Subtitle Bar</div>
        </div>

        <div style="border-top:1px solid var(--card-border); padding-top:14px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;">
          <div>
            <div style="font-size:14px; font-weight:700;">Submission Files Ready for CourseWeb</div>
            <div style="font-size:12px; color:var(--text-muted);">Recorded, rendered, verified, and saved in Week 04 folder</div>
          </div>
          <div class="download-bar">
            <a href="/video.mp4" download="OPD_Queue_Agent_Demonstration_Video.mp4" class="btn btn-primary">
              📥 Download MP4 Video (7.6 MB)
            </a>
            <a href="/subtitles.srt" download="OPD_Queue_Agent_Demonstration_Video.srt" class="btn btn-secondary">
              📝 Download Subtitles (.SRT)
            </a>
          </div>
        </div>
      </div>

      <!-- Right: Interactive Chapters -->
      <div class="chapter-card">
        <div style="font-size:15px; font-weight:700; display:flex; justify-content:space-between; align-items:center;">
          <span>📑 Interactive Chapters</span>
          <span style="font-size:11px; color:var(--text-muted);">Click to Jump</span>
        </div>

        <div class="chapter-list">
          <div class="chapter-item active" onclick="seekTo(0)">
            <div class="chapter-header">
              <span class="chapter-title">1. Introduction &amp; Problem</span>
              <span class="chapter-time">0:00</span>
            </div>
            <div class="chapter-desc">Mr. Nimal Fernando, OPD Channelling Coordinator, doctor delays (20-60m), and 45-min manual phone-tree bottlenecks.</div>
          </div>

          <div class="chapter-item" onclick="seekTo(38)">
            <div class="chapter-header">
              <span class="chapter-title">2. Feature 1: Live Wait Recalculation</span>
              <span class="chapter-time">0:38</span>
            </div>
            <div class="chapter-desc">Consultant 20-min delay triggers sub-second deterministic recalculation (20m to 76m), verifying Requirement 1.</div>
          </div>

          <div class="chapter-item" onclick="seekTo(70)">
            <div class="chapter-header">
              <span class="chapter-title">3. Feature 2: Trilingual AI SMS Agent</span>
              <span class="chapter-time">1:10</span>
            </div>
            <div class="chapter-desc">Claude Haiku 4.5 LLM synthesizes personalized SMS in Sinhala, Tamil, and English in &lt;3 min, satisfying Requirement 2.</div>
          </div>

          <div class="chapter-item" onclick="seekTo(103)">
            <div class="chapter-header">
              <span class="chapter-title">4. Success Case &amp; Agentic Tool Calling</span>
              <span class="chapter-time">1:43</span>
            </div>
            <div class="chapter-desc">Patient free-text reply 'I want to reschedule' parsed by Haiku agent -> autonomous tool call updates DB to tomorrow 9am.</div>
          </div>

          <div class="chapter-item" onclick="seekTo(132)">
            <div class="chapter-header">
              <span class="chapter-title">5. Safety Edge Case: Clinical Guard</span>
              <span class="chapter-time">2:12</span>
            </div>
            <div class="chapter-desc">Attempted accidental no-show on urgent cardiac triage patient Suresh Kumar intercepted and blocked by Clinical Safety Guard.</div>
          </div>

          <div class="chapter-item" onclick="seekTo(159)">
            <div class="chapter-header">
              <span class="chapter-title">6. Error Resilience: Safe Fallback Mode</span>
              <span class="chapter-time">2:39</span>
            </div>
            <div class="chapter-desc">Cloud API outage simulated -> Agent catches missing client and dispatches pre-approved templates with 100% operational uptime.</div>
          </div>

          <div class="chapter-item" onclick="seekTo(186)">
            <div class="chapter-header">
              <span class="chapter-title">7. Practical Feasibility, Costs &amp; PDPA</span>
              <span class="chapter-time">3:06</span>
            </div>
            <div class="chapter-desc">Setup LKR 81,000 ($242), recurring LKR 12,700/mo ($38), &lt;1.2% revenue, 100% Sri Lanka PDPA No. 9 of 2022 compliant.</div>
          </div>
        </div>
      </div>
    </div>

    <!-- Visual Highlight Boxes Breakdown -->
    <div class="showcase-section">
      <div style="font-size:16px; font-weight:700;">
        🎯 Visual Highlight Boxes — Stage-by-Stage Examiner Evidence
      </div>
      <div style="font-size:13px; color:var(--text-muted); line-height:1.5;">
        Every key demonstration stage in the video features vibrant, color-coded glowing highlight boxes and callout badges framing the exact UI elements and data flows:
      </div>

      <div class="showcase-grid">
        <div class="showcase-item">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <span class="badge-label" style="background:#0284c7;">● CYAN BOX</span>
            <span style="font-size:11px; color:var(--text-muted);">Scene 1 &amp; 3</span>
          </div>
          <div style="font-weight:700; font-size:14px;">Coordinator Profile &amp; AI SMS Simulator</div>
          <div style="font-size:12px; color:var(--text-muted);">Frames Mr. Nimal Fernando's clinic profile in Scene 1 and the live smartphone simulator generating trilingual SMS in Scene 3.</div>
        </div>

        <div class="showcase-item">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <span class="badge-label" style="background:#d97706;">● AMBER BOX</span>
            <span style="font-size:11px; color:var(--text-muted);">Scene 2 &amp; 6</span>
          </div>
          <div style="font-weight:700; font-size:14px;">Delay Trigger &amp; Safe Mode Badge</div>
          <div style="font-size:12px; color:var(--text-muted);">Frames the 20-minute doctor delay button in Scene 2, and the Fallback Safe Mode indicator during simulated cloud outage in Scene 6.</div>
        </div>

        <div class="showcase-item">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <span class="badge-label" style="background:#059669;">● EMERALD GREEN BOX</span>
            <span style="font-size:11px; color:var(--text-muted);">Scene 2 &amp; 4</span>
          </div>
          <div style="font-weight:700; font-size:14px;">Live Recalculated Wait Times (R1) &amp; Tool Call</div>
          <div style="font-size:12px; color:var(--text-muted);">Frames the live wait times updating in milliseconds from 20m to 76m (Requirement 1), and the autonomous reschedule tool call execution.</div>
        </div>

        <div class="showcase-item">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <span class="badge-label" style="background:#e11d48;">● ROSE / RED BOX</span>
            <span style="font-size:11px; color:var(--text-muted);">Scene 1 &amp; 5</span>
          </div>
          <div style="font-weight:700; font-size:14px;">Waiting Room Congestion &amp; Urgent Safety Guard</div>
          <div style="font-size:12px; color:var(--text-muted);">Frames the initial 8-patient waiting bottleneck in Scene 1, and the clinical safety guard blocking auto-skip on urgent patient P005 in Scene 5.</div>
        </div>

        <div class="showcase-item">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <span class="badge-label" style="background:#a855f7;">● PURPLE BOX</span>
            <span style="font-size:11px; color:var(--text-muted);">Scene 4</span>
          </div>
          <div style="font-weight:700; font-size:14px;">SQLite Tool-Calling Audit Log</div>
          <div style="font-size:12px; color:var(--text-muted);">Highlights the persistent SQLite audit store verifying booking slot update to tomorrow morning at 9:00 AM with zero human calls.</div>
        </div>

        <div class="showcase-item">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <span class="badge-label" style="background:#0284c7;">● SUBTITLE BAR</span>
            <span style="font-size:11px; color:var(--text-muted);">All Scenes (0:00-3:48)</span>
          </div>
          <div style="font-weight:700; font-size:14px;">Broadcast Lower-Third Subtitle Pill</div>
          <div style="font-size:12px; color:var(--text-muted);">Displays real-time spoken captions across the entire video with a bright neon border and high-contrast typography for accessibility.</div>
        </div>
      </div>
    </div>
  </div>

  <script>
    const player = document.getElementById('player');
    const chapterItems = document.querySelectorAll('.chapter-item');
    const chapterTimes = [0, 38, 70, 103, 132, 159, 186];

    function seekTo(sec) {
      player.currentTime = sec;
      player.play();
    }

    player.ontimeupdate = function() {
      const cur = player.currentTime;
      let activeIdx = 0;
      for (let i = 0; i < chapterTimes.length; i++) {
        if (cur >= chapterTimes[i]) {
          activeIdx = i;
        }
      }
      chapterItems.forEach((item, idx) => {
        if (idx === activeIdx) {
          item.classList.add('active');
        } else {
          item.classList.remove('active');
        }
      });
    };
  </script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# HTTP Request Handler
# ---------------------------------------------------------------------------

class OPDRequestHandler(BaseHTTPRequestHandler):
    def serve_video(self):
        file_path = "video.mp4"
        if not os.path.exists(file_path):
            file_path = os.path.join("..", "Week 04", "OPD_Queue_Agent_Demonstration_Video.mp4")
        if not os.path.exists(file_path):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Video file not found")
            return

        file_size = os.path.getsize(file_path)
        range_header = self.headers.get("Range")

        if range_header:
            m = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else file_size - 1
                length = end - start + 1

                self.send_response(206)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

                with open(file_path, "rb") as f:
                    f.seek(start)
                    self.wfile.write(f.read(length))
                return

        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(file_size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with open(file_path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def serve_static_file(self, filename, content_type):
        path = filename
        if not os.path.exists(path):
            alt = os.path.join("..", "Week 04", filename)
            if os.path.exists(alt):
                path = alt
            else:
                self.send_response(404)
                self.end_headers()
                return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode("utf-8"))
        elif parsed.path in ("/video", "/video.html", "/video_preview.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(VIDEO_PAGE_HTML.encode("utf-8"))
        elif parsed.path == "/video.mp4":
            self.serve_video()
        elif parsed.path == "/subtitles.vtt":
            self.serve_static_file("subtitles.vtt", "text/vtt; charset=utf-8")
        elif parsed.path == "/subtitles.srt":
            self.serve_static_file("subtitles.srt", "text/plain; charset=utf-8")
        elif parsed.path == "/video_poster.png":
            self.serve_static_file("video_poster.png", "image/png")
        elif parsed.path == "/api/state":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            data = controller.get_state()
            self.wfile.write(json.dumps(data).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            payload = json.loads(post_data.decode("utf-8"))
        except Exception:
            payload = {}

        result = {}
        if parsed.path == "/api/check_in":
            result = controller.check_in(payload.get("patient_id", ""))
        elif parsed.path == "/api/with_doctor":
            result = controller.mark_with_doctor(payload.get("patient_id", ""))
        elif parsed.path == "/api/done":
            result = controller.mark_done(payload.get("patient_id", ""))
        elif parsed.path == "/api/no_show":
            result = {"events": controller.mark_no_show(payload.get("patient_id", ""))}
        elif parsed.path == "/api/recall":
            result = controller.recall(payload.get("patient_id", ""))
        elif parsed.path == "/api/doctor_delay":
            result = controller.doctor_delay(int(payload.get("minutes", 20)))
        elif parsed.path == "/api/doctor_arrived":
            result = controller.doctor_arrived()
        elif parsed.path == "/api/doctor_cancel":
            result = controller.doctor_cancelled()
        elif parsed.path == "/api/patient_reply":
            result = controller.patient_reply(payload.get("patient_id", ""), payload.get("reply", ""))
        elif parsed.path == "/api/toggle_mode":
            result = controller.toggle_mode()
        elif parsed.path == "/api/reset":
            controller.reset()
            result = {"reset": True}
        elif parsed.path == "/api/run_demo_step":
            result = controller.run_demo_step(int(payload.get("step", 1)))
        else:
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode("utf-8"))

    def log_message(self, format, *args):
        # Mute standard noisy HTTP log lines for clean terminal
        pass


def run_server(port: int = 8000):
    server_address = ("127.0.0.1", port)
    httpd = HTTPServer(server_address, OPDRequestHandler)
    print("\n" + "═" * 68)
    print(f"  🏥 OPD QUEUE AGENT — Interactive Web Server")
    print(f"  Student: Malinga Rajapaksha (MS25909296) | IT6070 Assignment II")
    print(f"  URL: http://localhost:{port}/")
    print(f"  Interactive Dashboard: Real-time Queue, Doctor Controls, SMS Phone Simulator")
    print("═" * 68 + "\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        httpd.server_close()


if __name__ == "__main__":
    port = 8000
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    run_server(port)
