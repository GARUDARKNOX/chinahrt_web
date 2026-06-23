#!/usr/bin/env python3
"""Debug: trace actual API responses"""
import sys
sys.path.insert(0, '/home/knox/Downloads/chinahrt_web')
from learner import ChinaHrtLogin, ChinaHrtLearner
import json

# 读取已登录的账户信息
import requests

# 先看前端到底拿到了什么
resp = requests.get('http://localhost:5678/api/accounts')
print("=== /api/accounts ===")
print(resp.text)

# 直接用已有的token测试 (从浏览器里抓)
# 先看login API实际返回什么
login = ChinaHrtLogin("gp.chinahrt.com")

# 不真正登录, 只看getSession和trainplan接口的行为
# 用一个假token看看返回什么
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://gp.chinahrt.com/index.html",
    "hrttoken": "FAKE_TOKEN_FOR_DEBUG",
})

base = "https://gp.chinahrt.com"

# Test trainplan list
print("\n=== trainplan/list with fake token ===")
r = session.get(f"{base}/gp6/lms/stu/trainplan/list", params={"platformId":"291","t":"test"})
print(f"Status: {r.status_code}")
print(f"Content-Type: {r.headers.get('Content-Type','')}")
print(f"Body[:500]: {r.text[:500]}")

# Test getSession
print("\n=== getSession ===")
r = session.get(f"{base}/gp6/system/stu/user/getSession", params={"t":"test"})
print(f"Status: {r.status_code}")
print(f"Content-Type: {r.headers.get('Content-Type','')}")
print(f"Body[:500]: {r.text[:500]}")

# Test course list
print("\n=== selectedCourseList ===")
r = session.get(f"{base}/gp6/lms/stu/course/selectedCourseList", params={"trainplanId":"test","platformId":"291","t":"test"})
print(f"Status: {r.status_code}")
print(f"Content-Type: {r.headers.get('Content-Type','')}")
print(f"Body[:500]: {r.text[:500]}")
