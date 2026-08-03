# -*- coding: utf-8 -*-
"""
上传服务器代码到 GitHub 仓库（在 PowerShell 中运行）
支持更新已有文件（自动获取 sha）。
用法：
    $env:GH_TOKEN="你的令牌"
    & "python完整路径" 本文件路径/upload.py
"""
import base64
import json
import os
import sys
import urllib.request

REPO = "a13702045533-sketch/three-players-server"
FILES = ["main.py", "game_logic.py", "scoring.py", "requirements.txt", "render.yaml", "bot_ai.py"]
DIR = os.path.dirname(os.path.abspath(__file__))


def get_sha(token, f):
    """查询远程文件是否已存在，返回 sha 或 None。"""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/contents/{f}",
        headers={"Authorization": "token " + token,
                 "Accept": "application/vnd.github+json"},
    )
    try:
        resp = urllib.request.urlopen(req)
        data = json.loads(resp.read())
        return data.get("sha")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None   # 文件不存在
        raise


def main():
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        print("❌ 请先设置令牌：$env:GH_TOKEN='你的令牌'")
        sys.exit(1)

    ok = True
    for f in FILES:
        path = os.path.join(DIR, f)
        with open(path, "rb") as fh:
            content = base64.b64encode(fh.read()).decode()
        sha = get_sha(token, f)
        payload = {"message": "update " + f, "content": content}
        if sha:
            payload["sha"] = sha
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/contents/{f}",
            data=body,
            method="PUT",
            headers={
                "Authorization": "token " + token,
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
        )
        try:
            resp = urllib.request.urlopen(req)
            print(f"✅ {f}: 上传{'更新' if sha else '新建'}成功")
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:200]
            print(f"❌ {f}: 失败 - {detail}")
            ok = False
        except Exception as e:
            print(f"❌ {f}: 失败 - {e}")
            ok = False

    if ok:
        print("\n🎉 全部上传成功！")
    else:
        print("\n⚠️ 有文件上传失败。")


if __name__ == "__main__":
    main()
