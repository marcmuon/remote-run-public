---
name: remote-run
description: Offload heavy runs (simulations, sweeps, backtests, multi-worker or memory-hungry Python) from this machine to a configured SSH host with the remote-run CLI. Use before launching a heavy run, when remote-run exits non-zero (75 BUSY, 69 UNAVAILABLE, 70), to check or fetch a remote job, or to onboard a new repo for offload.
---

# remote-run

Heavy runs execute on the configured SSH host. This machine keeps the agents, editors and repos. Configure the host as described in the repository README before submitting. `remote-run` ships a snapshot of the current checkout (uncommitted changes included, filename exclusions applied), rebuilds the environment there, runs the job under one host-wide lock shared by every agent, and records logs, exit status, elapsed time and peak memory.

A **heavy run** is a full sweep or wave, anything with more than one worker process, a job expected to pass ~2 GB RAM or ~5 minutes, or a test suite that takes minutes. Small checks stay local: targeted tests, a seconds-long smoke on a tiny sample.

## Run a heavy job

1. `cd` to where you would run the command locally, then submit the exact command:
   `remote-run submit --label "<what this is>" -- <command...>`
   It prints the job id on stdout. The job runs in the same subdirectory you submitted from. The project config (`remote-run submit --dry-run -- <command>` shows which one) sets inputs, the worker count, git history and result paths. Read the config's `notes`: they carry repo-specific rules such as a `--workers 1` flag or a ledger that must not be copied back. Change the worker count only deliberately, with `--env K=V` or the command's own flag.
2. Read the exit code. A **0** exit confirms submission or, with `--wait`, success. An error after launch can leave a running job.
   - **75 BUSY**: another job holds the host. Re-submit with `--queue` to wait in line on the host, or submit later.
   - **69 UNAVAILABLE**: the host is asleep away from the home LAN, or offline. Submit later, or report the unavailable host.
   - **70**: a tool or transfer error. If it names a job id, run `remote-run status <job>` before doing anything else, because the job may have started.
   If an error names a job id, check its status before retrying. Keep it remote: queue it, retry, or report it. Running it on this laptop instead is exactly what the lock exists to stop.
3. `remote-run wait <job>` blocks until the job ends and exits with the job's exit code (137 means killed by the memory guard, 130 cancelled). Run it in the background for long jobs. It prints the final state, `peak_tree_rss` (parent plus every child process) and elapsed time.
4. `remote-run fetch <job>` copies the logs and the configured result paths to `~/.local/share/remote-run/results/<project>/<job>/` and prints that path. Extra paths go after the job id: workdir-relative, or absolute host paths for outputs written outside the repo (for example `/Users/you/Data/simulation/results/<run_id>`). Read results there, and move artifacts into the repo only as its AGENTS.md allows. Fetch leaves remote files in place. After checking your results, use `clean` to remove finished job directories.
5. Report the job id, host, command, elapsed time, peak tree RSS and exit code (`remote-run status <job>`).

## Other commands

`remote-run host` shows the lock holder, queued and running jobs, memory, disk and power. `logs <job> [-n N] [--setup]`, `status <job> [--json]`, `cancel <job>` (kills the whole process tree), `list`, `clean --days N`. `remote-run --help` lists every flag.

## Gotchas

- Snapshot exclusions match filenames, not contents. Inspect the snapshot before sending private work.
- If the host sleeps between jobs, configure its wake address locally. `remote-run` tries to wake it over the LAN, with a timeout set in the host config. A job keeps it awake while it runs and for 10 minutes after, so fetch promptly. Staying awake needs AC power, plus the lid open or an external display attached.
- Jobs run in a plain copy of the checkout.
  - Code that runs git while it works (recording the commit, `merge-base`): pass `--git`, or set `"git": true` in the config, to ship HEAD's history. That history is unfiltered, including past versions of files excluded from the source tar.
  - Gitignored inputs reach the host only if the config lists them: `data` (workdir-relative, cloned into each job) or `external` (absolute paths, recreated at the same path when the job starts). `--data` and `--external` add to those lists.
- One job holds the host at a time. The memory guard samples the command tree about every 2 seconds and host swap/free memory about every 10 seconds. It stops the command when configured RSS, swap or free-memory limits trip. Setup runs before the sampling loop. Start with the project's single-worker setting, and raise workers only after a measured run shows headroom.
- A sandboxed Codex session needs network for ssh (`-c sandbox_workspace_write.network_access=true`). Fetch with `--dest "$TMPDIR/rr/<job>"`, never inside the repo.
- The first submit for a project syncs its data (GBs) once. Later submits send only changed files.

## Onboard a new repo

Before the first remote run in a repo with no project config (the dry-run says `config NONE`), follow [ONBOARDING.md](ONBOARDING.md).
