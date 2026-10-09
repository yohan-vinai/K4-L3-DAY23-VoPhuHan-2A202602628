"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import datetime as dt
import json
import math
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Append one timestamped runbook checkpoint."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": dt.datetime.now(dt.timezone.utc).isoformat(),
           "step": n, "name": name, **kw}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
    print("RUNBOOK", json.dumps(rec, ensure_ascii=False))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """Keep operational runs semi-automated; CI may explicitly use --auto."""
    if auto:
        return True
    try:
        answer = input(f"{msg} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _latest_outage(primary: str) -> dict | None:
    events = [e for e in _read_jsonl(pathlib.Path("chaos/chaos-events.jsonl"))
              if e.get("action") == "kill" and e.get("region") == primary]
    return max(events, key=lambda e: e.get("ts", 0)) if events else None


def _wait_for_health_alert(primary: str, after_ts: float, timeout: float = 35.0) -> dict | None:
    """Wait for the independent health checker to record the primary outage."""
    deadline = time.monotonic() + timeout
    path = pathlib.Path("reports/health-events.jsonl")
    while time.monotonic() < deadline:
        for event in _read_jsonl(path):
            if (event.get("event") == "state_change" and event.get("region") == primary
                    and event.get("to") == "UNHEALTHY" and event.get("ts", 0) >= after_ts):
                return event
        time.sleep(0.1)
    return None


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Confirm the incident, call failover once, and record the seven runbook steps."""
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("primary and target must be different regions a/b")
    started_at = time.time()

    # Repeated readiness failures avoid initiating failover on a single lost probe.
    failed = 0
    reasons = []
    for _ in range(3):
        try:
            response = httpx.get(f"{URL[primary]}/readyz", timeout=1.5)
            if response.status_code == 200:
                failed = 0
                reasons = []
                break
            failed += 1
            try:
                reasons = response.json().get("reasons", [])
            except ValueError:
                reasons = [f"HTTP {response.status_code}"]
        except httpx.HTTPError as exc:
            failed += 1
            reasons = [f"{type(exc).__name__}: {exc}"]
        time.sleep(0.2)
    outage = _latest_outage(primary)
    outage_ts = outage.get("ts") if outage else None
    outage_confirmed = failed >= 3
    outage_reference = outage_ts if outage_ts is not None else started_at
    confirm_record = step(1, "xac_nhan_outage", primary=primary, target=target,
                          confirmed=outage_confirmed, consecutive_failures=failed,
                          reason="; ".join(map(str, reasons)) or "ready",
                          chaos_outage_ts=outage_ts)
    if not outage_confirmed:
        step(2, "thong_bao_incident", started=False,
             message="primary region did not fail three consecutive readiness probes")
        step(7, "post_incident", ok=False, elapsed_s=round(time.time() - started_at, 2))
        return {"ok": False, "failed_step": "1_xac_nhan_outage", "outage": outage,
                "confirmation": confirm_record}

    health_alert = _wait_for_health_alert(primary, outage_reference)
    if not health_alert:
        step(2, "thong_bao_incident", started=False, outage_ts=outage_ts,
             reason="no UNHEALTHY transition recorded by health checker within 35s")
        step(7, "post_incident", ok=False, elapsed_s=round(time.time() - started_at, 2))
        return {"ok": False, "failed_step": "2_thong_bao_incident",
                "reason": "health checker alert not observed", "outage": outage}

    incident = step(2, "thong_bao_incident", started=True, primary=primary,
                    outage_ts=outage_ts, health_alert_ts=health_alert["ts"],
                    operator_known_at=time.time(),
                    notification_delay_s=None if outage_ts is None else
                    round(time.time() - outage_ts, 2))
    if not confirm(auto, f"Fail over from region-{primary} to region-{target} now?"):
        step(3, "scale_gpu_pool", started=False, aborted=True, reason="operator declined")
        step(7, "post_incident", ok=False, elapsed_s=round(time.time() - started_at, 2))
        return {"ok": False, "failed_step": "3_scale_gpu_pool", "reason": "operator declined"}

    # This is the only call to failover.failover in the runbook.
    result = fo.failover(target, backend, wait=60)
    state = result.get("target_state") or {}
    replica_ok = bool(result.get("ok") and state.get("weights") and state.get("count", 0) > 0)
    step(3, "scale_gpu_pool", failover_ok=bool(result.get("ok")),
         failed_step=result.get("failed_step"),
         cutover_ts=result.get("cutover_ts"))
    step(4, "verify_state_replica", ok=replica_ok, region=target,
         vectors=state.get("count"), weights=state.get("weights"),
         pool_state=state.get("pool_state"), rpo_seconds=(result.get("rpo") or {}).get("rpo_seconds"),
         docs_lost=(result.get("rpo") or {}).get("docs_lost"))
    step(5, "dns_cutover", ok=bool(result.get("ok")),
         active_region=target if result.get("ok") else None,
         cutover_ts=result.get("cutover_ts"))

    latencies = []
    errors = []
    with httpx.Client(timeout=3.0) as client:
        for i in range(10):
            t0 = time.perf_counter()
            try:
                response = client.get(f"{URL[target]}/v1/infer", params={"q": f"golden-{i}"})
                latency_ms = (time.perf_counter() - t0) * 1000
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                ok = response.status_code == 200 and body.get("region") == target
                if not ok:
                    errors.append({"request": i, "status": response.status_code,
                                   "region": body.get("region"), "error": body.get("error")})
            except httpx.HTTPError as exc:
                latency_ms = (time.perf_counter() - t0) * 1000
                ok = False
                errors.append({"request": i, "error": f"{type(exc).__name__}: {exc}"})
            latencies.append(latency_ms)
    sorted_latencies = sorted(latencies)
    p95_ms = round(sorted_latencies[max(0, math.ceil(0.95 * len(sorted_latencies)) - 1)], 1)
    error_rate = round(len(errors) / 10, 3)
    signals = step(6, "verify_golden_signals", requests=10, p95_latency_ms=p95_ms,
                   error_rate=error_rate, errors=errors,
                   thresholds={"p95_latency_ms_max": 500, "error_rate_max": 0.0},
                   passed=p95_ms < 500 and error_rate == 0.0)

    elapsed_s = round(time.time() - incident["ts"], 2)
    overall_ok = bool(result.get("ok") and replica_ok and signals["passed"])
    final = step(7, "post_incident", ok=overall_ok, elapsed_s=elapsed_s,
                 rto_command="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300",
                 failover_ok=bool(result.get("ok")), golden_signals_ok=signals["passed"])
    return {"ok": overall_ok, "failover": result, "replica_ok": replica_ok,
            "golden_signals": {"p95_latency_ms": p95_ms, "error_rate": error_rate},
            "elapsed_s": elapsed_s, "runbook_finished_at": final["ts"],
            "outage": outage}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
