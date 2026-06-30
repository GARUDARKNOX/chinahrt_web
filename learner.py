"""
learner.py - chinahrt.com 核心学习逻辑
支持: 登录(验证码) / 课程扫描 / 多线程学习(进度回调+停止)
"""

import base64
import hashlib
import hmac
import json
import re
import time
import uuid

import requests

VIDEO_ADMIN_URL = "https://videoadmin.chinahrt.com"
CHARSET60 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"[:60]
HMAC_KEY_PREFIX = "chaXs2--c"
REPORT_INTERVAL = 28

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}


def _configure_session(session: requests.Session):
    """为 session 配置自动重试，应对 chinahrt 服务器连接重置"""
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)


class StoppedByUser(Exception):
    pass


def base60_encode(text: str) -> str:
    byte_array = text.encode("utf-8")
    num = int.from_bytes(byte_array, "big")
    if num == 0:
        return CHARSET60[0]
    result = ""
    while num > 0:
        num, rem = divmod(num, 60)
        result = CHARSET60[rem] + result
    return result


def compute_signature(token: str, time_val: int, timestamp: int) -> str:
    key = HMAC_KEY_PREFIX + token[1:5]
    message = f"{token}{time_val}{timestamp}"
    sig_bytes = hmac.new(key.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(sig_bytes).decode()


# ============================================================
# 登录器
# ============================================================
class ChinaHrtLogin:
    # lczj 等门户域名没有 /gp6/ API，统一走 gp.chinahrt.com
    _API_DOMAIN_MAP = {
        "lczj.chinahrt.com": "gp.chinahrt.com",
    }

    def __init__(self, domain: str = "gp.chinahrt.com"):
        self.domain = domain
        # 实际 API 域名：lczj 等门户域名映射到 gp.chinahrt.com
        api_domain = self._API_DOMAIN_MAP.get(domain, domain)
        self.base_url = f"https://{api_domain}"
        self.session = requests.Session()
        _configure_session(self.session)
        self.session.headers.update(COMMON_HEADERS)
        self.session.headers["Referer"] = f"https://{api_domain}/index.html"
        self._captcha_random = ""

    def fetch_captcha(self) -> bytes:
        """下载验证码图片，返回 JPEG bytes"""
        self._captcha_random = str(int(time.time() * 1000))
        url = f"{self.base_url}/gp6/system/manager/kaptcha"
        resp = self.session.get(url, params={"d": self._captcha_random})
        return resp.content

    def login(self, username: str, password: str, captcha: str,
              platform_id: str = "") -> dict:
        """登录，返回 {token, platform_id, user_name}"""
        md5_pw = hashlib.md5(password.encode("utf-8")).hexdigest()

        if not platform_id:
            platform_id = self._guess_platform_id()

        login_data = {
            "userName": username,
            "password": md5_pw,
            "captcha": captcha or "cl000000",
            "platformId": platform_id,
            "from": "1",
        }

        resp = self.session.post(
            f"{self.base_url}/gp6/system/manager/login/valid",
            data=login_data,
            headers={
                "Hrt-Random": self._captcha_random,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        result = resp.json()

        if result.get("code") != "0":
            return {"ok": False, "msg": result.get("msg", "未知错误")}

        token = result.get("data", "")
        if not token:
            return {"ok": False, "msg": "登录成功但未返回token"}

        # 获取会话信息
        self.session.headers["hrttoken"] = token
        session_info = self._get_session(token)
        real_pid = str(session_info.get("platformId", platform_id or ""))
        user_name = session_info.get("name", session_info.get("userName", username))

        return {
            "ok": True,
            "token": token,
            "platform_id": real_pid,
            "user_name": user_name,
        }

    def _guess_platform_id(self) -> str:
        try:
            resp = self.session.get(f"{self.base_url}/index.html")
            m = re.search(r'platformId["\']?\s*[:=]\s*["\']?(\d+)', resp.text)
            if m:
                return m.group(1)
        except Exception:
            pass
        return ""

    def _get_session(self, token: str) -> dict:
        try:
            resp = self.session.get(
                f"{self.base_url}/gp6/system/stu/user/getSession",
                params={"t": str(uuid.uuid4())},
            )
            data = resp.json()
            if data.get("code") == "0":
                return data.get("data", {})
        except Exception:
            pass
        return {}


# ============================================================
# 学习器
# ============================================================
class ChinaHrtLearner:
    def __init__(self, token: str, platform_id: str, base_url: str,
                 on_progress=None, stop_event=None):
        self.token = token
        self.platform_id = platform_id
        self.base_url = base_url
        self.on_progress = on_progress    # callable(event_type, data_dict)
        self.stop_event = stop_event      # threading.Event

        self.session = requests.Session()
        _configure_session(self.session)
        self.session.headers.update(COMMON_HEADERS)
        self.session.headers["hrttoken"] = token
        self.session.headers["Referer"] = f"{base_url}/index.html"

    def _emit(self, event_type, data=None):
        if self.on_progress:
            self.on_progress(event_type, data or {})

    def _check_stop(self):
        if self.stop_event and self.stop_event.is_set():
            raise StoppedByUser()

    # ---- 课程数据 ----

    def get_trainplans(self) -> list:
        url = f"{self.base_url}/gp6/lms/stu/trainplan/list"
        params = {"platformId": self.platform_id, "isFinish": "0", "t": str(uuid.uuid4())}
        resp = self.session.get(url, params=params)
        data = resp.json()
        if data.get("code") != "0":
            # 尝试 isFinish=1 (已完成)
            params2 = {"platformId": self.platform_id, "isFinish": "1", "t": str(uuid.uuid4())}
            resp2 = self.session.get(url, params=params2)
            data2 = resp2.json()
            if data2.get("code") != "0":
                print(f"[trainplan] API error: {data}")
                return []
            data = data2

        raw = data.get("data")
        print(f"[trainplan] raw data type={type(raw).__name__}, preview={str(raw)[:300]}")

        # data 可能是 dict{listData: [...]} 或 dict{list: [...]} 或直接是 list
        if isinstance(raw, dict):
            items = raw.get("listData", raw.get("list", [])) or raw.get("data", []) or []
        elif isinstance(raw, list):
            items = raw
        else:
            items = []

        # items 里的元素可能是 dict 或 string(id)
        result = []
        for item in items:
            if isinstance(item, dict):
                result.append(item)
            elif isinstance(item, str):
                result.append({"id": item, "name": item})
        print(f"[trainplan] parsed {len(result)} trainplans")
        return result

    def get_courses(self, trainplan_id: str) -> list:
        url = f"{self.base_url}/gp6/lms/stu/trainplanCourseHandle/selected_course"
        params = {
            "trainplanId": trainplan_id,
            "platformId": self.platform_id,
            "curPage": "1",
            "pageSize": "100",
            "sortField": "1",
            "sortType": "DESC",
            "t": str(uuid.uuid4()),
        }
        resp = self.session.get(url, params=params)
        data = resp.json()
        if data.get("code") != "0":
            print(f"[courses] API error for tp={trainplan_id}: code={data.get('code')}, msg={data.get('msg','')}, full={str(data)[:500]}")
            return []
        raw = data.get("data")
        # 响应结构: data.courseStudyList = [...]
        if isinstance(raw, dict):
            items = raw.get("courseStudyList", []) or raw.get("listData", []) or raw.get("list", []) or []
        elif isinstance(raw, list):
            items = raw
        else:
            items = []
        # 规范化：确保每个 item 是 dict
        result = []
        for item in items:
            if isinstance(item, dict):
                result.append(item)
            elif isinstance(item, str):
                result.append({"courseId": item, "courseName": item})
        print(f"[courses] tp={trainplan_id}: {len(result)} courses")
        return result

    def get_course_detail(self, course_id: str, trainplan_id: str) -> dict:
        url = f"{self.base_url}/gp6/lms/stu/course/courseDetail"
        params = {
            "courseId": course_id,
            "trainplanId": trainplan_id,
            "platformId": self.platform_id,
            "t": str(uuid.uuid4()),
        }
        resp = self.session.get(url, params=params)
        data = resp.json()
        if data.get("code") != "0":
            raise RuntimeError(f"获取课程详情失败: {data.get('msg', '')}")
        return data["data"]

    def get_uncompleted_sections(self, course_id: str, trainplan_id: str) -> list:
        detail = self.get_course_detail(course_id, trainplan_id)
        print(f"[sections] detail keys={list(detail.keys()) if isinstance(detail, dict) else type(detail)}")
        
        # detail 可能是 {course: {chapter_list: [...]}} 或直接 {chapter_list: [...]}
        course_obj = detail.get("course", detail) if isinstance(detail, dict) else {}
        chapters = course_obj.get("chapter_list", []) or course_obj.get("chapterList", []) or detail.get("chapters", [])
        print(f"[sections] chapters={len(chapters)}")
        
        sections = []
        for ch in chapters:
            if not isinstance(ch, dict):
                continue
            ch_name = ch.get("name", ch.get("title", ""))
            sec_list = ch.get("section_list", []) or ch.get("sectionList", []) or ch.get("sections", [])
            for sec in sec_list:
                if not isinstance(sec, dict):
                    continue
                study_pct = sec.get("studyProcess", sec.get("study_process", sec.get("process", 0)))
                if study_pct < 100:
                    sections.append({
                        "id": str(sec.get("id", sec.get("sectionId", ""))),
                        "name": sec.get("name", sec.get("title", "")),
                        "total_time": int(sec.get("total_time", sec.get("totalTime", sec.get("duration", 0)))),
                        "chapter": ch_name,
                        "study_time": sec.get("study_time", sec.get("studyTime", 0)),
                    })
        print(f"[sections] uncompleted={len(sections)}")
        return sections

    # ---- 学习执行 ----

    def get_play_url(self, course_id: str, section_id: str, trainplan_id: str) -> str:
        url = f"{self.base_url}/gp6/lms/stu/course/playVideo"
        params = {
            "courseId": course_id,
            "sectionId": section_id,
            "trainplanId": trainplan_id,
            "platformId": self.platform_id,
            "t": str(uuid.uuid4()),
        }
        resp = self.session.get(url, params=params)
        data = resp.json()
        if data.get("code") != "0":
            raise RuntimeError(f"获取播放URL失败: {data.get('msg', '')}")
        return data["data"]["playUrl"]

    def get_video_params(self, play_url: str) -> dict:
        resp = self.session.get(play_url, headers={"Referer": f"{self.base_url}/"})
        html = resp.text
        # token 可能用单引号或双引号包裹，格式也可能不同（hex 或短串）
        take_token = re.search(r"token:\s*['\"]([^'\"]+)['\"]", html)
        total_time = re.search(r"total_time['\"']?\s*[:=]\s*([\d.]+)", html)
        if not take_token:
            snippet = html[:2000] if len(html) > 2000 else html
            raise RuntimeError(f"无法提取 take.token (html_len={len(html)}, snippet={snippet[:500]})")

        params = {
            "take_token": take_token.group(1),
            "total_time": int(float(total_time.group(1))) if total_time else 0,
            "play_url": play_url,
        }

        # 检测新版 gp5 播放器：HTML 中含 signId / studyCode / recordId 等 attrset 字段
        sign_id = re.search(r'signId["\']?\s*[:=]\s*["\']([^"\';\n,}]+)', html)
        if sign_id:
            # 新版 gp5 课程，提取完整上报参数
            def extract_attr(field):
                m = re.search(
                    rf'{field}["\']?\s*[:=]\s*["\']([^"\';\n,}}]+)', html)
                return m.group(1) if m else ""

            last_play = re.search(
                r"lastPlayTime['\"]?\s*[:=]\s*([\d.]+)", html)
            params["mode"] = "gp5"
            params["sign_id"] = sign_id.group(1)
            params["study_code"] = extract_attr("studyCode")
            params["record_id"] = extract_attr("recordId")
            params["section_id_v2"] = extract_attr("sectionId")
            params["business_id"] = extract_attr("businessId") or "gp5"
            params["update_redis_map"] = extract_attr("updateRedisMap") or "1"
            params["last_play_time"] = float(
                last_play.group(1)) if last_play else 0
            # gp5 的 total_time 从 lastPlayTime 无法得知，用 0，靠 override
            if not params["total_time"]:
                params["total_time"] = 0
        else:
            params["mode"] = "gp6"

        return params

    def activate_token(self, take_token: str, play_url: str):
        url = f"{VIDEO_ADMIN_URL}/videoPlay/token_scope/{take_token}"
        self.session.get(url, headers={"Referer": play_url})

    def report_progress(self, take_token, time_val, duration=None,
                        is_end=False, play_url="", params=None) -> dict:
        mode = (params or {}).get("mode", "gp6")

        if mode == "gp5":
            # 新版 gp5 播放器：普通 form POST 到 /videoPlay/takeRecord
            data = {
                "studyCode": params["study_code"],
                "recordUrl": params.get("record_url", ""),
                "updateRedisMap": params["update_redis_map"],
                "recordId": params["record_id"],
                "sectionId": params["section_id_v2"],
                "signId": params["sign_id"],
                "time": int(time_val),
                "businessId": params["business_id"],
                "token": take_token,
            }
            resp = self.session.post(
                f"{VIDEO_ADMIN_URL}/videoPlay/takeRecord",
                data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": VIDEO_ADMIN_URL,
                    "Referer": play_url or f"{VIDEO_ADMIN_URL}/",
                },
            )
            result = resp.json()
            new_token = result.get("data", "")
            return {"token": new_token or take_token, "response": result}

        # 旧版 gp6 播放器：base60 编码 + signature
        timestamp = int(time.time() * 1000)
        data = {
            "token": take_token,
            "time": int(time_val),
            "timestamp": timestamp,
        }
        if is_end:
            data["duration"] = int(duration)
            data["isEnd"] = "true"
        data["signature"] = compute_signature(take_token, data["time"], timestamp)
        encoded = base60_encode(json.dumps(data, separators=(",", ":")))
        resp = self.session.post(
            f"{VIDEO_ADMIN_URL}/videoPlay/takeRecordByToken",
            data=encoded,
            headers={
                "Content-Type": "text/plain;charset=UTF-8",
                "Origin": VIDEO_ADMIN_URL,
                "Referer": play_url or f"{VIDEO_ADMIN_URL}/",
            },
        )
        result = resp.json()
        new_token = result.get("data", "")
        return {"token": new_token or take_token, "response": result}

    def learn_section(self, course_id, section_id, trainplan_id,
                      section_name="", total_time_override=None,
                      study_time_override=None) -> bool:
        self._check_stop()

        print(f"[learn] === section start: {section_name} (course={course_id}, sec={section_id}, tp={trainplan_id}) ===", flush=True)
        self._emit("step", {"step": "获取播放URL", "section": section_name})
        play_url = self.get_play_url(course_id, section_id, trainplan_id)
        print(f"[learn] play_url={play_url[:100]}", flush=True)

        self._emit("step", {"step": "提取视频参数", "section": section_name})
        params = self.get_video_params(play_url)
        take_token = params["take_token"]
        total_time = total_time_override or params["total_time"]
        print(f"[learn] take_token={take_token[:20]}... total_time={total_time}", flush=True)

        self._emit("step", {"step": "激活token", "section": section_name})
        self.activate_token(take_token, play_url)
        print(f"[learn] token activated", flush=True)

        # 从已学位置继续，避免从0重新开始浪费时间
        if params.get("mode") == "gp5":
            current_time = int(params.get("last_play_time", 0))
        else:
            # gp6: 用 study_time_override (来自章节详情)
            current_time = int(study_time_override or 0)
        print(f"[learn] resume from {current_time}s / {total_time}s", flush=True)
        # 基准时间偏移 current_time，使 wait_needed = REPORT_INTERVAL（而非 current_time）
        token_issue_time = time.time() - current_time

        while current_time < total_time:
            self._check_stop()

            current_time = min(current_time + REPORT_INTERVAL, total_time)
            elapsed = time.time() - token_issue_time
            wait_needed = current_time - elapsed

            if wait_needed > 0:
                self._emit("waiting", {
                    "section": section_name,
                    "current": current_time,
                    "total": total_time,
                    "wait": int(wait_needed),
                })
                # 分段等待，便于及时响应停止
                while wait_needed > 0:
                    self._check_stop()
                    sleep_chunk = min(wait_needed, 5)
                    time.sleep(sleep_chunk)
                    wait_needed -= sleep_chunk
            else:
                self._emit("reporting", {
                    "section": section_name,
                    "current": current_time,
                    "total": total_time,
                })

            resp = self.report_progress(take_token, current_time, play_url=play_url, params=params)
            take_token = resp["token"]

            self._emit("progress", {
                "section": section_name,
                "current": current_time,
                "total": total_time,
            })

        # 结束请求
        self._check_stop()
        elapsed = time.time() - token_issue_time
        wait_needed = total_time - elapsed + 1
        if wait_needed > 0:
            while wait_needed > 0:
                self._check_stop()
                time.sleep(min(wait_needed, 5))
                wait_needed -= 5

        self.report_progress(take_token, total_time, duration=total_time,
                             is_end=True, play_url=play_url, params=params)
        self._emit("section_done", {"section": section_name, "total": total_time})
        return True

    def learn_course(self, course_id, course_name, trainplan_id,
                     sections=None) -> dict:
        """学习一门课程的所有未完成章节"""
        self._check_stop()

        if not sections:
            sections = self.get_uncompleted_sections(course_id, trainplan_id)

        total = len(sections)
        done = 0
        failed = 0

        self._emit("course_start", {
            "course": course_name,
            "total_sections": total,
        })

        for i, sec in enumerate(sections, 1):
            self._check_stop()
            self._emit("section_start", {
                "course": course_name,
                "section": sec["name"],
                "index": i,
                "total": total,
                "section_time": sec["total_time"],
            })
            try:
                self.learn_section(
                    course_id, sec["id"], trainplan_id,
                    sec["name"], sec["total_time"], sec.get("study_time", 0)
                )
                done += 1
            except StoppedByUser:
                raise
            except Exception as e:
                failed += 1
                self._emit("error", {
                    "course": course_name,
                    "section": sec["name"],
                    "error": str(e),
                })
            time.sleep(2)

        self._emit("course_done", {
            "course": course_name,
            "done": done,
            "failed": failed,
        })
        return {"done": done, "failed": failed}
