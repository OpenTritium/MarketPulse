#!/bin/sh
# 修复 bind mount 数据卷所有权（docker 以 root 创建挂载目录），然后降权执行
chown -R appuser:appuser /app/data /models 2>/dev/null || true
exec gosu appuser "$@"
