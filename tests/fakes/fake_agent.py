"""A stand-in agent CLI. Directives in the prompt pick its behavior:

    exit:N     exit with code N            sleep:S   sleep S seconds
    write:F    write file F in the cwd     quota     fail like a provider outage
    json       reply with a JSON object

Prompt comes from stdin, or the argument after ``--prompt``. With
``stream-json`` in argv it speaks Claude Code's stream format (honoring
``--resume``). Each call appends a line to $FAKE_CALLS when set.
"""
import json
import os
import sys
import time

argv = sys.argv[1:]
prompt = argv[argv.index("--prompt") + 1] if "--prompt" in argv else sys.stdin.read()
if os.environ.get("FAKE_CALLS"):
    with open(os.environ["FAKE_CALLS"], "a", encoding="utf-8") as handle:
        handle.write(prompt.strip().splitlines()[-1] + "\n")

for word in prompt.split():
    if word.startswith("sleep:"):
        time.sleep(float(word[6:]))
    if word.startswith("write:"):
        with open(word[6:], "w", encoding="utf-8") as handle:
            handle.write("written\n")
    if word.startswith("exit:"):
        print("boom", file=sys.stderr)
        sys.exit(int(word[5:]))
if "quota" in prompt.split():
    print("Error: rate limit exceeded", file=sys.stderr)
    sys.exit(1)

last = prompt.strip().splitlines()[-1]
reply = json.dumps({"echo": last, "cwd": os.getcwd()}) if "json" in prompt.split() else f"echo: {last}"

if "stream-json" in argv:
    session = argv[argv.index("--resume") + 1] if "--resume" in argv else "sess-1"
    if "--resume" in argv:
        reply += f" (resumed {session})"
    for event in (
        {"type": "system", "subtype": "init", "session_id": session},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__tiny__echo", "input": {"message": "hi"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "echo: hi"}]}},
        {"type": "result", "subtype": "success", "result": reply, "session_id": session},
    ):
        print(json.dumps(event))
else:
    print(reply)
