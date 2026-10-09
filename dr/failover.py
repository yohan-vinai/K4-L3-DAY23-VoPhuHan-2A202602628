"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import datetime as dt
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append a timestamped failover event and print the same record."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": dt.datetime.now(dt.timezone.utc).isoformat(), **kw}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
    print("FAILOVER", json.dumps(rec, ensure_ascii=False))
    return rec


def state_of(region: str, timeout: float = 2.0) -> dict:
    """Read the target's liveness/state; an empty target is valid before restore."""
    response = httpx.get(f"{URL[region]}/v1/state", timeout=timeout)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore state, warm the target, and only then move edge traffic."""
    if target not in URL:
        raise ValueError(f"unknown target region: {target}")
    if wait <= 0:
        raise ValueError("wait must be positive")

    try:
        target_before = state_of(target)
    except (httpx.HTTPError, ValueError) as exc:
        emit(step="1_verify_target", target=target, ok=False, error=f"{type(exc).__name__}: {exc}")
        return {"ok": False, "failed_step": "1_verify_target", "error": str(exc)}
    emit(step="1_verify_target", target=target, ok=True, target_state=target_before)

    try:
        meta = snapshot.get(target, backend)
        primary_db = pathlib.Path("state/region-a/vectors.sqlite")
        restored_db = pathlib.Path(f"state/region-{target}/vectors.sqlite")
        rpo = snapshot.rpo(primary_db, restored_db)
        rpo_seconds = rpo.get("rpo_seconds")
        docs_lost = rpo.get("docs_lost")
        rec = emit(step="2_restore_snapshot", target=target, ok=True,
                   snapshot_at=meta.get("snapshot_at"), restored_at=meta.get("restored_at"),
                   rpo_seconds=rpo_seconds, docs_lost=docs_lost,
                   embed_model_version=meta.get("embed_model_version"),
                   restored_latest_doc_ts=rpo.get("restored_latest_doc_ts"),
                   primary_latest_doc_ts=rpo.get("primary_latest_doc_ts"))
    except Exception as exc:
        emit(step="2_restore_snapshot", target=target, ok=False,
             error=f"{type(exc).__name__}: {exc}")
        return {"ok": False, "failed_step": "2_restore_snapshot", "error": str(exc)}

    pool_file = pathlib.Path(f"state/region-{target}/pool_state")
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    previous_pool = pool_file.read_text().strip() if pool_file.exists() else "unknown"
    pool_file.write_text("full\n")
    emit(step="3_scale_pool", target=target, ok=True,
         previous_pool_state=previous_pool, pool_state="full")

    started = time.monotonic()
    deadline = started + wait
    last_reason = "readiness probe has not completed"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{URL[target]}/readyz", timeout=min(2.0, max(0.1, deadline - time.monotonic())))
            if response.status_code == 200:
                waited_s = round(time.monotonic() - started, 2)
                target_state = state_of(target)
                emit(step="4_wait_ready", target=target, ok=True, waited_s=waited_s,
                     target_state=target_state)
                break
            try:
                body = response.json()
            except ValueError:
                body = {}
            last_reason = "; ".join(map(str, body.get("reasons", []))) or f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    else:
        waited_s = round(time.monotonic() - started, 2)
        emit(step="4_wait_ready", target=target, ok=False, waited_s=waited_s,
             reason=last_reason, aborted=True)
        return {"ok": False, "failed_step": "4_wait_ready", "waited_s": waited_s,
                "reason": last_reason, "restore": meta, "rpo": rpo}

    pathlib.Path("edge").mkdir(parents=True, exist_ok=True)
    pathlib.Path("edge/active_region").write_text(target + "\n")
    cutover = emit(step="5_dns_cutover", target=target, ok=True, active_region=target)
    return {"ok": True, "target": target, "target_state": target_state,
            "restore": meta, "rpo": rpo, "waited_s": waited_s,
            "cutover_ts": cutover["ts"]}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
