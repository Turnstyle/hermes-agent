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
pending_prompt = None
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    record(method=method)
    result = {}
    if method == "session/new":
        if scenario == "setup-exit":
            sys.exit(1)  # before delivery: retry/fallback is safe
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
        if scenario.startswith("chunk-"):
            pending_prompt = msg["id"]
            print(json.dumps({"method": "session/update", "params": {"sessionId": "local", "update": {
                "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "partial answer "}}}}), flush=True)
            if scenario == "chunk-exit":
                sys.exit(1)
            if scenario == "chunk-rpc-error":
                print(json.dumps({"id": msg["id"], "error": {"code": -32000}}), flush=True)
            continue
        if scenario == "perm-closestdin":
            os.close(0)
            print(json.dumps({"id": 900, "method": "session/request_permission", "params": {
                "sessionId": "local", "toolCall": {"toolCallId": "native-1"}, "options": []}}), flush=True)
            while True:
                time.sleep(1)
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
        # Real Cursor answers the pending prompt with stopReason=cancelled (ACP spec) and may keep streaming
        # text until it does. Other scenarios stay silent: cancellation must still be bounded.
        if scenario in ("chunk-ackcancel", "chunk-latetext", "chunk-lateend") and pending_prompt is not None:
            late = ["rest of answer "] if scenario == "chunk-lateend" else (
                [f"late-text-{n} " for n in range(3)] if scenario == "chunk-latetext" else [])
            for text in late:
                print(json.dumps({"method": "session/update", "params": {"sessionId": "local", "update": {
                    "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}}), flush=True)
                time.sleep(.05)
            stop = "end_turn" if scenario == "chunk-lateend" else "cancelled"
            print(json.dumps({"id": pending_prompt, "result": {"stopReason": stop}}), flush=True)
            pending_prompt = None
        continue
    print(json.dumps({"id": msg["id"], "result": result}), flush=True)
if scenario in ("stall-setup", "success-ignore-eof", "timeout-ignore-eof"):
    while True:
        time.sleep(1)
