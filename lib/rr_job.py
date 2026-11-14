#!/usr/bin/env python3
"""remote-run host-side helper. Runs on the execution host (stdlib only, Python >= 3.9).

The local `remote-run` CLI installs this file as <root>/bin/rr_job.py and copies it
into every job dir. Subcommands:

  run <jobdir> [--queue]      execute one job (called inside a detached tmux session)
  status <jobdir>             JSON status; "lost" when the wrapper died mid-job; exit 3 if no job
  cancel <jobdir>             stop a queued or running job and its whole process tree
  hostinfo <root>             JSON: lock state, active jobs, memory, disk, power
  hold-lock <lockfile>        take an exclusive flock, print LOCKED; release on stdin EOF or
                              when the caller's heartbeat stops for 90 s
  clone-data <src_base> <dst_base> <rel>...   copy-on-write clone of cached inputs
  list <root> [N]             JSON lines: recent job summaries
  clean <root> <days> [id...] remove finished job dirs

Host-wide gate: one exclusive flock on <root>/host.lock (never deleted), held by this
wrapper and inherited by the setup and command processes.
"""
import fcntl
import json
import os
import platform
import resource
import select
import shutil
import signal
import socket
import subprocess
import sys
import time

EX_BUSY = 75
TERMINAL = {"done", "failed", "setup_failed", "cancelled", "killed_mem", "busy", "error", "lost"}
ACTIVE = {"starting", "queued", "setup", "running"}
SAMPLE_S = 2.0
HOST_CHECK_EVERY = 5  # samples between host-level memory checks


class Cancelled(Exception):
    pass


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def sh(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------- process tree

def ps_table():
    rows = {}
    for line in sh(["ps", "-A", "-o", "pid=", "-o", "ppid=", "-o", "rss="]).splitlines():
        p = line.split()
        if len(p) == 3:
            try:
                rows[int(p[0])] = (int(p[1]), int(p[2]))
            except ValueError:
                pass
    return rows


def descendants(root, rows):
    kids = {}
    for pid, (ppid, _) in rows.items():
        kids.setdefault(ppid, []).append(pid)
    out, stack = [], [root]
    while stack:
        p = stack.pop()
        if p in rows:
            out.append(p)
            stack.extend(kids.get(p, []))
    return out


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def kill_tree(root, grace=10.0):
    """SIGTERM the whole descendant tree (steps may sit in their own sessions), then SIGKILL."""
    pids = descendants(root, ps_table())
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + grace
    while time.time() < deadline and any(alive(p) for p in pids):
        time.sleep(0.5)
    for p in pids:
        if alive(p):
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass


def kill_group(pgid, grace=5.0):
    """Kill whatever is left in the job's process group (background leftovers holding the lock)."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.time() + grace
    while time.time() < deadline:
        try:
            os.killpg(pgid, 0)
        except OSError:
            return True
        time.sleep(0.25)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    return True


# ---------------------------------------------------------------- host facts

def host_memory():
    info = {"total_gb": None, "swap_used_gb": None, "free_pct": None}
    if sys.platform == "darwin":
        try:
            info["total_gb"] = round(int(sh(["sysctl", "-n", "hw.memsize"])) / 2**30, 1)
        except ValueError:
            pass
        swap = sh(["sysctl", "-n", "vm.swapusage"])  # total = 2048.00M  used = 512.69M  free = ...
        if "used = " in swap:
            v = swap.split("used = ")[1].split()[0]
            mult = {"M": 1 / 1024, "G": 1.0}.get(v[-1], 1 / 1024)
            info["swap_used_gb"] = round(float(v[:-1]) * mult, 2)
        for line in sh(["memory_pressure"]).splitlines():
            if "free percentage" in line:
                info["free_pct"] = int(line.rsplit(":", 1)[1].strip().rstrip("%"))
    else:
        mi = {}
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    mi[k] = int(v.split()[0])
            info["total_gb"] = round(mi["MemTotal"] / 2**20, 1)
            info["swap_used_gb"] = round((mi["SwapTotal"] - mi["SwapFree"]) / 2**20, 2)
            info["free_pct"] = int(100 * mi["MemAvailable"] / mi["MemTotal"])
            info["available_gb"] = round(mi["MemAvailable"] / 2**20, 1)
        except (OSError, KeyError, ValueError):
            pass
    return info


def power_state():
    if sys.platform != "darwin":
        return {}
    batt = sh(["pmset", "-g", "batt"])
    lid = sh(["ioreg", "-r", "-k", "AppleClamshellState", "-d", "1"])
    return {"on_ac": "AC Power" in batt, "lid_closed": '"AppleClamshellState" = Yes' in lid}


def lock_busy(root):
    """Probe the host lock without keeping it. Returns (busy, holder)."""
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "host.lock"), "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True, read_json(os.path.join(root, "lock-holder.json"))
        fcntl.flock(f, fcntl.LOCK_UN)
    return False, None


def effective_status(jobdir):
    """status.json, with active states whose wrapper is gone reported as 'lost'."""
    if not os.path.exists(os.path.join(jobdir, "job.json")):
        return None
    st = read_json(os.path.join(jobdir, "status.json")) or {"state": "starting"}
    if st.get("state") in ACTIVE and st.get("wrapper_pid") and not alive(st["wrapper_pid"]):
        st = dict(st, state="lost", lost_detail=(
            "wrapper died; job process %s still running" % st.get("child_pid")
            if alive(st.get("child_pid")) else "wrapper died; no job process left"))
    return st


def job_summaries(root):
    jobs_dir = os.path.join(root, "jobs")
    out = []
    try:
        names = os.listdir(jobs_dir)
    except OSError:
        return out
    for name in names:
        jobdir = os.path.join(jobs_dir, name)
        st = effective_status(jobdir)
        if not st:
            continue
        job = read_json(os.path.join(jobdir, "job.json"), {})
        out.append({
            "job_id": name, "project": job.get("project"), "state": st.get("state"),
            "submitted": job.get("submitted"), "elapsed_s": st.get("elapsed_s"),
            "peak_tree_rss_gb": st.get("peak_tree_rss_gb"), "exit_code": st.get("exit_code"),
            "label": job.get("label"), "command": job.get("command"),
        })
    out.sort(key=lambda j: j.get("submitted") or "", reverse=True)
    return out


def cmd_status(jobdir):
    st = effective_status(jobdir)
    if st is None:
        return 3
    print(json.dumps(st, sort_keys=True))
    return 0


def cmd_cancel(jobdir):
    st = effective_status(jobdir)
    if st is None:
        return 3
    if st.get("state") in ACTIVE and alive(st.get("wrapper_pid")):
        os.kill(int(st["wrapper_pid"]), signal.SIGTERM)  # the wrapper kills the tree and records it
        for _ in range(60):
            time.sleep(0.5)
            st = effective_status(jobdir)
            if st.get("state") not in ACTIVE:
                break
    elif st.get("state") == "lost":
        if alive(st.get("child_pid")):
            kill_tree(int(st["child_pid"]))
        raw = read_json(os.path.join(jobdir, "status.json"), {})
        raw.update(state="cancelled", ended=now(), cancel_note="wrapper was already dead")
        write_json(os.path.join(jobdir, "status.json"), raw)
        st = raw
    print(json.dumps(st, sort_keys=True))
    return 0


def cmd_hostinfo(root):
    busy, holder = lock_busy(root)
    stv = os.statvfs(os.path.expanduser("~"))
    print(json.dumps({
        "hostname": socket.gethostname(), "platform": platform.platform(), "cpus": os.cpu_count(),
        "busy": busy, "holder": holder,
        "active_jobs": [j for j in job_summaries(root) if j["state"] in ACTIVE or j["state"] == "lost"],
        "memory": host_memory(), "disk_free_gb": round(stv.f_bavail * stv.f_frsize / 2**30, 1),
        "power": power_state(), "uv": sh(["uv", "--version"]),
    }, sort_keys=True))
    return 0


def cmd_hold_lock(lockfile, idle_s=90):
    os.makedirs(os.path.dirname(lockfile), exist_ok=True)
    f = open(lockfile, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("WAITING", flush=True)
        fcntl.flock(f, fcntl.LOCK_EX)
    print("LOCKED", flush=True)
    # Released on EOF, or when the submitter's heartbeat stops (laptop gone mid-sync).
    while True:
        ready, _, _ = select.select([sys.stdin], [], [], idle_s)
        if not ready or not os.read(sys.stdin.fileno(), 4096):
            return 0


def clone(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.lexists(dst):
        if os.path.isdir(dst) and not os.path.islink(dst):
            shutil.rmtree(dst)
        else:
            os.remove(dst)
    # Copy-on-write clones: real, independent files (symlinks fail some loaders' checks)
    # that cost no space and cannot be changed through another job's writes.
    # -p keeps mtimes, so later rsync quick-checks against the clone skip unchanged files.
    cmd = ["cp", "-Rpc", src, dst] if sys.platform == "darwin" else ["cp", "-Rp", "--reflink=auto", src, dst]
    return subprocess.run(cmd).returncode


def cmd_clone_data(src_base, dst_base, rels):
    for rel in rels:
        src = os.path.join(src_base, rel)
        if not os.path.exists(src):
            print("missing cached input: %s" % rel, file=sys.stderr)
            return 1
        rc = clone(src, os.path.join(dst_base, rel))
        if rc:
            return rc
    return 0


def cmd_list(root, n):
    for j in job_summaries(root)[:n]:
        print(json.dumps(j, sort_keys=True))
    return 0


def cmd_clean(root, days, ids):
    cutoff = time.time() - days * 86400
    removed = 0
    for j in job_summaries(root):
        if (ids and j["job_id"] not in ids) or j["state"] not in TERMINAL or j["state"] == "lost":
            continue
        path = os.path.join(root, "jobs", j["job_id"])
        if not ids and os.path.getmtime(path) > cutoff:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
        print("removed %s" % j["job_id"])
    print("removed %d job dir(s)" % removed)
    return 0


def apply_external(jobdir, paths):
    """Copy this job's staged absolute-path inputs into place. Runs only while the job holds
    the host lock, so a queued submit can never change the inputs of a running job."""
    for p in paths:
        staged = os.path.join(jobdir, "external", p.lstrip("/"))
        if os.path.isdir(staged):
            os.makedirs(p, exist_ok=True)
            cmd = ["rsync", "-a", staged + "/", p + "/"]
        else:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            cmd = ["rsync", "-a", staged, p]
        rc = subprocess.run(cmd).returncode
        if rc:
            raise RuntimeError("could not place external input %s (rsync %d)" % (p, rc))


# ---------------------------------------------------------------- run one job

def cmd_run(jobdir, queue):
    jobdir = os.path.abspath(jobdir)
    job = read_json(os.path.join(jobdir, "job.json"))
    status_path = os.path.join(jobdir, "status.json")
    root = job["remote_root"]
    src = os.path.join(jobdir, "src")
    st = {"job_id": job["job_id"], "state": "starting", "host": socket.gethostname(),
          "wrapper_pid": os.getpid(), "command": job["command"], "updated": now()}
    write_json(status_path, st)

    def update(**kw):
        st.update(kw)
        st["updated"] = now()
        write_json(status_path, st)

    def on_signal(signum, frame):
        raise Cancelled(signal.Signals(signum).name)

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, on_signal)

    child = None
    t0 = None
    peak = {"kb": 0}
    try:
        lockf = open(os.path.join(root, "host.lock"), "a+")
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = read_json(os.path.join(root, "lock-holder.json"))
            if not queue:
                update(state="busy", holder=holder, ended=now())
                return EX_BUSY
            update(state="queued", holder=holder)
            fcntl.flock(lockf, fcntl.LOCK_EX)
        write_json(os.path.join(root, "lock-holder.json"), {
            "job_id": job["job_id"], "project": job.get("project"), "label": job.get("label"),
            "pid": os.getpid(), "since": now(), "command": job["command"]})
        has_caffeinate = bool(shutil.which("caffeinate"))
        if has_caffeinate:
            # Keeps the host out of idle sleep for exactly as long as this wrapper lives.
            subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)

        env = dict(os.environ)
        env.update(job.get("env", {}))
        env["PATH"] = job.get("path_prefix", "") + ":" + env.get("PATH", "/usr/bin:/bin")
        mem_before = host_memory()
        update(state="setup", lock_acquired=now(), mem_before=mem_before)
        apply_external(jobdir, job.get("external", []))

        os.set_inheritable(lockf.fileno(), True)
        workdir = os.path.join(src, job.get("workdir") or "")
        if job.get("setup"):
            with open(os.path.join(jobdir, "setup.log"), "ab") as lf:
                child = subprocess.Popen(["/bin/bash", "-c", job["setup"]], cwd=workdir, env=env,
                                         stdin=subprocess.DEVNULL, stdout=lf,
                                         stderr=subprocess.STDOUT, start_new_session=True,
                                         pass_fds=(lockf.fileno(),))
            update(child_pid=child.pid)
            rc = child.wait()
            child = None
            update(child_pid=None)
            if rc:
                update(state="setup_failed", exit_code=rc, ended=now())
                return rc

        # The job's main process inherits the lock fd, so the host stays locked while any
        # part of that process lives even if this wrapper dies.
        t0 = time.time()
        update(state="running", started=now())
        with open(os.path.join(jobdir, "run.log"), "ab") as lf:
            child = subprocess.Popen(["/bin/bash", "-c", job["command"]],
                                     cwd=os.path.join(src, job.get("rel_cwd") or job.get("workdir") or ""),
                                     env=env, stdin=subprocess.DEVNULL, stdout=lf,
                                     stderr=subprocess.STDOUT, start_new_session=True,
                                     pass_fds=(lockf.fileno(),))
        update(child_pid=child.pid)
        if has_caffeinate:
            # A second assertion tied to the job process itself, which outlives a dead wrapper.
            subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(child.pid)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)

        limit_kb = int(float(job.get("mem_limit_gb") or 0) * 2**20)
        swap_kill = float(job.get("swap_growth_kill_gb") or 0)
        min_free = int(job.get("min_free_pct") or 0)
        swap_gate = int(job.get("swap_growth_free_pct") or 25)
        swap0 = mem_before.get("swap_used_gb") or 0.0
        peak_n, last_update, n, kill_reason = 0, 0.0, 0, None
        with open(os.path.join(jobdir, "mem.tsv"), "w") as mem:
            mem.write("t_s\ttree_rss_mb\tnprocs\tlargest_proc_rss_mb\thost_swap_gb\thost_free_pct\n")
            hm = mem_before
            while child.poll() is None:
                rows = ps_table()
                pids = descendants(child.pid, rows)
                rss = [rows[p][1] for p in pids]
                tree_kb = sum(rss)
                peak["kb"] = max(peak["kb"], tree_kb)
                peak_n = max(peak_n, len(pids))
                n += 1
                if n % HOST_CHECK_EVERY == 0:
                    hm = host_memory()
                t = time.time() - t0
                mem.write("%.0f\t%d\t%d\t%d\t%s\t%s\n" % (t, tree_kb // 1024, len(pids),
                          max(rss or [0]) // 1024, hm.get("swap_used_gb"), hm.get("free_pct")))
                mem.flush()
                # ps RSS leaves out compressed/swapped pages, so also watch the host itself.
                if limit_kb and tree_kb > limit_kb:
                    kill_reason = "tree RSS %.1f GB > cap %.1f GB" % (tree_kb / 2**20, limit_kb / 2**20)
                elif (swap_kill and (hm.get("swap_used_gb") or 0) - swap0 > swap_kill
                      and hm.get("free_pct") is not None and hm["free_pct"] < swap_gate):
                    # macOS grows swap even with memory to spare; only act when it is also scarce.
                    kill_reason = "host swap grew %.1f GB during the job with only %d%% memory free" % (
                        (hm.get("swap_used_gb") or 0) - swap0, hm["free_pct"])
                elif min_free and hm.get("free_pct") is not None and hm["free_pct"] < min_free:
                    kill_reason = "host memory free %d%% < %d%%" % (hm["free_pct"], min_free)
                if kill_reason:
                    kill_tree(child.pid)
                    break
                if time.time() - last_update >= 15:
                    last_update = time.time()
                    update(elapsed_s=round(t, 1), tree_rss_gb=round(tree_kb / 2**20, 2),
                           peak_tree_rss_gb=round(peak["kb"] / 2**20, 2), nprocs=len(pids))
                time.sleep(SAMPLE_S)
        rc = child.wait()
        orphans = kill_group(child.pid)
        largest = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        largest_gb = largest / 2**30 if sys.platform == "darwin" else largest / 2**20
        state = "killed_mem" if kill_reason else ("done" if rc == 0 else "failed")
        update(state=state, exit_code=rc, ended=now(), elapsed_s=round(time.time() - t0, 1),
               peak_tree_rss_gb=round(peak["kb"] / 2**20, 2), peak_nprocs=peak_n,
               largest_single_proc_rss_gb=round(largest_gb, 2), mem_after=host_memory(),
               kill_reason=kill_reason, leftover_processes_killed=orphans,
               tree_rss_gb=None, nprocs=None)
        return rc
    except Cancelled as e:
        if child is not None and child.poll() is None:
            kill_tree(child.pid)
        if child is not None:
            kill_group(child.pid)
        update(state="cancelled", ended=now(), signal=str(e),
               elapsed_s=round(time.time() - t0, 1) if t0 else None,
               peak_tree_rss_gb=round(peak["kb"] / 2**20, 2), tree_rss_gb=None, nprocs=None)
        return 130
    except Exception as e:  # noqa: BLE001 - record any wrapper failure in status.json
        if child is not None and child.poll() is None:
            kill_tree(child.pid)
        update(state="error", ended=now(), error="%s: %s" % (type(e).__name__, e))
        raise
    finally:
        with open(os.path.join(jobdir, "exit_code"), "w") as f:
            f.write("%s\n" % st.get("exit_code", ""))
        if st.get("lock_acquired"):
            try:
                os.remove(os.path.join(root, "lock-holder.json"))
            except OSError:
                pass
            grace = int(job.get("grace_seconds") or 0)
            if grace and shutil.which("caffeinate"):
                # Short awake window so the submitter can fetch results before the host sleeps.
                subprocess.Popen(["caffeinate", "-i", "-s", "-t", str(grace)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)


def main(argv):
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, args = argv[1], argv[2:]
    table = {
        "run": lambda: cmd_run(args[0], "--queue" in args[1:]),
        "status": lambda: cmd_status(args[0]),
        "cancel": lambda: cmd_cancel(args[0]),
        "hostinfo": lambda: cmd_hostinfo(args[0]),
        "hold-lock": lambda: cmd_hold_lock(args[0]),
        "clone-data": lambda: cmd_clone_data(args[0], args[1], args[2:]),
        "list": lambda: cmd_list(args[0], int(args[1]) if len(args) > 1 else 20),
        "clean": lambda: cmd_clean(args[0], float(args[1]), args[2:]),
    }
    if cmd not in table:
        print("unknown subcommand %r" % cmd, file=sys.stderr)
        return 2
    return table[cmd]()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
