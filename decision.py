import json
import httpx
from schemas import DecisionOutput, Goal, MemoryItem, ToolCall
from config import GATEWAY_CHAT_URL

def next_step(
    goal: Goal,
    hits: list[MemoryItem],
    attached: list[tuple[str, bytes]],
    history: list[dict],
    mcp_tools: list[dict],
) -> DecisionOutput:
    def _hit_text(h):
        answer = h.value.get("answer", "") if isinstance(h.value, dict) else str(h.value)
        kw = ", ".join(h.keywords) if h.keywords else "—"
        return (
            f"[{h.id}] ({h.kind}) {h.descriptor}\n"
            f"  keywords : {kw}\n"
            f"  answer   : {answer[:400]}\n"
            f"  artifact : {h.artifact_id or 'none'}"
        )
    hits_text = "\n".join([_hit_text(h) for h in hits]) if hits else "(no relevant memory hits)"
    history_text = "\n".join([json.dumps(h) for h in history[-5:]])
    
    attached_text = ""
    if attached:
        attached_text = "ATTACHED ARTIFACTS:\n"
        for art_id, b in attached:
            attached_text += f"--- {art_id} ---\n{b.decode('utf-8', errors='replace')[:4000]}\n"

    system_prompt = """You are the Decision module. You must select the next action to satisfy the current goal.

Instructions:
1. First, check MEMORY HITS — if any hit's 'answer' field already fully answers the current goal, return that as a final ANSWER directly. Do NOT call a tool in that case.
2. If the required data is available in ATTACHED ARTIFACTS or RECENT HISTORY (e.g., recent tool results), use it to produce the final ANSWER without calling a tool.
3. Only call a tool if the needed information is not available in memory, artifacts, or history.
4. Respond with exactly one output: either a final answer or a tool call. Never both."""

    user_prompt = f"""CURRENT GOAL: {goal.text}

MEMORY HITS:
{hits_text}

RECENT HISTORY:
{history_text}

{attached_text}
"""

    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "auto_route": "decision",
        "provider": "g"
    }
    
    if mcp_tools:
        payload["tools"] = [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("parameters") or {}
            }
            for t in mcp_tools
        ]
        payload["tool_choice"] = "auto"

    resp = httpx.post(GATEWAY_CHAT_URL, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    
    if data.get("tool_calls"):
        tc = data["tool_calls"][0]
        args = tc.get("arguments", {})
        if isinstance(args, str):
            args = json.loads(args)
            
        return DecisionOutput(
            answer=None,
            tool_call=ToolCall(
                name=tc["name"],
                arguments=args
            )
        )
    else:
        return DecisionOutput(
            answer=data.get("text", ""),
            tool_call=None
        )
