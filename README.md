# chinahrt_web — 继续教育自动学习工具

> v1.2 — gp5 播放器支持、续学、智能模式识别、扫描优化、稳定性修复

chinahrt.com 继续教育平台自动学习脚本，支持多账户、多线程学习、SSE 实时进度推送。

## 功能

- 多账户管理，同时学习多门课程
- 自动登录（支持验证码）
- 课程扫描与进度展示
- 自动学习（视频进度上报）
- 续学：从已学位置继续，不从头开始
- SSE 实时进度推送，网页看板实时显示
- 支持 gp5 / gp6 两种播放器上报通道

## 版本历史

- **v1.2 (当前)**: gp5/gp6 智能模式识别、is_end 失败重试、稳定性修复
- **v1.1**: gp5 播放器上报、续学、扫描优化、预计时长显示
- **v1.0**: 初始版本，仅支持 gp6 播放器

## 环境要求

- Python 3.10+
- Flask
- requests

## 安装

```bash
cd chinahrt_web_patch
python -m venv venv
source venv/bin/activate
pip install flask requests
```

## 运行

```bash
./venv/bin/python app.py
```

浏览器访问 http://127.0.0.1:5678

## 使用

1. 输入域名（默认 gp.chinahrt.com）、用户名、密码
2. 点击获取验证码，输入
3. 点击登录，自动扫描课程列表
4. 勾选要学习的课程，点击开始学习
5. 进度看板实时显示学习进度
6. 可随时停止，下次继续从已学位置学习

## 技术细节

### 双播放器支持

chinahrt 平台存在两种播放器，上报机制不同：

| | gp6（旧版） | gp5（新版） |
|---|---|---|
| 上报接口 | /videoPlay/takeRecordByToken | /videoPlay/takeRecord |
| 数据编码 | base60 + HMAC signature | 普通 form POST |
| token 格式 | 32位 hex，单引号 | 10位短串，双引号 |
| 额外参数 | token, time, timestamp | studyCode, recordId, signId, sectionId, businessId |
| token 激活 | GET /videoPlay/token_scope/{token} | GET /videoPlay/token_scope/{token} |

脚本自动检测播放器类型：检测 HTML 中是否包含 `takeRecord` 的 ajax 调用（而非通过 signId/studyCode 判断，因为部分 gp6 课程也有这些字段）。

### 续学机制

- gp5 课程：从播放页 HTML 中的 lastPlayTime 续学
- gp6 课程：从章节详情中的 study_time 续学
- 上报间隔 28 秒，与真实播放时间同步
- 续学时基准时间偏移修正，避免卡死在错误等待时长

### 扫描优化

- 课程列表扫描只获取课程基本信息（名称、进度百分比、时长），不逐个获取章节详情
- 120 门课程 3 秒内完成（之前需数分钟）
- 章节详情在开始学习时才获取
- 预计时长从课程 API 的 duration 字段解析（HH:MM:SS → 秒）

### is_end 上报保护

章节学完后的结束上报（is_end=True）失败时会自动重试一次，并打印响应日志。避免因网络抖动导致进度无法记录。

### 章节未完成检测

- studyProcess < 100（进度百分比未满）
- 或 study_status != "已学完"（即使进度显示100%，状态标记不为已完成也重新学习）

## 网络重试

所有 HTTP 请求自动重试 3 次（退避 0.5 秒），应对 chinahrt 服务器连接重置。使用 requests.Session 的 HTTPAdapter 挂载配置。

## 文件说明

```
app.py        Flask 后端（多账户管理、SSE 推送、扫描逻辑）
learner.py    核心学习逻辑（登录、扫描、上报）
templates/    前端页面
debug_api.py  调试工具
Dockerfile    Docker 部署配置
```

## 部署

### 本地运行

```bash
./venv/bin/python app.py
```

### NAS 部署（Docker）

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir flask requests
EXPOSE 5678
CMD ["python", "app.py"]
```

```bash
docker build -t chinahrt_web .
docker run -d --name chinahrt_web --restart unless-stopped -p 5678:5678 chinahrt_web
```
