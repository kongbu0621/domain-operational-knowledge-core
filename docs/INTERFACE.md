# 公共接口：材料、候选与明确审查

`0.1.0a1` alpha，Linux / Python 3.12，单个受信本地数据域。当前仅实现首轮材料、候选、审查及查询；使用登记、效果、修订、退休、组合快照恢复和 P 适配尚未实现。

## 库与 CLI

包名 `domain_operational_knowledge_core`，根导出 `initialize`、`open_store`、`KnowledgeError`。库不需要常驻服务；每次调用自行关闭数据库连接，不保留需要调用方关闭的连接句柄。

| 库入口 | CLI：`operational-knowledge --store DIR …` | 行为 |
| --- | --- | --- |
| `initialize(path, *, ledger_path, anchor_artifact_id)` | `init --ledger-db PATH --anchor-artifact ID` | 创建不存在的私有知识目录，绑定一个已有 Ledger；目标已存在则拒绝 |
| `open_store(path)` | 其他命令自动打开既有目录 | 检查知识库身份与格式，文件缺失不自动重建 |
| `store.execute(request_utf8)` | `write --request FILE` | 接收严格 UTF-8 JSON bytes；四种写操作见下表 |
| `store.get(kind, id)` | `get --kind KIND --id ID` | 历史对象与当前材料观察；kind 为 material、candidate、review、version |
| `store.query(kind, context, *, after="", limit=10)` | `query --kind KIND --context FILE [--after ID] [--limit N]` | 当前有效且条件匹配的知识；实际核对所需材料字节 |
| `store.read_material(id)` | `read-material --id ID` | 校验准确材料版本并返回完整有界字节，JSON 中用 Base64 表示 |
| `store.get_operation(scope, operation, request_id)` | `operation --scope S --operation OP --request-id ID` | 原成功回执及本次状态/材料观察 |
| `store.pending(scope, request_id)` | `pending --scope S --request-id ID` | 原 publish_material 待决输入与最近十条失败，或其已提交回执 |
| `store.resume(scope, request_id)` | `resume --scope S --request-id ID` | 使用原持久输入接续 publish_material；成功过则返回原回执 |

只读 CLI 成功为 `{"status":"OK","commit_state":"not_applicable","data":…}`。init 成功使用同一形状，但 commit_state=committed，data 含新 store_id；write/resume 直接返回提交 envelope。命令只输出一行 JSON；`--help` 提供普通帮助文本。CLI 路径输入必须是有界普通文件，不接受 FIFO 或符号链接作为请求文件。

初始化在创建目标前失败为 not_committed；创建过程中结果未能完整确认时为 unknown，保留目录供核对；完成持久化后的重开检查失败仍为 committed，并在错误 details 中保留 store_id。失败后核对原目标，不能覆盖已存在的目录。只读命令的输出包装超限仍为 not_applicable。

初始化在创建目录前检查路径和完整身份的 JSON 表示，再把同一份已验证字节保存到数据库及身份文件。路径不能包含 NUL；Ledger 的解析后路径会进入 UTF-8 JSON 绑定，不能包含无法以 UTF-8 表示的文件名字节。这类输入返回 INVALID_INPUT / not_committed，不留下新知识目录；完整身份还须满足 256 KiB 及严格 JSON 结构限额。修正输入后可使用原目标继续初始化，既有 Ledger 不会因此被迁移或改名。

路径解析或目标探测遇到权限拒绝、文件名超过文件系统限制、符号链接循环，或相对路径的当前工作目录已消失时，初始化返回 IO_ERROR / not_committed；已有库打开时无法解析相对路径则返回 IO_ERROR / not_applicable。这些错误保留原文件及链接，不创建替代位置，修正实际原因后可沿原入口继续。可正常解析的 Ledger 链接仍绑定其解析后的路径。

打开既有库默认使用只读连接。若 SQLite 明确返回 `SQLITE_READONLY_ROLLBACK`，表示上次中断留下的热回滚日志必须先恢复，则重新核对私有文件与身份标记，针对同一既有库以 `mode=rw` 重开一次，由 SQLite 的锁和日志机制回滚未提交页面，随后核对库格式、身份并启用 query_only。不会手工删除日志、重放请求或初始化替代库。这个文件层恢复可能写回数据库，但不新增领域状态、回执或事件；只读 API/CLI 仍为 not_applicable。

所有连接在 SQLite 接触既有回滚日志前执行同一检查，包括已有库对象发起的写入和恢复重开：日志若存在，须为当前用户拥有、无组/其他用户权限且仅一个硬链接的普通文件。符号链接、悬空链接、FIFO、目录或不合要求的文件均拒绝并保留；日志不存在或已被并发提交删除是正常状态。此检查不通过时不会先让 SQLite 恢复或清理该日志。

库级 open_store 的失败统一为 not_applicable，表示打开与观察失败；写请求在提交前被同一检查拒绝仍为 not_committed。两者均不否认同键可能已有的历史提交。

知识目录必须仍是当前用户拥有、无组/其他用户权限的真实目录；每次连接（包括已有对象的调用及热日志恢复重开）都会重新检查，然后核对数据库、身份文件和回滚日志。连接时发现目录权限改变、变为符号链接或消失即拒绝，不重建目录；新材料发布不能绕过首次 K 连接检查直接写入 Ledger。恢复符合原约束的目录后，可继续使用原库对象和原请求。

身份文件上限为 256 KiB，新打开和已有对象每次连接使用同一有界读取。打开时不跟随最终文件名的符号链接，并使用非阻塞方式；对实际打开的文件再次检查普通文件类型、用户归属、私有权限、硬链接数及大小。读取最多取上限加一个检测字节，文件在大小检查后变大仍拒绝，不整文件载入或截断后接受；身份内容仍须与原绑定完全一致。

SQLite 内保存的身份副本同样按 256 KiB 的严格 JSON 解码，再与文件中的身份核对；重复字段、过深嵌套、缺失或非文本值均拒绝。读取该副本时临时限制 SQLite 单值/行的大小，先阻止超大值载入 Python，再检查 JSON 字节限额；检查后恢复连接原限额，不改变其他记录及诊断的契约。身份副本异常返回 STORE_INVALID，读取为 not_applicable、尚未开始写入的请求为 not_committed，原件保留，修正后可沿原请求继续。

仍在执行的写入者继续受 SQLite 锁保护，忙错误不触发上述回退。健康的只读库不要求改为可写；需要恢复但写权限不足或 I/O 失败时返回 IO_ERROR，保留文件，修正实际原因后可再次打开。损坏、格式或身份矛盾不会按该条件重试。本机制限于当前本地 SQLite 的中断事务恢复，不是组合快照启用或任意掉电恢复保证。

## 请求形状

每个请求恰含：

```json
{
  "schema": "operational-knowledge-v1",
  "scope": "example:operator",
  "operation": "publish_material",
  "request_id": "request:material-001",
  "body": {
    "material_id": "material:build-001",
    "source_ref": "example:build-tool",
    "task_ref": "task:build-001",
    "context": {"platform": "linux"},
    "claimed_outcome": "FAILED",
    "gaps": [],
    "text": "Synthetic build log: generated configuration was absent.\n"
  }
}
```

请求幂等键为 `(scope, operation, request_id)`，指纹覆盖完整规范化请求。对象键排序、数组保留顺序、字符串不做 Unicode normalization；相同键换内容冲突。scope 是去重边界；它和 reviewer、source_ref 都不构成身份认证或授权。

material_id 在一个知识库内全局唯一；候选的逻辑去重键为 `(scope, proposal_id)`。候选、审查和知识版本的对象 ID 由 K 创建后永久保留，不按文本相似度合并。

| operation | body 必需字段 | 结果 |
| --- | --- | --- |
| `publish_material` | `material_id source_ref task_ref context claimed_outcome gaps text` | 保存有界 UTF-8 原件到 Ledger，再提交材料记录 |
| `register_material` | 同上，将 `text` 替换为 `reference` | 读取已有准确版本、核对全部预期内容，再登记材料引用；不写 Ledger |
| `propose` | `proposal_id kind content conditions material_ids` | 引用已有材料，创建待审候选；同 scope/proposal_id 同内容返回同一候选 |
| `review` | `candidate_id candidate_sha256 decision reviewer reason evidence_material_ids` | 同候选一个终态决定；ACCEPT 创建一个有效知识版本，REJECT 不创建知识 |

未知字段、重复 JSON key、非法 UTF-8、BOM、孤立 surrogate、浮点数、NaN、Infinity 均拒绝。所有字段均必需，不隐式填补缺少的来源、条件或缺口数组。

- 稳定 K ID 长 3–128 个 ASCII 字符，首字符为字母或数字，其余允许字母、数字、`.`、`_`、`:`、`/`、`-`。scope 最多 128 UTF-8 bytes。
- source_ref、task_ref、reviewer 最多 512 UTF-8 bytes，reason 最多 4096 bytes；字符串拒绝 NUL。task_ref 同时拒绝 U+0000–U+001F 和 U+007F 控制字符，与 Ledger 来源引用约束一致；在保存发布意图或写 Ledger 前检查。
- `claimed_outcome` 为 SUCCEEDED、FAILED、CANCELLED 或 UNKNOWN，表示调用方声明，K 不验证外部任务执行事实。
- `context`、`conditions` 为最多 16 项的字符串映射；键匹配 `[a-z][a-z0-9_]{0,31}`，值 1–256 UTF-8 bytes。
- `kind` 为 1–64 UTF-8 bytes 的类型名；content 为 1–65536 UTF-8 bytes，text 允许空内容且至多 65536 bytes。
- gaps 最多 16 个非空字符串，每项最多 512 bytes。material_ids、evidence_material_ids 各为 1–8 个不同材料 ID；候选与审查依据合计最多八个材料。
- candidate_sha256 必须等于 propose 返回值，绑定候选的全部输入。decision 仅为 ACCEPT 或 REJECT。

## 准确材料引用与 Ledger 绑定

初始化时指定已有 Ledger 的数据库路径和一个已知 Artifact ID 作为逻辑锚点。K 保存其完整不可变 Artifact metadata，并在材料操作时通过公共接口重新核对；路径相同不代表绑定相同。锚点可以是调用方已确认的现有 Artifact，也可以由调用方通过 Ledger 公共入口建立；[合成示例](../examples/material_to_knowledge.py)展示后者。

这个绑定识别所选逻辑数据域，不是物理实例互斥、认证或防快照回滚机制。复制同一 Ledger 会保留相同锚点；本版不提供恢复副本启用或多副本写入协调。知识目录和 Ledger 路径均是本机私有配置。

reference 恰含 artifact_id、version_id、entries。entries 与实际根内容完整对应，顺序保持一致；不接受仅声明部分成员的完整性结论：

```json
{
  "artifact_id": "artifact:build-001",
  "version_id": "version:build-001",
  "entries": [
    {"entry_key":"@blob","blob_ref":"blob:build-001","sha256":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","byte_length":42}
  ]
}
```

以上摘要为格式示例，不代表任何真实内容。单 Blob 根使用固定 entry_key `@blob`；Manifest 根保留 Ledger 的逻辑 entry_key，不把它转换为文件路径。每个 entry 恰含上述四项。不同 Blob 的累计内容最多 256 KiB，最多八个 entry，每个 Blob 最多 64 KiB；byte_length 必须为整数，不能为 bool。

读取先确认 Version 属于 Artifact，再核对 Root、Manifest、Blob descriptor 和实际字节。task_ref 必须出现在该 Version 的 external_source_refs 中；publish_material 会按原输入设置该来源。此处只校对显式来源一致，不声称 metadata 能证明观察真实、替调用方解析日志或授予行动权限。

本版兼容 Ledger `0.2.0a1` 的 `bounded-local-v0.1` 公共接口。实测源码为 `be50effeced6ba92d74f24bf5cb4478c9a65fc9d`，与原设计基线 `58b08fa9cb188c48feec45592a0f775c7178fd7a` 的差异仅为 README。运行代码仅导入 Ledger 根包，不读取其私有表。

## 提交、恢复与当前观察

publish_material 先在 K 提交原请求及待决状态，随后依固定身份调用 Ledger create/append，最后提交 K 材料引用及成功回执、移除待决输入。原件成功写入但 K 尚未完成时，可以只存在原件；这不是跨库原子事务，也不重新执行生产任务。原输入在待决期间保留，成功后 K 保存结构化材料引用及请求指纹，原件从 Ledger 读取。

resume 核对原 Ledger 操作的幂等身份、指纹及结果；尚未找到时只重试同一确定请求，由 Ledger 的同键事务幂等保证收敛。它不换对象 ID 或 key，也不把一次 NOT_FOUND 当作另起一次写入的依据。失败不会永久封禁该请求；身份冲突应先核对，不能重命名绕过。

读取或接续 K 持久记录时也核对绑定：待决输入必须匹配原请求键、指纹和材料预留；回执必须匹配查询键、库身份、指纹索引及同事务保留的完整事件回执，结果 ID、reused 和时间也不能单独替换。审查重新计算候选内容摘要，知识版本读取核对已接受候选、审查依据及选中记录的索引。字段、摘要或关联不一致返回 STORE_INVALID，保留原记录，不执行不同的请求或用重算新摘要掩盖内容改变。这是当前操作所涉及记录的一致性检查，不是整库审计、密码学防篡改或快照回滚检测。

材料还需对应唯一的初次创建回执及其事件原件；后续 reused 回执不能替代初次创建绑定。register_material 直接核对结构化材料、准确引用与原请求指纹；publish_material 读取原文字节后重建原请求，核对来源、任务、上下文、结果声明、缺口和正文，并核对原请求确定的 Artifact/Version/Blob 身份。即使另一版本正文完全相同，也不能替换原版本。发现材料绑定不一致时拒绝读取、查询或新准入，原成功回执仍单独保留。

发布原件不可读时，仍可返回结构化记录与明确的材料 UNAVAILABLE/UNKNOWN，但不能据此声明全部材料绑定已重新验证。相同逻辑材料换请求 ID 时，利用这次提供的原正文核对已有记录及初次请求，不产生新原件；该复用回执仍不证明原件当前可读。本轮检查兼容既有数据格式，不补写摘要或迁移数据库。

pending 提供原请求及最近失败诊断；诊断 JSON 损坏或报告超限可能使这次报告读取失败。resume 独立读取并核对原持久意图或成功回执，不依赖诊断报告的解码和输出大小。诊断错误不删除原件，也不阻止已验证原请求按原 Ledger 身份接续；请求或回执自身的矛盾仍拒绝。

写成功返回 status=COMMITTED、commit_state=committed、replayed、receipt、observation。receipt 是不可变的原提交记录，含 store_id、scope、operation、request_id、fingerprint、recorded_at、result。result 包含 material_id，或 candidate_id/candidate_sha256，或 review_id/version_id，以及 reused。REJECT 的 version_id 为 null。

replayed 表示同请求重放；reused 表示首次处理该请求时复用既有逻辑对象。更换请求 ID 不会重复创建同一材料、提案或等价审查。同材料尚在另一原请求中发布时返回 PENDING，调用方接续原请求。已决候选再次审查只允许完全相同的决定、主体、理由与依据；矛盾决定冲突。

observation 与历史 receipt 分开，包含本次读取时间、对象当前状态和逐材料 VERIFIED / UNAVAILABLE / UNKNOWN。record_status=AVAILABLE 仅表示该次对象记录观察可返回，不证明每份材料现在可用，更不构成行动授权。当前对象/关联记录缺失或不一致时 record_status=UNAVAILABLE，临时读取故障等不能确定状态时为 UNKNOWN；此时 record 和 materials 均为 null，并附 error.code、message、details。

已核对成功的 receipt 继续返回；观察失败不冒充原操作 NOT_FOUND，也不使原提交重新执行。回执本身与原事件不一致时仍报错，不能用“当前未知”掩盖。直接 get/read-material/query 遇到相关错误仍拒绝；新审查 ACCEPT 必须重新核对材料可读，REJECT 可以保存对现存但不可读材料的拒绝判断。

query 按 kind 相等且 conditions 的每个键值均在 context 中精确匹配筛选 ACTIVE 版本；结果按 version_id 排序，limit 为 1–20，after 为上一页游标，末页 next_after=null。每页是一次独立观察；发生并发更新时不宣称跨页快照。返回知识文本、精确版本、内容摘要、审查/候选引用及已核对材料。匹配结果的必需材料不可用时明确失败，不静默漏掉该知识。查询不登记实际采用，usage_recorded=false。

## 限额与错误

请求最多 256 KiB，JSON 深度 12、节点数 10000；输出最多 1 MiB，超限拒绝且不截断，查询可缩小分页。错误响应也受此限额约束；诊断本身超限时返回简短 LIMIT，保留已知 commit_state。SQLite 主库最多 256 MiB；空间或锁问题不允许自动另建空库。目录 0700、文件 0600，要求当前用户拥有；活跃数据库只用于可靠本地文件系统。

KnowledgeError 提供 code、message、commit_state、details；CLI 错误为同名字段加 status=ERROR。常见 code：INVALID_INPUT、UNSUPPORTED_VERSION、LIMIT、NOT_FOUND、CONFLICT、IDENTITY_CONFLICT、PENDING、MATERIAL_UNAVAILABLE、BUSY、STORE_INVALID、IO_ERROR。Ledger 错误通过 details.ledger_code 和 ledger_commit_state 保留来源分类。

| commit_state | 含义 |
| --- | --- |
| `not_committed` | 本次 K 领域操作未提交；不否认同键的旧提交 |
| `pending` | 原发布意图已保存，材料尚未确认完整提交到 K；Ledger 可能部分或全部写入 |
| `unknown` | 本地提交结果待核对；先查询原 operation/pending |
| `committed` | 原 K 提交或初始化已确认，后续输出或观察仍可能失败 |
| `not_applicable` | 只读观察，不表示有写入被撤销 |

CLI 成功退出 0，NOT_FOUND 为 3，CONFLICT/IDENTITY_CONFLICT 为 4，BUSY 为 6，其他领域错误为 2，文件/输出 I/O 失败为 1。成功或错误响应的写入、刷新失败均按 I/O 失败处理；已处理的 stdout 失败不会在解释器退出时再次刷新并覆盖退出码。进程被杀或输出管道失效可能没有完整 JSON；不能单凭退出码认定未提交，按原请求核对。
