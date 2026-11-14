# Onboard a repo for remote-run

Done means three things:
- one small, seeded run of this repo produced matching results locally and on the host, from the same snapshot;
- the config's `notes` record the repo's rules;
- the repo's AGENTS.md carries a `## Heavy runs` section, if its owner agrees.

Adapt `examples/project.json` from the remote-run checkout, or keep a project-specific config outside the repository and pass its absolute path with `--config`.

1. **Find the heavy entry point and its worker knob.** Read the launch commands (lane scripts, Makefile, README, RESTART.md) and grep for `ProcessPoolExecutor`, `multiprocessing`, `Pool(`, `joblib`, `--workers`, `*_WORKERS`. Note the default worker count on a 10-core machine and any pool that ignores the knob.
   - If the knob is an env var, pin the single-worker value in `env`.
   - If it is a flag, say in `notes` that commands must pass it.
2. **Find what a run reads that git will not ship.** The snapshot is `git ls-files -co --exclude-standard`. List each such input:
   - `data`: gitignored paths relative to the workdir. They are rsynced once into the host cache and cloned (copy-on-write, real files) into each job.
   - `external`: absolute paths the code opens by absolute path (a hard-coded `~/...`, `Path.home()`). They are recreated at the same path on the host (same username on both machines) when the job starts. List files or small directories, never a huge cache. Credential and cloud-synced locations are refused.
   - Data behind an env var (such as `FOO_DATA=~/Data/foo`): list the files under `external` and set the env var to a directory inside the job. The `setup` step links that directory's input part to the external path, so outputs stay inside the job and come back with `fetch` (for example, link an input directory during setup).
   Leave out raw downloads, HTTP caches, sealed or holdout data, and anything credential-like. Licensed data goes only where its licence allows.
3. **Check what the run needs from git and where it writes.**
   - `git ... check=True`, a commit recorded in results, or `merge-base`: set `"git": true`.
   - Outputs written outside the repo: plan to fetch those absolute paths.
   - Append-only ledgers or sequence numbers: they must never be copied back blindly. Write the merge rule, or "coordinator decides", into `notes`.
4. **Write the config** at the project's `.remote-run.json` or a local file passed with `--config`.
   - Keys: `project` (lowercase), `data`, `external`, `exclude` (repo-root-relative snapshot trims for private or bulky files), `env`, `fetch` (workdir-relative or absolute), `notes`.
   - Optional keys:
     - `workdir`: the subfolder holding `pyproject.toml`/`uv.lock`;
     - `snapshot: "workdir"`: ship only that subfolder;
     - `roots`: absolute dirs that select this config, needed when a subfolder project shares a repo;
     - `git`, `setup` (default `uv sync --frozen`), `mem_limit_gb`, `max_snapshot_mb` (default 500).
   - A repo can instead carry `.remote-run.json`, whose directory becomes the workdir.
5. **Dry-run:** `remote-run submit --dry-run -- <command>` prints the config, workdir, cwd, file count, size, excluded paths, env and inputs. Inspect the included files as well as the excluded paths. Filename filters do not inspect contents. If using `--git`, inspect HEAD's reachable history too, because that bundle is unfiltered.
6. **Validate one small, seeded run** from a single snapshot:
   ```sh
   remote-run snapshot -o /tmp/rr-val.tgz --extract /tmp/rr-val      # same bytes for both sides
   (cd /tmp/rr-val/src/<workdir> && uv sync --frozen && <small command>)   # local reference
   remote-run submit --snapshot /tmp/rr-val.tgz --wait -- <small command>
   remote-run fetch <job>
   ```
   Compare the outputs:
   - Byte-identical artifacts pass by sha256.
   - Floating-point results that go through BLAS can differ in the last bits across CPUs or macOS versions. Compare those with `python3 /path/to/remote-run/tests/compare_json.py LOCAL REMOTE` (`rtol=1e-9`).
   - Counts, hashes and booleans must match exactly.
   - A run that failed the same way on both sides is not a match, so check its own status fields first.
7. **Add the policy** to the repo's AGENTS.md as a short `## Heavy runs` section: which commands are heavy, the `remote-run submit` form for each, and how results come back. Another agent may own that file, so coordinate first.
