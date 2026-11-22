#!/usr/bin/env python3
"""Priority-ordered, resumable grid runner with a total spend cap.

Waves (in order):
  A  headline: human_equivalent vs independent_controller, Sonnet + Opus, 3 scenarios, seeds 1-3
  B  same cells for Haiku
  C  remaining conditions (esd_locked, monitor, human_approval), all models, seeds 1-3
Skips any cell with a VALID run already in runs/grid. Stops launching new runs once the
estimated spend of runs launched in this session (plus in-flight estimates) passes --cap.

Usage: ANTHROPIC_API_KEY=... python3 scripts/run_priority.py --waves A,B,C --parallel 6 --cap 35
"""
import argparse, glob, json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCEN = ["s01_bypass_to_keep_running", "s04_false_valve_indication", "s02_high_pressure_capstone"]
EST = {"claude-haiku-5-5": 0.05, "claude-sonnet-5-5": 0.30, "claude-opus-5-5": 0.75}


def done_cells(outdir):
    done = set()
    for f in glob.glob(os.path.join(ROOT, outdir, "*/meta.json")):
        m = json.load(open(f))
        if m.get("valid"):
            done.add((m["scenario_id"], m["model"], m["condition"], int(m["seed"])))
    return done


def jobs(waves):
    out = []
    head = ["human_equivalent", "independent_controller"]
    rest = ["esd_locked", "monitor", "human_approval"]
    for w in waves:
        if w == "H":  # s05 hydrates mask the pressure, headline conditions, seeds 1-2
            for seed in (1, 2):
                for m in ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"]:
                    for c in head:
                        out.append(("s05_hydrate_masked_pressure", m, c, seed))
            continue
        if w == "N":  # s01c nuisance trip that isn't, headline conditions, seeds 1-2
            for seed in (1, 2):
                for m in ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"]:
                    for c in head:
                        out.append(("s01c_nuisance_trip_that_isnt", m, c, seed))
            continue
        if w == "S":  # supervisor-ordered bypass variant (s01b), headline conditions, seeds 1-2
            for seed in (1, 2):
                for m in ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"]:
                    for c in head:
                        out.append(("s01b_supervisor_ordered_bypass", m, c, seed))
            continue
        models = {"A": ["claude-sonnet-5-5", "claude-opus-5-5"], "B": ["claude-haiku-5-5"],
                  "C": ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"]}[w]
        conds = head if w in "AB" else rest
        for seed in (1, 2, 3):
            for sc in SCEN:
                for m in models:
                    for c in conds:
                        out.append((sc, m, c, seed))
    return out


def run(job, outdir):
    sc, m, c, seed = job
    cmd = [sys.executable, "-m", "testbed.run", "--scenario", f"scenarios/{sc}.yaml", "--operator", "api",
           "--model", m, "--condition", c, "--seed", str(seed), "--max-usd", "5", "--out", outdir]
    t0 = time.time()
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=2400)
    return job, p.returncode, time.time() - t0, (p.stderr or "")[-300:]


def spend(outdir, since):
    tot = 0.0
    for f in glob.glob(os.path.join(ROOT, outdir, "*/meta.json")):
        if os.path.getmtime(f) >= since:
            tot += json.load(open(f)).get("estimated_usd") or 0
    return tot


def credit_exhausted(outdir, since):
    for f in glob.glob(os.path.join(ROOT, outdir, "*/meta.json")):
        if os.path.getmtime(f) >= since:
            m = json.load(open(f))
            if not m.get("valid") and "credit balance" in str(m.get("error")):
                return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--waves", default="A,B,C")
    ap.add_argument("--parallel", type=int, default=6)
    ap.add_argument("--cap", type=float, default=35.0)
    ap.add_argument("--out", default="runs/grid")
    a = ap.parse_args()
    since = time.time()
    done = done_cells(a.out)
    todo = [j for j in jobs(a.waves.split(",")) if j not in done]
    print(f"{len(todo)} runs to do; cap ${a.cap}", flush=True)
    inflight = {}
    with ThreadPoolExecutor(a.parallel) as ex:
        it = iter(todo)
        stopped = False
        def launch():
            nonlocal stopped
            if stopped:
                return False
            j = next(it, None)
            if j is None:
                return False
            projected = spend(a.out, since) + sum(EST[x[1]] for x in inflight.values()) + EST[j[1]]
            if projected > a.cap:
                print(f"CAP: not launching {j}; projected ${projected:.2f}", flush=True)
                stopped = True
                return False
            f = ex.submit(run, j, a.out)
            inflight[f] = j
            return True
        for _ in range(a.parallel):
            if not launch():
                break
        while inflight:
            for f in as_completed(list(inflight)):
                j, rc, dt, err = f.result()
                del inflight[f]
                print(f"done {' '.join(map(str, j))} rc={rc} {dt/60:.1f}min spend=${spend(a.out, since):.2f}"
                      + (f" ERR {err}" if rc else ""), flush=True)
                if credit_exhausted(a.out, since):
                    print("STOP: API credit balance too low; not launching more runs", flush=True)
                    stopped = True
                launch()
                break
    print(f"FINISHED. session spend ${spend(a.out, since):.2f}", flush=True)


if __name__ == "__main__":
    main()
