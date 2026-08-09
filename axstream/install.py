"""`axstream install` — one-command agent onboarding.

Wires axstream into every coding agent found on the machine, idempotently:

  * Claude Code  skill -> ~/.claude/skills/axstream/SKILL.md
                 agent -> ~/.claude/agents/computer-use.md (the fast-model
                 UI subagent the skill delegates multi-step tasks to)
                 MCP   -> `claude mcp add axstream --scope user -- <axstream> mcp`
  * Codex        skill -> ~/.agents/skills/axstream/SKILL.md
                 MCP   -> `codex mcp add axstream -- <axstream> mcp`
                 (falls back to appending [mcp_servers.axstream] to
                 ~/.codex/config.toml when the subcommand is missing)

The skill file ships INSIDE the package (axstream/skills/axstream/SKILL.md)
and is COPIED, not symlinked — a pip/uv upgrade replaces the package files,
and `axstream install` after an upgrade refreshes the copies. Agents that
aren't installed are skipped with a note, never an error. Everything prints
what it did so the user can audit; re-running is always safe.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_SRC = Path(__file__).parent / "skills" / "axstream" / "SKILL.md"
AGENT_SRC = Path(__file__).parent / "agents" / "computer-use.md"

CODEX_TOML_BLOCK = """
[mcp_servers.axstream]
command = "{command}"
args = ["mcp"]
"""


def _axstream_bin() -> str:
    """Absolute path of the axstream entrypoint — agents spawn MCP servers
    with their own PATH, which often misses ~/.local/bin."""
    found = shutil.which("axstream")
    if found:
        return str(Path(found).resolve())
    return f"{sys.executable} -m axstream"  # last resort: module form


def _install_skill(dest_root: Path, label: str) -> str:
    dest = dest_root / "axstream" / "SKILL.md"
    if not SKILL_SRC.is_file():
        return f"  [!!] {label} skill: packaged SKILL.md missing ({SKILL_SRC})"
    if dest.parent.is_symlink():
        # a dev-checkout symlink from before packaging — replace with a copy
        dest.parent.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fresh = not dest.exists() or dest.read_text() != SKILL_SRC.read_text()
    if fresh:
        shutil.copyfile(SKILL_SRC, dest)
    return f"  [ok ] {label} skill: {dest}" + ("" if fresh else " (already current)")


def _install_agent(dest_root: Path, label: str) -> str:
    """Copy the computer-use subagent definition (Claude Code only — Codex
    has no subagent concept). Same copy-not-symlink rule as the skill."""
    dest = dest_root / AGENT_SRC.name
    if not AGENT_SRC.is_file():
        return f"  [!!] {label} agent: packaged {AGENT_SRC.name} missing ({AGENT_SRC})"
    dest.parent.mkdir(parents=True, exist_ok=True)
    fresh = not dest.exists() or dest.read_text() != AGENT_SRC.read_text()
    if fresh:
        shutil.copyfile(AGENT_SRC, dest)
    return f"  [ok ] {label} agent: {dest}" + ("" if fresh else " (already current)")


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _install_claude_mcp(bin_path: str) -> str:
    if shutil.which("claude") is None:
        return "  [ - ] Claude Code not found — skipped (install: https://claude.com/claude-code)"
    code, out = _run(["claude", "mcp", "get", "axstream"])
    if code == 0:
        return "  [ok ] Claude Code MCP: already registered"
    code, out = _run(["claude", "mcp", "add", "axstream", "--scope", "user",
                      "--", *bin_path.split(" "), "mcp"])
    if code != 0:
        return f"  [!!] Claude Code MCP registration failed: {out.splitlines()[-1] if out else code}"
    return "  [ok ] Claude Code MCP: registered (user scope)"


def _install_codex_mcp(bin_path: str) -> str:
    if shutil.which("codex") is None:
        return "  [ - ] Codex not found — skipped"
    code, _out = _run(["codex", "mcp", "get", "axstream"])
    if code == 0:
        return "  [ok ] Codex MCP: already registered"
    code, out = _run(["codex", "mcp", "add", "axstream",
                      "--", *bin_path.split(" "), "mcp"])
    if code == 0:
        return "  [ok ] Codex MCP: registered"
    # older codex without `mcp add` — append to config.toml ourselves
    cfg = Path("~/.codex/config.toml").expanduser()
    text = cfg.read_text() if cfg.exists() else ""
    if "[mcp_servers.axstream]" in text:
        return "  [ok ] Codex MCP: already in config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    block = CODEX_TOML_BLOCK.format(command=bin_path)
    cfg.write_text(text + ("" if text.endswith("\n") or not text else "\n") + block)
    return f"  [ok ] Codex MCP: appended to {cfg}"


def cmd_install(argv: list[str]) -> int:
    as_json = "--json" in argv
    bin_path = _axstream_bin()
    lines = [
        _install_skill(Path("~/.claude/skills").expanduser(), "Claude Code"),
        _install_agent(Path("~/.claude/agents").expanduser(), "Claude Code"),
        _install_skill(Path("~/.agents/skills").expanduser(), "Codex"),
        _install_claude_mcp(bin_path),
        _install_codex_mcp(bin_path),
    ]
    if as_json:
        print(json.dumps({"results": [l.strip() for l in lines],
                          "axstream": bin_path}))
    else:
        print("axstream agent setup:")
        print("\n".join(lines))
        print("\nSandbox note: replay needs the cua-driver socket in "
              "~/Library/Caches —\nsandboxed agents (Codex default) must run "
              "`axstream replay` with approved/\nescalated execution. "
              "Details are in the installed skill.")
        print("Verify prerequisites any time: axstream --doctor")
    return 1 if any("[!!]" in l for l in lines) else 0
