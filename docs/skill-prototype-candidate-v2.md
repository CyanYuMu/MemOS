# G2/G3：合同上下文、跨模块候选与新版资格合同

日期：2026-11-09。状态：已审阅冻结的开发设计，独立审阅 APPROVE。本文件不登记一次模型调用，不创建候选、任务资产、参考补丁或隐藏检查，也不授予发布资格。实施与真实执行须分别满足本文的冻结条件。

依赖 [新执行协议](skill-prototype-protocol-v2.md)、[实际交付记录](skill-prototype-delivery-record.md)、[旧首发资格设计](skill-prototype-release-validation.md) 与 [生命周期合同](skill-prototype-lifecycle.md)。冲突时，旧协议继续解释旧记录；本文件仅定义新版本。G0/G1 的声明、许可、三方闭合、故障阻断和预算合同不因本设计放宽。

## 1 已有事实与范围

完整 Feedback 模块导入与健康链资格 005 已通过，具体证据见交付记录。它证明指定依赖副本与 OS 隔离环境可以执行真实生产调用链，不是正式独立任务、Memory 来源、修复经验或 Skill 有效性证明。此前已读取 Feedback 源码及健康基线的角色必须进入曝光账本，不能把后续任务称为完全未见或盲测。

候选仍从原三条来源、两条 lineage 出发：只有一条 Dream 成功经验支持已观察到的多余位置参数修复；失败来源的根因保持未知；scoped 来源仅支撑其自身事实和范围区别。原 packet、MemorySnapshot、EvidenceRevision、事实 ID、watermark、资格及失败归因均不改写。

新候选拟诊断满足当前权威 `BaseGraphDB.update_node(id, fields, user_name=None)` 参数合同的调用。经验动作仍限于在证据充分时删除冗余位置参数，保持既有 id、fields、租户绑定及其他行为。不能扩展成任意 Feedback、mock、fixture、数据库、重试或向量同步修复。

抽象接口的签名与文档是规范性依据，不能单独证明所有后端的租户隔离、事务、幂等、重试或向量同步语义。当前具体调用与该接口是否兼容仍需核对。原 Dream 经验向另一完整模块迁移是待验证假设，不能写成已有跨模块成功经验。健康与停止属于规范性决策，不增加经验修复样本数。

旧 e14 正文绑定 Dream/caller/fakeGraph 范围，不直接用于新家族资格。新正文、输入及导出必须产生新的不可变 revision；旧候选及其覆盖、unknown、失败、挑战、裁决全部保留。

## 2 不可变边界与实施顺序

旧 `skill_drafting`、`skill_adapter`、`skill_release`、`skill_branch_*` 的已冻结生产文件与资产不改写、不加文件、不 monkeypatch。新实现放 `skill_release_v2`；管理与生命周期只增加显式版本分派。既有 schema 1–6 assessment、旧候选 schema 1 与旧 baseline 继续按原合同回读。

顺序固定为：

1. 审阅并冻结本设计；另冻结来源输入清单、合同上下文及生成预算。
2. 实施 candidate schema 2、合同上下文、来源回读、生成与工件回读；离线测试和独立代码审阅通过。
3. 仅以冻结输入生成一次新候选，保留失败且不自动重试；完成来源语义审阅后冻结候选和 v2 导出。
4. 依据已冻结的任务设计开发、资格检查并锁定六任务资产；记录开发曝光，不把任务观察反馈给同一候选的生成器。
5. 实施 schema 7 回读、裁决、管理与生命周期；离线与真实存储验收通过后，另冻结正式批次。
6. G1 实际协议探针及正式候选加载资格均通过后，才能执行新正式批次；真实资格与审阅通过后才可发布和进行 P5。

若任务开发发现候选确需修订，必须保留原候选、开发材料与曝光事实，创建新 revision 及新生成输入清单；已反馈的任务家族转为开发回归，不能改名继续充当未污染的独立资格。

## 3 ProjectContractContext

### 3.1 与 Memory 来源分开

新增不可变 `ProjectContractContext`，类型为 `project_contract_context`。它不是 MemorySnapshot、EvidenceRevision、来源运行或成功样本；不产生 `qualified_success`、run_id、来源 case_id，不计入 `source_refs`、Memory watermark、独立来源 lineage 或成功经验数量。

首版仅收录宿主预先选定的 `src/memos/graph_dbs/base.py` 中 `BaseGraphDB.update_node` 的完整方法声明及文档。不得把 Feedback 文件、调用点、健康 fixture、参考实现、任务目录、隐藏检查或新任务输出附入上下文。生成器无工具，不能指定路径、读取文件或追踪 URI。

context 必须包含并严格核验：

| 字段 | 含义 |
| --- | --- |
| schema_version / kind | context 合同版本及固定类型 |
| project_id / repository_revision | 项目身份与提取时明确的源码版本；不能用分支名代替不可变版本 |
| repository_relative_path | 固定仓库相对路径，不含私有部署绝对路径 |
| file_sha256 | 实际权威源文件的完整字节摘要 |
| symbol / selector_version | 精确限定名与有版本的提取规则 |
| excerpt / excerpt_sha256 | 完整方法声明及文档、其 UTF-8 字节摘要；不是手写近似签名 |
| ast_sha256 | 选定方法的规范化 AST 摘要及绑定的规范化规则 |
| authority_kind / empirical_sample | 固定为项目接口合同、false；禁止提升为经验样本 |
| extractor_digest / context_id | 提取器摘要及不含自身 ID 的完整 context 摘要 |

完整文件摘要与方法摘要同时保留：方法未变但文件其他内容改变仍触发生命周期观察，不能由自动语义等价判断忽略。合同变更只能恢复原冻结字节，或进入新的 context/revision 审阅。

提取接口 `extract_contract_context(repository, *, project_id, repository_revision)` 仅接受完整 Git commit ID，核对该提交中的目标文件与当前实际文件完全相同。选取规则固定为顶层 BaseGraphDB 中唯一 update_node，包含装饰器及完整方法正文；AST 使用无位置属性的 Python AST dump。context 的 schema_version 为 1，selector_version 为 `python-method-v1`，authority_kind 为 `project_interface_contract`。验证接口 `validate_contract_context(context)` 重算全部内嵌绑定；回读接口 `recheck_contract_context(context, *, authority_bindings)` 只从宿主 `project_id → 仓库根目录` 映射取路径，检查符号链接和当前完整字节。后续正常提交可以改变仓库 HEAD，但不能改变本 context 绑定的文件；不以重建 baseline 接受漂移。

首版选择器只接受声明型源文件：模块级 imports、常量与唯一目标类，类体为方法及文档字符串；动态属性/类名覆盖、条件定义、类内导入重绑定等不支持形态直接拒绝。Git 必须核对真正工作区根目录及对象类型为 commit，禁用 replace objects 和环境中的 Git 仓库重定向。它核验的是固定声明归属，不执行权威源码，也不证明任意 Python 动态语义或具体后端与抽象接口兼容。

### 3.2 单独的规范性引用

每条规则保留原 `evidence` 与来源证据链；另增 `contract_refs`，引用字段为 `context_id, locator, value_sha256, kind, claim`。kind 仅允许 `precondition` 或 `normative_support`，locator 必须解析到已提供的 context 内容。

来源事实与 context 引用分别校验和计数。原 Memory/Evidence 引用不能由 context 替代；所有原来源必须仍被引用。根因未知的失败来源，其非合同事实仍只能作为 background，不能因某次检查通过或新 context 存在而升级。

规则必须区分已观察动作、规范性约束和迁移假设。若一条规则的适用范围跨出来源场景，其迁移效果不能以 `basis=empirical` 宣称已经成立；引用真实成功动作不等于验证了扩大后的整体主张。独立语义审阅逐项记录该区别，模型不授予自己的适用资格。

## 4 Candidate schema 2 与生成记录

### 4.1 最小合同

candidate 使用 `schema_version=2` 和独立 domain/producer 版本；assessment 的 schema 7 是另一份合同，二者不混用。

保留原候选的项目/Skill 身份、过程正文、规则图、来源引用及内容摘要语义。新增宿主生成字段：父 revision/content/manifest 引用、contract context IDs、生成输入清单 ID、曝光清单快照 ID。规则新增独立 `contract_refs` 及其内容绑定。所有字段、引用、输入和生成记录均参与 revision/content hash，禁止生成后补写而不改变身份。

模型仅提出正文、规则和引用，不得提供发布状态、经验数量、已验证覆盖、宿主身份或批准字段。初始状态固定为 candidate，语义审阅 pending、独立验证 not_run、发布资格 not_evaluated。模型输出必须符合严格字段/类型/大小/引用合同，拒绝重复键、非有限数、未知字段及未完成响应。

目标结构为两个可达无环节点、六个分支和四个终态：`success/not_applicable/needs_evidence/unresolved`。入口先核对当前请求，再核对当前合同和可用观察；范围外、缺依据与进入处理分别表达。处理节点区分成功、工具/依据不可用及仍未解决。健康与修复可共享 success 分支，但它们是不同的资格子场景，不能以其中一个代替另一个。具体 rule ID、分支顺序与映射在生成和语义审阅之后冻结，不能事后根据结果调整。

### 4.2 输入清单与单次生成

生成输入清单单独冻结以下内容与摘要：原 packet 的明确公共投影、原来源 fact IDs、父候选、ProjectContractContext、仅依来源与权威合同形成的反馈、宿主周期策略、输出 schema、完整提示词、producer、模型请求身份及预算。

生成器不得读取本设计全文：本文后半部分含任务与评分设计，只属于宿主开发侧。只允许发送经白名单构建并回读核验的生成输入。反馈不能包含 Feedback 名称或路径、新任务请求、公开/隐藏 fixture、变异体、参考补丁、判分映射、任务执行结果，亦不能把健康资格 005 当作新增经验。

新生成器保持单次显式请求、无工具、无自动重试、无备用模型；客户端按所有权关闭。前后重新检查原 Memory 来源、父候选及权威文件，漂移即保留失败记录，不能封存可加载候选。

生成记录保存请求模型名、返回模型名及严格相等结果、请求/提示词/schema/响应摘要、公开响应内容、finish reason、用量及缺失状态、耗时和安全错误类别。历史请求 `deepseek-v4.1-flash-1017` 与返回 `deepseek-v4-1-flash-261017` 不同的事实继续保留，不自动当作已确认别名，也不把该开发生成称为固定模型效果实验。新调用的实际身份独立记录。

生成模型、max_tokens、timeout、最大调用次数及总预算须另行明确冻结；旧生成预算和 G1 四探针预算不构成本轮新调用批准。缺少这些字段不得运行。

### 4.3 实现与回读边界

可直接复用原 `validate_packet`、`collect_packet/recheck_packet`、旧父候选 `load_candidate`、公共 JSON 哈希与脱敏能力。不能直接向旧 `revise_candidate` 或 `OpenAIJSONGenerator.generate` 塞入 context：它们严格限制 revision_context、原 fact IDs、提示词与 schema 回算。旧 artifacts loader 也固定文件集合。

建议新文件责任与接口：

| 新文件 | 责任 / 关键接口 |
| --- | --- |
| `skill_release_v2/authority.py` | 提取/校验 context；`recheck_contract_context(context, *, authority_bindings)`；`subjects_for_candidate_v2(loaded, *, cli_path, authority_bindings)` |
| `skill_release_v2/candidate.py` | proposal/candidate schema 2、原事实与 contract_refs 分别验证、确定性 revision 构造 |
| `skill_release_v2/generation.py` | 白名单输入构建、一次公开 JSON 请求、版本化生成记录及诊断 |
| `skill_release_v2/revision.py` | 父版本与输入清单、前后来源回读、不可变失败/成功记录 |
| `skill_release_v2/artifacts.py` | schema 2 写入、原子封存、完整重新推导回读；不放宽旧 loader |
| `skill_release_v2/graph_bundle.py` | 将验证过的 schema 2 工件映射到现有图事务，单独保留 context 引用 |

新工件至少封存 packet、context、输入/曝光清单、proposal、generation、candidate、结构验证及 manifest。manifest 回读重算每项依赖，不能只比较目录中自报哈希。写入前后检查与原子发布不能把失败半目录注册为候选。

首版不增加图节点类型。现有 SkillRevision/Rule 的 JSON payload 可保存独立 context 引用与工件摘要；原 DERIVED_FROM/EVIDENCE_LINK 继续只指真实 Memory/Evidence。不能伪造必须绑定 run/case 的 EvidenceRevision 来容纳项目文件。以后如需要一等合同节点，须另行定义 v2 图合同与迁移，不改旧枚举或全局变量。

### 4.4 首版精确工件合同

纯合同实现增加 `inputs.py`：确定性 `build_messages`、输入/曝光清单构建及校验、生成记录校验。candidate 调用它时显式提供自身 proposal schema，避免与实际供应商调用模块循环依赖。`generation.py` 后续负责实际请求及前后身份检查，不能由合成记录声称发生真实模型调用。

proposal 保留旧正文的明确字段集合，每条 rule 增加 `contract_refs`。首版每条规则至少一条合同引用，locator 仅为 `/excerpt`；value_sha256 为所引用 JSON 字符串的规范哈希，与 excerpt_sha256 的原始 UTF-8 字节哈希分开。入口 basis 固定 normative，处理规则固定 hypothesis；原 Dream 已观察修复继续在 Memory evidence 主张中表达，不把跨模块整体效果标为 empirical。

parent.json 为旧 loader 返回的 packet/proposal/candidate/generation/validation/manifest 六项完整内容，按旧合同逐项重推；其 packet 必须等于本次 packet。首版只接 schema 1 父版本。candidate.parent 引用四项 schema_version/revision/content_hash/manifest_id。candidate 顶层沿用既有正文/来源身份与固定初始状态，schema_version=2、domain_version=2，增加 producer、parent、contract_context_ids、input_manifest_id、exposure_manifest_id。规则独立派生 contract_links，含原引用、producer/semantic_review/skill_id/revision/rule_id/link_id，不含 source_id 或经验资格。revision 先绑定全部输入及正文，再派生两类链接；content_hash 绑定完整派生结果。

input_manifest 精确包含 schema_version=1、kind=candidate_generation_input、project_id、packet_id、source_ids、source_fact_ids、parent、context_ids、exposure_manifest_id、feedback、host_policy、producer、producer_digest、requested_model、service_identity、parameters、budget、output_schema_sha256、messages、prompt_sha256、request_sha256、input_manifest_id。来源/事实/context IDs 排序且完全等于供给材料。feedback 每项为 issue_id/reason/required_change/fact_ids/context_ids，引用至少一类已供给依据；host_policy 仅 max_repair_cycles。parameters 为 timeout_seconds/max_tokens/temperature=0/max_retries=0/tools=[]；budget 为 max_calls=1、max_input_bytes。输入字节上限不超过 512000，响应公开内容不超过 131072；所有上限为明确正值。通用供应商 API 未提供美元硬限额时不得声称实现了美元预算强制，费用不可知如实保留。

producer 为 version=memos-candidate-generator-v2 及 files 映射，明确绑定 authority.py/candidate.py/inputs.py/generation.py 四项字节；producer_digest 为此对象的规范哈希。messages 由 packet 公共投影、父候选、context、feedback、host_policy 与输出 schema 的白名单确定性重建，不能只信任自报哈希。prompt/schema/request 哈希分别绑定实际消息、实际 schema 和 provider/service_identity/requested_model/parameters/messages/response_format={type:json_object}。input_manifest_id 绑定去除自身 ID 后全部字段。

exposure_manifest 精确包含 schema_version=1、kind=candidate_exposure_snapshot、project_id、previous_manifest_id、entries、exposure_manifest_id。每项 entry 包含 actor_id/actor_role/phase/asset_kind/asset_sha256/recorded_at/allowed_roles/included_in_generation/lineage_ids/derived_from；角色为 generator/host_developer/semantic_reviewer，时间为 UTC ISO 字符串，重复项拒绝。资产类别及用途遵循第 5 节；generator 不能接收 independent_family_baseline/task_request/public_fixture/hidden_check/reference_patch/task_result。整个曝光清单不发送模型。字段白名单不能自动证明自由文本的语义未泄漏，来源反馈仍须宿主逐项审阅。

generation 精确包含 schema_version=2、kind=candidate_generation、provider=openai_compatible_json、producer、producer_digest、input_manifest_id、sdk_version、service_identity、requested_model、reported_model、model_identity_match、parameters、request_sha256、prompt_sha256、schema_sha256、response_sha256、messages、response_content、finish_reason、usage、usage_status、elapsed_seconds。全部输入必须与重推清单一致；严格解析 response_content 后等于 proposal，finish_reason 必须 stop，用量缺失或无效明确保存。model_identity_match 是精确比较结果，不允许自报 true；false 保留为来源语义审阅局限，不自动拒绝固定内容候选封存，也不能据此宣称生成模型固定一致。以上 JSON 自洽不证明供应商真实性；实际调用身份由宿主生成流程与原始记录承担。

工件集合固定为 packet.json、contexts.json（恰一项）、parent.json、input-manifest.json、exposure-manifest.json、proposal.json、generation.json、candidate.json、validation.json、REVIEW.md、manifest.json。manifest 的 schema_version=2、kind=candidate_review_artifacts_v2，绑定项目/Skill/revision/packet/content/context/input/exposure/parent 与逐文件摘要及 manifest_id；每项 JSON 和 REVIEW 都确定性重推。结构验证只报告来源、lineage、两类引用和 context 数，不产生成功经验或分支覆盖。write_candidate 必须先后执行真实 source_recheck(packet) 及 authority 回读；load_candidate 必须 authority 回读，但不假称它完成了实时 Memory 认证。当前 authority 提取器字节改变会阻断实时回读，历史 context 仍可做结构展示；不能重算 extractor_digest 替旧 context 解禁。

### 4.5 真实生成与来源回读编排

`OpenAIJSONGeneratorV2(api_key, base_url, client_factory=None)` 提供 service_identity 及 `generate(packet, *, contexts, parent, input_manifest, exposure_manifest) -> (proposal, generation)`。输入先按确定性规则重推，并核对当前四个 producer 文件和 service 摘要；只发送已冻结 messages/model/json_object/temperature/max_tokens，不调用旧 generate 或自动补发提示。同一实例以锁保护，只允许一次 request_attempt，禁止重入、自动重试或备用模型；实例自行创建的 SDK/HTTP 客户端在成功和所有失败路径关闭。

parameters.timeout_seconds 在生成器中明确指 SDK/HTTP 请求 I/O 超时，不声称整个生成及来源回读具有相同墙钟硬截止。elapsed_seconds 记录实际耗时，超时的费用保持未知。该语义与 Claude 任务的独立进程总预算监督不同。max_calls=1 约束当前 attempt/单实例；首版受托宿主不重放该 attempt，但尚无跨所有新目录/进程的全局请求去重账本。

`generate_revision(parent_directory, output, generator, *, contexts, input_manifest, exposure_manifest, authority_bindings, source_recheck) -> Path` 从旧父工件读取 packet，不接受额外可变正文或反馈参数。source_recheck 为可信宿主绑定的旧 recheck_packet 回读闭包。预检、生成后、最终封存窗口均检查 Memory、父包六文档、context 和 producer；封存回调同时包含这些约束。父版本、producer 或来源漂移不得形成可加载 candidate。输出目录先独占创建，request-started 记录 fsync 完成后才尝试请求；已有/符号链接目录拒绝，崩溃留下 unknown，不能在同目录续跑。

完整且公开的 proposal/generation 可先留存在 attempt 外层，再校验/封存候选；后续失败不覆盖已保存公共响应或原始主要失败。失败记录不进入候选的 11 文件集合，不序列化异常对象、provider response 对象或辅助 reasoning 字段。诊断字段仅为 schema/kind、input_manifest_id、request_sha256、producer_digest、service_identity、requested_model、reported_model|null、model_identity_match|null、finish_reason|null、usage、usage_status、elapsed_seconds、stage、code、request_attempts(0|1)、provider_response_received。明确区分请求尝试与实际发送/计费，未获响应不推定零费用。

实现验收覆盖请求前任一输入/身份篡改均零调用；成功、超时和异常最多一次尝试；辅助 reasoning 属性不可读取；重复 JSON 键、非有限数、截断、工具/拒绝响应、超限或私密内容拒绝；模型别名差异真实保留 false；来源/父/context/producer 的请求后漂移保留公共结果但不封存；封存失败无可加载半包。测试使用明确合成客户端，不把它记成真实供应商调用；实际生成预算、来源快照和输入清单仍需另行预登记。

### 4.6 首次真实 v2 生成预登记

本次生成单元命名 candidate-v2-attempt-001，父候选固定 e14c3e73ba8e72d1337423e8b3bff94af916e68cc21f4b28e2b2b3e843ffd995，packet 固定 bd696e25dc30abc08af4c0e1424c6236bdb3e62e754f2ef86d23c5dae16b4be5。三来源、两 lineage 和所有资格不变。生成器实现独立审阅通过后，宿主才提取当前已提交版本中的 BaseGraphDB 合同，记录完整输入、上下文、曝光快照及 producer 摘要，先单独回读真实 Memory；冻结材料核验后才发起请求。

沿用已配置的 openai-compatible 生成服务，请求模型 deepseek-v4.1-flash-1017。单实例单次请求，temperature=0、max_tokens=32000、HTTP I/O timeout=180 秒、max_retries=0、无工具、无备用模型，输入上限 512000 字节，公开响应上限 131072 字节。调用总预算为一次、最大输出 32000 tokens；供应商无美元硬限额，费用未知不计为零。精确模型差异继续登记，不将别名假定为一致。任何失败留下独立 attempt，不在原目录续跑。

反馈仅允许：根据原成功补丁和接口声明，将可复用动作限制为有依据的冗余位置参数删除；区分当前请求范围、权威证据是否可得、健康与最小修复；保留失败根因未知、当前证据与最终补丁验收要求。最多两周期为宿主策略，不能记为来源已观察事实。生成器不接收任何新任务名称、代码、检查、参考补丁或结果，也不接收旧独立运行结果。

曝光快照登记原 packet、父候选、接口合同、反馈、策略和输出 schema 的实际生成用途；另以宿主角色登记已见旧资格批次、G1 协议记录及完整模块健康基线。该快照从现有工件回溯登记，时间表示登记时刻，不伪造过去曝光时刻，也不声称证明宿主从未看过其他材料或构成完整历史账本。实际请求由白名单 messages 精确约束。生成成功后仍须逐项语义审阅，不能凭结构通过自动注册、计覆盖或发布。

## 5 曝光与独立性账本

输入与曝光分别记录。生成输入清单证明实际送达模型的字节；宿主曝光账本证明谁在什么阶段看过什么，不以“未发送”掩盖开发者已知内容。

每条曝光至少绑定资产摘要、资产类别、角色/执行身份、时间、用途、允许接收者、是否进入生成输入、来源或任务 lineage、派生关系。公共记录只展示安全摘要，不公开私有来源正文、宿主定位或凭据。账本追加不可改写；任务开发或结果反馈后新增记录，不回填为事前未见。

| 类别 | 来源计数 | 允许进入候选生成输入 |
| --- | --- | --- |
| 原三条 Memory/Evidence | 保持原三来源/两 lineage；仅按实际资格解释 | 是，固定公共投影 |
| BaseGraphDB 权威合同 | 不是 Memory，不计成功或独立来源数 | 是，固定方法 context |
| 旧候选与来源语义反馈 | 不增加来源样本 | 是，仅冻结的允许字段 |
| 旧真实批次/任务反馈 | 已曝光开发材料，不构成新 holdout | 否；若以后使用，须新输入版本并重新评估独立性 |
| Feedback 导入及健康资格 005 | 环境/行为基线，不是修复经验 | 否 |
| 新六任务、公开/隐藏检查与变异体 | 新任务家族资格；六变体不等于六独立来源 | 否 |
| 本文任务与评分章节 | 宿主设计，非来源事实 | 否 |

新家族独立性依据是不同生产模块、调用链及副作用边界，同时披露共享接口、相同修复机制、预先健康基线曝光。只允许声称指定家族中的跨模块资格验证，不能宣称普遍有效、无污染盲测、平均成功率或效果优势。新任务运行结果不自动转成 Memory 或补足原来源成功样本数。

## 6 完整 Feedback 六任务设计

### 6.1 共同执行边界

新任务使用完整 `MemFeedback._single_update_operation` 生产模块，沿用资格 005 验证过的完整包隔离方法：显式模块白名单、`-I -S -B`、明确依赖目录、清洁环境、真实 OS 限制及完整访问日志。模块路径必须实际指向复制包中的当前被测文件；不能退回方法切片、跳过包初始化或把整个原仓库设为可读。

任务工作区只提供唯一可编辑 Feedback 文件及该任务明确公开的说明/合同。检查器先以当前候选替换隔离包中对应文件，再执行完整模块；Agent 不能读取隐藏正确版本、参考补丁、隐藏检查或其日志。仅把复制包与任务文件放在两个目录不是访问隔离证明，必须由 OS 自检证明边界。

保留真实 Pydantic 数据模型、版本 helper、注册 hook 与 fallback、Tenacity 装饰器/重试/等待及图写入与向量同步顺序。依赖边界使用绑定权威方法签名的 autospec，并独立断言 fields 字典、目标和租户；不得用宽松 Mock 消除绑定错误。构造器使用既有公开依赖注入方法，不初始化真实模型、数据库或网络。

公开与隐藏检查使用分别固定的输入，二者都执行真实完整模块与同一最终候选字节；公开程序不包含隐藏输入。任务开发先锁定契约与判分映射，再生成完整资产摘要；本文件不提供具体 fixture、预期补丁或隐藏答案。

### 6.2 任务矩阵

下表是资产设计合同，不是已创建或已通过的任务清单。最终 task ID、revision、lineage、公开请求摘要、程序与输入摘要、允许路径及资格 ID 必须在实现后另行封存。

| 任务类型 | 当前请求与起始事实 | 合理结局 | 必须观察的事实 |
| --- | --- | --- | --- |
| 修复 | 当前完整接口合同与可执行检查；目标调用存在来源支持的冗余位置参数 | 只修绑定问题，复验成功 | 初始失败、至少一次合法实际编辑、最终公开和隐藏通过；id/fields/租户、版本历史、hook/fallback与其他副作用未损坏 |
| 健康 | 同一诊断范围，完整模块及当前调用均已满足合同 | 零编辑，确认健康 | 初末公开/隐藏通过，目标文件字节不变；不计作修复成功 |
| 范围外 | 当前请求明确要求不属于参数绑定修复范围的行为；附带接口健康不回答该请求 | not_applicable，保留请求对应失败或限制 | 完整 TASK 已送达，初始公开观察真实；零修改；不得转入无关健康分支后宣称成功 |
| 缺合同 | 请求名义上属于 UPDATE 参数绑定诊断，但当前实际后端或动态绑定是否实现该抽象接口缺少权威依据；现有本地检查可全部通过 | needs_evidence，不猜测具体目标的兼容合同 | 零修改；指出具体绑定依据缺失，不能用已知抽象签名或其他替身的健康结果推导当前目标兼容 |
| 持续业务错误 | 有可支持的绑定缺陷；修复后固定图边界仍持续产生范围内、不可由本次允许编辑解决的业务失败 | 合法最小修复后 unresolved，保留剩余失败 | 公开检查程序正常执行，返回业务 failed 且原生 is_error=false；实际重试次数、外层异常和底层原因如实记录；无不当向量同步/清理与新增回归 |
| 持续工具不可用 | 初始公开检查真实执行；从第 2 次公开检查起，执行工具持续不可用直到结束 | 已发生合法最小编辑时保留补丁；needs_evidence 停止 | 宿主/Hook/原生错误精确闭合，is_error=true；不可用返回不含伪造业务检查；停止后不再重试检查或编辑，不声明 success |

分流先检查当前请求的范围：明确要求新业务策略或其他范围外行为时，走 not_applicable，不能评分为应走 needs_evidence。只有名义属于参数绑定诊断、但当前具体目标与抽象合同的兼容关系缺少依据，才进入缺合同路径。该任务不能简单删除已封存在候选中的 BaseGraphDB 签名，也不能通过使 authority 生命周期失效来替代 Agent 的缺依据判断。具体动态绑定材料不进入候选生成输入。

持续业务错误与工具不可用必须分开。被测业务产生 RuntimeError/RetryError，若检查程序成功捕获并输出合法失败结果，属于业务 failed；检查器不能启动、超时、失联或产生无效结果才属于 execution error。错误类型、层级和次数按真实观测记录，不能把底层 TypeError 自动写成直接抛出的异常。

持续工具故障只由固定宿主策略注入，Agent 参数不能改变索引、路径或故障行为。离线自检必须实际调用第 2、3 次并证明都不可用；正式 Agent 在第 2 次后合理停止时，只声称该次错误已送达，不声称它亲测了第 3 次。G1 的错误协议探针要求显式观察两次错误，与正式 Skill 任务的停止策略不同。

### 6.3 资产资格与负例

每任务先做真实 OS 隔离资格：初始候选、参考候选及预登记不安全变体均执行公开与隐藏检查；验证导入位置、最终候选摘要、保护文件、读写边界、失败集合及完整访问日志。纯静态断言或替身函数结果不能替代完整模块执行。

负例至少覆盖：保留绑定缺陷、宽泛吞错、接受错误参数而取消合同、跳过有效图写入、改错 id/fields/租户、破坏输入/版本历史、绕开 hook/fallback、图失败后仍同步向量或清理绑定、修改只读资产，以及把工具错误伪装成通过。哪些负例适用于哪项任务在锁文件中明确，不能以大量无关通过替代目标错误发现能力。

公开检查遵循 G1 当前独立 20 秒预算，原生 Hook/socket 仍为 5/4 秒。资格 005 的 90 秒是离线完整健康资格总预算，不自动成为每次公开检查预算。若真实检查程序无法在冻结公开预算内完成，先修订并重新冻结执行合同和 producer、重做必要资格；不能在正式运行中临时增加超时或改掉真实重试等待。

## 7 过程计数、评分与正式批次边界

### 7.1 过程事实

沿用最多两次编辑后公开验证周期的宿主策略。两次已送达公开检查之间的一批 Edit/Write 尝试计为一个周期，包括失败、无操作、撤回和被拒绝的尝试；多次编辑不各自伪造一个成功周期。没有实际发生的第二周期不能声称已耗尽。没有进一步有依据且获许可的修复时允许提前停止，不要求为了用尽预算重复无效修改。

所有写入仍须在同次 Pre Hook 获得并消费许可；声明需先经原生结果闭合提交。健康、范围外或缺依据停止不需要虚构 act。完整 TASK Read、当前检查及声明引用是门禁事实，不能由后置说明补造。新版明确记录 attempted、denied、permitted、consumed、executed、failed 与实际变化。

正式资格要求 protocolDeniedCount=0。可信拒绝加原生拒绝结果完整闭合仍可 captureComplete=true，且不算实际编辑；但该次运行不贡献分支资格。失败后恢复出正确补丁可保留独立的补丁正确性结论，不抹去拒绝或改称原本合规。

### 7.2 自动资格与语义裁决

| 结论 | 必要条件与计数 |
| --- | --- |
| repair_confirmed | 来源范围内的合法最小修改，初始失败与最终同补丁公开/隐藏通过，正确使用规则；计修复，不计新增来源经验 |
| healthy_confirmed | 当前请求在范围内、初末通过、零修改、正确健康判断；计健康，不计修复 |
| stop_confirmed / not_applicable | 当前请求确在范围外、零修改且保留实际失败/限制；不计任务修复 |
| stop_confirmed / needs_evidence | 当前请求依据缺失且零修改，或固定工具错误实际送达后的合法有限修改与立即停止；不计业务通过 |
| unresolved_confirmed | 本任务要求的合法实际修复已发生，范围内剩余业务失败真实可复现，无新增回归，停止与实际周期一致 |
| failed / needs_evidence | 捕获、加载、身份、许可、检查、预算或语义不能闭合；不贡献独立覆盖 |

持续业务错误任务若零修改停止，可以保留合理保守停止的诊断，但不能计作已验证“修复后未解决”子场景。工具故障任务的隐藏验收即使通过，也不能补造 Agent 已取得公开验证成功的事实。

逐分支贡献需绑定候选、任务预登记意图、真实条件/声明/动作/检查引用及顺序。两个节点的六条边是图覆盖单位；修复和健康等共享边的子场景另列矩阵。每条必要边和六类任务的指定子场景都合格，才满足首发资格，不能把健康贡献复用成修复贡献。

自动程序只生成可审阅事实；受托或人工语义审阅单独判断当前请求、适用性、具体归因、声明真实性、停止行为和迁移边界。来源结构校验、任务成功、正确 Skill 使用与发布资格分别呈现。

预期停止可以保留原始 validation 的 failed/unknown。仅绑定当前 schema 7 assessment、合规批次及独立语义确认后，解除该条预期停止的待审阻断；不改原评分，不设置旧历史 resolvedHistory，不影响其他失败/unknown，不计修复成功。未被审阅或使用不当的停止继续阻断。

### 7.3 真实运行前必须另行冻结

首发计划沿用“一次无 Skill 修复校准，随后六类 Skill 单元”的结构；校准只检验执行环境与任务可解性，不贡献 Skill 覆盖。校准失败阻断后续，每个单元一次，不自动重试。运行前还须冻结：候选 revision/content/export、context 与输入清单、任务及资格摘要、producer/profile、CLI 字节/版本、请求模型/effort、turns/时间/费用上限、单元顺序、最大调用数、输出目录与每项分支意图。

本文件不填写尚未批准的新生成、正式批次或 P5 数值预算。G1 四探针不是这七单元的额度，旧 release-batch-001 也不是新批次批准。缺少明确数值或前置资格时，执行入口必须拒绝开始。任何必要重跑建立新批次、解释原因并保留旧结果；不得更换输入或 producer 后续跑原 manifest。

## 8 Schema 7 回读与管理接入

### 8.1 新 assessment

新增独立 `validate_assessment(candidate, validation, assessment)`、`adjudicate_assessment(...)`、`assessment_outcome(...)` 和 `collector_digest()`；管理层显式分派 schema 7，旧 1–6 仍走原校验器。

schema 7 继承旧公共事实概念，但不从旧 consumer 的成功判断派生新资格。完整字段 schema 在实现前由这一新合同统一定义，至少绑定：

- 候选 schema/revision/content/export、context IDs、输入/曝光清单摘要。
- 任务 revision/lineage/资产及资格摘要、当前 run/attempt/session、批次/单元、producer/profile、CLI/模型身份、预算与真实用量。
- 初始/最终候选及收据、同补丁隐藏验收、公开检查开始/返回/宿主留存与完整错误身份。
- 已验证完整请求观察、声明 pending/committed、输入绑定、许可与单次消费、实际文件变化、拒绝和失败，以及连续审计摘要。
- processComplete、protocolDeniedCount、cyclesUsed、captureComplete、loadVerified、authority/lifecycle 观察绑定。
- taskOutcome、分支候选与六类任务子场景资格；所有派生字段按保留事实重算。

原生私有文本、凭据、绝对控制路径及推理不进入公共 DTO；公开显示安全类别、摘要、顺序和可定位引用。完整私有原生与 Hook 材料按既定有界策略留存供宿主回读；不能用公开删节投影冒充原始字节。

### 8.2 管理与图

现有 Validation DTO 无版本字段且拒绝额外字段。首版可保留其基本任务结果投影，把新过程/合同事实放入 schema 7 assessment；不得悄悄丢弃字段以适配旧 schema。若基本 DTO 本身需扩展，须明确新版本及旧/新双向回读测试。

配置注册、来源回读、candidate loader、导出/执行器、证据接口和前端详情必须按候选/assessment 版本显式分派。旧 `management.native_skill` 使用旧四字段规则声明，不能直接导出 schema 2。候选 v2 的 profile 需要绑定新六字段声明、固定规则图、真正加载的正文与 context；G1 微型 probe 的初始化允许列表不能直接冒充真实候选 Skill 加载验证。

管理证据页分开显示原 Memory 事实、权威合同上下文、迁移假设和实际验证贡献。历史模型身份不一致、失败根因未知、协议拒绝、业务错误、工具错误及捕获不完整均保留独立含义。新版本覆盖不能迁移到旧 revision。

## 9 Authority 生命周期与恢复

### 9.1 新 baseline 与准入

现有 `skill_lifecycle.observers.tool_subject(cli_path, implementation_paths=..., capabilities=...)` 可把权威文件摘要加入工具主体；`observe_tool` 会实际回读。首版不新增 subject kind，为新 candidate revision 建立新的 tool subject，绑定 v2 producer、权威源文件及 context 身份。原三条 memory subject 单独保留，合同文件不进入 Memory 来源计数。

必须先按冻结 context 校验权威文件，再建立新 subject；否则 factory 会把当前漂移文件算成健康 baseline。注册后只使用已存 baseline，服务重启不重新生成。`bind_lifecycle` 对旧绑定只允许完全相同的 binding ID，不向旧 revision 追加文件或重写 expectedDigest。

宿主 `authority_bindings` 保存可信定位，公开 API 不接受路径或命令。v2 AuthorityGuard 检查项目/文件/符号绑定、边界、符号链接、文件与片段摘要；当前旧工具指纹的 read_bytes 不能独立替代该检查。候选加载与实际执行开始必须重新校验，结束验收再次校验；可信写许可边界也需回读或等价的不可变绑定，避免运行期间漂移后继续编辑。异常关闭本次准入，保留实际执行记录。

### 9.2 恢复证据

文件变化、缺失或无效绑定分别记录 changed/unavailable/invalid，产生生命周期 root 并传播阻断。回到原冻结字节只形成 restored/restored_pending，不自动恢复发布或清除根因。若希望使用变化后的新合同，必须新建 context、revision 与资格，不能通过 restore 接受新的 baseline。

恢复继续要求根因之后的新真实独立执行、正确加载、完整捕获、合格语义裁决和当前 observation IDs。schema 7 还须核验新声明/许可/拒绝合同及 authority 绑定。恢复票据、开始/结束观察、run/attempt、单元、候选/导出、初末收据、报告和事件摘要必须一致，不能拿故障前结果或故障期间结果补作恢复证明。

当前 `_require_recovery_execution` 硬编码 schema 6。须增加到独立 schema 7 恢复验证器的显式分派，不能仅把等号放宽成包含 7 的集合。旧 schema 6 恢复合同与历史票据保持不变。

sourceWatermark 继续只表示原来源；authority/tool 主体通过 lifecycle binding/watermark、候选 context IDs 与执行票据绑定。任何恢复或发布包必须同时使用当前来源与 authority/lifecycle 事实。停止任务的合规贡献不自动满足恢复所要求的真实修复回归。

## 10 实施验收与未完成项

离线实现严格 RED→GREEN→REFACTOR，变更限新模块与显式管理/生命周期分派；受冻结旧字节约束，格式与 pre-commit 限定新/改文件。保留真实命令输出、独立审阅及旧文件/工件摘要核验。

验收至少包括：

1. 原 packet 三来源完整、未知根因限制不变；context 不能冒充来源成功、未知引用或扩大实际经验。
2. candidate schema 2 输入/生成/父版本/context/导出全链回读；字段篡改、删来源、改合同、模型身份不一致、半写目录与漂移均有明确结论。
3. 生成输入自动白名单核验，禁止 Feedback/任务/判分资产泄入；任务开发与后续反馈如实追加曝光，不能改旧清单。
4. 六任务真实完整模块资格及不安全变体失败，公开/隐藏隔离、错误身份、持续故障与原重试语义可回读。
5. schema 7 贯通 collector→校验→实际 Neo4j 追加→语义裁决→发布包；协议拒绝不算实际编辑且不给资格，健康/修复/停止/未解决分开计数。
6. schema 1–6、旧候选、旧 baseline 与旧恢复票据兼容；新记录不能清除旧失败或迁移覆盖。
7. 权威源文件变化/不可用/无效绑定阻断加载；恢复原字节仍待新验证；schema 7 新票据与当前观察一致才可受托恢复。
8. 管理页面真实服务与浏览器展示候选、两类引用、曝光边界、原始评分、裁决、发布与多根因恢复，单元测试或静态页面不代替实际验收。

本设计完成不表示以上实现、真实模型运行、首发资格、发布或 P5 已完成。正式候选生成预算、任务完整资产/资格、schema 7 精确字段合同、真实批次预算与 P5 计划须分别冻结并记录实际结果；未满足项持续作为执行入口的阻断条件。
