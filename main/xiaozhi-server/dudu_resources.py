"""Bounded, process-tree resource sampling. CPU 100% means one logical core."""
import math
import os
import platform
import subprocess
import threading
import time
from collections import OrderedDict, deque

import psutil
try:
    import resource
except ImportError:
    resource = None


def cpu_model():
    if platform.system() == "Darwin":
        try:
            return subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"],
                                           text=True, timeout=2).strip()
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        with open("/proc/cpuinfo") as source:
            for line in source:
                if line.startswith(("model name", "Hardware")):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def summarize(samples):
    samples = list(samples)
    if not samples:
        return {"samples": 0, "duration_seconds": 0}
    result = {"samples": len(samples), "duration_seconds": round(
        samples[-1]["timestamp"] - samples[0]["timestamp"], 3)}
    for field in ("rss_bytes", "parent_rss_bytes", "children_rss_bytes",
                  "cpu_core_percent", "host_cpu_percent", "host_available_bytes",
                  "host_swap_used_bytes", "threads", "children", "event_loop_lag_ms"):
        values = [s[field] for s in samples if s.get(field) is not None]
        if not values:
            continue
        ordered = sorted(values)
        average = sum(values) / len(values)
        if field == "cpu_core_percent":
            valid = [s for s in samples if s.get(field) is not None]
            elapsed = sum(s["interval_seconds"] for s in valid)
            if elapsed:
                average = sum(s[field] * s["interval_seconds"] for s in valid) / elapsed
        result[field] = {"min": round(min(values), 2), "mean": round(average, 2),
                         "p95": round(ordered[math.ceil(.95 * len(ordered)) - 1], 2),
                         "max": round(max(values), 2)}
    return result


class ResourceMonitor:
    def __init__(self, pid=None, interval=.5, retention=7200):
        self.process = psutil.Process(pid or os.getpid())
        self.interval = interval
        self.samples = deque(maxlen=retention)
        self.active = {}
        self.completed = OrderedDict()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.started_at = time.time()
        self.last_time = None
        self.last_cpu = None
        self.fallback_cpus = {}
        self.event_loop_lag_ms = None
        self.own_process = self.process.pid == os.getpid()
        self.reaped_mode = ("getrusage" if self.own_process and resource is not None else
                            "psutil" if platform.system() == "Linux" else "observed_children_only")
        vm = psutil.virtual_memory()
        self.host = {"os": platform.system(), "architecture": platform.machine(),
                     "cpu_model": cpu_model(),
                     "logical_cpus": psutil.cpu_count(),
                     "physical_cpus": psutil.cpu_count(logical=False),
                     "memory_total_bytes": vm.total,
                     "cpu_percent_convention": "100% = one logical core; may exceed 100%",
                     "rss_scope": "sum of parent and live descendants; shared pages may be counted more than once",
                     "sample_interval_seconds": interval,
                     "retention_samples": retention}
        self.host["child_cpu_accounting"] = self.reaped_mode

    def start(self):
        self.sample()
        self.thread = threading.Thread(target=self._run, name="dudu-resources", daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)

    def _run(self):
        psutil.cpu_percent(interval=None)
        while not self.stop_event.wait(self.interval):
            try:
                self.sample()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return

    def sample(self):
        now = time.monotonic()
        root_times = self.process.cpu_times()
        reaped = self.reaped_mode != "observed_children_only"
        total_cpu = root_times.user + root_times.system
        if self.reaped_mode == "getrusage":
            # psutil exposes children_user/system as zero placeholders on macOS.
            usage = resource.getrusage(resource.RUSAGE_CHILDREN)
            total_cpu += usage.ru_utime + usage.ru_stime
        elif reaped:
            total_cpu += root_times.children_user + root_times.children_system
        processes = [self.process]
        try:
            processes.extend(self.process.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        parent_rss = children_rss = threads = children = 0
        for process in processes:
            try:
                with process.oneshot():
                    rss = process.memory_info().rss
                    threads += process.num_threads()
                    if process.pid == self.process.pid:
                        parent_rss = rss
                    else:
                        children += 1
                        children_rss += rss
                        times = process.cpu_times()
                        if reaped:
                            total_cpu += times.user + times.system
                            total_cpu += getattr(times, "children_user", 0) + getattr(times, "children_system", 0)
                        else:
                            identity = (process.pid, process.create_time())
                            self.fallback_cpus[identity] = times.user + times.system
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if not reaped:
            total_cpu += sum(self.fallback_cpus.values())
        # Reading a process tree isn't atomic: a child may be reaped mid-sample.
        # A high-water mark prevents counting the same child CPU twice next time.
        effective_cpu = max(total_cpu, self.last_cpu or 0)
        elapsed = now - self.last_time if self.last_time is not None else 0
        cpu_percent = ((effective_cpu - self.last_cpu) / elapsed * 100
                       if self.last_cpu is not None and elapsed else None)
        self.last_cpu, self.last_time = effective_cpu, now
        memory = psutil.virtual_memory()
        sample = {"timestamp": time.time(), "interval_seconds": elapsed,
                  "rss_bytes": parent_rss + children_rss,
                  "parent_rss_bytes": parent_rss, "children_rss_bytes": children_rss,
                  "cpu_core_percent": round(cpu_percent, 2) if cpu_percent is not None else None,
                  "host_cpu_percent": psutil.cpu_percent(interval=None) if elapsed else None,
                  "host_available_bytes": memory.available,
                  "host_swap_used_bytes": psutil.swap_memory().used,
                  "threads": threads, "children": children,
                  "event_loop_lag_ms": self.event_loop_lag_ms}
        if hasattr(os, "getloadavg"):
            sample["host_load_average"] = list(os.getloadavg())
        with self.lock:
            sample["active_calls"] = sum(v["kind"] == "call" for v in self.active.values())
            sample["active_jobs"] = len(self.active)
            self.samples.append(sample)
        return sample

    def begin(self, session_id, kind="call"):
        with self.lock:
            self.active[session_id] = {"id": session_id, "kind": kind,
                                       "started_at": time.time(), "ended_at": None}

    def end(self, session_id):
        with self.lock:
            session = self.active.pop(session_id, None)
            if session:
                session["ended_at"] = time.time()
                session["summary"] = summarize(self._session_samples(session))
                self.completed[session_id] = session
                while len(self.completed) > 20:
                    self.completed.popitem(last=False)

    def _session_samples(self, session):
        return [s for s in self.samples if session["started_at"] <= s["timestamp"]
                and (session["ended_at"] is None or s["timestamp"] <= session["ended_at"])]

    def snapshot(self, session_id=None, include_samples=False):
        with self.lock:
            rows = list(self.samples)
            recent = [s for s in rows if s["timestamp"] >= time.time() - 60]
            result = {"host": self.host, "current": rows[-1] if rows else None,
                      "recent": summarize(recent),
                      "idle": summarize([s for s in recent if s["active_jobs"] == 0]),
                      "window": summarize(rows), "session": None}
            if session_id:
                session = self.active.get(session_id) or self.completed.get(session_id)
                if session:
                    selected = self._session_samples(session)
                    result["session"] = dict(session)
                    result["session"]["summary"] = session.get("summary") or summarize(selected)
                    if include_samples:
                        result["session"]["samples"] = selected
            if include_samples:
                result["samples"] = rows
            return result
