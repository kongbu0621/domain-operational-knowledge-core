# 维护与接续

先读 [README](README.md)、[需求](docs/REQUIREMENTS.md)、[架构](docs/ARCHITECTURE.md)和[实施计划](docs/IMPLEMENTATION_PLAN.md)。当前只完成建仓与设计初始化，产品代码、安装和真实消费尚未实现。来源与当前决定见[开发记录](docs/DEVELOPMENT.md)。

## 当前方向

本仓承接计划内 K 方向的运行知识组件，名称为 `domain-operational-knowledge-core`。本次建仓不改变先推进 P 首版实现的顺序，也不把 K 先完成作为 P 的前置条件。

首版围绕运行材料、候选、显式审查、知识使用、效果反馈、修订与退休形成一个窄闭环。复用 Ledger 公共接口；独立应用也应能消费本仓，不导入 `control-production-plane` 私有对象。不恢复旧 `local-ai-growth` 开发，不复制其实现或继承其验证结论，不新建额外治理或检索仓库。

README 面向使用者；开发历史与接续信息放在开发记录。不存在的接口和命令应标为设计，不能写成安装说明。沿用 Owner 已明确的直接提交推送 `main` 偏好，不自行创建分支或 PR；该偏好不扩大部署、数据迁移或其他仓库修改范围。

## 工程规则来源

- 来源：`kongbu0621/engineering-sop`，固定基线 `9372247bdfa24f46694bf2435ef9917e155e166c`。
- 采用范围：`docs/principles/reusable-module-repository-boundaries.md`、`docs/workflow/verification-evidence-gate.md`；不声明采用全部 Established 或 Experimental 规则。
- Owner-mandated：该基线 `AGENTS.md` 所指向的 `docs/workflow/program-repository-documentation-gate.md` 与 `docs/principles/repository-formation-standard-v1.0.md`。保留三层设计、形成依据与真实授权；两项仍为 Provisional，建仓不等于 Git Authority Admission 完成。
- 采用优先级：`docs/workflow/project-adoption-and-precedence.md`。当前用户的明确建仓决定据实执行，具体适用范围见开发记录，不新增泛化审批门槛。
- 可读取来源：有权限的同级 `engineering-sop` checkout 或其固定提交；核对完整 SHA 后读取正式正文。本文件仅为索引，不复制私有规则正文，不作为上游规则替代品。
- 决定权：Owner。采用范围和基线不自动跟随上游改变；当前建仓决定不提升规则成熟度或证明新实现复用成功。
- 私有来源不可读时只处理受影响的决定，不泄露私有材料，也不据此假设新增权限。产品使用不依赖私有理论或 SOP 仓库。

## 实施边界

- 明确区分运行材料、候选、审查、知识版本和实际使用。写入成功不证明观察真实，审查者名称不证明身份认证。
- 候选提交必须具备稳定请求身份及内容绑定；不能因超时换 ID 重发。成功率和检索排名不能自动修改知识状态。
- 修订和退休保留历史；同一谱系至多一个当前有效版本。迟到效果必须关联原使用版本，不能改绑新版本或使旧版本重新有效。
- 一个受信数据域内工作。多应用共享不等于多租户隔离；不以标签、命名空间或审查者字符串冒充权限边界。
- 只经 Ledger 公共接口读取和保存制品；不操作它的私有表。跨库写入保留原请求和中间状态，不声称跨库原子提交。
- 普通本地或只读错误修正后有界继续；写入未知时核对原请求。已有结果和失败保留，不制造永久封禁，也不覆盖用户数据。
- 实际知识、原始对话、机器路径、凭据和任务内容保持私有；公开样例使用合成数据。许可证在首次代码交付前明确。
- 文档检查链接、术语和来源；实现后的事务、幂等、修订竞争、迟到反馈和恢复使用相应验证。未运行与通过分别报告。

当前设计是实施提案。已有授权内的普通修复直接推进；具体目标、权限或重要约束确实缺失时，只澄清依赖该缺口的动作。
