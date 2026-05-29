import json
import os
import uuid
import hashlib
from datetime import datetime
from typing import Any
import httpx
from schemas import MemoryItem, Artifact
from config import GATEWAY_CHAT_URL

class ArtifactStore:
    def __init__(self, base_dir="state/artifacts"):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)

    def put(self, blob: bytes, *, content_type: str, source: str, descriptor: str) -> str:
        prefix = hashlib.sha256(blob).hexdigest()[:16]
        art_id = f"art:{prefix}"
        
        bin_path = os.path.join(self.base_dir, f"{art_id}.bin")
        json_path = os.path.join(self.base_dir, f"{art_id}.json")
        
        if not os.path.exists(bin_path):
            with open(bin_path, "wb") as f:
                f.write(blob)
            
            meta = Artifact(
                id=art_id,
                content_type=content_type,
                size_bytes=len(blob),
                source=source,
                descriptor=descriptor
            )
            with open(json_path, "w") as f:
                f.write(meta.model_dump_json())
                
        return art_id

    def get_bytes(self, artifact_id: str) -> bytes:
        bin_path = os.path.join(self.base_dir, f"{artifact_id}.bin")
        with open(bin_path, "rb") as f:
            return f.read()

    def get_meta(self, artifact_id: str) -> Artifact:
        json_path = os.path.join(self.base_dir, f"{artifact_id}.json")
        with open(json_path, "r") as f:
            return Artifact.model_validate_json(f.read())

    def exists(self, artifact_id: str) -> bool:
        bin_path = os.path.join(self.base_dir, f"{artifact_id}.bin")
        return os.path.exists(bin_path)

artifacts = ArtifactStore()

MEMORY_FILE = "state/memory.json"

def _load_memory() -> list[MemoryItem]:
    if not os.path.exists(MEMORY_FILE):
        return []
    with open(MEMORY_FILE, "r") as f:
        data = json.load(f)
        return [MemoryItem.model_validate(i) for i in data]

def _save_memory(items: list[MemoryItem]):
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    with open(MEMORY_FILE, "w") as f:
        json.dump([i.model_dump() for i in items], f, default=str, indent=2)

def read(query: str, history: list[dict], kinds: list[str] = None, top_k: int = 8) -> list[MemoryItem]:
    items = _load_memory()
    if kinds:
        items = [i for i in items if i.kind in kinds]
    
    stop_words = {"and", "to", "the", "a", "of", "in", "is", "for", "with", "about", "me", "tell", "fetch", "his", "her", "it", "from", "on", "at", "an", "by", "this", "that", "what", "where", "when", "how", "who"}
    query_tokens = set(w.strip(".,?'\"") for w in query.lower().split()) - stop_words
    
    scored = []
    for item in items:
        item_tokens = set()
        if getattr(item, "keywords", None):
            for k in item.keywords:
                item_tokens.update(w.strip(".,?'\"") for w in k.lower().split())
        else:
            item_tokens = set(w.strip(".,?'\"") for w in item.descriptor.lower().split())
            
        item_tokens -= stop_words
        overlap = len(query_tokens.intersection(item_tokens))
        
        if overlap > 0:
            scored.append((overlap, item))
            
    scored.sort(key=lambda x: x[0], reverse=True)
    relevant = [x[1] for x in scored]
    return relevant[:top_k]

def filter(kinds: list[str] = None, goal_id: str = None, recent: int = None) -> list[MemoryItem]:
    items = _load_memory()
    if kinds:
        items = [i for i in items if i.kind in kinds]
    if goal_id:
        items = [i for i in items if i.goal_id == goal_id]
    
    items.sort(key=lambda x: x.created_at, reverse=True)
    if recent is not None:
        return items[:recent]
    return items

def remember(raw_text: str, source: str, run_id: str, goal_id: str = None) -> MemoryItem | None:
    payload = {
        "messages": [
            {"role": "system", "content": "Extract fact, preference, tool_outcome, or scratchpad from the given text. Return JSON with kind, keywords (list of strings), descriptor (short string), and value (structured dict)."},
            {"role": "user", "content": raw_text}
        ],
        "auto_route": "memory",
        "provider": "g",
        "response_format": {
            "type": "json_schema",
            "name": "memory_item",
            "schema": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["fact", "preference", "tool_outcome", "scratchpad"]},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "descriptor": {"type": "string"},
                    "value": {"type": "object"}
                },
                "required": ["kind", "keywords", "descriptor", "value"],
                "additionalProperties": False
            },
            "strict": True
        }
    }
    
    try:
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
            parsed = json.loads(content)
            
    except Exception as e:
        print(f"Failed to get/parse response from gateway: {e}")
        return None
    
    item = MemoryItem(
        id=uuid.uuid4().hex[:8],
        kind=parsed["kind"],
        keywords=parsed["keywords"],
        descriptor=parsed["descriptor"],
        value=parsed["value"],
        artifact_id=None,
        source=source,
        run_id=run_id,
        goal_id=goal_id,
        confidence=1.0,
        created_at=datetime.utcnow()
    )
    
    items = _load_memory()
    items.append(item)
    _save_memory(items)
    
    return item

def record_outcome(tool_call, result_text: str, artifact_id: str | None, run_id: str, goal_id: str):
    text_to_analyze = f"Tool {tool_call.name} returned: {result_text[:1000]}"
    item = remember(text_to_analyze, source=f"tool_{tool_call.name}", run_id=run_id, goal_id=goal_id)
    if item and artifact_id:
        item.artifact_id = artifact_id
        items = _load_memory()
        for i in items:
            if i.id == item.id:
                i.artifact_id = artifact_id
        _save_memory(items)
