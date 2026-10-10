"""
test_cases.py
Three test cases for the OPD Queue Agent (Week 3 requirement).

Test 1 — Happy path: doctor delayed, all waiting patients notified.
Test 2 — No-show handling: urgent patient is NEVER skipped.
Test 3 — Error / fallback: LLM unavailable → template fallback used.

Each test prints:
  • Input
  • Expected result
  • Actual result
  • PASS / FAIL
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from io import StringIO
from typing import List

from queue_engine import (
    QueueEngine,
    Session,
    Patient,
    make_demo_session,
    init_db,
)
from notification_agent import (
    NotificationAgent,
    SimulatedChannel,
    SimulatedBookingBackend,
)

PASS = "✅ PASS"
FAIL = "❌ FAIL"


def separator(title: str) -> None:
    print(f"\n{'='*65}")
    print(f"  {title}")
    print(f"{'='*65}")


# ===========================================================================
# Test Case 1 — Happy path: doctor delayed, waiting patients notified
# ===========================================================================

def test_1_doctor_delay_notification() -> bool:
    separator("TEST 1 — Doctor Delayed → Notifications Sent to All Waiting Patients")

    print("\n  INPUT:")
    print("    Session with 8 synthetic patients.")
    print("    Patients P001–P004 checked in. Doctor marked delayed 25 min.")

    print("\n  EXPECTED:")
    print("    Events from set_doctor_delayed include 'DOCTOR_DELAYED:25', 'NOTIFY_ALL'.")
    print("    Estimates show positions 1-4 with est_wait_min based on delay.")
    print("    4 notifications sent (one per waiting patient).")
    print("    Each notification contains doctor name and delay info.")

    # --- Setup ---
    session = make_demo_session()
    engine = QueueEngine(session)
    for pid in ["P001", "P002", "P003", "P004"]:
        engine.check_in(pid)

    db = init_db(":memory:")
    channel = SimulatedChannel(db, session.session_id)
    booking = SimulatedBookingBackend()
    # Force template mode (no API key needed for test)
    agent = NotificationAgent.__new__(NotificationAgent)
    agent.engine = engine
    agent.channel = channel
    agent.booking = booking
    agent.db_conn = db
    agent.model = "claude-haiku-4-5"
    agent._client = None
    agent._llm_available = False

    events = engine.set_doctor_delayed(25)

    print("\n  ACTUAL:")
    print(f"    Events fired: {events}")

    results = agent.notify_all_waiting("delayed")
    estimates = engine.estimates()

    print(f"    Notifications sent: {len(results)}")
    for r in results:
        print(f"      {r['patient_id']} ({r['language']}) [{r['mode']}] → {r['message'][:60]}…")
    print(f"    Estimates: { {pid: v['est_wait_min'] for pid, v in estimates.items()} }")

    # --- Assertions ---
    ok = True
    if "DOCTOR_DELAYED:25" not in events:
        print(f"  {FAIL} DOCTOR_DELAYED:25 not in events")
        ok = False
    if "NOTIFY_ALL" not in events:
        print(f"  {FAIL} NOTIFY_ALL not in events")
        ok = False
    # 4 checked-in + 4 booked = 8 waiting (all 8 patients are waiting)
    if len(results) < 4:
        print(f"  {FAIL} Expected ≥4 notifications, got {len(results)}")
        ok = False
    # Estimates respect delay
    first_wait = list(estimates.values())[0]["est_wait_min"] if estimates else -1
    if first_wait < 25:
        print(f"  {FAIL} First wait {first_wait} < delay 25")
        ok = False
    # Check messages contain doctor name
    for r in results:
        if "Dr. Kumara" not in r["message"] and "Kumara" not in r["message"]:
            print(f"  {FAIL} Message for {r['patient_id']} missing doctor name")
            ok = False
            break

    print(f"\n  RESULT: {PASS if ok else FAIL}")
    return ok


# ===========================================================================
# Test Case 2 — Urgent patient NEVER auto-skipped as no-show
# ===========================================================================

def test_2_urgent_patient_no_show_protection() -> bool:
    separator("TEST 2 — Urgent Patient Protected from No-Show Skip")

    print("\n  INPUT:")
    print("    Patient P005 is marked is_urgent=True.")
    print("    Coordinator attempts mark_no_show('P005').")

    print("\n  EXPECTED:")
    print("    Event returned: 'NO_SHOW_SKIPPED_URGENT:P005'")
    print("    P005 status remains unchanged (not 'no_show').")
    print("    Queue estimates still include P005.")

    session = make_demo_session()
    engine = QueueEngine(session)
    engine.check_in("P005")

    # Verify urgent flag
    p005 = next(p for p in session.patients if p.patient_id == "P005")
    assert p005.is_urgent, "P005 must be urgent (test data error)"

    events = engine.mark_no_show("P005")
    status_after = p005.status
    estimates = engine.estimates()
    in_queue = "P005" in estimates

    print("\n  ACTUAL:")
    print(f"    Events: {events}")
    print(f"    P005 status after: {status_after}")
    print(f"    P005 in queue estimates: {in_queue}")

    ok = True
    if "NO_SHOW_SKIPPED_URGENT:P005" not in events:
        print(f"  {FAIL} Expected NO_SHOW_SKIPPED_URGENT event")
        ok = False
    if status_after == "no_show":
        print(f"  {FAIL} P005 was marked no_show — urgent protection failed")
        ok = False
    if not in_queue:
        print(f"  {FAIL} P005 disappeared from queue after failed no-show")
        ok = False

    print(f"\n  RESULT: {PASS if ok else FAIL}")
    return ok


# ===========================================================================
# Test Case 3 — Fallback: LLM unavailable → pre-approved template used
# ===========================================================================

def test_3_llm_unavailable_template_fallback() -> bool:
    separator("TEST 3 — LLM Unavailable → Template Fallback (Error/Fallback Case)")

    print("\n  INPUT:")
    print("    ANTHROPIC_API_KEY intentionally unset / agent in template-only mode.")
    print("    Doctor cancels session. Agent attempts to notify patient P003 (English).")

    print("\n  EXPECTED:")
    print("    Agent uses template fallback (mode='template').")
    print("    Message contains 'cancelled' or 'cancel' keyword.")
    print("    Message contains patient name and doctor name.")
    print("    Notification marked as sent=True (channel never fails).")

    # Force template-only (no API key)
    session = make_demo_session()
    engine = QueueEngine(session)
    engine.check_in("P003")

    db = init_db(":memory:")
    channel = SimulatedChannel(db, session.session_id)
    booking = SimulatedBookingBackend()

    # Build agent manually in template-only mode (simulates missing API key)
    agent = NotificationAgent.__new__(NotificationAgent)
    agent.engine = engine
    agent.channel = channel
    agent.booking = booking
    agent.db_conn = db
    agent.model = "claude-haiku-4-5"
    agent._client = None
    agent._llm_available = False

    events = engine.set_doctor_cancelled()

    print("\n  ACTUAL:")
    print(f"    Events: {events}")

    results = agent.notify_all_waiting("cancelled")
    p003_result = next((r for r in results if r["patient_id"] == "P003"), None)

    if p003_result:
        print(f"    P003 notification mode: {p003_result['mode']}")
        print(f"    P003 message: {p003_result['message']}")
        print(f"    Sent: {p003_result['sent']}")
    else:
        print("    P003 result: NOT FOUND")

    ok = True
    if p003_result is None:
        print(f"  {FAIL} No notification result for P003")
        return False
    if p003_result["mode"] != "template":
        print(f"  {FAIL} Expected mode=template, got {p003_result['mode']}")
        ok = False
    msg = p003_result["message"].lower()
    if "cancel" not in msg:
        print(f"  {FAIL} Message lacks 'cancel' keyword: {p003_result['message']}")
        ok = False
    if "rohan" not in msg and "fernando" not in msg:
        # P003 = Rohan Fernando
        print(f"  {FAIL} Patient name missing from message")
        ok = False
    if "kumara" not in msg:
        print(f"  {FAIL} Doctor name missing from message")
        ok = False
    if not p003_result["sent"]:
        print(f"  {FAIL} sent flag is False")
        ok = False

    print(f"\n  RESULT: {PASS if ok else FAIL}")
    return ok


# ===========================================================================
# Runner
# ===========================================================================

if __name__ == "__main__":
    print("\n" + "#" * 65)
    print("  OPD Queue Agent — Test Suite (Week 3)")
    print("  Student: Malinga Rajapaksha — MS25909296")
    print("#" * 65)

    results = {
        "Test 1 — Doctor Delay Notification": test_1_doctor_delay_notification(),
        "Test 2 — Urgent Patient Protection": test_2_urgent_patient_no_show_protection(),
        "Test 3 — LLM Fallback to Template":  test_3_llm_unavailable_template_fallback(),
    }

    print("\n" + "=" * 65)
    print("  SUMMARY")
    print("=" * 65)
    all_pass = True
    for name, passed in results.items():
        status = PASS if passed else FAIL
        print(f"  {status}  {name}")
        if not passed:
            all_pass = False

    print("=" * 65)
    sys.exit(0 if all_pass else 1)
