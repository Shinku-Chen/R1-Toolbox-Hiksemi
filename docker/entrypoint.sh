#!/bin/sh
set -eu

case "${1:-run}" in
  --help|-h|help)
    cat <<'HELP'
R1 Toolbox

Usage: r1-toolbox [run|--help|--version]

  run         Run the NAS boot-once task, then stop this container (default).
  --help      Show this help without accessing the NAS host.
  --version   Show the image version without accessing the NAS host.

Deploy with composer.yml for the required host mounts and namespaces.
composer.yml uses UI_MODE=image: the bundled UI runs from the host's /run tmpfs.
UI_MODE=diagnostic only reports diagnostics; UI_MODE=directory uses a NAS UI_DIR.
Without UI_MODE or UI_DIR, the task only reports diagnostics.
HELP
    ;;
  --version|-V|version)
    printf 'R1 Toolbox %s\n' "${TOOLBOX_VERSION:-dev}"
    ;;
  run)
    if [ "$#" -gt 1 ]; then
      echo '[ERROR] run 不接受额外参数；请使用 --help 查看用法。' >&2
      exit 2
    fi
    TASK_CONTAINER=${TASK_CONTAINER:-r1-toolbox}
    TASK_REVISION=${TASK_REVISION:-1}
    export TASK_CONTAINER TASK_REVISION
    exec /bin/sh /opt/r1-toolbox/runner.sh
    ;;
  *)
    printf '[ERROR] 未知命令：%s；请使用 --help 查看用法。\n' "$1" >&2
    exit 2
    ;;
esac
