# 安装与使用

当前 `0.1.0a3` 为源码交付的 alpha；未发布 PyPI 包。验证环境为 Linux / Python 3.12。当前功能和限额见[公共接口](INTERFACE.md)。

## 连接已有实例

使用已有知识时，先从该实例的部署或接续配置确认运行环境、数据目录和原成功回执。它们与代码仓库分别管理：

| 对象 | 保存什么 | 怎样选择 |
| --- | --- | --- |
| K 代码仓库 | 程序、接口与通用说明 | 用于开发和构建；拉取代码不等于更新运行安装 |
| Python 虚拟环境 | 已安装的 K、Ledger 及依赖 | 使用该机器、该项目或运行角色已选定的解释器；目录不必叫 `.venv` |
| K 数据目录 | 知识版本、审查、使用、反馈和原请求记录 | 从当前实例配置取得，作为 `--store`；不是源码目录或虚拟环境目录 |
| Ledger 材料库 | 知识依据及反馈原文的准确版本 | 由既有 K 实例绑定；普通查询不需要重新指定或创建它 |

同名项目在不同机器上可以使用不同路径。下面均为占位路径，须替换为本机已核实的选择。若数据在另一台机器或 VM 内，先通过已有获准连接到数据所在环境，再调用那里的库或 CLI；K 本身没有网络服务，宿主机的同名路径不自动指向 VM。运行资料与凭据留在私有配置中。

```sh
K_PYTHON=/absolute/path/to/selected-runtime/bin/python
K_STORE=/absolute/private/existing-knowledge
K_CONTEXT=/absolute/private/task-context.json
```

确认所选环境已安装兼容的 K/Ledger，运行用户有权访问该实例。可以用配置中保留的原 `scope`、`operation` 和 `request_id` 调用 `operation`，将返回回执的 `store_id` 与 `fingerprint` 同可信原记录比较；路径或一次成功响应不能单独证明实例选择正确。恢复副本还须按既有接续记录确认已选为当前实例，普通查询不会替用户完成切换。

已有实例无需运行 `init`、重新建库或重新建立材料锚点。位置尚未确定时，记录“未配置/未定位”；不要把一个新空目录当成原知识库。首次安装见[安装](#安装)，首次建库见[使用现有 Ledger](#使用现有-ledger)。

### 填写上下文并查询

将真实任务的上下文保存为私有 JSON 文件，例如：

```json
{
  "project": "example-component",
  "task_type": "component-documentation"
}
```

字段和值由调用者提供。查询要求 `kind` 相同，并且知识 `conditions` 中的每个键值都在 `context` 中精确匹配；上下文可以有额外字段。K 不从自然语言自动识别任务，也不做模糊语义匹配。例如，仅适用于 `component-documentation` 的知识不会匹配 `knowledge-entry-inspection`。这表示声明的适用条件未满足，不证明知识内容毫无参考价值；实际采用仍应核对原条件，不能仅为命中而改变任务标签。

```sh
"$K_PYTHON" -I -B -m domain_operational_knowledge_core --store "$K_STORE" \
  query --kind feedback-attribution-guidance --context "$K_CONTEXT"
```

成功响应的 `data.versions` 包含知识文本、准确 `version_id`、适用条件、内容摘要、审查引用及已核对的材料来源。若 `data.next_after` 非空，使用同一类型和上下文，把游标传给 `--after` 继续读取；每页是一次观察，不构成跨页快照。

| 结果 | 含义与后续处理 |
| --- | --- |
| 尚未配置或定位实例 | 调用尚未发生；先确定环境和当前实例，不能记作查询无匹配 |
| 查询成功且 `data.versions` 为空 | 本次类型、上下文和分页位置没有匹配；不能推断整个库没有知识 |
| 查询失败 | 保留退出码和错误，例如权限、材料不可读或实例不一致；修复后可有界重试原只读查询，不解释为空结果 |
| 查询成功并命中 | 阅读内容、条件与来源，判断是否适用于实际工作；查询本身没有登记采用 |

使用返回的准确 ID 读取版本及材料，下面的示例 ID 同样需要替换：

```sh
"$K_PYTHON" -I -B -m domain_operational_knowledge_core --store "$K_STORE" \
  get --kind version --id version:returned-id
"$K_PYTHON" -I -B -m domain_operational_knowledge_core --store "$K_STORE" \
  read-material --id material:returned-id
```

`read-material` 返回经核对的 Base64 字节和准确材料引用。查询或读取成功证明本次可读，不等于已经采用，也不等于一次恢复或完整生产验收。

### 登记采用与真实结果

调用方确定要把知识用于后续实际工作后，通过 `write` 提交 `start_use`，在开始采用前取得回执。下面是请求字段示例：选择本任务自己的稳定请求/使用 ID，填入查询得到的准确版本，并保持真实任务上下文一致。

```json
{
  "schema": "operational-knowledge-v1",
  "scope": "example:independent-docs",
  "operation": "start_use",
  "request_id": "request:docs-start-001",
  "body": {
    "usage_id": "usage:docs-001",
    "consumer_ref": "example:local-agent",
    "task_ref": "task:docs-001",
    "version_id": "version:returned-id",
    "context": {
      "project": "example-component",
      "task_type": "component-documentation"
    }
  }
}
```

保存为私有的 `start-use.json` 后调用：

```sh
"$K_PYTHON" -I -B -m domain_operational_knowledge_core --store "$K_STORE" \
  write --request /absolute/private/start-use.json
```

K 会再次核对该版本仍为 ACTIVE、条件匹配且材料可读。回执只证明采用登记成功，不替代任务执行或行动授权。后续完成实际工作和核验后，使用同一 `usage_id` 提交 `complete_use`，其 `material.source_ref` 必须等于原 `consumer_ref`，`task_ref` 和 `context` 也必须与原使用一致。

以下反馈仅示意字段，尚未代表任何真实工作完成。实际反馈应填写观察到的结果、检查、交付引用及知识的具体作用，不能照抄示例充当成功记录：

```json
{
  "schema": "operational-knowledge-v1",
  "scope": "example:independent-docs",
  "operation": "complete_use",
  "request_id": "request:docs-feedback-001",
  "body": {
    "usage_id": "usage:docs-001",
    "material": {
      "material_id": "material:docs-feedback-001",
      "source_ref": "example:local-agent",
      "task_ref": "task:docs-001",
      "context": {
        "project": "example-component",
        "task_type": "component-documentation"
      },
      "claimed_outcome": "UNKNOWN",
      "gaps": ["字段示例，尚未执行真实任务。"],
      "text": "请替换为实际观察、验证和交付依据，以及该准确知识版本在工作中的具体作用。"
    }
  }
}
```

```sh
"$K_PYTHON" -I -B -m domain_operational_knowledge_core --store "$K_STORE" \
  write --request /absolute/private/complete-use.json
"$K_PYTHON" -I -B -m domain_operational_knowledge_core --store "$K_STORE" \
  get --kind usage --id usage:docs-001
```

收到反馈回执后，可按其中的 `feedback_id`、`material_id` 读取记录和实际材料，核对它们仍关联原使用与准确知识版本。方案形成、采用、修改、检查、提交与发布应按实际顺序描述；登记前已确定的方案不能归因为后来采用的知识。一次成功也不证明知识提高了成功率。反馈成为运行材料，是否据此修订或准入知识仍须明确审查。

响应丢失时先按原 `scope/operation/request_id` 查询回执或待决状态，再按[中断后接续](#中断后接续)处理。保留原请求，不换 ID 重做工作。上述流程可以由独立应用或本地 Agent 直接调用 K 公共接口完成，无需导入 P。


## 安装

以下步骤适用于首次安装、且尚无选定环境的情况。已有可用环境时，沿用该机器和运行角色的实际路径，先按[连接已有实例](#连接已有实例)核对；`.venv` 只是下面的新安装示例。

准备本仓和 `infra-artifact-ledger` 的源码。Ledger 采用 `0.2.0a1`，已验证提交 `be50effeced6ba92d74f24bf5cb4478c9a65fc9d`；不要用相同版本号替代对来源的核对。在 K 仓执行，替换 Ledger 路径：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install setuptools==84.0.0 wheel==0.48.0
.venv/bin/python -m pip wheel --no-build-isolation --no-deps /absolute/path/to/infra-artifact-ledger -w dist
.venv/bin/python -m pip wheel --no-build-isolation --no-deps . -w dist
.venv/bin/python -m pip install --no-index --find-links dist domain-operational-knowledge-core==0.1.0a3
```

运行时只依赖 Ledger 和 Python 标准库；无需 P、Local Hand、模型账户或网络服务。构建工具与 wheel 可以预先准备，安装后的本地调用可离线进行。

## 一个完整的合成示例

提供一个尚不存在的输出目录，其父目录应已存在：

```sh
.venv/bin/python examples/material_to_knowledge.py /absolute/private/new-knowledge-example
```

示例通过 Ledger 公共 API 创建独立合成材料库和锚点，然后：

1. 保存一份合成失败材料，取得精确制品版本。
2. 提出经验候选，确认待审候选不进入有效知识查询。
3. 由示例中明确的审查输入接受候选。
4. 重新打开知识库，查询匹配知识，按原材料版本取回字节并比较。

输出包含知识版本和材料引用。这验证库的独立调用，不表示真实生产效果或 Ledger A3 已完成。重复运行应选新目录；接续已有材料请求应使用 resume，不能覆盖示例目录。

## 完整生命周期示例

```sh
.venv/bin/python examples/knowledge_lifecycle.py /absolute/private/new-lifecycle-example
```

示例在独立合成目录完成：材料 → 候选与接受 → 精确版本使用登记 → 效果材料 → 审查修订 → 退休。重开后核对旧版本与原使用仍关联、最新版本不再进入主动查询。示例保留各请求 JSON，便于用 CLI 查询原回执；数据和效果均明确为合成，不代表 P 或真实 Agent 已采用。

若使用 0.1.0a1 创建的原知识库，先停用旧版调用者、保留一致性备份，再显式升级所选知识目录：

```sh
operational-knowledge --store /absolute/private/knowledge upgrade
```

普通打开不会升级。升级保留原记录、回执、事件、待决请求和 Ledger 绑定；不要用复制活动主库或重新初始化代替升级。新建目录已是格式 2，无需该步骤。

## 使用现有 Ledger

选择调用方已确认的一个 Artifact 作为该逻辑材料域的锚点；ID 和其完整 metadata 会被保存及核对。建立新的知识目录：

```sh
operational-knowledge --store /absolute/private/knowledge init \
  --ledger-db /absolute/private/ledger.sqlite \
  --anchor-artifact artifact:known-anchor
```

材料请求的完整 JSON 样例见[请求形状](INTERFACE.md#请求形状)。保存为私有普通文件后提交：

```sh
operational-knowledge --store /absolute/private/knowledge write --request /absolute/private/material-request.json
```

已有材料采用 register_material，提供完整准确的 reference；新文本原件采用 publish_material。两者均需要明确 source_ref、task_ref、context、claimed_outcome 和 gaps。K 不把文件名或最新版本当作来源，也不凭日志文本猜测任务身份。

propose 引用已登记 material_id；review 使用 propose 返回的 candidate_id 和 candidate_sha256，明确 ACCEPT 或 REJECT、reviewer、reason 和材料依据。审查字符串用于归因，调用者必须位于受信本地权限边界内。

context.json 可写为 `{"platform":"linux"}`。查询与历史读取：

```sh
operational-knowledge --store /absolute/private/knowledge query --kind build-guidance --context /absolute/private/context.json
operational-knowledge --store /absolute/private/knowledge get --kind version --id version:returned-id
operational-knowledge --store /absolute/private/knowledge read-material --id material:build-001
```

query 返回知识文本、准确版本和材料来源。read-material 返回经校验的 Base64 字节；Manifest 的 entry_key 只是逻辑键，不应直接拼接为输出路径。查询不是实际使用登记；开始采用前调用 start_use，完成后调用 complete_use，字段见[使用与反馈接口](INTERFACE.md#使用反馈和版本变更)。

反馈说明应分别记录已有观察、实际采用和后续结果。材料可以在使用登记前取回；登记前已确定的方案，不能事后写成新知识带来的效果。登记后按准确版本执行并取得结果，也不自动证明该知识提高了成功率。若反馈的顺序或归因有误，用 `correct_feedback` 引用当前 `expected_feedback_id`，追加更正材料，保留原反馈和同一使用版本；不重做业务来修正文字记录。

## 中断后接续

先使用原 scope、operation、request_id 核对成功记录：

```sh
operational-knowledge --store /absolute/private/knowledge operation \
  --scope example:operator --operation publish_material --request-id request:material-001
```

材料发布未完成或结果不明时：

```sh
operational-knowledge --store /absolute/private/knowledge pending --scope example:operator --request-id request:material-001
operational-knowledge --store /absolute/private/knowledge resume --scope example:operator --request-id request:material-001
```

resume 使用原持久输入核对和接续，不需重新提供文本。propose/propose_revision/review/register_material/start_use/retire 均为单次 K 事务，丢响应后用原请求查询或原样重试。不要换请求 ID、改内容或重跑生产任务来猜测旧写入结果。身份冲突保留原件，核对所选库和原请求。

反馈 complete_use/correct_feedback 也走原请求接续；pending/resume 加 `--operation complete_use` 或 `--operation correct_feedback`，不能使用默认的 publish_material 去查询另一操作。

如果 pending 只是读取失败诊断或生成报告失败，仍可使用原 scope/request_id 调用 resume；它会独立校验原意图及 Ledger 请求身份，不依赖诊断报告。原请求或成功回执绑定错误不会因此被绕过，已有失败记录保持原样。

成功回执与材料当前可用性分开查看：历史已提交但 observation 显示 UNAVAILABLE/UNKNOWN 时，不据此认定旧操作失败或知识现在可以采用。缺库、损坏、权限或空间错误均不触发自动初始化。

当前记录无法完整观察时，operation 仍可返回已经核对的原 receipt；observation.record_status 为 UNAVAILABLE 或 UNKNOWN，record/materials 为 null，error 说明此次观察失败。不要把“历史提交已确认”当作“当前内容可以使用”。

STORE_INVALID 表示已定位的本地记录、索引或引用无法一致核对；保留原目录和请求调查原因，不能换请求 ID 绕过或自动重写记录。正常的上一版知识库可直接读取与接续原材料请求；新增生命周期操作前显式 upgrade，这不意味着未知来源的副本已具备恢复启用资格。

SQLite 进程中断后若留下热回滚日志，打开原知识库会在 SQLite 明确要求时尝试一次原库回滚，再检查身份并读取。不要手工删除 `knowledge.sqlite-journal`，也不要复制仍在变化的单个数据库文件来绕过错误。忙错误先确认原写入者；恢复写权限或存储问题后沿原路径重开，再查询原 operation/pending。恢复未提交页面不会重做 Ledger 发布或生产任务，详见[公共接口](INTERFACE.md)。

## 验证与当前限制

安装依赖后，从源码测试：

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q src tests examples
```

验证 wheel 时，在独立环境安装上述两个 wheel，从仓外用 `python -I -m unittest discover -s /absolute/path/to/this-repo/tests -v` 运行同一组测试，并运行合成示例；这样可核对安装包的实际调用路径。测试只使用临时数据，强制退出钩子只存在于测试脚本。

本版本不提供 P 自动接入、多租户鉴权或快照恢复启用。旧知识目录副本不能仅因引用齐全就直接作为当前库使用；真实消费及组合恢复按[实施计划](IMPLEMENTATION_PLAN.md)后续推进。
