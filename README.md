# 办事材料预检智能体

> 本 README 为项目补充说明文档，通过 Pull Request 添加。对应 Issue：`缺少 README，建议补充项目说明`。

## 项目简介

「办事材料预检与服务协同」智能体，面向政务服务大厅的**只读核对 + 人工交接**辅助系统。
系统只做两件事：**清单一致性预检**、**补正解释提示**，不代替行政审批、不回写业务系统、
不导入真实个人材料，AI 结论一律挂人工确认单后由接手岗位确认。

## 技术栈

| 模块 | 说明 |
| --- | --- |
| Web 框架 | FastAPI |
| 运行时 | Python 3.11+ |
| Agent 运行壳 | 项目内自研 `AgentHarness`（八步固定编排） |
| 知识库 | 规则库 `kb/rules.json` + 知识图谱 `kb/graph.json` |
| 本地大模型 | Ollama（`127.0.0.1:11434`，未启动时自动降级） |

## 目录结构

```
docs/        技术方案与调研材料
materials/   现场调研报告、现状流程图等
```

## 快速开始

```bash
# 1. 克隆
git clone https://github.com/wzk-12138/gov-material-precheck.git
cd gov-material-precheck

# 2. 安装依赖（按 docs/技术方案.md 的 requirements）
pip install -r requirements.txt

# 3. 启动服务
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 设计原则

- 统一响应：所有接口返回 `{"ok": bool, "data": ..., "error": {"code", "msg"}}`
- 安全三道关卡：权限校验 → 输入安全审查 → 输出审查
- 配置集中：密钥、模型地址、阈值一律在 `config.yaml`，不写死在代码
- 不引入需求外中间件：无数据库、无向量库、无消息队列

## 相关文档

- `docs/技术方案.md` —— 完整技术方案
- `materials/办事材料预检与服务协同-业务挖掘报告.md` —— 业务背景
