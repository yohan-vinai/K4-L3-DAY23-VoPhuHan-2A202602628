# Postmortem — Diễn tập DR Lab 23

Đánh giá không quy lỗi cá nhân cho sự cố Region A và quá trình khôi phục sang Region B.

## 1. Dòng thời gian

| Thời điểm UTC | Sự kiện | Bằng chứng |
|---|---|---|
| 2026-10-09 04:17:53.344 | Chặn mạng Region A; bắt đầu tính thời gian outage. | `chaos/chaos-events.jsonl:4` |
| 2026-10-09 04:17:53.484 | Request đầu tiên của người dùng thất bại. | `reports/drill-2-withdr.jsonl:25` |
| 2026-10-09 04:18:13.414 | Health checker đánh dấu Region A không khỏe sau nhiều probe lỗi liên tiếp. | `reports/health-events.jsonl:2` |
| 2026-10-09 04:18:13.487 | Runbook ghi nhận sự cố; lần chấm bài này chạy với `--auto`, không phải xác nhận thủ công. | `reports/runbook-run.jsonl:2` |
| 2026-10-09 04:18:19.751 | DNS cutover sang Region B đã sẵn sàng. | `reports/failover-events.jsonl:5` |
| 2026-10-09 04:18:22.010 | Request đầu tiên thành công từ Region B; outage kết thúc. | `reports/drill-2-withdr.jsonl:39` |

## 2. RTO/RPO và khoảng cách (gap) tới mục tiêu

- Mục tiêu RTO: 300s · đo được: 28.7s · còn thấp hơn mục tiêu 271.3s.
- Mục tiêu RPO: 300s · đo được: 8.02s, bản sao thiếu 4 document · còn thấp hơn mục tiêu 291.98s.
- Thành phần mất nhiều thời gian nhất là phát hiện outage: 20.069s. Ngưỡng cấu hình là 15s; probe timeout và pha polling làm tăng thời gian thực tế.
- Lần diễn tập chưa có DR có 16 request lỗi và không khôi phục trong 40 giây: `reports/measure-drill-1.json:1`.
- Lần diễn tập có DR hợp lệ, không có cảnh báo, khôi phục qua Region B và đạt mục tiêu: `reports/measure-drill-2.json:1`.

## 3. Phân tích nguyên nhân — 5 lần hỏi tại sao

1. Người dùng gặp lỗi vì edge tiếp tục gửi traffic đến Region A sau khi vùng này không còn phản hồi.
2. Traffic vẫn ở A vì lần chạy baseline chưa có cơ chế failover theo health check.
3. Region B chưa thể phục vụ ngay vì ban đầu không có model weights và vector; lần diễn tập phải restore snapshot rồi làm nóng pool.
4. Hệ thống chưa duy trì một vùng dự phòng luôn sẵn sàng và chưa có controller failover production; lab bổ sung replication snapshot, kiểm tra readiness và runbook.
5. Thiết kế ban đầu xem hai process là hai region mà chưa ghép replication state, phát hiện sự cố và cutover traffic thành một luồng khôi phục đã được kiểm chứng.

## 4. Mục hành động (action items)

| # | Việc cần làm | Người phụ trách | Hạn hoàn thành | Mức giảm RTO/RPO dự kiến |
|---|---|---|---|---|
| 1 | Đánh giá interval probe 1 giây nhưng giữ ngưỡng 3 lần lỗi; chạy lại diễn tập và theo dõi flapping cùng tác động của probe timeout. | Nhóm nền tảng/người trực | Trước lần diễn tập DR tiếp theo | Ngưỡng phát hiện danh nghĩa giảm từ 15s còn 3s, giảm 12s nếu probe và polling overhead ổn định. |
| 2 | Thử replication mỗi 5 giây và thêm kiểm tra đồng bộ failback trước khi chuyển traffic về A. | Nhóm dữ liệu/nền tảng | Sprint kế tiếp | Mục tiêu RPO không quá 5s; tránh failback sang replica cũ hơn. |

## 5. Tự phản ánh

1. `interval × threshold = 5s × 3 = 15s`, chiếm 52.3% RTO đo được 28.7s. Thực tế health check phát hiện sau 20.069s do probe timeout và pha polling.
2. Nếu interval giảm còn 1 giây và giữ threshold 3, ngưỡng phát hiện danh nghĩa còn 3s, giảm 12s. Kiểm tra thường xuyên hơn tăng tải probe và khả năng nhầm độ trễ tạm thời thành outage; giữ ba lần lỗi liên tiếp giúp hạn chế rủi ro đó.
3. Bốn document mới hơn snapshot bị restore, trong khoảng mất dữ liệu 8.02s. Khách hàng có thể mất các cập nhật mới nhất và cần gửi lại hoặc nhập lại chúng sau khi hệ thống phục hồi.
