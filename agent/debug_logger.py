"""Agent Debug Logger — passive sideband trace for every Agent run.

Logs are never visible to the Agent, never added to Memory, system prompt,
or conversation history.  Writing failures are swallowed so the Agent is
unaffected.
"""
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import traceback
import uuid
from contextlib import contextmanager
from pathlib import Path

_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs" / "agent_runs"

current_run: ContextVar = ContextVar("current_run", default=None)


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


class AgentRun:
    def __init__(self, run_id, session_id, trigger_type, start_time, automation_meta=None):
        self.run_id = run_id
        self.session_id = session_id
        self.trigger_type = trigger_type
        self.start_time = start_time
        self.end_time = None
        self.initial_context = {}
        self.steps = []
        self.final_response = None
        self.status = "running"
        self.error = None
        self.automation_meta = automation_meta or {}

    def record_llm_input(self, system, messages, tools, model=None):
        try:
            self.steps.append({
                "type": "llm_request",
                "timestamp": _now(),
                "model": model,
                "system": system,
                "messages": messages,
                "tools": tools,
            })
        except Exception:
            pass

    def record_llm_response(self, response):
        try:
            self.steps.append({
                "type": "llm_response",
                "timestamp": _now(),
                "response": response,
            })
        except Exception:
            pass

    def record_tool_start(self, tool_name, tool_call_id, arguments):
        try:
            self.steps.append({
                "type": "tool_call",
                "timestamp": _now(),
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "arguments": arguments,
            })
        except Exception:
            pass

    def record_tool_result(self, tool_call_id, result, error=None):
        try:
            self.steps.append({
                "type": "tool_result",
                "timestamp": _now(),
                "tool_call_id": tool_call_id,
                "result": result,
                "error": error,
            })
        except Exception:
            pass

    def finish(self, status="success", final_response=None, error=None):
        self.end_time = _now()
        self.status = status
        self.final_response = final_response
        self.error = error

    def save(self):
        try:
            _LOGS_DIR.mkdir(parents=True, exist_ok=True)
            ts = self.start_time[:19]
            safe_ts = ts.replace("T", "_").replace("-", "").replace(":", "")
            filename = safe_ts + "_" + self.run_id[:8] + ".json"
            data = {
                "run_id": self.run_id,
                "session_id": self.session_id,
                "trigger_type": self.trigger_type,
                "start_time": self.start_time,
                "end_time": self.end_time,
                "initial_context": self.initial_context,
                "automation_meta": self.automation_meta,
                "steps": self.steps,
                "final_response": self.final_response,
                "status": self.status,
                "error": self.error,
            }
            path = _LOGS_DIR / filename
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:
            pass


class DebugLogger:
    @contextmanager
    def run(self, session_id, trigger_type, automation_meta=None, initial_context=None):
        run_id = uuid.uuid4().hex
        start_time = _now()
        ar = AgentRun(run_id, session_id, trigger_type, start_time, automation_meta)
        if initial_context:
            ar.initial_context = initial_context
        token = current_run.set(ar)
        try:
            yield ar
        except BaseException as exc:
            ar.finish(status="error", error=type(exc).__name__ + ": " + str(exc))
            raise
        else:
            if ar.status == "running":
                ar.finish(status="success")
        finally:
            current_run.reset(token)
            ar.save()

    def get(self):
        return current_run.get(None)


agent_debug = DebugLogger()
