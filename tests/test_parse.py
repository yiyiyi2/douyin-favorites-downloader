# -*- coding: utf-8 -*-
"""测试 parse_aweme 对不同响应形状的处理, 以及最高画质选择逻辑。"""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from douyin_dl.core import parse_aweme, _iter_bit_rates, _is_watermarked, build_filename

# 模拟抖音真实返回: bit_rate 数组里从低到高排列, 且混有水印地址
SAMPLE = {
    "aweme_id": "7300000000000000001",
    "desc": "测试视频 / 带非法字符\\:*?\"<>| 的标题",
    "create_time": 1730000000,
    "author": {"nickname": "测试作者", "uid": "12345"},
    "music": {"title": "背景音乐", "author": "歌手"},
    "video": {
        "duration": 15000,
        "width": 720,
        "height": 1280,
        "cover": {"url_list": ["https://p3.douyinpic.com/cover.jpeg"]},
        "bit_rate": [
            {
                "gear_name": "low_540_0",
                "bit_rate": 800000,
                "quality_type": 20,
                "play_addr": {
                    "width": 576, "height": 1024, "data_size": 1500000,
                    "url_list": ["https://v3.douyin.com/low.mp4"],
                },
            },
            {
                "gear_name": "normal_720_0",
                "bit_rate": 1600000,
                "quality_type": 28,
                "play_addr": {
                    "width": 720, "height": 1280, "data_size": 3200000,
                    "url_list": ["https://v3.douyin.com/normal.mp4"],
                },
            },
            {
                "gear_name": "normal_1080_0",
                "bit_rate": 3200000,
                "quality_type": 30,
                "play_addr": {
                    "width": 1080, "height": 1920, "data_size": 6400000,
                    "url_list": ["https://v3.douyin.com/high.mp4"],
                },
            },
        ],
        # 水印地址, 必须被过滤掉
        "download_addr": {
            "width": 1080, "height": 1920,
            "url_list": ["https://v3.douyin.com/playwm/watermarked.mp4"],
        },
    },
}

info = parse_aweme(SAMPLE)
print("aweme_id      :", info.aweme_id)
print("author        :", info.author)
print("desc          :", info.desc)
print("duration(ms)  :", info.duration)
print("candidates    :", len(info.candidates))
for c in info.candidates:
    print(f"   {c.width}x{c.height}  bitrate={c.bitrate}  gear={c.gear_name}  score={c.score()}")

best = info.best()
print("\nBEST ->", best.width, "x", best.height, "bitrate", best.bitrate, "url", best.url)
assert best.url == "https://v3.douyin.com/high.mp4", "应选 1080p 最高画质"
assert "playwm" not in best.url, "不能选到水印地址"
assert len(info.candidates) == 3, f"水印地址应被过滤, 实际 {len(info.candidates)} 个候选"
print("文件名:", build_filename(info) + ".mp4")
print("文件名无非法字符:", not any(ch in build_filename(info) for ch in '\\/:*?"<>|'))

# --- 同分辨率不同码率时, 应选码率更高的 ---
SAME_RES = {
    "aweme_id": "2", "desc": "同分辨率",
    "video": {"bit_rate": [
        {"gear_name": "a", "bit_rate": 1000000,
         "play_addr": {"width": 1080, "height": 1920, "url_list": ["https://x/a.mp4"]}},
        {"gear_name": "b", "bit_rate": 5000000,
         "play_addr": {"width": 1080, "height": 1920, "url_list": ["https://x/b.mp4"]}},
    ]},
}
i2 = parse_aweme(SAME_RES)
print("\n同分辨率选最高码率 ->", i2.best().url, "bitrate", i2.best().bitrate)
assert i2.best().url == "https://x/b.mp4", "同分辨率应选码率更高的"

# --- 旧结构: 只有 play_addr ---
OLD = {
    "aweme_id": "3", "desc": "旧结构",
    "video": {"play_addr": {"width": 720, "height": 1280,
                            "url_list": ["https://x/old.mp4"]}},
}
i3 = parse_aweme(OLD)
print("旧结构候选:", len(i3.candidates), "->", i3.best().url if i3.best() else None)
assert i3.best() and i3.best().url == "https://x/old.mp4"

# --- 图集作品 ---
IMGS = {
    "aweme_id": "4", "desc": "图文",
    "images": [
        {"url_list": ["https://x/1.jpeg"]},
        {"url_list": ["https://x/2.jpeg"]},
    ],
}
i4 = parse_aweme(IMGS)
print("图文作品:", i4.is_image_post, "图片数:", len(i4.image_urls))
assert i4.is_image_post and len(i4.image_urls) == 2

# --- 无 bit_rate 时应回落到 video 上的宽高, 而不是 0x0 ---
NO_BR_DIM = {
    "aweme_id": "5", "desc": "无码率",
    "video": {"width": 1080, "height": 1920,
              "play_addr": {"url_list": ["https://x/c.mp4"]}},
}
i5 = parse_aweme(NO_BR_DIM)
print("无 bit_rate 时尺寸回落到 video 层:", i5.best().width, "x", i5.best().height)
assert i5.best().width == 1080, "应从 video 层继承宽高"

print("\n=== 全部解析测试通过 ===")
