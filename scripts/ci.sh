#!/usr/bin/env bash
# CI 统一入口:任意 CI 平台或 git pre-push 钩子直接调用,失败即非零退出(docs/06 §11)。
# 可选环境变量:PYTHON(建 venv 用的解释器,默认 python3.14 → python3)、CI_SKIP_INSTALL=1(跳过装依赖)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# 测试会调用 .venv/bin/alembic,venv 必须建在仓库根的 .venv
if [ ! -x .venv/bin/python ]; then
  PY="${PYTHON:-$(command -v python3.14 || command -v python3)}"
  echo "[ci] 创建 .venv($("$PY" --version))"
  "$PY" -m venv .venv
fi

if [ "${CI_SKIP_INSTALL:-0}" != "1" ]; then
  echo "[ci] 安装依赖"
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.lock -r requirements-dev.txt
fi

command -v ffmpeg >/dev/null || echo "[ci] 警告:未找到 ffmpeg,依赖它的测试(含离线端到端)将被跳过"

echo "[ci] pytest"
.venv/bin/python -m pytest -q

if command -v node >/dev/null; then
  echo "[ci] 前端测试"
  node --experimental-vm-modules tests/test_auth_frontend.mjs
else
  echo "[ci] 失败:未找到 node,无法运行前端测试" >&2
  exit 1
fi

echo "[ci] 全部通过"
