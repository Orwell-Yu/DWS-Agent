#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
CONFIG_PATH="${PROJECT_DIR}/config.yaml"
EXAMPLE_PATH="${PROJECT_DIR}/config.example.yaml"

if [[ -e "${CONFIG_PATH}" ]]; then
  print "配置已存在，未覆盖：${CONFIG_PATH}"
else
  /bin/cp "${EXAMPLE_PATH}" "${CONFIG_PATH}"
  /bin/chmod 600 "${CONFIG_PATH}"
  print "已创建本地配置：${CONFIG_PATH}"
fi

/bin/mkdir -p "${PROJECT_DIR}/logs" "${PROJECT_DIR}/runtime"
if command -v uv >/dev/null 2>&1; then
  (cd "${PROJECT_DIR}" && uv sync)
else
  print "未检测到 uv；请安装 uv 后运行：cd ${PROJECT_DIR} && uv sync"
fi

print "下一步：编辑 config.yaml，填写 DWS profile、身份、Codex 模型和本地仓库路径。"
