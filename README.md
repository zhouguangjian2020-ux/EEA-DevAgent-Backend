# 天枢 EE 开发平台 · Backend

FastAPI / Python 应用代码仓库。

## 代码与构建

- `app/`：接口、业务服务、模型与配置代码。
- `tools/`：现有 Dockerfile 引用的配套工具；本次未移除或改写。
- `requirements.txt`、`Dockerfile`、`.env.example` 保留。
- 实际 `.env` 与本地虚拟环境不进入 Git；不要提交数据库凭据或 Agent 密钥。

## 2026-10-09 资料分离

测试脚本、pytest 配置、历史说明和大部分验证产物保存在本机：
`E:\Git projects\EEA-DevAgent-support-20261009\TS-backend`

数据库增量和前后端联合交付材料在同一资料目录的 `TS-frontend/docs`、`TS-frontend/deploy` 和 `TS-frontend/artifacts` 下。
测试没有删除；重新使用前应在隔离工作目录恢复原目录关系，不要直接使用公司数据库运行测试。

此次未更改 `app/`、依赖或 Dockerfile，也未部署公司服务。公司当前候选 `20261009-workflows-rc1` 的可复现构建以冻结发布包为准；不要把仓库历史 Dockerfile 自动视为该候选的构建配方。

注意：从工作区移出文件不会清除 Git 历史；推送现有历史仍可能包含以前提交过的文档和测试。