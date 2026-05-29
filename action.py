from mcp.client.session import ClientSession
from schemas import ToolCall
from memory import artifacts

ARTIFACT_THRESHOLD_BYTES = 4096

async def execute(
    session: ClientSession,
    tool_call: ToolCall,
) -> tuple[str, str | None]:
    
    # 1. Guard against artifact handles in arguments
    for k, v in tool_call.arguments.items():
        if isinstance(v, str) and v.startswith("art:"):
            return f"Error: The argument {k} contains an artifact handle '{v}'. Artifact handles are not file paths or URLs.", None

    # 2. Dispatch
    try:
        result = await session.call_tool(tool_call.name, arguments=tool_call.arguments)
        
        # Collapse content blocks
        text_blocks = []
        for block in result.content:
            if block.type == "text":
                text_blocks.append(block.text)
            else:
                text_blocks.append(str(block))
        
        full_text = "\n".join(text_blocks)
    except Exception as e:
        return f"Tool execution failed: {e}", None

    # 3. Threshold check
    blob = full_text.encode("utf-8")
    if len(blob) > ARTIFACT_THRESHOLD_BYTES:
        art_id = artifacts.put(
            blob,
            content_type="text/plain",
            source=f"tool:{tool_call.name}",
            descriptor=full_text[:50].replace("\n", " ") + "..."
        )
        preview = full_text[:100].replace("\n", " ")
        descriptor = f"[artifact {art_id}, {len(blob)} bytes] preview: {preview}..."
        return descriptor, art_id
    else:
        return full_text, None
