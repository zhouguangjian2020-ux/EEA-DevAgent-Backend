# TS Backend

## 工作流API增量

本轮源代码已实现，未修改公司部署。需求：[45](<E:/Git projects/TS-frontend/docs/current/45_FR生成_FR分配_项目多会话_开发需求规格.md>)；契约：[46](<E:/Git projects/TS-frontend/docs/current/46_FR与项目对话_接口契约及Agent联调.md>)；联合验收与后续部署边界：[47](<E:/Git projects/TS-frontend/docs/current/47_FR生成_FR分配_项目多会话_联合交付与验收.md>)。下方带日期内容保留历史语境，以47号本批次记录为准。


FastAPI认证服务。2026-09-21新增PRD自动扩写转接；2026-09-22新增复用ee.projects的项目只读GET接口。2026-09-23新增手动功能/PRD/UC/FR创建、追加修订和显式接受，详见下方当前增量；公司部署另行验收。

## 环境

项目使用本地虚拟环境 `.venv`，不是全局 Python：

```powershell
.\.venv\Scripts\Activate.ps1
```

如果 PowerShell 禁止脚本执行，可以直接调用：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

## 配置

```powershell
Copy-Item .env.example .env
```

演示前请修改 `.env` 中的 `DEMO_PASSWORD` 和 `SECRET_KEY`。

## 启动

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

接口文档：

- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc
- OpenAPI: http://localhost:8000/openapi.json

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## 2026-09-29 PRD 自动扩写接口

已接通 PRD 输入框自动扩写的业务后端桥接，且不修改 Agent 软件、不写业务数据库、不把 Agent API Key 下发浏览器。

- 缓冲接口：`POST /v1/agent/prd/expand`，返回完整 JSON，保留兼容旧调用。
- 流式接口：`POST /v1/agent/prd/expand/stream`，要求登录 Cookie，向浏览器返回清洗后的 SSE `token`、`done`、`error` 事件。
- 上游请求：`{"message":"用户输入","scene":"PROMPT_EXPAND"}`。
- `AGENT_INVOKE_URL` 推荐配置为 `http://192.168.64.3:8300/api/v1/agent/invoke/stream`；适配器同时支持 `/invoke` 的 `application/json` 一次性响应。
- 浏览器只访问同源业务后端，Agent Key 只从后端 `.env` 的 `AGENT_API_KEY` 读取；禁止写入 `NEXT_PUBLIC_*`、前端源码、README、测试日志或 Git。
- 前端自动扩写实时消费 SSE token 并回填输入框；不自动发送、不自动保存、不创建对象。停止/失败恢复原输入，减少动态效果偏好下在 `done` 后一次性回填。

验证（2026-09-29）：后端全量 `186 passed, 118 skipped`；前端全量 `204 passed`；`pnpm lint` 0 errors（保留 2 条既有 `<img>` warning）；`pnpm typecheck`、`pnpm build` 通过。真实本地链路已验证登录 200、业务 SSE 200、多个 token、`done.scene=PROMPT_EXPAND`，密钥未输出。

详细契约见：`E:\Git projects\TS-frontend\docs\current\10_PRD自动扩写接口联调.md`。

## 2026-09-29 PRD 生成功能定义流式接口

已新增 `POST /v1/agent/prd/generate/stream`，用于从数据库中的功能定义调用 Agent 的 `FROM_DEFINITION_CREATE_PRD`。本接口只读取项目和功能，不创建或保存 PRD。

- 浏览器请求只提交 `project_id`、`function_id` 和可选 `version`，不提交项目名称或功能正文。
- 后端通过只读项目数据库校验当前用户可访问的项目，并读取项目名称、项目简介、功能名称和功能描述。
- Agent 请求中的 `inputs.project` 使用数据库实际项目名称，不硬编码为“通用”；`message` 同时明确携带项目、功能和版本上下文。
- Agent 的逐行 JSON 流会被转换为浏览器可消费的标准 SSE，包含 `token`、`done`、`error`。
- Agent API Key 只从后端 `.env` 读取，绝不下发浏览器。
- 当前版本不写正式 PRD；前端收到 `done.output` 后再接入 PRD 草稿/确认流程。
- 由于真实 Agent PRD 生成约需 2～3 分钟，`AGENT_TIMEOUT_SECONDS` 建议配置为 `300`。

详细契约见：`E:\Git projects\TS-frontend\docs\current\34_PRD生成流式接口联调_20260929.md`。
## 2026-09-22 项目只读接口（历史增量）

- GET /v1/projects?limit=20&offset=0：当前用户有project.read权限的未归档项目，items/limit/offset/has_more。
- GET /v1/projects/{project_id}：同权限过滤的单项目，未找到和无权限统一404。
- 当时未提供POST/PUT/PATCH/DELETE项目接口；后续项目创建见下一节，不新增/重建ee.projects，不返回原始metadata、created_by或数据库错误。
- Cookie验证后的demo JWT subject映射ee.app_users.external_subject；用户/成员active、角色/能力enabled及项目级project.read绑定均须成立，不从查询参数接收身份。
- 配置PROJECTS_DATABASE_URL（SecretStr，仅后端）和可选PROJECTS_CONNECT_TIMEOUT_SECONDS；未配置时项目接口503，其余健康/认证服务照常。禁止用postgres或ee_m0_owner连接；专用ee_project_api_ro只授予项目读取接口所需25张表的SELECT，没有DML；工作空间聚合查询的表清单由前端工程的独立只读授权包维护。
- 当前默认本地pytest为64 passed、14 skipped；在全新隔离PG18.6实例上完成schema/seed、样本项目、25表只读授权、真实HTTP workspace读取和29张业务表不变检查，78 passed、1 warning；不是公司后端8000服务或浏览器页面验收的完成回执。

完整项目表映射、样本改名/关系保留、限定授权、只读数据库账号、SSH隧道、私密配置和手工验证命令见：
`E:\Git projects\TS-frontend\docs\current\17_测试项目1配置与项目只读接口.md`。

正常`python -m pytest -q`执行默认测试；14项真实PG测试仅在显式设置TEST_PROJECTS_ADMIN_DSN与TEST_PROJECTS_READER_DSN且目标为127.0.0.1:56428、ee_project_read_前缀的隔离库时执行，否则跳过。测试覆盖配置与真实.env隔离，不消耗真实Agent请求。测试证据在artifacts/projects-read-20260922。



## 2026-09-22 项目创建接口

已实现 `POST /v1/projects`：在现有项目切换窗口创建空项目。请求需要现有HttpOnly Cookie、允许的Origin和`X-Project-Request: 1`，创建内容为项目名称和可选说明；编号由后端按creation_id生成，原请求UUID重试不会重复创建。新项目只初始化创建人成员、项目级读取绑定、空子系统方案入口和审计事件，不复制功能/PRD/UC/FR/子系统。

`PROJECTS_DATABASE_URL`继续使用`ee_project_api_ro`只读连接；`PROJECTS_WRITE_DATABASE_URL`必须使用共享的`ee_project_api_rw`低权限连接（由既有`ee_project_api_create`原位改名，不创建第二个登录）。旧项目创建授权原为8张表SELECT和5张表INSERT；2026-09-23手动流程增量扩展同一登录，不拥有DELETE/DDL，也不接受浏览器传入的创建人、成员、角色、编号或metadata。

公司数据库的账号授权由前端工程当前文档中的独立包执行，不由后端自动SSH或重启服务。


## 2026-09-23 当前增量：手动功能与需求维护

- POST /v1/projects/{project_id}/functions
- POST /v1/projects/{project_id}/functions/{function_id}/artifacts（PRD/UC/FR）
- POST /v1/projects/{project_id}/artifacts/{artifact_id}/revisions
- POST /v1/projects/{project_id}/artifacts/{artifact_id}/accept

新增业务写默认关闭：BUSINESS_WRITES_ENABLED=false。复用PROJECTS_WRITE_DATABASE_URL中的ee_project_api_rw（既有ee_project_api_create原位改名），绝不把原SELECT-only账号扩成写账号。先审核执行前端docs/current/database/manual-work-20260923包并绑定目标项目业务角色，再显式打开开关；运行时不自动迁移/授权、不创建数据库账号。

新增项目在该开关启用且角色就绪后给创建人project_api_author业务角色（不是PG账号），否则保持原project_api_reader行为。已有项目不会自动升级。每次对象写入重新验证项目成员、角色和具体动作权限，Cookie/Origin/JSON规则复用现有路径。

保存生成不可变版本；接受才切换accepted指针。创建及写入支持稳定UUID幂等，旧基线409不覆盖，D06/D07在R2触发器内继续校验。FR验收标准为修订metadata扩展，不改冻结schema/type definition。

本地115项通过，原只读/创建PG回归24项单独通过，真实浏览器端到端通过；不是公司上线回执。运行tests/run_manual_postgres.py；加--legacy在改名后权限策略上回归原读取/项目创建（业务写开关关闭），--browser运行独立3107/8107前后端。测试只使用指定localhost隔离端口和ee_manual_/ee_project_read_前缀，创建后自动停服务。

详细入口、HTTP契约、精确授权、回退与证据：E:/Git projects/TS-frontend/docs/current/21_手动功能PRD_UC_FR实现与部署.md。


## 2026-09-24 数据库账号改名 r2

只读DSN只接受ee_project_api_ro，写DSN只接受ee_project_api_rw；旧名/读写DSN互换/高权限或成员关系均拒绝。迁移先核对目标实例、有效SCRAM、旧8SELECT/5INSERT基线及无活跃写会话，再原位改名与授权；密码不回传、不自动重置。

公司数据库只由用户运行分阶段命令，助手不得直接访问。完整交接及命令见前端docs/current/22_数据库账号改名与一期迁移交接_20260924.md。冻结旧项目创建包仅用于历史/隔离基线，迁移后不得重新执行旧开户包。

本次最终验证：30项迁移安全、136项后端业务/单测、24项原数据库回归分别通过，浏览器完整链路通过。证据`artifacts/writer-rename-20260924/20260924-105003-133070`与`20260924-104927-223956`；前端126项及所有质量门禁通过。正式r2包SHA256：`fdb3453f0183ffcaf2dd98d7e02e29612f882bc1929bbd7f88d8c145caec8580`。公司操作仍待用户只读回执。
## 2026-09-29 首版项目权限与账号管理

已实现精简的项目权限页面和数据库账号管理接口：

- 项目角色固定为 `project_api_author`（界面显示“项目管理员”）和 `project_api_reader`（“项目查看者”）。
- 功能角色固定为 `function_viewer`（“功能查看者”）和 `function_editor`（“功能编辑者”）；工程审查和确认包含在功能编辑者中。
- 权限按当前项目隔离；功能级权限只对指定功能生效；撤销功能级权限后回退到项目级角色。
- `GET/POST/PATCH /v1/accounts...` 用于账号列表、创建和启停；登录凭据保存在 `ee.app_credentials` 的 Argon2id hash 中，停用账号不能登录。
- `GET/POST/DELETE /v1/projects/{project_id}/permissions...` 及成员/功能权限接口用于项目管理员维护成员和功能授权。
- 启用真实权限管理前，必须完成前端仓库中的 `docs/current/database/permission-admin-20260929` 现场检查和数据库增量，并配置 `ACCOUNT_ADMIN_SUBJECTS`、`PROJECTS_DATABASE_URL`、`PROJECTS_WRITE_DATABASE_URL` 与 `BUSINESS_WRITES_ENABLED`。公司数据库执行仍由用户/DBA现场完成，后端不会自动SSH、迁移或创建账号。
- 第一版不包含SSO、找回密码、自由权限矩阵、显式拒绝规则和复杂组织架构。

详细实现、测试和未部署边界见：
`E:\Git projects\TS-frontend\docs\current\33_权限管理实现与验证_20260929.md`。


## 2026-10-02 项目创建权限兼容性修复

现场只读回执确认：业务写入开启、engineer01允许创建且活跃，project_api_author包含20项启用权限（含project.manage_access），project_api_reader只有project.read。旧创建校验未兼容权限管理增量，误报project_create_role_not_ready；不是缺少reader角色。

修复仅限app/services/project_creation.py：接受metadata/deletion完整集合分别加project.manage_access的已审核组合；保持精确权限集合、enabled/scope/resource/action校验和reader限制；新建返回正确can_manage_access；更正误导性文案。新增16项本地替身回归，修复前2失败、修复后全部通过；完整回归278 passed、120 skipped，未运行公司数据库测试。

单文件镜像覆盖包位于tmp/project-create-role-fix-20261002-r1.tar.gz，基于ts-agent-backend:20260930-uc-r2，只覆盖创建服务源码，不包含其他本地诊断改动。包内README.md说明用户手动构建和后续切换前置检查；构建不会切换运行服务。未执行公司上传、SSH、Docker构建/重启或数据库写入。
