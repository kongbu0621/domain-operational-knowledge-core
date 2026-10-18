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

普通 CLI 成功为 `{"status":"OK","commit_state":"not_applicable","data":…}`。write/resume 直接返回提交 envelope。命令只输出一行 JSON；`--help` 提供普通帮助文本。CLI 路径输入必须是有界普通文件，不接受 FIFO 或符号链接作为请求文件。

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
- source_ref、task_ref、reviewer 最多 512 UTF-8 bytes，reason 最多 4096 bytes；字符串拒绝 NUL。
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

写成功返回 status=COMMITTED、commit_state=committed、replayed、receipt、observation。receipt 是不可变的原提交记录，含 store_id、scope、operation、request_id、fingerprint、recorded_at、result。result 包含 material_id，或 candidate_id/candidate_sha256，或 review_id/version_id，以及 reused。REJECT 的 version_id 为 null。

replayed 表示同请求重放；reused 表示首次处理该请求时复用既有逻辑对象。更换请求 ID 不会重复创建同一材料、提案或等价审查。同材料尚在另一原请求中发布时返回 PENDING，调用方接续原请求。已决候选再次审查只允许完全相同的决定、主体、理由与依据；矛盾决定冲突。

observation 与历史 receipt 分开，包含本次读取时间、对象当前状态和逐材料 VERIFIED / UNAVAILABLE / UNKNOWN。材料缺失、损坏或绑定矛盾不抹掉原提交；临时读取故障不冒充材料已损坏。审查 ACCEPT 要重新核对材料可读，REJECT 可以保存对现存但不可读材料的拒绝判断。

query 按 kind 相等且 conditions 的每个键值均在 context 中精确匹配筛选 ACTIVE 版本；结果按 version_id 排序，limit 为 1–20，after 为上一页游标，末页 next_after=null。每页是一次独立观察；发生并发更新时不宣称跨页快照。返回知识文本、精确版本、内容摘要、审查/候选引用及已核对材料。匹配结果的必需材料不可用时明确失败，不静默漏掉该知识。查询不登记实际采用，usage_recorded=false。

## 限额与错误

请求最多 256 KiB，JSON 深度 12、节点数 10000；输出最多 1 MiB，超限拒绝且不截断，查询可缩小分页。SQLite 主库最多 256 MiB；空间或锁问题不允许自动另建空库。目录 0700、文件 0600，要求当前用户拥有；活跃数据库只用于可靠本地文件系统。

KnowledgeError 提供 code、message、commit_state、details；CLI 错误为同名字段加 status=ERROR。常见 code：INVALID_INPUT、UNSUPPORTED_VERSION、LIMIT、NOT_FOUND、CONFLICT、IDENTITY_CONFLICT、PENDING、MATERIAL_UNAVAILABLE、BUSY、STORE_INVALID、IO_ERROR。Ledger 错误通过 details.ledger_code 和 ledger_commit_state 保留来源分类。

| commit_state | 含义 |
| --- | --- |
| `not_committed` | 本次 K 领域操作未提交；不否认同键的旧提交 |
| `pending` | 原发布意图已保存，材料尚未确认完整提交到 K；Ledger 可能部分或全部写入 |
| `unknown` | 本地提交结果待核对；先查询原 operation/pending |
| `committed` | 原 K 提交已确认，后续输出或观察仍可能失败 |
| `not_applicable` | 只读观察，不表示有写入被撤销 |

CLI 成功退出 0，NOT_FOUND 为 3，CONFLICT/IDENTITY_CONFLICT 为 4，BUSY 为 6，其他领域错误为 2，文件/输出 I/O 失败为 1。进程被杀或输出管道失效可能没有 JSON；不能单凭退出码认定未提交，按原请求核对。
