"""Smoke test for Laya. Run: .venv/bin/python smoke_test.py [english|multilingual]

Loads one checkpoint, asks three typed questions (choice, score, noul/boolean)
in English and Hindi, and prints answers plus measured latency.
"""
import sys
import time

from laya import Router

MODEL = sys.argv[1] if len(sys.argv) > 1 else "multilingual"

QUESTIONS = {
    "department": {
        "type": "choice",
        "instructions": "Which department should handle this?",
        "criteria": {
            "billing": "invoices, payments, refunds",
            "technical": "bugs, outages, system errors",
            "other": "everything else",
        },
    },
    "urgency": {
        "type": "score",
        "instructions": "How urgent is this?",
        "criteria": ["not urgent", "soon", "blocking"],
    },
    "churn_risk": {
        "type": "noul",
        "instructions": "Does the user threaten to cancel or leave?",
    },
}

STATES = [
    "Hi, we were billed twice for March. Please refund the duplicate today or we will cancel our plan.",
    "मुझसे मार्च में दो बार शुल्क लिया गया, कृपया डुप्लिकेट राशि वापस करें।",
    "La aplicación se cierra cada vez que abro la configuración.",
]

router = Router(default=MODEL)
router.preload([MODEL])

# Warm-up pass so the timed runs reflect steady-state latency.
router.predict(STATES[0], QUESTIONS, model=MODEL)

for state in STATES:
    t0 = time.perf_counter()
    r = router.predict(state, QUESTIONS, model=MODEL)
    ms = (time.perf_counter() - t0) * 1000
    a = r["answers"]
    print(f"[{ms:6.1f} ms] {state[:60]!r}")
    print(f"           department={a['department']['choice']}  "
          f"urgency={a['urgency'].get('score')}  "
          f"churn_risk={a['churn_risk']['noul']:.2f}  "
          f"model={r['routing']['model']}")
