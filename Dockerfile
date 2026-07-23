FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY bili_workflow.py .
COPY bili_server.py .
COPY dashboard.html .
COPY workflow.html .

# 数据目录（通过 volume 挂载持久化）
RUN mkdir -p /app/data /app/commented

# 环境变量
ENV DATA_DIR=/app/data
ENV COMMENTED_DIR=/app/commented
ENV FLASK_ENV=production

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

EXPOSE 5000

# 使用 Waitress 生产服务器启动
CMD ["python", "-c", "from bili_server import app; from waitress import serve; \
     import os; \
     os.makedirs(os.environ.get('DATA_DIR', '/app/data'), exist_ok=True); \
     os.makedirs(os.environ.get('COMMENTED_DIR', '/app/commented'), exist_ok=True); \
     print('='*60); \
     print('  B站自动评论工作流 - 云端版'); \
     print('  访问: http://<服务器IP>:5000'); \
     print('='*60); \
     serve(app, host='0.0.0.0', port=5000, threads=8)"]