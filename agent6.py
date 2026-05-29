import uuid
import httpx
import asyncio
import os
import httpx
from contextlib import asynccontextmanager
from config import GATEWAY_STATUS_URL, GATEWAY_CHAT_URL

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import memory
import perception
import decision
import action
from memory import artifacts
from schemas import Goal

MAX_ITERATIONS = 10

def ensure_gateway():
    try:
        resp = httpx.get(GATEWAY_STATUS_URL, timeout=5)
    except Exception as e:
        print(f"Warning: Gateway check failed: {e}. Is it running at port 8101?")

from mcp.client.stdio import stdio_client
from mcp import StdioServerParameters

@asynccontextmanager
async def mcp_session():
    mcp_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "mcp"))
    server_path = os.path.join(mcp_dir, "mcp_server_6.py")
    if not os.path.exists(server_path):
        server_path = os.path.join(mcp_dir, "mcp_server.py")
        
    server_params = StdioServerParameters(
        command=r"C:\Users\padam\AppData\Local\Programs\Python\Python310\python.exe",
        args=[server_path],
        env=None
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session

async def load_tools(session: ClientSession) -> list:
    result = await session.list_tools()
    return result.tools

def mcp_tools_for_decision(mcp_tools: list) -> list[dict]:
    tools = []
    for t in mcp_tools:
        tools.append({
            "name": t.name,
            "description": t.description,
            "parameters": t.inputSchema
        })
    return tools

def gathered_context_from(history: list[dict]) -> str:
    """Collect text usable for final summarization from run history."""
    parts: list[str] = []
    for h in history:
        kind = h.get("kind")
        if kind == "answer" and h.get("answer_text"):
            parts.append(h["answer_text"])
        elif kind == "tool_small" and h.get("result_text"):
            tool = h.get("tool", "tool")
            parts.append(f"[{tool}] {h['result_text']}")
        elif kind == "action":
            chunk = h.get("result_descriptor", "")
            if h.get("artifact_id"):
                chunk = f"{chunk}\n(artifact: {h['artifact_id']})" if chunk else f"(artifact: {h['artifact_id']})"
            if chunk:
                tool = h.get("tool", "tool")
                parts.append(f"[{tool}] {chunk}")
    return "\n\n".join(parts)

def print_log(it, hits, obs, attached, out, action_result=None):
    if action_result is None:
        print(f"\n--- iter {it} ---")
        print(f"[memory.read]   {len(hits)} hits")
        print(f"[perception]    ", end="")
        for i, g in enumerate(obs.goals):
            status = "[done]" if g.done else "[open]"
            indent = " " * 16 if i > 0 else ""
            print(f"{indent}{status} {g.text}")
            if g.attach_artifact_id:
                print(f"                  attach={g.attach_artifact_id}")
        
        for art_id, b in attached:
            print(f"[attach]        {art_id} ({len(b)} bytes)")
            
        if out.is_answer:
            answer_preview = out.answer.replace('\n', ' ')[:100] + "..." if len(out.answer) > 100 else out.answer
            print(f"[decision]      ANSWER: {answer_preview}")
        else:
            args_str = str(out.tool_call.arguments)
            if len(args_str) > 100: args_str = args_str[:100] + "..."
            print(f"[decision]      TOOL_CALL: {out.tool_call.name}({args_str})")
    else:
        result_text, art_id = action_result
        if art_id:
            print(f"[action]        -> {result_text}")
        else:
            res_preview = result_text.replace('\n', ' ')[:100] + "..." if len(result_text) > 100 else result_text
            print(f"[action]        -> {res_preview}")


async def run(query: str) -> str:
    ensure_gateway()
    run_id = uuid.uuid4().hex[:8]
    history: list[dict] = []
    prior_goals: list[Goal] = []

    # Do not store initial query in memory (only final answers or large tool results)

    async with mcp_session() as session:
        mcp_tools = await load_tools(session)
        tools = mcp_tools_for_decision(mcp_tools)

        for it in range(1, MAX_ITERATIONS + 1):
            print(f"\n===== Loop Iteration {it} =====")
            
            # 1. Memory read
            hits = memory.read(query, history)
            print(f"[Step 1: Memory] Read {len(hits)} hits from memory store:")
            for h in hits:
                print(f"  - [{h.id}] {h.descriptor} (kind: {h.kind})")
                
            # 2. Perception
            obs = perception.observe(query, hits, history, prior_goals, run_id)
            prior_goals = obs.goals
            print(f"[Step 2: Perception] Analyzed goals. All satisfied: {obs.all_done}")
            for g in obs.goals:
                print(f"  - [{'done' if g.done else 'open'}] {g.text} (attach: {g.attach_artifact_id})")
            
            if obs.all_done:
                print(f"\n[done] all {len(obs.goals)} goals satisfied")
                break

            goal = obs.next_unfinished()
            attached = []
            if goal.attach_artifact_id and artifacts.exists(goal.attach_artifact_id):
                attached.append((
                    goal.attach_artifact_id,
                    artifacts.get_bytes(goal.attach_artifact_id),
                ))
                print(f"[Step 2.5: Attach] Attached artifact {goal.attach_artifact_id} ({len(attached[-1][1])} bytes)")

            # 3. Decision
            out = decision.next_step(goal, hits, attached, history, tools)
            print(f"[Step 3: Decision] Selected next action for goal '{goal.text}':")
            if out.is_answer:
                answer_preview = out.answer.replace('\n', ' ')[:300] + "..." if len(out.answer) > 300 else out.answer
                print(f"  - Output: ANSWER")
                print(f"  - Content: {answer_preview}")
            else:
                print(f"  - Output: TOOL_CALL - {out.tool_call.name}")
                print(f"  - Arguments: {out.tool_call.arguments}")

            if out.is_answer:
                print(f"[Step 5: Memory] Recording final answer to goal in memory.")
                history.append({"iter": it, "kind": "answer",
                                "goal_id": goal.id,
                                "answer_text": out.answer})
                continue

            # 4. Action
            result_text, art_id = await action.execute(session, out.tool_call)
            print(f"[Step 4: Action] Executed tool {out.tool_call.name} successfully:")
            res_preview = result_text.replace('\n', ' ')[:300] + "..." if len(result_text) > 300 else result_text
            print(f"  - Result: {res_preview}")
            if art_id:
                print(f"  - Saved to artifact store: {art_id}")
            
            # Determine size of result_text
            result_size = len(result_text.encode('utf-8'))
            if art_id or result_size > 4096:
                # Large outcome: record in memory and artifact already handled
                if art_id:
                    print(f"[Step 5: Memory] Recorded large tool outcome in memory store.")
                else:
                    print(f"[Step 5: Memory] Large result without artifact, recording directly.")
                memory.record_outcome(
                    tool_call=out.tool_call,
                    result_text=result_text,
                    artifact_id=art_id,
                    run_id=run_id,
                    goal_id=goal.id,
                )
                # Append to history with artifact reference if any
                history.append({"iter": it, "kind": "action",
                                "goal_id": goal.id, "tool": out.tool_call.name,
                                "arguments": out.tool_call.arguments,
                                "result_descriptor": result_text[:300],
                                "artifact_id": art_id})
            else:
                # Small outcome: do not store in memory, but pass to perception via history entry
                print(f"[Step 5: Memory] Small tool outcome (<4KB), passing to perception in next loop.")
                history.append({"iter": it, "kind": "tool_small",
                                "goal_id": goal.id, "tool": out.tool_call.name,
                                "arguments": out.tool_call.arguments,
                                "result_text": result_text})

    # Process data from explicit answers, tool results in history, or memory hits
    context = gathered_context_from(history)

    memory_answers = []
    for g in prior_goals:
        if g.done and g.attach_artifact_id:
            for h in hits:
                if h.id == g.attach_artifact_id:
                    ans = h.value.get("answer", "") if isinstance(h.value, dict) else str(h.value)
                    if ans:
                        memory_answers.append(f"Answer for '{g.text}': {ans}")
                    break

    if context.strip() or memory_answers:
        if memory_answers:
            context = (context + "\n" + "\n".join(memory_answers)).strip() if context.strip() else "\n".join(memory_answers)

        try:
            print("[INFO] All goals satisfied; generating final summary from recorded answers.")
            payload = {
                "messages": [
                    {"role": "system", "content": (
                        "You are a precise summarizer. Given the user's query and the gathered answers, "
                        "produce a concise, well-structured final answer that directly answers the query. "
                        "Do not include raw JSON, URLs, or meta-commentary — just the answer."
                    )},
                    {"role": "user", "content": f"Query: {query}\n\nGathered information:\n{context}"}
                ],
                "auto_route": "decision",
                "provider": "g",
                "response_format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                        "additionalProperties": False
                    }
                }
            }
            resp = httpx.post(GATEWAY_CHAT_URL, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            parsed = data.get("parsed") or {}
            final_ans = parsed.get("answer", context)
        except Exception as e:
            print(f"[WARN] Summarization failed: {e}")
            final_ans = context
    else:
        final_ans = "Data is not sufficient to summarize."
    # Determine classification (fact or preference) for the final answer
    def classify_answer(goal_text: str, answer_text: str) -> str:
        try:
            classify_payload = {
                "messages": [
                    {"role": "system", "content": "Classify the following answer as either a factual statement or a user preference. Respond with JSON containing a single field 'kind' with value 'fact' or 'preference'."},
                    {"role": "user", "content": f"Goal: {goal_text}\nAnswer: {answer_text}"}
                ],
                "auto_route": "perception",
                "provider": "g",
                "response_format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {"kind": {"type": "string", "enum": ["fact", "preference"]}},
                        "required": ["kind"],
                        "additionalProperties": False
                    }
                }
            }
            resp = httpx.post(GATEWAY_CHAT_URL, json=classify_payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            parsed = data.get("parsed") or {}
            return parsed.get("kind", "fact")
        except Exception as e:
            print(f"[WARN] Classification failed: {e}")
            return "fact"
    
    # ─── Final Summary ───────────────────────────────────────────────────────
    separator = "=" * 60
    print(f"\n{separator}")
    print("  FINAL SUMMARY")
    print(separator)
    print(final_ans)
    print(separator)

    # ── Helpers: extract keywords and goal-specific answer via LLM ─────────
    def extract_keywords(goal_text: str) -> list[str]:
        try:
            resp = httpx.post(GATEWAY_CHAT_URL, json={
                "messages": [
                    {"role": "system", "content": "Extract 3–6 short search keywords from the goal. Return JSON with field 'keywords' as a list of strings."},
                    {"role": "user", "content": goal_text}
                ],
                "auto_route": "perception",
                "provider": "g",
                "response_format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {"keywords": {"type": "array", "items": {"type": "string"}}},
                        "required": ["keywords"],
                        "additionalProperties": False
                    }
                }
            }, timeout=30)
            resp.raise_for_status()
            return resp.json().get("parsed", {}).get("keywords", [])
        except Exception as e:
            print(f"[WARN] Keyword extraction failed: {e}")
            # simple fallback: split on spaces and take meaningful words
            return [w.strip(".,?") for w in goal_text.split() if len(w) > 4][:6]

    def extract_goal_answer(goal_text: str, full_summary: str) -> str:
        try:
            resp = httpx.post(GATEWAY_CHAT_URL, json={
                "messages": [
                    {"role": "system", "content": (
                        "From the provided full answer, extract ONLY the part that directly answers "
                        "the given goal. Return JSON with field 'answer' containing that targeted excerpt."
                    )},
                    {"role": "user", "content": f"Goal: {goal_text}\n\nFull answer:\n{full_summary}"}
                ],
                "auto_route": "perception",
                "provider": "g",
                "response_format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                        "additionalProperties": False
                    }
                }
            }, timeout=30)
            resp.raise_for_status()
            return resp.json().get("parsed", {}).get("answer", full_summary)
        except Exception as e:
            print(f"[WARN] Goal-answer extraction failed: {e}")
            return full_summary

    # Store each completed goal with its own keywords, targeted answer, and classification
    import datetime as _dt
    from memory import _load_memory, _save_memory, MemoryItem
    for g in prior_goals:
        if g.done:
            keywords    = extract_keywords(g.text)
            goal_answer = extract_goal_answer(g.text, final_ans)
            kind        = classify_answer(g.text, goal_answer)
            print(f"[INFO] Goal '{g.text[:60]}'")
            print(f"       keywords : {keywords}")
            print(f"       kind     : {kind}")
            print(f"       answer   : {goal_answer[:120]}")
            mem_item = MemoryItem(
                id=uuid.uuid4().hex[:8],
                kind=kind,
                keywords=keywords,
                descriptor=g.text,
                value={"answer": goal_answer},
                artifact_id=None,
                source="final_answer",
                run_id=run_id,
                goal_id=g.id,
                confidence=1.0,
                created_at=_dt.datetime.now(_dt.timezone.utc)
            )
            mem_items = _load_memory()
            mem_items.append(mem_item)
            _save_memory(mem_items)
    return final_ans

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        asyncio.run(run(" ".join(sys.argv[1:])))
    else:
        print("Usage: python agent6.py <query>")
