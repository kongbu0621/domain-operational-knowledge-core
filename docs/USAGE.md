# 安装与使用

当前 `0.1.0a1` 为源码交付的 alpha；未发布 PyPI 包。验证环境为 Linux / Python 3.12。当前功能和限额见[公共接口](INTERFACE.md)。

## 安装

准备本仓和 `infra-artifact-ledger` 的源码。Ledger 采用 `0.2.0a1`，已验证提交 `be50effeced6ba92d74f24bf5cb4478c9a65fc9d`；不要用相同版本号替代对来源的核对。在 K 仓执行，替换 Ledger 路径：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install setuptools==84.0.0 wheel==0.48.0
.venv/bin/python -m pip wheel --no-build-isolation --no-deps /absolute/path/to/infra-artifact-ledger -w dist
.venv/bin/python -m pip wheel --no-build-isolation --no-deps . -w dist
.venv/bin/python -m pip install --no-index --find-links dist domain-operational-knowledge-core==0.1.0a1
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

输出包含知识版本和材料引用。这验证库的独立调用，不表示真实生产效果、使用反馈或 Ledger A3 已完成。重复运行应选新目录；接续已有材料请求应使用 resume，不能覆盖示例目录。

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

query 返回知识文本、准确版本和材料来源。read-material 返回经校验的 Base64 字节；Manifest 的 entry_key 只是逻辑键，不应直接拼接为输出路径。查询不是实际使用登记；本版本尚无 use、feedback、revise 或 retire 接口。

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

resume 使用原持久输入核对和接续，不需重新提供文本。propose/review/register_material 均为单次 K 事务，丢响应后用原请求查询或原样重试。不要换请求 ID、改内容或重跑生产任务来猜测旧写入结果。身份冲突保留原件，核对所选库和原请求。

如果 pending 只是读取失败诊断或生成报告失败，仍可使用原 scope/request_id 调用 resume；它会独立校验原意图及 Ledger 请求身份，不依赖诊断报告。原请求或成功回执绑定错误不会因此被绕过，已有失败记录保持原样。

成功回执与材料当前可用性分开查看：历史已提交但 observation 显示 UNAVAILABLE/UNKNOWN 时，不据此认定旧操作失败或知识现在可以采用。缺库、损坏、权限或空间错误均不触发自动初始化。

当前记录无法完整观察时，operation 仍可返回已经核对的原 receipt；observation.record_status 为 UNAVAILABLE 或 UNKNOWN，record/materials 为 null，error 说明此次观察失败。不要把“历史提交已确认”当作“当前内容可以使用”。

STORE_INVALID 表示已定位的本地记录、索引或引用无法一致核对；保留原目录和请求调查原因，不能换请求 ID 绕过或自动重写记录。正常的上一版知识库可直接读取与接续，无需迁移；这不意味着未知来源的副本已具备恢复启用资格。

## 验证与当前限制

安装依赖后，从源码测试：

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q src tests examples
```

验证 wheel 时，在独立环境安装上述两个 wheel，从仓外用 `python -I -m unittest discover -s /absolute/path/to/this-repo/tests -v` 运行同一组测试，并运行合成示例；这样可核对安装包的实际调用路径。测试只使用临时数据，强制退出钩子只存在于测试脚本。

本版本不提供知识使用/反馈/修订/退休、P 自动接入、多租户鉴权或快照恢复启用。旧知识目录副本不能仅因引用齐全就直接作为当前库使用；完整生命周期及组合恢复按[实施计划](IMPLEMENTATION_PLAN.md)后续推进。
