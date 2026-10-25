# 007 · MCP 规范对齐与安全（Feature Spec）

| 项 | 内容 |
|---|---|
| Spec ID | SPEC-007 |
| 关联需求 | docs/05 §5、docs/43 §10.1/10.5、Phase D |
| 优先级 | P1 |
| 状态 | **Implemented** |
| 负责人 | 平台 |

## 1. Why

MCP 此前是 2024-11-05 旧协议、仅 5 个 tools、无 resources/prompts，
且与 Harness 工具实现**双路径**（`_call_tool` 手写 5 个分支）——改一处漏一处。
2025 年 MCP 生态曝出工具投毒、令牌泄露、SSRF 等漏洞，工业场景必须加固。

## 2. 需求

| ID | 类型 | 需求 | 验收标准 |
|---|---|---|---|
| FR-701 | Ubiquitous | 协议版本升至 2025 规范 | `protocolVersion == "2025-03-26"` |
| FR-702 | Ubiquitous | capabilities 声明 tools/resources/prompts | initialize 返回三能力 |
| FR-703 | Ubiquitous | tools 契约与 Harness 注册表同源 | 集合完全相等（12 工具） |
| FR-704 | Ubiquitous | 提供只读 Resources（判据/案例/热点设备） | `resources/list` + `resources/read` |
| FR-705 | Ubiquitous | 提供受控 Prompts（会诊/分诊） | `prompts/list` + `prompts/get` |
| FR-706 | Unwanted | 工具必须白名单（防投毒） | 未注册工具返回 -32602 |
| FR-707 | Unwanted | 资源 URI 必须白名单（防 SSRF） | 任意 http:// URI 被拒 |
| FR-708 | Unwanted | 缺租户必须全方法拦截 | 四个方法均返回 -32001 |
| FR-709 | Ubiquitous | 工具调用必须审计 | `_audit_tool_call` 被调用 |
| FR-710 | Unwanted | 不提供 sampling（边缘防数据外泄） | 无 sampling 方法 |

## 3. 设计要点

- **单一定义源**：`TOOLS = [t.public_spec() for t in harness.list_tools()]`
- **委托执行**：`_call_tool` 直接调 `harness.tools.execute`，不再手写分支
- **降级不崩**：resources/prompts 在库不可达时返回 `degraded` 结构而非 500
- **审计失败不影响调用**：遥测性写入，异常吞掉

## 4. 验收映射

| 需求 | 测试 |
|---|---|
| FR-701/702 | `test_protocol_version_upgraded` |
| FR-703 | `test_tool_list_matches_registry_exactly`、`test_tools_list_count` |
| FR-704/705 | `test_resources_list_and_read`、`test_prompts_list_and_get` |
| FR-706 | `test_tool_allowlist_rejects_unregistered` |
| FR-707 | `test_resource_uri_whitelist` |
| FR-708 | `test_missing_tenant_rejected_on_all_methods` |
| FR-709 | `test_audit_written_on_tool_call` |
