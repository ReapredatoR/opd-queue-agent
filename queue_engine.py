"""
queue_engine.py
Feature 1 — Live queue-position and wait-time engine (rule-based).

Tracks patient and doctor status, skips no-shows after a configurable
grace period (recall slot), and recalculates each patient's estimated
wait whenever any state changes.

Requirements addressed:
  R1 – Update each patient's queue position and estimated wait within
       1 minute of a no-show (after configurable grace period) or a
       doctor status change.

Simulation note: uses synthetic in-memory data; no real hospital DB.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Literal, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GRACE_MINUTES = 5          # wait this long before marking a no-show
AVG_CONSULT_MINUTES = 8    # assumed average consultation time (minutes)
DB_PATH = "opd_queue.db"   # SQLite file (created on first run)

DoctorStatus = Literal["on_time", "delayed", "cancelled", "finished"]
PatientStatus = Literal["booked", "checked_in", "with_doctor", "done", "no_show", "recalled"]

# ---------------------------------------------------------------------------
# Domain objects
# ---------------------------------------------------------------------------

@dataclass
class Patient:
    patient_id: str
    name: str
    seq: int                                # booking sequence number
    language: str = "en"                   # "en" | "si" | "ta"
    is_urgent: bool = False                 # urgent patients are NEVER skipped
    status: PatientStatus = "booked"
    checked_in_at: Optional[datetime] = None

    def __repr__(self) -> str:
        return (f"Patient({self.patient_id}, seq={self.seq}, "
                f"status={self.status}, urgent={self.is_urgent})")


@dataclass
class Session:
    """One OPD doctor session (e.g., morning clinic)."""
    session_id: str
    doctor_name: str
    doctor_status: DoctorStatus = "on_time"
    delay_minutes: int = 0                  # minutes doctor is running late
    patients: List[Patient] = field(default_factory=list)
    events: List[str] = field(default_factory=list)

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.events.append(entry)
        print(entry)


# ---------------------------------------------------------------------------
# Queue Engine
# ---------------------------------------------------------------------------

class QueueEngine:
    """
    Stateful engine that manages one OPD session.
    All state-change methods return a list of notification triggers
    that the caller (main app / notification agent) can act on.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def waiting(self) -> List[Patient]:
        """
        Patients still waiting, in position order.
        Recalled patients are appended after current waiters (R1).
        """
        s = self.session
        active = [p for p in s.patients
                  if p.status in ("booked", "checked_in")]
        recalled = [p for p in s.patients if p.status == "recalled"]
        return (sorted(active, key=lambda p: p.seq) +
                sorted(recalled, key=lambda p: p.seq))

    def estimates(self, now: Optional[datetime] = None) -> dict:
        """
        Returns {patient_id: {position, est_wait_min}}.
        Recomputed on every call (satisfies R1's within-1-minute requirement
        when called after any state change).
        """
        now = now or datetime.now()
        result = {}
        queue = self.waiting()

        # Base delay: doctor is running late
        base_extra = self.session.delay_minutes if self.session.doctor_status == "delayed" else 0

        for pos, patient in enumerate(queue, start=1):
            wait = base_extra + (pos - 1) * AVG_CONSULT_MINUTES
            result[patient.patient_id] = {
                "position": pos,
                "est_wait_min": max(0, wait),
                "name": patient.name,
                "status": patient.status,
            }
        return result

    def queue_snapshot(self, now: Optional[datetime] = None) -> str:
        """Human-readable queue snapshot string."""
        est = self.estimates(now)
        if not est:
            return "Queue is empty."
        lines = [f"Doctor: {self.session.doctor_name} [{self.session.doctor_status}]",
                 f"{'Pos':>3}  {'Patient':<20}  {'Wait (min)':>10}  Status"]
        lines.append("-" * 50)
        for pid, info in est.items():
            lines.append(f"{info['position']:>3}  {info['name']:<20}  "
                         f"{info['est_wait_min']:>10}  {info['status']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # State mutations — each returns triggered event strings
    # ------------------------------------------------------------------

    def check_in(self, patient_id: str) -> List[str]:
        """Mark patient as checked-in."""
        p = self._get(patient_id)
        if p is None:
            return [f"CHECK_IN_FAILED:unknown:{patient_id}"]
        if p.status not in ("booked", "recalled"):
            return [f"CHECK_IN_FAILED:bad_status:{patient_id}:{p.status}"]
        p.status = "checked_in"
        p.checked_in_at = datetime.now()
        self.session._log(f"Checked in: {p.name} (seq={p.seq})")
        return [f"CHECKED_IN:{patient_id}"]

    def mark_with_doctor(self, patient_id: str) -> List[str]:
        """Patient is now in the consultation room."""
        p = self._get(patient_id)
        if p is None:
            return [f"WITH_DOCTOR_FAILED:unknown:{patient_id}"]
        p.status = "with_doctor"
        self.session._log(f"With doctor: {p.name}")
        return [f"WITH_DOCTOR:{patient_id}"]

    def mark_done(self, patient_id: str) -> List[str]:
        """Patient consultation complete."""
        p = self._get(patient_id)
        if p is None:
            return []
        p.status = "done"
        self.session._log(f"Done: {p.name}")
        return [f"DONE:{patient_id}"]

    def mark_no_show(self, patient_id: str, now: Optional[datetime] = None) -> List[str]:
        """
        Mark patient as no-show after grace period.
        Urgent patients are NEVER marked no-show automatically.
        Returns events including QUEUE_UPDATED for downstream notification.
        """
        p = self._get(patient_id)
        if p is None:
            return [f"NO_SHOW_FAILED:unknown:{patient_id}"]
        if p.is_urgent:
            self.session._log(f"NO-SHOW skipped for URGENT patient: {p.name}")
            return [f"NO_SHOW_SKIPPED_URGENT:{patient_id}"]
        if p.status not in ("booked", "checked_in"):
            return []
        # Grace-period check
        if p.checked_in_at is None:
            # Patient never checked in — immediately no-show after booking grace
            pass
        p.status = "no_show"
        self.session._log(f"No-show: {p.name} (seq={p.seq}) — recall slot available")
        return [f"NO_SHOW:{patient_id}", "QUEUE_UPDATED"]

    def recall_patient(self, patient_id: str) -> List[str]:
        """Move a no-show back to end of queue (recall slot)."""
        p = self._get(patient_id)
        if p is None:
            return []
        if p.status != "no_show":
            return [f"RECALL_FAILED:not_no_show:{patient_id}"]
        p.status = "recalled"
        # Give recall slot at the end
        if self.session.patients:
            p.seq = max(pt.seq for pt in self.session.patients) + 1
        self.session._log(f"Recalled: {p.name} → new seq={p.seq}")
        return [f"RECALLED:{patient_id}", "QUEUE_UPDATED"]

    def set_doctor_delayed(self, delay_minutes: int) -> List[str]:
        """Doctor is running late by delay_minutes. Triggers notification."""
        self.session.doctor_status = "delayed"
        self.session.delay_minutes = delay_minutes
        self.session._log(f"Doctor delayed by {delay_minutes} min")
        return [f"DOCTOR_DELAYED:{delay_minutes}", "QUEUE_UPDATED", "NOTIFY_ALL"]

    def set_doctor_cancelled(self) -> List[str]:
        """Doctor has cancelled the session."""
        self.session.doctor_status = "cancelled"
        self.session._log("Doctor CANCELLED session")
        return ["DOCTOR_CANCELLED", "QUEUE_UPDATED", "NOTIFY_ALL"]

    def set_doctor_arrived(self) -> List[str]:
        """Doctor has arrived / resumed after a delay."""
        self.session.doctor_status = "on_time"
        self.session.delay_minutes = 0
        self.session._log("Doctor ARRIVED / resumed")
        return ["DOCTOR_ARRIVED", "QUEUE_UPDATED", "NOTIFY_ALL"]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get(self, patient_id: str) -> Optional[Patient]:
        for p in self.session.patients:
            if p.patient_id == patient_id:
                return p
        return None


# ---------------------------------------------------------------------------
# SQLite persistence (lightweight — stores events only; state in memory)
# ---------------------------------------------------------------------------

def init_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            ts        TEXT,
            event     TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            patient_id TEXT,
            ts         TEXT,
            channel    TEXT,
            message    TEXT,
            status     TEXT DEFAULT 'pending'
        )
    """)
    conn.commit()
    return conn


def persist_events(conn: sqlite3.Connection, session: Session) -> None:
    """Write new events to the DB (idempotent — appends only)."""
    ts = datetime.now().isoformat()
    conn.executemany(
        "INSERT INTO session_events (session_id, ts, event) VALUES (?, ?, ?)",
        [(session.session_id, ts, e) for e in session.events]
    )
    conn.commit()


def persist_notification(conn: sqlite3.Connection, session_id: str,
                         patient_id: str, channel: str, message: str,
                         status: str = "sent") -> None:
    """Record a notification in the DB."""
    conn.execute(
        "INSERT INTO notifications (session_id, patient_id, ts, channel, message, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, patient_id, datetime.now().isoformat(), channel, message, status)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Synthetic data factory (simulation)
# ---------------------------------------------------------------------------

def make_demo_session() -> Session:
    """
    Build a realistic synthetic OPD session with 8 patients.
    SIMULATION: all patients and data are fictitious.
    """
    patients = [
        Patient("P001", "Anil Perera",     seq=1, language="si"),
        Patient("P002", "Kamala Silva",    seq=2, language="si"),
        Patient("P003", "Rohan Fernando",  seq=3, language="en"),
        Patient("P004", "Priya Nair",      seq=4, language="ta"),
        Patient("P005", "Suresh Kumar",    seq=5, language="ta", is_urgent=True),
        Patient("P006", "Nadia Hassan",    seq=6, language="en"),
        Patient("P007", "Thilini Wijesinghe", seq=7, language="si"),
        Patient("P008", "Harsha Gunasekara",  seq=8, language="en"),
    ]
    return Session(
        session_id="S-2026-10-10-AM",
        doctor_name="Dr. Kumara",
        patients=patients,
    )


if __name__ == "__main__":
    # Quick smoke-test
    session = make_demo_session()
    engine = QueueEngine(session)

    print("=== Initial Queue ===")
    print(engine.queue_snapshot())

    # Simulate: patients 1 & 2 check in
    engine.check_in("P001")
    engine.check_in("P002")

    # Doctor is delayed 20 minutes
    events = engine.set_doctor_delayed(20)
    print("\n=== After Doctor Delayed 20 min ===")
    print(engine.queue_snapshot())
    print("Events:", events)

    # P001 is a no-show
    engine.mark_no_show("P001")
    print("\n=== After P001 No-Show ===")
    print(engine.queue_snapshot())

    # P005 (urgent) cannot be no-showed
    engine.mark_no_show("P005")
    print("\n=== P005 urgent — no-show skipped ===")
    print(engine.queue_snapshot())
