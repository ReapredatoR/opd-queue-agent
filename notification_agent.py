"""
notification_agent.py
Feature 2 — Delay and cancellation notification agent (LLM-powered).

Uses Claude claude-haiku-4-5 (Anthropic) with tool calling to:
  1. Draft a personalised message per patient in their language.
  2. "Send" it via a simulated SMS/WhatsApp channel.
  3. Fall back to a pre-approved template if the LLM or channel fails.
  4. Interpret free-text replies and dispatch booking actions.

SIMULATION NOTE:
  - No real SMS/WhatsApp channel is used; messages are printed and stored in SQLite.
  - LLM calls use the Anthropic API; API key is read from ANTHROPIC_API_KEY env var.
  - If ANTHROPIC_API_KEY is absent, the agent falls back to template mode automatically.

Requirements addressed:
  R2 – Notify all affected patients within 3 minutes of a doctor delay (>15 min)
       or cancellation, with actionable options (wait / reschedule / another doctor).
"""

from __future__ import annotations

import json
import os
import sys

# Ensure UTF-8 output even in standard Windows terminals
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from queue_engine import Patient, QueueEngine, Session, persist_notification

# ---------------------------------------------------------------------------
# Tool definitions for Claude tool-calling
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "send_sms",
        "description": (
            "Send an SMS or WhatsApp message to a patient. "
            "SIMULATION: messages are printed to console and stored in DB."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_id": {"type": "string", "description": "Patient identifier"},
                "phone":      {"type": "string", "description": "Destination phone (simulated)"},
                "message":    {"type": "string", "description": "Message body (≤ 160 chars for SMS)"},
            },
            "required": ["patient_id", "phone", "message"],
        },
    },
    {
        "name": "reschedule_appointment",
        "description": "Move a patient's appointment to the next available slot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_id":   {"type": "string"},
                "preferred_day": {"type": "string", "description": "e.g. 'tomorrow', 'next Monday'"},
            },
            "required": ["patient_id"],
        },
    },
    {
        "name": "assign_another_doctor",
        "description": "Assign the patient to the next available doctor of the same specialty.",
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "specialty":  {"type": "string"},
            },
            "required": ["patient_id", "specialty"],
        },
    },
]

# ---------------------------------------------------------------------------
# Pre-approved fallback templates
# ---------------------------------------------------------------------------

TEMPLATES: Dict[str, Dict[str, str]] = {
    "delayed": {
        "en": (
            "Dear {name}, your appointment with {doctor} is delayed by ~{delay} min. "
            "New est. wait: {wait} min. Reply WAIT / RESCHEDULE / OTHER_DOCTOR."
        ),
        "si": (
            "ආදරණීය {name}, ඔබේ {doctor} හමුව ~{delay} min ප‍්‍රමාද වේ. "
            "නව බලාපොරොත්තු: {wait} min. WAIT / RESCHEDULE / OTHER_DOCTOR reply කරන්න."
        ),
        "ta": (
            "அன்புள்ள {name}, உங்கள் {doctor} சந்திப்பு ~{delay} நிமிடம் தாமதமாகிறது. "
            "புதிய காத்திருப்பு: {wait} நிமிடம். WAIT / RESCHEDULE / OTHER_DOCTOR என்று பதில் அனுப்பவும்."
        ),
    },
    "cancelled": {
        "en": (
            "Dear {name}, your appointment with {doctor} has been cancelled today. "
            "Reply RESCHEDULE / OTHER_DOCTOR / REFUND."
        ),
        "si": (
            "ආදරණීය {name}, ඔබේ {doctor} හමුව අද අවලංගු විය. "
            "RESCHEDULE / OTHER_DOCTOR / REFUND reply කරන්න."
        ),
        "ta": (
            "அன்புள்ள {name}, உங்கள் {doctor} சந்திப்பு இன்று ரத்து செய்யப்பட்டது. "
            "RESCHEDULE / OTHER_DOCTOR / REFUND என்று பதில் அனுப்பவும்."
        ),
    },
    "doctor_arrived": {
        "en": (
            "Good news, {name}! {doctor} has arrived. "
            "Your est. wait is now {wait} min. Please be ready."
        ),
        "si": (
            "සුභ ආරංචි, {name}! {doctor} පැමිණියා. "
            "ඔබේ බලාපොරොත්තු: {wait} min. සූදානම් වන්න."
        ),
        "ta": (
            "நற்செய்தி {name}! {doctor} வந்துவிட்டார். "
            "உங்கள் காத்திருப்பு: {wait} நிமிடம். தயாராக இருங்கள்."
        ),
    },
}

# ---------------------------------------------------------------------------
# Simulated channel & booking backend
# ---------------------------------------------------------------------------

class SimulatedChannel:
    """Mimics an SMS/WhatsApp gateway. Logs messages; never fails silently."""

    def __init__(self, db_conn=None, session_id: str = ""):
        self.db_conn = db_conn
        self.session_id = session_id
        self._log: List[Dict] = []

    def send(self, patient_id: str, phone: str, message: str) -> bool:
        entry = {
            "ts": datetime.now().isoformat(),
            "patient_id": patient_id,
            "phone": phone,
            "message": message,
        }
        self._log.append(entry)
        print(f"\n  📱 [SIM-SMS → {phone}] {message}")
        if self.db_conn:
            persist_notification(self.db_conn, self.session_id,
                                 patient_id, "sim_sms", message, "sent")
        return True

    def get_log(self) -> List[Dict]:
        return list(self._log)


class SimulatedBookingBackend:
    """Simulated booking system — logs actions, no real DB write."""

    def __init__(self):
        self._actions: List[Dict] = []

    def reschedule(self, patient_id: str, preferred_day: str) -> str:
        slot = f"tomorrow 09:00" if preferred_day in ("tomorrow", "") else f"{preferred_day} 09:00"
        action = {"type": "reschedule", "patient_id": patient_id, "slot": slot}
        self._actions.append(action)
        print(f"  📅 [SIM-BOOK] Rescheduled {patient_id} → {slot}")
        return slot

    def assign_doctor(self, patient_id: str, specialty: str) -> str:
        dr = "Dr. Perera (Duty)"
        action = {"type": "other_doctor", "patient_id": patient_id, "doctor": dr}
        self._actions.append(action)
        print(f"  👨‍⚕️ [SIM-BOOK] Assigned {patient_id} → {dr} ({specialty})")
        return dr

    def get_actions(self) -> List[Dict]:
        return list(self._actions)


# ---------------------------------------------------------------------------
# Notification Agent
# ---------------------------------------------------------------------------

class NotificationAgent:
    """
    LLM-powered notification agent with tool calling.
    Falls back to pre-approved templates if LLM is unavailable.
    """

    def __init__(
        self,
        engine: QueueEngine,
        channel: SimulatedChannel,
        booking: SimulatedBookingBackend,
        db_conn=None,
        model: str = "claude-haiku-4-5",
    ) -> None:
        self.engine = engine
        self.channel = channel
        self.booking = booking
        self.db_conn = db_conn
        self.model = model
        self._client: Optional[Any] = None
        self._llm_available = False
        self._init_client()

    def _init_client(self) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            print("  ⚠️  ANTHROPIC_API_KEY not set — running in TEMPLATE-ONLY mode.")
            return
        try:
            import anthropic
            self._client = anthropic.Anthropic(api_key=api_key)
            self._llm_available = True
            print(f"  ✅ LLM client initialised ({self.model})")
        except ImportError:
            print("  ⚠️  anthropic package not installed — template-only mode.")
        except Exception as exc:
            print(f"  ⚠️  LLM init error: {exc} — template-only mode.")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def notify_all_waiting(self, reason: str) -> List[Dict]:
        """
        Notify every waiting patient about a doctor delay or cancellation.
        Returns list of notification result dicts.
        """
        session = self.engine.session
        estimates = self.engine.estimates()
        results = []

        print(f"\n{'='*60}")
        print(f"  NOTIFICATION BATCH — reason: {reason.upper()}")
        print(f"  Doctor: {session.doctor_name}  |  Delay: {session.delay_minutes} min")
        print(f"  Patients to notify: {len(estimates)}")
        print(f"{'='*60}")

        for patient in session.patients:
            if patient.status not in ("booked", "checked_in", "recalled"):
                continue
            info = estimates.get(patient.patient_id, {})
            wait = info.get("est_wait_min", 0)

            result = self._notify_patient(patient, reason, wait)
            results.append(result)

        return results

    def handle_reply(self, patient_id: str, reply_text: str) -> str:
        """
        Interpret a free-text reply from a patient and dispatch the
        appropriate booking action.  Falls back to keyword matching.
        """
        reply_upper = reply_text.upper().strip()

        # Fast keyword matching (backup)
        if "RESCHEDULE" in reply_upper:
            slot = self.booking.reschedule(patient_id, "tomorrow")
            return f"Rescheduled to {slot}"
        if "OTHER_DOCTOR" in reply_upper or "OTHER DOCTOR" in reply_upper:
            dr = self.booking.assign_doctor(patient_id, "General Medicine")
            return f"Assigned to {dr}"
        if "WAIT" in reply_upper or "OK" in reply_upper:
            return "Patient chose to wait — no action taken."
        if "REFUND" in reply_upper:
            return "Refund request logged (simulated)."

        # LLM interpretation if available
        if self._llm_available:
            return self._llm_interpret_reply(patient_id, reply_text)

        return "Reply received. Please call the clinic for further assistance."

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _notify_patient(self, patient: Patient, reason: str, wait_min: int) -> Dict:
        """Craft and send a single notification (LLM first, template fallback)."""
        session = self.engine.session
        message = None
        mode = "llm"

        if self._llm_available:
            try:
                message = self._llm_draft(patient, reason, wait_min)
            except Exception as exc:
                print(f"  ⚠️  LLM draft failed for {patient.patient_id}: {exc} — using template")
                message = None

        if not message:
            mode = "template"
            message = self._template_message(patient, reason, wait_min)

        # Simulate phone number
        phone = f"+94-77-{hash(patient.patient_id) % 9_000_000 + 1_000_000}"

        success = self.channel.send(patient.patient_id, phone, message)
        return {
            "patient_id": patient.patient_id,
            "name": patient.name,
            "language": patient.language,
            "mode": mode,
            "message": message,
            "sent": success,
        }

    def _llm_draft(self, patient: Patient, reason: str, wait_min: int) -> Optional[str]:
        """
        Ask Claude to draft a personalised message and send it via tool call.
        Returns the sent message, or None on failure.
        """
        session = self.engine.session
        phone = f"+94-77-{hash(patient.patient_id) % 9_000_000 + 1_000_000}"
        lang_name = {"en": "English", "si": "Sinhala", "ta": "Tamil"}.get(
            patient.language, "English"
        )

        system_prompt = (
            "You are a hospital notification assistant. "
            "Draft a SHORT, polite, culturally appropriate SMS message for an OPD patient. "
            "The message must:\n"
            "  - Be in the patient's preferred language.\n"
            "  - State the doctor's name, the reason (delay or cancellation) and the estimated wait.\n"
            "  - Offer options: WAIT / RESCHEDULE / OTHER_DOCTOR (or REFUND if cancelled).\n"
            "  - Stay under 160 characters for SMS.\n"
            "  - NOT include any medical diagnosis or personal health detail.\n"
            "Call the send_sms tool to dispatch the message. Do not add explanatory prose."
        )

        user_msg = (
            f"Patient: {patient.name} (ID: {patient.patient_id})\n"
            f"Language: {lang_name}\n"
            f"Urgent: {patient.is_urgent}\n"
            f"Doctor: {session.doctor_name}\n"
            f"Reason: {reason}\n"
            f"Doctor delay: {session.delay_minutes} min\n"
            f"Patient's estimated wait: {wait_min} min\n"
            f"Patient phone (simulated): {phone}\n"
        )

        response = self._client.messages.create(
            model=self.model,
            max_tokens=512,
            system=system_prompt,
            tools=TOOLS,
            messages=[{"role": "user", "content": user_msg}],
        )

        # Process tool calls
        sent_message = None
        for block in response.content:
            if block.type == "tool_use" and block.name == "send_sms":
                inp = block.input
                self.channel.send(inp["patient_id"], inp.get("phone", phone), inp["message"])
                sent_message = inp["message"]
                break
            elif block.type == "tool_use" and block.name == "reschedule_appointment":
                self.booking.reschedule(block.input["patient_id"],
                                        block.input.get("preferred_day", "tomorrow"))
            elif block.type == "tool_use" and block.name == "assign_another_doctor":
                self.booking.assign_doctor(block.input["patient_id"],
                                           block.input.get("specialty", "General Medicine"))

        # If LLM returned text instead of tool call, extract and send
        if sent_message is None:
            for block in response.content:
                if hasattr(block, "text") and block.text.strip():
                    text = block.text.strip()[:160]
                    self.channel.send(patient.patient_id, phone, text)
                    sent_message = text
                    break

        return sent_message

    def _llm_interpret_reply(self, patient_id: str, reply_text: str) -> str:
        """Use LLM to interpret an ambiguous patient reply."""
        session = self.engine.session
        prompt = (
            f"Patient {patient_id} replied: '{reply_text}'\n"
            "Available actions: WAIT, RESCHEDULE, OTHER_DOCTOR, REFUND.\n"
            "Determine the patient's intent and call the appropriate tool. "
            "If intent is unclear, reply explaining the options in English."
        )
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=256,
                tools=TOOLS,
                messages=[{"role": "user", "content": prompt}],
            )
            for block in response.content:
                if block.type == "tool_use":
                    if block.name == "reschedule_appointment":
                        slot = self.booking.reschedule(patient_id,
                                                       block.input.get("preferred_day", "tomorrow"))
                        return f"Rescheduled to {slot}"
                    if block.name == "assign_another_doctor":
                        dr = self.booking.assign_doctor(patient_id,
                                                        block.input.get("specialty", "General Medicine"))
                        return f"Assigned to {dr}"
            # Text reply
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text.strip()
        except Exception as exc:
            return f"LLM interpretation failed ({exc}); please call the clinic."
        return "Reply received — unclear intent. Please call the clinic."

    def _template_message(self, patient: Patient, reason: str, wait_min: int) -> str:
        """Build message from pre-approved templates (fallback)."""
        session = self.engine.session
        lang = patient.language if patient.language in ("en", "si", "ta") else "en"
        if reason in ("delayed", "delay"):
            key = "delayed"
        elif reason == "doctor_arrived":
            key = "doctor_arrived"
        else:
            key = "cancelled"
        template = TEMPLATES[key][lang]
        return template.format(
            name=patient.name,
            doctor=session.doctor_name,
            delay=session.delay_minutes,
            wait=wait_min,
        )


# ---------------------------------------------------------------------------
# Reply interpretation demo (standalone)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from queue_engine import make_demo_session, init_db, QueueEngine

    session = make_demo_session()
    engine = QueueEngine(session)

    # Check everyone in
    for p in session.patients[:4]:
        engine.check_in(p.patient_id)

    # Doctor delayed 20 min
    engine.set_doctor_delayed(20)

    db = init_db()
    channel = SimulatedChannel(db, session.session_id)
    booking = SimulatedBookingBackend()
    agent = NotificationAgent(engine, channel, booking, db)

    results = agent.notify_all_waiting("delayed")
    print(f"\n✅ Sent {len(results)} notifications.")

    # Simulate a reply
    print("\n--- Simulated reply from P002 ---")
    action = agent.handle_reply("P002", "I want to reschedule please")
    print(f"  Action taken: {action}")
