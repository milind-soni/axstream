---
description: Status of fast computer use — macro library, verified state, prerequisites
---

Show the user the state of their axstream setup, compactly:

1. Run `axstream --doctor` (if the command is missing, run `uvx --from axstream axstream --doctor`; if that also fails, tell the user to `uv tool install axstream`).
2. Run `axstream list --json` and summarize: how many macros, which are `verified` vs `unverified`/`stale` (use the list_macros MCP tool if available — it includes the verified field).
3. If the cua-driver check failed, give the one-line install: `/bin/bash -c "$(curl -fsSL https://cua.ai/driver/install.sh)"` and note the Accessibility permission grant.

Keep it to a short status block, then one suggestion: either "try /axstream-teach to compile a task" (if the library is thin) or the name of an unverified macro worth gating with `axstream verify`.
