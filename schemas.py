from pydantic import BaseModel
from typing import Literal
from datetime import datetime

class MemoryItem(BaseModel):
    id: str
    kind: Literal["fact", "preference", "tool_outcome", "scratchpad"]
    keywords: list[str]
    descriptor: str            # one short human-readable line
    value: dict                # structured payload
    artifact_id: str | None    # handle into the artifact store
    source: str
    run_id: str
    goal_id: str | None
    confidence: float
    created_at: datetime

class Artifact(BaseModel):
    id: str                    # "art:<sha256-prefix>"
    content_type: str
    size_bytes: int
    source: str
    descriptor: str

class Goal(BaseModel):
    id: str
    text: str                  # short imperative description
    done: bool
    attach_artifact_id: str | None

class Observation(BaseModel):
    goals: list[Goal]
    
    @property
    def all_done(self) -> bool:
        return all(g.done for g in self.goals)
        
    def next_unfinished(self) -> Goal | None:
        for g in self.goals:
            if not g.done:
                return g
        return None

class ToolCall(BaseModel):
    name: str
    arguments: dict

class DecisionOutput(BaseModel):
    answer: str | None         # exactly one of these two is populated
    tool_call: ToolCall | None
    
    @property
    def is_answer(self) -> bool:
        return self.answer is not None
