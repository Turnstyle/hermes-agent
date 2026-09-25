"""Offline stdio peer: no Cursor executable, credentials, or network required."""
import json
import sys
from pathlib import Path

scenario, journal = sys.argv[1:]
options = [
    {"id": "model", "category": "model", "currentValue": "default[]",
     "options": [{"value": "default[]"}, {"value": "composer-2.5[fast=true]"}]},
    {"id": "mode", "category": "mode", "currentValue": "agent",
     "options": [{"value": "agent"}, {"value": "ask"}]},
]
pending_prompt = None


def send(**data):
    print(json.dumps({"jsonrpc": "2.0", **data}), flush=True)


def update(session_update, **data):
    send(method="session/update", params={"sessionId": "fake-session", "update": {"sessionUpdate": session_update, **data}})


def finish():
    update("agent_message_chunk", content={"type": "text", "text": "pong"})
    send(id=pending_prompt, result={"stopReason": "end_turn"})


for line in sys.stdin:
    msg = json.loads(line)
    with Path(journal).open("a") as out:
        out.write(json.dumps(msg) + "\n")
    method, params = msg.get("method"), msg.get("params", {})
    if method == "initialize":
        send(id=msg["id"], result={"protocolVersion": 1, "agentCapabilities": {}})
    elif method == "session/new":
        send(id=msg["id"], result={"sessionId": "fake-session", "configOptions": options})
    elif method == "session/set_config_option":
        if scenario == "reject-mode" and params["configId"] == "mode":
            send(id=msg["id"], error={"code": -32602, "message": "mode unavailable"})
        elif scenario == "reject-model" and params["configId"] == "model":
            send(id=msg["id"], error={"code": -32602, "message": "model unavailable"})
        else:
            for option in options:
                if option["id"] == params["configId"] and scenario != "ignore-mode":
                    option["currentValue"] = params["value"]
            send(id=msg["id"], result={"configOptions": options})
    elif method == "session/prompt":
        pending_prompt = msg["id"]
        if scenario == "permission":
            for request_id, request_method in enumerate([
                "session/request_permission", "cursor/ask_question", "cursor/execute", "fs/write_text_file"
            ], 100):
                send(id=request_id, method=request_method, params={"sessionId": "fake-session"})
        elif scenario == "tools":
            update("tool_call", toolCallId="native-1", title="Web search", kind="search", status="in_progress")
            update("tool_call_update", toolCallId="native-1", status="completed")
            update("agent_message_chunk", content={"type": "text", "text": '<tool_call>{"function":{"name":"unsafe","arguments":"{}"}}</tool_call>'})
            send(id=pending_prompt, result={"stopReason": "end_turn"})
        elif scenario == "stream":
            update("agent_message_chunk", content={"type": "text", "text": "first"})
            # Intentionally never finish until cancellation: buffered streaming deadlocks.
        elif scenario == "usage":
            update("agent_message_chunk", content={"type": "text", "text": "pong"})
            send(id=pending_prompt, result={"stopReason": "end_turn", "usage": {"inputTokens": 7, "outputTokens": 3, "totalTokens": 10}})
        else:
            finish()
    elif method == "session/cancel":
        send(id=pending_prompt, result={"stopReason": "cancelled"})
    elif method is None and msg.get("id") == 103:
        finish()
