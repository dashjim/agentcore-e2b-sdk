# AgentCore E2B 风格 SDK 0.1.0：实施与测试报告

本文保留首轮 SDK 验收记录。维护者随后使用真实 E2B 凭证完成了两轮共同用例及生命周期／超时专项，见 [双端对照测试报告](backend-comparison-2026-10-12.md)。下文 SKIPPED 只描述首轮环境，不能代表后续测试状态。

报告日期：2026-10-12。开发者已实现同步 Python SDK、Runtime 适配程序、部署工具与验收脚本。测试程序已完成本地、ARM64 容器和真实 AgentCore 验证；真实 E2B 对照测试因验收环境缺少 API key 而跳过。

**本文是验收结果的去标识摘要。** 维护者未在公开仓库中发布内部原始 JSON／JUnit 日志、部署状态文件、AWS 账户、云资源标识或会话标识。本文分别记录首版验收与公开副本复测，不把公开整理后的本地复测当作新的云测试。

## 需求与交付范围

客户应用希望沿用 E2B 常见的命令和文件调用方式访问 AgentCore。开发者实现了 `Client`、`RuntimeTarget`、`SessionRef`、可恢复的 `CreateAttempt`、同步 `commands.run`、输出回调和基础 `files` API，并提供验收标准、测试程序和部署脚本。

客户应用负责 AWS 凭证、用户到会话的授权、模板配置和跨进程协调。客户端 SDK 负责参数校验、会话引用、AWS 调用和命令事件解析。Runtime HTTP 适配程序负责初始化、能力协商及文件操作；命令助手负责检查 boot ID、对齐 Unix 身份并启动 Bash。AgentCore Runtime 服务负责会话计算环境与原生命令执行；本实现不调用模型。组件关系见 [架构与职责](architecture.md)。

SDK 提供的是 E2B 风格功能子集。客户需要调整 import、配置和生命周期调用。SDK 明确拒绝未实现的永久 kill、单会话 TTL、后台进程、PTY、内存快照和有状态 `run_code`，不宣称完整替代原版 E2B。

## 首轮测试结果

| 执行主体 | 被测系统 | 结果 |
|---|---|---|
| pytest | SDK、适配程序、本地真实 Bash／文件系统及 E2B 检查脚本的控制流程 | **145 通过，0 失败，0 跳过** |
| `scripts/test_container.py` | 最终 ARM64 镜像内安装的 HTTP 服务与命令助手 | **27 项检查通过**；测试程序已删除测试容器 |
| `scripts/test_agentcore_live.py` | 真实 AWS AgentCore Runtime、SDK 与适配程序 | **11 通过，0 失败**；服务已接受会话停止请求 |
| 独立安装检查程序 | 首轮构建的 wheel 与公共 Python 导出 | **通过**；该检查针对内部首轮构建产物，不代表公开副本重新构建后的安装验证 |
| `scripts/check_e2b.py --live` | 官方 E2B 云服务与官方 SDK 2.51.0 | **SKIPPED**；缺少 `E2B_API_KEY`，脚本未发出 E2B API 请求 |

测试程序在本地使用 Python 3.12.3、pytest 8.3.4、boto3 1.43.6、botocore 1.43.6。项目声明 Python 3.11+，首轮没有执行完整 Python 版本矩阵。测试程序通过 CodeZip Runtime 的 `DEFAULT` Endpoint 执行真实 AgentCore 测试。

本地 145 项由 SDK 测试 86 项、适配程序测试 39 项、本地集成测试 14 项和 E2B 检查脚本测试 6 项组成。本地集成测试执行真实 Bash 和文件操作，但其 transport 合成 AWS 事件；上述 11 项云测试才验证了本次部署的 AWS 调用链路。

容器测试程序关闭容器网络、不发布宿主机端口，并模拟原生命令入口以 root 启动、HTTP 应用以 UID 10001 启动的情形。命令助手在启动客户 Bash 前切换到 HTTP 应用身份。普通非 root 入口的容器检查也通过了相同的 27 项检查。

## 公开副本复测

维护者整理公开副本时去除了环境标识与内部日志，并将 `tests/conftest.py` 和 `scripts/deploy.py` 的临时文件位置改为 `TMPDIR` 或项目 `.tmp` 目录；维护者未修改 `src/` 中的核心 SDK 与适配程序。

2026-10-12，pytest 在公开副本再次执行全部本地用例，结果为 **145 通过，0 失败**，耗时约 2.32 秒。复测环境为 Python 3.12.3、pytest 9.1.1、boto3 1.43.111、botocore 1.43.111。该复测确认公开副本的本地功能用例仍然通过；维护者没有在这一步重新运行真实 AgentCore、E2B 或容器测试。公开发布前的扫描范围与结果见 [安全扫描报告](security-scan.md)。

## 验收标准覆盖

开发者在 [验收标准与测试用例](acceptance.md) 中定义 AC-01 至 AC-16；以下映射说明首轮证据能够支持哪些结论。

| 验收标准 | 测试程序覆盖的重点 | 测试位置与结论 |
|---|---|---|
| AC-01、02、03 | 测试程序验证文件与命令互通、Unicode／二进制、目录属性、回调与非零退出字段 | `tests/test_sdk.py`、`tests/test_adapter.py`、`tests/test_integration.py` 通过；云测试也覆盖这些行为 |
| AC-04、05、06 | 测试程序注入无效头、断流、流内异常、缺失／重复终止事件、阻塞或无输出响应，并区分等待超时与执行超时 | SDK 故障测试通过；云测试验证两种超时 |
| AC-07、08、09、12 | 测试程序检查旧 boot ID、stop 响应丢失后的 token 复用、本地 close、初始化失败后的原会话恢复 | 本地测试通过；容器测试验证重启后的旧引用拒绝；云测试验证旧 boot ID 拒绝及停止请求获受理 |
| AC-10、11、13 | 测试程序请求不支持能力、越界路径、符号链接、超限文件、非空目录及同 client 并发／重入 | 本地测试通过；SDK 不静默降级或自动重放 |
| AC-14 | 测试程序通过 IAM 签名调用真实 Runtime，解析原生命令事件 | 云测试 11 项通过 |
| AC-15 | 测试程序使用官方 E2B SDK 执行公共能力子集 | **未完成真实对照**；脚本因缺少凭证而跳过 |
| AC-16 | 测试程序检查独立入口的 cwd、模块路径、环境变量与 Unix 身份差异 | 本地、容器及云端可执行文件／相同身份测试通过 |

真实 AgentCore 的 11 个用例分别为：创建会话、Unicode 文件写入与执行、二进制往返、目录元数据、可执行文件及身份一致性、stdout／stderr 回调、非零退出异常、目录创建与删除、执行超时、本地等待超时、旧 boot 引用拒绝。

开发者据此确认首版达到“本地功能通过”和“AgentCore 实测通过”的门槛；开发者尚不能确认“E2B 双后端对照通过”。测试程序没有执行长期负载、跨进程并发协调或多个 AWS 区域的验证。

## 开发者依据云端实测完成的修正

1. **Runtime 应用需要可写目录。** 实测 CodeZip 环境将 `/var/task` 作为只读代码目录。部署脚本将工作区放在 Runtime 应用用户的 home 下，将启动标识放在工作区以外的 `run/boot.json`。
2. **原生命令入口没有继承应用环境。** 实测 `InvokeAgentRuntimeCommand` 从 `/` 启动，使用独立的 Python 路径和 root 身份。SDK 因此在 `RuntimeTarget`／`SessionRef` 中保存并显式传递 `workspace`、`boot_file` 和可选 `module_path`。命令助手依据启动标识文件的 UID/GID 切换身份，并设置对应的 HOME、USER、LOGNAME；云测试确认 HTTP 文件操作和客户命令使用相同身份。
3. **AgentCore 服务管理 DEFAULT Endpoint。** 部署脚本在更新 Runtime 后等待 DEFAULT 跟随新版本，避免对 DEFAULT 发起会产生冲突的显式 Endpoint 更新。
4. **AWS 响应不总是携带 session ID 头。** SDK 在响应提供该头时校验一致性，并继续通过请求关联标识和 boot ID 校验应用协议。
5. **AgentCore 创建校验需要适当的角色信任范围。** 部署脚本按服务主体、当前账户和当前区域 AgentCore ARN 限制信任关系；角色的数据权限仅覆盖本项目工件前缀及 Runtime 日志前缀。

这些观察来自本次部署，客户应在自己的区域、Runtime 版本和部署方式中复测。

## 实现与验证边界

- SDK 的等待超时只结束客户等待，不保证远端进程已经结束。客户提供的同步输出回调必须及时返回；SDK 不能强制中断任意回调代码。
- SDK 不自动重放命令或文件写入。服务结果不明时，客户应根据异常和 CreateAttempt 处理恢复，避免重复业务副作用。
- Runtime 服务接受 stop 请求不等于永久删除 Session ID。SDK 不把停止请求受理解释为永久 kill 语义。
- 适配程序约束文件 API 和命令初始 cwd；任意 Bash 命令仍具有该 Unix 用户在容器中的权限。客户不能把路径检查当作同容器内恶意命令的隔离机制。
- 适配程序限制单文件为 1 MiB、单次目录列表为 1000 项；SDK 默认限制命令输出为 8 MiB。
- 测试通过只代表上述用例在记录的环境中通过，不能证明完整 E2B 兼容或不存在安全缺陷。

## 测试人员如何复跑

测试人员从仓库根目录执行下列命令。测试人员需要先按 [README](../README.md) 安装项目；云测试需要测试人员自己的 AWS 账户、部署权限和运行费用预算。

```bash
export TMPDIR="$(pwd)/.tmp"
mkdir -p "$TMPDIR"
python3 -m pytest tests -q
python3 scripts/deploy.py deploy --region us-west-2
PYTHONPATH=src python3 scripts/test_agentcore_live.py \
  --output "$TMPDIR/agentcore-live-results.json"
docker build --platform linux/arm64 -t agentcore-e2b-sdk:0.1.0 .
python3 scripts/test_container.py --image agentcore-e2b-sdk:0.1.0 \
  --root-helper --output "$TMPDIR/container-results.json"
```

部署脚本在本地生成 `docs/deployment.local.json`；公开仓库不提供可共享的部署状态或测试 Runtime。测试程序在 finally 中停止本次 AgentCore 会话，维护者仍需管理自己创建的 Runtime、S3 工件桶与执行角色。客户不再需要测试环境时，可执行 `python3 scripts/deploy.py cleanup`。清理脚本删除本地状态文件记录的 Runtime、工件及桶、执行角色；脚本不删除 CloudWatch 日志组，客户应按日志保留策略处理。

测试人员配置 `E2B_API_KEY` 后，可按照 [E2B 环境说明](e2b-environment.md) 执行官方服务对照测试。脚本的 `--require-config` 参数会将缺少配置转为非零退出，防止 CI 把跳过误判为通过。
