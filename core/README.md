# Core module

`core` contains the upstream Tabbit protocol clients and shared compatibility
logic used by the FastAPI routes.

## Why it exists

The public OpenAI and Claude routes should not contain Tabbit authentication,
session, signing, streaming, or tool-event implementation details. Those
details live here so routes can translate protocols without duplicating the
upstream transport.

## Main responsibilities

- `tabbit_client.py`: authenticated HTTP session, chat completion, quota, and
  session APIs.
- `tabbit_agent.py`: official Task-mode `/chat/send` signing and Agent v2
  WebSocket transport.
- `responses_bridge.py`: pending MCP invocation store and Responses
  `function_call_output` / Chat `tool_call_id` continuation state machine.
- `token_manager.py`: token rotation, health, cooldown, and refreshed-cookie
  persistence.
- `claude_compat.py`, `tool_policy.py`, `tool_events.py`: downstream protocol
  translation and dual-tool-plane policy.
- `model_registry.py`: authenticated dynamic model discovery.

## Dependencies

Upstream dependencies are declared in the project `requirements.txt`, notably
`httpx` for HTTP and `websockets` for Agent transport. Downstream consumers are
the modules under `routes/` and diagnostic scripts under `scripts/`.

## Quick example

```python
from core.tabbit_agent import AgentTaskRequest, TabbitAgentClient

agent = TabbitAgentClient(authenticated_tabbit_client)
bootstrap = await agent.bootstrap_task(
    AgentTaskRequest(
        session_id="session-id",
        content="Complete this task",
        model="Default",
    )
)

async for event in agent.run_task(bootstrap):
    print(event.type)
```

Protocol clients never log credentials. Callers must apply the same rule when
handling raw upstream events.
