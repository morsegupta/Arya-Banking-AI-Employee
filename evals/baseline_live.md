# Eval report (live models)

Generated 2026-10-19 20:06.

| world | run | status | days | progress | captured | promises | msgs/failed | human | fallbk | memory | time | checks |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| sharma_default | 1 | ACTIVE | 2 | ₹3.63L/₹12.00L | 100% | 100% | 15/1 | 4 | 0 | 12 | 610s | PASS |

Columns: progress = verified money (or customers confirmed) vs target; captured = verified / money that really arrived from in-scope customers; promises = customers who promised (ground truth) whose promise Arya recorded (customers handed to a human excluded); msgs/failed = messages Arya sent / not delivered; human = items Arya asked a human; fallbk = reliability fallbacks used.

## sharma_default (run 1): 17/17 checks pass
- final states: {'ACKNOWLEDGED': 1, 'CONTACTED': 3, 'ESCALATED': 2, 'PAID': 4, 'PROMISED': 5}; human asks: {'mission_plan': 1, 'dispute': 1, 'review_quarantine': 1, 'concession': 1}; quarantined: ['C09']; fraud flagged: ['C10']
