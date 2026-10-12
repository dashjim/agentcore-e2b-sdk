# E2B 测试环境与真实服务验收

检查日期：2026-10-12。本文区分 E2B 官方服务、官方 Python SDK 和本项目的 AgentCore SDK。

## 当前结论

测试人员可以使用 E2B 官方云服务建立对照基线，需要自行注册账户并配置 `E2B_API_KEY`。首轮验收环境没有配置该凭证，因此测试脚本将真实 E2B 测试记录为 **SKIPPED**。脚本没有发出 E2B API 请求，也没有创建 E2B 沙箱。官方 SDK 安装成功、测试脚本的本地用例通过，均不能替代真实服务验收。

E2B 官方 Quickstart 说明了账户注册和 API key 获取流程。测试人员应以注册时控制台显示的试用额度、有效期和收费条款为准；本项目不提供共享凭证或免凭证沙箱。

- [E2B Quickstart](https://e2b.dev/docs/quickstart)
- [E2B Pricing](https://e2b.dev/pricing)
- [E2B Console](https://console.e2b.dev/)
- [固定 SDK 版本：e2b 2.51.0](https://pypi.org/project/e2b/2.51.0/)

## 测试人员如何运行

测试人员先从 E2B Console 获取 API key，再通过进程环境或 CI secret 注入 `E2B_API_KEY`。测试人员不要把 key 提交到代码库或粘贴进报告。脚本不自动读取 `.env`，也不会把密钥写入 JSON。

测试人员在仓库根目录安装固定版本的官方 SDK，并执行只读认证检查：

```bash
export TMPDIR="$(pwd)/.tmp"
mkdir -p "$TMPDIR"
python3 -m venv "$TMPDIR/e2b-venv"
"$TMPDIR/e2b-venv/bin/pip" install 'e2b==2.51.0'
"$TMPDIR/e2b-venv/bin/python" scripts/check_e2b.py --require-config
```

默认模式下，脚本通过 `Sandbox.list(limit=1).next_items()` 发出一次认证请求，不创建沙箱，也不输出现有沙箱的 ID 或元数据。测试人员增加 `--live` 后，脚本先执行认证检查，再创建一个有效期不超过 120 秒的临时沙箱：

```bash
"$TMPDIR/e2b-venv/bin/python" scripts/check_e2b.py \
  --live --require-config \
  --output "$TMPDIR/e2b-live-results.json"
```

本项目的 AgentCore SDK 本身不依赖 E2B 账户；真实 E2B 对照测试才需要该账户。测试人员负责在自己的环境中审查和保存本次生成的报告。

## 验收标准与测试用例

执行主体是 `scripts/check_e2b.py`；被测系统是 **E2B 官方云服务和官方 e2b Python SDK 2.51.0**。这些用例建立 E2B 基线，不单独证明本项目的 AgentCore SDK 达到相同行为；测试人员还需要运行对应的 AgentCore 验收测试。

| 用例 | 脚本执行的动作 | E2B 基线的通过条件 |
|---|---|---|
| E2B-01 认证 | 脚本读取一页沙箱列表 | E2B 服务成功响应，空列表也算通过 |
| E2B-02 创建 | 脚本创建专用临时沙箱，配置 120 秒自动到期 | E2B SDK 返回新沙箱对象与 ID |
| E2B-03 正常命令 | 脚本请求沙箱执行分别写 stdout、stderr 的命令 | SDK 返回 `stdout=hello-out`、`stderr=hello-err`、`exit_code=0` |
| E2B-04 Unicode 文件 | 脚本写入并读取中文、emoji、重音字符和换行 | SDK 返回文本逐字符一致 |
| E2B-05 二进制文件 | 脚本写入包含所有 256 种字节值和 NUL 的内容，再以 `format="bytes"` 读取 | SDK 返回字节逐项一致 |
| E2B-06 非零退出 | 脚本请求命令输出后以退出码 7 结束 | SDK 抛出 `CommandExitException`，异常保留退出码、stdout、stderr |
| E2B-07 清理 | 脚本在 `finally` 调用自己创建的沙箱的 `kill()`，再检查运行状态 | SDK 确认沙箱不再运行；脚本将清理失败计入整体失败 |
| E2B-08 缺配置 | 测试人员移除 E2B_API_KEY 后运行脚本 | 脚本返回 `status=skipped`，不发出 E2B API 请求 |

脚本仅在所有实际执行的用例及清理均通过时返回 `status=passed`。默认模式的 `passed` 只代表只读认证通过；完整 E2B 基线要求报告同时满足 `mode=live`、`status=passed`、6 项业务检查通过、`cleanup.status=passed`。缺少凭证时脚本默认退出码为 0，但 JSON 明确标记 `skipped`；CI 应加 `--require-config`，让缺少凭证时退出码变为 2。测试失败或清理失败时退出码为 1。

## 首轮执行摘要

测试人员使用官方 `e2b==2.51.0` 执行 `--live`，脚本返回以下去标识摘要；本公开仓库不包含内部原始日志：

```json
{
  "mode": "live",
  "status": "skipped",
  "api_key_present": false,
  "expected_sdk_version": "2.51.0",
  "sdk_version": "2.51.0",
  "checks": [],
  "cleanup": {"status": "not_needed"},
  "reason": "E2B_API_KEY is not configured; no E2B API requests made."
}
```

测试人员另外对脚本运行了 6 个本地替身用例：缺凭证跳过、只读模式不创建沙箱、完整流程与清理、业务断言失败仍清理、清理失败导致整体失败且不泄漏异常消息中的秘密、SDK 版本不匹配时拒绝测试。6 项均通过。替身用例只验证测试脚本的控制流程，真实 E2B 验收仍待配置 API key 后执行。
