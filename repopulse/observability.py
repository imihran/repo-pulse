"""
Langfuse observability for the RepoPulse investigator agent.

Langfuse records every agent step as a trace with:
  - which tool was called and with what arguments
  - the tool's output
  - token counts and estimated cost per LLM call
  - total latency for the investigation

Each investigation run appears as one top-level trace in the Langfuse UI,
with child spans for every tool call and LLM generation.

If LANGFUSE_PUBLIC_KEY is not set, the handler is a no-op so local dev
works without needing a Langfuse account.
"""

import os
from dotenv import load_dotenv

load_dotenv()

_PUBLIC_KEY  = os.getenv("LANGFUSE_PUBLIC_KEY", "")
_SECRET_KEY  = os.getenv("LANGFUSE_SECRET_KEY", "")
_HOST        = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")

_langfuse_enabled = bool(_PUBLIC_KEY and _SECRET_KEY)


def get_langfuse_handler(trace_name: str, metadata: dict = None):
    """
    Return a LangChain/LangGraph callback handler that sends traces to Langfuse.

    Pass the returned handler in the `config` dict when invoking the graph:
        graph.invoke(state, config={"callbacks": [get_langfuse_handler(...)]})

    If Langfuse keys are not configured, returns an empty list so the call
    site doesn't need to check — LangGraph silently ignores an empty list.
    """
    if not _langfuse_enabled:
        return []

    # Langfuse v4: reads LANGFUSE_PUBLIC_KEY / SECRET_KEY / HOST from env automatically.
    # trace_name and metadata are set via update_current_trace after the run.
    from langfuse.langchain import CallbackHandler

    handler = CallbackHandler()
    return [handler]


def is_enabled() -> bool:
    return _langfuse_enabled
