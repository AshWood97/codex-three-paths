# Codex Three Paths / Codex 三路径技能集

Native agents · External providers · Harness delegation

原生 agent · 外部 provider · 独立 harness

[中文](#中文) · [English](#english)

## 中文

这个仓库提供三个**独立安装、独立选择**的 Codex skill。安装全部三个后，技能菜单仍显示三个选项；使用时直接选所需的那个，不会再出现“总入口”或第二次模式选择。

| Skill | 用途 |
| --- | --- |
| [Codex Native Orchestration](.agents/skills/codex-native-orchestrator/SKILL.md) | 使用 Codex 原生 agent 协调任务；需要批量、依赖或跨轮恢复时使用持久化 runner。 |
| [Codex External Provider](.agents/skills/codex-external-provider/SKILL.md) | 在**会话启动时**已配置外部 Responses API provider 的前提下，检查当前会话及原生 agent 的路由。 |
| [Codex Harness Bridge](.agents/skills/codex-harness-bridge/SKILL.md) | 把一个有明确边界的任务交给已配置的独立编码 CLI，并由当前 Codex 会话检查结果。 |

三者可以组成工作流程，但不会自动混用模型或偷偷切换 provider：按当前任务选一个 skill；如需在同一工作流程里调用独立 CLI，再明确使用 Harness Bridge。Native 与 External 的会话路由条件不同，不能把 External 当作在已启动的 Native 会话中切换 provider 的按钮。**完全混合路由是未来方向，目前未实现。**

### 安装

在 Codex 中使用内置 `$skill-installer`，请它从本仓库一次安装下面三个路径：

```text
仓库：AshWood97/codex-three-paths
路径：.agents/skills/codex-native-orchestrator
      .agents/skills/codex-external-provider
      .agents/skills/codex-harness-bridge
```

安装器将三个目录分别放入 `$CODEX_HOME/skills/`（默认 `~/.codex/skills/`）。在下一轮对话中，从技能菜单选一个即可；也可以明确写 `$codex-native-orchestrator`、`$codex-external-provider` 或 `$codex-harness-bridge`。如已有同名 skill，先检查现有安装，避免覆盖个人改动。

建议使用 Python 3.11 或更新版本；External 的预检及测试要求至少 3.11。Native 的 skill 安装和命名 agent 配置是两步：安装器只安装 skill；按下方说明部署固定子代理角色后，再启动新会话。当前 Codex 主会话只负责协调，不属于技能配置的角色映射；调用技能不会改变用户选择的主会话模型。External 的 provider、模型目录和凭据只放在个人配置及环境变量中，参照[示例](.agents/skills/codex-external-provider/examples/user-config.example.toml)和[设置说明](.agents/skills/codex-external-provider/references/provider-setup.md)。Harness Bridge 的私有配置从[示例](.agents/skills/codex-harness-bridge/config.example.json)创建，参照[设置说明](.agents/skills/codex-harness-bridge/references/setup.md)；仅安装要用的 CLI。

#### Native 固定编排与部署

| 角色 | 模型 | 推理强度 |
| --- | --- | --- |
| Explorer、Worker | `gpt-6-luna` | `max` |
| Tester、Reviewer | `gpt-6-sol` | `xhigh` |
| Researcher、Guardian | `gpt-6-astra` | `medium` |

Guardian 仅在明确请求额外关卡时启动。需要写入产物的测试走原生 Tester 派遣；持久运行器只接受只读 Tester 节点。模型或配置不符时停止派遣，不会自动替换角色。

把本仓库 `.agents/skills/codex-native-orchestrator/roles/*.toml` 的六个配置放入个人 `CODEX_HOME/agents`，并将清单中的 `config_values` 合并到个人 `CODEX_HOME/config.toml`。这些值仅配置子代理，不包含全局 `model` 或 `model_reasoning_effort`。不要覆盖配置文件中的其他设置或追加重复的 TOML 键。从本仓库检出目录运行检查命令；已运行的会话和 agent 不会自动切换模型。

```sh
python3 .agents/skills/codex-native-orchestrator/scripts/codex_native_orchestrator.py --repo "$PWD" doctor
```

### 公开仓库与验证范围

本仓库只包含源码、文档、占位符示例和测试。不要提交 API key、真实 provider URL、个人 `config.toml`、私有模型目录、任务 brief、运行日志或报告。External 的预检需要可信的运行时 provider/model 身份；配置检查本身不证明远端请求的实际路由。Harness Bridge 的适配器测试使用模拟 CLI，不代表所有真实 CLI 版本或服务端都已验证。DeepSeek Harness 仍是实验性支持。

运行现有测试：

```sh
for skill in codex-native-orchestrator codex-external-provider codex-harness-bridge; do
  PYTHONDONTWRITEBYTECODE=1 python3.11 -B -m unittest discover \
    -s ".agents/skills/$skill/tests" -p 'test_*.py' -q || exit 1
done
```

## English

This repository contains three Codex skills that are **installed and selected independently**. Installing all three adds three entries to the skill picker. Select the skill you need once for the task; there is no umbrella skill or second mode picker.

| Skill | Purpose |
| --- | --- |
| [Codex Native Orchestration](.agents/skills/codex-native-orchestrator/SKILL.md) | Coordinate native Codex agents, using the persistent runner when task dependencies, batches, or recovery require it. |
| [Codex External Provider](.agents/skills/codex-external-provider/SKILL.md) | Check routing for a Codex session and native agents **started with** a configured external Responses API provider. |
| [Codex Harness Bridge](.agents/skills/codex-harness-bridge/SKILL.md) | Send one bounded job to a configured coding CLI and review its evidence from the current Codex session. |

The skills can participate in one workflow, but they do not automatically mix models or switch providers. Select a skill for the current task and explicitly invoke Harness Bridge if a bounded subprocess is useful. Native and External have different session routing requirements; External cannot change the provider of a session that has already started. **Fully mixed routing is a possible future enhancement, not a current feature.**

### Install

In Codex, ask the built-in `$skill-installer` to install these three paths from this repository:

```text
Repository: AshWood97/codex-three-paths
Paths: .agents/skills/codex-native-orchestrator
       .agents/skills/codex-external-provider
       .agents/skills/codex-harness-bridge
```

The installer places each directory in `$CODEX_HOME/skills/` (default `~/.codex/skills/`). On the next turn, select one from the skill picker, or invoke `$codex-native-orchestrator`, `$codex-external-provider`, or `$codex-harness-bridge` directly. Inspect any existing skill with the same name before updating it so local changes are preserved.

Python 3.11 or newer is recommended; External preflight and tests require at least 3.11. Installing the Native skill and configuring named agents are separate steps: the installer adds the skill, while the deployment below installs the fixed subagent roles. The current Codex session coordinates work but is not one of the skill's named roles; invoking the skill does not change its selected model. Start a new session after deployment. Keep External provider settings, model catalogs, and credentials in user configuration and environment variables, using the [example](.agents/skills/codex-external-provider/examples/user-config.example.toml) and [setup guide](.agents/skills/codex-external-provider/references/provider-setup.md). Create a private Harness Bridge configuration from its [example](.agents/skills/codex-harness-bridge/config.example.json) and [setup guide](.agents/skills/codex-harness-bridge/references/setup.md); install only the CLIs you plan to use.

#### Native fixed roles and deployment

| Role | Model | Reasoning effort |
| --- | --- | --- |
| Explorer, Worker | `gpt-6-luna` | `max` |
| Tester, Reviewer | `gpt-6-sol` | `xhigh` |
| Researcher, Guardian | `gpt-6-astra` | `medium` |

Guardian runs only when an extra gate is explicitly requested. Tests that write output use native Tester dispatch; persistent runner Tester nodes must be read-only. Configuration drift stops dispatch instead of substituting a role.

Place the six `.agents/skills/codex-native-orchestrator/roles/*.toml` profiles in your personal `CODEX_HOME/agents`, and merge the manifest's `config_values` into your personal `CODEX_HOME/config.toml`. These values configure subagents only; they do not include the global `model` or `model_reasoning_effort`. Preserve unrelated settings and avoid duplicate TOML keys. Run the check from this repository checkout. Running sessions and agents do not switch models automatically.

```sh
python3 .agents/skills/codex-native-orchestrator/scripts/codex_native_orchestrator.py --repo "$PWD" doctor
```

### Public repository and verification limits

This repository contains only source code, documentation, placeholder examples, and tests. Do not commit API keys, real provider URLs, personal `config.toml`, private model catalogs, task briefs, run logs, or reports. External preflight requires trusted runtime provider/model identity; a configuration check alone does not prove remote request routing. Harness Bridge adapter tests use stub CLIs and do not establish compatibility with every live CLI version or service. DeepSeek Harness support remains experimental.

Run the existing tests:

```sh
for skill in codex-native-orchestrator codex-external-provider codex-harness-bridge; do
  PYTHONDONTWRITEBYTECODE=1 python3.11 -B -m unittest discover \
    -s ".agents/skills/$skill/tests" -p 'test_*.py' -q || exit 1
done
```

## License

MIT. See [LICENSE](LICENSE).
