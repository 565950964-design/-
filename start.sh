#!/bin/bash
# 启动记账小助手

echo "🚀 正在启动记账小助手..."
cd "$(dirname "$0")"

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "❌ 未找到 Python3，请先安装 Python 3.8+"
    exit 1
fi

# 安装依赖
echo "📦 安装依赖..."
pip3 install -r backend/requirements.txt -q

# 初始化数据目录
mkdir -p data

# 启动服务
echo "✅ 启动服务，访问 http://localhost:5000"
echo "   按 Ctrl+C 停止服务"
echo ""
python3 backend/app.py
