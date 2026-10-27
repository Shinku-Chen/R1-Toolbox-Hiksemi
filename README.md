# R1 Toolbox

用于保存海康 R1 NAS 的配置、脚本和可下载工具包。当前提供在官方固件上通过 Docker 执行宿主任务的工具。

| 工具 | 用途 | 说明 |
| --- | --- | --- |
| [开机执行一次](tools/boot-once/) | 每次 NAS 开机执行一次任务，完成后容器自行停止 | [部署与配置](tools/boot-once/README.md) |

## 下载与部署

1. 打开本仓库的 [Actions](../../actions)，选择对应分支最近一次成功的 `检查与打包`。
2. 在运行页面的 Artifacts 中下载工具包，解开后取得 ZIP 与 `SHA256SUMS`。
3. 解开工具 ZIP，将里面的 `r1-boot-once/compose.yaml` 导入 NAS Docker 市场的新项目。

也可以直接打开 [Compose 配置](tools/boot-once/compose.yaml)，复制完整内容到 NAS 项目编辑器。

默认 `NEW_HTML` 为空，只检查环境并验证执行后自停。真正替换小屏页面时，按工具说明设置新 HTML 的宿主路径并递增 `TASK_REVISION`。

运行机制为 `restart: always` 配合作业结束后的明确 `docker stop`；同一次 NAS 启动和同一任务版本成功后会跳过重复操作。可选的小屏程序通过宿主临时服务继续运行，容器完成后停止。

## 本地检查与打包

需要 Python 3.9 或以上。Compose 检查需要 Docker Compose，但模拟测试和 ZIP 打包不需要连接 Docker 守护进程。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python tools/boot-once/build-compose.py
python tools/boot-once/test_runner.py
python scripts/package.py --version dev
```

打包前需先提交工具源码，确保包内文件对应记录的 Git commit。输出位于 `dist/`，包含 ZIP 和 `SHA256SUMS`；ZIP 内的 `manifest.json` 记录版本、提交与各文件校验值。ZIP 使用固定文件顺序、时间和权限，相同版本与提交可以重复生成一致的文件。生成物由 Actions 保存，仓库维护源码与可直接导入的 Compose 配置。

每次推送相关源码后，自动流程会检查生成文件是否同步、校验 Compose、运行模拟测试，再打包上传。默认分支纳入 workflow 后，也可以从 Actions 手动触发。

自动化检查使用伪宿主工具。真实 NAS 的首次开机、自停、实际画面和厂商容器管理行为仍需部署验证，详见[工具说明](tools/boot-once/README.md)。
