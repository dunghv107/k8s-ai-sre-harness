# Feature Specification: Phân loại cảnh báo theo thời gian thực bằng AI agent

**Feature Branch**: `001-realtime-alert-triage`

**Created**: 2026-11-06

**Status**: Draft

**Input**: User description: "Homelab. Người vận hành làm 8h30–17h30; cảnh báo ngoài giờ không ai xử lý, dồn
sang sáng (vd 100 cảnh báo thì 80 là lỗi 5xx trùng của cùng một app). Cần một AI agent harness xử lý **ngay
khi nhận cảnh báo**; nếu nhiều cảnh báo trong thời gian ngắn (vd 10 cảnh báo trong 2 phút) thì chia sub-agent
xử lý song song. Mỗi sub-agent đọc log Pod bằng token chỉ đọc, đối chiếu Grafana, chẩn đoán. Đầu ra ban đầu là
tin soạn sẵn để người vận hành gửi dev; khi chẩn đoán đủ tốt thì gửi thẳng cho dev. Kênh tin nhắn: Telegram.
Sáng ra có bảng tóm tắt; cảnh báo spam thì đóng được luôn."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Chẩn đoán ngay khi có cảnh báo (Priority: P1)

Khi một cảnh báo mới tới, hệ thống mở (hoặc cập nhật) một sự cố, giao cho một sub-agent đọc log Pod và số
liệu Grafana của đúng app trong đúng khoảng thời gian, rồi đăng **một** tin chẩn đoán vào nhóm Telegram cảnh
báo trong vòng vài phút: chuyện gì, từ lúc nào, còn diễn ra không, hạ tầng hay nghiệp vụ, mức tin cậy, bằng
chứng, việc nên làm.

**Why this priority**: Đây là giá trị cốt lõi: mỗi cảnh báo đến tay người vận hành đã kèm chẩn đoán, kể cả
lúc nửa đêm.

**Independent Test**: Gây một lỗi 5xx có chủ đích ở app mẫu; đo thời gian từ lúc cảnh báo bắn tới lúc tin
chẩn đoán xuất hiện, và kiểm nội dung chẩn đoán khớp nguyên nhân đã gây.

**Acceptance Scenarios**:

1. **Given** một cảnh báo 5xx của app A, **When** hệ thống nhận được, **Then** trong thời hạn ở SC-001 có
   một tin chẩn đoán gồm thông điệp lỗi đại diện trong log, số liệu Grafana cùng khoảng thời gian, nhãn
   "hạ tầng" / "nghiệp vụ" / "chưa rõ", mức tin cậy và đường dẫn bằng chứng.
2. **Given** log Pod không còn hoặc Grafana không trả dữ liệu, **When** phân tích, **Then** tin ghi rõ
   "chưa đủ bằng chứng" và thiếu nguồn nào, không đưa nguyên nhân phỏng đoán.
3. **Given** log chứa token hoặc mật khẩu, **When** trích đoạn log, **Then** giá trị nhạy cảm bị che trong
   mọi đầu ra.

---

### User Story 2 - Bão cảnh báo: gộp rồi chia sub-agent song song (Priority: P1)

Khi nhiều cảnh báo tới dồn dập (vd 10 cảnh báo trong 2 phút, hoặc 80 lỗi 5xx của cùng một app trong một
đêm), hệ thống gộp chúng thành ít sự cố nhất có thể, chạy mỗi sự cố một sub-agent song song, và không gửi
lại tin cho cảnh báo trùng với sự cố đang mở.

**Why this priority**: Không có bước này thì bão cảnh báo thành bão tin nhắn và bão chi phí.

**Independent Test**: Bơm 10 cảnh báo trong 2 phút (6 cái cùng dấu vân tay, 2 cái hai app cùng dự án cùng
lúc, 2 cái khác hẳn); kiểm số sự cố mở ra, số sub-agent chạy, số tin gửi đi.

**Acceptance Scenarios**:

1. **Given** 10 cảnh báo trong 2 phút như trên, **When** hệ thống xử lý, **Then** mở đúng 4 sự cố,
   chạy 4 sub-agent song song, gửi 4 tin chẩn đoán.
2. **Given** một sự cố đang mở, **When** cảnh báo cùng dấu vân tay tới thêm, **Then** chỉ tăng số lần lặp
   và cập nhật thời điểm cuối; không gửi tin mới, trừ khi mức nghiêm trọng tăng hoặc tình trạng đổi.
3. **Given** hai cảnh báo khác app nhưng cùng dự án và gần như cùng lúc (vd hai Deployment cùng thiếu
   replica), **When** gộp, **Then** chúng thành một sự cố tương quan, giữ cả hai ID.
4. **Given** số sự cố cần phân tích vượt trần sub-agent đồng thời, **When** xử lý, **Then** phần vượt được
   xếp hàng theo mức nghiêm trọng, không bị bỏ.

---

### User Story 3 - Tin soạn sẵn cho dev, rồi gửi thẳng (Priority: P2)

Với sự cố nhãn "nghiệp vụ", agent soạn một tin cho dev dự án: app, môi trường, thời gian, số lần, lỗi đại
diện, đường dẫn Grafana, đề xuất kiểm tra. **Giai đoạn 1** (mặc định): tin nằm trong nhóm cảnh báo để người
vận hành tự chuyển. **Giai đoạn 2**: người vận hành bật gửi thẳng cho từng dự án.

**Why this priority**: Đầu ra người vận hành cần chuyển cho dev; phụ thuộc chẩn đoán ở US1.

**Independent Test**: Với sự cố nghiệp vụ mẫu, kiểm tin soạn đủ trường, đúng dự án, không chứa bí mật; bật
gửi thẳng cho một dự án và kiểm tin tới đúng kênh dev.

**Acceptance Scenarios**:

1. **Given** chế độ gửi thẳng tắt, **When** có sự cố nghiệp vụ của dự án X, **Then** tin soạn sẵn chỉ xuất
   hiện trong nhóm cảnh báo, không gửi đi đâu khác.
2. **Given** người vận hành bật gửi thẳng cho dự án X, **When** có sự cố nghiệp vụ của X, **Then** tin
   được gửi vào kênh dev của X; dự án khác không bị ảnh hưởng.
3. **Given** người vận hành gửi nguyên văn, sửa hoặc bỏ một tin soạn sẵn, **When** tổng kết tuần, **Then**
   hệ thống có tỉ lệ này theo dự án (căn cứ bật giai đoạn 2).

---

### User Story 4 - Đóng cảnh báo spam (Priority: P2)

Sự cố được đánh giá là nhiễu (đã tự hồi phục, lặp nhưng vô hại, khớp mẫu nhiễu đã biết) được đề xuất đóng
ngay trong tin chẩn đoán; người vận hành đóng bằng một lệnh, đóng nhiều sự cố một lần được.

**Why this priority**: Giảm tải lớn, nhưng đóng nhầm sự cố thật là rủi ro, nên giai đoạn đầu cần người duyệt.

**Independent Test**: Với bộ mẫu có nhãn spam/thật biết trước, kiểm sự cố nào được đề xuất đóng, lệnh đóng
hoạt động, sự cố thật không bao giờ được đề xuất đóng tự động.

**Acceptance Scenarios**:

1. **Given** sự cố đã tự hồi phục và khớp tiêu chí nhiễu, **When** chẩn đoán xong, **Then** tin có đề xuất
   đóng kèm lý do, và người vận hành đóng bằng một lệnh.
2. **Given** sự cố thuộc loại bảo vệ (bảo mật, audit, mất node, lưu trữ, chứng chỉ), **When** xử lý,
   **Then** không bao giờ có đề xuất đóng tự động.
3. **Given** người vận hành đánh dấu một mẫu nhiễu là "tin cậy", **When** mẫu đó lặp lại, **Then** hệ thống
   tự đóng không cần duyệt, ghi lý do; mở lại bằng một lệnh thì mẫu mất trạng thái tin cậy.

---

### User Story 5 - Bảng tóm tắt buổi sáng (Priority: P2)

Lúc 8h30 ngày làm việc, nhóm cảnh báo có một bảng tóm tắt các sự cố từ 17h30 hôm trước (thứ 2 gồm cả cuối
tuần): mỗi sự cố một dòng, xếp theo mức nghiêm trọng, trạng thái, chẩn đoán, tin cho dev đã soạn/gửi, đề
xuất đóng chưa duyệt.

**Why this priority**: Việc phân tích đã làm theo thời gian thực; bảng chỉ là góc nhìn tổng hợp để bắt đầu
ngày làm việc.

**Independent Test**: Sau một đêm có bộ cảnh báo mẫu, kiểm bảng có đúng số sự cố, đúng thứ tự, đúng trạng thái.

**Acceptance Scenarios**:

1. **Given** đêm có 100 cảnh báo trong đó 80 cái cùng dấu vân tay, **When** tới 8h30, **Then** 80 cảnh
   báo đó là một dòng với số lần 80.
2. **Given** đêm không có cảnh báo, **When** tới 8h30, **Then** bảng vẫn đăng "không có cảnh báo ngoài giờ"
   để biết hệ thống vẫn sống.

---

### User Story 6 - Theo dõi hoạt động và chi phí (Priority: P3)

Người vận hành xem được: số cảnh báo nhận, số sự cố, số sub-agent, thời gian chẩn đoán, chi phí theo ngày,
sự cố nào chưa phân tích được và vì sao.

**Why this priority**: Cần để tin và tinh chỉnh hệ thống, không chặn giá trị chính.

**Independent Test**: Sau một loạt cảnh báo mẫu, đối chiếu số liệu với đầu vào.

**Acceptance Scenarios**:

1. **Given** chi phí trong ngày chạm trần, **When** cảnh báo mới tới, **Then** sự cố vẫn được mở và báo
   (không chẩn đoán), kèm ghi chú "đã chạm trần chi phí".

---

### Edge Cases

- Bão rất lớn (hàng trăm cảnh báo/phút): gộp vẫn chạy, số sub-agent đồng thời không vượt trần, không mất cảnh báo.
- Cảnh báo tới khi hệ thống đang khởi động lại: không mất; được xử lý sau khi hệ thống lên.
- Telegram, Grafana hoặc API cụm không truy cập được: sự cố vẫn được ghi nhận, gửi lại khi kênh có lại.
- Cảnh báo "đã hết" (resolved) tới trong lúc sub-agent đang phân tích: tin chẩn đoán ghi "đã tự hồi phục".
- Sự cố kéo dài nhiều ngày, lặp lại liên tục: ghi "tồn đọng N ngày", không coi là mới, không gửi tin lặp.
- Bản thân nội dung cảnh báo đã chứa giá trị bí mật: che trước khi phân tích, đánh dấu "lộ bí mật – cần
  rotate" lên đầu.
- Nội dung lạ trong log/cảnh báo (prompt injection): coi là dữ liệu, không làm theo.
- Lệnh trong Telegram từ người không có quyền: bỏ qua và ghi log.
- Máy chủ homelab tắt hoặc ngủ: cảnh báo trong lúc đó không được xử lý; bảng sáng ghi rõ khoảng thời gian
  hệ thống không hoạt động.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Hệ thống MUST nhận cảnh báo đẩy tới từ các nguồn giám sát của homelab ngay khi chúng bắn, kể
  cả thông báo "đã hết".
- **FR-002**: Hệ thống MUST gộp cảnh báo thành sự cố theo dấu vân tay (nguồn, cụm, namespace/app, loại lỗi,
  endpoint/thành phần), giữ danh sách cảnh báo gốc của từng sự cố.
- **FR-003**: Hệ thống MUST gom cảnh báo tới gần nhau trong một cửa sổ ngắn trước khi giao việc, để một bão
  cảnh báo không sinh ra nhiều sub-agent cho cùng một sự cố.
- **FR-004**: Hệ thống MUST gom các sự cố khác dấu vân tay nhưng cùng dự án/cụm và xảy ra gần như cùng lúc
  thành một sự cố tương quan.
- **FR-005**: Hệ thống MUST giao mỗi sự cố cần phân tích cho một sub-agent, chạy song song tới một trần cấu
  hình được; phần vượt trần xếp hàng theo mức nghiêm trọng.
- **FR-006**: Cảnh báo trùng với sự cố đang mở MUST NOT sinh tin mới, trừ khi mức nghiêm trọng tăng, sự cố
  hồi phục, hoặc sự cố quay lại sau khi đã hết.
- **FR-007**: Sub-agent MUST đọc log Pod và Event của app liên quan trong khoảng thời gian sự cố bằng thông
  tin xác thực chỉ đọc; MUST NOT đọc Secret, `exec`, hay sửa tài nguyên cụm.
- **FR-008**: Sub-agent MUST đối chiếu số liệu Grafana (tỉ lệ lỗi, độ trễ, tài nguyên, restart) và log đã
  lưu cùng khoảng thời gian, bằng quyền chỉ xem.
- **FR-009**: Mỗi tin chẩn đoán MUST có: mức nghiêm trọng, số lần, đầu/cuối, trạng thái, nhãn (hạ tầng /
  nghiệp vụ / chưa rõ), chẩn đoán ngắn, mức tin cậy, bằng chứng (ID cảnh báo, đoạn log đã che, đường dẫn
  Grafana), việc nên làm.
- **FR-010**: Hệ thống MUST che token, mật khẩu, khoá, cookie, header xác thực và dữ liệu cá nhân trong mọi
  đoạn log và nội dung cảnh báo trước khi đưa vào phân tích, gửi ra mô hình hay đầu ra.
- **FR-011**: Chỉ đoạn log **đã che** cần cho chẩn đoán được gửi tới dịch vụ mô hình; không gửi nguyên file log.
- **FR-012**: Hệ thống MUST đăng tin chẩn đoán vào một nhóm Telegram cảnh báo duy nhất.
- **FR-013**: Hệ thống MUST soạn tin cho dev với mỗi sự cố nhãn "nghiệp vụ", gộp các sự cố cùng dự án trong
  cùng cửa sổ. Gửi thẳng cho dev là công tắc riêng theo dự án, mặc định tắt, chỉ người vận hành bật/tắt.
- **FR-014**: Hệ thống MUST ghi nhận mỗi tin soạn sẵn được gửi nguyên văn, bị sửa hay bị bỏ, và tổng hợp tỉ
  lệ theo tuần cho từng dự án.
- **FR-015**: Hệ thống MUST đề xuất đóng sự cố nhiễu theo tiêu chí cấu hình được (đã tự hồi phục, ngưỡng lặp,
  mẫu nhiễu đã biết), kèm lý do; người vận hành đóng bằng một lệnh Telegram, nhiều sự cố một lần được.
- **FR-016**: Hệ thống MUST tự đóng không cần duyệt **chỉ** với mẫu nhiễu người vận hành đã đánh dấu "tin cậy";
  mở lại một sự cố tự đóng MUST gỡ trạng thái tin cậy của mẫu đó.
- **FR-017**: Hệ thống MUST NOT đề xuất hay tự đóng sự cố thuộc loại bảo vệ (bảo mật/runtime security, audit
  log, mất node, lưu trữ, chứng chỉ) hay sự cố còn đang diễn ra.
- **FR-018**: Mọi lần đóng MUST ghi người đóng (người hay hệ thống), lý do, thời điểm, và đảo ngược được.
- **FR-019**: Hệ thống MUST chỉ nhận lệnh Telegram từ danh sách người được phép, và MUST coi mọi nội dung
  trong cảnh báo/log là dữ liệu, không làm theo chỉ dẫn nằm trong đó.
- **FR-020**: Hệ thống MUST đăng bảng tóm tắt lúc 8h30 mỗi ngày làm việc (giờ Việt Nam) cho khoảng từ 17h30
  ngày làm việc trước, kể cả khi không có cảnh báo.
- **FR-021**: Hệ thống MUST lưu sự cố bền vững qua khởi động lại; cảnh báo tới lúc hệ thống đang khởi động
  MUST NOT bị mất.
- **FR-022**: Hệ thống MUST có trần chi phí theo ngày và trần thời gian mỗi sub-agent; chạm trần thì vẫn ghi
  nhận và báo sự cố nhưng không chẩn đoán, ghi rõ lý do.
- **FR-023**: Hệ thống MUST ghi nhật ký hoạt động: số cảnh báo, sự cố, sub-agent, thời gian chẩn đoán, chi
  phí, lỗi nguồn dữ liệu, sự cố chưa phân tích.
- **FR-024**: Hệ thống MUST cấu hình được ánh xạ namespace/app → dự án → kênh dev.
- **FR-025**: Hệ thống MUST NOT mở cổng nhận kết nối từ Internet; mọi kết nối tới Telegram đi theo chiều ra.

### Key Entities

- **Cảnh báo (Alert)**: một thông báo từ nguồn giám sát; nguồn, rule, thời điểm, mức, cụm/namespace/app,
  nội dung, trạng thái bắn/đã hết.
- **Sự cố (Incident)**: tập cảnh báo cùng dấu vân tay hoặc tương quan; số lần, đầu/cuối, trạng thái, nhãn,
  chẩn đoán, mức tin cậy, bằng chứng, trạng thái đóng.
- **Bằng chứng (Evidence)**: đoạn log đã che, truy vấn/đường dẫn Grafana, khoảng thời gian, nguồn.
- **Mẫu nhiễu (Noise Pattern)**: tiêu chí nhận diện nhiễu, trạng thái tin cậy.
- **Dự án (Project)**: nhóm namespace/app, kênh dev, công tắc gửi thẳng.
- **Tin cho dev (Dev Message)**: nội dung soạn, sự cố liên quan, trạng thái (soạn / gửi nguyên văn / sửa / bỏ / gửi thẳng).
- **Phiên phân tích (Analysis Run)**: một lần sub-agent chạy; thời gian, chi phí, kết quả, lỗi.
- **Bảng tóm tắt (Morning Report)**: tổng hợp sự cố ngoài giờ.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 95% sự cố có tin chẩn đoán trong vòng 3 phút kể từ cảnh báo đầu tiên; 100% trong vòng 10 phút
  khi chưa chạm trần.
- **SC-002**: Với 10 cảnh báo trong 2 phút thuộc 4 sự cố, người vận hành nhận đúng 4 tin, không phải 10.
- **SC-003**: Ít nhất 95% cảnh báo trùng trong bộ mẫu được gộp đúng sự cố; với bão cảnh báo trùng, số tin gửi
  đi không quá 10% số cảnh báo.
- **SC-004**: 0 sự cố thuộc loại bảo vệ hoặc còn diễn ra bị đóng tự động hay được đề xuất đóng.
- **SC-005**: Tỉ lệ đóng nhầm (bị mở lại) dưới 5% mỗi tháng.
- **SC-006**: 0 giá trị bí mật xuất hiện trong tin, bảng hay nhật ký (kiểm bằng bộ log mẫu có chèn bí mật giả, gồm cả khoá API của mô hình).
- **SC-007**: Ít nhất 80% chẩn đoán mức tin cậy "cao" khớp đánh giá của người vận hành sau 1 tháng.
- **SC-008**: Buổi sáng, người vận hành nắm tình hình đêm qua và gửi xong tin cho dev trong dưới 10 phút.
- **SC-009**: Điều kiện đề xuất bật gửi thẳng cho một dự án: ít nhất 4 tuần, tối thiểu 20 tin, ≥ 90% tin gửi
  nguyên văn hoặc chỉ sửa câu chữ, không có tin nào chẩn đoán sai hướng.
- **SC-010**: 0 cảnh báo bị mất khi hệ thống khởi động lại giữa một bão cảnh báo.

## Assumptions

- Chạy trên homelab trên máy của người vận hành; các nguồn cảnh báo, Grafana và cụm Kubernetes đều thuộc
  homelab (spec nền tảng homelab tách riêng). Muốn xử lý ban đêm thì máy phải bật và không ngủ.
- Nguồn cảnh báo dự kiến: cảnh báo Grafana/Alertmanager (5xx theo endpoint, Pod restart, thiếu replica, tài
  nguyên node), runtime security và audit log của cụm, sự kiện mạng/CNI.
- Kênh tin nhắn là Telegram: một nhóm cảnh báo cho người vận hành; ở giai đoạn 2 mỗi dự án có kênh dev riêng.
- Một người vận hành duy nhất có quyền gõ lệnh; danh sách người được phép mở rộng được.
- Mặc định: tối đa 5 sub-agent đồng thời, cửa sổ gom 60 giây, trần chi phí 2 USD/ngày, mỗi sub-agent tối đa
  5 phút; đều chỉnh được.
- Mô hình chạy qua OpenAI API (khoá API của người vận hành, nạp qua kho bí mật), chỉ nhận đoạn log đã che.
- Danh sách loại bảo vệ mặc định: runtime security, audit log, mất node, lưu trữ, chứng chỉ; chỉnh được.
- Phiên bản này không tự sửa hay restart bất cứ gì; mọi khắc phục do người.
- Bộ cảnh báo mẫu để kiểm thử dựng lại theo định dạng phòng cảnh báo thật người vận hành từng dùng, **đã che
  mọi bí mật**.
