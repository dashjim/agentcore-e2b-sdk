# 公开发布前安全扫描

扫描日期：2026-10-12。发布人员在上传 GitHub 前检查公开副本，扫描器检查密钥、Python 代码与已解析的依赖版本；独立代码审查者复核命令执行、文件边界、部署权限和日志行为。

后续双端对照更新也经过发布扫描：Gitleaks 未发现密钥，Bandit 扫描 3101 行源码／工具代码，仍为原有的 23 项低危提示，没有新增中高危或低危提示。本轮未改运行依赖，测试程序共通过 178 项本地用例。更新记录见 [复扫摘要](comparison/security-check.json)、[Bandit JSON](comparison/bandit.json) 和 [Gitleaks JSON](comparison/gitleaks.json)；下表保留首次发布时的扫描规模与结果。

## 扫描器结果与发布人员处理

| 执行主体 | 检查范围 | 结果 |
|---|---|---|
| Gitleaks 8.30.1 | 公开文件快照与首次提交的 Git 历史 | 未发现匹配规则的密钥；发布人员在推送前再次扫描提交 |
| pip-audit 2.10.1 | 干净安装环境解析出的 35 个依赖，含 `test` 和 `e2b-test` extras | 0 个已知漏洞，0 个跳过的包 |
| Bandit 1.9.4 | `src/` 与 `scripts/`，共 2081 行代码 | 0 高危、0 中危、23 低危提示；扫描器没有解析错误 |
| 独立代码审查者与发布人员 | IAM、文件路径、命令启动、HTTP 入口、凭证日志及待上传清单 | 未发现阻断本次公开发布的问题；发布人员移除了内部部署元数据 |
| pytest | 公开副本的本地功能和故障用例 | 145 通过，0 失败，0 跳过 |

发布人员保留了 Bandit 的完整提示，没有用 `nosec` 或关闭规则来得到零告警。发布人员逐类复核了 23 项低危提示：

| Bandit 规则 | 数量 | 审查者的判断 |
|---|---|---|
| B101 | 15 | 云端验收脚本使用 `assert` 验证结果。调用者应按 README 使用普通 Python 执行测试，不使用会移除断言的 `python -O` 或 `PYTHONOPTIMIZE`；这些断言没有承担生产鉴权职责。 |
| B404、B603、B607 | 6 | 容器测试脚本通过参数数组启动本机 Docker，没有启用 `shell=True`。测试人员需要使用可信 PATH 和镜像；镜像参数是测试人员显式提供的配置。 |
| B110 | 1 | SDK 在异步关闭输出流时忽略关闭异常，避免清理异常覆盖原始超时／业务异常或阻塞调用者。SDK 不因此把失败命令报告为成功。 |
| B606 | 1 | 命令助手在身份和 boot ID 检查后有意调用 `/bin/bash` 执行客户命令；这是 SDK 的公开功能。Runtime 的隔离与 IAM、客户应用的会话授权仍承担各自安全责任。 |

发布人员将扫描证据放在本目录的 `security/` 中：

- [Bandit 原始 JSON](security/bandit.json)
- [pip-audit 原始 JSON](security/pip-audit.json)
- [此次审计的依赖精确版本](security/requirements-audited.txt)
- [Gitleaks 文件扫描 JSON](security/gitleaks.json)
- [Gitleaks 提交历史扫描 JSON](security/gitleaks-history.json)
- [公开副本测试摘要](security/public-tests.json)

依赖清单记录本次解析结果，供审计复现；它不是跨平台安装锁文件。pip-audit 的范围不包括本地项目本身、pip 安装器、扫描工具依赖或容器操作系统软件包。审查者没有开展渗透测试、容器镜像 CVE 扫描或恶意命令跨租户隔离认证。扫描结果仅代表本次版本及扫描时的规则和漏洞数据库。

## 发布人员排除的内部材料

发布人员只上传源码、测试、部署工具及去标识文档。公开副本不包含原始 AWS 部署 state、账户／会话／资源标识、内部主机名、个人文件系统路径、凭证、虚拟环境、缓存或旧 wheel。发布人员配置 `.gitignore` 和 `.dockerignore` 排除相关生成物，并使用 GitHub noreply 邮箱提交。

SDK 单元测试保留了虚构 AWS ARN、测试专用认证字符串和协议 fixture；这些值不对应实际账户访问凭证。公开测试报告保留已验证的结论，原始云端日志及部署清单不随仓库发布。

## 客户和部署人员承担的安全边界

客户应用负责校验用户对 SessionRef 的访问权限，并保护自己的 AWS／E2B 凭证。Runtime HTTP 服务依赖 AgentCore IAM 入站认证；部署人员不能直接把该 HTTP 服务开放给不可信网络。命令助手的文件目录检查和 Unix 身份对齐不提供同容器内恶意 Bash 的隔离。

部署工具把本地 `deployment.local.json` 当作可信清单。部署人员只应使用自己生成并保存的 state 执行 `update`、`resume` 和 `cleanup`；工具没有用云端资源标签证明任意外部 state 的归属。测试人员也不应向会被 Runtime 记录的命令、环境参数或测试异常加入秘密。

## 审查者如何复现扫描

审查者安装对应版本的 Gitleaks、Bandit 和 pip-audit 后，在项目根目录运行：

```bash
export TMPDIR="$(pwd)/.tmp"
mkdir -p "$TMPDIR"
gitleaks dir . --redact --report-format json --report-path "$TMPDIR/gitleaks.json"
gitleaks git . --redact --report-format json --report-path "$TMPDIR/gitleaks-history.json"
bandit -r src scripts -f json -o "$TMPDIR/bandit.json"
pip-audit -r docs/security/requirements-audited.txt --no-deps --disable-pip \
  -f json -o "$TMPDIR/pip-audit.json"
```

Bandit 因上述低危提示返回非零退出码；审查者需要查看具体结果。Gitleaks 的目录扫描会包含未忽略的本地生成文件，审查者应在干净 checkout 执行或另外扫描准确的发布文件快照。
