#!/bin/sh
# 1) 幂等初始化预置 raw 镜像：
#    - 已有盘容量与 BLOCK_COUNT 相符 -> 原样复用（原盘内容与已打的洞保留）
#    - 容量不符 -> 打印盘上/配置容量并以非零码退出，set -e 使启动停止，原盘不动
#    - 空卷或上次建盘被强杀 -> flock 串行 + 临时文件原子 rename 建盘，先清残留
# 2) 布局完整后才以单进程多线程启动：回收在后台线程执行，块操作走块级锁
set -e

python -m app.cli init-image

exec gunicorn --bind "0.0.0.0:${PORT:-8000}" \
    --workers 1 \
    --threads 8 \
    --timeout 120 \
    "app.web:create_app()"
