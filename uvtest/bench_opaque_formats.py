from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import time
from pathlib import Path

from PIL import Image
import compress_dir as cd


def worker(task):
    started = time.perf_counter()
    if task.get("_trace"):
        row = {"name": task["name"], "seq": task["seq"], "pid": os.getpid(), "started": started}
        try:
            row.update(cd._encode_task(task)["row"])
        except Exception as exc:
            row.update({"action": "failed", "error": str(exc)})
    else:
        try:
            row = cd._encode_task(task)["row"]
        except Exception as exc:
            row = {"name": task["name"], "action": "failed", "error": str(exc)}
    row["task_s"] = time.perf_counter() - started
    return row


def _stats(values):
    ordered = sorted(values)
    if not ordered:
        return None
    return {"n": len(ordered),
            "mean_ms": round(1000 * sum(ordered) / len(ordered), 1),
            "median_ms": round(1000 * ordered[len(ordered) // 2], 1),
            "p95_ms": round(1000 * ordered[min(len(ordered) - 1, int(len(ordered) * .95))], 1),
            "min_ms": round(1000 * ordered[0], 1),
            "max_ms": round(1000 * ordered[-1], 1)}


def main():
    started = time.perf_counter()
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixed-cq", action="store_true")
    parser.add_argument("--source-report", type=Path)
    parser.add_argument("--same-size", type=int, metavar="TASKS_PER_WORKER",
                        help="plan A: every worker encodes the most common size back to back "
                             "(all tasks after the first per worker are context hits)")
    parser.add_argument("--same-size-wh", metavar="WxH",
                        help="size to use with --same-size (default: size with most source files)")
    parser.add_argument("--alternate-wh", metavar="WxH",
                        help="plan B: alternate with this size so every task after the first "
                             "evicts and reopens the context (all misses)")
    args = parser.parse_args()
    previous = json.loads(args.source_report.read_text(encoding="utf-8")) if args.source_report else None
    output = Path("uvtest/out") / time.strftime("opaque_bench_%Y%m%d_%H%M%S")
    output.mkdir(parents=True, exist_ok=False)
    sources = previous["sources"] if previous else []
    excluded = previous["excluded"] if previous else []
    for path in ([] if previous else sorted(Path("test_imgs").rglob("*"))):
        if path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
            continue
        try:
            with Image.open(path) as image:
                w, h = image.size
                reason = ("animated" if getattr(image, "is_animated", False) else
                          "alpha_header" if "A" in image.getbands() or "transparency" in image.info else
                          "dimensions" if w < 130 or h < 66 or max(w, h) > 8192 else None)
            if reason:
                excluded.append({"name": str(path), "reason": reason})
                continue
            sources.append({"path": str(path), "probe": cd.probe_cost(path), "mp": w*h/1e6})
        except Exception as exc:
            excluded.append({"name": str(path), "reason": str(exc)})
    # Mirror the production submission order (compress_dir.py) so context hits
    # in this benchmark match what the batch actually gets.
    sources.sort(key=lambda x: (x["probe"]["cost"], x["probe"]["width"], x["probe"]["height"]), reverse=True)
    workers = previous["workers"] if previous else cd.default_workers()
    os.environ["NVAVIF_CTX_CACHE"] = str(max(1, cd.NVENC_SESSION_LIMIT // workers))
    report = {"workers": workers, "sources": sources, "excluded": excluded,
              "source_report": str(args.source_report) if args.source_report else None,
              "settings": {"avif": "GPU P7 fixed CQ20" if args.fixed_cq else "GPU P7 auto quality 88", "webp": "Q90 method 2 lossy",
                           "keep_smaller": False, "round_budget_s": 90}, "rounds": []}
    manifest_s = time.perf_counter() - started
    report["scan_s"] = manifest_s
    print(f"OUTPUT {output}; images={len(sources)} workers={workers} scan={manifest_s:.3f}s", flush=True)
    if args.same_size:
        groups = {}
        for item in sources:
            groups.setdefault((item["probe"]["width"], item["probe"]["height"]), []).append(item)
        (sw, sh), group = max(groups.items(), key=lambda kv: (len(kv[1]), kv[0][0] * kv[0][1]))
        if args.same_size_wh:
            sw, sh = (int(v) for v in args.same_size_wh.lower().split("x"))
            group = groups[(sw, sh)]
        alt = None
        if args.alternate_wh:
            alt = groups[tuple(int(v) for v in args.alternate_wh.lower().split("x"))]
        group.sort(key=lambda s: s["path"])
        dest = output / "same_size"
        dest.mkdir()
        tasks = []
        for index in range(workers * args.same_size):
            source_group = group if alt is None or index % 2 == 0 else alt
            item = source_group[index % len(source_group)]
            path = Path(item["path"])
            tasks.append({"path": str(path), "name": path.name,
                          "out_path": str(dest / f"{index:03d}.avif"), "in_place": False,
                          "device": "gpu", "preset": 7, "cq": 20, "auto_quality": None,
                          "keep_smaller": False, "copy_skipped": False,
                          "transparent_format": "webp", "webp_quality": 90,
                          "webp_method": 2, "webp_lossless_max_mb": 1,
                          "opaque_format": "avif", "opaque_webp_quality": 90,
                          "oversize_format": "webp", "oversize_webp_quality": 80,
                          "oversize_webp_method": 2, "oversize_max_edge": 16383,
                          "_trace": True, "seq": index, **item["probe"]})
        rows = []
        begin = time.perf_counter()
        pool = mp.get_context("spawn").Pool(workers)
        try:
            results = pool.imap_unordered(worker, tasks, chunksize=1)
            with (dest / "rows.jsonl").open("w", encoding="utf-8", buffering=1) as stream:
                for _ in tasks:
                    remaining = 180 - (time.perf_counter() - begin)
                    if remaining <= 0:
                        raise mp.TimeoutError
                    row = results.next(timeout=remaining)
                    rows.append(row)
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            pool.close()
        except mp.TimeoutError:
            pool.terminate()
            print("Same-size round budget exhausted.", flush=True)
        finally:
            pool.join()
        wall = time.perf_counter() - begin
        by_worker = {}
        for row in rows:
            by_worker.setdefault(row.get("pid"), []).append(row)
        per_worker = {}
        cold, switched, hits = [], [], []
        for pid, worker_rows in by_worker.items():
            worker_rows.sort(key=lambda r: r["started"])
            per_worker[str(pid)] = [round(r["task_s"] * 1000) for r in worker_rows]
            previous = None
            for row in worker_rows:
                # With --alternate-wh the size is encoded by seq parity, so a
                # same-parity predecessor was a hit; otherwise every worker
                # task after the first is a hit.
                size = row["seq"] % 2 if alt is not None else 0
                if previous is None:
                    cold.append(row["task_s"])
                elif size == previous:
                    hits.append(row["task_s"])
                else:
                    switched.append(row["task_s"])
                previous = size
        summary = {"mode": "same-size", "size": f"{sw}x{sh}", "files": [s["path"] for s in group],
                   "workers": workers, "ctx_cache": os.environ.get("NVAVIF_CTX_CACHE"),
                   "tasks": len(rows), "wall_s": round(wall, 3),
                   "actions": sorted({r.get("action") for r in rows}),
                   "modes": sorted({r.get("mode") for r in rows}),
                   "cold": _stats(cold), "hit": _stats(hits),
                   "switch": _stats(switched) if alt is not None else None,
                   "per_worker_ms": per_worker,
                   "seq_task_ms": {r["seq"]: round(r["task_s"] * 1000) for r in sorted(rows, key=lambda r: r["seq"])}}
        report["rounds"].append(summary)
        report["elapsed_s"] = time.perf_counter() - started
        (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in summary.items() if k != "seq_task_ms"}, ensure_ascii=False), flush=True)
        print(f"TOTAL {time.perf_counter()-started:.3f}s; REPORT {output / 'results.json'}", flush=True)
        return
    # Interleave the two formats so machine drift cancels; --fixed-cq only
    # changes the AVIF round's quality mode (fixed CQ20 vs auto quality 88).
    formats = ("avif", "webp", "webp", "avif")
    for number, fmt in enumerate(formats, 1):
        dest = output / f"{number}_{fmt}"
        dest.mkdir()
        tasks = []
        for index, item in enumerate(sources):
            path = Path(item["path"])
            tasks.append({"path": str(path), "name": path.name,
                          "out_path": str(dest / f"{index:03d}.avif"), "in_place": False,
                          "device": "gpu", "preset": 7, "cq": 20, "auto_quality": None if args.fixed_cq else 88,
                          "keep_smaller": False, "copy_skipped": False,
                          "transparent_format": "webp", "webp_quality": 90,
                          "webp_method": 2, "webp_lossless_max_mb": 1,
                          "opaque_format": fmt, "opaque_webp_quality": 90,
                          "oversize_format": "webp", "oversize_webp_quality": 80,
                          "oversize_webp_method": 2, "oversize_max_edge": 16383,
                          **item["probe"]})
        rows = []
        begin = time.perf_counter()
        pool = mp.get_context("spawn").Pool(workers)
        timed_out = False
        try:
            results = pool.imap_unordered(worker, tasks, chunksize=1)
            with (dest / "rows.jsonl").open("w", encoding="utf-8", buffering=1) as stream:
                for _ in tasks:
                    remaining = 90 - (time.perf_counter() - begin)
                    if remaining <= 0:
                        raise mp.TimeoutError
                    row = results.next(timeout=remaining)
                    rows.append(row)
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            pool.close()
        except mp.TimeoutError:
            timed_out = True
            pool.terminate()
        except BaseException:
            pool.terminate()
            raise
        finally:
            pool.join()
        wall = time.perf_counter() - begin
        success = [r for r in rows if r["action"] == "encoded"]
        ordered = sorted(r["task_s"] for r in success)
        summary = {"round": number, "format": fmt, "wall_s": wall,
                   "completed": len(rows), "encoded": len(success), "timed_out": timed_out,
                   "out_bytes": sum(r["out_bytes"] for r in success),
                   "sum_task_s": sum(ordered),
                   "median_s": ordered[len(ordered)//2] if ordered else None,
                   "p95_s": ordered[min(len(ordered)-1, int(len(ordered)*.95))] if ordered else None,
                   "max_s": max(ordered, default=0), "rows": rows}
        report["rounds"].append(summary)
        report["elapsed_s"] = time.perf_counter() - started
        (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k:v for k,v in summary.items() if k != "rows"}), flush=True)
        if timed_out:
            print("Round budget exhausted; stopping benchmark.", flush=True)
            break
    print(f"TOTAL {time.perf_counter()-started:.3f}s; REPORT {output / 'results.json'}", flush=True)


if __name__ == "__main__":
    main()
