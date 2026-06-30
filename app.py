"""
app.py - Flask 后端
多账户管理 + 多线程学习 + SSE 实时进度推送
"""

import base64
import json
import queue
import threading
import time
import uuid

from flask import Flask, render_template, request, jsonify, Response

from learner import ChinaHrtLogin, ChinaHrtLearner, StoppedByUser

app = Flask(__name__)

# ============================================================
# 全局状态
# ============================================================

# 账户库: account_id -> {info, login_obj, learner_obj, stop_event, thread, status}
ACCOUNTS = {}

# SSE 客户端队列列表
SSE_CLIENTS = []

# 全局事件锁
SSE_LOCK = threading.Lock()


def broadcast(event_type: str, data: dict):
    """向所有 SSE 客户端推送事件"""
    msg = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
    with SSE_LOCK:
        dead = []
        for i, q in enumerate(SSE_CLIENTS):
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(i)
        for i in reversed(dead):
            SSE_CLIENTS.pop(i)


def make_progress_callback(account_id: str, account_name: str):
    """为每个账户创建进度回调，推送到 SSE"""
    def callback(event_type, data):
        data["account_id"] = account_id
        data["account_name"] = account_name
        data["ts"] = time.time()
        broadcast(event_type, data)
    return callback


# ============================================================
# 账户操作
# ============================================================

def do_scan_courses(account_id: str) -> list:
    """扫描某账户所有未完成课程，返回扁平列表"""
    import traceback
    acct = ACCOUNTS[account_id]
    learner = acct["learner"]
    base_url = acct["base_url"]

    try:
        trainplans = learner.get_trainplans()
    except Exception as e:
        print(f"[scan] get_trainplans error: {e}")
        traceback.print_exc()
        return []

    print(f"[scan] found {len(trainplans)} trainplans for {acct['user_name']}")
    courses_flat = []

    for tp in trainplans:
        tp_id = tp.get("id", tp.get("trainplanId", tp.get("trainplan_id", "")))
        tp_name = tp.get("name", tp.get("trainplanName", tp.get("title", tp_id)))
        print(f"[scan] trainplan: {tp_name} ({tp_id})")

        try:
            courses = learner.get_courses(tp_id)
        except Exception as e:
            print(f"[scan] get_courses error: {e}")
            courses = []

        print(f"[scan]   courses: {len(courses)}")
        for c in courses:
            c_id = c.get("courseId", c.get("id", ""))
            c_name = c.get("courseName", c.get("name", ""))
            learn_pct = c.get("learnPercent", c.get("studyProcess", 0))

            # 从课程列表的 duration 字段解析总时长 (格式 "HH:MM:SS")
            duration_str = c.get("duration", "")
            total_sec = 0
            if duration_str and ":" in str(duration_str):
                parts = str(duration_str).split(":")
                try:
                    if len(parts) == 3:
                        total_sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                    elif len(parts) == 2:
                        total_sec = int(parts[0]) * 60 + int(parts[1])
                except ValueError:
                    pass

            # 不在扫描时获取章节详情（太慢且阻塞学习线程）
            # 章节详情在 learn_course 开始学习时才获取
            print(f"[scan]     {c_name} - {learn_pct}% - {duration_str}")
            courses_flat.append({
                "course_id": c_id,
                "course_name": c_name,
                "trainplan_id": tp_id,
                "trainplan_name": tp_name,
                "learn_pct": learn_pct,
                "uncompleted": 0 if int(learn_pct or 0) >= 100 else 1,
                "total_time": total_sec,
                "sections": [],
            })

    return courses_flat


def do_learn_thread(account_id: str, selected_courses: list):
    """学习线程主函数"""
    acct = ACCOUNTS[account_id]
    learner = acct["learner"]
    stop_event = acct["stop_event"]
    account_name = acct["user_name"]

    acct["status"] = "learning"
    broadcast("account_status", {
        "account_id": account_id,
        "account_name": account_name,
        "status": "learning",
    })

    total_courses = len(selected_courses)
    completed_courses = 0

    for i, course in enumerate(selected_courses, 1):
        if stop_event.is_set():
            break

        course_id = course["course_id"]
        course_name = course["course_name"]
        trainplan_id = course["trainplan_id"]
        sections = course.get("sections", [])

        broadcast("course_start", {
            "account_id": account_id,
            "account_name": account_name,
            "course": course_name,
            "course_index": i,
            "total_courses": total_courses,
            "total_sections": len(sections),
        })

        try:
            result = learner.learn_course(
                course_id, course_name, trainplan_id, sections
            )
            completed_courses += 1
            broadcast("course_done", {
                "account_id": account_id,
                "account_name": account_name,
                "course": course_name,
                "done": result["done"],
                "failed": result["failed"],
                "course_index": i,
                "total_courses": total_courses,
            })
        except StoppedByUser:
            broadcast("account_stopped", {
                "account_id": account_id,
                "account_name": account_name,
            })
            break
        except Exception as e:
            broadcast("error", {
                "account_id": account_id,
                "account_name": account_name,
                "course": course_name,
                "error": str(e),
            })

    acct["status"] = "idle"
    broadcast("account_status", {
        "account_id": account_id,
        "account_name": account_name,
        "status": "idle",
        "completed_courses": completed_courses,
        "total_courses": total_courses,
    })


# ============================================================
# API 路由
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/captcha", methods=["POST"])
def api_captcha():
    """获取验证码图片 (base64)"""
    data = request.json or {}
    domain = data.get("domain", "gp.chinahrt.com")

    login = ChinaHrtLogin(domain)
    img_bytes = login.fetch_captcha()

    # 缓存 login 对象，用 session_id 关联
    session_id = str(uuid.uuid4())
    # 存到临时区，登录时取回
    if not hasattr(api_captcha, "_cache"):
        api_captcha._cache = {}
    api_captcha._cache[session_id] = login

    # 清理过期缓存 (>5分钟)
    now = time.time()
    for k in list(api_captcha._cache.keys()):
        v = api_captcha._cache[k]
        if now - getattr(v, "_created", now) > 300:
            del api_captcha._cache[k]

    return jsonify({
        "image": base64.b64encode(img_bytes).decode(),
        "session_id": session_id,
    })


@app.route("/api/login", methods=["POST"])
def api_login():
    """登录"""
    data = request.json or {}
    username = data.get("username", "")
    password = data.get("password", "")
    captcha = data.get("captcha", "")
    domain = data.get("domain", "gp.chinahrt.com")
    session_id = data.get("session_id", "")

    if not username or not password:
        return jsonify({"ok": False, "msg": "请填写用户名和密码"})

    # 取回缓存的 login 对象 (保持验证码 session)
    login = None
    if session_id and hasattr(api_captcha, "_cache"):
        login = api_captcha._cache.pop(session_id, None)

    if not login:
        # 没有缓存，新建 (验证码可能失效，但部分平台不需要)
        login = ChinaHrtLogin(domain)
        # 需要先获取验证码 random
        login.fetch_captcha()

    result = login.login(username, password, captcha)
    if not result.get("ok"):
        return jsonify(result)

    token = result["token"]
    platform_id = result["platform_id"]
    user_name = result["user_name"]
    # lczj 等门户域名映射到 gp.chinahrt.com 做 API 调用
    api_domain = ChinaHrtLogin._API_DOMAIN_MAP.get(domain, domain)
    base_url = f"https://{api_domain}"

    account_id = str(uuid.uuid4())
    stop_event = threading.Event()

    learner = ChinaHrtLearner(
        token=token,
        platform_id=platform_id,
        base_url=base_url,
        on_progress=make_progress_callback(account_id, user_name),
        stop_event=stop_event,
    )

    ACCOUNTS[account_id] = {
        "id": account_id,
        "user_name": user_name,
        "domain": domain,
        "base_url": base_url,
        "token": token,
        "platform_id": platform_id,
        "login": login,
        "learner": learner,
        "stop_event": stop_event,
        "thread": None,
        "status": "idle",
        "courses": [],
    }

    # 立即扫描课程
    try:
        courses = do_scan_courses(account_id)
        ACCOUNTS[account_id]["courses"] = courses
    except Exception as e:
        import traceback
        print(f"[login] 扫描课程异常: {e}")
        traceback.print_exc()
        courses = []

    return jsonify({
        "ok": True,
        "account_id": account_id,
        "user_name": user_name,
        "domain": domain,
        "courses": courses,
    })


@app.route("/api/scan/<account_id>", methods=["POST"])
def api_scan(account_id):
    """重新扫描课程"""
    if account_id not in ACCOUNTS:
        return jsonify({"ok": False, "msg": "账户不存在"})

    courses = do_scan_courses(account_id)
    ACCOUNTS[account_id]["courses"] = courses
    return jsonify({"ok": True, "courses": courses})


@app.route("/api/start", methods=["POST"])
def api_start():
    """开始学习 (多账户并行)"""
    data = request.json or {}
    # selections: [{account_id, course_ids: [course_id, ...]}]
    selections = data.get("selections", [])

    if not selections:
        return jsonify({"ok": False, "msg": "请选择要学习的课程"})

    started = []
    for sel in selections:
        account_id = sel["account_id"]
        course_ids = sel.get("course_ids", [])

        if account_id not in ACCOUNTS:
            continue
        acct = ACCOUNTS[account_id]
        if acct["status"] == "learning":
            continue

        # 找到选中的课程
        all_courses = acct["courses"]
        selected = [c for c in all_courses if c["course_id"] in course_ids]
        if not selected:
            continue

        # 重置 stop_event
        acct["stop_event"].clear()

        # 启动线程
        t = threading.Thread(
            target=do_learn_thread,
            args=(account_id, selected),
            daemon=True,
        )
        acct["thread"] = t
        t.start()
        started.append(account_id)

    return jsonify({"ok": True, "started": started})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    """停止学习"""
    data = request.json or {}
    account_id = data.get("account_id")

    if account_id and account_id in ACCOUNTS:
        ACCOUNTS[account_id]["stop_event"].set()
        return jsonify({"ok": True})

    # 停止所有
    for acct in ACCOUNTS.values():
        if acct["status"] == "learning":
            acct["stop_event"].set()
    return jsonify({"ok": True})


@app.route("/api/accounts", methods=["GET"])
def api_accounts():
    """获取所有账户状态"""
    result = []
    for acct in ACCOUNTS.values():
        result.append({
            "id": acct["id"],
            "user_name": acct["user_name"],
            "domain": acct["domain"],
            "status": acct["status"],
            "course_count": len(acct["courses"]),
            "uncompleted_count": sum(1 for c in acct["courses"] if c["uncompleted"] > 0),
        })
    return jsonify(result)


@app.route("/api/events")
def api_events():
    """SSE 事件流"""
    q = queue.Queue(maxsize=1000)
    with SSE_LOCK:
        SSE_CLIENTS.append(q)

    def stream():
        # 发送连接成功事件
        yield 'data: {"type":"connected","data":{}}\n\n'
        while True:
            try:
                msg = q.get(timeout=15)
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield ": keepalive\n\n"

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    print("=" * 50)
    print("  chinahrt 自动学习 Web")
    print("  访问: http://localhost:5678")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5678, debug=True, threaded=True)
