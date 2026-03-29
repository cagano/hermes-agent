# Hermes Payload Optimization Plan

> **For implementer:** Use this plan top-to-bottom. Each task is self-contained. Run tests after every task. Commit after every task. Do NOT batch.

**Goal:** Reduce Hermes first-turn API payload from ~55k chars to ~32k chars by trimming tool schemas, collapsing the skills index, and adding conditional tool tiers.

**Architecture:**
- Tool schemas live in `tools/*.py` as dicts passed to `registry.register()`
- Skills index is built by `agent/prompt_builder.py::build_skills_system_prompt()`
- Tools are enabled/disabled via `check_fn` in the registry
- Prompt caching is already in `agent/prompt_caching.py` — no changes needed there

**Tech Stack:** Python 3.11, pytest, hermes-agent repo at `~/.hermes/hermes-agent/`

**Baseline (to beat):**
```
Tool schemas : 34,295 chars  (~8,574 tokens)
Skills index : 14,871 chars  (~3,718 tokens)
TOTAL payload: ~55,567 chars (~13,892 tokens)
```

**Target:** ≤ 32,000 chars total payload (~8,000 tokens)

---

## Task 1: Measure baseline (no code changes)

**Objective:** Capture exact before-numbers to diff against after each task.

**Files:**
- Create: `tools/payload_audit.py`

**Step 1: Create the audit script**

```python
# tools/payload_audit.py
"""Standalone payload audit — run with: python tools/payload_audit.py"""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def audit():
    import model_tools  # noqa — populates registry
    from tools.registry import registry
    from agent.prompt_builder import build_skills_system_prompt

    names = registry.get_all_tool_names()
    schemas = registry.get_definitions(set(names), quiet=True)
    schema_json = json.dumps(schemas, ensure_ascii=False)

    sp = build_skills_system_prompt()
    sp_lines = sp.count('\n')

    by_tool = sorted(
        [(s['function']['name'], len(json.dumps(s))) for s in schemas],
        key=lambda x: -x[1]
    )

    print(f"=== HERMES PAYLOAD AUDIT ===")
    print(f"Tool schemas  : {len(schema_json):>7,} chars  ({len(schemas)} tools)")
    print(f"Skills index  : {len(sp):>7,} chars  ({sp_lines} lines)")
    print(f"--- Top 15 tool schemas ---")
    for name, sz in by_tool[:15]:
        ts = registry.get_toolset_for_tool(name) or '?'
        print(f"  {sz:>6}  {ts}/{name}")
    print(f"--- Skills index preview ---")
    print(sp[:600])
    print("...")
    return len(schema_json), len(sp)

if __name__ == '__main__':
    audit()
```

**Step 2: Run it and capture output**

```bash
cd ~/.hermes/hermes-agent
python tools/payload_audit.py
```

Expected: prints baseline numbers matching ~34,295 tool chars + ~14,871 skills chars.

**Step 3: Commit**

```bash
git add tools/payload_audit.py
git commit -m "chore: add payload audit script"
```

---

## Task 2: Trim verbose tool schemas (Option 1)

**Objective:** Cut the top-7 over-worded tool schemas down without changing behavior. Target: save ≥8,000 chars total.

**Files to modify:**
- `tools/memory_tool.py` — `MEMORY_TOOL_SCHEMA` (currently 3,010 chars)
- `tools/delegation_tools.py` (or wherever `delegate_task` schema lives) — (2,828 chars)
- `tools/skills_tool.py` — `skill_manage` schema (2,828 chars)
- `tools/terminal_tool.py` — `terminal` schema (2,805 chars)
- `tools/code_execution_tool.py` — `execute_code` schema (2,550 chars)
- `tools/session_tools.py` (or similar) — `session_search` schema (2,287 chars)
- `tools/file_tools.py` — `search_files` (1,786) and `patch` (1,534 chars)

**Rules for trimming:**
1. Keep parameter names and types exactly as-is
2. Shorten `description` fields: remove redundant examples, cut anything after the first sentence of each parameter description if it's ≥ 3 sentences
3. Cut any inline examples that duplicate what the parameter name already implies
4. Do NOT remove required/optional markers or enum values
5. Each trimmed schema should be ≤ 1,200 chars

**Step 1: Find the schema dicts**

```bash
cd ~/.hermes/hermes-agent
grep -n "SCHEMA\s*=\s*{" tools/memory_tool.py tools/file_tools.py tools/terminal_tool.py
grep -rn "delegate_task\|skill_manage\|session_search\|execute_code" tools/ | grep '"name"' | head -20
```

**Step 2: For each tool, trim the description strings**

For each schema dict, the pattern is:
```python
"description": "Long verbose description that explains the same thing three times and gives five examples..."
```
Trim to:
```python
"description": "One clear sentence. Two if truly needed.",
```

For parameter descriptions specifically:
- Keep: what it does + valid values
- Cut: examples that aren't edge cases, repetition of the parameter name, "for example" blocks

**Step 3: Write a test that checks trimmed schemas still compile**

```python
# tests/tools/test_schema_sizes.py
"""Regression guard: no single tool schema should exceed 1,500 chars."""
import json
import model_tools  # noqa
from tools.registry import registry

MAX_SCHEMA_CHARS = 1500

def test_no_schema_exceeds_limit():
    names = registry.get_all_tool_names()
    schemas = registry.get_definitions(set(names), quiet=True)
    violations = []
    for s in schemas:
        sz = len(json.dumps(s, ensure_ascii=False))
        if sz > MAX_SCHEMA_CHARS:
            violations.append((s['function']['name'], sz))
    assert not violations, f"Oversized schemas: {violations}"

def test_total_schema_payload_under_target():
    names = registry.get_all_tool_names()
    schemas = registry.get_definitions(set(names), quiet=True)
    total = len(json.dumps(schemas, ensure_ascii=False))
    assert total < 20_000, f"Total schema payload {total} chars exceeds 20,000 target"
```

**Step 4: Run tests (expect FAIL initially)**

```bash
cd ~/.hermes/hermes-agent
python -m pytest tests/tools/test_schema_sizes.py -v
```

**Step 5: Trim each schema until tests pass**

Work through each file in order. After trimming each:
```bash
python -m pytest tests/tools/test_schema_sizes.py::test_no_schema_exceeds_limit -v
```

**Step 6: Run full test suite to check no regressions**

```bash
cd ~/.hermes/hermes-agent
python -m pytest tests/ -x -q --timeout=30 2>&1 | tail -30
```

**Step 7: Commit**

```bash
git add tools/ tests/tools/test_schema_sizes.py
git commit -m "perf: trim verbose tool schemas to <=1500 chars each"
```

---

## Task 3: Collapse skills index to category summaries (Options 2 & 5 combined)

**Objective:** Replace the flat 137-skill index (~15k chars) with a compact category-only view (~1.5k chars). Skills remain loadable on-demand via `skill_view`. Saves ~13k chars.

**Files to modify:**
- `agent/prompt_builder.py` — `build_skills_system_prompt()` (lines ~435-587)

**The change:**

Current behavior: lists every skill with its description under each category.

New behavior:
```
## Skills (mandatory)
...
<available_skills>
  autonomous-ai-agents: Spawn and orchestrate autonomous AI coding agents.
    - claude-code: Delegate coding tasks to Claude Code...   ← KEEP for top categories only
    - codex: ...
  dogfood: Hermes-specific dev/ops skills — self-update, cron, routing, plugins...
  mlops: ML training, inference, evaluation, cloud GPU, vector DBs... (40 skills)
  productivity: Google Workspace, Drive, Obsidian, reports, PDF, PowerPoint... (16 skills)
  ...
</available_skills>
```

**Exact approach:**
- Categories with ≤ 5 skills: show full skill list as now
- Categories with > 5 skills: show category name + description + "(N skills)" summary
- Always show all skills for: `software-development`, `autonomous-ai-agents`, `dogfood` (these are used often)

**Step 1: Add a constant to control always-expanded categories**

In `agent/prompt_builder.py`, after the existing constants (~line 188):
```python
# Categories always shown with full skill list regardless of size
_ALWAYS_EXPANDED_CATEGORIES = frozenset({
    "software-development",
    "autonomous-ai-agents",
    "dogfood",
})
_CATEGORY_EXPAND_THRESHOLD = 5  # categories with > N skills collapse to summary
```

**Step 2: Modify the rendering loop in `build_skills_system_prompt`**

Find this block (around line 546):
```python
    if not skills_by_category:
        result = ""
    else:
        index_lines = []
        for category in sorted(skills_by_category.keys()):
            ...
            for name, desc in sorted(skills_by_category[category], ...):
                ...
                index_lines.append(f"    - {name}: {desc}")
```

Change the inner part to:
```python
        index_lines = []
        for category in sorted(skills_by_category.keys()):
            cat_desc = category_descriptions.get(category, "")
            skills_in_cat = skills_by_category[category]
            skill_count = len(set(name for name, _ in skills_in_cat))
            should_expand = (
                category in _ALWAYS_EXPANDED_CATEGORIES
                or skill_count <= _CATEGORY_EXPAND_THRESHOLD
            )
            if cat_desc:
                if should_expand:
                    index_lines.append(f"  {category}: {cat_desc}")
                else:
                    index_lines.append(f"  {category}: {cat_desc} ({skill_count} skills)")
            else:
                if should_expand:
                    index_lines.append(f"  {category}:")
                else:
                    index_lines.append(f"  {category}: ({skill_count} skills)")
            if should_expand:
                seen = set()
                for name, desc in sorted(skills_in_cat, key=lambda x: x[0]):
                    if name in seen:
                        continue
                    seen.add(name)
                    if desc:
                        index_lines.append(f"    - {name}: {desc}")
                    else:
                        index_lines.append(f"    - {name}")
            # else: collapsed — only category line shown
```

**Step 3: Write tests**

```python
# tests/agent/test_skills_index_compact.py
"""Skills index should stay compact by default."""
import os
import pytest
from unittest.mock import patch

def test_skills_index_under_target(tmp_path, monkeypatch):
    """Full skills index (real install) should be under 4,000 chars."""
    from agent.prompt_builder import build_skills_system_prompt, clear_skills_system_prompt_cache
    clear_skills_system_prompt_cache(clear_snapshot=True)
    sp = build_skills_system_prompt()
    clear_skills_system_prompt_cache(clear_snapshot=True)
    # Allow slightly over if very many skills installed, but should be compact
    assert len(sp) < 6_000, f"Skills index {len(sp)} chars, expected < 6,000"

def test_large_category_collapses(tmp_path, monkeypatch):
    """A category with > 5 skills should collapse to a single summary line."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    skills_dir = tmp_path / "skills" / "mlops"
    for i in range(8):
        skill_dir = skills_dir / f"skill-{i}"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: skill-{i}\ndescription: Skill number {i}\n---\n"
        )
    from agent.prompt_builder import build_skills_system_prompt, clear_skills_system_prompt_cache
    clear_skills_system_prompt_cache(clear_snapshot=True)
    sp = build_skills_system_prompt()
    clear_skills_system_prompt_cache(clear_snapshot=True)
    # Should show "(8 skills)" not all 8 entries
    assert "8 skills" in sp
    assert "skill-0:" not in sp  # individual skills not listed

def test_small_category_expands(tmp_path, monkeypatch):
    """A category with <= 5 skills should show full skill list."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    skills_dir = tmp_path / "skills" / "tiny-cat"
    for i in range(3):
        skill_dir = skills_dir / f"skill-{i}"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: skill-{i}\ndescription: Desc {i}\n---\n"
        )
    from agent.prompt_builder import build_skills_system_prompt, clear_skills_system_prompt_cache
    clear_skills_system_prompt_cache(clear_snapshot=True)
    sp = build_skills_system_prompt()
    clear_skills_system_prompt_cache(clear_snapshot=True)
    assert "skill-0" in sp
    assert "skill-1" in sp
    assert "skill-2" in sp
```

**Step 4: Run tests (expect FAIL)**

```bash
python -m pytest tests/agent/test_skills_index_compact.py -v
```

**Step 5: Apply the code change, run tests until green**

```bash
python -m pytest tests/agent/test_skills_index_compact.py -v
python -m pytest tests/agent/test_prompt_builder.py -v
```

**Step 6: Run audit to verify savings**

```bash
python tools/payload_audit.py
```

Expected: skills index drops from ~15k to ~3k chars.

**Step 7: Commit**

```bash
git add agent/prompt_builder.py tests/agent/test_skills_index_compact.py
git commit -m "perf: collapse large skill categories to summary lines in system prompt"
```

---

## Task 4: Add rarely-used tool tier (Option 3)

**Objective:** Mark `rl_*` (10 tools) and `ha_*` (4 tools) as `rarely_used=True` in the registry so they only appear in tool definitions when explicitly enabled via env var `HERMES_INCLUDE_RARELY_USED_TOOLS=1`. Saves ~5k chars from tool schemas.

**Files to modify:**
- `tools/registry.py` — add `rarely_used` field to `ToolEntry` and filter in `get_definitions()`
- `tools/rl_training_tool.py` — add `rarely_used=True` to all `registry.register()` calls
- (find ha_* tool file) — add `rarely_used=True` to all `registry.register()` calls
- `tools/payload_audit.py` — update to show rarely_used count

**Step 1: Find rl and ha tool files**

```bash
cd ~/.hermes/hermes-agent
grep -rn "rl_list_environments\|ha_list_entities" tools/ | grep "registry.register" | head -10
```

**Step 2: Add `rarely_used` to ToolEntry and registry.register()**

In `tools/registry.py`, update `ToolEntry.__slots__` and `__init__`:
```python
__slots__ = (
    "name", "toolset", "schema", "handler", "check_fn",
    "requires_env", "is_async", "description", "emoji", "rarely_used",
)

def __init__(self, name, toolset, schema, handler, check_fn,
             requires_env, is_async, description, emoji, rarely_used=False):
    ...
    self.rarely_used = rarely_used
```

Update `register()` method signature:
```python
def register(
    self,
    name: str,
    toolset: str,
    schema: dict,
    handler: Callable,
    check_fn: Callable = None,
    requires_env: list = None,
    is_async: bool = False,
    description: str = "",
    emoji: str = "",
    rarely_used: bool = False,
):
    ...
    self._tools[name] = ToolEntry(
        ...
        rarely_used=rarely_used,
    )
```

Update `get_definitions()` to filter rarely_used tools unless env var is set:
```python
def get_definitions(self, tool_names: Set[str], quiet: bool = False) -> List[dict]:
    include_rarely_used = os.environ.get("HERMES_INCLUDE_RARELY_USED_TOOLS", "").lower() in ("1", "true", "yes")
    result = []
    ...
    for name in sorted(tool_names):
        entry = self._tools.get(name)
        if not entry:
            continue
        if entry.rarely_used and not include_rarely_used:
            continue
        ...
```

Add `import os` at top of `tools/registry.py` if not present.

**Step 3: Mark rl_* and ha_* tools as rarely_used**

In each `registry.register(...)` call for rl_* and ha_* tools, add `rarely_used=True`:
```python
registry.register(
    name="rl_list_environments",
    ...
    rarely_used=True,
)
```

**Step 4: Write test**

```python
# tests/tools/test_rarely_used_tools.py
import os
import model_tools  # noqa
from tools.registry import registry

def test_rl_tools_excluded_by_default(monkeypatch):
    monkeypatch.delenv("HERMES_INCLUDE_RARELY_USED_TOOLS", raising=False)
    names = registry.get_all_tool_names()
    schemas = registry.get_definitions(set(names), quiet=True)
    tool_names = [s['function']['name'] for s in schemas]
    assert "rl_list_environments" not in tool_names
    assert "ha_list_entities" not in tool_names

def test_rl_tools_included_when_env_set(monkeypatch):
    monkeypatch.setenv("HERMES_INCLUDE_RARELY_USED_TOOLS", "1")
    names = registry.get_all_tool_names()
    schemas = registry.get_definitions(set(names), quiet=True)
    tool_names = [s['function']['name'] for s in schemas]
    assert "rl_list_environments" in tool_names
```

**Step 5: Run tests**

```bash
python -m pytest tests/tools/test_rarely_used_tools.py -v
python -m pytest tests/tools/test_schema_sizes.py -v
```

**Step 6: Commit**

```bash
git add tools/registry.py tools/rl_training_tool.py tests/tools/test_rarely_used_tools.py
git commit -m "perf: add rarely_used tier to tool registry, hide rl/ha tools by default"
```

---

## Task 5: Verify Anthropic prompt caching is working (Option 4 — audit only)

**Objective:** Confirm `apply_anthropic_cache_control` is actually being called for Anthropic providers and that the system prompt breakpoint is being set. No code changes needed — just verify and document.

**Files to read:**
- `agent/prompt_caching.py`
- `run_agent.py` — search for `apply_anthropic_cache_control`

**Step 1: Find where caching is applied**

```bash
cd ~/.hermes/hermes-agent
grep -n "apply_anthropic_cache_control\|cache_control\|prompt_caching" run_agent.py | head -20
```

**Step 2: Verify the system prompt cache breakpoint is set**

Confirm that in the request-building path, the system message gets `cache_control: {type: ephemeral}` injected. If it does, the ~5k-token system prompt is only billed once per 5-minute TTL window.

**Step 3: If NOT being applied for non-Anthropic providers, document that**

Write a note in `docs/plans/` about which providers support prompt caching. GPT-4o-mini (our default) does NOT support `cache_control` — so every turn re-bills the full prompt. This means savings from Task 2 and Task 3 directly reduce per-turn costs for GPT.

**Step 4: No commit needed** — this is audit-only.

---

## Task 6: Full audit and measurement

**Objective:** Run the payload audit before and after all changes to quantify improvements.

**Step 1: Run the audit script**

```bash
cd ~/.hermes/hermes-agent
python tools/payload_audit.py
```

**Step 2: Run the full test suite**

```bash
python -m pytest tests/ -x -q --timeout=30 2>&1 | tail -40
```

Expected: all tests pass.

**Step 3: Record final numbers**

Expected results after all tasks:
```
Tool schemas : ≤ 18,000 chars  (was 34,295) — savings from Task 2 + Task 4
Skills index : ≤  4,000 chars  (was 14,871) — savings from Task 3
TOTAL        : ≤ 32,000 chars  (was 55,567)
Reduction    : ~42% payload cut
```

**Step 4: Commit audit results as a doc**

```bash
# paste numbers into docs/plans/2026-03-28-payload-optimization.md under a "Results" section
git add docs/plans/2026-03-28-payload-optimization.md
git commit -m "docs: record payload optimization results"
```

---

## Results (fill in after Task 6)

| Component | Before | After | Saved |
|-----------|--------|-------|-------|
| Tool schemas | 34,295 | TBD | TBD |
| Skills index | 14,871 | TBD | TBD |
| Total payload | ~55,567 | TBD | TBD |
| ~Tokens | ~13,892 | TBD | TBD |
