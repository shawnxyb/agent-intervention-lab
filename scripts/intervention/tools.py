"""The explicit model-visible experiment capabilities."""

from copy import deepcopy


def tool(name, description, properties, required):
    return {
        "type": "function",
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


EXEC = tool(
    "container_exec",
    "Execute Bash in your own isolated Linux task container. "
    "Read/write code and run public tests here. No network or host access. "
    "Commands must be foreground. No command duration cap is imposed.",
    {
        "command": {"type": "string"},
    },
    ["command"],
)

REPORT = tool(
    "report_event",
    "Publish a stable snapshot with observed progress or a coordination issue. "
    "Wait for the receipt before continuing; messages=[] means no instructions. "
    "The controller supplies snapshot identity; use the provided event reporting rules.",
    {
        "issue_id": {"type": "string"},
        "kind": {"type": "string", "enum": ["status", "contract_change", "cross_task_blocker"]},
        "summary": {"type": "string"},
        "evidence": {"type": "string"},
        "attempts": {"type": "array", "items": {"type": "string"}},
        "resolved": {"type": "boolean"},
    },
    ["issue_id", "kind", "summary", "evidence", "attempts", "resolved"],
)

PLAN = tool(
    "plan_tasks",
    "Record the initial assignment before either Worker starts. Assign each complete "
    "feature to exactly one Worker and explain shared interfaces and responsibilities. "
    "Declare cross-worker interface contracts explicitly; [] means none. "
    "The controller checks structure and identifiers, not plan quality. "
    "Only available during initial planning; do not provide implementation code. "
    "Validation errors leave Workers stopped: correct and resubmit until recorded=true.",
    {
        "reason": {"type": "string"},
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "enum": ["worker_a", "worker_b"]},
                    "feature": {"type": "string"},
                    "instructions": {"type": "string"},
                },
                "required": ["target", "feature", "instructions"],
                "additionalProperties": False,
            },
        },
    },
    ["reason", "assignments", "shared_regions", "interface_contracts"],
)
PLAN["inputSchema"]["properties"]["shared_regions"] = {
    "type": "array",
    "description": "Declare shared edit regions, intended changes and verification. "
    "Each (path, symbol) has one owner; use [] only when no shared regions exist.",
    "items": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "symbol": {
                "type": "string",
                "description": "Identifier for the shared edit region within path.",
            },
            "owner": {"type": "string", "enum": ["worker_a", "worker_b"]},
            "action": {"type": "string"},
            "verification": {"type": "string"},
        },
        "required": ["path", "symbol", "owner", "action", "verification"],
        "additionalProperties": False,
    },
}
PLAN["inputSchema"]["properties"]["interface_contracts"] = {
    "type": "array",
    "description": "Cross-worker interfaces, with one modification owner per (path, symbol). "
    "Specify who implements, connects and verifies them. Use [] when independent; "
    "explain any inapplicable text field rather than leaving it empty.",
    "items": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "symbol": {"type": "string"},
            "owner": {"type": "string", "enum": ["worker_a", "worker_b"]},
            "consumers": {
                "type": "array",
                "items": {"type": "string", "enum": ["worker_a", "worker_b"]},
                "minItems": 1,
                "uniqueItems": True,
            },
            "inputs": {"type": "string"},
            "outputs": {"type": "string"},
            "state": {"type": "string", "description": "State location, ownership and lifecycle."},
            "sequence": {
                "type": "string",
                "description": "Call order and connection responsibility.",
            },
            "errors": {"type": "string", "description": "Exceptions and fallback behavior."},
            "verification": {
                "type": "string",
                "description": "Observable public behavior, verification method and responsible Worker.",
            },
        },
        "required": [
            "path",
            "symbol",
            "owner",
            "consumers",
            "inputs",
            "outputs",
            "state",
            "sequence",
            "errors",
            "verification",
        ],
        "additionalProperties": False,
    },
}

INSPECT = tool(
    "inspect_code",
    "Read/list/search tracked public code and tests without executing commands. "
    "view=base is immutable baseline code; integration requires a snapshot_pair from "
    "verify_integration and never changes a Worker's own checkout.",
    {
        "view": {"type": "string", "enum": ["base", "integration"]},
        "operation": {"type": "string", "enum": ["list", "read", "search"]},
        "path": {"type": "string"},
        "query": {"type": "string"},
        "snapshot_pair": {"type": "string"},
    },
    ["view", "operation", "path"],
)

VERIFY = tool(
    "verify_integration",
    "Publish your current contribution and strictly merge it with the peer's last "
    "published snapshot in an isolated integration workspace. Return conflict evidence "
    "or run specified public pytest paths or Go package/test-file paths. No patches are synchronized back to Workers. "
    "Results apply only to the returned snapshot_pair. nonempty_contributions lists "
    "Workers with nonempty patches, not completed features or test coverage. "
    "No Manager is called by Static.",
    {"test_paths": {"type": "array", "items": {"type": "string"}}},
    ["test_paths"],
)


def planning_tool(requirements: dict) -> dict:
    """Expose the exact feature IDs without assigning them to Workers."""
    definition = deepcopy(PLAN)
    assignments = definition["inputSchema"]["properties"]["assignments"]
    assignments.update(minItems=len(requirements), maxItems=len(requirements))
    assignments["items"]["properties"]["feature"]["enum"] = sorted(requirements)
    return definition


COORDINATE = tool(
    "coordinate",
    "Send coordination instructions through the controller. "
    "One call is one decision and may contain messages for both Workers. "
    "Do not write implementation code during supervision. "
    "An empty messages list explicitly records a no-action decision.",
    {
        "reason": {"type": "string"},
        "messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "enum": ["worker_a", "worker_b"]},
                    "text": {"type": "string"},
                },
                "required": ["target", "text"],
                "additionalProperties": False,
            },
        },
    },
    ["reason", "messages"],
)

ACK = tool(
    "acknowledge",
    "Acknowledge a received manager message by its message_id. "
    "Receipt alone does not demonstrate that your code follows the instruction.",
    {"message_id": {"type": "string"}, "understanding": {"type": "string"}},
    ["message_id", "understanding"],
)


# Role permissions are owned by the runtime, never supplied by Skill prose/config.
ROLE_TOOLS = {
    "single": ("container_exec", "report_event"),
    "worker_a": (
        "container_exec",
        "report_event",
        "acknowledge",
        "inspect_code",
        "verify_integration",
    ),
    "worker_b": (
        "container_exec",
        "report_event",
        "acknowledge",
        "inspect_code",
        "verify_integration",
    ),
    "manager": ("inspect_code", "plan_tasks", "coordinate"),
}
