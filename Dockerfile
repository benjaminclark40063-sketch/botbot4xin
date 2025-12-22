# ⚠️ 使用完整版 Python 镜像，确保有 libpq 等所有底层库
FROM python:3.13

# 设置环境变量：
# 1. 也就是让 Python 立刻打印日志，不要缓存
# 2. 确保 .venv 在路径里
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 创建虚拟环境并安装依赖
RUN python -m venv .venv
COPY requirements.txt ./
# 升级 pip 并安装依赖
RUN .venv/bin/pip install --upgrade pip && \
    .venv/bin/pip install -r requirements.txt

# 复制所有代码
COPY . .

# 暴露端口
EXPOSE 8080
ENV PORT=8080

# 🚀 启动命令：强制运行 python main.py
CMD ["python", "main.py"]