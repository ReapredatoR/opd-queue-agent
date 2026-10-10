"""
main.py
OPD Queue Agent — Interactive CLI demo.

Usage:
  python main.py [--demo] [--api-key YOUR_KEY]

  --demo       Run automated demo sequence (no user input needed)
  --api-key    Anthropic API key (overrides ANTHROPIC_API_KEY env var)

Without flags: interactive menu-driven session.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

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
# Helpers
# ---------------------------------------------------------------------------

def banner(text: str, char: str = "─") -> None:
    width = 64
    print(f"\n{char * width}")
    print(f"  {text}")
    print(f"{char * width}")


def print_queue(engine: QueueEngine) -> None:
    print(engine.queue_snapshot())


# ---------------------------------------------------------------------------
# Automated demo
# ---------------------------------------------------------------------------

def run_demo(agent: NotificationAgent, engine: QueueEngine) -> None:
    """
    Scripted demo that exercises both features and one fallback case.
    Suitable for a 60-90 second screen capture.
    """
    session = engine.session
    banner("OPD Queue Agent — Automated Demo", "═")
    print("  Student: Malinga Rajapaksha — MS25909296")
    print("  Professional: Mr. Nimal Fernando (pseudonym)")
    print("  Role: OPD Appointment Coordinator, Private Hospital, Sri Lanka")

    # Step 1: Initial queue
    banner("STEP 1 — Initial Queue State")
    print_queue(engine)

    # Step 2: Patients check in
    banner("STEP 2 — Patients P001–P004 Check In")
    for pid in ["P001", "P002", "P003", "P004"]:
        engine.check_in(pid)
    print_queue(engine)

    # Step 3: Doctor delayed (F1 + F2 trigger)
    banner("STEP 3 — Doctor Delayed 20 Minutes (F1 recalculates; F2 notifies)")
    events = engine.set_doctor_delayed(20)
    print(f"  Events fired: {events}")
    print()
    print_queue(engine)
    print()
    print("  [F2] Sending notifications to all waiting patients…")
    results = agent.notify_all_waiting("delayed")
    print(f"\n  ✅ {len(results)} notifications dispatched.")

    # Step 4: P001 no-show
    banner("STEP 4 — P001 (Anil Perera) is a No-Show")
    engine.mark_no_show("P001")
    print_queue(engine)

    # Step 5: Attempt no-show on urgent P005
    banner("STEP 5 — Attempt No-Show on URGENT Patient P005 (should be blocked)")
    events = engine.mark_no_show("P005")
    print(f"  Events: {events}")
    print_queue(engine)

    # Step 6: Patient reply interpretation
    banner("STEP 6 — Patient P002 Replies 'I want to reschedule'")
    action = agent.handle_reply("P002", "I want to reschedule please")
    print(f"  Action taken: {action}")

    # Step 7: Doctor arrives
    banner("STEP 7 — Doctor Arrives / Resumes")
    events = engine.set_doctor_arrived()
    print(f"  Events: {events}")
    results = agent.notify_all_waiting("doctor_arrived")
    print(f"  {len(results)} arrival notifications sent.")
    print_queue(engine)

    # Step 8: Limitation
    banner("STEP 8 — Identified Defect / Limitation")
    print("  LIMITATION: LLM message quality in Sinhala/Tamil not verified by a")
    print("  native speaker. Mitigation: use pre-approved templates as default;")
    print("  LLM drafts are opt-in after coordinator review.")
    print("\n  KNOWN BUG: If doctor_status transitions from 'cancelled' back to")
    print("  'on_time' without re-creating the session, stale estimates may linger.")
    print("  FIX PLAN: Reset delay_minutes=0 and re-emit QUEUE_UPDATED on any")
    print("  doctor status change (already done in set_doctor_arrived).")

    banner("Demo Complete", "═")


# ---------------------------------------------------------------------------
# Interactive menu
# ---------------------------------------------------------------------------

MENU = """
  OPD QUEUE AGENT — Main Menu
  ─────────────────────────────────────────
  1. Show current queue
  2. Check in patient
  3. Mark patient as with doctor
  4. Mark patient as done
  5. Mark patient as no-show
  6. Recall a no-show patient
  7. Set doctor delayed (minutes)
  8. Set doctor cancelled
  9. Set doctor arrived/resumed
  10. Notify all waiting patients
  11. Handle patient reply
  12. Exit
  ─────────────────────────────────────────
"""


def interactive_loop(agent: NotificationAgent, engine: QueueEngine) -> None:
    session = engine.session
    banner("OPD Queue Agent — Interactive Mode", "═")
    print(f"  Session: {session.session_id}  |  Doctor: {session.doctor_name}")
    print(f"  Patients: {len(session.patients)}")

    while True:
        print(MENU)
        choice = input("  Enter choice: ").strip()

        if choice == "1":
            print_queue(engine)

        elif choice == "2":
            pid = input("  Patient ID: ").strip()
            print(engine.check_in(pid))

        elif choice == "3":
            pid = input("  Patient ID: ").strip()
            print(engine.mark_with_doctor(pid))

        elif choice == "4":
            pid = input("  Patient ID: ").strip()
            print(engine.mark_done(pid))

        elif choice == "5":
            pid = input("  Patient ID: ").strip()
            events = engine.mark_no_show(pid)
            print(f"  Events: {events}")
            if "QUEUE_UPDATED" in events:
                print_queue(engine)

        elif choice == "6":
            pid = input("  Patient ID: ").strip()
            events = engine.recall_patient(pid)
            print(f"  Events: {events}")

        elif choice == "7":
            delay = int(input("  Delay (minutes): ").strip())
            events = engine.set_doctor_delayed(delay)
            print(f"  Events: {events}")
            if "NOTIFY_ALL" in events:
                yn = input("  Send notifications now? [y/N]: ").strip().lower()
                if yn == "y":
                    agent.notify_all_waiting("delayed")

        elif choice == "8":
            events = engine.set_doctor_cancelled()
            print(f"  Events: {events}")
            yn = input("  Send cancellation notifications? [y/N]: ").strip().lower()
            if yn == "y":
                agent.notify_all_waiting("cancelled")

        elif choice == "9":
            events = engine.set_doctor_arrived()
            print(f"  Events: {events}")
            if "NOTIFY_ALL" in events:
                agent.notify_all_waiting("doctor_arrived")

        elif choice == "10":
            reason = input("  Reason [delayed/cancelled/doctor_arrived]: ").strip()
            agent.notify_all_waiting(reason)

        elif choice == "11":
            pid = input("  Patient ID: ").strip()
            reply = input("  Patient reply text: ").strip()
            action = agent.handle_reply(pid, reply)
            print(f"  Action: {action}")

        elif choice == "12":
            print("  Exiting. Goodbye.")
            break

        else:
            print("  Invalid choice.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="OPD Queue Agent")
    parser.add_argument("--demo", action="store_true",
                        help="Run automated demo sequence")
    parser.add_argument("--api-key", default="",
                        help="Anthropic API key (overrides env var)")
    args = parser.parse_args()

    if args.api_key:
        os.environ["ANTHROPIC_API_KEY"] = args.api_key

    # Bootstrap
    session = make_demo_session()
    engine = QueueEngine(session)
    db = init_db()
    channel = SimulatedChannel(db, session.session_id)
    booking = SimulatedBookingBackend()
    agent = NotificationAgent(engine, channel, booking, db)

    if args.demo:
        run_demo(agent, engine)
        # Persist events to DB
        persist_events(db, session)
        print(f"\n  DB saved to: opd_queue.db")
    else:
        interactive_loop(agent, engine)
        persist_events(db, session)


if __name__ == "__main__":
    main()
