"""Hostile local peer with inherited pipes; no external executable or network."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

scenario, journal = sys.argv[1:]


def record(**row):
    with Path(journal).open("a") as out:
        out.write(json.dumps(row) + "\n")


record(pid=os.getpid(), scenario=scenario)
if scenario in ("child", "grandchild"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if scenario == "child":
        subprocess.Popen([sys.executable, __file__, "grandchild", journal])
    while True:
        time.sleep(1)

options = [{"id": name, "category": name, "currentValue": value,
            "options": [{"value": value}]} for name, value in (("model", "default[]"), ("mode", "ask"))]
if scenario == "stall-setup":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    record(method=method)
    result = {}
    if method == "session/new":
        result = {"sessionId": "local", "configOptions": options}
    elif method == "session/set_config_option":
        if scenario == "stall-setup":
            continue
        result = {"configOptions": options}
        if scenario == "stopped-reader" and msg["params"]["configId"] == "mode":
            print(json.dumps({"id": msg["id"], "result": result}), flush=True)
            # Prove the prompt writer has started before the test cancels/closes.
            sys.stdin.read(1)
            record(ready=True)
            while True:
                time.sleep(1)
    elif method == "session/prompt":
        if scenario in ("success-ignore-eof", "success"):
            print(json.dumps({"method": "session/update", "params": {"sessionId": "local", "update": {
                "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "pong"}}}}), flush=True)
            result = {"stopReason": "end_turn"}
            print(json.dumps({"id": msg["id"], "result": result}), flush=True)
            continue
        if scenario == "exit-mid":
            sys.exit(1)
        if scenario == "rpc-error":
            print(json.dumps({"id": msg["id"], "error": {"code": -32000}}), flush=True)
            continue
        if scenario in ("empty", "native-empty", "malformed-update"):
            if scenario != "empty":
                update = {"sessionUpdate": "tool_call", "toolCallId": "native-1"}
                if scenario == "malformed-update":
                    update = ["invalid"]
                print(json.dumps({"method": "session/update", "params": {
                    "sessionId": "local", "update": update}}), flush=True)
            print(json.dumps({"id": msg["id"], "result": {"stopReason": "end_turn"}}), flush=True)
            continue
        if scenario in ("tree-exit", "tree-stall"):
            subprocess.Popen([sys.executable, __file__, "child", journal])
        if scenario == "tree-exit":
            print(json.dumps({"method": "session/update", "params": {"sessionId": "local", "update": {
                "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "pong"}}}}), flush=True)
            # Exit before close; descendants still own stdout and the process group.
            print(json.dumps({"id": msg["id"], "result": {"stopReason": "end_turn"}}), flush=True)
            break
        continue
    elif method == "session/cancel":
        continue  # No acknowledgment: cancellation must still be bounded.
    print(json.dumps({"id": msg["id"], "result": result}), flush=True)
if scenario in ("stall-setup", "success-ignore-eof", "timeout-ignore-eof"):
    while True:
        time.sleep(1)
