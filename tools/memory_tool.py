#!/usr/bin/env python3
"""
Memory Tool Module - Persistent Curated Memory

Provides bounded, file-backed memory that persists across sessions. Two stores:
  - MEMORY.md: agent's personal notes and observations (environment facts, project
    conventions, tool quirks, things learned)
  - USER.md: what the agent knows about the user (preferences, communication style,
    expectations, workflow habits)

Both are injected into the system prompt as a frozen snapshot at session start.
Mid-session writes update files on disk immediately (durable) but do NOT change
the system prompt -- this preserves the prefix cache for the entire session.
The snapshot refreshes on the next session start.

Entry delimiter: § (section sign). Entries can be multiline.
Character limits (not tokens) because char counts are model-independent.

Design:
- Single `memory` tool with action parameter: add, replace, remove, read
- replace/remove use short unique substring matching (not full text or IDs)
- Behavioral guidance lives in the tool schema description
- Frozen snapshot pattern: system prompt is stable, tool responses show live state
"""

import fcntl
import json
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# Where memory files live
MEMORY_DIR = get_hermes_home() / "memories"

ENTRY_DELIMITER = "\n§\n"


# ---------------------------------------------------------------------------
# Memory content scanning — lightweight check for injection/exfiltration
# in content that gets injected into the system prompt.
# ---------------------------------------------------------------------------

_MEMORY_THREAT_PATTERNS = [
    # Prompt injection
    (r"ignore\s+(previous|all|above|prior)\s+instructions", "prompt_injection"),
    (r"you\s+are\s+now\s+", "role_hijack"),
    (r"do\s+not\s+tell\s+the\s+user", "deception_hide"),
    (r"system\s+prompt\s+override", "sys_prompt_override"),
    (
        r"disregard\s+(your|all|any)\s+(instructions|rules|guidelines)",
        "disregard_rules",
    ),
    (
        r"act\s+as\s+(if|though)\s+you\s+(have\s+no|don\'t\s+have)\s+(restrictions|limits|rules)",
        "bypass_restrictions",
    ),
    # Exfiltration via curl/wget with secrets
    (r"curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_curl"),
    (r"wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_wget"),
    (
        r"cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)",
        "read_secrets",
    ),
    # Persistence via shell rc
    (r"authorized_keys", "ssh_backdoor"),
    (r"\$HOME/\.ssh|\~/\.ssh", "ssh_access"),
    (r"\$HOME/\.hermes/\.env|\~/\.hermes/\.env", "hermes_env"),
]

# Subset of invisible chars for injection detection
_INVISIBLE_CHARS = {
    "\u200b",
    "\u200c",
    "\u200d",
    "\u2060",
    "\ufeff",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
}


def _scan_memory_content(content: str) -> Optional[str]:
    """Scan memory content for injection/exfil patterns. Returns error string if blocked."""
    # Check invisible unicode
    for char in _INVISIBLE_CHARS:
        if char in content:
            return f"Blocked: content contains invisible unicode character U+{ord(char):04X} (possible injection)."

    # Check threat patterns
    for pattern, pid in _MEMORY_THREAT_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return f"Blocked: content matches threat pattern '{pid}'. Memory entries are injected into the system prompt and must not contain injection or exfiltration payloads."

    return None


class MemoryStore:
    """
    Bounded curated memory with file persistence. One instance per AIAgent.

    Maintains two parallel states:
      - _system_prompt_snapshot: frozen at load time, used for system prompt injection.
        Never mutated mid-session. Keeps prefix cache stable.
      - memory_entries / user_entries: live state, mutated by tool calls, persisted to disk.
        Tool responses always reflect this live state.
    """

    def __init__(self, memory_char_limit: int = 2200, user_char_limit: int = 1375):
        self.memory_entries: List[str] = []
        self.user_entries: List[str] = []
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        # Frozen snapshot for system prompt -- set once at load_from_disk()
        self._system_prompt_snapshot: Dict[str, str] = {"memory": "", "user": ""}

    def load_from_disk(self):
        """Load entries from MEMORY.md and USER.md, capture system prompt snapshot."""
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)

        self.memory_entries = self._read_file(MEMORY_DIR / "MEMORY.md")
        self.user_entries = self._read_file(MEMORY_DIR / "USER.md")

        # Deduplicate entries (preserves order, keeps first occurrence)
        self.memory_entries = list(dict.fromkeys(self.memory_entries))
        self.user_entries = list(dict.fromkeys(self.user_entries))

        # Note: high/low priority splitting happens dynamically in _render_block()
        # using the '[LOW] ' prefix stored in each entry. No separate lists needed.

        # Capture frozen snapshot for system prompt injection
        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", self.memory_entries),
            "user": self._render_block("user", self.user_entries),
        }

    @staticmethod
    @contextmanager
    def _file_lock(path: Path):
        """Acquire an exclusive file lock for read-modify-write safety.

        Uses a separate .lock file so the memory file itself can still be
        atomically replaced via os.replace().
        """
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(lock_path, "w")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()

    @staticmethod
    def _path_for(target: str) -> Path:
        if target == "user":
            return MEMORY_DIR / "USER.md"
        return MEMORY_DIR / "MEMORY.md"

    def _reload_target(self, target: str):
        """Re-read entries from disk into in-memory state.

        Called under file lock to get the latest state before mutating.
        """
        fresh = self._read_file(self._path_for(target))
        fresh = list(dict.fromkeys(fresh))  # deduplicate
        self._set_entries(target, fresh)

    def save_to_disk(self, target: str):
        """Persist entries to the appropriate file. Called after every mutation."""
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        self._write_file(self._path_for(target), self._entries_for(target))

    def _entries_for(self, target: str) -> List[str]:
        if target == "user":
            return self.user_entries
        return self.memory_entries

    def _set_entries(self, target: str, entries: List[str]):
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        if not entries:
            return 0
        return len(ENTRY_DELIMITER.join(entries))

    def _char_limit(self, target: str) -> int:
        if target == "user":
            return self.user_char_limit
        return self.memory_char_limit

    def add(self, target: str, content: str, priority: str = "high") -> Dict[str, Any]:
        """Append a new entry. Returns error if it would exceed the char limit.

        Args:
            target: 'memory' or 'user'
            content: The text to store.
            priority: 'high' (always injected, default) or 'low' (suppressed when
                      store is near capacity). Low-priority entries are prefixed with
                      '[LOW] ' on disk and omitted from system-prompt injection when
                      they would push usage over ~70% of cap.
        """
        content = content.strip()
        # Strip accidental [LOW] prefix from caller
        if content.startswith("[LOW] "):
            content = content[6:]
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        # Determine stored form
        stored_content = ("[LOW] " + content) if priority == "low" else content

        # Scan for injection/exfiltration before accepting
        scan_error = _scan_memory_content(content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            # Re-read from disk under lock to pick up writes from other sessions
            self._reload_target(target)

            entries = self._entries_for(target)
            limit = self._char_limit(target)

            # Reject exact duplicates (check both stored forms)
            if stored_content in entries or content in entries:
                return self._success_response(
                    target, "Entry already exists (no duplicate added)."
                )

            # Calculate what the new total would be
            new_entries = entries + [stored_content]
            new_total = len(ENTRY_DELIMITER.join(new_entries))

            if new_total > limit:
                current = self._char_count(target)
                # Suggest an eviction candidate (shortest low-priority entry)
                low_entries = [e for e in entries if e.startswith("[LOW] ")]
                error_resp: Dict[str, Any] = {
                    "success": False,
                    "error": (
                        f"Memory at {current:,}/{limit:,} chars. "
                        f"Adding this entry ({len(stored_content)} chars) would exceed the limit. "
                        f"Replace or remove existing entries first."
                    ),
                    "current_entries": entries,
                    "usage": f"{current:,}/{limit:,}",
                }
                if low_entries:
                    candidate = min(low_entries, key=len)
                    error_resp["eviction_candidate"] = candidate[:50]
                    error_resp["chars_freed_if_removed"] = len(candidate) + len(ENTRY_DELIMITER)
                return error_resp

            entries.append(stored_content)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry added.")

    def replace(self, target: str, old_text: str, new_content: str) -> Dict[str, Any]:
        """Find entry containing old_text substring, replace it with new_content."""
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {
                "success": False,
                "error": "new_content cannot be empty. Use 'remove' to delete entries.",
            }

        # Scan replacement content for injection/exfiltration
        scan_error = _scan_memory_content(new_content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            self._reload_target(target)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if len(matches) == 0:
                return {"success": False, "error": f"No entry matched '{old_text}'."}

            if len(matches) > 1:
                # If all matches are identical (exact duplicates), operate on the first one
                unique_texts = set(e for _, e in matches)
                if len(unique_texts) > 1:
                    previews = [
                        e[:80] + ("..." if len(e) > 80 else "") for _, e in matches
                    ]
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }
                # All identical -- safe to replace just the first

            idx = matches[0][0]
            limit = self._char_limit(target)

            # Check that replacement doesn't blow the budget
            test_entries = entries.copy()
            test_entries[idx] = new_content
            new_total = len(ENTRY_DELIMITER.join(test_entries))

            if new_total > limit:
                return {
                    "success": False,
                    "error": (
                        f"Replacement would put memory at {new_total:,}/{limit:,} chars. "
                        f"Shorten the new content or remove other entries first."
                    ),
                }

            entries[idx] = new_content
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> Dict[str, Any]:
        """Remove the entry containing old_text substring."""
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        with self._file_lock(self._path_for(target)):
            self._reload_target(target)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if len(matches) == 0:
                return {"success": False, "error": f"No entry matched '{old_text}'."}

            if len(matches) > 1:
                # If all matches are identical (exact duplicates), remove the first one
                unique_texts = set(e for _, e in matches)
                if len(unique_texts) > 1:
                    previews = [
                        e[:80] + ("..." if len(e) > 80 else "") for _, e in matches
                    ]
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }
                # All identical -- safe to remove just the first

            idx = matches[0][0]
            entries.pop(idx)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry removed.")

    def format_for_system_prompt(self, target: str) -> Optional[str]:
        """
        Return the frozen snapshot for system prompt injection.

        This returns the state captured at load_from_disk() time, NOT the live
        state. Mid-session writes do not affect this. This keeps the system
        prompt stable across all turns, preserving the prefix cache.

        Returns None if the snapshot is empty (no entries at load time).
        """
        block = self._system_prompt_snapshot.get(target, "")
        return block if block else None

    # -- Internal helpers --

    def _success_response(self, target: str, message: str = None) -> Dict[str, Any]:
        entries = self._entries_for(target)
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        resp = {
            "success": True,
            "target": target,
            "entries": entries,
            "usage": f"{pct}% — {current:,}/{limit:,} chars",
            "entry_count": len(entries),
        }
        if message:
            resp["message"] = message
        return resp

    def _render_block(self, target: str, entries: List[str]) -> str:
        """Render a system prompt block with priority-aware injection.

        High-priority entries are always included. Low-priority entries (prefixed
        '[LOW] ') are included only if they fit within the cap after high entries.
        Suppressed low entries are counted and noted in the block footer.
        '[LOW] ' prefixes are stripped for display.
        """
        if not entries:
            return ""

        limit = self._char_limit(target)

        high_entries = [e for e in entries if not e.startswith("[LOW] ")]
        low_entries = [e for e in entries if e.startswith("[LOW] ")]

        # Build high content (always injected)
        high_parts = [e for e in high_entries]  # already display-ready
        content = ENTRY_DELIMITER.join(high_parts)
        high_chars = len(content)

        # Remaining budget for low-priority entries
        remaining = limit - high_chars
        if high_parts and low_entries:
            remaining -= len(ENTRY_DELIMITER)  # separator before first low entry

        included_low: List[str] = []
        suppressed_count = 0
        suppressed_chars = 0
        for low_entry in low_entries:
            display = low_entry[6:]  # strip '[LOW] '
            cost = len(display) + (len(ENTRY_DELIMITER) if included_low else 0)
            if cost <= remaining:
                included_low.append(display)
                remaining -= cost
            else:
                suppressed_count += 1
                suppressed_chars += len(display)

        # Assemble final content
        all_display_parts = high_parts + included_low
        if suppressed_count:
            all_display_parts.append(
                f"[{suppressed_count} low-priority entries suppressed to save ~{suppressed_chars} chars]"
            )
        content = ENTRY_DELIMITER.join(all_display_parts)

        current = len(content)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        if target == "user":
            header = (
                f"USER PROFILE (who the user is) [{pct}% — {current:,}/{limit:,} chars]"
            )
        else:
            header = (
                f"MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]"
            )

        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"

    @staticmethod
    def _read_file(path: Path) -> List[str]:
        """Read a memory file and split into entries.

        No file locking needed: _write_file uses atomic rename, so readers
        always see either the previous complete file or the new complete file.
        """
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, IOError):
            return []

        if not raw.strip():
            return []

        # Use ENTRY_DELIMITER for consistency with _write_file. Splitting by "§"
        # alone would incorrectly split entries that contain "§" in their content.
        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
        return [e for e in entries if e]

    @staticmethod
    def _write_file(path: Path, entries: List[str]):
        """Write entries to a memory file using atomic temp-file + rename.

        Previous implementation used open("w") + flock, but "w" truncates the
        file *before* the lock is acquired, creating a race window where
        concurrent readers see an empty file. Atomic rename avoids this:
        readers always see either the old complete file or the new one.
        """
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        try:
            # Write to temp file in same directory (same filesystem for atomic rename)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp", prefix=".mem_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(path))  # Atomic on same filesystem
            except BaseException:
                # Clean up temp file on any failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except (OSError, IOError) as e:
            raise RuntimeError(f"Failed to write memory file {path}: {e}")


def memory_tool(
    action: str,
    target: str = "memory",
    content: str = None,
    old_text: str = None,
    priority: str = "high",
    store: Optional[MemoryStore] = None,
) -> str:
    """
    Single entry point for the memory tool. Dispatches to MemoryStore methods.

    Returns JSON string with results.
    """
    # read_ctx is store-independent — bypass store and target checks
    if action != "read_ctx":
        if store is None:
            return json.dumps(
                {
                    "success": False,
                    "error": "Memory is not available. It may be disabled in config or this environment.",
                },
                ensure_ascii=False,
            )

        if target not in ("memory", "user"):
            return json.dumps(
                {
                    "success": False,
                    "error": f"Invalid target '{target}'. Use 'memory' or 'user'.",
                },
                ensure_ascii=False,
            )

    if action == "add":
        if not content:
            return json.dumps(
                {"success": False, "error": "Content is required for 'add' action."},
                ensure_ascii=False,
            )
        result = store.add(target, content, priority=priority)

    elif action == "replace":
        if not old_text:
            return json.dumps(
                {
                    "success": False,
                    "error": "old_text is required for 'replace' action.",
                },
                ensure_ascii=False,
            )
        if not content:
            return json.dumps(
                {
                    "success": False,
                    "error": "content is required for 'replace' action.",
                },
                ensure_ascii=False,
            )
        result = store.replace(target, old_text, content)

    elif action == "remove":
        if not old_text:
            return json.dumps(
                {
                    "success": False,
                    "error": "old_text is required for 'remove' action.",
                },
                ensure_ascii=False,
            )
        result = store.remove(target, old_text)

    elif action == "read_ctx":
        # Lazy-load a context file from ~/.hermes/memories/ctx/<name>.md
        # `target` is repurposed as the context name when action='read_ctx'
        ctx_name = target.strip().lstrip("/")
        # Prevent path traversal
        if ".." in ctx_name or ctx_name.startswith("/"):
            return json.dumps(
                {"success": False, "error": "Invalid ctx name — path traversal not allowed."},
                ensure_ascii=False,
            )
        # Strip .md suffix if caller included it
        if ctx_name.endswith(".md"):
            ctx_name = ctx_name[:-3]
        ctx_path = MEMORY_DIR / "ctx" / f"{ctx_name}.md"
        if not ctx_path.exists():
            available = sorted(p.stem for p in (MEMORY_DIR / "ctx").glob("*.md")) if (MEMORY_DIR / "ctx").exists() else []
            return json.dumps(
                {
                    "success": False,
                    "error": f"ctx/{ctx_name}.md not found.",
                    "available": available,
                },
                ensure_ascii=False,
            )
        try:
            text = ctx_path.read_text(encoding="utf-8").strip()
            scan_error = _scan_memory_content(text)
            if scan_error:
                return json.dumps({"success": False, "error": f"ctx file blocked: {scan_error}"}, ensure_ascii=False)
            return json.dumps(
                {"success": True, "ctx": ctx_name, "content": text, "chars": len(text)},
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    else:
        return json.dumps(
            {
                "success": False,
                "error": f"Unknown action '{action}'. Use: add, replace, remove, read_ctx",
            },
            ensure_ascii=False,
        )

    return json.dumps(result, ensure_ascii=False)


def check_memory_requirements() -> bool:
    """Memory tool has no external requirements -- always available."""
    return True


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

MEMORY_SCHEMA = {
    "name": "memory",
    "description": (
        "Save durable information to persistent memory that survives across sessions. "
        "Memory is injected into future turns — keep entries compact and focused on facts "
        "that will still matter later. Save proactively: user preferences/corrections, "
        "env facts, tool quirks, stable conventions. "
        "Do NOT save task progress or session outcomes; use session_search for those.\n\n"
        "TWO TARGETS:\n"
        "- 'user': who the user is — name, role, preferences, pet peeves\n"
        "- 'memory': your notes — env facts, project conventions, tool quirks\n\n"
        "ACTIONS: add (new entry), replace (update existing — old_text identifies it), "
        "remove (delete — old_text identifies it).\n\n"
        "SKIP: trivial info, things easily re-discovered, raw data dumps, temporary state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "replace", "remove", "read_ctx"],
                "description": (
                    "The action to perform. "
                    "'add' — append a new entry. "
                    "'replace' — update an existing entry. "
                    "'remove' — delete an entry. "
                    "'read_ctx' — lazy-load a context file from ctx/<name>.md. "
                    "Use read_ctx whenever you encounter a memory pointer like "
                    "'→ ctx/telegram.md' or when you need topic-specific details "
                    "that aren't in the main memory blocks."
                ),
            },
            "target": {
                "type": "string",
                "description": (
                    "For add/replace/remove: 'memory' or 'user'. "
                    "For read_ctx: the context file name, e.g. 'telegram', 'email', "
                    "'crons', 'biz-ideas', 'shopping', 'planning', 'hermes-dev'."
                ),
            },
            "content": {
                "type": "string",
                "description": "The entry content. Required for 'add' and 'replace'.",
            },
            "old_text": {
                "type": "string",
                "description": "Short unique substring identifying the entry to replace or remove.",
            },
            "priority": {
                "type": "string",
                "enum": ["high", "low"],
                "description": (
                    "Injection priority for 'add' action. "
                    "'high' (default) — always injected into every turn. "
                    "'low' — only injected when the store has budget to spare; "
                    "suppressed at near-capacity to save tokens. "
                    "Use 'low' for nice-to-have context (shopping lists, transient config) "
                    "and 'high' for must-know facts (user identity, critical paths, active IDs)."
                ),
            },
        },
        "required": ["action", "target"],
    },
}


# --- Registry ---
from tools.registry import registry

registry.register(
    name="memory",
    toolset="memory",
    schema=MEMORY_SCHEMA,
    handler=lambda args, **kw: memory_tool(
        action=args.get("action", ""),
        target=args.get("target", "memory"),
        content=args.get("content"),
        old_text=args.get("old_text"),
        priority=args.get("priority", "high"),
        store=kw.get("store"),
    ),
    check_fn=check_memory_requirements,
    emoji="🧠",
)
