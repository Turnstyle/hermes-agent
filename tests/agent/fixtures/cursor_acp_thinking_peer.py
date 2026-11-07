"""Inert ACP peer returning inline thinking as answer text; pipes only."""
import json
import os
import sys
from pathlib import Path

scenario, journal = sys.argv[1:]

def record(**row):
    with Path(journal).open("a") as out:
        out.write(json.dumps(row) + "\n")

record(pid=os.getpid(), scenario=scenario)
options = [{"id": name, "category": name, "currentValue": value,
            "options": [{"value": value}]} for name, value in (("model", "default[]"), ("mode", "ask"))]
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    record(method=method)
    result = {}
    if method == "session/new":
        result = {"sessionId": "local", "configOptions": options}
    elif method == "session/set_config_option":
        result = {"configOptions": options}
    elif method == "session/prompt":
        print(json.dumps({"method": "session/update", "params": {"sessionId": "local", "update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "<think>working</think>"}}}}), flush=True)
        result = {"stopReason": "end_turn"}
    elif method == "session/cancel":
        continue
    print(json.dumps({"id": message["id"], "result": result}), flush=True)
