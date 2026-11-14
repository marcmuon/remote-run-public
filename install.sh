#!/bin/sh
# Install remote-run for this user: the CLI on PATH and the agent skill for every harness.
# Idempotent. The skill in this repo (skill/remote-run) is the source of truth.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$HOME/.local/bin" "$HOME/.agents/skills"
ln -sfn "$here/bin/remote-run" "$HOME/.local/bin/remote-run"
# Codex, OMP, Pi and Qwen read ~/.agents/skills directly.
rsync -a --delete "$here/skill/remote-run/" "$HOME/.agents/skills/remote-run/"
# Claude Code and Hermes need a link in their own skills dir.
for d in "$HOME/.claude/skills" "$HOME/.hermes/skills"; do
  if [ -d "$d" ] && [ ! -e "$d/remote-run" ]; then
    ln -s ../../.agents/skills/remote-run "$d/remote-run"
  fi
done
echo "remote-run: $(command -v remote-run || echo "$HOME/.local/bin/remote-run (add ~/.local/bin to PATH)")"
echo "skill:      $HOME/.agents/skills/remote-run"
