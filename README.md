# AgentCore E2B 风格 Python SDK · 0.1.0

客户通过同步 Python SDK 调用 AgentCore Runtime，复用常见的 `commands`／`files` 调用方式。SDK 支持显式客户端、会话引用、前台命令与输出回调、基础文件操作和停止请求。部署开发者需要随 Runtime 发布本项目的确定性适配程序；适配程序不调用模型。

这是一版功能子集。客户需要修改 import、AWS 配置和生命周期调用；SDK 不承诺原版 E2B 的完整协议、类型身份、永久 kill、TTL、内存快照、后台进程、PTY 或有状态 run_code。

## 安装与本地测试

项目要求 Python 3.11+。在本目录执行：

```bash
export TMPDIR="$(pwd)/.tmp"
mkdir -p "$TMPDIR"
python3 -m venv "$TMPDIR/venv"
source "$TMPDIR/venv/bin/activate"
python3 -m pip install -e '.[test]'
python3 -m pytest tests -q
```

pytest 配置会把临时文件放入 `TMPDIR` 或项目 `.tmp` 目录。开发者也可以使用已有安装 pytest／boto3 的 Python：`python3 -m pytest tests -q`。本地集成测试运行真实文件处理程序和 Bash 子进程，但测试 transport 合成 AWS 事件，不能据此宣称 AWS 云兼容已验证。

## 客户调用示例

客户后端通过 AWS 凭证链获得 IAM 权限。客户将 Runtime ARN 放入环境变量，不能把 E2B API key 当成 AWS 凭证。

```python
import os
from agentcore_sandbox import Client, RuntimeTarget

with Client(
    region="us-west-2",
    templates={"python-tools": RuntimeTarget(
        runtime_arn=os.environ["SANDBOX_RUNTIME_ARN"],
        qualifier="DEFAULT",
        workspace="/home/agentcore-runtime-user/agentcore-sandbox/workspace",
        boot_file="/home/agentcore-runtime-user/agentcore-sandbox/run/boot.json",
        module_path="/var/task",  # CodeZip 模块目录；普通容器安装无需设置
    )},
) as client:
    sbx = client.sandboxes.create(template="python-tools")
    try:
        sbx.files.write("hello.py", "print('你好，AgentCore')")
        result = sbx.commands.run("python3 hello.py", on_stdout=print)
        assert result.exit_code == 0
        ref_json = sbx.runtime.export_ref().to_json()
        print(result.stdout)
    finally:
        try:
            sbx.runtime.stop()
        finally:
            sbx.close()
```

`close()` 只关闭本地资源；`runtime.stop()` 请求 Runtime 停止当前计算，后端仍可能用同一 Session ID 启动新计算。SDK 不将它包装成永久 `kill()`。客户应用应单独记录清理错误，避免清理异常覆盖业务错误。

客户应用通过 `SessionRef.from_json(...)` 读取保存的引用，再调用 `client.sandboxes.connect(ref)` 构造本地句柄。SDK 在首次远程操作时协商能力；旧 boot ID 会被 SDK／适配程序拒绝。客户应用自己维护用户与引用归属，并负责跨进程协调。

## 关键兼容边界

- SDK 的 `commands.run(timeout=60)` 限制本地等待；`timeout=0/None` 不设置总等待期限。`execution_timeout=300` 限制 Runtime 命令执行，范围 1–3600 秒。SDK 超时不表示远端进程已结束。客户回调应非阻塞，SDK 无法中断任意 Python 回调代码。
- SDK 不自动重放命令和文件写入。SDK 遇到结果不明时保留部分输出或 CreateAttempt；客户可以通过 `resume_create(attempt)` 恢复初始化，或通过 `stop_attempt(attempt)` 请求停止。
- SDK 保留非零退出异常中的 stdout、stderr、exit_code；SDK 对缺失终止事件、异常流和无效协议头不返回成功。
- 适配程序将文件和命令 cwd 限制在配置的工作区内，拒绝符号链接路径。文件上限 1 MiB，列表最多 1000 项，SDK 默认命令输出上限 8 MiB。适配程序只删除普通文件或空目录。
- Runtime 会记录输入命令；Base64 包装不隐藏内容。客户通过受控的容器凭证来源获取秘密，不在命令或 envs 中携带需要避开日志的秘密。
- SDK 首版只支持 IAM 调用、同步 Python；客户需要的 JWT、异步、PTY、文件流和共享会话目录属于后续版本。

## 部署与真实 AgentCore 测试

本项目的 `scripts/deploy.py` 使用官方 CodeZip API 创建独立测试 Runtime、私有 S3 工件桶和只读工件的执行角色。部署脚本配置 IAM 入站认证、60 秒 idle timeout 和 600 秒 max lifetime。部署脚本不会修改已有业务 Runtime；部署会使用当前 AWS 账户并产生服务用量。

```bash
python3 scripts/deploy.py deploy --region us-west-2
PYTHONPATH=src python3 scripts/test_agentcore_live.py
```

脚本将本次资源标识写入已被 Git 忽略的 `docs/deployment.local.json`；开发者应将该文件及真实测试日志保留在本地。开发者修改源码后运行 `python3 scripts/deploy.py update`；部署脚本更新 Runtime 版本并等待服务托管的 DEFAULT Endpoint 跟随更新。测试程序在 finally 中停止本次会话。若创建因 IAM 传播等原因中断，开发者可运行 `python3 scripts/deploy.py resume` 继续已记录的创建流程。需要删除本项目创建的云资源时执行：

```bash
python3 scripts/deploy.py cleanup
```

清理脚本不删除 CloudWatch 日志组，开发者应按自己的日志保留策略处理。

Dockerfile 也提供自定义镜像方式，容器工作区为 `/workspace`，与 CodeZip 的 `/home/agentcore-runtime-user/agentcore-sandbox/workspace` 不同；客户必须使用匹配的 RuntimeTarget。SDK 会显式传递 `workspace`、`boot_file` 和可选 `module_path`，因为真实 CodeZip 命令入口不继承应用的这些环境变量。执行助手按 Boot 文件的 UID/GID 对齐默认运行身份；客户命令不会因为原生命令入口默认 root 而自动以 root 执行。容器入口提供 `/ping` 和 `/invocations`；本地调试服务器默认只监听 127.0.0.1。

开发者可执行真实容器检查：

```bash
docker build --platform linux/arm64 -t agentcore-e2b-sdk:0.1.0 .
python3 scripts/test_container.py --image agentcore-e2b-sdk:0.1.0 --root-helper
```

测试脚本使用 `--network none`，不发布宿主机端口，并在结束后删除测试容器。

## E2B 对照测试

官方 E2B 云服务可以用于测试，需要用户账户和 API key。首轮验收环境没有配置凭证，因此真实 E2B 测试记录为 `skipped`。用户在自己的安全配置中设置 `E2B_API_KEY`，然后执行：

```bash
python3 -m pip install 'e2b==2.51.0'
python3 scripts/check_e2b.py --live --require-config
```

测试程序创建最多 120 秒的临时 E2B 沙箱，检查命令、文本／二进制文件、非零退出，并在 finally 中销毁。脚本默认不带 `--live` 时仅检查配置与只读认证。官方入口和检查结果见 [E2B 环境说明](docs/e2b-environment.md)。

## 文档

- [验收标准与测试用例](docs/acceptance.md)：开发者的发布门槛及执行步骤。
- [实际测试报告](docs/test-report.md)：测试程序的实际结果、环境限制和资源状态。
- [架构与职责](docs/architecture.md)：客户端、Runtime 服务与确定性适配程序的职责和兼容边界。
- [公开发布安全扫描](docs/security-scan.md)：扫描范围、工具结果及已知限制。

SDK 不承担恶意命令的同容器隔离；Runtime 的计算隔离、IAM 权限与客户应用的会话授权各有独立责任。
