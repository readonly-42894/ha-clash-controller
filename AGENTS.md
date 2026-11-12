# AGENTS.md — ha-clash-controller 协作约定

本文件是本仓库的常驻 agent 指令，跨会话生效（新会话打开本仓库时自动加载）。它不绑定任何具体 CLI、harness 或模型。

## 项目说明

Home Assistant 自定义集成，通过 Clash 兼容 API 控制外部 Clash / Mihomo 实例。本仓库不是 Clash 实现或客户端；协议细节与 API 客户端由独立仓库 `clash-controller-api` 提供，并以 `manifest.json` 中的 `requirements` 固定版本。

目录结构：

```text
custom_components/clash_controller/   集成代码
  __init__.py                         入口、RuntimeData、config entry 迁移
  coordinator.py                      ClashEntityData 模型与数据轮询
  base.py                             实体基类
  sensor.py / select.py / button.py    实体平台，按 entity_type 分派
  config_flow.py                      配置流程、Options、MINOR_VERSION
  services.py / services.yaml         服务动作定义与注册
  diagnostics.py                      诊断信息与脱敏
  const.py                            领域常量与服务名
  manifest.json                       集成清单、API 依赖版本、发布版本号
  icons.json                          实体图标
  translations/                       en / de / zh-Hans 三语翻译
tests/
  test_integration.py                 快速 HA 契约测试（mock API 边界）
  test_coordinator_builders.py        实体构建单元测试
  core_compatibility/                 真实内核三层测试系统
```

运行环境要求：Home Assistant Core `2026.8.0` 或更新，Python 3.14。

## 工作纪律

- 每完成一个独立、可验证的步骤（功能改动、修复、清理、文档与配置更新、测试通过等）就**立即 `git commit`**，不把多个步骤攒到会话末尾一次性提交。
- 提交信息遵循 conventional commit 风格（如 `feat:`、`fix:`、`refactor:`、`docs:`、`chore:`、`build:`），并提供简洁正文。
- 提交时用 `git commit --author "<模型名> <模型id@llm.invalid>"` 标明**当前正在执行这次提交的 agent/模型自身**，不要照搬别的 agent 的名字。例如：

  ```bash
  git commit --author "DeepSeek Flash <deepseek-flash@llm.invalid>" -m "$(cat <<'EOF'
  fix: 描述改动

  Co-Authored-By: opencode v2.2.7
  EOF
  )"
  ```

- `Co-Authored-By` 写实际运行的 Agent 客户端及版本（`opencode --version`），不写邮箱。
- 任务闭环（实现完成 + 验证通过）后直接提交，无需先询问用户。

## 测试与验证

仓库使用一套测试系统、三层执行方式，细节见 `tests/core_compatibility/README.md`：

```bash
python -m pip install -r tests/requirements.txt

python tests/run.py integration    # 快速 HA 契约，不启动真实内核
python tests/run.py system         # 启动全部 pinned 内核做真实 HA 冒烟
python tests/run.py system --core mihomo --full
python tests/run.py release        # integration + 完整内核矩阵 + 慢速故障场景
```

- 改动提交前至少运行 `python tests/run.py integration`。
- 涉及真实内核行为、能力探测、端点兼容性时，运行 `system`；发布前运行 `release`。
- 系统层内核二进制与数据库缓存在 `.cache/core-compatibility`（已被 `.gitignore` 忽略），首次运行会下载并按 SHA-256 校验；不要提交缓存。
- 集成层只 mock API 边界（`coordinator.ClashAPI` / `config_flow.ClashAPI`），Home Assistant 的配置项、注册表、实体和服务都使用真实实现。不要 mock HA 内部对象来绕过契约。
- 真实内核测试用 `@pytest.mark.system` 标记；更慢的故障/恢复场景再加 `@pytest.mark.release`。
- CI 在 PR 上运行 `.github/workflows/tests.yml`（integration + 全部 pinned 内核）以及 `.github/workflows/hacs.yml`（HACS Action + hassfest）。改动翻译、manifest、集成结构后要保证 hassfest 能通过。

## 集成代码约定

- 实体不是在各平台里临时拼装的：`coordinator.py` 把 API 数据构建成 `ClashEntityData`，平台模块按 `entity_type` 映射到实体类，再通过 `async_add_listener` 增量添加。新增实体类型时，按这个链路补齐 builder、平台映射、翻译和测试。
- 能力探测决定实体是否创建：内核不支持的端点不生成对应实体。新增实体必须由 `capabilities` 或数据可用性门控，避免向不支持的内核暴露实体。
- **实体 unique ID 是持久契约**：`{device_id}_{entity_type}_{unique_key}`，其中 `device_id` 由控制器地址规范化得到（见 `_device_id_from_host`）。不要修改这套命名规则、`entity_type` 或既有 `unique_key`，否则会破坏用户已有的实体注册表。`translation_key` 只影响显示名，可以调整。
- `RuntimeData` 通过 `config_entry.runtime_data` 暴露协调器；平台统一从 `config_entry.runtime_data.coordinator` 取数据。
- 持久化结构变更：修改 config entry `data` 结构时，提升 `config_flow.py` 的 `MINOR_VERSION` 并在 `__init__.py` 的 `async_migrate_entry` 中迁移。仅删除不再使用的可选 `options` 键时，旧键可安全忽略，不必强制迁移。
- 服务动作需要同时更新：`const.py` 服务名、`services.py` schema 与处理逻辑、`services.yaml`、三语翻译的 `services` 段。设备选择器只允许控制器设备。
- 诊断信息新增敏感字段（地址、令牌、代理凭据等）时，加入 `diagnostics.py` 的 `TO_REDACT`。

## 文案与资源约定

- 用户可见文案只放在 `translations/en.json`、`translations/de.json`、`translations/zh-Hans.json`，三份文件**键结构必须保持一致**。新增实体或服务时同步补齐三语。
- 实体图标放在 `icons.json`，键与 `translation_key` 对应。
- `README.md` 使用英文，描述对外行为、配置方法与实体/服务清单；行为或能力变化时同步更新。
- 修改 JSON 后至少做一次语法校验：

  ```bash
  for f in custom_components/clash_controller/icons.json \
           custom_components/clash_controller/translations/*.json; do
    python -m json.tool "$f" >/dev/null || exit 1
  done
  ```

- 遵循现有代码风格：4 空格缩进、双引号、完整类型注解；需要日志时使用模块级 `_LOGGER`。本仓库未配置独立 linter/格式化工具，格式以周边代码为准。

## 依赖与版本

- 升级 `clash-controller-api` 时更新 `manifest.json` 的 `requirements` 固定版本，并单独提交 `build:` 改动；协议兼容性问题归属 `clash-controller-api` 仓库，不在本仓库改协议实现。
- 功能 PR 不修改 `manifest.json` 的 `version`。发布时单独提交 `chore: bump integration version to X.Y.Z`，与 Git 标签 `vX.Y.Z` 保持一致。

## 不要做的事

- 不要提交 `.cache/`、`__pycache__/`、内核二进制或任何测试生成的临时文件。
- 不要在测试或文档中写入真实令牌、真实控制器地址或个人凭据；示例一律用占位值。
- 不要把多个独立步骤压成一个提交，也不要在未运行对应测试层的情况下声称验证通过。
