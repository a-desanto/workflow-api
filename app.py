"""
Workflow-API — Business Workflow Authority
==========================================

Owns:
- Intent meaning
- execution_mode classification
- multica_priority derivation
- Routing targets (assignee_id, assignee_type)

Writes:
- intent table (sole writer)

Does NOT:
- Call agents directly
- Expose business state to callers beyond intent_id

Callers:
- api-gateway (proxies external requests here)
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
import asyncpg
import os
import json

app = FastAPI(title="Workflow-API")

DATABASE_URL: str | None = os.getenv("DATABASE_URL")

# ── Startup validation ────────────────────────────────────────────────────────

@app.on_event("startup")
async def validate_config() -> None:
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL environment variable is required but not set."
        )


# ── Canonical execution_mode → multica priority mapping ──────────────────────
# This mapping lives HERE and nowhere else.
_MODE_TO_PRIORITY: dict[str, str] = {
    "foreground": "high",
    "background": "low",
    "critical":   "urgent",
}

# Legacy priority → execution_mode shims
_LEGACY_PRIORITY_TO_MODE: dict[str, str] = {
    "high":   "foreground",
    "low":    "background",
    "urgent": "critical",
}

# Default routing target (Operator agent)
_DEFAULT_ASSIGNEE_ID: str = os.environ.get(
    "OPERATOR_AGENT_ID",
    "d1ad91da-75e2-4137-9ad0-3379c92b1c7d",
)
_DEFAULT_ASSIGNEE_TYPE: str = "agent"


# ── Request schema ────────────────────────────────────────────────────────────

class IntentRequest(BaseModel):
    # Canonical fields
    title: Optional[str] = Field(None, description="Human-readable task title")
    execution_mode: Optional[str] = Field(
        None, description="foreground | background | critical"
    )
    assignee_id: Optional[str] = Field(
        None, description="Multica agent UUID (defaults to Operator)"
    )
    assignee_type: str = Field("agent", description="agent | member")
    context: Optional[dict] = Field(None, description="Caller-defined context payload")

    # Backward-compat fields
    task:     Optional[str] = Field(None, exclude=True)
    goal:     Optional[str] = Field(None, exclude=True)
    priority: Optional[str] = Field(None, exclude=True)

    def resolved_title(self) -> str:
        return self.title or self.task or self.goal or ""

    def resolved_execution_mode(self) -> str:
        if self.execution_mode in _MODE_TO_PRIORITY:
            return self.execution_mode
        if self.priority in _LEGACY_PRIORITY_TO_MODE:
            return _LEGACY_PRIORITY_TO_MODE[self.priority]
        if self.priority in _MODE_TO_PRIORITY:
            return self.priority
        return "foreground"


# ── Endpoint ──────────────────────────────────────────────────────────────────

@app.post("/intents", status_code=202)
async def create_intent(req: IntentRequest):
    title = req.resolved_title()
    execution_mode = req.resolved_execution_mode()
    multica_priority = _MODE_TO_PRIORITY.get(execution_mode, "medium")

    if not title:
        raise HTTPException(status_code=422, detail="title is required")

    assignee_id = req.assignee_id or _DEFAULT_ASSIGNEE_ID
    assignee_type = req.assignee_type

    payload = req.model_dump(exclude={"task", "goal", "priority"})
    payload["execution_mode"] = execution_mode

    conn = await asyncpg.connect(DATABASE_URL, timeout=5)
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO intent
              (type, priority, payload, status,
               title, multica_priority, assignee_id, assignee_type, execution_mode)
            VALUES
              ($1, $2, $3::jsonb, 'pending', $4, $5, $6::uuid, $7, $8)
            RETURNING id
            """,
            "PLAN",
            execution_mode,
            json.dumps(payload),
            title,
            multica_priority,
            assignee_id,
            assignee_type,
            execution_mode,
        )
    finally:
        await conn.close()

    return {"status": "accepted", "intent_id": str(row["id"])}


@app.get("/health")
async def health():
    return {"status": "ok"}
