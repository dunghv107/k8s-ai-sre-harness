# k8s-ai-sre-harness Constitution

## Core Principles

### I. Chỉ đọc là mặc định (Read-Only by Default)

- Mọi thông tin xác thực của agent MUST là quyền tối thiểu, chỉ đọc: đọc log Pod và trạng thái
  Pod/Event trên cụm; quyền xem (Viewer) trên Grafana; đọc tin trong phòng chat cảnh báo.
- Agent MUST NOT có quyền `exec`, đọc Secret, sửa/xoá/scale/restart bất kỳ tài nguyên nào,
  hay quyền ghi vào Git/GitOps.
- Hành động ghi duy nhất được phép là những hành động nằm trong **danh sách cho phép** được khai
  rõ trong cấu hình (vd đăng bảng tóm tắt, gửi lệnh đóng cảnh báo trùng). Hành động ngoài danh
  sách MUST bị từ chối và ghi log.

Lý do: agent chạy ngoài giờ, không ai giám sát; một token rò rỉ hay một suy luận sai không được
phép biến thành thay đổi trên hạ tầng.

### II. Con người quyết định (Human-in-the-Loop)

- Agent chẩn đoán, gộp, xếp hạng và soạn thảo; người vận hành quyết định sửa gì và báo ai.
- Mọi hành động tự động (vd đóng cảnh báo trùng) MUST đi kèm lý do, có thể đảo ngược, và hiện
  trong bảng sáng hôm sau để người vận hành xem lại.
- Agent MUST NOT tự sửa cấu hình, deploy, hay mở/đóng quyền truy cập.

### III. Bằng chứng trước kết luận (Evidence First)

- Mỗi kết luận MUST trích nguồn: ID cảnh báo, đoạn log (đã che bí mật), truy vấn hoặc đường dẫn
  Grafana, khoảng thời gian.
- Khi dữ liệu không đủ, agent MUST ghi "chưa đủ bằng chứng" kèm thứ còn thiếu, không đoán nguyên
  nhân gốc.
- Mức tin cậy (cao/trung bình/thấp) MUST đi cùng mọi chẩn đoán.

### IV. Bí mật và dữ liệu nhạy cảm (Secrets & Data Hygiene)

- Token, mật khẩu, khoá, cookie, header Authorization và dữ liệu cá nhân trong log MUST được che
  trước khi đưa vào mô hình, bảng tóm tắt hay tin nhắn.
- Thông tin xác thực của agent MUST đến từ kho bí mật (Vault + External Secrets hoặc
  SealedSecrets); trong repo chỉ có placeholder. Không in giá trị bí mật ra log hay output.
- Dữ liệu log gửi ra ngoài lab mô phỏng production chỉ được phép theo đúng phạm vi đã chốt trong spec.

### V. Gộp trước, phân tích sau (Dedupe Before Analysis)

- Cảnh báo MUST được gộp theo dấu vân tay (nguồn + app + loại lỗi + endpoint) trước khi giao
  cho sub-agent; một nhóm sự cố = một sub-agent, không phải một cảnh báo = một sub-agent.
- Mỗi lượt chạy MUST có trần: số sub-agent, thời gian và chi phí. Vượt trần thì dừng phần còn
  lại, ghi rõ nhóm nào chưa phân tích, không im lặng bỏ qua.

### VI. Hạ tầng của người vận hành, nghiệp vụ của dev (Ownership Boundary)

- Lỗi hạ tầng (node, mạng/CNI, lưu trữ, chứng chỉ, quota, image) → chẩn đoán cho người vận hành.
- Lỗi nghiệp vụ của app (exception, 5xx do code, dữ liệu sai) → chẩn đoán rồi soạn tin cho dev
  dự án; agent không đề xuất sửa code thay dev.
- Mỗi nhóm sự cố MUST được gắn một trong hai nhãn này, hoặc "chưa rõ".

## Ràng buộc bảo mật và nền tảng

- Workload của agent trên Kubernetes: `runAsNonRoot: true`, `allowPrivilegeEscalation: false`,
  `readOnlyRootFilesystem: true`, `capabilities.drop: ["ALL"]`, `seccompProfile: RuntimeDefault`;
  khai đủ `resources.requests/limits`; có NetworkPolicy mặc định chặn, chỉ mở tới API cụm,
  Grafana, máy chủ chat và nhà cung cấp mô hình (nếu được phép).
- RBAC: ServiceAccount riêng, Role/ClusterRole chỉ `get`/`list`/`watch` trên `pods`, `pods/log`,
  `events`; không wildcard, không `cluster-admin`.
- Image pin tag cố định hoặc digest, từ registry nội bộ; không `latest`.
- Mọi kết nối dùng TLS và kiểm chứng chứng chỉ.
- Pipeline CI có SAST, SCA, quét image và SBOM; không tắt bước quét để pipeline xanh.

## Quy trình phát triển

- Spec-Driven: `/speckit-specify` → `/speckit-clarify` → `/speckit-plan` → `/speckit-tasks` →
  `/speckit-implement`. Không viết code trước khi spec được chốt.
- Repo là nguồn sự thật cho cấu hình; không dán cấu hình thẳng lên server.
- Kiểm thử bằng mock trước khi chạy thật: Grafana, máy chủ chat, API cụm và mô hình giả lập, có
  bộ cảnh báo mẫu (gồm bão cảnh báo trùng) để đo kết quả gộp.
- AI trợ lý không tự `git commit`/`git push`; không tự SSH/kubectl lên cụm, chỉ đưa lệnh.
- Comment trong code chỉ bằng tiếng Anh, một dòng, chỉ ghi lý do mà code không tự nói được; không comment cả
  đoạn văn. Chỗ làm tắt có giới hạn ghi `shortcut: <giới hạn>, <khi nào nâng cấp>`.

## Governance

- Constitution này đứng trên mọi spec/plan/tasks của dự án; plan MUST có mục "Constitution Check".
- Sửa đổi: ghi lý do, tăng phiên bản theo semver (MAJOR: bỏ/định nghĩa lại nguyên tắc; MINOR: thêm
  nguyên tắc hoặc mở rộng đáng kể; PATCH: sửa câu chữ), cập nhật ngày.
- Nếu xung đột với quy tắc bảo mật toàn cục của người vận hành, quy tắc chặt hơn thắng.

**Version**: 1.0.0 | **Ratified**: 2026-11-06 | **Last Amended**: 2026-11-06
