"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import concurrent.futures
import datetime as dt
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Return readiness and a useful reason; the timeout bounds SIGSTOP/netblock probes."""
    try:
        response = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code == 200:
            return True, "ready"
        reasons = body.get("reasons") or [f"HTTP {response.status_code}"]
        return False, "; ".join(map(str, reasons))
    except httpx.TimeoutException:
        return False, f"readiness probe timed out after {timeout}s"
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Poll both regions and append one JSONL record per health-state transition."""
    if interval <= 0 or timeout <= 0 or threshold < 1 or duration < 0:
        raise ValueError("interval/timeout must be positive, threshold >= 1, duration >= 0")
    out.parent.mkdir(parents=True, exist_ok=True)
    states = {region: "HEALTHY" for region in URL}
    failures = {region: 0 for region in URL}
    failure_started = {region: None for region in URL}
    successes = {region: 0 for region in URL}
    end = time.monotonic() + duration

    with out.open("a", encoding="utf-8") as f, concurrent.futures.ThreadPoolExecutor(
            max_workers=len(URL)) as pool:
        while time.monotonic() < end:
            cycle_started = time.monotonic()
            results = {region: future.result() for region, future in {
                region: pool.submit(probe, region, timeout) for region in URL
            }.items()}
            for region, (ready, reason) in results.items():
                if ready:
                    failures[region] = 0
                    failure_started[region] = None
                    successes[region] += 1
                    if states[region] == "UNHEALTHY" and successes[region] >= threshold:
                        to = "HEALTHY"
                    else:
                        to = states[region]
                else:
                    if failures[region] == 0:
                        failure_started[region] = time.monotonic()
                    successes[region] = 0
                    failures[region] += 1
                    floor_elapsed = (failure_started[region] is not None and
                                     time.monotonic() - failure_started[region] >= interval * threshold)
                    if (states[region] == "HEALTHY" and failures[region] >= threshold
                            and floor_elapsed):
                        to = "UNHEALTHY"
                    else:
                        to = states[region]
                if to != states[region]:
                    rec = {
                        "ts": time.time(),
                        "iso": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "event": "state_change",
                        "region": region,
                        "from": states[region],
                        "to": to,
                        "reason": reason,
                        "consecutive_fails": failures[region],
                        "consecutive_successes": successes[region],
                        "interval_s": interval,
                        "threshold": threshold,
                    }
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    f.flush()
                    print("HEALTH", json.dumps(rec, ensure_ascii=False))
                    states[region] = to
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, max(0.0, interval - (time.monotonic() - cycle_started))))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
