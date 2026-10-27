#!/bin/sh
# CI-only image build, isolated smoke checks, registry push, and offline delivery.
set -eu

cd "$(dirname "$0")/.."
commit=$(git rev-parse HEAD)
if [ "${GITHUB_SHA:-$commit}" != "$commit" ]; then
  echo '源码 HEAD 与 GITHUB_SHA 不一致，已停止打包。' >&2
  exit 1
fi
if [ -n "$(git status --porcelain --untracked-files=all)" ]; then
  echo '源码尚未提交，已停止打包。' >&2
  exit 1
fi

repository=ghcr.io/shinku-chen/r1-toolbox
image="$repository:sha-$commit"
version="sha-$commit"
channel=preview
if [ "${GITHUB_EVENT_NAME:-}" = pull_request ]; then
  channel=pull-request
elif [ "${GITHUB_REF:-}" = refs/heads/main ]; then
  channel=latest
else
  case "${GITHUB_REF:-}" in
    refs/tags/v*) channel=latest ;;
  esac
fi

mkdir -p dist
docker build --platform linux/amd64 --build-arg "TOOLBOX_VERSION=$version" \
  --label "org.opencontainers.image.revision=$commit" -t "$image" .
docker image inspect "$image" > dist/build-image-inspect.json
python - dist/build-image-inspect.json "$version" <<'PY'
import json
from pathlib import Path
import sys

images = json.loads(Path(sys.argv[1]).read_text())
if len(images) != 1:
    raise SystemExit("镜像检查没有返回唯一结果")
image = images[0]
config = image.get("Config", {})
expected = {
    "architecture": image.get("Architecture") == "amd64",
    "os": image.get("Os") == "linux",
    "entrypoint": config.get("Entrypoint") == ["/usr/local/bin/r1-toolbox"],
    "command": config.get("Cmd") == ["run"],
    "version": "TOOLBOX_VERSION=" + sys.argv[2] in config.get("Env", []),
}
failed = [name for name, valid in expected.items() if not valid]
if failed:
    raise SystemExit("镜像检查失败：" + ", ".join(failed))
PY
image_id=$(docker image inspect --format '{{.Id}}' "$image")

# No host mounts, host namespaces, privileged mode, or network in smoke checks.
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 32 "$image" --help
actual_version=$(docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 32 "$image" --version)
if [ "$actual_version" != "R1 Toolbox $version" ]; then
  echo '镜像 --version 输出与源码版本不一致。' >&2
  exit 1
fi

if [ "${PUBLISH_IMAGE:-0}" = 1 ]; then
  if [ "${GITHUB_EVENT_NAME:-}" = pull_request ]; then
    echo 'PR 检查禁止发布镜像。' >&2
    exit 1
  fi
  : "${GHCR_TOKEN:?missing GHCR_TOKEN}"
  : "${GITHUB_ACTOR:?missing GITHUB_ACTOR}"
  printf '%s' "$GHCR_TOKEN" | docker login ghcr.io --username "$GITHUB_ACTOR" --password-stdin
  trap 'docker logout ghcr.io >/dev/null 2>&1 || true' EXIT

  # Never replace a previously published sha tag with a different image.
  if docker manifest inspect "$image" >/dev/null 2>&1; then
    docker pull --platform linux/amd64 "$image"
    if [ "$(docker image inspect --format '{{.Id}}' "$image")" != "$image_id" ]; then
      echo '相同源码 SHA 已发布不同镜像，已停止以保护不可变标签。' >&2
      exit 1
    fi
  else
    docker push "$image"
  fi
  docker tag "$image" "$repository:$channel"
  docker push "$repository:$channel"
  case "${GITHUB_REF:-}" in
    refs/tags/v*)
      release_tag=${GITHUB_REF#refs/tags/}
      if ! printf '%s' "$release_tag" | LC_ALL=C grep -Eq '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$'; then
        echo '版本标签不能用作 Docker tag，已停止。' >&2
        exit 1
      fi
      docker tag "$image" "$repository:$release_tag"
      docker push "$repository:$release_tag"
      ;;
  esac

  # Read back the actual registry tag before saving the matching offline image.
  docker pull --platform linux/amd64 "$image"
  if [ "$(docker image inspect --format '{{.Id}}' "$image")" != "$image_id" ]; then
    echo '已发布镜像 ID 与本次构建不一致，已停止。' >&2
    exit 1
  fi
fi

# docker save (not export) preserves RepoTags, image configuration, and layers.
docker save --output dist/r1-toolbox-linux-amd64.tar "$image"
python scripts/package.py --image "$image" --image-id "$image_id" \
  --archive dist/r1-toolbox-linux-amd64.tar --channel "$channel" --version "$version"
docker compose -f dist/composer.yml config --quiet
(cd dist && sha256sum -c SHA256SUMS)
