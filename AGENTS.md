# Working in this repo

- Tests: `python -m pytest -q` from the checkout root. Use `python -m` so the checkout is
  imported; a bare `pytest` in a worktree imports the editable install of another checkout.
- The suite runs on the real Windows desktop. A test must never send a pointer step with
  `go=true` on an image or screen stamp: `act` maps it to screen pixels and really clicks.
  Live-path tests use fakes, or keys only (refused without a window).
- The running MCP server is the editable install of the `master` checkout. Work in a
  separate worktree and branch (`git worktree add -b <branch> ../cu-perceive-<name> master`).
- MCP tool docstrings are checked against their signatures (`tests/test_round9.py`): a
  `name=` token in a docstring must be a parameter of that tool.
- Docs to keep in step with behaviour: `README.md` (English) and `SKILL.md` (Chinese).
