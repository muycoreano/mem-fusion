---
name: remember
description: Store a specific piece of information into the persistent vector memory system
trigger: User says "remember this", "remember that", "/remember", or asks Claude to save something for future sessions
---

# /remember — Store to Persistent Memory

When this skill is triggered, store the specified content into the vector memory system
with `importance=5` (user-curated, highest priority) and confirm back to the user
what was stored and its ID.

## Protocol

1. Identify WHAT to remember:
   - If the user specified content explicitly, use that verbatim
   - If the user said "remember this" without specifying, summarize the current context
     into a clear, self-contained statement (1–3 sentences)

2. Classify the memory TYPE:
   - `decision`   — a choice was made (architecture, approach, tool selection)
   - `preference` — user expressed how they want things done
   - `fact`       — factual information about a project or system
   - `error`      — an error that was resolved and how
   - `code`       — a significant code pattern or implementation
   - `context`    — background context about a project or initiative

3. Call `store_memory` with:
   - `importance: 5`  (always for /remember)
   - Infer `project` from conversation context if not stated
   - Infer `tags` from the content topic

4. Confirm to the user:
   ```
   Remembered: [brief summary of what was stored]
   ID: [memory_id]
   Type: [type] | Project: [project] | Tags: [tags]
   ```

## Examples

User: "remember that we always use uv for Python environments on this machine"
→ store_memory(content="Always use uv for Python environment management on this machine — not pip or venv directly", type="preference", importance=5, tags=["python", "tooling"])

User: "remember why we went with Qdrant"
→ summarize from context, store as type="decision", importance=5

User: "/remember the API key format is Bearer <token> not Basic"
→ store_memory(content="API auth format: Bearer <token> (not Basic auth)", type="fact", importance=5)
