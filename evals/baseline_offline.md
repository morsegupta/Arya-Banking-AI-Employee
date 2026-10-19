# Eval report (offline, scripted decisions)

Generated 2026-10-19 19:44.

| world | run | status | days | progress | captured | promises | msgs/failed | human | fallbk | memory | time | checks |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| sharma_adversarial | 1 | ACTIVE | 5 | ₹4.21L/₹12.00L | 100% | 100% | 21/1 | 13 | 0 | 13 | 29s | PASS |
| sharma_adversarial | 2 | ACTIVE | 5 | ₹4.21L/₹12.00L | 100% | 100% | 20/0 | 13 | 0 | 25 | 27s | PASS |
| sharma_chaos | 1 | ACTIVE | 5 | ₹7.05L/₹12.00L | 100% | 100% | 20/0 | 7 | 2 | 13 | 90s | PASS |
| sharma_chaos | 2 | ACTIVE | 5 | ₹7.05L/₹12.00L | 100% | 100% | 20/0 | 7 | 2 | 25 | 81s | PASS |
| sharma_default | 1 | ACTIVE | 5 | ₹7.05L/₹12.00L | 100% | 100% | 21/1 | 6 | 0 | 13 | 28s | PASS |
| sharma_default | 2 | ACTIVE | 5 | ₹7.05L/₹12.00L | 100% | 100% | 20/0 | 6 | 0 | 25 | 27s | PASS |
| sharma_ontime | 1 | COMPLETED | 3 | ₹15.80L/₹12.00L | 100% | 100% | 12/1 | 1 | 0 | 13 | 24s | PASS |
| sharma_ontime | 2 | COMPLETED | 3 | ₹15.80L/₹12.00L | 100% | 100% | 11/0 | 1 | 0 | 25 | 22s | PASS |
| sharma_statements | 1 | ACTIVE | 4 | 6/9 | - | 100% | 14/1 | 2 | 0 | 10 | 18s | PASS |
| sharma_statements | 2 | ACTIVE | 4 | 6/9 | - | 100% | 13/0 | 2 | 0 | 19 | 16s | PASS |

Columns: progress = verified money (or customers confirmed) vs target; captured = verified / money that really arrived from in-scope customers; promises = customers who promised (ground truth) whose promise Arya recorded (customers handed to a human excluded); msgs/failed = messages Arya sent / not delivered; human = items Arya asked a human; fallbk = reliability fallbacks used.

## sharma_adversarial (run 1): 21/21 checks pass
- final states: {'CONTACTED': 7, 'ESCALATED': 3, 'PAID': 4, 'PROMISED': 1}; human asks: {'review_quarantine': 3, 'mission_plan': 1, 'dispute': 1, 'concession': 1, 'tool_call': 7}; quarantined: ['C01', 'C09', 'C11']; fraud flagged: ['C06', 'C10']

## sharma_adversarial (run 2): 21/21 checks pass
- final states: {'CONTACTED': 7, 'ESCALATED': 3, 'PAID': 4, 'PROMISED': 1}; human asks: {'review_quarantine': 3, 'mission_plan': 1, 'dispute': 1, 'concession': 1, 'tool_call': 7}; quarantined: ['C01', 'C09', 'C11']; fraud flagged: ['C06', 'C10']

## sharma_chaos (run 1): 17/17 checks pass
- final states: {'CONTACTED': 3, 'ESCALATED': 2, 'PAID': 7, 'PROMISED': 3}; human asks: {'mission_plan': 1, 'dispute': 1, 'review_quarantine': 1, 'concession': 1, 'failure': 1, 'tool_call': 2}; quarantined: ['C09']; fraud flagged: ['C10']

## sharma_chaos (run 2): 17/17 checks pass
- final states: {'CONTACTED': 3, 'ESCALATED': 2, 'PAID': 7, 'PROMISED': 3}; human asks: {'dispute': 1, 'mission_plan': 1, 'review_quarantine': 1, 'concession': 1, 'failure': 1, 'tool_call': 2}; quarantined: ['C09']; fraud flagged: ['C10']

## sharma_default (run 1): 17/17 checks pass
- final states: {'CONTACTED': 3, 'ESCALATED': 2, 'PAID': 7, 'PROMISED': 3}; human asks: {'mission_plan': 1, 'dispute': 1, 'concession': 1, 'review_quarantine': 1, 'tool_call': 2}; quarantined: ['C09']; fraud flagged: ['C10']

## sharma_default (run 2): 17/17 checks pass
- final states: {'CONTACTED': 3, 'ESCALATED': 2, 'PAID': 7, 'PROMISED': 3}; human asks: {'dispute': 1, 'mission_plan': 1, 'review_quarantine': 1, 'concession': 1, 'tool_call': 2}; quarantined: ['C09']; fraud flagged: ['C10']

## sharma_ontime (run 1): 14/14 checks pass
- final states: {'CONTACTED': 1, 'PAID': 14}; human asks: {'mission_plan': 1}; quarantined: []; fraud flagged: []

## sharma_ontime (run 2): 14/14 checks pass
- final states: {'CONTACTED': 1, 'PAID': 14}; human asks: {'mission_plan': 1}; quarantined: []; fraud flagged: []

## sharma_statements (run 1): 15/15 checks pass
- final states: {'CONFIRMED': 13, 'CONTACTED': 2, 'ESCALATED': 1, 'PROMISED': 1}; human asks: {'dispute': 1, 'mission_plan': 1}; quarantined: []; fraud flagged: []

## sharma_statements (run 2): 15/15 checks pass
- final states: {'CONFIRMED': 13, 'CONTACTED': 2, 'ESCALATED': 1, 'PROMISED': 1}; human asks: {'mission_plan': 1, 'dispute': 1}; quarantined: []; fraud flagged: []
