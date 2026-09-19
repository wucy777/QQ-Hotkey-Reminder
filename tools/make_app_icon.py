# -*- coding: utf-8 -*-
"""生成应用图标：app.ico（exe/窗口图标）与 app_icon.png（界面标题栏用）。

构建期一次性运行：python tools/make_app_icon.py
"""
import os

from PIL import Image, ImageDraw

SIZE = 256


def main():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # 圆角矩形背景 + 蓝色垂直渐变
    grad = Image.new("RGBA", (SIZE, SIZE))
    gd = ImageDraw.Draw(grad)
    top, bot = (59, 130, 246), (29, 78, 216)
    for y in range(SIZE):
        t = y / (SIZE - 1)
        gd.line([(0, y), (SIZE - 1, y)],
                fill=(round(top[0] + (bot[0] - top[0]) * t),
                      round(top[1] + (bot[1] - top[1]) * t),
                      round(top[2] + (bot[2] - top[2]) * t), 255))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([10, 10, SIZE - 10, SIZE - 10], radius=58, fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    # 白色对话气泡 + 左下尾巴
    d.rounded_rectangle([54, 62, 202, 170], radius=34, fill=(255, 255, 255, 255))
    d.polygon([(84, 164), (84, 208), (132, 164)], fill=(255, 255, 255, 255))
    # 气泡里三个蓝点（表示"名单逐个输入"）
    for cx in (94, 128, 162):
        d.ellipse([cx - 11, 104, cx + 11, 126], fill=(37, 99, 235, 255))

    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    img.save(os.path.join(out, "app.ico"),
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    img.save(os.path.join(out, "app_icon.png"))
    print("app.ico / app_icon.png written to", out)


if __name__ == "__main__":
    main()
