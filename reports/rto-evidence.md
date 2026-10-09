# Bằng chứng RTO/RPO — Lab 23

Các mốc thời gian dưới đây lấy từ hai lần diễn tập cục bộ. Mỗi đường dẫn bằng chứng trỏ đến file và dòng ghi nhận sự kiện. Hai file JSON `measure-drill` lưu kết quả thực tế của `tools/measure_rto.py`.

## 1. Lần diễn tập 1 — chưa có disaster recovery

| Chỉ số | Giá trị đo được | Cách đo | Bằng chứng |
|---|---:|---|---|
| Bắt đầu outage | 2026-10-09 04:16:04 UTC | Chặn mạng Region A bằng `netblock` | `chaos/chaos-events.jsonl:2` |
| Request lỗi đầu tiên | +0.04s | Dòng `ok:false` đầu tiên sau outage | `reports/drill-1-nodr.jsonl:17` |
| Request lỗi | 16 trên 32 | Các request sau outage | `reports/drill-1-nodr.jsonl:17` |
| Khôi phục | Không có | Không có request thành công nào sau outage | `reports/measure-drill-1.json:1` |
| RTO | `NO_RECOVERY` | Đo từ timestamp của request | `reports/measure-drill-1.json:1` |

## 2. Lần diễn tập 2 — disaster recovery tự động

| Mốc | Thời gian từ outage | Bằng chứng |
|---|---:|---|
| Bắt đầu outage | 0s | `chaos/chaos-events.jsonl:4` |
| Người dùng thấy lỗi đầu tiên | +0.1s | `reports/drill-2-withdr.jsonl:25` |
| Health checker đánh dấu A không khỏe | +20.1s | `reports/health-events.jsonl:2` |
| Khôi phục snapshot; 4 document chưa có trong bản sao | RPO 8.02s | `reports/failover-events.jsonl:2` |
| Region B sẵn sàng | +26.4s | `reports/failover-events.jsonl:4` |
| DNS cutover sang B | +26.4s | `reports/failover-events.jsonl:5` |
| Request thành công đầu tiên từ B | +28.7s | `reports/drill-2-withdr.jsonl:39` |
| Golden signals của runbook | p95 2.5ms; lỗi 0% | `reports/runbook-run.jsonl:6` |

| Chỉ số | Kết quả đo | Mục tiêu | Đánh giá |
|---|---:|---:|---|
| RTO của inference API | 28.7s | 300s | Đạt; thấp hơn mục tiêu 271.3s |
| RPO của vector DB | 8.02s; mất 4 document | 300s | Đạt; thấp hơn mục tiêu 291.98s |

Công cụ đo xác nhận `valid: true`, không có cảnh báo và request được phục vụ lại bởi Region B: `reports/measure-drill-2.json:1`.

## 3. Các thành phần tạo nên RTO

Ngưỡng phát hiện theo cấu hình là `5s × 3 = 15s`. Lần đo này phát hiện sau 20.069s vì thời điểm outage, timeout của probe và pha polling làm tăng thời gian so với ngưỡng danh nghĩa. Các thành phần đo dưới đây cộng lại thành 28.665s; công cụ RTO làm tròn thành 28.7s.

| Thành phần | Giây | Cách đo và bằng chứng | Cách có thể giảm |
|---|---:|---|---|
| Phát hiện outage bằng health check | 20.069 | Từ outage tại `chaos/chaos-events.jsonl:4` đến sự kiện `UNHEALTHY` tại `reports/health-events.jsonl:2`; ngưỡng danh nghĩa là 15s. | Giảm interval một cách thận trọng, giữ điều kiện nhiều lần lỗi liên tiếp. |
| Chuyển giao runbook và khôi phục snapshot | 0.117 | Từ cảnh báo tại `reports/health-events.jsonl:2` đến lúc mở rộng pool tại `reports/failover-events.jsonl:3`; hoàn tất restore được ghi tại `reports/failover-events.jsonl:2`. | Giảm độ trễ bàn giao cho automation và giữ snapshot gọn. |
| Làm nóng GPU pool và chờ sẵn sàng | 6.220 | Từ lúc mở rộng pool tại `reports/failover-events.jsonl:3` đến khi sẵn sàng/cutover tại `reports/failover-events.jsonl:5`; thời gian chờ readiness ghi là 6.19s tại `reports/failover-events.jsonl:4`. | Duy trì pool dự phòng đã làm nóng nếu chi phí phù hợp. |
| DNS/LB TTL và quan sát request đầu tiên | 2.259 | Request thành công đầu tiên tại `reports/drill-2-withdr.jsonl:39` trừ thời điểm cutover tại `reports/failover-events.jsonl:5`. | Giảm edge TTL và tiếp tục probe qua edge. |

## 4. Lệnh chạy lại phép đo

```bash
python3 tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl --target-rto 300
python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300
```
