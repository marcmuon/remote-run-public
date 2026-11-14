# remote-run

Source: [marcmuon/remote-run-public](https://github.com/marcmuon/remote-run-public).

Send heavy jobs from coding agents (simulations, backtests, sweeps) to another machine over SSH, one at a time, and get the results back. The agents, the editor and the repos all stay on the laptop.

```
laptop (agents, editor, repos)                          execution host (e.g. a spare 32 GB Mac)
remote-run submit -- <cmd>  ── snapshot + inputs  ──▶   ~/remote-run/jobs/<id>/src
                            ── tmux + rr_job.py   ──▶   host lock, caffeinate, uv sync, run <cmd>
remote-run wait / fetch     ◀── status, logs, results   status.json, run.log, mem.tsv
```

## Why

Several coding agents can each launch a memory-hungry job at the same time. A limit inside each agent does not limit the total number of jobs.

remote-run moves the execution rather than the agents. The concurrency gate lives in the runner, not in the prompts:
- **One host-wide lock**, shared by every agent session. It is a kernel `flock` held for the life of the job, and the job's own process inherits it.
- **No local fallback.** A busy or unreachable host returns a distinct exit code (75 or 69) with a "do not run this locally" message that agents are told to obey.
- **Everything the agent needs to report:** exit status, elapsed time, peak memory of the parent plus every child process, logs, and result files.

## Quick start

The CLI needs Python 3.9+, git, ssh and rsync. The execution host needs Python 3.9+, tmux and rsync, plus uv when the project uses it. macOS hosts are the tested path.

Configure SSH access to your host as `remote-worker`. Copy `hosts.json` to `hosts.local.json`, set its SSH alias, Python path and memory limits, then set `REMOTE_RUN_HOSTS_FILE` to the absolute path of that local file. The local file is gitignored. Every client for one execution host must use the same `root`, because the lock lives in that directory.

```sh
./install.sh                      # CLI -> ~/.local/bin, skill -> ~/.agents/skills (+ Claude/Hermes links)
cd ~/Projects/my-sim-repo
remote-run submit --label "wave 1" -- uv run python run_sweep.py --workers 1
remote-run wait <job>             # exits with the job's exit code
remote-run fetch <job>            # results -> ~/.local/share/remote-run/results/<project>/<job>/
```

| command | what it does |
|---|---|
| `submit [opts] -- <cmd>` | Snapshot the checkout, sync inputs, start `<cmd>` on the host. Prints the job id. Options: `--queue` waits in line instead of exiting 75; `--wait` blocks until the job ends; `--git` ships HEAD's history; `--env K=V`; `--data` and `--external` add inputs; `--dry-run` shows the plan |
| `wait <job>` | Block until the job ends, then exit with its exit code |
| `status <job> [--json]` | State, elapsed time, peak tree RSS, kill reason |
| `logs <job> [-n N] [--setup] [-f]` | Tail the job's output |
| `fetch <job> [paths]` | Copy logs and result paths back. Paths are workdir-relative or absolute on the host |
| `cancel <job>` | Stop a queued or running job and its whole process tree |
| `host` | Lock holder, queue, memory, swap, disk, power, lid |
| `list`, `clean --days N` | Recent jobs; delete finished job dirs |
| `snapshot -o F [--extract D]` | Build the exact snapshot without submitting, for a local reference run |

| exit | meaning |
|---|---|
| 0 | submitted (running or queued); for `wait`, the job succeeded |
| 75 | host busy. The job did not run: `--queue` or retry |
| 69 | host unreachable or would not wake. The job did not run |
| 70 | tool or transfer error. If a job id is printed, check `status` before anything else |
| 78 | bad config |
| 124 | `wait --timeout` expired; the job keeps running |
| 130 / 137 | job cancelled / killed by the memory guard |

A zero exit confirms a submitted job or, with `--wait`, a successful job. An error after launch may leave a running job. If an error prints a job id, check its status before retrying. Keep heavy work on the execution host.

## How a job runs

1. **Wake.** `ssh` first. A sleeping Mac with "Wake for network access" on is woken by a TCP knock on its LAN IP, and its own Wi-Fi hardware wakes it. The IP comes from Tailscale's record, or the LAN IP the host last reported about itself (cached on every connection, so a DHCP change doesn't break waking), or `wake_lan_ip`. An optional `wake_lan_host` uses name resolution instead. Every wake is logged to `~/.local/share/remote-run/wake.log`. A `caffeinate` lease then stops it from dropping back to sleep, which it otherwise does after about 45 s.
2. **Busy check.** It exits 75 before uploading anything if the lock is held.
3. **Snapshot.**
   - It takes `git ls-files -co --exclude-standard` as it is on disk, so uncommitted edits are included.
   - Dropped, case-insensitively on every path component:
     - credential-like names (`.env*`, `.envrc`, keys, `*secret*`, `*credential*`, `.mcp.json`, …);
     - agent-state directories (`.git`, `.venv`, `.planning`, `.claude`, `.codex`, …);
     - the project's excludes.
   - `meta/snapshot.json` records HEAD, `git status`, a per-file sha256 manifest and what was excluded.
   - These filters match filenames, not file contents. Inspect the snapshot before sending private work.
   - With `--git`, HEAD's full reachable history ships as an unfiltered bundle, including past versions of files excluded from the snapshot. Use it only with history you intend to send. Files left out of the source tar are marked skip-worktree.
4. **Inputs.** Gitignored data (`data`) and absolute-path inputs (`external`) are rsynced into a per-project cache on the host, under a lock with a heartbeat.
   - Each job gets APFS copy-on-write clones: independent files that initially share data blocks.
   - Absolute-path inputs are placed at their real path only while the job holds the host lock, so a queued job cannot change a running job's inputs.
5. **Run.** A detached tmux session runs `lib/rr_job.py`, so the job survives the laptop sleeping or disconnecting. It:
   - takes the lock;
   - holds `caffeinate` while the job runs, plus a 10-minute grace period for fetching;
   - runs setup in the project dir (`uv sync --frozen` pinned to the laptop's exact Python patch version);
   - runs the command from the same subdirectory it was submitted from;
   - samples the command's process-tree RSS about every 2 s into `mem.tsv`; host swap and free memory refresh every fifth sample, about 10 s;
   - kills the command tree when its RSS exceeds the configured cap, or when configured host swap/free-memory thresholds trip. Setup runs before this sampling loop. macOS RSS leaves out compressed pages, which is why the host itself is watched too;
   - kills leftover background processes when the command ends.

   A wrapper that dies shows up as `lost`. The job keeps the lock, and `cancel` cleans up.
6. **Results.** `fetch` writes outside the working repo by default and refuses an active job unless `--partial` is set. It treats transport failures as errors and skips requested paths that do not exist. An explicit `--dest` can choose another destination. Fetch does not verify a backup or delete remote files. After checking the fetched results, use `clean` to remove finished job directories.

## Configuration

**`hosts.json`** holds the execution hosts: ssh alias, wake targets (`tailscale_ip` to look up the LAN address, pinned `wake_lan_ip`; optional `wake_lan_host`, a name that relies on LAN name resolution), PATH prefix, python, lease and grace seconds, memory cap, host guard thresholds. `REMOTE_RUN_HOST` or `--host` picks one. `REMOTE_RUN_HOSTS_FILE` points at your local host configuration. A different host root has a separate lock, so use one shared root for all clients that submit to the same host.

**Project configs** are needed only when a repo reads gitignored or absolute-path data, needs git while it runs, lives in a subfolder, or has a worker knob to pin.

| key | meaning |
|---|---|
| `data` | gitignored inputs, workdir-relative; cached and cloned into each job |
| `external` | absolute-path inputs, recreated at the same path on the host (same username). Credential and cloud-synced locations are refused |
| `exclude` | repo-root-relative snapshot trims |
| `env` | environment variables, e.g. the project's single-worker setting |
| `fetch` | default result paths (workdir-relative or absolute) |
| `workdir`, `snapshot: "workdir"` | project in a subfolder; ship only that subfolder |
| `git` | ship HEAD's history (the same as `--git`) |
| `setup` | environment command (default `uv sync --frozen`, or a pip venv when there is a `requirements.txt`) |
| `roots` | absolute directories that select this config, for a subfolder project that shares a repo |
| `notes` | rules agents must read, e.g. "pass `--workers 1`" or "never copy the ledger back" |

The config is resolved in this order:
1. `--config`;
2. the nearest `.remote-run.json` from the cwd upward (its directory becomes the workdir);
3. `projects/*.json` whose `roots` contain the cwd;
4. `projects/<repo name>.json`.

`examples/project.json` shows a project with gitignored inputs, one worker and an output directory. Adapt it to your command, then save it as the project's `.remote-run.json` or pass its absolute path with `--config`. Keep private paths and operating rules in a local config outside public repositories.

## Agent integration

- `skill/remote-run/SKILL.md` is the agent-facing procedure: submit, read the exit code, wait, fetch, report. `ONBOARDING.md` is the checklist for adding a repo. `install.sh` puts it in `~/.agents/skills`, which Codex, OMP, Pi and Qwen read, and links it for Claude Code and Hermes.
- Each harness's global instructions and each repo's AGENTS.md carry a short "Heavy runs" policy pointing at the skill. That policy is guidance. The lock is the enforcement.

## Local checks

`tests/compare_json.py` compares local and remote JSON results with tolerances for floats. `tests/alloc_tree.py` is a small process-tree memory fixture. Start with a tiny workload before setting a production memory cap.

`tests/test_regressions.py` checks setup lock ownership after wrapper death, cancellation and JSON comparison using local scratch processes and files. It requires process inspection through `ps` and blocks SSH, rsync, tmux and Tailscale.

## License

This snapshot has no license file.

## Assumptions and limits

- **macOS host.** It uses `caffeinate`, `cp -c` (APFS clones), `memory_pressure` and `pmset`. Linux branches exist (`cp --reflink=auto`, `/proc/meminfo`) but are untested.
- **Same username on both machines**, so that absolute-path inputs keep their paths.
- **Wake-on-demand needs** the same LAN, "Wake for network access" on, and the lid open or an external display. Otherwise keep the host awake. Add your own `wake_lan_ip` or `tailscale_ip` only to the local host configuration.
- **One shared host root, one job at a time.** This is a deliberate gate, not a scheduler.
- **Append-only research ledgers** that a job writes are never merged back automatically. Each project decides its merge rule (see `notes`).

## Layout

`bin/remote-run` (laptop CLI, Python 3.9+ stdlib) · `lib/rr_job.py` (host side, installed on the host automatically when its hash changes and copied into each job) · `hosts.json` · `examples/` · `skill/remote-run/` · `install.sh` · `tests/`.

On the host, `~/remote-run/` holds `host.lock`, `lock-holder.json`, `bin/`, `cache/<project>/` and `jobs/<id>/` (`job.json`, `meta/`, `src/`, `external/`, `setup.log`, `run.log`, `mem.tsv`, `status.json`, `exit_code`).
