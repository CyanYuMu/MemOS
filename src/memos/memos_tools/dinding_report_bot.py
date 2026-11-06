"""dinding_report_bot.py"""

import base64
import contextlib
import hashlib
import hmac
import json
import os
import time
import traceback
import urllib.parse

from datetime import datetime
from uuid import uuid4

from dotenv import load_dotenv

from memos.log import get_logger


logger = get_logger(__name__)


load_dotenv()

try:
    import io

    import matplotlib
    import matplotlib.font_manager as fm
    import numpy as np
    import oss2
    import requests

    from PIL import Image, ImageDraw, ImageFont

    matplotlib.use("Agg")
except ImportError as e:
    raise ImportError(
        f"DingDing bot dependencies not found: {e}. "
        "Please install required packages: pip install requests oss2 pillow matplotlib"
    ) from e

# =========================
# 🔧  common tools
# =========================
ACCESS_TOKEN_USER = os.getenv("DINGDING_ACCESS_TOKEN_USER")
SECRET_USER = os.getenv("DINGDING_SECRET_USER")
ACCESS_TOKEN_ERROR = os.getenv("DINGDING_ACCESS_TOKEN_ERROR")
SECRET_ERROR = os.getenv("DINGDING_SECRET_ERROR")
OSS_CONFIG = {
    "endpoint": os.getenv("OSS_ENDPOINT"),
    "region": os.getenv("OSS_REGION"),
    "bucket_name": os.getenv("OSS_BUCKET_NAME"),
    "oss_access_key_id": os.getenv("OSS_ACCESS_KEY_ID"),
    "oss_access_key_secret": os.getenv("OSS_ACCESS_KEY_SECRET"),
    "public_base_url": os.getenv("OSS_PUBLIC_BASE_URL"),
}
DING_APP_KEY = os.getenv("DINGDING_APP_KEY")
DING_APP_SECRET = os.getenv("DINGDING_APP_SECRET")
ENV_NAME = os.getenv("ENV_NAME", "PLAYGROUND_OFFLINE")

theme_map = {
    "ONLINE": {
        "color": "#2196F3",
        "grad": ("#E3F2FD", "#BBDEFB"),
        "emoji": "🩵",
    },
    "OFFLINE": {
        "color": "#FFC107",
        "grad": ("#FFF8E1", "#FFECB3"),
        "emoji": "🤍",
    },
}


# Get access_token


def get_access_token():
    url = f"https://oapi.dingtalk.com/gettoken?appkey={DING_APP_KEY}&appsecret={DING_APP_SECRET}"
    resp = requests.get(url)
    return resp.json()["access_token"]


def _pick_font(size: int = 48) -> ImageFont.ImageFont:
    """
    Try to find a font from the following candidates (macOS / Windows / Linux are common):
    Helvetica → Arial → DejaVu Sans
    If found, use truetype, otherwise return the default bitmap font.
    """
    candidates = ["Helvetica", "Arial", "DejaVu Sans"]
    for name in candidates:
        try:
            font_path = fm.findfont(name, fallback_to_default=False)
            return ImageFont.truetype(font_path, size)
        except Exception:
            continue
    # Cannot find truetype, fallback to default and manually scale up
    bitmap = ImageFont.load_default()
    return ImageFont.FreeTypeFont(bitmap.path, size) if hasattr(bitmap, "path") else bitmap


def make_header(
    title: str,
    subtitle: str,
    size=(1080, 260),
    colors=("#C8F6E1", "#E8F8F5"),  # Stylish mint green → lighter green
    fg="#00956D",
) -> bytes:
    """
    Generate a "Notification" banner with green gradient and bold large text.
    title: main title (suggested ≤ 35 characters)
    subtitle: sub title (e.g. "Notification")
    """

    # Can be placed inside or outside make_header

    def _text_wh(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont):
        """
        return (width, height), compatible with both Pillow old version (textsize) and new version (textbbox)
        """
        if hasattr(draw, "textbbox"):  # Pillow ≥ 8.0
            left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
            return right - left, bottom - top
        else:  # Pillow < 10.0
            return draw.textsize(text, font=font)

    w, h = size
    # --- 1) background gradient ---
    g = np.linspace(0, 1, w)
    grad = np.outer(np.ones(h), g)
    rgb0 = tuple(int(colors[0].lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    rgb1 = tuple(int(colors[1].lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    img = np.zeros((h, w, 3), dtype=np.uint8)
    for i in range(3):
        img[:, :, i] = rgb0[i] * (1 - grad) + rgb1[i] * grad
    im = Image.fromarray(img)

    # --- 2) text ---
    draw = ImageDraw.Draw(im)
    font_title = _pick_font(54)  # main title
    font_sub = _pick_font(30)  # sub title

    # center alignment
    title_w, title_h = _text_wh(draw, title, font_title)
    sub_w, _sub_h = _text_wh(draw, subtitle, font_sub)

    title_x = (w - title_w) // 2
    title_y = h // 2 - title_h
    sub_x = (w - sub_w) // 2
    sub_y = title_y + title_h + 8

    draw.text((title_x, title_y), title, fill=fg, font=font_title)
    draw.text((sub_x, sub_y), subtitle, fill=fg, font=font_sub)

    # --- 3) PNG bytes ---
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _sign(secret: str, ts: str):
    s = f"{ts}\n{secret}"
    return urllib.parse.quote_plus(
        base64.b64encode(hmac.new(secret.encode(), s.encode(), hashlib.sha256).digest())
    )


def _send_md(title: str, md: str, type="user", at=None):
    if type == "user":
        access_token = ACCESS_TOKEN_USER
        secret = SECRET_USER
    else:
        access_token = ACCESS_TOKEN_ERROR
        secret = SECRET_ERROR
    ts = str(round(time.time() * 1000))
    url = (
        f"https://oapi.dingtalk.com/robot/send?access_token={access_token}"
        f"&timestamp={ts}&sign={_sign(secret, ts)}"
    )
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": md},
        "at": at or {"atUserIds": [], "isAtAll": False},
    }
    requests.post(url, headers={"Content-Type": "application/json"}, data=json.dumps(payload))


# ------------------------- OSS -------------------------


def upload_bytes_to_oss(
    data: bytes,
    oss_dir: str = "xcy-share/jfzt/",
    filename: str | None = None,
    keep_latest: int = 1,  # Keep latest N files; 0 = delete all
) -> str:
    """
    -  If filename_prefix is provided, delete the older files in {oss_dir}/{prefix}_*.png, only keep the latest keep_latest files
    -  Always create <prefix>_<timestamp>_<uuid>.png → ensure the URL is unique
    """
    filename_prefix = filename

    conf = OSS_CONFIG
    auth = oss2.Auth(conf["oss_access_key_id"], conf["oss_access_key_secret"])
    bucket = oss2.Bucket(auth, conf["endpoint"], conf["bucket_name"])

    # ---------- delete old files ----------
    if filename_prefix and keep_latest >= 0:
        prefix_path = f"{oss_dir.rstrip('/')}/{filename_prefix}_"
        objs = bucket.list_objects(prefix=prefix_path).object_list
        old_files = [(o.key, o.last_modified) for o in objs if o.key.endswith(".png")]
        if old_files and len(old_files) > keep_latest:
            # sort by last_modified from new to old
            old_files.sort(key=lambda x: x[1], reverse=True)
            to_del = [k for k, _ in old_files[keep_latest:]]
            for k in to_del:
                with contextlib.suppress(Exception):
                    bucket.delete_object(k)

    # ---------- upload new file ----------
    ts = int(time.time())
    uniq = uuid4().hex
    prefix = f"{filename_prefix}_" if filename_prefix else ""
    object_name = f"{oss_dir.rstrip('/')}/{prefix}{ts}_{uniq}.png"
    bucket.put_object(object_name, data)

    return f"{conf['public_base_url'].rstrip('/')}/{object_name}"


# --------- Markdown Table Helper ---------


# --------- online_bot ---------
# ---------- Convert dict → colored KV lines ----------


def _kv_lines(d: dict, emoji: str = "", heading: str = "", heading_color: str = "#00956D") -> str:
    """
    Returns:
    ### 📅 <font color='#00956D'><b>Daily Summary</b></font>
    - **Request count:** 1364
    ...
    """
    parts = [f"### {emoji} <font color='{heading_color}' size='3'><b>{heading}</b></font>"]
    parts += [f"- **{k}:** {v}" for k, v in d.items()]
    return "\n".join(parts)


# -------------- online_bot(colored title version) -----------------


def online_bot(
    header_name: str,
    sub_title_name: str,
    title_color: str,
    other_data1: dict,
    other_data2: dict,
    emoji: dict,
):
    try:
        logger.info("in online bot")
        theme = "OFFLINE" if "OFFLINE" in ENV_NAME or "TEST" in ENV_NAME else "ONLINE"
        style = theme_map.get(theme, theme_map["OFFLINE"])
        heading_color = style["color"]  # Use theme color for subtitle

        # 0) Banner
        banner_bytes = make_header(
            header_name,
            sub_title_name,
            colors=style["grad"],
            fg=style["color"],
        )
        banner_url = upload_bytes_to_oss(banner_bytes, filename=f"{ENV_NAME}_online_report.png")

        # 1) Colored main title
        colored_title = f"<font color='{style['color']}' size='4'><b>{ENV_NAME}</b></font>"

        # 3) Markdown
        md = "\n\n".join(
            filter(
                None,
                [
                    f"![banner]({banner_url})",
                    f"### {style['emoji']} <font color='{heading_color}' size='4'><b>{colored_title}</b></font>\n\n",
                    _kv_lines(
                        other_data1,
                        next(iter(emoji.keys())),
                        next(iter(emoji.values())),
                        heading_color=heading_color,
                    ),
                    _kv_lines(
                        other_data2,
                        list(emoji.keys())[1],
                        list(emoji.values())[1],
                        heading_color=heading_color,
                    ),
                    f"<font color='#9E9E9E' size='1'>Time: "
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</font>\n",
                ],
            )
        )

        _send_md(colored_title, md, type="user")
    except Exception:
        logger.error(traceback.format_exc())


if __name__ == "__main__":
    other_data = {
        "recent_overall_data": "what is memos",
        "site_data": "**📊 Simulated content\nLa la la <font color='red'>320</font>hahaha<font "
        "color='red'>155</font>",
    }

    online_bot(
        header_name="TextualMemory",  # must in English
        sub_title_name="Search",  # must in English
        title_color="#00956D",
        other_data1={"Retrieval source 1": "This is plain text memory retrieval content blablabla"},
        other_data2=other_data,
        emoji={"Plain text memory retrieval source": "😨", "Retrieval content": "🕰🐛"},
    )
    print("All messages sent successfully")
