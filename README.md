# MBSESAI 辅助建模原型（Python）

> 新增真实技术栈版本：MySQL、Redis、Milvus、千问生成与Embedding API。见[真实技术栈说明](真实技术栈说明.md)。下文保留旧离线模式说明；两个模式的数据和验证结果不得混用。

基于原始《电动牙刷案例说明20240828.docx》跑通“文档入库、检索、JSON草稿、校验、人工审核、正式模型保存、可靠度复算”。默认完全离线，不需要Java、API Key或Docker。

**定位：可运行的AI应用工程原型，不是自主多智能体，不是MBSES插件，也不声称输出已经通过SysMLv2工具验证。**

**公开仓库只包含源代码、测试和脱敏说明，不包含公司案例原文、私有案例摘要、API Key、数据库或运行产物。** 原文验收需自行准备有权使用的案例文件，通过`--source`指定；也可在被忽略的`.env.real`中设置`MBSES_CASE_SOURCE`。未配置时默认读取`private/toothbrush_case.docx`，该目录不会提交。

## 1. 本次选型

| 模块 | 当前实际运行 | 可选接入（未做外部服务联调） |
|---|---|---|
| API与页面 | Python 3.11、FastAPI、原生HTML/JS | 无需Java或Spring |
| 文档解析 | python-docx、pypdf | OCR另行评估 |
| 文本表示 | 确定性字符哈希向量，1024维 | 百炼text-embedding-v4 |
| 召回 | NumPy余弦全量扫描，向量持久化到SQLite | Milvus FLAT + COSINE |
| 生成 | 电动牙刷文字事实规则提取器 | 通义千问qwen-plus，OpenAI兼容HTTP接口 |
| 数据 | SQLAlchemy + SQLite | MySQL + PyMySQL |
| 缓存 | 共享进程内TTL缓存 | Redis |
| 输出与校验 | Pydantic、JSON Schema、业务规则 | 标准SysMLv2解析/工具导入需后续实现 |

小案例只有88块，不需要HNSW、Rerank、LangGraph、多Agent或分布式锁。先用可复查的基线验证链路，再根据真实检索评测决定是否引入复杂组件。

## 2. 本机快速使用

PowerShell，工作目录为本文件所在文件夹：

```powershell
$env:PYTHONUTF8='1'
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe scripts/run_demo.py --source 'C:\path\to\your-case.docx'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

页面：[http://127.0.0.1:8000](http://127.0.0.1:8000)，接口：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)。端口被占用时换一个，例如8160。

`run_demo.py`每次创建新演示项目。它会调用本应用的HTTP接口并执行模拟审核确认；不是现实中工程师已经审批的意思。浏览器演示则必须手动点击确认。

```powershell
# 指定另一份原文的位置
.\.venv\Scripts\python.exe scripts/run_demo.py --source 'C:\路径\电动牙刷案例说明20240828.docx'
# 仅入库，不自动生成或确认
.\.venv\Scripts\python.exe scripts/ingest_docs.py 'C:\路径\说明.docx' --project '我的项目'
# 自动化测试（独立临时数据库，不修改演示数据）
.\.venv\Scripts\python.exe -m pytest -q
```

数据默认保存在绝对路径`.data/mbses.db`，不会读写旧版根目录下的`mbses.db`；没有旧库自动迁移功能。原始上传件保存在`.data/uploads/`，不要上传涉密或未经授权文档。

## 3. 页面操作

1. 选择“电动牙刷案例完整演示”，能查看原文与已确认模型。
2. 输入需求，生成新的草稿；或用`artifacts/run-report.json`中的任务ID加载既有任务。
3. 展开依据片段，核对文档名、段落号和原文。`P091`表示第91个顶层段落（包括空段落计数），不是页码。
4. 修改JSON后点击“重新校验”，再点击“人工确认”。未校验、存在错误、存在待澄清问题时禁止确认。
5. 切换“拓扑、元素、关系”查看正式结果。拓扑是内部模型示意图，不是原文44张模型视图的重建。

推荐输入：

> 提取电动牙刷的自动刷牙、更换刷头、质量要求、防水需求，以及刷牙、充电、更换刷头三个用例和模块结构。

> 复算案例可靠性，提取五个子系统MTBF、任务时间与可靠度阈值，保留来源和单位。

离线提取器只支持本案例的有限模式。其他需求可能返回待澄清，不要将它理解成通用大模型。

## 4. 实现流程

```text
原始DOCX / 文本PDF / Markdown / TXT
  -> 解析正文（按文档顺序保留表格、段落位置）
  -> 章节/段落切分（目标600字符，短段落条件重叠80字符）
  -> 确定性哈希向量 / 可选真实Embedding
  -> SQLite原文与向量 / 可选Milvus索引

用户需求
  -> 校验项目与知识库版本
  -> 检索缓存 -> 余弦Top12 -> 最多6块、12000字符上下文
  -> 案例可靠性任务使用固定的四类子查询补齐参数证据与相邻上下文
  -> 拼接角色、规则、来源、需求、Schema
  -> 离线规则模拟 / 可选千问API
  -> JSON类型、字段、来源、命名、关系端点与包含循环检查
  -> 保存草稿任务（尚未写正式模型）
  -> 人工修改 -> 重新校验 -> 带修订号确认
  -> 一个本地事务写入全部元素及关系
```

### 4.1 切分细节

- 这里是**字符长度**，不是400–600 Token；重叠不承诺每块固定80字符。
- 标题样式可识别时保留章节路径；样式不规范的原文按段落处理，不能假称全部章节识别正确。
- 完整普通段落优先保留；超过1200字符的长段落再按句子或有重叠的窗口拆开。
- 片段有稳定数据库ID、文档ID、段落定位、正文、内容Hash、Embedding版本。
- 原始文件保存在磁盘，数据库保存文档元数据和分块正文，不把全部文件作为一个巨大TEXT字段反复存储。
- Word图片不做OCR，PDF只读取文字层。解析结果会显示警告。
- Word入库前限制512个压缩成员、32MB解压总量、4MB XML总量、2MB正文XML与2万个关键结构标签，并拒绝DTD/实体声明；超出时拆分原文，不绕过限制。

### 4.2 数据与一致性

六张表：`project`、`knowledge_document`、`doc_chunk`、`generation_task`、`model_element`、`model_relationship`。

- 草稿JSON、召回记录、校验信息和计算结果存任务表JSON字段，适合当前小原型，不额外拆数十张表。
- 正式模型元素与关系分表，关系端点引用元素主键。
- 文档先处于INDEXING，只有向量索引完成且状态为INDEXED后才可检索。
- 外部Embedding/Milvus调用不占用数据库事务；失败标记FAILED，重复上传可用原chunkId重试。
- MySQL/SQLite与Milvus**不存在跨库本地原子事务**。当前是状态门控与稳定ID重试；生产级任务对账、删除、增量版本替换尚未实现。
- 内容相同的文档重复上传不重复建块；修改过的文档按新的不可变文档入库，不覆盖旧文档。
- 生成任务的知识库版本改变时失败，要求重新提交，避免把旧缓存误认作新结果。

### 4.3 缓存与幂等

- 检索缓存键包括项目、知识库版本、查询、Embedding配置与检索参数。
- 生成响应缓存键还包括生成模型、温度、输出模式、提示词/Schema版本。
- 任务状态以数据库为准；Redis/内存缓存只是加速，不是唯一事实来源。
- 同一项目和`idempotency_key`重复提交返回原任务；相同Key但输入不同返回409。
- 正式确认采用任务状态与修订号条件更新，唯一索引阻止重复模型，事务失败整体回滚。
- 本实现没有Redis分布式锁，不声称实现供应商“恰好调用一次”。超时重试仍可能重复推理和计费。
- 文档新增导致知识库版本变化，旧缓存自动不再命中；旧缓存等待TTL过期。

### 4.4 API调用与错误边界

- 连接超时3秒，响应超时60秒，最多额外重试2次；它们是单次请求配置，不是整个任务180秒总截止时间。
- 只对网络临时错误、429、500/502/503/504重试；401、403等直接报错。
- 指数退避并限制Retry-After；不循环重试JSON业务错误。
- 默认`json_object`模式、关闭思考模式并配合Pydantic严格检查；`json_schema`为可选配置，须先核实目标模型和Schema子集支持。提示词或低温度本身不能保证正确。
- 模型输出引用的片段必须属于本次检索结果；引用ID有效不等于证据真正支持结论，仍需人工复核。
- 可靠度只调用固定Python公式，**不eval模型返回的表达式**。

## 5. 实际案例结果

以`artifacts/运行报告.md`与`run-report.json`为本次实际数据来源：

- 446个顶层段落，356个非空文字块，88个入库片段。
- 建模批次11个元素：5个需求（含1个草稿根）、3个用例、3个部件类型；4条包含关系。
- 参数批次7个元素：5个MTBF、1个任务时间、1个可靠度要求。
- 合计18个内部模型元素、4条关系。**这里的18不是“18个需求节点”。**
- 重复上传、任务幂等、重复确认、响应缓存命中、人工确认前校验：演示脚本已断言。
- T=1000h，MTBF=60000、65000、58000、48000、50000h。
- 采用`lambda=1/MTBF`、`Ri=exp(-lambda*T)`、`R=R1*...*R5`，复算`R=0.9138160420441955 > 0.9`。
- 前提为案例中的独立串联、恒定失效率，并忽略刷头和刷柄结构件。此数值是参数计算，不是物理试验或AI性能指标。

原文有一处将约0.9138写在底座可靠度说明附近，但底座独立可靠度约0.98347；本原型使用五子系统公式独立计算系统值，不直接照抄结果。

**没有证明**：18个需求、25个模块、23个活动、44张图、230条关系全量恢复；真实LLM准确率；生产吞吐；真实Redis/Milvus性能；MBSES导入兼容性。

## 6. 可选接入真实模型

没有API Key时不要切换，不会替你调用付费服务。

```powershell
Copy-Item .env.example .env
# 编辑.env，不要提交Key
```

```dotenv
LLM_BACKEND=qwen
EMBEDDING_BACKEND=qwen
QWEN_API_KEY=your-key
QWEN_MODEL=qwen-plus
QWEN_EMBED_MODEL=text-embedding-v4
EMBED_DIM=1024
```

模型名称可配置，不把某个版本号写死为永远可用；按你的百炼地域、权限核实。`text-embedding-v4`的可选维度包含1024，接口参考[官方Embedding文档](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api)；结构化输出支持参考[官方文档](https://help.aliyun.com/zh/model-studio/qwen-structured-output)。

更换Embedding后需要重新上传/向量化；查询与库内向量必须同模型同维度，代码按`embedding_profile`隔离，不会将mock向量与真实向量混用。

可选中间件安装：

```powershell
.\.venv\Scripts\python.exe -m pip install pymysql cryptography redis pymilvus
.\.venv\Scripts\python.exe scripts/configure_real.py
docker compose --env-file .env.real -f docker-compose.real.yml up -d --wait
```

Docker配置沿用原型示例，仅作本机开发参考；本次未启动或验证容器服务。生产需要固定镜像版本、持久化etcd、健康检查、认证、资源限制及替换示例密码。仅修改`.env`还不足以证明外部服务接入成功，应先分别测试再跑端到端验收。

## 7. API与目录

| 接口 | 作用 |
|---|---|
| GET /api/health | 配置模式；不代表外部服务已连通 |
| GET/POST /api/projects | 项目列表与创建 |
| POST /api/knowledge/upload | multipart：project_id、file |
| GET /api/knowledge/{project_id} | 解析信息与索引状态 |
| POST /api/generate | project_id、input_text、可选idempotency_key/bypass_cache |
| GET /api/tasks/{task_id} | 状态、草稿、片段、校验、计算 |
| POST /api/tasks/{id}/validate | draft、expected_revision |
| POST /api/tasks/{id}/confirm | expected_revision |
| GET /api/tasks/{id}/export | 内部JSON及来源导出 |
| GET /api/elements/{project_id} | 已确认元素及关系 |
| GET /api/chunks/{chunk_id} | 片段原文 |

核心实现：`app/parsing.py`、`chunking.py`、`retrieval.py`、`llm_client.py`、`validation.py`、`reliability.py`、`services.py`；页面为`app/static/index.html`。`tests/`存自动化测试；`artifacts/`存本次导出和运行报告；`seed/`为旧版辅助材料，不参与原文演示的参数计算。

## 8. 尚未实现的生产能力

- 认证授权、租户安全隔离、审计、密钥管理、文件杀毒、限流。
- 多实例任务队列和完整故障转移；现为单进程线程池，已支持租约心跳、周期性回收和旧执行结果围栏。
- 文档删除/覆盖版本管理、跨库定时对账、容量治理。
- 全文或混合检索、精排、真实模型固定测试集评估、提示词攻防系统性测试。
- SysMLv2标准映射、完整语法与语义校验、MBSES接口适配、图形布局、仿真集成。

因此只监听`127.0.0.1`作本地演示，不要直接暴露到公网。面试可描述为“已跑通离线原型并保留真实模型接入适配”，不能描述为已上线的生产系统。

## 9. 轻量 Agent 更新

- **多轮记忆**：会话、轮次和上一轮草稿快照持久化到数据库，支持追问修改、刷新恢复、会话版本检查和重复请求去重。失败的追问不会清空上一份草稿。
- **Function Calling**：开放 `search_knowledge`、`read_previous_draft` 两个只读工具，项目范围由服务端确定；最多两轮规划、三次工具调用。工具不可用则回到固定检索生成流程，不允许工具确认或写入正式模型。
- **上下文控制**：保留最近四轮用户消息和上一份完整草稿；累计需求与快照设字符上限，超限明确提示，不自动摘要或悄悄截断约束。
- **边界**：没有加入多 Agent、自动摘要或开放式自主规划。模拟模型只演示链路和指定的质量数值修改，不代表真实模型效果。
- **新增接口**：`POST /api/conversations`、`GET /api/conversations/{id}`、`GET /api/tasks/{id}/usage`；生成接口可携带 `conversation_id`、`expected_conversation_revision`。
- **旧库迁移**：停应用后运行 `scripts/migrate_runtime.py --apply`，先在私有数据目录备份，再补充租约列和新增表，不删除已有业务数据。

实现边界、降级、成本和指标定义见 [IMPLEMENTATION_NOTES.md](IMPLEMENTATION_NOTES.md)。回归命令为 `python -m pytest -q`；页面验证使用 `scripts/verify_agent_ui.cjs`，要求运行模拟模型，禁止启用付费调用。
