"""Standalone payload audit — run with: python tools/payload_audit.py"""

import json, sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def audit():
    import model_tools
    from tools.registry import registry
    from agent.prompt_builder import build_skills_system_prompt

    names = registry.get_all_tool_names()
    schemas = registry.get_definitions(set(names), quiet=True)
    schema_json = json.dumps(schemas, ensure_ascii=False)
    sp = build_skills_system_prompt()
    by_tool = sorted(
        [(s["function"]["name"], len(json.dumps(s))) for s in schemas],
        key=lambda x: -x[1],
    )
    print(f"=== HERMES PAYLOAD AUDIT ===")
    print(f"Tool schemas  : {len(schema_json):>7,} chars  ({len(schemas)} tools)")
    print(f"Skills index  : {len(sp):>7,} chars")
    print("--- Top 15 tool schemas ---")
    for name, sz in by_tool[:15]:
        ts = registry.get_toolset_for_tool(name) or "?"
        print(f"  {sz:>6}  {ts}/{name}")
    return len(schema_json), len(sp)


if __name__ == "__main__":
    audit()
