# Feature Specification: Nền tảng homelab Kubernetes mô phỏng môi trường production

**Feature Branch**: `002-homelab-platform`

**Created**: 2026-11-06

**Status**: Draft

**Input**: User description: "Dựng homelab chạy full Docker trên chính máy của tôi: một cụm k3s mini có đủ
tính năng như cụm Kubernetes production (CNI, NetworkPolicy, audit log, runtime security, giám sát, GitOps),
có app mẫu sinh lỗi để agent ở spec 001 có cảnh báo thật mà xử lý. Repo public để làm demo trong CV."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Dựng và gỡ cả homelab bằng một lệnh (Priority: P1)

Người vận hành chạy một lệnh trên máy Windows có Docker Desktop và sau vài phút có một cụm nhiều node, đủ
thành phần nền tảng, mọi thứ lấy từ repo. Một lệnh khác gỡ sạch. Người xem repo (nhà tuyển dụng) làm lại
được trên máy họ theo README.

**Why this priority**: Không có cụm thì không có cảnh báo, không có gì để agent xử lý hay để demo.

**Independent Test**: Trên máy sạch chỉ có Docker Desktop, làm theo README; đo thời gian tới khi mọi thành
phần báo sẵn sàng; gỡ rồi dựng lại, kết quả giống hệt.

**Acceptance Scenarios**:

1. **Given** máy có Docker Desktop và các CLI ghi trong README, **When** chạy lệnh dựng, **Then** có cụm
   1 node điều khiển + 2 node worker, mọi thành phần nền tảng ở trạng thái sẵn sàng.
2. **Given** cụm đang chạy, **When** chạy lệnh gỡ, **Then** không còn container, network hay volume nào
   của homelab trên máy.
3. **Given** một thay đổi cấu hình được commit vào repo, **When** công cụ GitOps đồng bộ, **Then** cụm
   phản ánh thay đổi đó mà không cần lệnh tay lên cụm.

---

### User Story 2 - Quan sát đầy đủ: số liệu, log, trace, cảnh báo (Priority: P1)

Mọi app và thành phần nền tảng có số liệu, log và trace trên Grafana. Các luật cảnh báo chuẩn (5xx theo
endpoint, Pod restart liên tục, Deployment thiếu replica, tài nguyên node cao, Pod bị chặn mạng, sự kiện
runtime security, thao tác audit bất thường) bắn tới agent của spec 001 ngay khi xảy ra.

**Why this priority**: Đây là đầu vào của agent; thiếu nó spec 001 không chạy được.

**Independent Test**: Gây từng loại lỗi bằng app mẫu; kiểm cảnh báo tương ứng bắn ra và tới được đầu nhận
của agent; kiểm Grafana có log/trace cùng khoảng thời gian.

**Acceptance Scenarios**:

1. **Given** app mẫu trả 5xx liên tục, **When** quá ngưỡng, **Then** một cảnh báo 5xx kèm tên app và
   endpoint được gửi tới agent trong vòng 2 phút.
2. **Given** một cảnh báo đã bắn, **When** lỗi hết, **Then** thông báo "đã hết" được gửi tới agent.
3. **Given** một request lỗi, **When** mở Grafana, **Then** đi được từ số liệu → log → trace của chính
   request đó.

---

### User Story 3 - App mẫu sinh lỗi theo yêu cầu (Priority: P1)

Có vài app mẫu thuộc 2 dự án giả định, mỗi app có cả môi trường develop và production. Người vận hành kích
được từng loại lỗi: 5xx ở một endpoint, crash/restart, hết bộ nhớ, chậm, mất kết nối tới dịch vụ phụ thuộc,
và kịch bản "bão" (10 cảnh báo trong 2 phút, 80 lỗi 5xx trùng).

**Why this priority**: Cần lỗi tái hiện được để kiểm thử và demo agent.

**Independent Test**: Kích từng kịch bản, kiểm đúng loại cảnh báo bắn ra với đúng số lượng.

**Acceptance Scenarios**:

1. **Given** cụm đang chạy, **When** kích kịch bản "bão", **Then** trong 2 phút có ít nhất 10 cảnh báo
   thuộc 4 nhóm biết trước.
2. **Given** một kịch bản lỗi đang chạy, **When** người vận hành tắt nó, **Then** app trở lại bình thường
   và cảnh báo chuyển "đã hết".

---

### User Story 4 - Bảo mật ngang production (Priority: P2)

Cụm áp dụng các lớp bảo vệ như production: CNI có NetworkPolicy mặc định chặn và quan sát được luồng mạng;
admission chỉ nhận image từ registry được phép và Pod đạt chuẩn bảo mật; audit log của API server; giám sát
runtime trong container; bí mật chỉ nằm trong repo ở dạng đã mã hoá.

**Why this priority**: Làm homelab sát thực tế và sinh ra các loại cảnh báo bảo mật cho agent, nhưng không
chặn luồng chính.

**Independent Test**: Thử từng hành vi bị cấm và kiểm nó bị chặn kèm cảnh báo.

**Acceptance Scenarios**:

1. **Given** NetworkPolicy mặc định chặn, **When** một Pod gọi tới Pod không được phép, **Then** kết nối
   bị chặn và luồng bị chặn nhìn thấy được trên công cụ quan sát mạng.
2. **Given** một manifest dùng image ngoài registry được phép hoặc chạy root, **When** apply, **Then** bị
   từ chối.
3. **Given** một shell được mở trong container, **When** sự kiện xảy ra, **Then** có cảnh báo runtime
   security tới agent.
4. **Given** ai đó liệt kê Secret bằng tài khoản quản trị, **When** sự kiện được ghi vào audit log, **Then**
   có cảnh báo audit tới agent.

---

### User Story 5 - Tài liệu và repo đủ để làm portfolio (Priority: P2)

Người đọc repo hiểu được luồng homelab trong 5 phút: sơ đồ kiến trúc, luồng cảnh báo, các quyết định thiết
kế và lý do, cách chạy, cách demo. Không có bí mật hay thông tin của tổ chức khác.

**Why this priority**: Repo là sản phẩm trưng bày trong CV.

**Independent Test**: Một người chưa biết dự án đọc README rồi trả lời được: cụm có gì, cảnh báo đi đường
nào, agent làm gì, chạy thử thế nào.

**Acceptance Scenarios**:

1. **Given** repo public, **When** chạy công cụ quét bí mật trên toàn bộ lịch sử Git, **Then** không có phát hiện nào.
2. **Given** README, **When** người đọc làm theo phần chạy thử, **Then** họ thấy được một tin chẩn đoán trên
   Telegram từ một lỗi họ tự kích.

---

### Edge Cases

- Máy thiếu RAM: lệnh dựng kiểm trước tài nguyên trống và dừng với thông báo rõ, không dựng dở dang.
- Docker Desktop chưa bật: lệnh dựng báo rõ, không treo.
- Máy khởi động lại: cụm lên lại được bằng một lệnh, dữ liệu giám sát gần nhất còn giữ.
- Tải image chậm hoặc lỗi mạng: lệnh dựng thử lại có giới hạn, báo thành phần nào chưa lên.
- Người xem repo không có Telegram bot hay khoá OpenAI: homelab vẫn dựng được; agent chạy chế độ demo không
  gọi ra ngoài.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Homelab MUST chạy hoàn toàn trong Docker trên một máy Windows 11 có Docker Desktop (WSL2),
  không cần dịch vụ cloud nào ngoài Telegram và nhà cung cấp mô hình của spec 001.
- **FR-002**: Cụm MUST có 1 node điều khiển và 2 node worker, là Kubernetes đạt chuẩn conformance.
- **FR-003**: Toàn bộ cấu hình nền tảng và app MUST nằm trong repo và được đồng bộ vào cụm bằng GitOps; chỉ
  bước khởi tạo ban đầu được chạy bằng lệnh.
- **FR-004**: Cụm MUST có CNI hỗ trợ NetworkPolicy và quan sát luồng mạng (gồm luồng bị chặn); mỗi namespace
  app MUST có NetworkPolicy mặc định chặn.
- **FR-005**: Cụm MUST có admission policy: chỉ image từ registry trong danh sách cho phép, Pod đạt chuẩn
  bảo mật "restricted" ở namespace app, cấm tag `latest`.
- **FR-006**: API server MUST ghi audit log; audit log MUST vào được hệ thống log tập trung.
- **FR-007**: Cụm MUST có giám sát runtime security trong container, sự kiện đẩy tới agent.
- **FR-008**: Cụm MUST có số liệu, log tập trung và trace phân tán, xem trên Grafana, nối được số liệu ↔ log ↔ trace.
- **FR-009**: Cụm MUST có các luật cảnh báo: 5xx theo app/endpoint, Pod restart liên tục, Deployment thiếu
  replica, CPU/RAM node cao, Pod OOMKilled, luồng mạng bị chặn bất thường, sự kiện runtime security, thao tác
  audit nhạy cảm (đọc/liệt kê Secret ngoài các controller đã biết, `exec` vào Pod).
- **FR-010**: Mọi cảnh báo và thông báo "đã hết" MUST được đẩy tới đầu nhận của agent spec 001 trong nội bộ cụm.
- **FR-011**: Homelab MUST có app mẫu thuộc 2 dự án, mỗi app có develop và production, có endpoint kích lỗi
  (5xx, crash, OOM, chậm, mất phụ thuộc) và một lệnh chạy kịch bản "bão".
- **FR-012**: Bí mật MUST chỉ nằm trong repo ở dạng đã mã hoá cho đúng cụm, hoặc dạng placeholder; giá trị
  thật nạp từ file `.env` cục bộ không commit.
- **FR-013**: Truy cập giao diện (Grafana, GitOps, quan sát mạng) MUST chỉ từ localhost, qua TLS.
- **FR-014**: Mọi image và chart MUST ghim phiên bản cố định.
- **FR-015**: Workload tự viết MUST chạy non-root, không leo quyền, filesystem chỉ đọc khi được, bỏ mọi
  capability, có requests/limits.
- **FR-016**: Repo MUST có README tiếng Anh với sơ đồ kiến trúc, luồng cảnh báo, hướng dẫn chạy, kịch bản
  demo; tài liệu quyết định thiết kế; không chứa bí mật hay thông tin tổ chức khác.
- **FR-017**: Repo MUST có CI: lint manifest/chart, quét bí mật, SAST, quét dependency và image, SBOM.
- **FR-018**: Lệnh dựng MUST kiểm trước điều kiện (Docker chạy, RAM trống, CLI cần thiết) và báo rõ thiếu gì.

### Key Entities

- **Cụm (Cluster)**: các node, CNI, thành phần nền tảng, phiên bản.
- **Thành phần nền tảng (Platform Component)**: tên, phiên bản ghim, namespace, vai trò.
- **App mẫu (Sample App)**: dự án, môi trường, endpoint kích lỗi.
- **Luật cảnh báo (Alert Rule)**: điều kiện, mức, nhãn app/dự án/cụm, đích gửi.
- **Kịch bản lỗi (Fault Scenario)**: chuỗi lỗi kích theo thời gian, cảnh báo kỳ vọng.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Từ máy đã có image cache, dựng xong toàn bộ homelab trong dưới 15 phút; gỡ trong dưới 2 phút.
- **SC-002**: Homelab dùng không quá 10 GB RAM khi chạy ổn định, để máy 24 GB vẫn làm việc bình thường.
- **SC-003**: 100% loại lỗi trong FR-009 kích được bằng app mẫu hoặc thao tác ghi trong README, và đều sinh
  cảnh báo tới agent trong vòng 2 phút.
- **SC-004**: Người chưa biết dự án làm theo README thấy được tin chẩn đoán đầu tiên trên Telegram trong dưới
  30 phút (không tính thời gian tải image).
- **SC-005**: 0 phát hiện từ công cụ quét bí mật trên toàn bộ lịch sử Git.
- **SC-006**: 0 workload tự viết vi phạm chuẩn bảo mật Pod "restricted".

## Assumptions

- Máy chủ homelab: Windows 11, 24 GB RAM, 12 luồng CPU, Docker Desktop (WSL2). Giới hạn RAM cho WSL khoảng
  12 GB.
- Lựa chọn công cụ (người vận hành đã chốt, chi tiết và lý do ở `docs/decisions.md`): k3s chạy trong Docker
  bằng k3d; Cilium + Hubble thay CNI mặc định; Falco thay Wazuh vì RAM; Kyverno cho admission; ArgoCD cho
  GitOps; Prometheus + Alertmanager + Grafana + Loki + Tempo + Alloy cho giám sát; Sealed Secrets cho bí mật;
  Traefik + Gateway API làm cửa vào.
- AKS chỉ là phương án dự phòng nếu máy không đủ tài nguyên; không nằm trong phạm vi bản này.
- Không có tên miền public; truy cập qua localhost.
- Registry được phép: một registry cục bộ do k3d tạo, cộng các registry public cần cho thành phần nền tảng
  (ghim theo digest/phiên bản).
- Repo trên GitHub, CI bằng GitHub Actions.
