# Báo Cáo Cá Nhân - Day 9: Multi-Agent E-commerce Dispute Resolution

## 1. Thông tin cá nhân

| Thông tin | Nội dung |
| --- | --- |
| Họ và tên | Nguyễn Thúy Trang |
| MSSV | 2A202601559 |
| Khóa/Lớp | K3 |
| Vai trò chính | Thiết kế và tích hợp workflow multi-agent, policy engine, verifier và artifact nộp bài |
| Ngày hoàn thành | 2026-08-05 |

## 2. Vai trò và phạm vi công việc

### Phần việc sở hữu

| Module/deliverable | File/hàm phụ trách | Input nhận vào | Output bàn giao | Trạng thái |
| --- | --- | --- | --- | --- |
| Orchestration multi-agent | `src/dispute_resolution/workflow.py` | Case JSON, Olist facts đã index | Handoff state, output candidate, trace events | Hoàn thành |
| Data access và policy | `repository.py`, `policy/EC_POLICY_V1.json`, `workflow.py` | Olist orders/items/payments/sellers CSV | Facts theo order, policy decision, financial resolution | Hoàn thành |
| Kiểm tra và đóng gói | `main.py`, `output.zip` | 50 input case và output JSON | 50 JSON, trace, metadata, archive | Hoàn thành về schema; cần chạy lại model audit hợp lệ |
| Tài liệu kiến trúc | `architecture.md` | Yêu cầu README | Sơ đồ agent, quyền truy cập, handoff, retry | Hoàn thành |

### Việc hỗ trợ ngoài phạm vi chính

| Hoạt động | Module được hỗ trợ | Kết quả |
| --- | --- | --- |
| Chuẩn hóa runtime | `.venv`, `.env.example`, `requirements.txt` | Có virtual environment và template cấu hình provider; không đưa secret vào git |
| Kiểm tra artifact | `output/`, `output.zip` | Xác nhận 50 output JSON; zip có đúng 50 entry `output/EC_001.json` đến `output/EC_050.json` |

## 3. Kết quả theo vai trò

| Nhiệm vụ đã thực hiện | File/artifact liên quan | Kết quả bàn giao | Cách xác minh |
| --- | --- | --- | --- |
| Tách domain thành các agent có handoff | `workflow.py`, `architecture.md` | Coordinator, Order & Seller, Payment, Delivery, Policy, Verifier | Đọc state transition và trace agent events |
| Áp dụng chính sách `EC_POLICY_V1` | `policy/EC_POLICY_V1.json`, `workflow.py` | Sáu nhánh issue, root cause, party, refund, action, confidence, tolerance và output limits | So sánh điều kiện rule với README và CSV facts |
| Sinh output theo schema | `output/EC_001.json` ... `EC_050.json` | Đủ 50 JSON | Audit read-only: 50 file, không lỗi schema/limit |
| Đóng gói submission | `output.zip` | 50 entry dưới `output/` | Liệt kê archive và đối chiếu toàn bộ tên file |

Full run sinh output vào staging directory và chỉ publish khi 50 case đều pass verifier; nhờ đó lỗi giữa chừng không làm `output/` trở thành tập file cũ/mới lẫn lộn.

Artifact cụ thể: đợt audit hiện tại xác nhận 50 JSON output, 50 entry trong zip và không có lỗi top-level schema, giới hạn entity ID, evidence ID, root cause, action hay confidence range.

## 4. Giải thích phần kỹ thuật đã thực hiện

### Vấn đề cần giải quyết

Một khiếu nại không thể kết luận từ nội dung khách hàng. Mỗi case phải join order với item, seller và payment; sau đó phân biệt giao trễ do seller, logistics, order canceled/unavailable đã thanh toán, split payment hợp lệ hoặc claim giao trễ không được hỗ trợ. Kết quả phải có evidence ID truy ngược được và số tiền hoàn chính xác.

### Cách triển khai

Coordinator nhận `claimed_order_id`, tạo `CaseState` và dispatch ba specialist agent đọc dữ liệu độc lập:

1. Order & Seller Agent lấy status, items, seller, item total và freight total.
2. Payment Agent tổng payment, số dòng payment và payment ID.
3. Delivery Agent so sánh carrier date, delivered date, estimated date và shipping limit.
4. Policy Agent yêu cầu model tạo proposal JSON từ facts/rules, so sánh proposal với rule engine và chỉ dùng decision deterministic để chốt refund.
5. Verifier Agent kiểm tra schema, số tiền, giới hạn list, evidence và policy outcome trước khi writer ghi JSON.

Phép tính tiền dùng `Decimal`, làm tròn 2 chữ số. Evidence ID chỉ được dựng từ ID có trong CSV và policy code hợp lệ. Confidence được cấu hình theo evidence strength: `0.95` cho status/payment, `0.97` cho timestamp giao hàng, `0.98` khi payment đối soát chính xác và `0.92` khi chỉ khớp trong tolerance. Model tạo proposal JSON từ facts/rules, sau đó trace ghi mức khớp với decision deterministic. Ở `model_assisted`, model được thay decision fields sau khi proposal qua gate rule/refund/party/evidence; ở `deterministic`, proposal chỉ là audit. Mặc định verifier audit một lần mỗi case, và có thể chuyển sang `per_agent` khi điều tra.

### Input, output và contract

| Thành phần | Mô tả |
| --- | --- |
| Input | `input/EC_XXX.json`: `case_id`, `customer_request.claimed_order_id`, `policy_version=EC_POLICY_V1` |
| Output | `output/EC_XXX.json` theo schema README: assessment, entities, cause/party, evidence, financial resolution, actions |
| Module phụ thuộc | `repository.py` cung cấp CSV index; `model_audit.py` là provider client tùy chọn |
| Module sử dụng output | `main.py` ghi file, `create_submission_zip` tạo archive, grader đọc zip |
| Điều kiện lỗi | Thiếu order, policy version sai, không có rule khớp, evidence/amount/schema không hợp lệ hoặc model audit strict thất bại |

### Cách xác minh

```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

- **Kết quả mong đợi:** tạo 50 JSON, `trace.jsonl`, `metadata.json` và `output.zip` chứa đúng `output/EC_001.json` đến `output/EC_050.json`.
- **Kết quả đã xác minh:** 50 output JSON và archive 50 entry hợp lệ về schema/giới hạn.
- **Artifact/log:** `output/`, `output.zip`, `trace.jsonl`, `metadata.json`; không ghi API key vào các artifact này.

## 5. Một quyết định kỹ thuật quan trọng

- **Bối cảnh:** Quy tắc refund và evidence có tiêu chí dữ liệu rõ ràng, trong khi bài vẫn yêu cầu multi-agent và trace.
- **Các phương án đã cân nhắc:** (1) để LLM đọc toàn bộ CSV và tự ra quyết định; (2) dùng workflow deterministic có agent domain rõ ràng, LLM chỉ audit handoff.
- **Phương án đã chọn:** Supervisor + specialist workers + deterministic Policy/Verifier; provider model chỉ là audit layer.
- **Lý do:** Cách này tái lập được, tránh hallucinated evidence/refund, cho phép debug theo từng handoff và vẫn thể hiện phân công agent thực sự.
- **Bằng chứng:** Verifier hoàn tất 50 case với `valid: true`; output audit không phát hiện lỗi schema hay giới hạn output.

## 6. Một lỗi hoặc blocker đã xử lý

- **Triệu chứng cũ:** Provider audit từng trả `401 Missing Authentication header` khi provider/key/endpoint không cùng cặp.
- **Nguyên nhân gốc:** Runtime OpenAI đã từng trỏ sang endpoint OpenRouter nên key tương ứng không được gửi đúng cách.
- **Cách xử lý:** Tách cấu hình `OPENROUTER_*` và `OPENAI_*`, chọn qua `DISPUTE_MODEL_PROVIDER`; không log secret. Runtime hiện chọn `gpt-4o-mini` để tạo policy proposal và audit; Qwen 8.2B vẫn là lựa chọn thay thế khi cần parameter size công bố rõ.
- **Trạng thái hiện tại:** Lượt tái sinh artifact dùng `--no-model-audit`, nên trace không chứa event provider lỗi hoặc event `disabled` dư thừa. Deterministic verifier vẫn xác minh 50 case trước khi publish output.
- **Điều học được:** Metadata và trace phải được kiểm tra như một contract độc lập; output đúng schema chưa đủ để chứng minh external agent provider hoạt động.

## 7. Hiểu biết về luồng end-to-end

1. Input case cung cấp order ID. Repository index CSV theo `order_id` để specialist agent lấy facts mà không quét lại toàn bộ file cho mỗi case.
2. Ba specialist handoff facts vào shared state. Policy chỉ đọc facts đã chuẩn hóa và chọn rule ưu tiên đầu tiên khớp.
3. Verifier kiểm tra output trước khi ghi file: ID có format hợp lệ, số tiền khớp facts, refund/action đúng policy và list không vượt giới hạn.
4. Chất lượng bài được đánh giá trên cùng 50 case bằng output schema, entities, causes, evidence, financial fields và actions. Không có Crossref, vector index hoặc retrieval ground truth trong bài Olist này; các câu hỏi đó thuộc template chung nên được thay bằng luồng Olist thực tế.
5. Một lượt chạy thành công chỉ được coi là hoàn tất khi output có đủ 50 file, zip có đúng 50 entry dưới `output/`, verifier pass và trace/metadata phản ánh đúng model/provider đã dùng.

## 8. Cam kết của thành viên

- [x] Nội dung báo cáo phản ánh đúng phần việc và mức hiểu của tôi.
- [x] Tôi có thể giải thích luồng end-to-end, không chỉ module mình phụ trách.
- [x] Tôi phân biệt rõ lượt chạy offline không gọi provider với lượt chạy bật model proposal/audit.
- [x] Báo cáo không chứa `.env`, API key, token hoặc secret.
- [x] Báo cáo này được viết theo artifact và workflow của bài Olist, không sao chép nội dung Crossref/vector-index không liên quan.

**Họ và tên:** Nguyễn Thúy Trang  
**Ngày xác nhận:** 2026-08-05
