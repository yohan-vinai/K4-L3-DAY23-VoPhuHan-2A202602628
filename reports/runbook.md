# Runbook — Region A không hoạt động

**Phạm vi:** cụm lab chạy cục bộ tại `127.0.0.1`, Region A là vùng chính và Region B là vùng dự phòng. Người trực vận hành phụ trách sự cố. Chỉ huy sự cố phê duyệt việc chuyển lưu lượng trở lại A.

| # | Bước | Lệnh có thể sao chép | Dấu hiệu hoàn tất | Người phụ trách |
|---|---|---|---|---|
| 1 | Xác nhận sự cố | `python3 chaos/kill_region.py status` | `a.alive=false`; runbook kiểm tra thêm ba lần `/readyz` thất bại liên tiếp và chờ health checker ghi trạng thái `UNHEALTHY`. | Người trực vận hành |
| 2 | Mở incident và bắt đầu tính RTO | `python3 dr/runbook.py --primary a --target b --backend fs` | Trả lời `y` khi được hỏi. `reports/runbook-run.jsonl` ghi `thong_bao_incident` cùng thời điểm outage, cảnh báo và lúc người vận hành nhận biết. Chỉ chạy lệnh này một lần; runbook tự thực hiện các bước 3–7. | Người trực vận hành |
| 3 | Khôi phục snapshot và làm nóng Region B | `tail -n 5 reports/failover-events.jsonl` | Có đủ sự kiện theo thứ tự: xác minh vùng đích, khôi phục snapshot, mở rộng pool, chờ sẵn sàng. Bản ghi restore có phiên bản embedding model và RPO. | Người trực nền tảng |
| 4 | Xác minh state đã khôi phục | `curl -s http://127.0.0.1:8002/v1/state && curl -s http://127.0.0.1:8002/readyz` | `weights=true`, số vector lớn hơn 0 và `/readyz` trả HTTP 200. | Người trực nền tảng |
| 5 | Chuyển lưu lượng sang Region B | `curl -s http://127.0.0.1:8080/edge/state` | `active_region=b`; sự kiện `5_dns_cutover` chỉ xuất hiện sau khi B sẵn sàng. Chờ TTL edge 5 giây hết hạn. | Chỉ huy sự cố |
| 6 | Kiểm tra các chỉ số vận hành chính | `tail -n 2 reports/runbook-run.jsonl` | `verify_golden_signals` ghi 10 request, p95 dưới 500 ms và tỷ lệ lỗi 0%. | Chủ dịch vụ |
| 7 | Đo RTO và viết postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid=true`, không có cảnh báo và `rto_verdict=PASS`; lưu các log JSONL gốc làm bằng chứng. | Chỉ huy sự cố |

**Điều kiện rollback/chuyển lưu lượng về A:** tiếp tục phục vụ ở B nếu A chưa sẵn sàng qua ba lần kiểm tra liên tiếp hoặc vector, model weights, hay model version không khớp state đang phục vụ. Chỉ chuyển về A sau khi A sẵn sàng và state đã đồng bộ với B; lab này chỉ triển khai replication từ A sang B. Chỉ huy sự cố phê duyệt sau khi chủ dịch vụ xác minh state. Sau đó người trực chạy `printf a > edge/active_region`, chờ ít nhất 5 giây cho edge TTL hết hạn, rồi xác nhận `/edge/state` trả `a` và inference thành công. Nếu bất kỳ bước nào lỗi, chuyển lưu lượng lại B và tiếp tục mở incident.
