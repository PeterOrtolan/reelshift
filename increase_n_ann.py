#!/bin/bash
# Auto-increase N for anna bot by 2 each day, max 15
LOOP_STATE="/opt/reelshift-graph/deployments/anna/state/loop_state.json"
python3 -c "
import json
with open('$LOOP_STATE') as f:
    state = json.load(f)
current_n = state.get('N', 5)
if current_n < 15:
    new_n = min(current_n + 2, 15)
    state['N'] = new_n
    with open('$LOOP_STATE', 'w') as f:
        json.dump(state, f, indent=2)
    print(f'N increased: {current_n} -> {new_n}')
    if new_n >= 15:
        print('N reached 15 (max) — no more increases')
else:
    print('N already at 15 (max)')
"
