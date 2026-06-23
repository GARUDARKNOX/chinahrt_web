# chinahrt_web — 继续教育自动学习工具

## 功能

- 多账户同时登录管理
- 自动扫描培训计划 / 课程 / 未完成章节
- 多线程并行学习，SSE 实时进度推送
- 支持 lczj / gp 域名自动映射
- 默认只显示未完成课程

## 本地运行

```bash
cd ~/Downloads/chinahrt_web
./venv/bin/python3 app.py
```

访问 http://localhost:5678

依赖: Flask, requests (项目自带 venv)

## NAS 部署

```bash
# 1. 上传文件
rsync -avz --exclude venv --exclude __pycache__ ~/Downloads/chinahrt_web/ \
  admin@fnos.noxon.top:/vol1/1000/chinahrt_web/

# 2. 安装依赖
ssh -p 4669 admin@fnos.noxon.top \
  'cd /vol1/1000/chinahrt_web && python3 -m venv venv && ./venv/bin/pip install flask requests'

# 3. 启动
ssh -p 4669 admin@fnos.noxon.top \
  'cd /vol1/1000/chinahrt_web && nohup ./venv/bin/python3 app.py > chinahrt_web.log 2>&1 &'
```

NAS 外网访问: https://fnos.noxon.top:4673

## 使用流程

1. 打开网页，输入身份证号、密码
2. 输入验证码，点击登录
3. 系统自动扫描所有培训计划和课程
4. 课程列表默认只显示未完成的课程
5. 勾选要学习的课程，点击"开始学习选中课程"
6. 右侧面板实时显示学习进度
7. 学完后点击"刷新所有课程"更新进度

## 文件结构

```
chinahrt_web/
├── app.py              # Flask 后端 (多账户+SSE+线程)
├── learner.py          # 核心学习逻辑 (登录/扫描/上报)
├── templates/
│   └── index.html      # 单页 Web UI
├── venv/               # Python 虚拟环境
└── README.md           # 本文档
```

## 技术要点

- **登录**: POST /gp6/system/manager/login/valid，密码 MD5 加密，需 Hrt-Random 头
- **课程列表**: GET /gp6/lms/stu/trainplanCourseHandle/selected_course，响应键 courseStudyList
- **反作弊**: 每 28 秒上报一次，必须真实等待。签名算法 HMAC-SHA256，数据 base60 编码
- **域名映射**: lczj.chinahrt.com → gp.chinahrt.com (lczj 是门户无 API)

## 已知限制 (v1.0)

1. Flask debug 模式 — 修改文件会自动重载并中断学习线程
2. 课程列表进度只在扫描时快照，学习过程中不实时更新
3. 无持久化 — 服务重启后需重新登录
4. 无开机自启动

## 版本

- v1.0 (2026-06-22): 初始版本
