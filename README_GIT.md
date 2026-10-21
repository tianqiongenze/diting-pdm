# 谛听 Diting · 设备预测性维护智能体平台

> 应用主体：雅迪电动车集团公司（全球近 20 个生产基地）及上下游供应商
> 演进目标：行业包 SPI，可迁移至汽车制造等离散制造集团
> 状态：Ph1 核心切片已运行（E2E 23/23 通过，1 亿行数据，多级缓存+warmup）

## 快速导览

| 目录 | 内容 |
|---|---|
| `docs/00–21` | 21 份工程文档：总览/可研/SRS/架构/数据模型/Harness/接口/安全/测试/DevOps/路线/验收/标准/自评/雅迪场景/DDD-CQRS/多语言/存储ADR/K8s修复/性能/测试报告 |
| `specs/` | SDD 规范：项目宪法 + SPEC-001/002/003（含 Gherkin） |
| `impl/` | 可运行工程：信号核/诊断服务（Litestar+Granian）/TiDB DDL/K8s 清单/CI |
| `export/` | 全部文档的 Word + PDF 导出（64 个文件） |
| `DECISIONS.md` | D1–D25 决策基线（已确认） |
| `index.html` / `index-v2.html` | 可视化总览 |

## 技术栈

Python（Litestar/Granian/msgspec）· Rust(规划) · Go(规划) · Java(规划) ｜ TiDB + TiFlash · Pulsar · Redis · Milvus · NebulaGraph ｜ K8s 1.28.2

## 运行

见 `impl/README.md`。数据层：TiDB `pdm` 库 18 张表；入口：`/pdm-api/*`。
