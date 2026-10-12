# E2B 与 AgentCore SDK 真实双端对照测试报告

日期：2026-10-12，时间均为 UTC。被测客户端：官方 `e2b==2.51.0` 与本项目 `agentcore-e2b-sdk==0.1.0`。

**结论：两边的表现不完全一样。客户应用可以复用基础命令和文件操作的业务代码，但当前 SDK 不能作为原版 E2B SDK 的无改动替代。** 测试程序在两个真实后端执行了两轮、每轮 28 项共同用例，另执行上下文管理器和超时专项。基础 Python 文件执行、文本／二进制往返等链路成功；文件参数、部分文件语义、异常类型、生命周期和超时后的副作用存在明确差异。

开发者没有为本次对照修改 `src/agentcore_sandbox/`。本次测试验证的是已发布的 0.1.0 核心实现，其基线提交为 [`70a7246`](https://github.com/dashjim/agentcore-e2b-sdk/commit/70a724647130079c2843e7dcd0575b857b918a6a)。开发者新增了对照程序和报告，并修正了测试程序对一种已观察到的输入拒绝异常的分类。

## 1. 测试程序如何定义“同样的客户端调用”

本次验证的是：**客户应用切换沙箱工厂与清理方法后，同一份业务函数能否得到一致的行为。** E2B 工厂调用官方 `Sandbox.create()`；AgentCore 工厂调用本项目 `Client.sandboxes.create()`。两个工厂各自提供一个 sandbox 对象和专用工作目录，随后测试程序调用相同的案例函数，不按后端重写业务操作。

例如，测试程序在两边都执行以下链路：

```python
def same_business_code(sandbox, workdir):
    source = "import json\nprint(json.dumps({'message': '你好', 'value': 6 * 7}, ensure_ascii=False))\n"
    info = sandbox.files.write(workdir + "/script.py", source)
    result = sandbox.commands.run("python3 script.py", cwd=workdir, timeout=15)
    content = sandbox.files.read(workdir + "/script.py")
    return info, result, content
```

测试程序在两边都验证了源码读回一致、退出码为 0、stdout 中的 JSON 为 `{"message": "你好", "value": 42}`。不过，两边返回的 WriteInfo 和 CommandResult 属于不同 Python 类。

本项目 SDK 通过 AWS IAM 签名调用 AgentCore API 和自己的适配协议；官方 E2B SDK 使用 E2B 的服务协议。本次测试没有让原版 E2B SDK 只修改 endpoint 就直接连接 AgentCore，也没有证明这种方式可行。

## 2. 测试环境与证据范围

| 项目 | E2B 后端 | AgentCore 后端 |
|---|---|---|
| 客户端 SDK | 官方 e2b 2.51.0 | agentcore-e2b-sdk 0.1.0 |
| 客户端测试进程 | Python 3.12.3 | 同一环境的 Python 3.12.3 |
| 服务端环境 | 官方默认模板 | 已部署的 CodeZip Runtime，版本 4，DEFAULT Endpoint |
| 沙箱 Python | 3.11.6 | 3.12.12 |
| 沙箱 CPU 架构 | x86_64 | aarch64 |
| 默认工作目录 | `/home/user` | `/home/agentcore-runtime-user/agentcore-sandbox/workspace` |
| 命令用户 | `user` | `agentcore-runtime-user` |
| 身份认证 | 用户提供的 E2B API key | 当前环境的 AWS 凭证链，区域 us-west-2 |

测试程序为每项用例建立独立子目录，避免前一项失败污染后续文件操作。测试程序保留原始观察，并仅在单独的比较视图中替换本次随机目录前缀、分离时间／用户／组等动态元数据。测试程序保留 `mode`、`permissions`、目录 size、回调分块、命令 stdout 中的绝对路径以及 Python 类型身份差异。

对照程序使用独立断言验证已知输出，不能因为两边共同失败就认定业务通过。报告把正常操作、预期异常和能力探针分别计数。异常记录只包含受限字段；测试程序不保存任意异常全文，对非白名单字符串保留类型、长度和去密钥后的摘要。因此，下文“所采集字段相同”不代表完整异常对象、所有方法或全部 SDK 契约相同。

## 3. 两轮执行结果

| 测试程序执行的阶段 | 时间范围 | 结果 |
|---|---|---|
| 共同用例 R01 | 07:04:37–07:05:08 | 两个后端各执行 28 项；18 项 core 预期全部满足；一个 bytearray 拒绝异常最初被测试程序归为 execution_failed |
| 共同用例 R02 | 07:07:02–07:07:33 | 两个后端各执行 28 项；18 项 core 预期全部满足；测试程序正确记录 bytearray 为双方拒绝输入 |
| `with sandbox` 专项 | 07:03:30–07:03:34 | 测试程序实证两边退出后的远端生命周期不同 |
| 有开始标记的超时专项 | 07:08:48–07:09:12 | 正向对照成功；测试程序观察到超时后的副作用和回调交付不同 |

两轮共同用例共计 **112 次后端用例执行**，不是 112 次 API 请求；每个用例可能调用多个 API。测试程序在两轮使用新建会话，主要业务观察和断言结果重复一致。R02 只修正了测试程序对官方 `InvalidArgumentException` 的精确分类，开发者没有改变 bytearray 输入或 SDK 实现；两边拒绝输入仍不计为业务成功。

28 项共同用例的业务口径如下：

| 类别 | 数量 | 测试程序实际观察 |
|---|---:|---|
| core 正向操作 | 15 | 两边都完成业务断言；返回类型、元数据等仍可能不同 |
| core 预期异常／超时 | 3 | 两边都满足各自的异常预期；异常契约和超时后行为不等价 |
| 边界探针，双方完成业务 | 2 | 覆盖写入保留权限、自动创建缺失父目录 |
| 边界探针，E2B 返回而 AgentCore 拒绝 | 7 | 具体见下表；这些调用不能直接等价迁移 |
| 边界探针，双方拒绝输入 | 1 | bytearray 写入；异常类型不同 |

因此，本次共有 **17 组正向业务操作在两边都成功**。这个数和“18 项 core 预期满足”含义不同，均不是整个 E2B SDK 的兼容率。

R02 的机器比较摘要为：5 项所采集字段及类型完全相同、23 项存在差异、0 项未分类执行故障；去掉 Python 类型身份后，有 13 项字段投影相同。该严格比较还包含回调分块、实际路径和元数据，因此不能把 23 项直接解释为 23 个功能故障。边界探针没有独立成功断言时，JSON 中 `both_contract_checks_passed` 的真值也不能证明支持该能力；报告以返回／拒绝结果及 `business_operation_succeeded` 为准。

## 4. 28 项共同用例的逐项结果

以下结果以 R02 为主；R01 的业务观察一致。表中“业务一致”仅指本项已经断言的内容，不包含原版 Python 类型身份。

| # | 测试程序执行的调用 | E2B 实际表现 | AgentCore 实际表现 | 判断与影响 |
|---:|---|---|---|---|
| 1 | 命令分别输出 stdout／stderr，退出 0 | 返回预期输出和 0 | 返回相同字段值 | 业务一致；CommandResult 类不同 |
| 2 | 命令输出后 `exit 7` | CommandExitException，保留输出和 7 | 同名但不同类的异常，保留输出和 7 | 关键业务字段一致；异常类型和附加字段不同 |
| 3 | 命令间隔输出并触发回调 | stdout 分为 `first`、`second` 两块 | stdout 为 `firstsecond` 一块 | 拼接内容一致；客户不能依赖相同分块次数 |
| 4 | 中文、emoji、重音字符文件往返 | 返回逐字一致的 str | 返回相同 str | 本项采集结果一致 |
| 5 | 写入全部 256 字节值及额外 NUL 等，再 bytes 读取 | 返回内容正确的 260 字节 bytearray | 返回相同类型和内容 | 本项采集结果一致 |
| 6 | 检查 files.write 返回值 | WriteInfo 的 name/type/path 正确 | 对应字段正确 | 字段值一致；类和 FileType 不同 |
| 7 | 列出文件与目录并检查元数据 | 文件 mode=420，permissions=`-rw-r--r--` | 文件 mode=420，permissions=`rw-r--r--` | 权限字符串格式不同；详见第 5 节 |
| 8 | 两次 make_dir 同一路径 | 第一次 True，第二次 False | 相同 | 本项采集结果一致 |
| 9 | 删除已存在普通文件 | 返回 None，文件消失 | 相同 | 本项采集结果一致 |
| 10 | remove 不存在的路径 | 返回 None | FileOperationError，code=NOT_FOUND | 缺失删除的幂等语义不同 |
| 11 | read 不存在的文件 | FileNotFoundException | FileOperationError，code=NOT_FOUND | 都拒绝；异常捕获代码需要适配 |
| 12 | cwd 含空格／引号／中文，env 值含 `$()` 等字符 | 环境值不被插值，cwd 正确 | 同样符合各自输入 | 业务一致；输出的实际目录自然不同 |
| 13 | 命令输出中文和 emoji | stdout 正确 | 相同字段值 | 业务一致；结果类不同 |
| 14 | 命令写文件，再通过 SDK 读取 | 读到 `command-file` | 相同 | 本项采集结果一致 |
| 15 | SDK 写 Python 文件、命令执行、SDK 读回 | 源码一致，输出 message=你好、value=42 | 相同业务结果 | 核心使用链路可复用 |
| 16 | chmod 0750 后用 files.write 覆盖 | 内容更新，mode 保留 488/0750 | 同样保留 | 业务一致；permissions 字符串仍差首位 |
| 17 | write 到多层不存在的父目录 | 自动创建父目录并写入 | 同样成功 | 本次实测双方均支持，不应列为缺失能力 |
| 18 | 向 files.write 传 bytearray | InvalidArgumentException | UnsupportedCapabilityError | 双方均拒绝；转换为 bytes 后的路径已由 #5 验证 |
| 19 | 向 files.write 传 BytesIO | 成功往返 | UnsupportedCapabilityError | 客户需先读取为 bytes，或扩展适配器 |
| 20 | files.write/read 传 request_timeout | 成功往返 | UnsupportedCapabilityError | 常见 E2B 文件调用参数不能直接复用 |
| 21 | commands.run 传 request_timeout | 命令成功 | 命令成功 | 仅证明参数被接受，不证明它实际控制请求期限 |
| 22 | files.list(depth=2) | 返回嵌套文件 | UnsupportedCapabilityError | AgentCore 当前仅支持 depth=1 |
| 23 | remove 非空目录 | 目录及内容被删除 | FileOperationError，code=NOT_EMPTY | 递归删除语义不同 |
| 24 | read 指向本次文件的符号链接 | 返回目标内容 | FileOperationError，code=PATH_VIOLATION | AgentCore 的路径安全边界更窄 |
| 25 | 使用相对路径进行文件和命令操作 | 在其默认目录下互通 | 在配置工作区下互通 | 业务一致；默认根目录不同 |
| 26 | timeout=0.2，命令 sleep 后写文件 | TimeoutException；等待后未见完成文件 | CommandWaitTimeout；等待后读到完成文件 | 原用例未确认启动；0.5 秒专项另见第 6 节 |
| 27 | timeout=0 执行短命令 | 成功输出 | 同样成功 | 本次短命令验证符合无总等待期限的用法；不代表无限期运行能力已验证 |
| 28 | 将 `FF FE` 字节按文本读取 | 返回两个替换字符 `��` | FileOperationError，code=ENCODING_ERROR | 解码错误策略不同 |

## 5. 元数据、异常与回调差异

**权限位没有被覆盖写入破坏，权限字符串的形状有差异。** 测试程序观察到两边普通文件 mode 都为 420（0644），目录都为 493（0755），chmod 后覆盖写入都保留 488（0750）。E2B 的 permissions 包含文件类型首位，例如 `-rwxr-x---`；AgentCore 返回 `rwxr-x---`。客户若按十位字符串解析文件类型，当前调用结果会不同。

测试程序还观察到目录 size 为 E2B 60、AgentCore 4096。两个后端的文件系统环境不同，目录 size 差异不能据此认定为文件内容错误。测试程序没有从比较数据中删除这一差异。

**同名异常不等于相同异常类。** 两边的非零退出都保留了 `stdout=partial-out`、`stderr=expected-error`、`exit_code=7`，但 AgentCore 的异常不继承官方 `e2b.CommandExitException`。客户保留 `except e2b.CommandExitException` 时，不能捕获本项目的异常。缺失文件和超时的异常名称本身也不同。

测试程序在两轮普通回调案例中都观察到 E2B 发送两段 stdout、AgentCore 发送一段，但拼接结果一致。这个观察不能证明某个后端保证固定分块，也不能单凭分块次数判定回调失效。超时专项进一步记录了客户端返回前能否收到初始输出，见下一节。

## 6. 超时专项：命令开始后，两边的副作用不同

主用例只写完成文件，未见文件可能表示“命令没有启动”。测试程序因此补做有独立正向对照、开始标记和完成标记的共同探针，避免把缺少副作用直接解释为取消成功。

两边都执行以下命令：

```bash
printf started > started; printf started; sleep 2; printf completed > completed; printf completed
```

测试程序先在 control 目录用 `timeout=15` 执行；两边都输出 `startedcompleted`，且开始／完成文件均正确。测试程序再换新目录，用 `timeout=0.5` 执行同一命令，并注册 stdout 回调。

| 观察点 | E2B | AgentCore |
|---|---|---|
| 客户端结束等待 | 约 0.501 秒，TimeoutException | 约 0.501 秒，CommandWaitTimeout |
| 超时异常的远端状态声明 | 未提供本项目的 remote_state_unknown 字段 | remote_state_unknown=True |
| 返回前 stdout 回调 | 约 0.072 秒收到 `started` | 未收到回调 |
| 命令开始的文件证据 | started 存在且内容正确 | started 存在且内容正确 |
| 返回后约 3.2 秒观察 | completed 不存在 | completed 存在，内容为 `completed` |
| 第二次观察的实际时间 | 返回后约 6.500 秒 | 返回后约 6.814 秒 |
| 第二次观察的完成文件 | 仍不存在 | 仍存在且内容正确 |

测试程序已经排除了 E2B 命令从未开始这一解释；也实证了 AgentCore 在客户端等待超时后仍可完成远端写入。**客户不能把 AgentCore 的等待超时当作远端操作撤销，再无条件重试带副作用的命令。** 客户应设计幂等性、状态核对或显式停止策略。

这些证据只覆盖本次两个有限观察窗口，不证明 E2B 提供永久取消保证，也不证明所有 E2B 超时都会终止进程。AgentCore 返回前没有回调同样不表示命令没有开始。测试程序没有用这一组数据推导长期性能或所有命令的实时流交付保证。

## 7. 生命周期专项：相同 with 代码，退出含义不同

测试程序在两边执行完全相同的业务块：

```python
with sandbox:
    result = sandbox.commands.run("printf context-ok", timeout=15)
```

两个后端都返回了 `context-ok`。退出后，后端专用观察器分别执行状态检查：

- E2B 观察器通过原句柄调用 `is_running()`，得到 False；官方 SDK 的 `__exit__` 已调用 kill。
- AgentCore 观察器看到原句柄 state=CLOSED；客户应用使用退出前保存的同一 SessionRef 重连，并成功执行 `printf resumed`。本项目 `__exit__` 只关闭本地句柄。

因此，客户不能把原 E2B 的 `with` 清理写法原样迁移后，假设 AgentCore 远端计算已停止。客户需要显式请求 `runtime.stop()`；测试清理程序使用重新连接的开放句柄完成了停止请求。服务接受停止请求仍不等于永久删除 Session ID。

## 8. 本地 Python 契约探针补充

测试程序另用禁止网络连接的本地探针检查类型和签名。这些结果来自真实安装包和本项目源码，**不属于新增云用例**：

| 探针 | E2B | AgentCore | 对客户代码的影响 |
|---|---|---|---|
| CommandResult 等五类的 Python 身份 | 官方 E2B 类 | 自有类，非 E2B 子类 | isinstance、类型导入及异常捕获不能保持原样 |
| FileType.FILE == "file" | False | True | 相同判断式可能走不同分支；公共调用可显式读取 `.value` |
| CommandExitException 是否为本端 CommandResult 实例 | 是 | 否 | 基于结果基类的处理代码会不同 |
| CommandResult 位置构造字段顺序 | stderr、stdout、exit_code、error | stdout、stderr、exit_code、error | 客户自行位置构造结果会交换 stdout/stderr |
| CommandResult 的字段可变性 | 可修改 | frozen，修改抛 FrozenInstanceError | 客户不能在返回对象上执行相同赋值操作 |
| commands.run 的第二个位置参数 | Python 签名接受 | keyword-only，Python 签名拒绝 | 客户应改用明确的关键字参数 |
| files.read 的第二个位置参数、files.list 的第二个位置参数 | Python 签名接受 | keyword-only，Python 签名拒绝 | 旧客户端调用语法需要适配 |

签名探针只检查参数绑定，不证明每种参数值都被远端支持。探针构造的异常实例也不替代第 4 节真实非零退出的观测。

## 9. 兼容性整改建议

以下为审查者依据实测结果给出的后续建议，本次没有据此修改 SDK：

| 优先级 | 责任组件与建议 | 原因 |
|---|---|---|
| 高 | 客户应用显式处理 stop、超时后的状态和幂等性；SDK 文档突出这些区别 | 生命周期和副作用差异可能导致重复执行或遗留远端计算 |
| 高 | SDK 维护者定义一致的异常适配契约，并评估文件 request_timeout 参数支持 | 现有 E2B 调用和 except 代码可能直接失败 |
| 中 | SDK 维护者统一 permissions 格式，明确 FileType 和返回对象的兼容目标 | 客户读取相同字段时目前可能得到不同结构或分支结果 |
| 中 | SDK 维护者明确缺失删除、无效 UTF-8、文件对象写入的处理策略 | 这些属于可直接影响业务结果的调用差异 |
| 按需 | SDK 维护者评估递归列表／删除，保留并说明符号链接安全边界 | 维护者不应为了表面兼容而无条件放宽路径隔离规则 |

CPU 架构、Python 版本、默认用户和目录属于本次模板与部署环境差异。客户运行带本地扩展、系统依赖或特定路径假设的程序时，还需要相应的工作负载测试。

## 10. 验证质量、资源清理与凭证处理

开发者为比较程序新增了 33 项本地测试：主比较程序 25 项、with 专项 4 项、超时专项 4 项。包括 TypeError 不得冒充超时、双方共同失败不得算业务成功、明确能力拒绝与 transport 故障分开、业务失败仍清理等回归用例。开发者执行原有 145 项与新增测试，最终结果为 **178 通过，0 失败，0 跳过**。独立审查者复核了比较规则和真实结果，Supervisor 复核了原始字段、计数及清理记录。

测试程序本次为两轮共同用例、with 专项和超时专项各创建一组后端会话，共 4 个 E2B 沙箱和 4 个 AgentCore 会话。四次 E2B 清理都确认 `is_running=False`；四次 AgentCore 清理都确认服务接受停止请求。测试程序没有删除供后续测试使用的 AgentCore Runtime 部署。

测试程序仅通过内存中的环境变量使用 E2B key，测试后持有 key 的进程均已退出。发布材料不包含 key、AWS 账户／资源 ARN、会话 ID、私有部署 state 或内部主机名。离线类型证据的发布版只替换了本地源码绝对路径，云端业务观测保持原样。

发布人员另对更新内容执行 Gitleaks 和 Bandit：未发现密钥，未发现中高危问题，原有 23 项低危提示没有增加。具体记录见 [发布复扫摘要](comparison/security-check.json)。

本次验证覆盖同步 Python、小文件、前台短命令和已列出的边界。测试程序没有验证所有 E2B API、异步客户端、PTY、后台任务、网络端口、大文件、长期负载、多区域或恶意命令隔离。两个后端的环境也未做镜像级统一，因此报告不提供性能排名或“完整兼容率”。

## 11. 原始证据与复跑方式

| 材料 | 内容 |
|---|---|
| [R01 结果](comparison/results-r01.json) | 首轮完整观察，保留原 bytearray 分类，未回写为通过 |
| [R02 结果](comparison/results-r02.json) | 第二轮完整观察、独立断言、严格比较、动态字段和清理结果 |
| [with 专项](comparison/context-manager-results.json) | 退出后运行状态、同引用重连及清理 |
| [超时专项](comparison/timeout-results.json) | 正向对照、开始／完成标记、回调和两次实际观察窗口 |
| [本地类型探针](comparison/type-contract-results.json) | 类、字段顺序、可变性、签名及源码证据 |
| [178 项本地测试摘要](comparison/local-tests-summary.json) | 真实 pytest 结果与测试构成 |
| [证据与源码 SHA-256](comparison/evidence-manifest.json) | SDK 核心、比较程序和结果文件的摘要 |

维护者在项目根目录安装 `.[test,e2b-test]`，通过安全的进程环境配置 `E2B_API_KEY`，并使用自己保存的、可信的 `docs/deployment.local.json`。维护者可以按以下命令复跑；脚本会新建并清理真实沙箱／会话：

```bash
export TMPDIR="$(pwd)/.tmp"
mkdir -p "$TMPDIR"
python3 -m pip install -e '.[test,e2b-test]'
python3 scripts/compare_backends.py --state docs/deployment.local.json \
  --output "$TMPDIR/comparison-r01.json"
python3 scripts/compare_backends.py --state docs/deployment.local.json \
  --output "$TMPDIR/comparison-r02.json"
python3 scripts/compare_context_manager.py --state docs/deployment.local.json \
  --output "$TMPDIR/context-manager.json"
python3 scripts/compare_timeout.py --state docs/deployment.local.json \
  --output "$TMPDIR/timeout.json"
python3 scripts/probe_python_contract.py --output "$TMPDIR/type-contract.json"
python3 -m pytest tests -q
```

公开仓库不提供测试者的凭证和部署 state。新环境的维护者需要先按 README 部署自己的 Runtime。比较程序的 `status=completed` 表示本轮观察与清理完整，不表示后端无差异；维护者必须同时查看 core 验收、边界结果、逐项观察和清理语义。
