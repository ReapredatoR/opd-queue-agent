# OPD Queue Agent

> **IT6070 — Emerging Topics in Information Technology — Assignment II**  
> Student: **Malinga Rajapaksha — MS25909296**  
> Professional: **Mr. Nimal Fernando** *(declared pseudonym — real identity withheld)*  
> Role: Channelling (OPD) Appointment Coordinator, Private Hospital Outpatient Department, Healthcare, Sri Lanka

---

## What it does

This proof-of-concept addresses the problem that **OPD patients hold a booking number but receive no live queue information**. When a doctor is late or cancels, the coordinator must contact patients one-by-one, and no-shows create silent "ghost" slots that block everyone behind them.

### Feature 1 — Live Queue-Position and Wait-Time Engine (F1)
Rule-based engine that:
- Tracks patient and doctor status
- Skips no-shows after a configurable grace period (creates a recall slot)
- **Never auto-skips urgent patients**
- Recalculates every patient's position and estimated wait instantly on any state change

### Feature 2 — Delay & Cancellation Notification Agent (F2)
LLM-powered agent (Claude claude-haiku-4-5) that:
- Drafts personalised SMS messages per patient in their language (English / Sinhala / Tamil)
- Uses **tool calling** to send messages and dispatch booking actions
- Falls back to **pre-approved templates** if the LLM or channel fails
- Interprets free-text replies (WAIT / RESCHEDULE / OTHER_DOCTOR / REFUND)

---

## Requirements addressed

| ID | Requirement |
|----|------------|
| R1 | Update each patient's queue position and estimated wait within 1 minute of a no-show (after grace period) or doctor status change. |
| R2 | Notify all affected patients within 3 minutes of a doctor delay (>15 min) or cancellation, with actionable options. |

---

## Software Requirements

- **Python 3.10+**
- **anthropic** Python package (for LLM feature; template fallback works without it)
- No other external services required for the proof-of-concept

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/ReapredatoR/opd-queue-agent.git
cd opd-queue-agent

# 2. (Recommended) Create a virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt
```

---

## Configuration

Copy `.env.example` to `.env` and add your Anthropic API key:

```bash
cp .env.example .env
```

Edit `.env`:
```
ANTHROPIC_API_KEY=sk-ant-...
```

> **If you do not set an API key**, the agent will automatically run in **template-only mode** — all notifications are sent using pre-approved multilingual templates. This is the safe fallback and fully demonstrates the system.

---

## Running the Application

### 1. Interactive Web Dashboard (Recommended for Live Demo & Viva)
```bash
python server.py
# or
python main.py --web
```
Open **`http://localhost:8000`** in your browser. This provides:
- Live hospital coordinator queue board (Requirement 1, recalculation <1s)
- Doctor session controls (Delay 20m, Arrived, Cancel)
- Interactive smartphone SMS simulator (Requirement 2, trilingual Sinhala/Tamil/English)
- Inbound patient free-text reply & agentic tool-calling simulation
- 1-Click 8-step rubric automated demo sequence
- LLM vs Fallback Template mode toggle (Error/fallback Test 3)

### 2. Automated CLI Demo (For 4-minute screen capture)
```bash
python main.py --demo
```

### 3. Interactive CLI Menu
```bash
python main.py
```

### 4. Automated Test Suite (All 3 Tests)
```bash
python test_cases.py
```

### 5. Provide Anthropic API key directly
```bash
python main.py --demo --api-key sk-ant-YOUR_KEY_HERE
```

---

## Project Structure

```
opd-queue-agent/
├── server.py              # Interactive Web Dashboard (HTTP & JSON API)
├── main.py                # CLI entry point (interactive, demo & web launch)
├── queue_engine.py        # Feature 1 — Live queue engine (rule-based)
├── notification_agent.py  # Feature 2 — LLM notification agent (Claude Haiku 4.5)
├── test_cases.py          # Three automated test cases (Week 3/4)
├── requirements.txt       # Python dependencies
├── .env.example           # API key template
├── .gitignore             # Git ignore patterns
├── README.md              # Software requirements, guide & architecture
├── video.mp4              # Standalone demonstration video (3m 48s, 1080p)
├── video_poster.png       # Video player preview poster
├── subtitles.srt          # Synchronized subtitles (SRT format)
└── subtitles.vtt          # HTML5 WebVTT caption track
```

---

## API / Configuration Information

| Parameter | Source | Default |
|-----------|--------|---------|
| `ANTHROPIC_API_KEY` | `.env` or `--api-key` flag | *(not set → template mode)* |
| `GRACE_MINUTES` | `queue_engine.py` constant | `5` |
| `AVG_CONSULT_MINUTES` | `queue_engine.py` constant | `8` |
| LLM model | `notification_agent.py` | `claude-haiku-4-5` |

---

## Simulation Declarations

The following components are **simulated** and clearly declared:

| Component | Simulation | Production equivalent |
|-----------|-----------|----------------------|
| Patient database | Synthetic in-memory data (8 fictitious patients) | Hospital booking system DB |
| SMS/WhatsApp gateway | `SimulatedChannel` — logs to console + SQLite | Text.lk / Dialog SMS / WhatsApp Business API |
| Booking backend | `SimulatedBookingBackend` — logs actions | Hospital booking system API |
| Patient phone numbers | Deterministic hash of patient ID | Real patient contact data |

## Sample Login Details (if applicable)

**Not applicable:** This proof-of-concept application and demonstration dashboard (`http://localhost:8000/`) run in open local clinic coordinator mode with zero authentication barriers for seamless evaluation. 

In a production hospital deployment, user authentication would be handled via the hospital's central Active Directory / LDAP single sign-on (SSO) with role-based access control (Coordinator, Triage Nurse, Consultant Doctor).

---

## Known Limitations

1. **No real hospital data**: uses synthetic patients; real deployment requires read access to the booking system.
2. **Sinhala/Tamil LLM quality**: not verified by a native speaker. Mitigation: pre-approved templates are the default; LLM drafts are opt-in.
3. **No persistent patient state across restarts**: all state is in-memory (SQLite only logs events/notifications).
4. **No reply channel**: the system sends messages but cannot receive real SMS replies; reply interpretation is demonstrated via `handle_reply()`.
5. **Privacy**: system is designed to never include medical details in messages; LLM prompt explicitly forbids it.

---

## Estimated Cost (LKR, ~335 LKR/USD)

| Item | Setup (LKR) | Monthly (LKR) | Assumption |
|------|------------|--------------|-----------|
| Development / prototype | 0 | 0 | Student work; open-source stack |
| Build + booking integration (in-house) | 60,000 | 0 | 40 hrs × 1,500; read-only export |
| Staff training | 9,000 | 0 | 3 coordinators × 2 hrs × 1,500 |
| SMS sender ID setup | 5,000 | — | Provider setup fee |
| Hosting | 0 | 0–2,000 | Existing server or small VPS |
| LLM API (Claude Haiku 4.5) | 0 | ~900 | 1,500 msgs/month |
| SMS gateway (Text.lk) | 0 | ~3,800 | 1,500 × 3 units × LKR 0.84 |
| Maintenance (in-kind) | 0 | ~6,000 | 4 hrs/month × 1,500 |
| Contingency 10% | 7,400 | 0 | — |
| **TOTAL** | **~81,000** | **~10,700–12,700** | |

*Prototype itself runs on free/simulated resources.*

---

## AI Tool Use Declaration

This project used **Google Gemini / Anthropic Claude** for:
- Drafting initial code structure
- Reviewing edge cases (urgent patient handling, template fallback logic)
- Improving docstrings and README clarity

All AI-generated output was reviewed, tested, and modified. The code was verified to run correctly as shown in `test_cases.py`.

---

## Adoption Approach

1. **Pilot**: run alongside the coordinator for 2 weeks with template-only mode; coordinator reviews every outbound message.
2. **Approval condition**: hospital management sign-off and IT integration agreement with the booking system vendor.
3. **Who pays**: the hospital (recurring cost ~LKR 10,700/month is low relative to coordinator time saved and patient satisfaction gains).
4. **PDPA compliance**: messages contain no medical details; patient data stored locally; no third-party analytics.
