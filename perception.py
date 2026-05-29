import json
import httpx
import uuid
from schemas import Observation, Goal, MemoryItem
from config import GATEWAY_CHAT_URL

def observe(
    query: str,
    hits: list[MemoryItem],
    history: list[dict],
    prior_goals: list[Goal],
    run_id: str,
) -> Observation:
    def _hit_text(h):
        answer = h.value.get("answer", "") if isinstance(h.value, dict) else str(h.value)
        kw = ", ".join(h.keywords) if h.keywords else "—"
        return (
            f"[{h.id}] ({h.kind}) {h.descriptor}\n"
            f"  keywords : {kw}\n"
            f"  answer   : {answer[:400]}\n"
            f"  artifact : {h.artifact_id or 'none'}"
        )
    hits_text = "\n".join([_hit_text(h) for h in hits]) if hits else "(no memory hits)"
    history_text = "\n".join([json.dumps(h) for h in history])
    prior_goals_text = "\n".join([f"[{i}] {g.text} (done: {g.done})" for i, g in enumerate(prior_goals)])
    
    system_prompt = """You are the Perception module of an agent.
Your contract:
1. Read the query asked by user and break it down into multiple small executable goals.
2. If the prior goal list is already present, then for each prior goal, examine the run history AND the MEMORY HITS.
   - If a memory hit's 'answer' field already satisfies a goal, mark that goal `done: true` and set attach_artifact_id to the hit's id.
   - If the run history contains a final answer for a goal, mark it `done: true`.
3. For the first unfinished goal, decide whether it needs raw bytes from a previously fetched artifact. If yes, set the goal's attach_artifact_id to one of the artifact handles in MEMORY HITS.
4. Preserve goal order. Do not reorder, do not insert, do not drop a goal.

Return JSON representing the Observation schema."""

    user_prompt = f"""Query: {query}
MEMORY HITS:
{hits_text}

HISTORY:
{history_text}

PRIOR GOALS:
{prior_goals_text}
"""

    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "auto_route": "perception",
        "provider": "g",
        "response_format": {
            "type": "json_schema",
            "name": "observation",
            "schema": {
                "type": "object",
                "properties": {
                    "goals": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "text": {"type": "string"},
                                "done": {"type": "boolean"},
                                "attach_artifact_id": {"type": "string"}
                            },
                            "required": ["id", "text", "done"]
                        }
                    }
                },
                "required": ["goals"]
            }
        }
    }

    resp = httpx.post(GATEWAY_CHAT_URL, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    
    parsed = data.get("parsed")
    if parsed is None:
        content = data.get("text", "")
        content = content.strip()
        if content.startswith("```json"):
            content = content[7:]
        elif content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
        
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            print(f"Failed to parse JSON from perception module: {content}")
            parsed = {"goals": []}
    
    for g in parsed.get("goals", []):
        if "id" not in g or not g["id"]:
            g["id"] = uuid.uuid4().hex[:8]
        if "attach_artifact_id" not in g:
            g["attach_artifact_id"] = None
    
    answered_goals = {h["goal_id"] for h in history if h.get("kind") == "answer"}
    if prior_goals:
        for i, new_goal in enumerate(parsed["goals"]):
            if i < len(prior_goals):
                new_goal["id"] = prior_goals[i].id
                new_goal["text"] = prior_goals[i].text
                if prior_goals[i].done or prior_goals[i].id in answered_goals:
                    new_goal["done"] = True
    else:
        for new_goal in parsed.get("goals", []):
            new_goal["done"] = False

    return Observation.model_validate(parsed)
